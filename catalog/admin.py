from django.contrib import admin
from .models import ProductCategory, Product, Brand


class ProductCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "description")
    list_filter = ("name", "is_active")
    search_fields = ("name",)

class BrandAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    list_filter = ("name", "is_active")
    search_fields = ("name",)

class ProductAdmin(admin.ModelAdmin):
    list_display = ("sku", "name", "category", "oem_number", "cost_price",
                    "selling_price", "is_published_online", "is_active",
                    "created_at",
                )
    list_filter = ("category", "brand", "is_active", "is_published_online")
    search_fields = ("sku", "name", "oem_number", "barcode")


admin.site.register(ProductCategory, ProductCategoryAdmin)
admin.site.register(Brand, BrandAdmin)
admin.site.register(Product, ProductAdmin)
