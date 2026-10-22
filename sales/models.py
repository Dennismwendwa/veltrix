
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from catalog.models import Product
from core.models import Branch, Warehouse


class Sale(models.Model):
    class Channel(models.TextChoices):
        IN_STORE = "IN_STORE", "Physical Store"
        ONLINE = "ONLINE", "Online Store"

    class FulfilmentMethod(models.TextChoices):
        COUNTER = "COUNTER", "Counter Sale"
        COLLECTION = "COLLECTION", "Branch Collection"
        DELIVERY = "DELIVERY", "Delivery"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    number = models.CharField(max_length=30, unique=True)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    fulfilment_method = models.CharField(
        max_length=12, choices=FulfilmentMethod.choices)
    status = models.CharField(max_length=10,
        choices=Status.choices, default=Status.DRAFT)
    # The branch responsible for the sale.
    branch = models.ForeignKey(Branch,
        on_delete=models.PROTECT, related_name="sales")
    # The warehouse from which this sale consumes stock.
    warehouse = models.ForeignKey(Warehouse,
        on_delete=models.PROTECT, related_name="sales")

    customer_name = models.CharField(max_length=200, blank=True)
    customer_phone = models.CharField(max_length=30, blank=True)
    customer_email = models.EmailField(blank=True)

    subtotal = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"))
    discount_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"))
    tax_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"))
    total_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"))

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="sales_created",)
    sold_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["branch", "status", "created_at"]),
            models.Index(fields=["channel", "status", "created_at"]),
            models.Index(fields=["warehouse", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(subtotal__gte=0),
                name="sale_subtotal_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(discount_amount__gte=0),
                name="sale_discount_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(tax_amount__gte=0),
                name="sale_tax_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(total_amount__gte=0),
                name="sale_total_nonnegative",
            ),
        ]

    def clean(self):
        super().clean()

        if self.warehouse_id and self.branch_id:
            warehouse = self.warehouse

            if (
                warehouse.warehouse_type == Warehouse.WarehouseType.BRANCH
                and warehouse.branch_id != self.branch_id
            ):
                raise ValidationError({
                    "warehouse":
                        "The warehouse must belong to the responsible branch."
                })

        if (
            self.channel == self.Channel.IN_STORE
            and self.fulfilment_method
            != self.FulfilmentMethod.COUNTER
        ):
            raise ValidationError({
                "fulfilment_method":
                    "Physical store sales must use counter fulfilment."
            })

        if (
            self.channel == self.Channel.ONLINE
            and self.fulfilment_method
            == self.FulfilmentMethod.COUNTER
        ):
            raise ValidationError({
                "fulfilment_method":
                    "Online sales must use collection or delivery."
            })

    def __str__(self):
        return self.number


class SaleItem(models.Model):
    sale = models.ForeignKey(
        Sale, on_delete=models.PROTECT, related_name="items")
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT,
        related_name="sale_items")

    # Preserve the product details and prices as they were at sale time.
    sku_snapshot = models.CharField(max_length=50)
    product_name_snapshot = models.CharField(max_length=200)

    quantity = models.DecimalField(
        max_digits=14, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    unit_cost_snapshot = models.DecimalField(
        max_digits=12, decimal_places=2)
    discount_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"))
    line_total = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="sale_item_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0),
                name="sale_item_price_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(unit_cost_snapshot__gte=0),
                name="sale_item_cost_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(discount_amount__gte=0),
                name="sale_item_discount_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(line_total__gte=0),
                name="sale_item_total_nonnegative",
            ),
            models.UniqueConstraint(
                fields=["sale", "product"],
                name="unique_product_per_sale",
            ),
        ]

    def __str__(self):
        return f"{self.sku_snapshot} x {self.quantity}"


class SalePayment(models.Model):
    class Method(models.TextChoices):
        CASH = "CASH", "Cash"
        MPESA = "MPESA", "M-Pesa"
        CARD = "CARD", "Card"
        BANK_TRANSFER = "BANK_TRANSFER", "Bank Transfer"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CONFIRMED = "CONFIRMED", "Confirmed"
        FAILED = "FAILED", "Failed"
        REFUNDED = "REFUNDED", "Refunded"

    sale = models.ForeignKey(Sale,
        on_delete=models.PROTECT, related_name="payments")
    method = models.CharField(max_length=20, choices=Method.choices)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    transaction_reference = models.CharField(max_length=100, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT, related_name="sale_payments_recorded")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="sale_payment_amount_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["sale", "status"]),
            models.Index(fields=["method", "status"]),
        ]

    def __str__(self):
        return f"{self.sale.number} - {self.amount}"

