
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from catalog.models import Product, ProductCategory
from core.models import Branch, BranchStaffAssignment, Warehouse
from inventory.models import StockBalance, StockTransfer
from orders.models import OnlineOrder, OnlineOrderItem


User = get_user_model()


class BranchReplenishmentViewTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(
            username="branch_manager",
            password="test-password",
        )

        self.warehouse_staff = User.objects.create_user(
            username="warehouse_staff",
            password="test-password",
        )

        self.sales_staff = User.objects.create_user(
            username="sales_staff",
            password="test-password",
        )

        self.unassigned_user = User.objects.create_user(
            username="unassigned_user",
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

        self.other_branch = Branch.objects.create(
            name="Naivasha Branch",
            code="NVH",
        )

        self.branch_warehouse = Warehouse.objects.create(
            name="Nakuru Warehouse",
            code="NKR-WH",
            warehouse_type=Warehouse.WarehouseType.BRANCH,
            branch=self.branch,
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

        BranchStaffAssignment.objects.create(
            user=self.manager,
            branch=self.branch,
            role=BranchStaffAssignment.Role.MANAGER,
        )

        BranchStaffAssignment.objects.create(
            user=self.warehouse_staff,
            branch=self.branch,
            role=BranchStaffAssignment.Role.WAREHOUSE,
        )

        BranchStaffAssignment.objects.create(
            user=self.sales_staff,
            branch=self.branch,
            role=BranchStaffAssignment.Role.SALES,
        )

        # Provide stock at the master warehouse so the service
        # can recommend replenishment for outstanding demand.
        self.master_balance = StockBalance.objects.create(
            product=self.product,
            warehouse=self.master_warehouse,
            quantity_on_hand=Decimal("10.000"),
            quantity_reserved=Decimal("0.000"),
            reorder_level=Decimal("0.000"),
        )

        self.branch_balance = StockBalance.objects.create(
            product=self.product,
            warehouse=self.branch_warehouse,
            quantity_on_hand=Decimal("0.000"),
            quantity_reserved=Decimal("0.000"),
            reorder_level=Decimal("0.000"),
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

        OnlineOrderItem.objects.create(
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

        self.url = reverse("inventory:replenishment-list")

    def login_as(self, user):
        self.client.force_login(user)

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn("next=", response.url)

    def test_branch_manager_can_view_replenishment(self):
        self.login_as(self.manager)

        response = self.client.get(
            self.url,
            {"warehouse": self.branch_warehouse.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Branch Replenishment")
        self.assertContains(response, "Engine Filter")
        self.assertContains(response, "5.000")

    def test_warehouse_staff_can_view_replenishment(self):
        self.login_as(self.warehouse_staff)

        response = self.client.get(
            self.url,
            {"warehouse": self.branch_warehouse.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Engine Filter")

    def test_sales_staff_cannot_view_replenishment(self):
        self.login_as(self.sales_staff)

        response = self.client.get(
            self.url,
            {"warehouse": self.branch_warehouse.pk},
        )

        self.assertEqual(response.status_code, 404)

    def test_unassigned_user_cannot_view_replenishment(self):
        self.login_as(self.unassigned_user)

        response = self.client.get(
            self.url,
            {"warehouse": self.branch_warehouse.pk},
        )

        self.assertEqual(response.status_code, 404)

    def test_user_cannot_access_another_branch_warehouse(self):
        self.login_as(self.manager)

        response = self.client.get(
            self.url,
            {"warehouse": self.other_warehouse.pk},
        )

        self.assertEqual(response.status_code, 404)

    def test_invalid_warehouse_id_returns_404(self):
        self.login_as(self.manager)

        response = self.client.get(
            self.url,
            {"warehouse": "not-a-number"},
        )

        self.assertEqual(response.status_code, 404)

    def test_superuser_can_view_any_branch_warehouse(self):
        admin_user = User.objects.create_superuser(
            username="system_admin",
            email="admin@example.com",
            password="test-password",
        )

        self.login_as(admin_user)

        response = self.client.get(
            self.url,
            {"warehouse": self.other_warehouse.pk},
        )

        self.assertEqual(response.status_code, 200)

    def test_viewing_page_does_not_change_stock_or_create_transfers(self):
        self.login_as(self.manager)

        master_stock_before = self.master_balance.quantity_on_hand
        branch_stock_before = self.branch_balance.quantity_on_hand
        transfer_count_before = StockTransfer.objects.count()

        response = self.client.get(
            self.url,
            {"warehouse": self.branch_warehouse.pk},
        )

        self.assertEqual(response.status_code, 200)

        self.master_balance.refresh_from_db()
        self.branch_balance.refresh_from_db()

        self.assertEqual(
            self.master_balance.quantity_on_hand,
            master_stock_before,
        )
        self.assertEqual(
            self.branch_balance.quantity_on_hand,
            branch_stock_before,
        )
        self.assertEqual(
            StockTransfer.objects.count(),
            transfer_count_before,
        )

