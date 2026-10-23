from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import render

from core.models import BranchStaffAssignment, Warehouse
from core.services.permissions import BranchPermissionService
from inventory.services.replenishment import BranchReplenishmentService


@login_required
def replenishment_list(request):
    """
    Display read-only replenishment recommendations for an
    authorized branch warehouse.
    """

    warehouses = Warehouse.objects.filter(
        warehouse_type=Warehouse.WarehouseType.BRANCH,
        is_active=True,
        branch__isnull=False,
    ).select_related("branch")

    if not request.user.is_superuser:
        warehouses = warehouses.filter(
            branch__staff_assignments__user=request.user,
            branch__staff_assignments__is_active=True,
            branch__staff_assignments__role__in=[
                BranchStaffAssignment.Role.MANAGER,
                BranchStaffAssignment.Role.WAREHOUSE,
            ],
        ).distinct()

    warehouses = warehouses.order_by("name")

    selected_warehouse = None
    result = None
    error_message = None

    warehouse_id = request.GET.get("warehouse")

    if warehouse_id:
        try:
            selected_warehouse = warehouses.get(pk=int(warehouse_id))
        except (ValueError, TypeError, Warehouse.DoesNotExist):
            raise Http404("Branch warehouse not found.")

    elif warehouses.count() == 1:
        selected_warehouse = warehouses.first()

    if selected_warehouse:
        # Keep the centralized permission service as the
        # authorization check for replenishment access.
        if not BranchPermissionService.can_manage_fulfilment(
            request.user,
            selected_warehouse.branch_id,
        ):
            raise Http404("Branch warehouse not found.")

        try:
            result = BranchReplenishmentService(
                branch_warehouse_id=selected_warehouse.pk,
            ).suggest()

        except ValidationError as exc:
            error_message = " ".join(exc.messages)

    context = {
        "warehouses": warehouses,
        "selected_warehouse": selected_warehouse,
        "result": result,
        "error_message": error_message,
        "suggestions": (
            result["suggestions"] if result else []
        ),
    }

    return render(
        request,
        "inventory/replenishment_list.html",
        context,
    )

