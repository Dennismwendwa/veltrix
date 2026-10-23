from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from catalog.models import Product, ProductCategory
from core.models import Branch, Warehouse
from orders.models import OnlineOrder, OnlineOrderItem, OrderPayment
from orders.services.cash_collection import CashCollectionService


class CashCollectionServiceTests(TestCase):
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

        category = ProductCategory.objects.create(name="Engine Parts")

        product = Product.objects.create(
            sku="ENG-CASH-001",
            name="Engine Filter",
            category=category,
            cost_price=Decimal("500.00"),
            selling_price=Decimal("750.00"),
            is_active=True,
            is_published_online=True,
        )

        self.order = OnlineOrder.objects.create(
            number="WEB-CASH-001",
            status=OnlineOrder.Status.CONFIRMED,
            fulfilment_method=OnlineOrder.FulfilmentMethod.COLLECTION,
            payment_option=(
                OnlineOrder.PaymentOption.CASH_ON_COLLECTION
            ),
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

        self.service = CashCollectionService()

    def test_cash_payment_is_recorded_and_confirmed(self):
        payment = self.service.collect_payment(
            order_id=self.order.pk,
            amount=Decimal("3750.00"),
            idempotency_key="cash-receipt-001",
        )

        self.assertEqual(payment.status, OrderPayment.Status.CONFIRMED)
        self.assertEqual(payment.method, OrderPayment.Method.CASH_ON_COLLECTION)
        self.assertEqual(payment.amount, Decimal("3750.00"))

    def test_repeated_request_does_not_create_duplicate_payment(self):
        first = self.service.collect_payment(
            order_id=self.order.pk,
            amount=Decimal("3750.00"),
            idempotency_key="cash-receipt-002",
        )

        second = self.service.collect_payment(
            order_id=self.order.pk,
            amount=Decimal("3750.00"),
            idempotency_key="cash-receipt-002",
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            OrderPayment.objects.filter(order=self.order).count(),
            1,
        )

    def test_cash_collection_rejects_online_payment_order(self):
        self.order.payment_option = OnlineOrder.PaymentOption.ONLINE
        self.order.save(update_fields=["payment_option"])

        with self.assertRaises(ValidationError):
            self.service.collect_payment(
                order_id=self.order.pk,
                amount=Decimal("3750.00"),
                idempotency_key="cash-receipt-003",
            )

    def test_cash_collection_rejects_missing_idempotency_key(self):
        with self.assertRaises(ValidationError):
            self.service.collect_payment(
                order_id=self.order.pk,
                amount=Decimal("3750.00"),
                idempotency_key="",
            )

    def test_cash_collection_rejects_cancelled_order(self):
        self.order.status = OnlineOrder.Status.CANCELLED
        self.order.save(update_fields=["status"])

        with self.assertRaises(ValidationError):
            self.service.collect_payment(
                order_id=self.order.pk,
                amount=Decimal("3750.00"),
                idempotency_key="cash-receipt-004",
            )

    def test_cash_collection_rejects_overpayment(self):
        with self.assertRaises(ValidationError):
            self.service.collect_payment(
                order_id=self.order.pk,
                amount=Decimal("4000.00"),
                idempotency_key="cash-receipt-005",
            )

        self.assertFalse(
            OrderPayment.objects.filter(order=self.order).exists()
        )

