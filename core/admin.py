from django.contrib import admin

from .models import Branch, Warehouse


class BranchAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "address", "is_active")
    search_fields = ("code", "name")
    list_filter = ("is_active",)

class WarehouseAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "warehouse_type", "branch", "is_active")
    search_fields = ("code", "name", "branch__name")
    list_filter = ("warehouse_type", "is_active")


admin.site.register(Branch, BranchAdmin)
admin.site.register(Warehouse, WarehouseAdmin)

