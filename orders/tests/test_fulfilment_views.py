from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Branch, BranchStaffAssignment, Warehouse
from orders.models import OnlineOrder


User = get_user_model()


class FulfilmentViewTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(
            username="branch_manager",
            password="test-password",
        )
        self.sales_staff = User.objects.create_user(
            username="sales_staff",
            password="test-password",
        )
        self.other_staff = User.objects.create_user(
            username="other_branch_staff",
            password="test-password",
        )

        self.branch = Branch.objects.create(
            name="Nakuru Branch",
            code="NKR",
        )
        self.other_branch = Branch.objects.create(
            name="Naivasha Branch",
            code="NVH",
        )

        self.warehouse = Warehouse.objects.create(
            name="Nakuru Warehouse",
            code="NKR-WH",
            warehouse_type=Warehouse.WarehouseType.BRANCH,
            branch=self.branch,
        )

        BranchStaffAssignment.objects.create(
            user=self.manager,
            branch=self.branch,
            role=BranchStaffAssignment.Role.MANAGER,
        )
        BranchStaffAssignment.objects.create(
            user=self.sales_staff,
            branch=self.branch,
            role=BranchStaffAssignment.Role.SALES,
        )
        BranchStaffAssignment.objects.create(
            user=self.other_staff,
            branch=self.other_branch,
            role=BranchStaffAssignment.Role.MANAGER,
        )

        self.order = OnlineOrder.objects.create(
            number="ORD-TEST-001",
            status=OnlineOrder.Status.PROCESSING,
            fulfilment_method=OnlineOrder.FulfilmentMethod.DELIVERY,
            payment_option=OnlineOrder.PaymentOption.CASH_ON_DELIVERY,
            fulfilment_branch=self.branch,
            fulfilment_warehouse=self.warehouse,
            customer_name="Test Customer",
            customer_phone="0712345678",
            delivery_address="Nakuru Town",
        )

    def test_anonymous_user_cannot_view_order(self):
        response = self.client.get(
            reverse("orders:order_detail", args=[self.order.pk])
        )

        self.assertEqual(response.status_code, 302)

    def test_staff_from_another_branch_cannot_view_order(self):
        self.client.force_login(self.other_staff)

        response = self.client.get(
            reverse("orders:order_detail", args=[self.order.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_sales_staff_cannot_dispatch_delivery(self):
        self.client.force_login(self.sales_staff)

        response = self.client.post(
            reverse("orders:dispatch_for_delivery", args=[self.order.pk])
        )

        self.order.refresh_from_db()

        self.assertEqual(self.order.status, OnlineOrder.Status.PROCESSING)
        self.assertEqual(response.status_code, 302)

    def test_get_request_cannot_dispatch_order(self):
        self.client.force_login(self.manager)

        response = self.client.get(
            reverse("orders:dispatch_for_delivery", args=[self.order.pk])
        )

        self.order.refresh_from_db()

        self.assertEqual(response.status_code, 405)
        self.assertEqual(self.order.status, OnlineOrder.Status.PROCESSING)

    def test_authorized_manager_can_dispatch_delivery(self):
        self.client.force_login(self.manager)

        response = self.client.post(
            reverse("orders:dispatch_for_delivery", args=[self.order.pk])
        )

        self.order.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.order.status,
            OnlineOrder.Status.OUT_FOR_DELIVERY,
        )

    def test_order_list_only_shows_assigned_branches(self):
        self.client.force_login(self.manager)

        response = self.client.get(reverse("orders:order_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ORD-TEST-001")

        # Create an order assigned to another branch.
        other_order = OnlineOrder.objects.create(
            number="ORD-OTHER-001",
            status=OnlineOrder.Status.PROCESSING,
            fulfilment_method=OnlineOrder.FulfilmentMethod.DELIVERY,
            payment_option=OnlineOrder.PaymentOption.CASH_ON_DELIVERY,
            fulfilment_branch=self.other_branch,
            fulfilment_warehouse=Warehouse.objects.create(
                name="Naivasha Warehouse",
                code="NVH-WH",
                warehouse_type=Warehouse.WarehouseType.BRANCH,
                branch=self.other_branch,
            ),
            customer_name="Other Customer",
            customer_phone="0711111111",
            delivery_address="Naivasha Town",
        )

        response = self.client.get(reverse("orders:order_list"))

        self.assertContains(response, "ORD-TEST-001")
        self.assertNotContains(response, other_order.number)


    def test_order_list_can_filter_by_status(self):
        self.client.force_login(self.manager)

        response = self.client.get(
            reverse("orders:order_list"),
            {"status": OnlineOrder.Status.PROCESSING},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ORD-TEST-001")
        self.assertEqual(len(response.context["orders"]), 1)


    def test_order_list_can_search_by_order_number(self):
        self.client.force_login(self.manager)

        response = self.client.get(
            reverse("orders:order_list"),
            {"q": "ORD-TEST-001"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ORD-TEST-001")
        self.assertEqual(len(response.context["orders"]), 1)


    def test_unassigned_staff_cannot_see_branch_orders(self):
        self.client.force_login(self.other_staff)

        response = self.client.get(reverse("orders:order_list"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "ORD-TEST-001")
        self.assertEqual(len(response.context["orders"]), 0)

