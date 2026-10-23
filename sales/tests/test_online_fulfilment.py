from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from catalog.models import Product, ProductCategory
from core.models import Branch, Warehouse
from inventory.models import StockBalance, StockMovement
from orders.models import (
    OnlineOrder,
    OnlineOrderItem,
    OrderPayment,
    StockReservation,
)
from sales.models import Sale, SaleItem
from sales.service.online_fulfilment_service import (
    OnlineFulfilmentService,
)


class OnlineFulfilmentServiceTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="fulfilment_test",
            password="test-password-123",
        )

        self.branch = Branch.objects.create(
            name="Nakuru Branch",
            code="NKR",
            address="Nakuru, Kenya",
            phone="0700000000",
            is_active=True,
        )

        self.warehouse = Warehouse.objects.create(
            name="Nakuru Warehouse",
            warehouse_type=Warehouse.WarehouseType.BRANCH,
            branch=self.branch,
        )

        self.category = ProductCategory.objects.create(
            name="Engine Parts",
            is_active=True,
        )

        self.product = Product.objects.create(
            sku="ENG-001",
            name="Engine Filter",
            category=self.category,
            cost_price=Decimal("60.00"),
            selling_price=Decimal("100.00"),
            is_active=True,
            is_published_online=True,
        )

        self.balance = StockBalance.objects.create(
            product=self.product,
            warehouse=self.warehouse,
            quantity_on_hand=Decimal("10.000"),
            quantity_reserved=Decimal("2.000"),
            reorder_level=Decimal("2.000"),
        )

    def create_order(self, fulfilment_method=None):
        if fulfilment_method is None:
            fulfilment_method = (
                OnlineOrder.FulfilmentMethod.COLLECTION
            )

        order = OnlineOrder.objects.create(
            number="WEB-TEST-001",
            status=(
                OnlineOrder.Status.READY_FOR_COLLECTION
                if fulfilment_method
                == OnlineOrder.FulfilmentMethod.COLLECTION
                else OnlineOrder.Status.OUT_FOR_DELIVERY
            ),
            fulfilment_method=fulfilment_method,
            payment_option=OnlineOrder.PaymentOption.ONLINE,
            fulfilment_branch=self.branch,
            fulfilment_warehouse=self.warehouse,
            customer_name="Test Customer",
            customer_phone="0712345678",
            customer_email="customer@example.com",
            subtotal=Decimal("200.00"),
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            delivery_fee=Decimal("0.00"),
            total_amount=Decimal("200.00"),
        )

        item = OnlineOrderItem.objects.create(
            order=order,
            product=self.product,
            sku_snapshot=self.product.sku,
            product_name_snapshot=self.product.name,
            quantity=Decimal("2.000"),
            unit_price=Decimal("100.00"),
            unit_cost_snapshot=Decimal("60.00"),
            discount_amount=Decimal("0.00"),
            line_total=Decimal("200.00"),
        )

        StockReservation.objects.create(
            order_item=item,
            warehouse=self.warehouse,
            quantity=Decimal("2.000"),
            status=StockReservation.Status.ACTIVE,
        )

        return order, item

    def confirm_full_payment(self, order):
        return OrderPayment.objects.create(
            order=order,
            method=OrderPayment.Method.MPESA,
            status=OrderPayment.Status.CONFIRMED,
            amount=order.total_amount,
            transaction_reference=f"TEST-{order.pk}",
        )

    def test_collection_order_creates_sale_and_consumes_stock(self):
        order, item = self.create_order()
        self.confirm_full_payment(order)

        sale = OnlineFulfilmentService(
            user=self.user
        ).complete_order(order_id=order.pk)

        order.refresh_from_db()
        self.balance.refresh_from_db()

        reservation = StockReservation.objects.get(order_item=item)

        self.assertEqual(sale.status, Sale.Status.COMPLETED)
        self.assertEqual(sale.channel, Sale.Channel.ONLINE)
        self.assertEqual(
            sale.fulfilment_method,
            Sale.FulfilmentMethod.COLLECTION,
        )

        self.assertEqual(order.status, OnlineOrder.Status.COMPLETED)
        self.assertEqual(order.completed_sale_id, sale.pk)

        self.assertEqual(
            self.balance.quantity_on_hand,
            Decimal("8.000"),
        )
        self.assertEqual(
            self.balance.quantity_reserved,
            Decimal("0.000"),
        )

        self.assertEqual(
            reservation.status,
            StockReservation.Status.CONSUMED,
        )

        self.assertTrue(
            SaleItem.objects.filter(
                sale=sale,
                product=self.product,
                quantity=Decimal("2.000"),
                unit_price=Decimal("100.00"),
            ).exists()
        )

        movement = StockMovement.objects.get(
            reference_type="SALE",
            reference_id=sale.pk,
            product=self.product,
        )
        self.assertEqual(
            movement.quantity_delta,
            Decimal("-2.000"),
        )

    def test_delivery_order_uses_delivery_fulfilment(self):
        order, _ = self.create_order(
            OnlineOrder.FulfilmentMethod.DELIVERY
        )
        self.confirm_full_payment(order)

        sale = OnlineFulfilmentService(
            user=self.user
        ).complete_order(order_id=order.pk)

        self.assertEqual(
            sale.fulfilment_method,
            Sale.FulfilmentMethod.DELIVERY,
        )
        self.assertEqual(sale.channel, Sale.Channel.ONLINE)

    def test_unpaid_order_cannot_be_completed(self):
        order, _ = self.create_order()

        with self.assertRaises(ValidationError):
            OnlineFulfilmentService(
                user=self.user
            ).complete_order(order_id=order.pk)

        order.refresh_from_db()
        self.balance.refresh_from_db()

        self.assertEqual(
            order.status,
            OnlineOrder.Status.READY_FOR_COLLECTION,
        )
        self.assertEqual(
            self.balance.quantity_on_hand,
            Decimal("10.000"),
        )
        self.assertEqual(
            self.balance.quantity_reserved,
            Decimal("2.000"),
        )
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(StockMovement.objects.exists())

    def test_incomplete_reservation_prevents_completion(self):
        order, item = self.create_order()
        self.confirm_full_payment(order)

        reservation = StockReservation.objects.get(order_item=item)
        reservation.quantity = Decimal("1.000")
        reservation.save(update_fields=["quantity"])

        self.balance.quantity_reserved = Decimal("1.000")
        self.balance.save(update_fields=["quantity_reserved"])

        with self.assertRaises(ValidationError):
            OnlineFulfilmentService(
                user=self.user
            ).complete_order(order_id=order.pk)

        self.balance.refresh_from_db()
        order.refresh_from_db()

        self.assertEqual(
            self.balance.quantity_on_hand,
            Decimal("10.000"),
        )
        self.assertEqual(
            self.balance.quantity_reserved,
            Decimal("1.000"),
        )
        self.assertEqual(
            order.status,
            OnlineOrder.Status.READY_FOR_COLLECTION,
        )
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(StockMovement.objects.exists())

    def test_repeated_completion_does_not_deduct_stock_twice(self):
        order, _ = self.create_order()
        self.confirm_full_payment(order)

        service = OnlineFulfilmentService(user=self.user)

        first_sale = service.complete_order(order_id=order.pk)
        second_sale = service.complete_order(order_id=order.pk)

        self.balance.refresh_from_db()

        self.assertEqual(first_sale.pk, second_sale.pk)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertEqual(
            self.balance.quantity_on_hand,
            Decimal("8.000"),
        )
        self.assertEqual(
            self.balance.quantity_reserved,
            Decimal("0.000"),
        )

    def test_order_in_wrong_status_cannot_be_completed(self):
        order, _ = self.create_order()
        self.confirm_full_payment(order)

        order.status = OnlineOrder.Status.CONFIRMED
        order.save(update_fields=["status"])

        with self.assertRaises(ValidationError):
            OnlineFulfilmentService(
                user=self.user
            ).complete_order(order_id=order.pk)

        self.assertFalse(Sale.objects.exists())
        self.assertFalse(StockMovement.objects.exists())

