from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from orders.models import OnlineOrder, OrderPayment


ZERO = Decimal("0.00")


class OrderLifecycleService:
    """Manage order status transitions independently of stock operations."""

    def __init__(self, user=None):
        self.user = user

    @transaction.atomic
    def confirm_order(self, *, order_id):
        order = self._lock_order(order_id)

        # Safe to retry an already completed confirmation.
        if order.status == OnlineOrder.Status.CONFIRMED:
            return order

        if order.status not in {
            OnlineOrder.Status.PLACED,
            OnlineOrder.Status.PENDING_PAYMENT,
        }:
            raise ValidationError(
                "Only placed orders can be confirmed."
            )

        payment_option = order.payment_option

        if payment_option == OnlineOrder.PaymentOption.ONLINE:
            confirmed_total = (
                OrderPayment.objects.filter(
                    order=order,
                    status=OrderPayment.Status.CONFIRMED,
                ).aggregate(total=Sum("amount"))["total"]
                or ZERO
            )

            if confirmed_total < order.total_amount:
                raise ValidationError(
                    "The order cannot be confirmed until full payment "
                    "has been verified."
                )

        elif payment_option not in {
            OnlineOrder.PaymentOption.CASH_ON_COLLECTION,
            OnlineOrder.PaymentOption.CASH_ON_DELIVERY,
        }:
            raise ValidationError("Unsupported payment option.")

        order.status = OnlineOrder.Status.CONFIRMED
        order.save(update_fields=["status", "updated_at"])

        return order

    @transaction.atomic
    def mark_processing(self, *, order_id):
        order = self._lock_order(order_id)

        if order.status == OnlineOrder.Status.PROCESSING:
            return order

        if order.status != OnlineOrder.Status.CONFIRMED:
            raise ValidationError(
                "Only confirmed orders can enter processing."
            )

        order.status = OnlineOrder.Status.PROCESSING
        order.save(update_fields=["status", "updated_at"])

        return order

    @staticmethod
    def _lock_order(order_id):
        try:
            return OnlineOrder.objects.select_for_update().get(pk=order_id)
        except OnlineOrder.DoesNotExist:
            raise ValidationError("The requested order does not exist.")
