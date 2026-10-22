from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from inventory.models import StockBalance, StockMovement
from purchasing.models import (
    GoodsReceipt,
    GoodsReceiptItem,
    PurchaseOrder,
    PurchaseOrderItem,
)

class GoodsReceiptPostingService:
    """Post supplier receipts and update inventory atomically."""

    def __init__(self, receipt_id, user):
        self.receipt_id = receipt_id
        self.user = user
        self.receipt = None
        self.purchase_order = None
        self.receipt_items = []
        self.po_items = {}

    @transaction.atomic
    def post(self):
        """Entry point: coordinate the complete posting operation."""
        self._load_and_lock_receipt()
        self._validate_receipt()
        self._validate_quantities()

        self._update_stock()
        self._mark_receipt_posted()
        self._update_purchase_order_status()

        return self.receipt

    def _load_and_lock_receipt(self):
        """Lock the receipt and its purchase order."""
        self.receipt = (
            GoodsReceipt.objects
            .select_for_update()
            .get(pk=self.receipt_id)
        )

        self.purchase_order = (
            PurchaseOrder.objects
            .select_for_update()
            .get(pk=self.receipt.purchase_order_id)
        )

        self.receipt_items = list(
            GoodsReceiptItem.objects
            .select_for_update()
            .filter(receipt=self.receipt)
            .select_related(
                "purchase_order_item",
                "purchase_order_item__product",
            )
            .order_by("pk")
        )

        po_item_ids = sorted({
            item.purchase_order_item_id
            for item in self.receipt_items
        })

        self.po_items = {
            item.pk: item
            for item in (
                PurchaseOrderItem.objects
                .select_for_update()
                .filter(pk__in=po_item_ids)
                .order_by("pk")
            )
        }

    def _validate_receipt(self):
        """Validate receipt state and purchase-order relationships."""
        if self.receipt.status != GoodsReceipt.Status.DRAFT:
            raise ValidationError(
                "Only draft goods receipts can be posted."
            )

        allowed_statuses = {
            PurchaseOrder.Status.APPROVED,
            PurchaseOrder.Status.ORDERED,
            PurchaseOrder.Status.PARTIALLY_RECEIVED,
        }

        if self.purchase_order.status not in allowed_statuses:
            raise ValidationError(
                "This purchase order cannot receive goods "
                "in its current status."
            )

        if not self.receipt_items:
            raise ValidationError(
                "A goods receipt must contain at least one item."
            )

        for item in self.receipt_items:
            po_item = self.po_items.get(
                item.purchase_order_item_id
            )

            if po_item is None:
                raise ValidationError(
                    "A receipt item references an invalid "
                    "purchase order line."
                )

            if po_item.purchase_order_id != self.purchase_order.pk:
                raise ValidationError(
                    "Every receipt item must belong to this "
                    "purchase order."
                )

            if (
                po_item.destination_warehouse_id
                != self.receipt.warehouse_id
            ):
                raise ValidationError(
                    f"The destination warehouse for "
                    f"{po_item.product} does not match "
                    "the receipt warehouse."
                )

    def _validate_quantities(self):
        """Prevent invalid quantities and over-receiving."""
        accepted_by_po_item = defaultdict(
            lambda: Decimal("0")
        )

        for item in self.receipt_items:
            delivered = item.quantity_delivered
            accepted = item.quantity_accepted
            rejected = item.quantity_rejected

            if delivered <= 0 or accepted < 0 or rejected < 0:
                raise ValidationError(
                    "Receipt quantities must be valid and non-negative."
                )

            if accepted + rejected != delivered:
                raise ValidationError(
                    f"For {item.purchase_order_item.product}, "
                    "delivered quantity must equal accepted "
                    "plus rejected quantity."
                )

            accepted_by_po_item[
                item.purchase_order_item_id
            ] += accepted

        for po_item_id, accepted_now in accepted_by_po_item.items():
            po_item = self.po_items[po_item_id]

            already_received = (
                GoodsReceiptItem.objects
                .filter(
                    purchase_order_item_id=po_item_id,
                    receipt__status=GoodsReceipt.Status.POSTED,
                )
                .aggregate(total=Sum("quantity_accepted"))["total"]
                or Decimal("0")
            )

            if already_received + accepted_now > po_item.quantity_ordered:
                raise ValidationError(
                    f"Cannot receive more than ordered for "
                    f"{po_item.product}. "
                    f"Ordered: {po_item.quantity_ordered}; "
                    f"already received: {already_received}."
                )

    def _update_stock(self):
        """Add accepted quantities to stock and record movements."""
        for item in self.receipt_items:
            quantity = item.quantity_accepted

            # Rejected items must not increase available stock.
            if quantity == 0:
                continue

            po_item = self.po_items[item.purchase_order_item_id]

            balance, _ = StockBalance.objects.get_or_create(
                product=po_item.product,
                warehouse=self.receipt.warehouse,
                defaults={
                    "quantity_on_hand": Decimal("0"),
                    "quantity_reserved": Decimal("0"),
                    "reorder_level": Decimal("0"),
                },
            )

            balance = (
                StockBalance.objects
                .select_for_update()
                .get(pk=balance.pk)
            )

            balance.quantity_on_hand += quantity
            balance.save(update_fields=[
                "quantity_on_hand",
                "updated_at",
            ])

            StockMovement.objects.create(
                product=po_item.product,
                warehouse=self.receipt.warehouse,
                movement_type=StockMovement.MovementType.RECEIPT,
                quantity_delta=quantity,
                unit_cost=po_item.unit_cost,
                reference_type="GOODS_RECEIPT",
                reference_id=self.receipt.pk,
                idempotency_key=f"goods-receipt-item:{item.pk}",
                notes=f"Received against PO {self.purchase_order.number}",
                created_by=self.user,
                occurred_at=self.receipt.received_at or timezone.now(),
            )

    def _mark_receipt_posted(self):
        self.receipt.status = GoodsReceipt.Status.POSTED
        self.receipt.save(update_fields=["status"])

    def _update_purchase_order_status(self):
        """Recalculate the purchase order's receiving progress."""
        received_totals = {
            row["purchase_order_item_id"]: row["total"]
            for row in (
                GoodsReceiptItem.objects
                .filter(
                    purchase_order_item__purchase_order=self.purchase_order,
                    receipt__status=GoodsReceipt.Status.POSTED,
                )
                .values("purchase_order_item_id")
                .annotate(total=Sum("quantity_accepted"))
            )
        }

        po_items = PurchaseOrderItem.objects.filter(
            purchase_order=self.purchase_order
        )

        all_received = True
        any_received = False

        for po_item in po_items:
            received = received_totals.get(
                po_item.pk, Decimal("0")
            )

            any_received = any_received or received > 0

            if received < po_item.quantity_ordered:
                all_received = False

        if all_received:
            status = PurchaseOrder.Status.RECEIVED
        elif any_received:
            status = PurchaseOrder.Status.PARTIALLY_RECEIVED
        else:
            status = PurchaseOrder.Status.ORDERED

        self.purchase_order.status = status
        self.purchase_order.save(update_fields=["status"])
