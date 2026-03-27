from decimal import Decimal, ROUND_HALF_UP

from inventory.product_matching import normalize_product_name

from .forms import PurchaseOrderInvoiceForm
from .reconciliation import (
    clean_reconciliation_name,
    format_money,
    format_quantity_note_value,
)

def _display_money(value):
    if value in (None, ""):
        return None
    try:
        return format_money(Decimal(str(value)))
    except Exception:
        return None


def _decimal_or_none(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _quantize_display_decimal(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _derive_display_tax_total(invoice_net_total, invoice_grand_total, invoice_tax_total=None):
    if invoice_net_total is not None and invoice_grand_total is not None:
        return _quantize_display_decimal(invoice_grand_total - invoice_net_total)
    return None


def build_history_display_totals(purchase_order):
    validation_data = purchase_order.validation_data or {}
    invoice_net_total = _decimal_or_none(validation_data.get("invoice_net_total"))
    invoice_grand_total = _decimal_or_none(validation_data.get("invoice_grand_total"))
    invoice_tax_total = _derive_display_tax_total(
        invoice_net_total,
        invoice_grand_total,
        invoice_tax_total=_decimal_or_none(validation_data.get("invoice_tax_total")),
    )

    return {
        "net_total": _display_money(invoice_net_total),
        "tax_total": _display_money(invoice_tax_total),
        "grand_total": _display_money(invoice_grand_total),
        "uses_invoice_totals": any(
            value is not None for value in (invoice_net_total, invoice_tax_total, invoice_grand_total)
        ),
    }


def _line_value_change_text(before_value, after_value):
    before_decimal = _decimal_or_none(before_value)
    after_decimal = _decimal_or_none(after_value)
    if before_decimal is None or after_decimal is None or before_decimal == after_decimal:
        return None

    direction = "increased" if after_decimal > before_decimal else "decreased"
    return (
        f"Line value {direction} from {format_money(before_decimal)} to {format_money(after_decimal)}."
    )


def build_history_reconciliation_summary(purchase_order):
    validation_data = purchase_order.validation_data or {}
    effective_items = validation_data.get("effective_items") or []
    if not effective_items:
        return []

    invoice_items = validation_data.get("invoice_items") or []
    manual_product_mappings = {
        int(invoice_item_index): int(product_id)
        for invoice_item_index, product_id in (validation_data.get("manual_product_mappings") or {}).items()
    }

    summaries_by_product_id = {}

    for effective_item in effective_items:
        product_id = effective_item.get("product_id")
        product_name = effective_item.get("product_name") or "Unknown product"
        po_item_id = effective_item.get("po_item_id")
        invoice_item_index = effective_item.get("invoice_item_index")
        summary_entry = summaries_by_product_id.setdefault(
            product_id,
            {"product_name": product_name, "parts": []},
        )

        parts = []
        original_quantity = _decimal_or_none(effective_item.get("original_quantity"))
        invoice_quantity = _decimal_or_none(effective_item.get("invoice_quantity"))
        effective_quantity = _decimal_or_none(effective_item.get("effective_quantity"))
        original_unit_price = _decimal_or_none(effective_item.get("original_unit_price"))
        invoice_unit_price = _decimal_or_none(effective_item.get("invoice_unit_price"))
        effective_unit_price = _decimal_or_none(effective_item.get("effective_unit_price"))
        original_line_total = _decimal_or_none(effective_item.get("original_line_total"))
        invoice_line_total = _decimal_or_none(effective_item.get("invoice_line_total"))
        effective_line_total = _decimal_or_none(effective_item.get("effective_line_total"))

        if (
            invoice_item_index in manual_product_mappings
            and invoice_item_index is not None
            and invoice_item_index < len(invoice_items)
        ):
            invoice_item_name = clean_reconciliation_name(invoice_items[invoice_item_index].get("name"))
            if invoice_item_name and normalize_product_name(invoice_item_name) != normalize_product_name(product_name):
                parts.append(f'Invoice item name "{invoice_item_name}" was confirmed as this product.')

        if (
            original_quantity is not None
            and invoice_quantity is not None
            and effective_quantity is not None
            and invoice_quantity != original_quantity
        ):
            original_quantity_text = format_quantity_note_value(original_quantity)
            invoice_quantity_text = format_quantity_note_value(invoice_quantity)
            effective_quantity_text = format_quantity_note_value(effective_quantity)

            if invoice_quantity > original_quantity:
                if effective_quantity == invoice_quantity:
                    parts.append(
                        "Extra quantity was received. "
                        f"PO quantity was {original_quantity_text}, invoice quantity was {invoice_quantity_text}. "
                        f"User verified and accepted {effective_quantity_text}."
                    )
                else:
                    parts.append(
                        "Extra quantity was shown on the invoice. "
                        f"PO quantity was {original_quantity_text}, invoice quantity was {invoice_quantity_text}. "
                        f"User verified and kept {effective_quantity_text}."
                    )
            else:
                if effective_quantity == invoice_quantity:
                    parts.append(
                        "Shortage was confirmed. "
                        f"PO quantity was {original_quantity_text}, invoice quantity was {invoice_quantity_text}. "
                        f"User verified and accepted {effective_quantity_text}."
                    )
                else:
                    parts.append(
                        "Full quantity was received. "
                        f"PO quantity was {original_quantity_text}, invoice quantity was {invoice_quantity_text}. "
                        f"User verified and accepted {effective_quantity_text}."
                    )

        if (
            original_unit_price is not None
            and invoice_unit_price is not None
            and effective_unit_price is not None
            and invoice_unit_price != original_unit_price
        ):
            original_unit_price_text = format_money(original_unit_price)
            invoice_unit_price_text = format_money(invoice_unit_price)
            effective_unit_price_text = format_money(effective_unit_price)

            if effective_unit_price == invoice_unit_price:
                parts.append(
                    "Price changed. "
                    f"PO unit price was {original_unit_price_text}, invoice unit price was {invoice_unit_price_text}. "
                    "User verified and accepted the new price."
                )
            else:
                parts.append(
                    "Invoice price was incorrect. "
                    f"PO unit price was {original_unit_price_text}, invoice unit price was {invoice_unit_price_text}. "
                    f"User verified and kept {effective_unit_price_text}."
                )

        line_value_change = _line_value_change_text(
            original_line_total,
            effective_line_total,
        )
        if line_value_change and parts:
            parts.append(line_value_change)

        for part in parts:
            if part not in summary_entry["parts"]:
                summary_entry["parts"].append(part)

    return [
        f'{summary_entry["product_name"]}: {" ".join(summary_entry["parts"])}'
        for summary_entry in summaries_by_product_id.values()
        if summary_entry["parts"]
    ]


def build_reconciliation_product_options(purchase_order):
    options = []
    seen_product_ids = set()

    for item in purchase_order.items.all():
        product = item.product
        if product.pk in seen_product_ids:
            continue

        seen_product_ids.add(product.pk)
        options.append(
            {
                "id": product.pk,
                "po_item_id": item.id,
                "label": product.display_name,
                "group": "po",
                "po_quantity": str(item.quantity),
                "po_unit_price": str(Decimal(str(item.unit_price)).quantize(Decimal("0.01"))),
            }
        )

    return options

def prepare_purchase_orders(purchase_orders, include_invoice_form=True):
    for purchase_order in purchase_orders:
        purchase_order.display_total_amount = sum(
            (item.line_total for item in purchase_order.items.all()),
            Decimal("0.00"),
        )
        if include_invoice_form:
            purchase_order.invoice_form = PurchaseOrderInvoiceForm(instance=purchase_order)
        purchase_order.validation_data = purchase_order.validation_data or {}
        purchase_order.audit_notes = purchase_order.validation_data.get("audit_notes") or []
        purchase_order.reconciliation_summary = build_history_reconciliation_summary(
            purchase_order
        )
        purchase_order.invoice_net_total = _display_money(
            purchase_order.validation_data.get("invoice_net_total")
        )
        purchase_order.invoice_tax_total = _display_money(
            purchase_order.validation_data.get("invoice_tax_total")
        )
        purchase_order.invoice_grand_total = _display_money(
            purchase_order.validation_data.get("invoice_grand_total")
        )
        purchase_order.invoice_tax_breakdown_items = list(
            (purchase_order.validation_data.get("invoice_tax_breakdown") or {}).items()
        )
        history_display_totals = build_history_display_totals(purchase_order)
        purchase_order.history_net_total = history_display_totals["net_total"]
        purchase_order.history_tax_total = history_display_totals["tax_total"]
        purchase_order.history_grand_total = history_display_totals["grand_total"]
        purchase_order.history_uses_invoice_totals = history_display_totals["uses_invoice_totals"]
        purchase_order.history_tax_breakdown_items = (
            purchase_order.invoice_tax_breakdown_items
            if purchase_order.history_uses_invoice_totals
            else []
        )
        purchase_order.effective_net_total = _display_money(
            purchase_order.validation_data.get("effective_net_total")
        )
        purchase_order.effective_tax_total = _display_money(
            purchase_order.validation_data.get("effective_tax_total")
        )
        purchase_order.effective_grand_total = _display_money(
            purchase_order.validation_data.get("effective_grand_total")
        )
        purchase_order.reconciliation_product_options = build_reconciliation_product_options(
            purchase_order
        )
    return purchase_orders
