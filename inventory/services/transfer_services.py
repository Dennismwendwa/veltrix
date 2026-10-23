from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from inventory.utils.transfer_numbers import (
    generate_transfer_number,
)
from inventory.models import (
    StockBalance,
    StockMovement,
    StockTransfer,
    StockTransferItem,
    StockTransferReceipt,
    StockTransferReceiptItem,
)


class StockTransferService:
    """Manage stock transfer approval, dispatch, receipt, and cancellation."""

    def __init__(self, transfer_id, user):
        self.transfer_id = transfer_id
        self.user = user

    @transaction.atomic
    def approve(self):
        transfer = self._lock_transfer()

        if transfer.status != StockTransfer.Status.DRAFT:
            raise ValidationError("Only draft transfers can be approved.")

        items = self._get_transfer_items(transfer)

        if not items:
            raise ValidationError("A transfer must contain at least one item.")

        self._lock_source_balances(transfer, items)
        self._validate_available_stock(transfer, items)

        transfer.status = StockTransfer.Status.APPROVED
        transfer.approved_by = self.user
        transfer.approved_at = timezone.now()
        transfer.save(update_fields=[
            "status", "approved_by", "approved_at", "updated_at",
        ])

        return transfer

    @transaction.atomic
    def dispatch(self):
        transfer = self._lock_transfer()

        if transfer.status != StockTransfer.Status.APPROVED:
            raise ValidationError("Only approved transfers can be dispatched.")

        items = self._get_transfer_items(transfer)

        if not items:
            raise ValidationError("A transfer must contain at least one item.")

        # Recheck stock at dispatch: approval does not reserve stock.
        balances = self._lock_source_balances(transfer, items)
        self._validate_available_stock(transfer, items, balances)

        for item in items:
            balance = balances[item.product_id]
            quantity = item.quantity_requested

            balance.quantity_on_hand -= quantity
            balance.save(update_fields=[
                "quantity_on_hand", "updated_at",
            ])

            self._create_movement(
                product_id=item.product_id,
                warehouse_id=transfer.source_warehouse_id,
                movement_type=StockMovement.MovementType.TRANSFER_OUT,
                quantity=-quantity,
                transfer=transfer,
                idempotency_key=f"transfer:{transfer.pk}:item:{item.pk}:out",
                notes=f"Dispatched to {transfer.destination_warehouse}",
            )

        transfer.status = StockTransfer.Status.DISPATCHED
        transfer.dispatched_by = self.user
        transfer.dispatched_at = timezone.now()
        transfer.save(update_fields=[
            "status", "dispatched_by", "dispatched_at", "updated_at",
        ])

        return transfer

    @transaction.atomic
    def receive(self, *, receipt_id):
        transfer = self._lock_transfer()

        if transfer.status not in {
            StockTransfer.Status.DISPATCHED,
            StockTransfer.Status.PARTIALLY_RECEIVED,
        }:
            raise ValidationError(
                "Only dispatched transfers can be received."
            )

        receipt = (
            StockTransferReceipt.objects
            .select_for_update()
            .get(pk=receipt_id, transfer=transfer)
        )

        if receipt.status != StockTransferReceipt.Status.DRAFT:
            raise ValidationError("Only draft transfer receipts can be posted.")

        receipt_items = list(
            StockTransferReceiptItem.objects
            .select_for_update()
            .filter(receipt=receipt)
            .select_related("transfer_item", "transfer_item__product")
            .order_by("pk")
        )

        if not receipt_items:
            raise ValidationError("The receipt must contain at least one item.")

        transfer_items = self._lock_transfer_items(transfer)

        self._validate_receipt_items(
            transfer, receipt_items, transfer_items
        )

        self._validate_remaining_quantities(
            transfer_items, receipt_items
        )

        self._post_receipt_stock(transfer, receipt_items)

        receipt.status = StockTransferReceipt.Status.POSTED
        receipt.save(update_fields=["status"])

        self._update_transfer_status(transfer, transfer_items)

        return receipt

    @transaction.atomic
    def cancel(self):
        transfer = self._lock_transfer()

        if transfer.status not in {
            StockTransfer.Status.DRAFT,
            StockTransfer.Status.APPROVED,
        }:
            raise ValidationError(
                "A dispatched transfer cannot be cancelled. "
                "It must be reconciled through the receiving process."
            )

        transfer.status = StockTransfer.Status.CANCELLED
        transfer.save(update_fields=["status", "updated_at"])

        return transfer

    # Loading and locking
    def _lock_transfer(self):
        return (
            StockTransfer.objects
            .select_for_update()
            .select_related("source_warehouse", "destination_warehouse")
            .get(pk=self.transfer_id)
        )

    @staticmethod
    def _get_transfer_items(transfer):
        return list(
            StockTransferItem.objects
            .filter(transfer=transfer)
            .select_related("product")
            .order_by("product_id", "pk")
        )

    @staticmethod
    def _lock_transfer_items(transfer):
        return {
            item.pk: item
            for item in (
                StockTransferItem.objects
                .select_for_update()
                .filter(transfer=transfer)
                .select_related("product")
                .order_by("pk")
            )
        }

    @staticmethod
    def _lock_source_balances(transfer, items):
        balances = {}

        # Consistent ordering helps reduce deadlock risk.
        for product_id in sorted({item.product_id for item in items}):
            balance, _ = StockBalance.objects.get_or_create(
                product_id=product_id,
                warehouse_id=transfer.source_warehouse_id,
                defaults={
                    "quantity_on_hand": Decimal("0"),
                    "quantity_reserved": Decimal("0"),
                    "reorder_level": Decimal("0"),
                },
            )

            balances[product_id] = (
                StockBalance.objects
                .select_for_update()
                .get(pk=balance.pk)
            )

        return balances

    # Validation
    @staticmethod
    def _validate_available_stock(transfer, items, balances=None):
        balances = balances or StockTransferService._lock_source_balances(
            transfer, items
        )

        for item in items:
            balance = balances[item.product_id]
            available = (
                balance.quantity_on_hand - balance.quantity_reserved
            )

            if item.quantity_requested <= 0:
                raise ValidationError(
                    f"Transfer quantity for {item.product} must be positive."
                )

            if item.quantity_requested > available:
                raise ValidationError(
                    f"Insufficient available stock for {item.product}. "
                    f"Available: {available}; requested: "
                    f"{item.quantity_requested}."
                )

    @staticmethod
    def _validate_receipt_items(transfer, receipt_items, transfer_items):
        for receipt_item in receipt_items:
            item = transfer_items.get(receipt_item.transfer_item_id)

            if item is None:
                raise ValidationError(
                    "A receipt item does not belong to this transfer."
                )

            delivered = receipt_item.quantity_delivered
            accepted = receipt_item.quantity_accepted
            damaged = receipt_item.quantity_damaged

            if delivered <= 0 or accepted < 0 or damaged < 0:
                raise ValidationError("Receipt quantities are invalid.")

            if accepted + damaged != delivered:
                raise ValidationError(
                    f"For {item.product}, delivered quantity must equal "
                    "accepted plus damaged quantity."
                )

    @staticmethod
    def _validate_remaining_quantities(transfer_items, receipt_items):
        accepted_or_damaged_now = defaultdict(
            lambda: Decimal("0")
        )

        for receipt_item in receipt_items:
            accepted_or_damaged_now[
                receipt_item.transfer_item_id
            ] += (
                receipt_item.quantity_accepted
                + receipt_item.quantity_damaged
            )

        for item_id, quantity_now in accepted_or_damaged_now.items():
            item = transfer_items[item_id]

            already_accounted = (
                StockTransferReceiptItem.objects
                .filter(
                    transfer_item=item,
                    receipt__status=StockTransferReceipt.Status.POSTED,
                )
                .aggregate(
                    accepted=Sum("quantity_accepted"),
                    damaged=Sum("quantity_damaged"),
                )
            )

            accounted_before = (
                already_accounted["accepted"] or Decimal("0")
            ) + (
                already_accounted["damaged"] or Decimal("0")
            )

            if accounted_before + quantity_now > item.quantity_requested:
                raise ValidationError(
                    f"Receipt quantities exceed the dispatched quantity "
                    f"for {item.product}. Dispatched: "
                    f"{item.quantity_requested}; already accounted for: "
                    f"{accounted_before}."
                )

    # Stock and audit records
    def _post_receipt_stock(self, transfer, receipt_items):
        product_ids = sorted({
            item.transfer_item.product_id
            for item in receipt_items
        })

        destination_balances = {}

        for product_id in product_ids:
            balance, _ = StockBalance.objects.get_or_create(
                product_id=product_id,
                warehouse_id=transfer.destination_warehouse_id,
                defaults={
                    "quantity_on_hand": Decimal("0"),
                    "quantity_reserved": Decimal("0"),
                    "reorder_level": Decimal("0"),
                },
            )

            destination_balances[product_id] = (
                StockBalance.objects
                .select_for_update()
                .get(pk=balance.pk)
            )

        for receipt_item in receipt_items:
            transfer_item = receipt_item.transfer_item
            accepted = receipt_item.quantity_accepted
            damaged = receipt_item.quantity_damaged
            product_id = transfer_item.product_id

            balance = destination_balances[product_id]

            if accepted:
                balance.quantity_on_hand += accepted

            # Record accepted and damaged quantities separately.
            if accepted or damaged:
                self._create_movement(
                    product_id=product_id,
                    warehouse_id=transfer.destination_warehouse_id,
                    movement_type=StockMovement.MovementType.TRANSFER_IN,
                    quantity=accepted + damaged,
                    transfer=transfer,
                    idempotency_key=(
                        f"transfer-receipt-item:{receipt_item.pk}:in"
                    ),
                    notes=(
                        f"Received against transfer {transfer.number}; "
                        f"accepted {accepted}, damaged {damaged}."
                    ),
                )

            # Damaged goods are recorded as a separate write-off so
            # they do not remain in saleable stock.
            if damaged:
                self._create_movement(
                    product_id=product_id,
                    warehouse_id=transfer.destination_warehouse_id,
                    movement_type=StockMovement.MovementType.ADJUSTMENT_OUT,
                    quantity=-damaged,
                    transfer=transfer,
                    idempotency_key=(
                        f"transfer-receipt-item:{receipt_item.pk}:damage"
                    ),
                    notes=(
                        f"Damaged goods from transfer {transfer.number}."
                    ),
                )

            if accepted:
                balance.save(update_fields=[
                    "quantity_on_hand", "updated_at",
                ])

    def _create_movement(
        self,
        *,
        product_id,
        warehouse_id,
        movement_type,
        quantity,
        transfer,
        idempotency_key,
        notes,
    ):
        StockMovement.objects.create(
            product_id=product_id,
            warehouse_id=warehouse_id,
            movement_type=movement_type,
            quantity_delta=quantity,
            reference_type="STOCK_TRANSFER",
            reference_id=transfer.pk,
            idempotency_key=idempotency_key,
            notes=notes,
            created_by=self.user,
            occurred_at=timezone.now(),
        )

    @staticmethod
    def _update_transfer_status(transfer, transfer_items):
        totals = {
            row["transfer_item_id"]: row
            for row in (
                StockTransferReceiptItem.objects
                .filter(
                    transfer_item__transfer=transfer,
                    receipt__status=StockTransferReceipt.Status.POSTED,
                )
                .values("transfer_item_id")
                .annotate(
                    accepted=Sum("quantity_accepted"),
                    damaged=Sum("quantity_damaged"),
                )
            )
        }

        all_accounted = True
        any_accounted = False

        for item in transfer_items.values():
            row = totals.get(item.pk, {})
            accepted = row.get("accepted") or Decimal("0")
            damaged = row.get("damaged") or Decimal("0")
            accounted = accepted + damaged

            any_accounted = any_accounted or accounted > 0

            if accounted < item.quantity_requested:
                all_accounted = False

        if all_accounted:
            transfer.status = StockTransfer.Status.RECEIVED
        elif any_accounted:
            transfer.status = StockTransfer.Status.PARTIALLY_RECEIVED
        else:
            transfer.status = StockTransfer.Status.DISPATCHED

        transfer.save(update_fields=["status", "updated_at"])


