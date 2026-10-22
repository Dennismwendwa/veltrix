from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from catalog.models import Product
from core.models import Warehouse


class Supplier(models.Model):
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=30, unique=True)
    contact_person = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    payment_terms = models.CharField(max_length=150, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.code} - {self.name}"


class PurchaseOrder(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        APPROVED = "APPROVED", "Approved"
        ORDERED = "ORDERED", "Sent to Supplier"
        PARTIALLY_RECEIVED = "PARTIAL", "Partially Received"
        RECEIVED = "RECEIVED", "Fully Received"
        CANCELLED = "CANCELLED", "Cancelled"

    number = models.CharField(max_length=30, unique=True)
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="purchase_orders",
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.DRAFT,
    )

    order_date = models.DateField(default=timezone.localdate)
    expected_delivery_date = models.DateField(null=True, blank=True)

    ordered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_purchase_orders",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_purchase_orders",
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "expected_delivery_date"]),
            models.Index(fields=["supplier", "order_date"]),
        ]

    def clean(self):
        super().clean()

        if (
            self.expected_delivery_date
            and self.expected_delivery_date < self.order_date
        ):
            raise ValidationError({
                "expected_delivery_date":
                    "Expected delivery cannot be before the order date."
            })

        if self.status in {
            self.Status.APPROVED,
            self.Status.ORDERED,
            self.Status.PARTIALLY_RECEIVED,
            self.Status.RECEIVED,
        } and not self.approved_by_id:
            raise ValidationError({
                "approved_by":
                    "An approved purchase order must have an approver."
            })

    def __str__(self):
        return self.number


class PurchaseOrderItem(models.Model):
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.PROTECT,
        related_name="items",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="purchase_order_items",
    )
    destination_warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="incoming_purchase_items",
    )

    quantity_ordered = models.DecimalField(
        max_digits=14, decimal_places=3
    )
    unit_cost = models.DecimalField(
        max_digits=12, decimal_places=2
    )

    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity_ordered__gt=0),
                name="po_item_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_cost__gte=0),
                name="po_item_cost_nonnegative",
            ),
            models.UniqueConstraint(
                fields=[
                    "purchase_order",
                    "product",
                    "destination_warehouse",
                ],
                name="unique_po_product_destination",
            ),
        ]

    def __str__(self):
        return f"{self.product} - {self.quantity_ordered}"

    @property
    def quantity_received(self):
        return sum(
            self.receipt_items.filter(
                receipt__status=GoodsReceipt.Status.POSTED
            ).values_list("quantity_accepted", flat=True)
        )

    @property
    def quantity_outstanding(self):
        return max(
            self.quantity_ordered - self.quantity_received,
            0,
        )


class GoodsReceipt(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        POSTED = "POSTED", "Posted"
        CANCELLED = "CANCELLED", "Cancelled"

    number = models.CharField(max_length=30, unique=True)
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.PROTECT,
        related_name="receipts",
    )

    # Each receipt represents one physical delivery location.
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="goods_receipts",
    )

    supplier_delivery_note = models.CharField(
        max_length=100, blank=True
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.DRAFT,
    )

    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="goods_receipts",
    )
    received_at = models.DateTimeField(default=timezone.now)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["warehouse", "received_at"]),
            models.Index(fields=["purchase_order", "status"]),
        ]

    def clean(self):
        super().clean()

        if self.purchase_order_id:
            if self.purchase_order.status in {
                PurchaseOrder.Status.DRAFT,
                PurchaseOrder.Status.CANCELLED,
            }:
                raise ValidationError({
                    "purchase_order":
                        "Goods can only be received against an approved order."
                })

    def __str__(self):
        return self.number


class GoodsReceiptItem(models.Model):
    receipt = models.ForeignKey(
        GoodsReceipt,
        on_delete=models.PROTECT,
        related_name="items",
    )
    purchase_order_item = models.ForeignKey(
        PurchaseOrderItem,
        on_delete=models.PROTECT,
        related_name="receipt_items",
    )

    quantity_delivered = models.DecimalField(
        max_digits=14, decimal_places=3
    )
    quantity_accepted = models.DecimalField(
        max_digits=14, decimal_places=3
    )
    quantity_rejected = models.DecimalField(
        max_digits=14, decimal_places=3, default=0
    )

    rejection_reason = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity_delivered__gt=0),
                name="receipt_delivered_positive",
            ),
            models.CheckConstraint(
                condition=Q(quantity_accepted__gte=0),
                name="receipt_accepted_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(quantity_rejected__gte=0),
                name="receipt_rejected_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(
                    quantity_accepted__lte=models.F("quantity_delivered")
                ),
                name="receipt_accepted_not_above_delivered",
            ),
            models.CheckConstraint(
                condition=Q(
                    quantity_rejected__lte=models.F("quantity_delivered")
                ),
                name="receipt_rejected_not_above_delivered",
            ),
        ]

    def clean(self):
        super().clean()

        if (
            self.quantity_accepted is not None
            and self.quantity_rejected is not None
            and self.quantity_delivered is not None
            and (
                self.quantity_accepted + self.quantity_rejected
                > self.quantity_delivered
            )
        ):
            raise ValidationError(
                "Accepted and rejected quantities cannot exceed "
                "the delivered quantity."
            )

        if self.receipt_id and self.purchase_order_item_id:
            if (
                self.receipt.purchase_order_id
                != self.purchase_order_item.purchase_order_id
            ):
                raise ValidationError(
                    "The purchase order item must belong to this receipt's "
                    "purchase order."
                )

            if (
                self.receipt.warehouse_id
                != self.purchase_order_item.destination_warehouse_id
            ):
                raise ValidationError(
                    "The receipt warehouse must match the item's "
                    "planned destination warehouse."
                )

    def __str__(self):
        return (
            f"{self.purchase_order_item.product} - "
            f"{self.quantity_accepted} accepted"
        )
