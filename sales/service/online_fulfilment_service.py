from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from inventory.models import StockBalance, StockMovement
from orders.models import (
    OnlineOrder,
    OnlineOrderItem,
    OrderPayment,
    StockReservation,
)
from sales.models import Sale, SaleItem


class OnlineFulfilmentService:
    """
    Completes an online order and records its sale and stock movements.

    The caller must provide the authenticated staff member performing
    the fulfilment operation.
    """

    def __init__(self, user):
        if user is None or not getattr(user, "is_authenticated", False):
            raise ValidationError(
                "An authenticated user is required to complete fulfilment."
            )

        self.user = user

    @transaction.atomic
    def complete_order(self, *, order_id):
        order = (
            OnlineOrder.objects
            .select_for_update()
            .select_related("fulfilment_branch", "fulfilment_warehouse")
            .get(pk=order_id)
        )

        # A retry must not create another sale or deduct stock again.
        if order.status == OnlineOrder.Status.COMPLETED:
            if order.completed_sale_id:
                return order.completed_sale

            raise ValidationError(
                "The order is completed but has no linked sale. "
                "Investigate this record before retrying."
            )

        self._validate_fulfilment_status(order)
        self._validate_payment(order)

        items = list(
            OnlineOrderItem.objects
            .filter(order=order)
            .select_related("product")
            .order_by("product_id", "pk")
        )

        if not items:
            raise ValidationError("Cannot complete an order without items.")

        reservations = self._lock_reservations(items)
        balances = self._lock_balances(items, order)

        self._validate_reservations(items, reservations, balances)

        now = timezone.now()

        sale = Sale(
            number=f"ON-{order.pk:010d}",
            channel=Sale.Channel.ONLINE,
            fulfilment_method=(
                Sale.FulfilmentMethod.COLLECTION
                if order.fulfilment_method
                == OnlineOrder.FulfilmentMethod.COLLECTION
                else Sale.FulfilmentMethod.DELIVERY
            ),
            status=Sale.Status.COMPLETED,
            branch=order.fulfilment_branch,
            warehouse=order.fulfilment_warehouse,
            customer_name=order.customer_name,
            customer_phone=order.customer_phone,
            customer_email=order.customer_email or "",
            subtotal=order.subtotal,
            discount_amount=order.discount_amount,
            tax_amount=order.tax_amount,
            total_amount=order.total_amount,
            created_by=self.user,
            sold_at=now,
            completed_at=now,
            notes=f"Completed from online order {order.number}.",
        )
        sale.full_clean()
        sale.save()

        self._create_sale_items(sale, items)
        self._consume_stock(
            sale=sale,
            items=items,
            reservations=reservations,
            balances=balances,
            now=now,
        )

        order.status = OnlineOrder.Status.COMPLETED
        order.completed_at = now
        order.completed_sale = sale
        order.save(
            update_fields=[
                "status",
                "completed_at",
                "completed_sale",
                "updated_at",
            ]
        )

        return sale

    @staticmethod
    def _validate_fulfilment_status(order):
        allowed_statuses = {
            OnlineOrder.Status.READY_FOR_COLLECTION,
            OnlineOrder.Status.OUT_FOR_DELIVERY,
        }

        if order.status not in allowed_statuses:
            raise ValidationError(
                "The order must be ready for collection or out for delivery "
                "before it can be completed."
            )

        if not order.fulfilment_branch_id or not order.fulfilment_warehouse_id:
            raise ValidationError(
                "The order must have a fulfilment branch and warehouse."
            )

        warehouse = order.fulfilment_warehouse

        if warehouse.branch_id != order.fulfilment_branch_id:
            raise ValidationError(
                "The fulfilment warehouse does not belong to the "
                "responsible branch."
            )

    @staticmethod
    def _validate_payment(order):
        confirmed_total = (
            OrderPayment.objects
            .filter(
                order=order,
                status=OrderPayment.Status.CONFIRMED,
            )
            .aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )

        if confirmed_total < order.total_amount:
            raise ValidationError(
                "The order cannot be completed until its full payment "
                "has been confirmed."
            )

    @staticmethod
    def _lock_reservations(items):
        item_ids = [item.pk for item in items]

        reservations = list(
            StockReservation.objects
            .select_for_update()
            .filter(
                order_item_id__in=item_ids,
                status=StockReservation.Status.ACTIVE,
            )
            .order_by("warehouse_id", "order_item_id", "pk")
        )

        grouped = {}
        for reservation in reservations:
            grouped.setdefault(reservation.order_item_id, []).append(
                reservation
            )

        return grouped

    @staticmethod
    def _lock_balances(items, order):
        product_ids = sorted({item.product_id for item in items})
        warehouse_id = order.fulfilment_warehouse_id

        # Create missing balance records before acquiring row locks.
        for product_id in product_ids:
            StockBalance.objects.get_or_create(
                product_id=product_id,
                warehouse_id=warehouse_id,
                defaults={
                    "quantity_on_hand": Decimal("0.000"),
                    "quantity_reserved": Decimal("0.000"),
                    "reorder_level": Decimal("0.000"),
                },
            )

        balances = StockBalance.objects.select_for_update().filter(
            product_id__in=product_ids,
            warehouse_id=warehouse_id,
        ).order_by("product_id")

        return {balance.product_id: balance for balance in balances}

    @staticmethod
    def _validate_reservations(items, reservations, balances):
        for item in items:
            active_reservations = reservations.get(item.pk, [])

            reserved_quantity = sum(
                (reservation.quantity for reservation in active_reservations),
                Decimal("0.000"),
            )

            if reserved_quantity != item.quantity:
                raise ValidationError(
                    f"Insufficient active reservations for "
                    f"{item.product_name_snapshot}. "
                    f"Required: {item.quantity}; reserved: "
                    f"{reserved_quantity}."
                )

            balance = balances[item.product_id]

            if balance.quantity_reserved < reserved_quantity:
                raise ValidationError(
                    f"Reserved stock is inconsistent for "
                    f"{item.product_name_snapshot}."
                )

            if balance.quantity_on_hand < reserved_quantity:
                raise ValidationError(
                    f"Insufficient physical stock for "
                    f"{item.product_name_snapshot}."
                )

    @staticmethod
    def _create_sale_items(sale, items):
        sale_items = []

        for item in items:
            gross_amount = item.unit_price * item.quantity
            discount = max(
                Decimal("0.00"),
                gross_amount - item.line_total,
            )

            sale_items.append(
                SaleItem(
                    sale=sale,
                    product=item.product,
                    sku_snapshot=item.sku_snapshot,
                    product_name_snapshot=item.product_name_snapshot,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    unit_cost_snapshot=item.unit_cost_snapshot,
                    discount_amount=discount,
                    line_total=item.line_total,
                )
            )

        SaleItem.objects.bulk_create(sale_items)

    @staticmethod
    def _consume_stock(*, sale, items, reservations, balances, now):
        for item in items:
            balance = balances[item.product_id]
            item_reservations = reservations[item.pk]

            balance.quantity_on_hand -= item.quantity
            balance.quantity_reserved -= item.quantity

            balance.save(
                update_fields=[
                    "quantity_on_hand",
                    "quantity_reserved",
                    "updated_at",
                ]
            )

            StockMovement.objects.create(
                product_id=item.product_id,
                warehouse_id=sale.warehouse_id,
                movement_type=StockMovement.MovementType.SALE,
                quantity_delta=-item.quantity,
                unit_cost=item.unit_cost_snapshot,
                reference_type="SALE",
                reference_id=sale.pk,
                idempotency_key=f"sale-item:{sale.pk}:{item.pk}:out",
                notes=f"Online order {sale.number} fulfilled.",
                created_by=sale.created_by,
                occurred_at=now,
            )

            for reservation in item_reservations:
                reservation.status = StockReservation.Status.CONSUMED
                reservation.consumed_at = now
                reservation.save(
                    update_fields=["status", "consumed_at", "updated_at"]
                )

