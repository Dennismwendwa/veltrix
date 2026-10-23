from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import DecimalField, F, Prefetch, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.models import BranchStaffAssignment
from core.services.permissions import BranchPermissionService
from orders.models import OnlineOrder, OnlineOrderItem, StockReservation
from orders.services.fulfilment import FulfilmentWorkflowService


def _can_view_order(user, branch_id):
    """Check whether the user can access orders for this branch."""
    if user.is_superuser:
        return True

    return BranchStaffAssignment.objects.filter(
        user=user,
        branch_id=branch_id,
        is_active=True,
    ).exists()


@login_required
def order_detail(request, pk):
    """Display an order only to staff assigned to its branch."""
    order = get_object_or_404(OnlineOrder, pk=pk)

    if not _can_view_order(request.user, order.fulfilment_branch_id):
        # Return 404 rather than disclose another branch's order.
        from django.http import Http404
        raise Http404

    return render(
        request,
        "orders/order_detail.html",
        {"order": order},
    )


@login_required
@require_POST
def mark_ready_for_collection(request, pk):
    order = get_object_or_404(OnlineOrder, pk=pk)

    if not BranchPermissionService.can_manage_fulfilment(
        request.user, order.fulfilment_branch_id
    ):
        messages.error(request, "You are not authorized to manage this order.")
        return redirect("orders:order_detail", pk=order.pk)

    try:
        FulfilmentWorkflowService(user=request.user).mark_ready_for_collection(
            order_id=order.pk
        )
        messages.success(request, "Order marked ready for collection.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))

    return redirect("orders:order_detail", pk=order.pk)


@login_required
@require_POST
def dispatch_for_delivery(request, pk):
    order = get_object_or_404(OnlineOrder, pk=pk)

    if not BranchPermissionService.can_manage_fulfilment(
        request.user, order.fulfilment_branch_id
    ):
        messages.error(request, "You are not authorized to manage this order.")
        return redirect("orders:order_detail", pk=order.pk)

    try:
        FulfilmentWorkflowService(user=request.user).dispatch_for_delivery(
            order_id=order.pk
        )
        messages.success(request, "Order marked as out for delivery.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))

    return redirect("orders:order_detail", pk=order.pk)


@login_required
@require_POST
def complete_order(request, pk):
    order = get_object_or_404(OnlineOrder, pk=pk)

    if not BranchPermissionService.can_complete_order(
        request.user, order.fulfilment_branch_id
    ):
        messages.error(request, "You are not authorized to complete this order.")
        return redirect("orders:order_detail", pk=order.pk)

    try:
        FulfilmentWorkflowService(user=request.user).complete_order(
            order_id=order.pk
        )
        messages.success(request, "Order completed successfully.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))

    return redirect("orders:order_detail", pk=order.pk)

@login_required
def order_list(request):
    """List online orders accessible to the current branch staff member."""

    item_queryset = OnlineOrderItem.objects.annotate(
        reserved_quantity=Coalesce(
            Sum(
                "reservations__quantity",
                filter=Q(
                    reservations__status=StockReservation.Status.ACTIVE,
                    reservations__warehouse_id=F(
                        "order__fulfilment_warehouse_id"
                    ),
                ),
            ),
            Value(Decimal("0.000")),
            output_field=DecimalField(
                max_digits=14,
                decimal_places=3,
            ),
        )
    )

    orders = OnlineOrder.objects.select_related(
        "fulfilment_branch",
        "fulfilment_warehouse",
    ).prefetch_related(
        Prefetch(
            "items",
            queryset=item_queryset,
            to_attr="queue_items",
        )
    )

    # Restrict staff to orders assigned to their active branches.
    if not request.user.is_superuser:
        branch_ids = BranchStaffAssignment.objects.filter(
            user=request.user,
            is_active=True,
        ).values_list("branch_id", flat=True)

        orders = orders.filter(
            fulfilment_branch_id__in=branch_ids,
        )

    status = request.GET.get("status", "").strip()
    fulfilment_method = request.GET.get(
        "fulfilment_method", ""
    ).strip()
    search = request.GET.get("q", "").strip()

    valid_statuses = {
        value for value, label in OnlineOrder.Status.choices
    }
    valid_methods = {
        value for value, label in OnlineOrder.FulfilmentMethod.choices
    }

    if status in valid_statuses:
        orders = orders.filter(status=status)

    if fulfilment_method in valid_methods:
        orders = orders.filter(
            fulfilment_method=fulfilment_method,
        )

    if search:
        orders = orders.filter(
            Q(number__icontains=search)
            | Q(customer_name__icontains=search)
            | Q(customer_phone__icontains=search)
            | Q(customer_email__icontains=search)
        )

    orders = orders.order_by("-created_at", "-pk")

    paginator = Paginator(orders, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    for order in page_obj.object_list:
        outstanding_items = []

        for item in order.queue_items:
            quantity_to_source = max(
                Decimal("0.000"),
                item.quantity - item.reserved_quantity,
            )

            if quantity_to_source > 0:
                outstanding_items.append({
                    "name": item.product_name_snapshot,
                    "quantity": quantity_to_source,
                })

        order.stock_ready = not outstanding_items
        order.stock_shortages = outstanding_items

    context = {
        "page_obj": page_obj,
        "orders": page_obj.object_list,
        "status_choices": OnlineOrder.Status.choices,
        "fulfilment_choices": OnlineOrder.FulfilmentMethod.choices,
        "selected_status": status,
        "selected_fulfilment_method": fulfilment_method,
        "search_query": search,
    }

    return render(request, "orders/order_list.html", context)



