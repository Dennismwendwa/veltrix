from django.contrib import admin
from .models import StockBalance, StockMovement


class StpckBalanceAdmin(admin.ModelAdmin):
    list_display = ("product", "warehouse", "quantity_on_hand",
                    "quantity_reserved", "reorder_level", "updated_at",
                    )
    list_filter = ("product", "warehouse",)

class StockMovementAdmin(admin.ModelAdmin):
    list_display = ("product", "warehouse", "movement_type", "quantity_delta",
                    "unit_cost",
                    )
    list_filter = ("product", "warehouse", "movement_type",)
    readonly_fields = (
        "product", "warehouse", "movement_type", "quantity_delta",
        "unit_cost", "reference_type", "reference_id",
        "idempotency_key", "notes", "created_by",
        "occurred_at", "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(StockBalance, StpckBalanceAdmin)
admin.site.register(StockMovement, StockMovementAdmin)
