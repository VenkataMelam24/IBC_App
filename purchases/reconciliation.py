from decimal import Decimal

from inventory.models import ProductAlias
from inventory.product_matching import get_normalized_product_names, normalize_product_name


MANUAL_QUANTITY_USE_PO = "use_po_quantity"
MANUAL_QUANTITY_USE_INVOICE = "use_invoice_quantity"
MANUAL_QUANTITY_FULL_RECEIVED = MANUAL_QUANTITY_USE_PO
MANUAL_QUANTITY_SHORTAGE = MANUAL_QUANTITY_USE_INVOICE
MANUAL_PRICE_ACCEPTED = "accepted"
MANUAL_PRICE_KEEP_OLD = "keep_old"
MANUAL_CALCULATION_USE_CORRECT_AMOUNT = "use_correct_amount"


def format_money(value):
    return f"{Decimal(value):.2f}"


def clean_reconciliation_name(value):
    return " ".join((value or "").split()).strip()


def format_quantity_note_value(value):
    quantity = Decimal(str(value))
    if quantity == quantity.to_integral_value():
        return str(int(quantity))
    return format(quantity.normalize(), "f")


def normalize_quantity_resolution(value):
    if value in {MANUAL_QUANTITY_USE_PO, "full_received"}:
        return MANUAL_QUANTITY_USE_PO
    if value in {MANUAL_QUANTITY_USE_INVOICE, "shortage"}:
        return MANUAL_QUANTITY_USE_INVOICE
    return value


def maybe_create_manual_alias(product, alias_name, normalized_names_by_product_id):
    cleaned_alias_name = clean_reconciliation_name(alias_name)
    normalized_alias_name = normalize_product_name(cleaned_alias_name)
    if not normalized_alias_name:
        return False

    normalized_names = normalized_names_by_product_id.setdefault(
        product.pk,
        set(get_normalized_product_names(product)),
    )
    if normalized_alias_name in normalized_names:
        return False

    ProductAlias.objects.create(product=product, alias_name=cleaned_alias_name)
    normalized_names.add(normalized_alias_name)
    return True


def reconciliation_field_name(prefix, item, mapped_invoice_item_indexes):
    invoice_item_index = item.get("invoice_item_index")
    if invoice_item_index is not None and invoice_item_index in mapped_invoice_item_indexes:
        return f"mapped_{prefix}_{invoice_item_index}"
    return f"{prefix}_{item['po_item_id']}"


def build_manual_matched_item(
    po_item,
    *,
    invoice_item_index=None,
    quantity=None,
    unit_price=None,
):
    matched_quantity = Decimal(str(po_item.quantity if quantity is None else quantity))
    matched_unit_price = Decimal(str(po_item.unit_price if unit_price is None else unit_price)).quantize(
        Decimal("0.01")
    )
    line_total = (matched_quantity * matched_unit_price).quantize(Decimal("0.01"))

    return {
        "invoice_item_index": invoice_item_index,
        "po_item_id": po_item.id,
        "product_id": po_item.product_id,
        "product_name": po_item.product.display_name,
        "quantity": format_quantity_note_value(matched_quantity),
        "unit_price": str(matched_unit_price),
        "line_total": str(line_total),
    }
