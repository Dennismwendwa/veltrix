
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from catalog.models import Product
from core.models import Branch, Warehouse
from inventory.models import StockBalance, StockMovement
from sales.models import Sale, SaleItem, SalePayment


CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


class SaleService:
    """Business operations for physical counter sales."""

    def __init__(self, user, sale_id=None):
        self.user = user
        self.sale_id = sale_id

    @transaction.atomic
    def create_counter_sale(
        self, *, branch_id, warehouse_id, items,
        customer_name="", customer_phone="", customer_email="",
    ):
        """Create a draft sale using current catalogue prices."""
        branch = Branch.objects.get(pk=branch_id, is_active=True)
        warehouse = Warehouse.objects.get(
            pk=warehouse_id, is_active=True
        )

        if (
            warehouse.warehouse_type == Warehouse.WarehouseType.BRANCH
            and warehouse.branch_id != branch.pk
        ):
            raise ValidationError(
                "The selected warehouse does not belong to this branch."
            )

        quantities = defaultdict(lambda: Decimal("0"))

        for row in items:
            try:
                product_id = int(row["product_id"])
                quantity = Decimal(str(row["quantity"]))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                raise ValidationError("Invalid product or quantity.")

            if not quantity.is_finite() or quantity <= 0:
                raise ValidationError("Quantities must be positive.")

            quantities[product_id] += quantity

        if not quantities:
            raise ValidationError("A sale must contain at least one item.")

        products = {
            product.pk: product
            for product in Product.objects.filter(
                pk__in=sorted(quantities)
            ).order_by("pk")
        }

        if len(products) != len(quantities):
            raise ValidationError("One or more products do not exist.")

        sale = Sale.objects.create(
            number=f"POS-{timezone.now():%Y%m%d}-{uuid4().hex[:8].upper()}",
            channel=Sale.Channel.IN_STORE,
            fulfilment_method=Sale.FulfilmentMethod.COUNTER,
            branch=branch,
            warehouse=warehouse,
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_email=customer_email,
            created_by=self.user,
        )

        subtotal = ZERO

        for product_id, quantity in quantities.items():
            product = products[product_id]

            if not product.is_active:
                raise ValidationError(
                    f"{product.name} is not available for sale."
                )

            unit_price = money(product.selling_price)
            line_total = money(quantity * unit_price)
            subtotal += line_total

            SaleItem.objects.create(
                sale=sale,
                product=product,
                sku_snapshot=product.sku,
                product_name_snapshot=product.name,
                quantity=quantity,
                unit_price=unit_price,
                unit_cost_snapshot=money(product.cost_price),
                discount_amount=ZERO,
                line_total=line_total,
            )

        sale.subtotal = money(subtotal)
        sale.total_amount = money(subtotal)
        sale.save(update_fields=[
            "subtotal", "total_amount", "updated_at",
        ])

        self.sale_id = sale.pk
        return sale

    @transaction.atomic
    def record_payment(
        self, *, method, amount, transaction_reference="",
        confirmed=False,
    ):
        """Record a payment; confirm it only after verification."""
        sale = self._lock_sale()

        if sale.status != Sale.Status.DRAFT:
            raise ValidationError(
                "Payments can only be recorded against draft sales."
            )

        try:
            amount = money(Decimal(str(amount)))
        except (TypeError, ValueError, ArithmeticError):
            raise ValidationError("Invalid payment amount.")

        if not amount.is_finite() or amount <= 0:
            raise ValidationError("Payment amount must be positive.")

        if method not in SalePayment.Method.values:
            raise ValidationError("Invalid payment method.")

        paid = self._confirmed_payment_total(sale)

        if paid + amount > sale.total_amount:
            raise ValidationError(
                "Payment exceeds the remaining sale balance."
            )

        payment = SalePayment.objects.create(
            sale=sale,
            method=method,
            status=(
                SalePayment.Status.CONFIRMED
                if confirmed
                else SalePayment.Status.PENDING
            ),
            amount=amount,
            transaction_reference=transaction_reference,
            received_at=timezone.now() if confirmed else None,
            recorded_by=self.user,
        )

        return payment

    @transaction.atomic
    def complete(self):
        """Deduct stock and complete a fully paid counter sale."""
        sale = self._lock_sale()

        if sale.status != Sale.Status.DRAFT:
            raise ValidationError("This sale is not a draft.")

        items = list(
            SaleItem.objects.filter(sale=sale)
            .select_related("product")
            .order_by("product_id", "pk")
        )

        if not items:
            raise ValidationError("Cannot complete a sale without items.")

        if self._confirmed_payment_total(sale) < sale.total_amount:
            raise ValidationError(
                "The sale must be fully paid before completion."
            )

        balances = {}

        for product_id in sorted({item.product_id for item in items}):
            balance, _ = StockBalance.objects.get_or_create(
                product_id=product_id,
                warehouse_id=sale.warehouse_id,
                defaults={
                    "quantity_on_hand": Decimal("0"),
                    "quantity_reserved": Decimal("0"),
                    "reorder_level": Decimal("0"),
                },
            )

            balances[product_id] = (
                StockBalance.objects.select_for_update()
                .get(pk=balance.pk)
            )

        # Validate every line before modifying any balance.
        for item in items:
            balance = balances[item.product_id]
            available = balance.quantity_on_hand - balance.quantity_reserved

            if not item.product.is_active:
                raise ValidationError(
                    f"{item.product_name_snapshot} is inactive."
                )

            if item.quantity > available:
                raise ValidationError(
                    f"Insufficient stock for {item.product_name_snapshot}. "
                    f"Available: {available}; requested: {item.quantity}."
                )

        for item in items:
            balance = balances[item.product_id]
            balance.quantity_on_hand -= item.quantity
            balance.save(update_fields=[
                "quantity_on_hand", "updated_at",
            ])

            StockMovement.objects.create(
                product_id=item.product_id,
                warehouse_id=sale.warehouse_id,
                movement_type=StockMovement.MovementType.SALE,
                quantity_delta=-item.quantity,
                unit_cost=item.unit_cost_snapshot,
                reference_type="SALE",
                reference_id=sale.pk,
                idempotency_key=f"sale-item:{item.pk}:out",
                notes=f"Sale {sale.number}",
                created_by=self.user,
                occurred_at=timezone.now(),
            )

        sale.status = Sale.Status.COMPLETED
        sale.sold_at = timezone.now()
        sale.completed_at = sale.sold_at
        sale.save(update_fields=[
            "status", "sold_at", "completed_at", "updated_at",
        ])

        return sale

    def _lock_sale(self):
        if self.sale_id is None:
            raise ValidationError("Create or select a sale first.")

        return Sale.objects.select_for_update().get(pk=self.sale_id)

    @staticmethod
    def _confirmed_payment_total(sale):
        return (
            sale.payments.filter(
                status=SalePayment.Status.CONFIRMED
            ).aggregate(total=Sum("amount"))["total"]
            or ZERO
        )

