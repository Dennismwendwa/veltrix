from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Q, Sum

from catalog.models import Product
from core.models import Warehouse
from inventory.models import (
    StockBalance,
    StockTransfer,
    StockTransferItem,
)
from orders.models import (
    OnlineOrder,
    OnlineOrderItem,
    StockReservation,
)


ZERO = Decimal("0.000")


class BranchReplenishmentService:
    """
    Suggest replenishment quantities for a branch warehouse.

    Suggestions are based on:
    - Outstanding demand from orders being processed.
    - Available stock at the destination branch.
    - Available stock at the master warehouse.
    - Existing approved and in-transit stock transfers.

    This service is read-only. It does not create transfers,
    approve transfers, dispatch stock, or modify reservations.
    """

    ACTIVE_ORDER_STATUSES = (
        OnlineOrder.Status.PROCESSING,
    )

    PIPELINE_TRANSFER_STATUSES = (
        StockTransfer.Status.APPROVED,
        StockTransfer.Status.DISPATCHED,
        StockTransfer.Status.PARTIALLY_RECEIVED,
    )

    def __init__(self, *, branch_warehouse_id):
        self.branch_warehouse_id = branch_warehouse_id

    def suggest(self):
        """Return suggested replenishment quantities for a branch."""

        branch_warehouse = self._get_branch_warehouse()
        master_warehouse = self._get_master_warehouse()

        demand_by_product = self._get_outstanding_demand(
            branch_warehouse_id=branch_warehouse.pk,
        )

        branch_stock = self._get_available_stock(
            warehouse_id=branch_warehouse.pk,
        )

        master_stock = self._get_master_available_stock(
            master_warehouse_id=master_warehouse.pk,
        )

        pipeline = self._get_transfer_pipeline(
            source_warehouse_id=master_warehouse.pk,
            destination_warehouse_id=branch_warehouse.pk,
        )

        product_ids = set(demand_by_product) | set(pipeline)
        products = Product.objects.in_bulk(product_ids)

        suggestions = []

        for product_id in product_ids:
            product = products.get(product_id)

            if product is None:
                continue

            outstanding_demand = demand_by_product.get(
                product_id, ZERO
            )

            branch_available = branch_stock.get(
                product_id, ZERO
            )

            master_available = master_stock.get(
                product_id, ZERO
            )

            pipeline_quantity = pipeline.get(
                product_id, ZERO
            )

            # Demand not covered by available branch stock.
            shortage = max(
                ZERO,
                outstanding_demand - branch_available,
            )

            # Do not recommend quantities already covered by
            # approved or in-transit transfers to this branch.
            remaining_shortage = max(
                ZERO,
                shortage - pipeline_quantity,
            )

            # Never suggest more than the currently available
            # master stock after accounting for approved transfers.
            suggested_quantity = min(
                remaining_shortage,
                master_available,
            )

            if suggested_quantity <= ZERO:
                continue

            suggestions.append({
                "product_id": product_id,
                "product": product,
                "outstanding_demand": outstanding_demand,
                "branch_available": branch_available,
                "master_available": master_available,
                "shortage": shortage,
                "in_transfer_pipeline": pipeline_quantity,
                "suggested_quantity": suggested_quantity,
                "uncovered_shortage": max(
                    ZERO,
                    remaining_shortage - suggested_quantity,
                ),
            })

        suggestions.sort(
            key=lambda item: item["product"].name.lower()
        )

        return {
            "branch_warehouse": branch_warehouse,
            "master_warehouse": master_warehouse,
            "suggestions": suggestions,
        }

    def _get_branch_warehouse(self):
        try:
            warehouse = Warehouse.objects.get(
                pk=self.branch_warehouse_id,
            )
        except Warehouse.DoesNotExist as exc:
            raise ValidationError(
                "The requested branch warehouse does not exist."
            ) from exc

        if (
            warehouse.warehouse_type
            != Warehouse.WarehouseType.BRANCH
        ):
            raise ValidationError(
                "The destination must be a branch warehouse."
            )

        return warehouse

    @staticmethod
    def _get_master_warehouse():
        try:
            return Warehouse.objects.get(
                warehouse_type=Warehouse.WarehouseType.MASTER,
            )
        except Warehouse.DoesNotExist as exc:
            raise ValidationError(
                "A master warehouse has not been configured."
            ) from exc

    def _get_outstanding_demand(self, *, branch_warehouse_id):
        """
        Aggregate unreserved demand for orders being processed
        at the specified fulfilment warehouse.
        """

        items = (
            OnlineOrderItem.objects
            .filter(
                order__status__in=self.ACTIVE_ORDER_STATUSES,
                order__fulfilment_warehouse_id=branch_warehouse_id,
            )
            .annotate(
                reserved_quantity=Sum(
                    "reservations__quantity",
                    filter=Q(
                        reservations__status=(
                            StockReservation.Status.ACTIVE
                        ),
                        reservations__warehouse_id=(
                            branch_warehouse_id
                        ),
                    ),
                )
            )
            .values(
                "product_id",
                "quantity",
                "reserved_quantity",
            )
        )

        demand = defaultdict(lambda: ZERO)

        for item in items:
            reserved = item["reserved_quantity"] or ZERO

            outstanding = max(
                ZERO,
                item["quantity"] - reserved,
            )

            demand[item["product_id"]] += outstanding

        return dict(demand)

    @staticmethod
    def _get_available_stock(*, warehouse_id):
        """Return available stock grouped by product."""

        balances = StockBalance.objects.filter(
            warehouse_id=warehouse_id,
        ).values(
            "product_id",
            "quantity_on_hand",
            "quantity_reserved",
        )

        return {
            row["product_id"]: (
                row["quantity_on_hand"]
                - row["quantity_reserved"]
            )
            for row in balances
        }

    def _get_master_available_stock(self, *, master_warehouse_id):
        """
        Exclude quantities committed to approved transfers.

        Dispatched transfers are already deducted from master
        on-hand stock by StockTransferService.
        """

        available = self._get_available_stock(
            warehouse_id=master_warehouse_id,
        )

        committed_items = (
            StockTransferItem.objects
            .filter(
                transfer__source_warehouse_id=master_warehouse_id,
                transfer__status=StockTransfer.Status.APPROVED,
            )
            .values("product_id")
            .annotate(
                committed_quantity=Sum("quantity_requested"),
            )
        )

        for item in committed_items:
            product_id = item["product_id"]

            committed = (
                item["committed_quantity"] or ZERO
            )

            available[product_id] = max(
                ZERO,
                available.get(product_id, ZERO) - committed,
            )

        return available

    @staticmethod
    def _get_transfer_pipeline(
        *,
        source_warehouse_id,
        destination_warehouse_id,
    ):
        """
        Return quantities already approved or in transit
        from the master warehouse to this branch.
        """

        transfers = (
            StockTransfer.objects
            .filter(
                source_warehouse_id=source_warehouse_id,
                destination_warehouse_id=destination_warehouse_id,
                status__in=(
                    BranchReplenishmentService
                    .PIPELINE_TRANSFER_STATUSES
                ),
            )
            .prefetch_related("items")
        )

        pipeline = defaultdict(lambda: ZERO)

        for transfer in transfers:
            for item in transfer.items.all():
                if (
                    transfer.status
                    == StockTransfer.Status.APPROVED
                ):
                    quantity = item.quantity_requested
                else:
                    quantity = item.quantity_outstanding

                pipeline[item.product_id] += quantity

        return dict(pipeline)
