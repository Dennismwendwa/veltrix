from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import Warehouse
from catalog.models import Product

class TransferNumberSequence(models.Model):
    """Database-backed sequence for stock transfer numbers."""

    name = models.CharField(
        max_length=30,
        primary_key=True,
    )
    next_value = models.PositiveBigIntegerField(default=1)

    def __str__(self):
        return f"{self.name}: {self.next_value}"

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
    quantity_on_hand = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    quantity_reserved = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    reorder_level = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["product", "warehouse"],
                name="unique_product_warehouse_stock",
            ),
            models.CheckConstraint(
                condition=Q(quantity_on_hand__gte=0),
                name="stock_on_hand_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(quantity_reserved__gte=0),
                name="stock_reserved_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(reorder_level__gte=0),
                name="stock_reorder_level_nonnegative",
            ),
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


class StockTransfer(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        APPROVED = "APPROVED", "Approved"
        DISPATCHED = "DISPATCHED", "Dispatched"
        PARTIALLY_RECEIVED = "PARTIAL", "Partially Received"
        RECEIVED = "RECEIVED", "Received"
        CANCELLED = "CANCELLED", "Cancelled"

    number = models.CharField(max_length=30, unique=True)
    source_warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="outgoing_transfers"
    )
    destination_warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="incoming_transfers"
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requested_stock_transfers",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_stock_transfers",
    )
    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="dispatched_stock_transfers",
    )

    approved_at = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["source_warehouse", "status"]),
            models.Index(fields=["destination_warehouse", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(source_warehouse=models.F("destination_warehouse")),
                name="transfer_warehouses_must_differ",
            ),
        ]

    def clean(self):
        super().clean()

        if (
            self.source_warehouse_id
            and self.destination_warehouse_id
            and self.source_warehouse_id == self.destination_warehouse_id
        ):
            from django.core.exceptions import ValidationError

            raise ValidationError(
                {
                    "destination_warehouse": "The source and destination must be different."
                }
            )

    def __str__(self):
        return self.number


class StockTransferItem(models.Model):
    transfer = models.ForeignKey(
        StockTransfer, on_delete=models.PROTECT, related_name="items"
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="stock_transfer_items"
    )
    quantity_requested = models.DecimalField(max_digits=14, decimal_places=3)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity_requested__gt=0),
                name="transfer_item_quantity_positive",
            ),
            models.UniqueConstraint(
                fields=["transfer", "product"],
                name="unique_product_per_stock_transfer",
            ),
        ]

    @property
    def quantity_dispatched(self):
        return self.quantity_requested

    @property
    def quantity_received(self):
        from django.db.models import Sum

        return self.receipt_items.filter(
            receipt__status=StockTransferReceipt.Status.POSTED
        ).aggregate(total=Sum("quantity_accepted"))["total"] or Decimal("0")

    @property
    def quantity_damaged(self):
        from django.db.models import Sum

        return self.receipt_items.filter(
            receipt__status=StockTransferReceipt.Status.POSTED
        ).aggregate(total=Sum("quantity_damaged"))["total"] or Decimal("0")

    @property
    def quantity_outstanding(self):
        return max(
            self.quantity_dispatched - self.quantity_received - self.quantity_damaged,
            Decimal("0"),
        )

    def __str__(self):
        return f"{self.product} - {self.quantity_requested}"


class StockTransferReceipt(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        POSTED = "POSTED", "Posted"
        CANCELLED = "CANCELLED", "Cancelled"

    number = models.CharField(max_length=30, unique=True)
    transfer = models.ForeignKey(
        StockTransfer, on_delete=models.PROTECT, related_name="receipts"
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="stock_transfer_receipts",
    )
    received_at = models.DateTimeField(default=timezone.now)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["transfer", "status"]),
        ]

    def __str__(self):
        return self.number


class StockTransferReceiptItem(models.Model):
    receipt = models.ForeignKey(
        StockTransferReceipt, on_delete=models.PROTECT, related_name="items"
    )
    transfer_item = models.ForeignKey(
        StockTransferItem, on_delete=models.PROTECT, related_name="receipt_items"
    )
    quantity_delivered = models.DecimalField(max_digits=14, decimal_places=3)
    quantity_accepted = models.DecimalField(max_digits=14, decimal_places=3)
    quantity_damaged = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity_delivered__gt=0),
                name="transfer_receipt_delivered_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity_accepted__gte=0),
                name="transfer_receipt_accepted_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity_damaged__gte=0),
                name="transfer_receipt_damaged_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    quantity_accepted__lte=models.F("quantity_delivered")
                ),
                name="transfer_receipt_accepted_not_above_delivered",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    quantity_damaged__lte=models.F("quantity_delivered")
                ),
                name="transfer_receipt_damaged_not_above_delivered",
            ),
        ]

    def clean(self):
        super().clean()

        if (
            self.quantity_accepted is not None
            and self.quantity_damaged is not None
            and self.quantity_delivered is not None
            and self.quantity_accepted + self.quantity_damaged
            != self.quantity_delivered
        ):
            from django.core.exceptions import ValidationError

            raise ValidationError(
                "Delivered quantity must equal accepted plus damaged."
            )

        if self.receipt_id and self.transfer_item_id:
            if self.receipt.transfer_id != self.transfer_item.transfer_id:
                from django.core.exceptions import ValidationError

                raise ValidationError(
                    "This item does not belong to the receipt's transfer."
                )

    def __str__(self):
        return f"{self.transfer_item.product} - {self.quantity_accepted}"
