from django.db import transaction

from inventory.models import TransferNumberSequence


@transaction.atomic
def generate_transfer_number():
    sequence = (
        TransferNumberSequence.objects
        .select_for_update()
        .get(name="stock_transfer")
    )

    number = sequence.next_value

    sequence.next_value = number + 1
    sequence.save(update_fields=["next_value"])

    return f"TRF-{number:06d}"
