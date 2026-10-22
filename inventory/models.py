from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import Warehouse
from catalog.models import Product


class StockBalance(models.Model):
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="stock_balances",
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="stock_balances",
    )
    quantity_on_hand = models.DecimalField(
        max_digits=14, decimal_places=3, default=0)
    quantity_reserved = models.DecimalField(
        max_digits=14, decimal_places=3, default=0)
    reorder_level = models.DecimalField(
        max_digits=14, decimal_places=3, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["product", "warehouse"],
                name="unique_product_warehouse_stock",),
            models.CheckConstraint(
                condition=Q(quantity_on_hand__gte=0),
                name="stock_on_hand_nonnegative",),
            models.CheckConstraint(
                condition=Q(quantity_reserved__gte=0),
                name="stock_reserved_nonnegative",),
            models.CheckConstraint(
                condition=Q(reorder_level__gte=0),
                name="stock_reorder_level_nonnegative",),
            models.CheckConstraint(
                condition=Q(quantity_reserved__lte=models.F("quantity_on_hand")),
                name="reserved_stock_not_above_on_hand",
            ),
        ]
        indexes = [
            models.Index(fields=["warehouse", "product"]),
        ]

    @property
    def quantity_available(self):
        return self.quantity_on_hand - self.quantity_reserved

    def __str__(self):
        return f"{self.product} at {self.warehouse}"


class StockMovement(models.Model):
    class MovementType(models.TextChoices):
        RECEIPT = "RECEIPT", "Goods Received"
        SALE = "SALE", "Sale"
        TRANSFER_IN = "TRANSFER_IN", "Transfer Received"
        TRANSFER_OUT = "TRANSFER_OUT", "Transfer Dispatched"
        CUSTOMER_RETURN = "CUSTOMER_RETURN", "Customer Return"
        SUPPLIER_RETURN = "SUPPLIER_RETURN", "Supplier Return"
        ADJUSTMENT_IN = "ADJUSTMENT_IN", "Positive Adjustment"
        ADJUSTMENT_OUT = "ADJUSTMENT_OUT", "Negative Adjustment"

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="stock_movements",
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="stock_movements",
    )
    movement_type = models.CharField(
        max_length=20,
        choices=MovementType.choices,
    )
    quantity_delta = models.DecimalField(max_digits=14, decimal_places=3)
    unit_cost = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    # Links this movement to a sale, receipt, transfer or adjustment.
    reference_type = models.CharField(max_length=50, blank=True)
    reference_id = models.PositiveBigIntegerField(null=True, blank=True)

    # Prevents accidental duplicate posting by the same operation.
    idempotency_key = models.CharField(
        max_length=100, unique=True, null=True, blank=True
    )

    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    occurred_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["product", "warehouse", "occurred_at"]),
            models.Index(fields=["reference_type", "reference_id"]),
        ]

    def __str__(self):
        return (
            f"{self.get_movement_type_display()}: "
            f"{self.product} ({self.quantity_delta})"
        )
