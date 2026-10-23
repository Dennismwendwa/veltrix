
from core.models import BranchStaffAssignment


class BranchPermissionService:
    """
    Centralized branch-level authorization.
    """

    @staticmethod
    def has_branch_role(user, branch_id, allowed_roles):
        if not user or not user.is_authenticated:
            return False

        # Superusers can manage all branches.
        if user.is_superuser:
            return True

        return BranchStaffAssignment.objects.filter(
            user=user,
            branch_id=branch_id,
            role__in=allowed_roles,
            is_active=True,
        ).exists()

    @classmethod
    def can_manage_fulfilment(cls, user, branch_id):
        return cls.has_branch_role(
            user,
            branch_id,
            allowed_roles=[
                BranchStaffAssignment.Role.MANAGER,
                BranchStaffAssignment.Role.WAREHOUSE,
            ],
        )

    @classmethod
    def can_complete_order(cls, user, branch_id):
        return cls.has_branch_role(
            user,
            branch_id,
            allowed_roles=[
                BranchStaffAssignment.Role.MANAGER,
                BranchStaffAssignment.Role.WAREHOUSE,
                BranchStaffAssignment.Role.SALES,
            ],
        )


