from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from catalog.models import Product, ProductCategory
from core.models import Branch, Warehouse
from orders.models import OnlineOrder, OnlineOrderItem, OrderPayment
from orders.services.lifecycle import OrderLifecycleService


class OrderLifecycleServiceTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(
            name="Nakuru Branch",
            code="NKR",
        )

        self.warehouse = Warehouse.objects.create(
            name="Nakuru Warehouse",
            code="NKR-WH",
            warehouse_type=Warehouse.WarehouseType.BRANCH,
            branch=self.branch,
        )

        category = ProductCategory.objects.create(
            name="Engine Parts",
        )

        product = Product.objects.create(
            sku="ENG-LIFE-001",
            name="Engine Filter",
            category=category,
            cost_price=Decimal("500.00"),
            selling_price=Decimal("750.00"),
            is_active=True,
            is_published_online=True,
        )

        self.order = OnlineOrder.objects.create(
            number="WEB-LIFE-001",
            status=OnlineOrder.Status.PLACED,
            fulfilment_method=OnlineOrder.FulfilmentMethod.COLLECTION,
            payment_option=OnlineOrder.PaymentOption.ONLINE,
            fulfilment_branch=self.branch,
            fulfilment_warehouse=self.warehouse,
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

        self.service = OrderLifecycleService()

    def create_confirmed_payment(self, amount):
        return OrderPayment.objects.create(
            order=self.order,
            method=OrderPayment.Method.MPESA,
            status=OrderPayment.Status.CONFIRMED,
            amount=Decimal(amount),
            transaction_reference="MPESA-LIFE-001",
        )

    def test_online_order_confirms_after_full_payment(self):
        self.create_confirmed_payment("3750.00")

        order = self.service.confirm_order(order_id=self.order.pk)

        self.assertEqual(order.status, OnlineOrder.Status.CONFIRMED)

    def test_online_order_rejects_confirmation_without_payment(self):
        with self.assertRaises(ValidationError):
            self.service.confirm_order(order_id=self.order.pk)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OnlineOrder.Status.PLACED)

    def test_online_order_rejects_partial_payment(self):
        self.create_confirmed_payment("1000.00")

        with self.assertRaises(ValidationError):
            self.service.confirm_order(order_id=self.order.pk)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OnlineOrder.Status.PLACED)

    def test_cash_on_collection_can_be_confirmed_without_payment(self):
        self.order.payment_option = (
            OnlineOrder.PaymentOption.CASH_ON_COLLECTION
        )
        self.order.save(update_fields=["payment_option"])

        order = self.service.confirm_order(order_id=self.order.pk)

        self.assertEqual(order.status, OnlineOrder.Status.CONFIRMED)
        self.assertFalse(order.payments.filter(
            status=OrderPayment.Status.CONFIRMED
        ).exists())

    def test_cash_on_delivery_can_be_confirmed_without_payment(self):
        self.order.fulfilment_method = (
            OnlineOrder.FulfilmentMethod.DELIVERY
        )
        self.order.payment_option = (
            OnlineOrder.PaymentOption.CASH_ON_DELIVERY
        )
        self.order.save(update_fields=[
            "fulfilment_method",
            "payment_option",
        ])

        order = self.service.confirm_order(order_id=self.order.pk)

        self.assertEqual(order.status, OnlineOrder.Status.CONFIRMED)

    def test_cancelled_order_cannot_be_confirmed(self):
        self.order.status = OnlineOrder.Status.CANCELLED
        self.order.save(update_fields=["status"])

        with self.assertRaises(ValidationError):
            self.service.confirm_order(order_id=self.order.pk)

    def test_reconfirming_order_is_idempotent(self):
        self.create_confirmed_payment("3750.00")

        first = self.service.confirm_order(order_id=self.order.pk)
        second = self.service.confirm_order(order_id=self.order.pk)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.status, OnlineOrder.Status.CONFIRMED)

    def test_confirmed_order_can_enter_processing(self):
        self.create_confirmed_payment("3750.00")
        self.service.confirm_order(order_id=self.order.pk)

        order = self.service.mark_processing(order_id=self.order.pk)

        self.assertEqual(order.status, OnlineOrder.Status.PROCESSING)

    def test_processing_transition_is_idempotent(self):
        self.create_confirmed_payment("3750.00")
        self.service.confirm_order(order_id=self.order.pk)
        self.service.mark_processing(order_id=self.order.pk)

        order = self.service.mark_processing(order_id=self.order.pk)

        self.assertEqual(order.status, OnlineOrder.Status.PROCESSING)

    def test_draft_order_cannot_enter_processing(self):
        self.order.status = OnlineOrder.Status.DRAFT
        self.order.save(update_fields=["status"])

        with self.assertRaises(ValidationError):
            self.service.mark_processing(order_id=self.order.pk)

