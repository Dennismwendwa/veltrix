from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from catalog.models import Product
from core.models import Branch, Warehouse
from sales.models import Sale


class OnlineOrder(models.Model):
    class FulfilmentMethod(models.TextChoices):
        COLLECTION = "COLLECTION", "Branch Collection"
        DELIVERY = "DELIVERY", "Delivery"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PENDING_PAYMENT = "PENDING_PAYMENT", "Pending Payment"
        PLACED = "PLACED", "Placed"
        CONFIRMED = "CONFIRMED", "Confirmed"
        PROCESSING = "PROCESSING", "Processing"
        READY_FOR_COLLECTION = "READY_FOR_COLLECTION", "Ready for Collection"
        OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY", "Out for Delivery"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"
        EXPIRED = "EXPIRED", "Expired"

    number = models.CharField(max_length=30, unique=True)
    status = models.CharField(
        max_length=24, choices=Status.choices,
        default=Status.DRAFT, db_index=True)

    fulfilment_method = models.CharField(
        max_length=12, choices=FulfilmentMethod.choices)
    # The branch responsible for preparing and fulfilling the order.
    fulfilment_branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT,
        related_name="online_orders")
    # The stock location from which this branch fulfils the order.
    fulfilment_warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT,
        related_name="online_orders")

    customer_name = models.CharField(max_length=150)
    customer_phone = models.CharField(max_length=30, db_index=True)
    customer_email = models.EmailField(blank=True)

    delivery_address = models.TextField(blank=True)
    delivery_latitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True)
    delivery_longitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True)
    delivery_instructions = models.TextField(blank=True)
    # Optional dates confirmed by the branch.
    requested_fulfilment_at = models.DateTimeField(null=True, blank=True)
    promised_fulfilment_at = models.DateTimeField(null=True, blank=True)

    subtotal = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"))
    discount_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"))
    tax_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"))
    delivery_fee = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"))
    total_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"))
    # Optional link created when the online order becomes a completed sale.
    completed_sale = models.OneToOneField(
        Sale, on_delete=models.PROTECT,
        related_name="online_order", null=True, blank=True)
    # Protects against duplicate checkout submissions.
    idempotency_key = models.CharField(
        max_length=100, unique=True, null=True, blank=True)

    placed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["fulfilment_branch", "status", "created_at"],
                name="order_branch_status_idx",
            ),
            models.Index(
                fields=["status", "expires_at"],
                name="order_status_expiry_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(subtotal__gte=0)
                    & Q(discount_amount__gte=0)
                    & Q(tax_amount__gte=0)
                    & Q(delivery_fee__gte=0)
                    & Q(total_amount__gte=0)
                ),
                name="online_order_amounts_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(discount_amount__lte=models.F("subtotal")),
                name="online_order_discount_lte_subtotal",
            ),
        ]

    def clean(self):
        super().clean()

        if self.fulfilment_warehouse_id and self.fulfilment_branch_id:
            warehouse = self.fulfilment_warehouse

            if warehouse.type != Warehouse.Type.BRANCH:
                raise models.ValidationError(
                    {
                        "fulfilment_warehouse":
                            "Online orders must be fulfilled from a branch warehouse."
                    }
                )

            if warehouse.branch_id != self.fulfilment_branch_id:
                raise models.ValidationError(
                    {
                        "fulfilment_warehouse":
                            "The warehouse must belong to the fulfilment branch."
                    }
                )

        if (
            self.fulfilment_method == self.FulfilmentMethod.DELIVERY
            and not self.delivery_address.strip()
        ):
            raise models.ValidationError(
                {"delivery_address": "A delivery address is required."}
            )

    def __str__(self):
        return self.number


