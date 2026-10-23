from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Q, Sum
from django.db import transaction

from orders.models import OnlineOrder, StockReservation


class FulfilmentWorkflowService:
    """
    Controls collection and delivery transitions for online orders.
    """

    @transaction.atomic
    def mark_ready_for_collection(self, *, order_id):
        order = self._lock_order(order_id)

        if (
            order.fulfilment_method
            != OnlineOrder.FulfilmentMethod.COLLECTION
        ):
            raise ValidationError(
                "Only collection orders can be marked ready for collection."
            )

        if order.status == OnlineOrder.Status.READY_FOR_COLLECTION:
            return order

        if order.status != OnlineOrder.Status.PROCESSING:
            raise ValidationError(
                "Only processing orders can be marked ready for collection."
            )
        self._ensure_fully_reserved(order)

        order.status = OnlineOrder.Status.READY_FOR_COLLECTION
        order.save(update_fields=["status", "updated_at"])

        return order

    @transaction.atomic
    def dispatch_for_delivery(self, *, order_id):
        order = self._lock_order(order_id)

        if (
            order.fulfilment_method
            != OnlineOrder.FulfilmentMethod.DELIVERY
        ):
            raise ValidationError(
                "Only delivery orders can be dispatched."
            )

        if order.status == OnlineOrder.Status.OUT_FOR_DELIVERY:
            return order

        if order.status != OnlineOrder.Status.PROCESSING:
            raise ValidationError(
                "Only processing orders can be dispatched."
            )

        if not order.delivery_address.strip():
            raise ValidationError(
                "A delivery address is required before dispatch."
            )
        self._ensure_fully_reserved(order)

        order.status = OnlineOrder.Status.OUT_FOR_DELIVERY
        order.save(update_fields=["status", "updated_at"])

        return order

    @transaction.atomic
    def complete_order(self, *, order_id):
        # Import locally to keep the workflow and inventory implementation
        # separate and avoid unnecessary module-level coupling.
        from sales.service.online_fulfilment_service import (
            OnlineFulfilmentService,
        )

        return OnlineFulfilmentService(
            user=self._require_user()
        ).complete_order(order_id=order_id)

    def __init__(self, user=None):
        self.user = user

    def _require_user(self):
        if self.user is None or not getattr(
            self.user, "is_authenticated", False
        ):
            raise ValidationError(
                "An authenticated user is required for completion."
            )
        return self.user

    @staticmethod
    def _lock_order(order_id):
        try:
            return OnlineOrder.objects.select_for_update().get(
                pk=order_id
            )
        except OnlineOrder.DoesNotExist:
            raise ValidationError("The online order does not exist.")

    @staticmethod
    def _ensure_fully_reserved(order):
        """
        Ensure every ordered item has sufficient active reservations
        at the order's assigned fulfilment warehouse.
        """
        items = order.items.annotate(
            reserved_quantity=Sum(
                "reservations__quantity",
                filter=Q(
                    reservations__status=StockReservation.Status.ACTIVE,
                    reservations__warehouse_id=order.fulfilment_warehouse_id,
                ),
            )
        )

        shortages = []

        for item in items:
            reserved = item.reserved_quantity or Decimal("0.000")
            outstanding = item.quantity - reserved

            if outstanding > 0:
                shortages.append(
                    f"{item.product_name_snapshot}: "
                    f"{outstanding} still needs reserving"
                )

        if shortages:
            raise ValidationError(
                "This order cannot proceed because some items are "
                "not fully reserved at the fulfilment warehouse. "
                + "; ".join(shortages)
            )


