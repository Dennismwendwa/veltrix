# orders/services.py

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from catalog.models import Product
from core.models import Branch, Warehouse
from inventory.models import StockBalance
from orders.models import OnlineOrder, OnlineOrderItem, StockReservation


ZERO = Decimal("0.00")
ZERO_QTY = Decimal("0.000")
CENT = Decimal("0.01")
QUANTITY_STEP = Decimal("0.001")


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def quantity_value(value):
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError("Enter a valid product quantity.")

    if not quantity.is_finite() or quantity <= ZERO_QTY:
        raise ValidationError("Product quantities must be positive.")

    if quantity != quantity.quantize(QUANTITY_STEP):
        raise ValidationError("Quantities can have at most 3 decimal places.")

    return quantity


class OnlineOrderService:

    def __init__(self, user=None):
        self.user = user

    @transaction.atomic
    def create_order(
        self,
        *,
        branch_id,
        warehouse_id,
        fulfilment_method,
        customer_name,
        customer_phone,
        items,
        customer_email="",
        delivery_address="",
        delivery_instructions="",
        requested_fulfilment_at=None,
        idempotency_key=None,
    ):
        if not customer_name or not customer_name.strip():
            raise ValidationError("Customer name is required.")

        if not customer_phone or not customer_phone.strip():
            raise ValidationError("Customer phone is required.")

        if fulfilment_method not in OnlineOrder.FulfilmentMethod.values:
            raise ValidationError("Invalid fulfilment method.")

        if (
            fulfilment_method == OnlineOrder.FulfilmentMethod.DELIVERY
            and not delivery_address.strip()
        ):
            raise ValidationError(
                "A delivery address is required for delivery orders."
            )

        # Return the existing order for a repeated checkout request.
        if idempotency_key:
            existing = OnlineOrder.objects.filter(
                idempotency_key=idempotency_key
            ).first()

            if existing:
                return existing

        branch = Branch.objects.filter(
            pk=branch_id,
            is_active=True,
        ).first()

        if not branch:
            raise ValidationError("The selected branch is unavailable.")

        warehouse = Warehouse.objects.filter(
            pk=warehouse_id,
            is_active=True,
        ).first()

        if not warehouse:
            raise ValidationError("The selected warehouse is unavailable.")

        if (
            warehouse.type != Warehouse.Type.BRANCH
            or warehouse.branch_id != branch.pk
        ):
            raise ValidationError(
                "The warehouse must belong to the selected branch."
            )

        # Aggregate duplicate product rows before creating order items.
        requested = {}

        for row in items:
            product_id = row.get("product_id")
            qty = quantity_value(row.get("quantity"))

            if not product_id:
                raise ValidationError("Every item must have a product.")

            requested[product_id] = (
                requested.get(product_id, ZERO_QTY) + qty
            )

        if not requested:
            raise ValidationError("An order must contain at least one item.")

        products = Product.objects.filter(
            pk__in=requested.keys(),
            is_active=True,
            is_published_online=True,
        ).order_by("pk")

        product_map = {product.pk: product for product in products}

        if len(product_map) != len(requested):
            raise ValidationError(
                "One or more products are unavailable for online ordering."
            )

        order = OnlineOrder.objects.create(
            number=f"WEB-{timezone.now():%Y%m%d}-{uuid4().hex[:8].upper()}",
            status=OnlineOrder.Status.DRAFT,
            fulfilment_method=fulfilment_method,
            fulfilment_branch=branch,
            fulfilment_warehouse=warehouse,
            customer_name=customer_name.strip(),
            customer_phone=customer_phone.strip(),
            customer_email=customer_email.strip(),
            delivery_address=delivery_address.strip(),
            delivery_instructions=delivery_instructions.strip(),
            requested_fulfilment_at=requested_fulfilment_at,
            idempotency_key=idempotency_key,
        )

        subtotal = ZERO

        for product_id, qty in sorted(requested.items()):
            product = product_map[product_id]
            unit_price = money(product.selling_price)
            unit_cost = money(product.cost_price)
            line_total = money(qty * unit_price)

            OnlineOrderItem.objects.create(
                order=order,
                product=product,
                sku_snapshot=product.sku,
                product_name_snapshot=product.name,
                quantity=qty,
                unit_price=unit_price,
                unit_cost_snapshot=unit_cost,
                discount_amount=ZERO,
                line_total=line_total,
            )

            subtotal += line_total

        order.subtotal = money(subtotal)
        order.total_amount = order.subtotal
        order.save(update_fields=[
            "subtotal",
            "total_amount",
            "updated_at",
        ])

        return order

    @transaction.atomic
    def place_order(self, *, order_id):
        order = self._lock_order(order_id)

        if order.status != OnlineOrder.Status.DRAFT:
            raise ValidationError("Only draft orders can be placed.")

        items = list(
            OnlineOrderItem.objects.filter(order=order)
            .select_related("product")
            .order_by("product_id")
        )

        if not items:
            raise ValidationError("Cannot place an empty order.")

        balances = self._lock_balances(
            product_ids=[item.product_id for item in items],
            warehouse_id=order.fulfilment_warehouse_id,
        )

        for item in items:
            balance = balances[item.product_id]
            available = balance.quantity_on_hand - balance.quantity_reserved

            reserve_quantity = min(item.quantity, max(available, ZERO_QTY))

            if reserve_quantity > ZERO_QTY:
                self._add_reservation(
                    item=item,
                    warehouse=order.fulfilment_warehouse,
                    balance=balance,
                    quantity=reserve_quantity,
                )

        order.status = OnlineOrder.Status.PLACED
        order.placed_at = timezone.now()
        order.save(update_fields=["status", "placed_at", "updated_at"])

        return order

    @transaction.atomic
    def reserve_newly_available_stock(self, *, order_id):
        """
        Reserve newly available stock against outstanding order quantities.

        Call after the branch receives stock or completes a transfer.
        """

        order = self._lock_order(order_id)

        allowed_statuses = {
            OnlineOrder.Status.PLACED,
            OnlineOrder.Status.CONFIRMED,
            OnlineOrder.Status.PROCESSING,
        }

        if order.status not in allowed_statuses:
            raise ValidationError(
                "Stock can only be reserved for an active order."
            )

        items = list(
            OnlineOrderItem.objects.filter(order=order)
            .select_related("product")
            .order_by("product_id")
        )

        balances = self._lock_balances(
            product_ids=[item.product_id for item in items],
            warehouse_id=order.fulfilment_warehouse_id,
        )

        for item in items:
            outstanding = item.quantity_to_source

            if outstanding <= ZERO_QTY:
                continue

            balance = balances[item.product_id]
            available = balance.quantity_on_hand - balance.quantity_reserved

            reserve_quantity = min(
                outstanding,
                max(available, ZERO_QTY),
            )

            if reserve_quantity > ZERO_QTY:
                self._add_reservation(
                    item=item,
                    warehouse=order.fulfilment_warehouse,
                    balance=balance,
                    quantity=reserve_quantity,
                )

        return order

    @transaction.atomic
    def cancel_order(self, *, order_id):
        """
        Cancel an order and release its active stock reservations.

        Confirmed payments must be handled by the refund workflow first.
        """

        order = self._lock_order(order_id)

        if order.status in {
            OnlineOrder.Status.COMPLETED,
            OnlineOrder.Status.CANCELLED,
            OnlineOrder.Status.EXPIRED,
        }:
            raise ValidationError(
                "This order cannot be cancelled in its current status."
            )

        if order.completed_sale_id:
            raise ValidationError(
                "This order has a linked sale and cannot be cancelled here."
            )

        # Do not silently cancel an order for which money was received.
        if order.payments.filter(
            status="CONFIRMED"
        ).exists():
            raise ValidationError(
                "This order has a confirmed payment. Process the refund "
                "before cancelling the order."
            )

        reservations = list(
            StockReservation.objects.filter(
                order_item__order=order,
                status=StockReservation.Status.ACTIVE,
            )
            .select_related("order_item")
            .order_by("order_item__product_id")
        )

        for reservation in reservations:
            balance = StockBalance.objects.select_for_update().filter(
                product_id=reservation.order_item.product_id,
                warehouse_id=reservation.warehouse_id,
            ).first()

            if not balance:
                raise ValidationError(
                    "A reserved stock balance could not be found."
                )

            if balance.quantity_reserved < reservation.quantity:
                raise ValidationError(
                    "Reserved stock is inconsistent. Investigate before "
                    "cancelling this order."
                )

            balance.quantity_reserved -= reservation.quantity
            balance.save(update_fields=[
                "quantity_reserved",
                "updated_at",
            ])

            reservation.status = StockReservation.Status.RELEASED
            reservation.released_at = timezone.now()
            reservation.save(update_fields=[
                "status",
                "released_at",
                "updated_at",
            ])

        order.status = OnlineOrder.Status.CANCELLED
        order.save(update_fields=["status", "updated_at"])

        return order

    def _lock_order(self, order_id):
        try:
            return OnlineOrder.objects.select_for_update().get(pk=order_id)
        except OnlineOrder.DoesNotExist:
            raise ValidationError("The requested order does not exist.")

    def _lock_balances(self, *, product_ids, warehouse_id):
        """
        Lock balances in consistent product order to reduce deadlock risk.
        """

        balances = {}

        for product_id in sorted(set(product_ids)):
            balance, _ = StockBalance.objects.get_or_create(
                product_id=product_id,
                warehouse_id=warehouse_id,
                defaults={
                    "quantity_on_hand": ZERO_QTY,
                    "quantity_reserved": ZERO_QTY,
                    "reorder_level": ZERO_QTY,
                },
            )

            balance = StockBalance.objects.select_for_update().get(
                pk=balance.pk
            )
            balances[product_id] = balance

        return balances

    def _add_reservation(
        self,
        *,
        item,
        warehouse,
        balance,
        quantity,
    ):
        """
        Create or reactivate the reservation and update its stock balance
        atomically. This model currently allows one reservation row per
        order item and warehouse.
        """

        reservation, created = StockReservation.objects.get_or_create(
            order_item=item,
            warehouse=warehouse,
            defaults={
                "quantity": quantity,
                "status": StockReservation.Status.ACTIVE,
                "idempotency_key": (
                    f"order-reservation:{item.order_id}:item:{item.pk}"
                ),
            },
        )

        if not created:
            if reservation.status == StockReservation.Status.ACTIVE:
                reservation.quantity += quantity
            elif reservation.status in {
                StockReservation.Status.RELEASED,
                StockReservation.Status.EXPIRED,
            }:
                reservation.quantity = quantity
                reservation.status = StockReservation.Status.ACTIVE
                reservation.released_at = None
                reservation.consumed_at = None
            else:
                raise ValidationError(
                    "This order item has a reservation that cannot be reused."
                )

            reservation.save(update_fields=[
                "quantity",
                "status",
                "released_at",
                "consumed_at",
                "updated_at",
            ])

        balance.quantity_reserved += quantity

        if balance.quantity_reserved > balance.quantity_on_hand:
            raise ValidationError(
                "Reservation would exceed the stock available in the warehouse."
            )

        balance.save(update_fields=[
            "quantity_reserved",
            "updated_at",
        ])

        return reservation

