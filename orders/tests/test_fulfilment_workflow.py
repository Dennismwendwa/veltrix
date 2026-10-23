from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import Branch, Warehouse
from orders.models import OnlineOrder
from orders.services.fulfilment import FulfilmentWorkflowService


class FulfilmentWorkflowServiceTests(TestCase):
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

        self.service = FulfilmentWorkflowService()

    def create_order(
        self,
        *,
        fulfilment_method=OnlineOrder.FulfilmentMethod.COLLECTION,
        status=OnlineOrder.Status.PROCESSING,
        delivery_address="",
        number="WEB-FLOW-001",
    ):
        return OnlineOrder.objects.create(
            number=number,
            status=status,
            fulfilment_method=fulfilment_method,
            payment_option=OnlineOrder.PaymentOption.ONLINE,
            fulfilment_branch=self.branch,
            fulfilment_warehouse=self.warehouse,
            customer_name="Test Customer",
            customer_phone="0712345678",
            delivery_address=delivery_address,
            subtotal=Decimal("1000.00"),
            total_amount=Decimal("1000.00"),
        )

    # Collection workflow

    def test_processing_collection_order_can_be_marked_ready(self):
        order = self.create_order()

        updated = self.service.mark_ready_for_collection(
            order_id=order.pk
        )

        self.assertEqual(
            updated.status,
            OnlineOrder.Status.READY_FOR_COLLECTION,
        )

        order.refresh_from_db()
        self.assertEqual(
            order.status,
            OnlineOrder.Status.READY_FOR_COLLECTION,
        )

    def test_marking_collection_order_ready_is_idempotent(self):
        order = self.create_order(
            status=OnlineOrder.Status.READY_FOR_COLLECTION,
        )

        first = self.service.mark_ready_for_collection(
            order_id=order.pk
        )
        second = self.service.mark_ready_for_collection(
            order_id=order.pk
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            second.status,
            OnlineOrder.Status.READY_FOR_COLLECTION,
        )

    def test_delivery_order_cannot_be_marked_ready_for_collection(self):
        order = self.create_order(
            fulfilment_method=OnlineOrder.FulfilmentMethod.DELIVERY,
            delivery_address="Nakuru, Kenya",
        )

        with self.assertRaises(ValidationError):
            self.service.mark_ready_for_collection(
                order_id=order.pk
            )

        order.refresh_from_db()
        self.assertEqual(
            order.status,
            OnlineOrder.Status.PROCESSING,
        )

    # Delivery workflow

    def test_processing_delivery_order_can_be_dispatched(self):
        order = self.create_order(
            fulfilment_method=OnlineOrder.FulfilmentMethod.DELIVERY,
            delivery_address="Nakuru, Kenya",
        )

        updated = self.service.dispatch_for_delivery(
            order_id=order.pk
        )

        self.assertEqual(
            updated.status,
            OnlineOrder.Status.OUT_FOR_DELIVERY,
        )

        order.refresh_from_db()
        self.assertEqual(
            order.status,
            OnlineOrder.Status.OUT_FOR_DELIVERY,
        )

    def test_dispatching_delivery_order_is_idempotent(self):
        order = self.create_order(
            fulfilment_method=OnlineOrder.FulfilmentMethod.DELIVERY,
            status=OnlineOrder.Status.OUT_FOR_DELIVERY,
            delivery_address="Nakuru, Kenya",
        )

        first = self.service.dispatch_for_delivery(
            order_id=order.pk
        )
        second = self.service.dispatch_for_delivery(
            order_id=order.pk
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            second.status,
            OnlineOrder.Status.OUT_FOR_DELIVERY,
        )

    def test_delivery_order_without_address_cannot_be_dispatched(self):
        order = self.create_order(
            fulfilment_method=OnlineOrder.FulfilmentMethod.DELIVERY,
            delivery_address="",
        )

        with self.assertRaises(ValidationError):
            self.service.dispatch_for_delivery(
                order_id=order.pk
            )

        order.refresh_from_db()
        self.assertEqual(
            order.status,
            OnlineOrder.Status.PROCESSING,
        )

    def test_collection_order_cannot_be_dispatched(self):
        order = self.create_order(
            fulfilment_method=OnlineOrder.FulfilmentMethod.COLLECTION,
        )

        with self.assertRaises(ValidationError):
            self.service.dispatch_for_delivery(
                order_id=order.pk
            )

        order.refresh_from_db()
        self.assertEqual(
            order.status,
            OnlineOrder.Status.PROCESSING,
        )

    # Invalid transitions

    def test_unconfirmed_order_cannot_be_marked_ready(self):
        order = self.create_order(
            status=OnlineOrder.Status.PLACED,
        )

        with self.assertRaises(ValidationError):
            self.service.mark_ready_for_collection(
                order_id=order.pk
            )

        order.refresh_from_db()
        self.assertEqual(order.status, OnlineOrder.Status.PLACED)

    def test_unconfirmed_order_cannot_be_dispatched(self):
        order = self.create_order(
            fulfilment_method=OnlineOrder.FulfilmentMethod.DELIVERY,
            status=OnlineOrder.Status.CONFIRMED,
            delivery_address="Nakuru, Kenya",
        )

        with self.assertRaises(ValidationError):
            self.service.dispatch_for_delivery(
                order_id=order.pk
            )

        order.refresh_from_db()
        self.assertEqual(order.status, OnlineOrder.Status.CONFIRMED)

    def test_cancelled_order_cannot_enter_fulfilment(self):
        order = self.create_order(
            status=OnlineOrder.Status.CANCELLED,
        )

        with self.assertRaises(ValidationError):
            self.service.mark_ready_for_collection(
                order_id=order.pk
            )

        order.refresh_from_db()
        self.assertEqual(order.status, OnlineOrder.Status.CANCELLED)

    def test_nonexistent_order_raises_validation_error(self):
        with self.assertRaises(ValidationError):
            self.service.mark_ready_for_collection(
                order_id=999999
            )

