
from django.contrib import admin

from .models import (
    Supplier,
    PurchaseOrder,
    PurchaseOrderItem,
    GoodsReceipt,
    GoodsReceiptItem,
)


class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    extra = 0


class SupplierAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "phone", "is_active")
    search_fields = ("code", "name", "phone", "email")
    list_filter = ("is_active",)

class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = (
        "number", "supplier", "status", "order_date",
        "expected_delivery_date", "ordered_by",
    )
    search_fields = ("number", "supplier__name")
    list_filter = ("status", "order_date")
    inlines = [PurchaseOrderItemInline]
    readonly_fields = ("created_at", "updated_at")

class GoodsReceiptItemInline(admin.TabularInline):
    model = GoodsReceiptItem
    extra = 0

class GoodsReceiptAdmin(admin.ModelAdmin):
    list_display = (
        "number", "purchase_order", "warehouse",
        "status", "received_at", "received_by",
    )
    search_fields = (
        "number", "purchase_order__number",
        "supplier_delivery_note",
    )
    list_filter = ("status", "warehouse")
    inlines = [GoodsReceiptItemInline]


admin.site.register(Supplier, SupplierAdmin)
admin.site.register(PurchaseOrder, PurchaseOrderAdmin)
admin.site.register(GoodsReceipt, GoodsReceiptAdmin)

