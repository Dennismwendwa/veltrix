from django.core.exceptions import ValidationError
from django.db import transaction

from orders.models import OnlineOrder


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

