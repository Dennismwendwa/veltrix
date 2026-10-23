from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from orders.models import OnlineOrder, OrderPayment
from orders.services.lifecycle import OrderLifecycleService


CENT = Decimal("0.01")
ZERO = Decimal("0.00")


class OrderPaymentService:
    """
    Records and confirms payments for online orders.

    Recording a payment does not imply that money has been received.
    Only a verified payment should be confirmed.
    """

    def __init__(self, user=None):
        self.user = user

    @staticmethod
    def _amount(value):
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError("Enter a valid payment amount.")

        if not amount.is_finite() or amount <= ZERO:
            raise ValidationError("Payment amount must be positive.")

        if amount != amount.quantize(CENT):
            raise ValidationError(
                "Payment amounts can have at most two decimal places."
            )

        return amount.quantize(CENT, rounding=ROUND_HALF_UP)

    @transaction.atomic
    def record_payment(
        self,
        *,
        order_id,
        method,
        amount,
        idempotency_key=None,
        transaction_reference="",
    ):
        """Create a pending payment, safely handling checkout retries."""

        amount = self._amount(amount)

        if method not in OrderPayment.Method.values:
            raise ValidationError("Invalid payment method.")

        if idempotency_key and not idempotency_key.strip():
            raise ValidationError("Invalid idempotency key.")

        if transaction_reference:
            transaction_reference = transaction_reference.strip()

        order = self._lock_order(order_id)

        allowed_statuses = {
            OnlineOrder.Status.PLACED,
            OnlineOrder.Status.PENDING_PAYMENT,
            OnlineOrder.Status.CONFIRMED,
            OnlineOrder.Status.PROCESSING,
        }

        if order.status not in allowed_statuses:
            raise ValidationError(
                "Payments cannot be recorded for this order status."
            )

        if idempotency_key:
            existing = OrderPayment.objects.filter(
                idempotency_key=idempotency_key
            ).first()

            if existing:
                if (
                    existing.order_id != order.pk
                    or existing.method != method
                    or existing.amount != amount
                ):
                    raise ValidationError(
                        "This idempotency key was already used for "
                        "a different payment request."
                    )

                return existing

        payment = OrderPayment.objects.create(
            order=order,
            method=method,
            amount=amount,
            status=OrderPayment.Status.PENDING,
            transaction_reference=transaction_reference,
            idempotency_key=idempotency_key,
            recorded_by=self.user if getattr(
                self.user, "is_authenticated", False
            ) else None,
        )

        return payment

    @transaction.atomic
    def confirm_payment(
        self,
        *,
        payment_id,
        transaction_reference=None,
    ):
        """
        Confirm a verified payment without exceeding the order total.

        Call this only after the payment provider or an authorized
        staff member has verified the transaction.
        """

        try:
            payment_ref = OrderPayment.objects.only(
                "order_id"
            ).get(pk=payment_id)
        except OrderPayment.DoesNotExist:
            raise ValidationError("The requested payment does not exist.")

        # Always lock the order before its payments. This serializes
        # competing confirmations for the same order.
        order = self._lock_order(payment_ref.order_id)

        try:
            payment = OrderPayment.objects.select_for_update().get(
                pk=payment_id,
                order=order,
            )
        except OrderPayment.DoesNotExist:
            raise ValidationError("The requested payment does not exist.")

        if payment.status == OrderPayment.Status.CONFIRMED:
            if (
                transaction_reference
                and payment.transaction_reference
                and payment.transaction_reference
                != transaction_reference.strip()
            ):
                raise ValidationError(
                    "The payment has already been confirmed with "
                    "a different transaction reference."
                )
            return payment

        if payment.status != OrderPayment.Status.PENDING:
            raise ValidationError(
                "Only pending payments can be confirmed."
            )

        if order.status in {
            OnlineOrder.Status.CANCELLED,
            OnlineOrder.Status.EXPIRED,
            OnlineOrder.Status.COMPLETED,
        }:
            raise ValidationError(
                "Payments cannot be confirmed for this order status."
            )

        confirmed_total = (
            OrderPayment.objects.filter(
                order=order,
                status=OrderPayment.Status.CONFIRMED,
            )
            .exclude(pk=payment.pk)
            .aggregate(total=Sum("amount"))["total"]
            or ZERO
        )

        if confirmed_total + payment.amount > order.total_amount:
            raise ValidationError(
                "Confirming this payment would exceed the order total."
            )

        if transaction_reference is not None:
            payment.transaction_reference = transaction_reference.strip()

        payment.status = OrderPayment.Status.CONFIRMED
        payment.confirmed_at = timezone.now()
        payment.save(update_fields=[
            "status",
            "confirmed_at",
            "transaction_reference",
        ])

        self._confirm_order_if_fully_paid(order)
        return payment

    @staticmethod
    def _lock_order(order_id):
        try:
            return OnlineOrder.objects.select_for_update().get(pk=order_id)
        except OnlineOrder.DoesNotExist:
            raise ValidationError("The requested order does not exist.")

    def _confirm_order_if_fully_paid(self, order):
        """Confirm online orders only after full payment is verified."""

        if order.payment_option != OnlineOrder.PaymentOption.ONLINE:
            return order

        confirmed_total = (
            OrderPayment.objects.filter(
                order=order,
                status=OrderPayment.Status.CONFIRMED,
            ).aggregate(total=Sum("amount"))["total"]
            or ZERO
        )

        if confirmed_total >= order.total_amount:
            return OrderLifecycleService(
                user=self.user
            ).confirm_order(order_id=order.pk)

        return order
