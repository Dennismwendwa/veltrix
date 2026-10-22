from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from .models import (
    StockBalance, StockMovement,
    StockTransfer,
    StockTransferItem,
    StockTransferReceipt,
    StockTransferReceiptItem,
    )
from .services.transfer_services import StockTransferService


def model_field_names(model):
    """Return concrete fields suitable for read-only display."""
    return tuple(
        field.name
        for field in model._meta.fields
        if not field.primary_key
    )

class StockBalanceAdmin(admin.ModelAdmin):
    list_display = (
        "product", "warehouse", "quantity_on_hand", "quantity_reserved",
        "quantity_available_display", "reorder_level", "updated_at",
        )
    list_filter = ("warehouse",)
    search_fields = ("product__sku", "product__name", "warehouse__name")
    readonly_fields = model_field_names(StockBalance)

    def quantity_available_display(self, obj):
        return obj.quantity_available

    quantity_available_display.short_description = "Available"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
    list_filter = ("product", "warehouse",)


class StockMovementAdmin(admin.ModelAdmin):
    list_display = (
        "product", "warehouse", "movement_type", "quantity_delta",
        "reference_type", "reference_id", "created_by", "occurred_at",
        )
    list_filter = ("movement_type", "warehouse", "occurred_at")
    search_fields = (
        "product__sku", "product__name", "reference_type",
        "reference_id", "idempotency_key",
        )
    readonly_fields = model_field_names(StockMovement)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class StockTransferItemInline(admin.TabularInline):
    model = StockTransferItem
    extra = 0

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status != StockTransfer.Status.DRAFT:
            return model_field_names(StockTransferItem)
        return ()

    def has_add_permission(self, request, obj=None):
        if obj and obj.status != StockTransfer.Status.DRAFT:
            return False
        return super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status != StockTransfer.Status.DRAFT:
            return False
        return super().has_delete_permission(request, obj)


class StockTransferAdmin(admin.ModelAdmin):
    list_display = (
        "number", "source_warehouse", "destination_warehouse", "status",
        "requested_by", "created_at",
        )
    list_filter = (
        "status", "source_warehouse", "destination_warehouse", "created_at",
        )
    search_fields = (
        "number", "source_warehouse__name",
        "destination_warehouse__name",
        )
    readonly_fields = (
        "approved_by", "approved_at", "dispatched_by",
        "dispatched_at", "created_at", "updated_at",
        )
    inlines = [StockTransferItemInline]

    actions = ["approve_selected", "dispatch_selected"]

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))

        if obj and obj.status != StockTransfer.Status.DRAFT:
            fields.extend(model_field_names(StockTransfer))

        return tuple(dict.fromkeys(fields))

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status != StockTransfer.Status.DRAFT:
            return False
        return super().has_delete_permission(request, obj)

    @admin.action(description="Approve selected draft transfers")
    def approve_selected(self, request, queryset):
        succeeded = 0
        failed = 0

        for transfer in queryset:
            try:
                StockTransferService(
                    transfer_id=transfer.pk,
                    user=request.user,
                ).approve()
                succeeded += 1
            except (ValidationError, StockTransfer.DoesNotExist) as exc:
                failed += 1
                self.message_user(
                    request,
                    f"Transfer {transfer.number}: {exc}",
                    level=messages.ERROR,
                )

        if succeeded:
            self.message_user(
                request,
                f"Approved {succeeded} transfer(s).",
                level=messages.SUCCESS,
            )

        if failed:
            self.message_user(
                request,
                f"{failed} transfer(s) could not be approved.",
                level=messages.WARNING,
            )

    @admin.action(description="Dispatch selected approved transfers")
    def dispatch_selected(self, request, queryset):
        succeeded = 0
        failed = 0

        for transfer in queryset:
            try:
                StockTransferService(
                    transfer_id=transfer.pk,
                    user=request.user,
                ).dispatch()
                succeeded += 1
            except (ValidationError, StockTransfer.DoesNotExist) as exc:
                failed += 1
                self.message_user(
                    request,
                    f"Transfer {transfer.number}: {exc}",
                    level=messages.ERROR,
                )

        if succeeded:
            self.message_user(
                request,
                f"Dispatched {succeeded} transfer(s).",
                level=messages.SUCCESS,
            )

        if failed:
            self.message_user(
                request,
                f"{failed} transfer(s) could not be dispatched.",
                level=messages.WARNING,
            )


class StockTransferReceiptItemInline(admin.TabularInline):
    model = StockTransferReceiptItem
    extra = 0

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status != StockTransferReceipt.Status.DRAFT:
            return model_field_names(StockTransferReceiptItem)
        return ()

    def has_add_permission(self, request, obj=None):
        if obj and obj.status != StockTransferReceipt.Status.DRAFT:
            return False
        return super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status != StockTransferReceipt.Status.DRAFT:
            return False
        return super().has_delete_permission(request, obj)


class StockTransferReceiptAdmin(admin.ModelAdmin):
    list_display = (
        "number", "transfer", "status", "received_by", "received_at",
        )
    list_filter = ("status", "received_at")
    search_fields = ("number", "transfer__number")
    readonly_fields = ("created_at",)
    inlines = [StockTransferReceiptItemInline]
    actions = ["post_selected_receipts"]

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))

        if obj and obj.status != StockTransferReceipt.Status.DRAFT:
            fields.extend(model_field_names(StockTransferReceipt))

        return tuple(dict.fromkeys(fields))

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status != StockTransferReceipt.Status.DRAFT:
            return False
        return super().has_delete_permission(request, obj)

    @admin.action(description="Post selected draft receipts")
    def post_selected_receipts(self, request, queryset):
        succeeded = 0
        failed = 0

        for receipt in queryset:
            try:
                StockTransferService(
                    transfer_id=receipt.transfer_id,
                    user=request.user,
                ).receive(receipt_id=receipt.pk)
                succeeded += 1
            except (
                ValidationError,
                StockTransfer.DoesNotExist,
                StockTransferReceipt.DoesNotExist,
            ) as exc:
                failed += 1
                self.message_user(
                    request,
                    f"Receipt {receipt.number}: {exc}",
                    level=messages.ERROR,
                )

        if succeeded:
            self.message_user(
                request,
                f"Posted {succeeded} receipt(s).",
                level=messages.SUCCESS,
            )

        if failed:
            self.message_user(
                request,
                f"{failed} receipt(s) could not be posted.",
                level=messages.WARNING,
            )


admin.site.register(StockBalance, StockBalanceAdmin)
admin.site.register(StockMovement, StockMovementAdmin)
admin.site.register(StockTransfer, StockTransferAdmin)
admin.site.register(StockTransferReceipt, StockTransferReceiptAdmin)
