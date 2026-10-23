from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Branch, BranchStaffAssignment
from core.services.permissions import BranchPermissionService


class BranchPermissionServiceTests(TestCase):
    def setUp(self):
        User = get_user_model()

        self.manager = User.objects.create_user(
            username="branch_manager",
            password="test-password",
        )
        self.sales_staff = User.objects.create_user(
            username="sales_staff",
            password="test-password",
        )
        self.other_staff = User.objects.create_user(
            username="other_staff",
            password="test-password",
        )

        self.branch = Branch.objects.create(
            name="Nakuru Branch",
            code="NKR",
        )
        self.other_branch = Branch.objects.create(
            name="Naivasha Branch",
            code="NAI",
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

    def test_manager_can_manage_branch_fulfilment(self):
        self.assertTrue(
            BranchPermissionService.can_manage_fulfilment(
                self.manager, self.branch.pk
            )
        )

    def test_sales_staff_cannot_manage_fulfilment(self):
        self.assertFalse(
            BranchPermissionService.can_manage_fulfilment(
                self.sales_staff, self.branch.pk
            )
        )

    def test_sales_staff_can_complete_order(self):
        self.assertTrue(
            BranchPermissionService.can_complete_order(
                self.sales_staff, self.branch.pk
            )
        )

    def test_staff_cannot_access_unassigned_branch(self):
        self.assertFalse(
            BranchPermissionService.can_complete_order(
                self.manager, self.other_branch.pk
            )
        )

    def test_inactive_assignment_grants_no_access(self):
        assignment = BranchStaffAssignment.objects.get(
            user=self.manager,
            branch=self.branch,
        )
        assignment.is_active = False
        assignment.save(update_fields=["is_active"])

        self.assertFalse(
            BranchPermissionService.can_manage_fulfilment(
                self.manager, self.branch.pk
            )
        )

    def test_superuser_can_manage_any_branch(self):
        User = get_user_model()
        admin = User.objects.create_superuser(
            username="system_admin",
            password="test-password",
        )

        self.assertTrue(
            BranchPermissionService.can_manage_fulfilment(
                admin, self.other_branch.pk
            )
        )

