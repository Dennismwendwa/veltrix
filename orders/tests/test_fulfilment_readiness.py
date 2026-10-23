from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from catalog.models import Product, ProductCategory
from core.models import Branch, Warehouse
from orders.models import OnlineOrder, OnlineOrderItem, StockReservation
from orders.services.fulfilment import FulfilmentWorkflowService


class FulfilmentReadinessTests(TestCase):
    def setUp(self):
        self.service = FulfilmentWorkflowService()

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

        self.other_branch = Branch.objects.create(
            name="Naivasha Branch",
            code="NVH",
        )

        self.other_warehouse = Warehouse.objects.create(
            name="Naivasha Warehouse",
            code="NVH-WH",
            warehouse_type=Warehouse.WarehouseType.BRANCH,
            branch=self.other_branch,
        )

        self.category = ProductCategory.objects.create(
            name="Engine Parts",
        )

        self.product = Product.objects.create(
            sku="ENG-001",
            name="Engine Filter",
            category=self.category,
            selling_price=Decimal("500.00"),
        )

        self.collection_order = self.create_order(
            number="WEB-TEST-001",
            method=OnlineOrder.FulfilmentMethod.COLLECTION,
            payment_option=OnlineOrder.PaymentOption.CASH_ON_COLLECTION,
        )

        self.item = OnlineOrderItem.objects.create(
            order=self.collection_order,
            product=self.product,
            sku_snapshot=self.product.sku,
            product_name_snapshot=self.product.name,
            quantity=Decimal("5.000"),
            unit_price=Decimal("500.00"),
            unit_cost_snapshot=Decimal("300.00"),
            discount_amount=Decimal("0.00"),
            line_total=Decimal("2500.00"),
        )

    def create_order(self, number, method, payment_option):
        return OnlineOrder.objects.create(
            number=number,
            status=OnlineOrder.Status.PROCESSING,
            fulfilment_method=method,
            payment_option=payment_option,
            fulfilment_branch=self.branch,
            fulfilment_warehouse=self.warehouse,
            customer_name="Test Customer",
            customer_phone="0712345678",
            delivery_address=(
                "Nakuru Town"
                if method == OnlineOrder.FulfilmentMethod.DELIVERY
                else ""
            ),
        )

    def reserve(self, quantity, warehouse=None):
        return StockReservation.objects.create(
            order_item=self.item,
            warehouse=warehouse or self.warehouse,
            quantity=Decimal(quantity),
            status=StockReservation.Status.ACTIVE,
        )

    def test_fully_reserved_order_can_be_marked_ready(self):
        self.reserve("5.000")

        order = self.service.mark_ready_for_collection(
            order_id=self.collection_order.pk,
        )

        self.assertEqual(
            order.status,
            OnlineOrder.Status.READY_FOR_COLLECTION,
        )

    def test_partially_reserved_order_cannot_be_marked_ready(self):
        self.reserve("3.000")

        with self.assertRaises(ValidationError) as context:
            self.service.mark_ready_for_collection(
                order_id=self.collection_order.pk,
            )

        self.assertIn("2.000", str(context.exception))

        self.collection_order.refresh_from_db()
        self.assertEqual(
            self.collection_order.status,
            OnlineOrder.Status.PROCESSING,
        )

    def test_order_without_reservations_cannot_be_marked_ready(self):
        with self.assertRaises(ValidationError):
            self.service.mark_ready_for_collection(
                order_id=self.collection_order.pk,
            )

        self.collection_order.refresh_from_db()
        self.assertEqual(
            self.collection_order.status,
            OnlineOrder.Status.PROCESSING,
        )

    def test_reservations_at_another_warehouse_do_not_count(self):
        self.reserve("5.000", warehouse=self.other_warehouse)

        with self.assertRaises(ValidationError):
            self.service.mark_ready_for_collection(
                order_id=self.collection_order.pk,
            )

        self.collection_order.refresh_from_db()
        self.assertEqual(
            self.collection_order.status,
            OnlineOrder.Status.PROCESSING,
        )

    def test_fully_reserved_delivery_order_can_be_dispatched(self):
        delivery_order = self.create_order(
            number="WEB-TEST-002",
            method=OnlineOrder.FulfilmentMethod.DELIVERY,
            payment_option=OnlineOrder.PaymentOption.CASH_ON_DELIVERY,
        )

        delivery_item = OnlineOrderItem.objects.create(
            order=delivery_order,
            product=self.product,
            sku_snapshot=self.product.sku,
            product_name_snapshot=self.product.name,
            quantity=Decimal("5.000"),
            unit_price=Decimal("500.00"),
            unit_cost_snapshot=Decimal("300.00"),
            discount_amount=Decimal("0.00"),
            line_total=Decimal("2500.00"),
        )

        StockReservation.objects.create(
            order_item=delivery_item,
            warehouse=self.warehouse,
            quantity=Decimal("5.000"),
            status=StockReservation.Status.ACTIVE,
        )

        order = self.service.dispatch_for_delivery(
            order_id=delivery_order.pk,
        )

        self.assertEqual(
            order.status,
            OnlineOrder.Status.OUT_FOR_DELIVERY,
        )

    def test_partially_reserved_delivery_order_cannot_be_dispatched(self):
        delivery_order = self.create_order(
            number="WEB-TEST-003",
            method=OnlineOrder.FulfilmentMethod.DELIVERY,
            payment_option=OnlineOrder.PaymentOption.CASH_ON_DELIVERY,
        )

        delivery_item = OnlineOrderItem.objects.create(
            order=delivery_order,
            product=self.product,
            sku_snapshot=self.product.sku,
            product_name_snapshot=self.product.name,
            quantity=Decimal("5.000"),
            unit_price=Decimal("500.00"),
            unit_cost_snapshot=Decimal("300.00"),
            discount_amount=Decimal("0.00"),
            line_total=Decimal("2500.00"),
        )

        StockReservation.objects.create(
            order_item=delivery_item,
            warehouse=self.warehouse,
            quantity=Decimal("2.000"),
            status=StockReservation.Status.ACTIVE,
        )

        with self.assertRaises(ValidationError):
            self.service.dispatch_for_delivery(
                order_id=delivery_order.pk,
            )

        delivery_order.refresh_from_db()
        self.assertEqual(
            delivery_order.status,
            OnlineOrder.Status.PROCESSING,
        )

