from django.db import models


class ProductCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Product categories"

    def __str__(self):
        return self.name


class Brand(models.Model):
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Product(models.Model):
    sku = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=200)
    category = models.ForeignKey(
        ProductCategory,
        on_delete=models.PROTECT,
        related_name="products",
    )
    brand = models.ForeignKey(
        Brand,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="products",
    )
    oem_number = models.CharField(max_length=100, blank=True)
    barcode = models.CharField(
        max_length=100, unique=True, null=True, blank=True
    )
    description = models.TextField(blank=True)

    cost_price = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    selling_price = models.DecimalField(
        max_digits=12, decimal_places=2
    )
    percentage_discount = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    is_published_online = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["name"]),
            models.Index(fields=["category", "is_active"]),
        ]

    def __str__(self):
        return f"{self.sku} - {self.name}"

