from django.core.exceptions import ValidationError
from django.db import transaction

from orders.models import OnlineOrder, OrderPayment
from orders.services.payment import OrderPaymentService


class CashCollectionService:
    """Record cash received for an online order at its collection branch."""

    def __init__(self, user=None):
        self.user = user
        self.payment_service = OrderPaymentService(user=user)

    @transaction.atomic
    def collect_payment(
        self,
        *,
        order_id,
        amount,
        idempotency_key,
    ):
        """
        Record and confirm cash received at the branch.

        The calling view must restrict this operation to authorized staff.
        """

        try:
            order = OnlineOrder.objects.select_for_update().get(
                pk=order_id
            )
        except OnlineOrder.DoesNotExist:
            raise ValidationError("The requested order does not exist.")

        if (
            order.fulfilment_method
            != OnlineOrder.FulfilmentMethod.COLLECTION
            or order.payment_option
            != OnlineOrder.PaymentOption.CASH_ON_COLLECTION
        ):
            raise ValidationError(
                "This order is not configured for cash on collection."
            )

        if order.status not in {
            OnlineOrder.Status.CONFIRMED,
            OnlineOrder.Status.PROCESSING,
            OnlineOrder.Status.READY_FOR_COLLECTION,
        }:
            raise ValidationError(
                "Cash can only be collected for an order being prepared "
                "or ready for collection."
            )

        if not idempotency_key or not idempotency_key.strip():
            raise ValidationError(
                "An idempotency key is required for cash collection."
            )

        payment = self.payment_service.record_payment(
            order_id=order.pk,
            method=OrderPayment.Method.CASH_ON_COLLECTION,
            amount=amount,
            idempotency_key=idempotency_key,
        )

        if payment.status == OrderPayment.Status.CONFIRMED:
            return payment

        return self.payment_service.confirm_payment(
            payment_id=payment.pk,
        )

