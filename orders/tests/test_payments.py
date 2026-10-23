from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from catalog.models import Product, ProductCategory
from core.models import Branch, Warehouse
from orders.models import OnlineOrder, OnlineOrderItem, OrderPayment
from orders.services.payment import OrderPaymentService


class OrderPaymentServiceTests(TestCase):
    def setUp(self):
        branch = Branch.objects.create(
            name="Nakuru Branch",
            code="NKR",
        )

        warehouse = Warehouse.objects.create(
            name="Nakuru Warehouse",
            code="NKR-WH",
            warehouse_type=Warehouse.WarehouseType.BRANCH,
            branch=branch,
        )

        category = ProductCategory.objects.create(
            name="Engine Parts",
        )

        product = Product.objects.create(
            sku="ENG-001",
            name="Engine Filter",
            category=category,
            cost_price=Decimal("500.00"),
            selling_price=Decimal("750.00"),
            is_active=True,
            is_published_online=True,
        )

        self.order = OnlineOrder.objects.create(
            number="WEB-PAY-001",
            status=OnlineOrder.Status.PLACED,
            fulfilment_method=OnlineOrder.FulfilmentMethod.COLLECTION,
            fulfilment_branch=branch,
            fulfilment_warehouse=warehouse,
            customer_name="Test Customer",
            customer_phone="0712345678",
            subtotal=Decimal("3750.00"),
            total_amount=Decimal("3750.00"),
        )

        OnlineOrderItem.objects.create(
            order=self.order,
            product=product,
            sku_snapshot=product.sku,
            product_name_snapshot=product.name,
            quantity=Decimal("5.000"),
            unit_price=Decimal("750.00"),
            unit_cost_snapshot=Decimal("500.00"),
            discount_amount=Decimal("0.00"),
            line_total=Decimal("3750.00"),
        )

        self.service = OrderPaymentService()

    def record_payment(self, amount, key=None):
        return self.service.record_payment(
            order_id=self.order.pk,
            method=OrderPayment.Method.MPESA,
            amount=Decimal(amount),
            idempotency_key=key,
        )

    def test_record_payment_creates_pending_payment(self):
        payment = self.record_payment("1000.00")

        self.assertEqual(payment.status, OrderPayment.Status.PENDING)
        self.assertEqual(payment.amount, Decimal("1000.00"))
        self.assertIsNone(payment.confirmed_at)

    def test_repeated_request_returns_existing_payment(self):
        first = self.record_payment("1000.00", "checkout-payment-001")
        second = self.record_payment("1000.00", "checkout-payment-001")

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(OrderPayment.objects.count(), 1)

    def test_idempotency_key_cannot_be_reused_for_different_amount(self):
        self.record_payment("1000.00", "checkout-payment-002")

        with self.assertRaises(ValidationError):
            self.record_payment("1500.00", "checkout-payment-002")

        self.assertEqual(OrderPayment.objects.count(), 1)

    def test_confirm_payment_updates_status(self):
        payment = self.record_payment("1000.00")

        confirmed = self.service.confirm_payment(
            payment_id=payment.pk,
            transaction_reference="MPESA-ABC123",
        )

        self.assertEqual(
            confirmed.status,
            OrderPayment.Status.CONFIRMED,
        )
        self.assertEqual(
            confirmed.transaction_reference,
            "MPESA-ABC123",
        )
        self.assertIsNotNone(confirmed.confirmed_at)

    def test_reconfirming_payment_does_not_create_another_payment(self):
        payment = self.record_payment("1000.00")

        self.service.confirm_payment(payment_id=payment.pk)
        repeated = self.service.confirm_payment(payment_id=payment.pk)

        self.assertEqual(repeated.pk, payment.pk)
        self.assertEqual(OrderPayment.objects.count(), 1)

    def test_overpayment_is_rejected(self):
        first = self.record_payment("2000.00")
        second = self.record_payment("2000.00")

        self.service.confirm_payment(payment_id=first.pk)

        with self.assertRaises(ValidationError):
            self.service.confirm_payment(payment_id=second.pk)

        second.refresh_from_db()
        self.assertEqual(second.status, OrderPayment.Status.PENDING)

    def test_zero_payment_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.record_payment("0.00")

    def test_payment_cannot_be_recorded_for_cancelled_order(self):
        self.order.status = OnlineOrder.Status.CANCELLED
        self.order.save(update_fields=["status"])

        with self.assertRaises(ValidationError):
            self.record_payment("100.00")

    def test_full_online_payment_automatically_confirms_order(self):
        payment = self.record_payment("3750.00")

        self.service.confirm_payment(payment_id=payment.pk)

        self.order.refresh_from_db()
        self.assertEqual(
            self.order.status,
            OnlineOrder.Status.CONFIRMED,
        )

    def test_partial_payment_does_not_confirm_order(self):
        payment = self.record_payment("1000.00")

        self.service.confirm_payment(payment_id=payment.pk)

        self.order.refresh_from_db()
        self.assertEqual(
            self.order.status,
            OnlineOrder.Status.PLACED,
        )

    def test_multiple_payments_confirm_order_when_fully_paid(self):
        first = self.record_payment("2000.00")
        second = self.record_payment("1750.00")

        self.service.confirm_payment(payment_id=first.pk)
        self.service.confirm_payment(payment_id=second.pk)

        self.order.refresh_from_db()
        self.assertEqual(
            self.order.status,
            OnlineOrder.Status.CONFIRMED,
        )

    def test_cash_on_collection_payment_does_not_auto_confirm_order(self):
        self.order.payment_option = (
            OnlineOrder.PaymentOption.CASH_ON_COLLECTION
        )
        self.order.save(update_fields=["payment_option"])

        payment = self.record_payment("3750.00")
        self.service.confirm_payment(payment_id=payment.pk)

        self.order.refresh_from_db()
        self.assertEqual(
            self.order.status,
            OnlineOrder.Status.PLACED,
        )

