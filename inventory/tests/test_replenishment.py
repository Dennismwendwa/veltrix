from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from catalog.models import Product, ProductCategory
from core.models import Branch, Warehouse
from inventory.models import StockBalance, StockTransfer, StockTransferItem
from inventory.services.replenishment import BranchReplenishmentService
from orders.models import OnlineOrder, OnlineOrderItem, StockReservation


User = get_user_model()


class BranchReplenishmentServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="replenishment_manager",
            password="test-password",
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

        self.branch = Branch.objects.create(
            name="Nakuru Branch",
            code="NKR",
        )

        self.branch_warehouse = Warehouse.objects.create(
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

        self.master_warehouse = Warehouse.objects.create(
            name="Master Warehouse",
            code="MASTER-WH",
            warehouse_type=Warehouse.WarehouseType.MASTER,
        )

        self.branch_balance = self.create_balance(
            warehouse=self.branch_warehouse,
            on_hand="0",
            reserved="0",
        )

        self.master_balance = self.create_balance(
            warehouse=self.master_warehouse,
            on_hand="20",
            reserved="0",
        )

        self.order = OnlineOrder.objects.create(
            number="ORD-TEST-001",
            status=OnlineOrder.Status.PROCESSING,
            fulfilment_method=OnlineOrder.FulfilmentMethod.COLLECTION,
            fulfilment_branch=self.branch,
            fulfilment_warehouse=self.branch_warehouse,
            customer_name="Test Customer",
            customer_phone="0700000000",
        )

        self.order_item = OnlineOrderItem.objects.create(
            order=self.order,
            product=self.product,
            sku_snapshot=self.product.sku,
            product_name_snapshot=self.product.name,
            quantity=Decimal("10.000"),
            unit_price=Decimal("750.00"),
            unit_cost_snapshot=Decimal("500.00"),
            discount_amount=Decimal("0.00"),
            line_total=Decimal("7500.00"),
        )

        self.service = BranchReplenishmentService(
            branch_warehouse_id=self.branch_warehouse.pk,
        )

    @staticmethod
    def create_balance(*, warehouse, on_hand, reserved):
        return StockBalance.objects.create(
            product=Product.objects.get(sku="ENG-001"),
            warehouse=warehouse,
            quantity_on_hand=Decimal(on_hand),
            quantity_reserved=Decimal(reserved),
            reorder_level=Decimal("0"),
        )

    def create_reservation(
        self,
        *,
        warehouse,
        quantity,
        status=StockReservation.Status.ACTIVE,
    ):
        return StockReservation.objects.create(
            order_item=self.order_item,
            warehouse=warehouse,
            quantity=Decimal(quantity),
            status=status,
        )

    def create_transfer(
        self,
        *,
        status,
        quantity,
        number="TRF-TEST-001",
    ):
        transfer = StockTransfer.objects.create(
            number=number,
            source_warehouse=self.master_warehouse,
            destination_warehouse=self.branch_warehouse,
            status=status,
            requested_by=self.user,
        )

        StockTransferItem.objects.create(
            transfer=transfer,
            product=self.product,
            quantity_requested=Decimal(quantity),
        )

        return transfer

    def get_suggestion(self):
        result = self.service.suggest()

        return next(
            (
                item
                for item in result["suggestions"]
                if item["product_id"] == self.product.pk
            ),
            None,
        )

    def test_suggests_shortage_after_available_branch_stock(self):
        self.branch_balance.quantity_on_hand = Decimal("3.000")
        self.branch_balance.save(update_fields=["quantity_on_hand"])

        suggestion = self.get_suggestion()

        self.assertIsNotNone(suggestion)
        self.assertEqual(
            suggestion["outstanding_demand"],
            Decimal("10.000"),
        )
        self.assertEqual(
            suggestion["branch_available"],
            Decimal("3.000"),
        )
        self.assertEqual(
            suggestion["suggested_quantity"],
            Decimal("7.000"),
        )

    def test_suggestion_does_not_exceed_master_available_stock(self):
        self.master_balance.quantity_on_hand = Decimal("4.000")
        self.master_balance.save(update_fields=["quantity_on_hand"])

        suggestion = self.get_suggestion()

        self.assertIsNotNone(suggestion)
        self.assertEqual(
            suggestion["suggested_quantity"],
            Decimal("4.000"),
        )
        self.assertEqual(
            suggestion["uncovered_shortage"],
            Decimal("6.000"),
        )

    def test_fully_reserved_demand_does_not_require_replenishment(self):
        self.create_reservation(
            warehouse=self.branch_warehouse,
            quantity="10.000",
        )

        suggestion = self.get_suggestion()

        self.assertIsNone(suggestion)

    def test_reservation_at_another_warehouse_does_not_cover_demand(self):
        self.create_reservation(
            warehouse=self.other_warehouse,
            quantity="10.000",
        )

        suggestion = self.get_suggestion()

        self.assertIsNotNone(suggestion)
        self.assertEqual(
            suggestion["suggested_quantity"],
            Decimal("10.000"),
        )

    def test_released_reservation_does_not_cover_demand(self):
        self.create_reservation(
            warehouse=self.branch_warehouse,
            quantity="10.000",
            status=StockReservation.Status.RELEASED,
        )

        suggestion = self.get_suggestion()

        self.assertIsNotNone(suggestion)
        self.assertEqual(
            suggestion["suggested_quantity"],
            Decimal("10.000"),
        )

    def test_approved_transfer_is_not_suggested_again(self):
        self.create_transfer(
            status=StockTransfer.Status.APPROVED,
            quantity="4.000",
        )

        suggestion = self.get_suggestion()

        self.assertIsNotNone(suggestion)
        self.assertEqual(
            suggestion["in_transfer_pipeline"],
            Decimal("4.000"),
        )
        self.assertEqual(
            suggestion["suggested_quantity"],
            Decimal("6.000"),
        )

    def test_dispatched_transfer_reduces_remaining_shortage(self):
        self.master_balance.quantity_on_hand = Decimal("16.000")
        self.master_balance.save(update_fields=["quantity_on_hand"])

        self.create_transfer(
            status=StockTransfer.Status.DISPATCHED,
            quantity="4.000",
        )

        suggestion = self.get_suggestion()

        self.assertIsNotNone(suggestion)
        self.assertEqual(
            suggestion["in_transfer_pipeline"],
            Decimal("4.000"),
        )
        self.assertEqual(
            suggestion["suggested_quantity"],
            Decimal("6.000"),
        )

    def test_transfer_to_another_branch_does_not_cover_this_branch(self):
        transfer = StockTransfer.objects.create(
            number="TRF-OTHER-001",
            source_warehouse=self.master_warehouse,
            destination_warehouse=self.other_warehouse,
            status=StockTransfer.Status.DISPATCHED,
            requested_by=self.user,
        )

        StockTransferItem.objects.create(
            transfer=transfer,
            product=self.product,
            quantity_requested=Decimal("4.000"),
        )

        suggestion = self.get_suggestion()

        self.assertIsNotNone(suggestion)
        self.assertEqual(
            suggestion["in_transfer_pipeline"],
            Decimal("0.000"),
        )
        self.assertEqual(
            suggestion["suggested_quantity"],
            Decimal("10.000"),
        )

    def test_master_warehouse_cannot_be_replenishment_destination(self):
        service = BranchReplenishmentService(
            branch_warehouse_id=self.master_warehouse.pk,
        )

        with self.assertRaises(ValidationError):
            service.suggest()

    def test_missing_master_warehouse_raises_validation_error(self):
        # Remove the stock balance first because the warehouse
        # is protected by a foreign key.
        StockBalance.objects.filter(
            warehouse=self.master_warehouse
        ).delete()

        self.master_warehouse.delete()

        with self.assertRaises(ValidationError):
            self.service.suggest()

    def test_suggestion_does_not_modify_stock_balances(self):
        original_branch_quantity = (
            self.branch_balance.quantity_on_hand
        )
        original_master_quantity = (
            self.master_balance.quantity_on_hand
        )

        self.service.suggest()

        self.branch_balance.refresh_from_db()
        self.master_balance.refresh_from_db()

        self.assertEqual(
            self.branch_balance.quantity_on_hand,
            original_branch_quantity,
        )
        self.assertEqual(
            self.master_balance.quantity_on_hand,
            original_master_quantity,
        )

