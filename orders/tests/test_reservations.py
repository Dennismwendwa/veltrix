
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase

from catalog.models import Product, ProductCategory
from core.models import Branch, Warehouse
from inventory.models import StockBalance
from orders.models import OnlineOrder, OnlineOrderItem, StockReservation
from orders.services.online_order import OnlineOrderService


class StockReservationTests(TestCase):
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

        self.category = ProductCategory.objects.create(
            name="Engine Parts",
        )

        self.product = Product.objects.create(
            sku="ENG-001",
            name="Engine Filter",
            category=self.category,
            cost_price=Decimal("500.00"),
            selling_price=Decimal("750.00"),
            is_active=True,
            is_published_online=True,
        )

        self.balance = StockBalance.objects.create(
            product=self.product,
            warehouse=self.warehouse,
            quantity_on_hand=Decimal("10.000"),
            quantity_reserved=Decimal("0.000"),
        )

        self.order = OnlineOrder.objects.create(
            number="WEB-TEST-001",
            status=OnlineOrder.Status.DRAFT,
            fulfilment_method=(
                OnlineOrder.FulfilmentMethod.COLLECTION
            ),
            fulfilment_branch=self.branch,
            fulfilment_warehouse=self.warehouse,
            customer_name="Test Customer",
            customer_phone="0712345678",
        )

        self.item = OnlineOrderItem.objects.create(
            order=self.order,
            product=self.product,
            sku_snapshot=self.product.sku,
            product_name_snapshot=self.product.name,
            quantity=Decimal("5.000"),
            unit_price=Decimal("750.00"),
            unit_cost_snapshot=Decimal("500.00"),
            discount_amount=Decimal("0.00"),
            line_total=Decimal("3750.00"),
        )

        self.service = OnlineOrderService()

    def create_reservation(self, *, status, quantity="2.000"):
        return StockReservation.objects.create(
            order_item=self.item,
            warehouse=self.warehouse,
            quantity=Decimal(quantity),
            status=status,
        )

    def test_only_one_active_reservation_per_item_and_warehouse(self):
        self.create_reservation(
            status=StockReservation.Status.ACTIVE,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_reservation(
                    status=StockReservation.Status.ACTIVE,
                )

        self.assertEqual(
            StockReservation.objects.filter(
                order_item=self.item,
                warehouse=self.warehouse,
                status=StockReservation.Status.ACTIVE,
            ).count(),
            1,
        )

    def test_released_reservation_can_have_a_new_active_reservation(self):
        old_reservation = self.create_reservation(
            status=StockReservation.Status.RELEASED,
        )

        new_reservation = self.create_reservation(
            status=StockReservation.Status.ACTIVE,
            quantity="3.000",
        )

        self.assertNotEqual(old_reservation.pk, new_reservation.pk)

        self.assertEqual(
            StockReservation.objects.filter(
                order_item=self.item,
                warehouse=self.warehouse,
            ).count(),
            2,
        )

    def test_place_order_reserves_available_stock(self):
        self.service.place_order(order_id=self.order.pk)

        self.balance.refresh_from_db()

        self.assertEqual(
            self.balance.quantity_reserved,
            Decimal("5.000"),
        )

        self.assertEqual(
            self.item.quantity_reserved,
            Decimal("5.000"),
        )

        self.assertEqual(
            self.item.quantity_to_source,
            Decimal("0.000"),
        )

    def test_place_order_only_reserves_available_quantity(self):
        self.balance.quantity_on_hand = Decimal("2.000")
        self.balance.save(update_fields=[
            "quantity_on_hand",
            "updated_at",
        ])

        self.service.place_order(order_id=self.order.pk)

        self.balance.refresh_from_db()

        self.assertEqual(
            self.balance.quantity_reserved,
            Decimal("2.000"),
        )

        self.assertEqual(
            self.item.quantity_to_source,
            Decimal("3.000"),
        )

    def test_newly_available_stock_increases_active_reservation(self):
        self.balance.quantity_on_hand = Decimal("2.000")
        self.balance.save(update_fields=[
            "quantity_on_hand",
            "updated_at",
        ])

        self.service.place_order(order_id=self.order.pk)

        self.balance.refresh_from_db()
        self.balance.quantity_on_hand = Decimal("5.000")
        self.balance.save(update_fields=[
            "quantity_on_hand",
            "updated_at",
        ])

        self.service.reserve_newly_available_stock(
            order_id=self.order.pk,
        )

        self.balance.refresh_from_db()

        active_reservations = StockReservation.objects.filter(
            order_item=self.item,
            warehouse=self.warehouse,
            status=StockReservation.Status.ACTIVE,
        )

        self.assertEqual(active_reservations.count(), 1)
        self.assertEqual(
            active_reservations.get().quantity,
            Decimal("5.000"),
        )
        self.assertEqual(
            self.balance.quantity_reserved,
            Decimal("5.000"),
        )
        self.assertEqual(
            self.item.quantity_to_source,
            Decimal("0.000"),
        )

    def test_cancel_order_releases_active_reservations(self):
        self.service.place_order(order_id=self.order.pk)

        self.service.cancel_order(order_id=self.order.pk)

        self.balance.refresh_from_db()
        self.order.refresh_from_db()

        self.assertEqual(
            self.balance.quantity_reserved,
            Decimal("0.000"),
        )
        self.assertEqual(
            self.order.status,
            OnlineOrder.Status.CANCELLED,
        )
        self.assertFalse(
            StockReservation.objects.filter(
                order_item=self.item,
                status=StockReservation.Status.ACTIVE,
            ).exists()
        )
        self.assertTrue(
            StockReservation.objects.filter(
                order_item=self.item,
                status=StockReservation.Status.RELEASED,
            ).exists()
        )