class OnlineOrderItem(models.Model):
    order = models.ForeignKey(
        OnlineOrder, on_delete=models.PROTECT, related_name="items")
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="online_order_items")
    # Preserve what the customer ordered, even if the catalogue changes.
    sku_snapshot = models.CharField(max_length=80)
    product_name_snapshot = models.CharField(max_length=255)

    quantity = models.DecimalField(
        max_digits=14, decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],)
    unit_price = models.DecimalField(
        max_digits=12, decimal_places=2)
    unit_cost_snapshot = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"))
    discount_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"))
    line_total = models.DecimalField(max_digits=14, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "product"],
                name="unique_product_per_online_order",
            ),
            models.CheckConstraint(
                condition=(
                    Q(quantity__gt=0)
                    & Q(unit_price__gte=0)
                    & Q(unit_cost_snapshot__gte=0)
                    & Q(discount_amount__gte=0)
                    & Q(line_total__gte=0)
                ),
                name="online_order_item_values_valid",
            ),
        ]

    @property
    def quantity_reserved(self):
        return self.reservations.filter(
            status=StockReservation.Status.ACTIVE
        ).aggregate(
            total=models.Sum("quantity")
        )["total"] or Decimal("0.000")

    @property
    def quantity_to_source(self):
        """Quantity not yet reserved at the fulfilment branch."""
        return max(
            Decimal("0.000"),
            self.quantity - self.quantity_reserved,
        )

    def __str__(self):
        return f"{self.order.number} - {self.product_name_snapshot}"


class StockReservation(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        CONSUMED = "CONSUMED", "Consumed"
        RELEASED = "RELEASED", "Released"
        EXPIRED = "EXPIRED", "Expired"

    order_item = models.ForeignKey(
        OnlineOrderItem, on_delete=models.PROTECT,
        related_name="reservations")
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT,
        related_name="stock_reservations")
    # May be partial while the branch sources the remaining quantity.
    quantity = models.DecimalField(
        max_digits=14, decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))])
    status = models.CharField(
        max_length=10, choices=Status.choices,
        default=Status.ACTIVE, db_index=True)

    expires_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    idempotency_key = models.CharField(
        max_length=100, unique=True, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["order_item", "warehouse"],
                name="unique_order_item_warehouse_reservation",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="stock_reservation_quantity_positive",
            ),
        ]
        indexes = [
            models.Index(
                fields=["warehouse", "status"],
                name="reservation_wh_status_idx",
            ),
            models.Index(
                fields=["status", "expires_at"],
                name="reservation_status_expiry_idx",
            ),
        ]

    def clean(self):
        super().clean()

        if self.order_item_id and self.warehouse_id:
            order = self.order_item.order

            if self.warehouse_id != order.fulfilment_warehouse_id:
                raise models.ValidationError(
                    {
                        "warehouse":
                            "Reserve stock at the order's fulfilment warehouse."
                    }
                )

    def __str__(self):
        return (
            f"{self.order_item.order.number} - "
            f"{self.order_item.product_name_snapshot} "
            f"({self.quantity})"
        )


class OrderPayment(models.Model):
    class Method(models.TextChoices):
        MPESA = "MPESA", "M-Pesa"
        CARD = "CARD", "Card"
        BANK_TRANSFER = "BANK_TRANSFER", "Bank Transfer"
        CASH_ON_COLLECTION = "CASH_ON_COLLECTION", "Cash on Collection"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CONFIRMED = "CONFIRMED", "Confirmed"
        FAILED = "FAILED", "Failed"
        REFUNDED = "REFUNDED", "Refunded"

    order = models.ForeignKey(
        OnlineOrder,
        on_delete=models.PROTECT,
        related_name="payments",
    )

    method = models.CharField(max_length=20, choices=Method.choices)
    status = models.CharField(
        max_length=10, choices=Status.choices,
        default=Status.PENDING, db_index=True)
    amount = models.DecimalField(
        max_digits=14, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))])
    transaction_reference = models.CharField(
        max_length=150, blank=True, db_index=True)
    idempotency_key = models.CharField(
        max_length=150, unique=True, null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="online_order_payments_recorded",
        null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["order", "status"],
                name="order_payment_status_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="order_payment_amount_positive",
            ),
        ]

    def __str__(self):
        return f"{self.order.number} - {self.amount} - {self.status}"

