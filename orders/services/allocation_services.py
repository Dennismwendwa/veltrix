from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import Warehouse
from orders.models import OnlineOrder
from orders.services.online_order import OnlineOrderService


class BranchOrderAllocationService:
    """
    Allocate available branch stock to existing online orders.

    Run this after the stock receipt or transfer transaction has committed.
    Orders are processed oldest first.
    """

    ACTIVE_ORDER_STATUSES = (
        OnlineOrder.Status.PLACED,
        OnlineOrder.Status.CONFIRMED,
        OnlineOrder.Status.PROCESSING,
    )

    @transaction.atomic
    def allocate_for_warehouse(self, *, warehouse_id):
        warehouse = Warehouse.objects.filter(
            pk=warehouse_id,
            is_active=True,
            type=Warehouse.Type.BRANCH,
        ).first()

        if warehouse is None:
            raise ValidationError(
                "An active branch warehouse must be selected."
            )

        now = timezone.now()

        # Process orders that are still eligible for allocation.
        order_ids = list(
            OnlineOrder.objects.filter(
                fulfilment_warehouse=warehouse,
                status__in=self.ACTIVE_ORDER_STATUSES,
            )
            .filter(
                Q(expires_at__isnull=True) | Q(expires_at__gt=now)
            )
            .order_by("created_at", "pk")
            .values_list("pk", flat=True)
        )

        results = []

        for order_id in order_ids:
            result = self._allocate_order(order_id)

            if result is not None:
                results.append(result)

        return {
            "warehouse_id": warehouse.pk,
            "orders_processed": len(results),
            "orders_with_shortages": sum(
                result["has_shortage"] for result in results
            ),
            "orders": results,
        }

    def _allocate_order(self, order_id):
        service = OnlineOrderService()

        # The underlying service locks the order and stock balances.
        order = service.reserve_newly_available_stock(
            order_id=order_id,
        )

        outstanding = Decimal("0.000")

        for item in order.items.all():
            outstanding += item.quantity_to_source

        return {
            "order_id": order.pk,
            "order_number": order.number,
            "has_shortage": outstanding > Decimal("0.000"),
            "quantity_still_to_source": outstanding,
        }
