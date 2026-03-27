from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from analytics.constants import PriceChangeType, PriceDirection, QuantityChangeType
from analytics.models import FactPriceChangeEvent, FactPurchaseOrder, FactPurchaseOrderItem
from purchases.models import PurchaseOrder

PRICE_UPDATE_ACCEPTED_NOTES = {
    PurchaseOrder.NOTE_PRICE_UPDATED_AND_CLOSED,
    PurchaseOrder.NOTE_PRODUCT_MISMATCH_MANUAL_WITH_PRICE_UPDATE,
    PurchaseOrder.NOTE_QUANTITY_MISMATCH_MANUAL_WITH_PRICE_UPDATE,
    "Financial differences reviewed, closed manually. Matched product prices updated from invoice.",
}


def _to_decimal(value, default=None):
    if value in (None, ""):
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _quantize_money(value):
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _quantize_quantity(value):
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _derive_invoice_tax_total(validation_data):
    invoice_net_total = _to_decimal(validation_data.get("invoice_net_total"))
    invoice_grand_total = _to_decimal(validation_data.get("invoice_grand_total"))
    invoice_tax_total = _to_decimal(validation_data.get("invoice_tax_total"))

    if invoice_tax_total is None and invoice_net_total is not None and invoice_grand_total is not None:
        invoice_tax_total = invoice_grand_total - invoice_net_total

    return {
        "invoice_net_total": _quantize_money(invoice_net_total),
        "invoice_tax_total": _quantize_money(invoice_tax_total),
        "invoice_grand_total": _quantize_money(invoice_grand_total),
    }


def _build_source_map(validation_data):
    source_map = {}

    for key in (
        "matched_items",
        "quantity_mismatches",
        "price_mismatches",
        "resolved_quantity_mismatches",
        "confirmed_shortages",
        "accepted_additional_quantities",
        "resolved_price_mismatches",
        "effective_items",
    ):
        for item in validation_data.get(key) or []:
            po_item_id = item.get("po_item_id")
            invoice_item_index = item.get("invoice_item_index")
            if po_item_id is None:
                continue

            entry = source_map.setdefault(int(po_item_id), {})
            if entry.get("invoice_item_index") is None and invoice_item_index is not None:
                entry["invoice_item_index"] = int(invoice_item_index)

    return source_map


def _build_quantity_resolution_map(validation_data):
    quantity_resolution_map = {}

    for key in (
        "resolved_quantity_mismatches",
        "confirmed_shortages",
        "accepted_additional_quantities",
    ):
        for item in validation_data.get(key) or []:
            po_item_id = item.get("po_item_id")
            resolution = item.get("resolution")
            if po_item_id is None or resolution in (None, ""):
                continue
            quantity_resolution_map[int(po_item_id)] = resolution

    return quantity_resolution_map


def _build_price_decision_map(validation_data):
    price_decision_map = {}

    for item in validation_data.get("resolved_price_mismatches") or []:
        po_item_id = item.get("po_item_id")
        decision = item.get("decision")
        if po_item_id is None or decision in (None, ""):
            continue
        price_decision_map[int(po_item_id)] = decision

    return price_decision_map


def _manual_product_mapping_ids(validation_data):
    return {
        int(product_id)
        for product_id in (validation_data.get("manual_product_mappings") or {}).values()
        if str(product_id).isdigit()
    }


def _accepted_all_price_mismatches(purchase_order, validation_data):
    if validation_data.get("resolved_price_mismatches"):
        return False

    if validation_data.get("has_product_mismatch") or validation_data.get("has_quantity_mismatch"):
        return False

    if not (validation_data.get("price_mismatches") or []):
        return False

    return (purchase_order.validation_note or "").strip() in PRICE_UPDATE_ACCEPTED_NOTES


def _has_syncable_final_state(purchase_order, validation_data):
    if purchase_order.status != PurchaseOrder.STATUS_CLOSED:
        return False

    if validation_data.get("effective_items"):
        return True

    return _accepted_all_price_mismatches(purchase_order, validation_data)


def _determine_quantity_change_type(po_quantity, invoice_quantity, accepted_quantity):
    if invoice_quantity is None or accepted_quantity is None or po_quantity == invoice_quantity:
        return QuantityChangeType.NONE

    if invoice_quantity < po_quantity:
        if accepted_quantity == invoice_quantity:
            return QuantityChangeType.SHORTAGE_CONFIRMED
        return QuantityChangeType.FULL_QUANTITY_CONFIRMED

    if accepted_quantity == invoice_quantity:
        return QuantityChangeType.EXTRA_QTY_ACCEPTED
    return QuantityChangeType.EXTRA_QTY_REJECTED


def _determine_price_change_type(po_unit_price, invoice_unit_price, accepted_unit_price):
    if invoice_unit_price is None or accepted_unit_price is None or po_unit_price == invoice_unit_price:
        return PriceChangeType.NONE

    if accepted_unit_price != invoice_unit_price:
        return PriceChangeType.INVOICE_PRICE_REJECTED

    if invoice_unit_price > po_unit_price:
        return PriceChangeType.PRICE_INCREASE_ACCEPTED
    return PriceChangeType.PRICE_DECREASE_ACCEPTED


def _build_item_payloads(purchase_order):
    validation_data = purchase_order.validation_data or {}
    if not _has_syncable_final_state(purchase_order, validation_data):
        return []

    invoice_items = validation_data.get("invoice_items") or []
    source_map = _build_source_map(validation_data)
    quantity_resolution_map = _build_quantity_resolution_map(validation_data)
    price_decision_map = _build_price_decision_map(validation_data)
    effective_item_map = {
        int(item["po_item_id"]): item
        for item in (validation_data.get("effective_items") or [])
        if item.get("po_item_id") is not None
    }
    manually_mapped_product_ids = _manual_product_mapping_ids(validation_data)
    accepted_all_price_mismatches = _accepted_all_price_mismatches(purchase_order, validation_data)

    payloads = []
    for po_item in purchase_order.items.select_related("product").all():
        product = po_item.product
        source_entry = source_map.get(po_item.id, {})
        effective_item = effective_item_map.get(po_item.id)
        invoice_item_index = (
            effective_item.get("invoice_item_index")
            if effective_item is not None and effective_item.get("invoice_item_index") is not None
            else source_entry.get("invoice_item_index")
        )

        invoice_item = {}
        if invoice_item_index is not None and 0 <= int(invoice_item_index) < len(invoice_items):
            invoice_item = invoice_items[int(invoice_item_index)]

        po_quantity = _quantize_quantity(po_item.quantity)
        po_unit_price = _quantize_money(po_item.unit_price)
        po_line_total = _quantize_money(po_item.line_total)

        invoice_quantity = _quantize_quantity(
            _to_decimal(
                effective_item.get("invoice_quantity") if effective_item else invoice_item.get("quantity")
            )
        )
        invoice_unit_price = _quantize_money(
            _to_decimal(
                effective_item.get("invoice_unit_price") if effective_item else invoice_item.get("unit_price")
            )
        )
        invoice_line_total = _quantize_money(
            _to_decimal(
                effective_item.get("invoice_line_total")
                if effective_item
                else invoice_item.get("amount") or invoice_item.get("line_total")
            )
        )

        if effective_item is not None:
            accepted_quantity = _quantize_quantity(
                _to_decimal(effective_item.get("effective_quantity"), default=po_quantity)
            )
            accepted_unit_price = _quantize_money(
                _to_decimal(effective_item.get("effective_unit_price"), default=po_unit_price)
            )
        else:
            accepted_quantity = po_quantity
            if quantity_resolution_map.get(po_item.id) == "use_invoice_quantity" and invoice_quantity is not None:
                accepted_quantity = invoice_quantity

            accepted_unit_price = po_unit_price
            accepted_price = (
                price_decision_map.get(po_item.id) == "accepted"
                or (
                    accepted_all_price_mismatches
                    and invoice_unit_price is not None
                    and invoice_unit_price != po_unit_price
                )
            )
            if accepted_price:
                accepted_unit_price = invoice_unit_price

        accepted_line_total = _quantize_money(accepted_quantity * accepted_unit_price)
        quantity_change_type = _determine_quantity_change_type(
            po_quantity,
            invoice_quantity,
            accepted_quantity,
        )
        price_change_type = _determine_price_change_type(
            po_unit_price,
            invoice_unit_price,
            accepted_unit_price,
        )

        payloads.append(
            {
                "po_item_id": po_item.id,
                "product": product,
                "product_display_name_snapshot": product.display_name or product.product_name,
                "created_at": purchase_order.created_at,
                "closed_at": purchase_order.closed_at,
                "po_quantity": po_quantity,
                "po_unit_price": po_unit_price,
                "po_line_total": po_line_total,
                "invoice_quantity": invoice_quantity,
                "invoice_unit_price": invoice_unit_price,
                "invoice_line_total": invoice_line_total,
                "accepted_quantity": accepted_quantity,
                "accepted_unit_price": accepted_unit_price,
                "accepted_line_total": accepted_line_total,
                "name_reconciled_flag": product.id in manually_mapped_product_ids,
                "quantity_reconciled_flag": quantity_change_type != QuantityChangeType.NONE,
                "price_reconciled_flag": price_change_type != PriceChangeType.NONE,
                "quantity_change_type": quantity_change_type,
                "price_change_type": price_change_type,
                "quantity_difference": _quantize_quantity(accepted_quantity - po_quantity),
                "unit_price_difference": _quantize_money(accepted_unit_price - po_unit_price),
                "line_total_difference": _quantize_money(accepted_line_total - po_line_total),
            }
        )

    return payloads


def _build_purchase_order_fact_defaults(purchase_order, item_payloads, synced_at):
    validation_data = purchase_order.validation_data or {}
    invoice_totals = _derive_invoice_tax_total(validation_data)

    original_po_total = _quantize_money(
        sum((item["po_line_total"] for item in item_payloads), Decimal("0.00"))
    )
    final_net_total = _quantize_money(
        sum((item["accepted_line_total"] for item in item_payloads), Decimal("0.00"))
    )
    final_tax_total = invoice_totals["invoice_tax_total"]
    final_grand_total = (
        _quantize_money(final_net_total + final_tax_total)
        if final_tax_total is not None
        else None
    )

    quantity_variance_value = _quantize_money(
        sum(
            (
                (item["accepted_quantity"] - item["po_quantity"]) * item["po_unit_price"]
                for item in item_payloads
            ),
            Decimal("0.00"),
        )
    )
    price_variance_value = _quantize_money(
        sum(
            (
                item["accepted_quantity"] * (item["accepted_unit_price"] - item["po_unit_price"])
                for item in item_payloads
            ),
            Decimal("0.00"),
        )
    )

    has_name_reconciliation = any(item["name_reconciled_flag"] for item in item_payloads)
    has_quantity_reconciliation = any(item["quantity_reconciled_flag"] for item in item_payloads)
    has_price_reconciliation = any(item["price_reconciled_flag"] for item in item_payloads)
    was_manually_reconciled = any(
        (
            validation_data.get("manual_reconciliation_complete"),
            has_name_reconciliation,
            has_quantity_reconciliation,
            has_price_reconciliation,
        )
    )

    return {
        "po_number": purchase_order.po_number,
        "vendor": purchase_order.vendor,
        "created_by": purchase_order.created_by,
        "created_at": purchase_order.created_at,
        "closed_at": purchase_order.closed_at,
        "status": purchase_order.status,
        "original_po_total": original_po_total,
        "final_net_total": final_net_total,
        "final_tax_total": final_tax_total,
        "final_grand_total": final_grand_total,
        "invoice_net_total": invoice_totals["invoice_net_total"],
        "invoice_tax_total": invoice_totals["invoice_tax_total"],
        "invoice_grand_total": invoice_totals["invoice_grand_total"],
        "po_item_count": len(item_payloads),
        "was_auto_validated": (
            purchase_order.status == PurchaseOrder.STATUS_CLOSED
            and not was_manually_reconciled
            and (purchase_order.validation_note or "").strip() == PurchaseOrder.NOTE_VALIDATED_SUCCESSFULLY
        ),
        "was_manually_reconciled": was_manually_reconciled,
        "has_name_reconciliation": has_name_reconciliation,
        "has_quantity_reconciliation": has_quantity_reconciliation,
        "has_price_reconciliation": has_price_reconciliation,
        "total_value_variance": _quantize_money(final_net_total - original_po_total),
        "quantity_variance_value": quantity_variance_value,
        "price_variance_value": price_variance_value,
        "last_synced_at": synced_at,
    }


def _build_price_event_payloads(purchase_order, item_payloads, synced_at):
    payloads = []
    changed_at = purchase_order.closed_at or purchase_order.validated_at or timezone.now()

    for item in item_payloads:
        if item["price_change_type"] not in {
            PriceChangeType.PRICE_INCREASE_ACCEPTED,
            PriceChangeType.PRICE_DECREASE_ACCEPTED,
        }:
            continue

        old_price = item["po_unit_price"]
        new_price = item["accepted_unit_price"]
        price_difference = _quantize_money(new_price - old_price)
        price_change_percent = None
        if old_price not in {None, Decimal("0.00")}:
            price_change_percent = _quantize_money((price_difference / old_price) * Decimal("100"))

        payloads.append(
            {
                "product": item["product"],
                "po_number": purchase_order.po_number,
                "vendor": purchase_order.vendor,
                "changed_at": changed_at,
                "old_price": old_price,
                "new_price": new_price,
                "price_difference": price_difference,
                "price_change_percent": price_change_percent,
                "direction": (
                    PriceDirection.INCREASE
                    if new_price > old_price
                    else PriceDirection.DECREASE
                ),
                "accepted_flag": True,
                "last_synced_at": synced_at,
            }
        )

    return payloads


def _upsert_fact_purchase_order(defaults, purchase_order):
    fact, _ = FactPurchaseOrder.objects.update_or_create(
        po=purchase_order,
        defaults=defaults,
    )
    return fact


def _upsert_fact_item(purchase_order, payload, synced_at):
    defaults = {
        "po_number": purchase_order.po_number,
        "vendor": purchase_order.vendor,
        "product_display_name_snapshot": payload["product_display_name_snapshot"],
        "created_at": payload["created_at"],
        "closed_at": payload["closed_at"],
        "po_quantity": payload["po_quantity"],
        "po_unit_price": payload["po_unit_price"],
        "po_line_total": payload["po_line_total"],
        "invoice_quantity": payload["invoice_quantity"],
        "invoice_unit_price": payload["invoice_unit_price"],
        "invoice_line_total": payload["invoice_line_total"],
        "accepted_quantity": payload["accepted_quantity"],
        "accepted_unit_price": payload["accepted_unit_price"],
        "accepted_line_total": payload["accepted_line_total"],
        "name_reconciled_flag": payload["name_reconciled_flag"],
        "quantity_reconciled_flag": payload["quantity_reconciled_flag"],
        "price_reconciled_flag": payload["price_reconciled_flag"],
        "quantity_change_type": payload["quantity_change_type"],
        "price_change_type": payload["price_change_type"],
        "quantity_difference": payload["quantity_difference"],
        "unit_price_difference": payload["unit_price_difference"],
        "line_total_difference": payload["line_total_difference"],
        "last_synced_at": synced_at,
    }
    lookup = {
        "po": purchase_order,
        "product": payload["product"],
    }

    existing_facts = list(FactPurchaseOrderItem.objects.filter(**lookup).order_by("id"))
    if existing_facts:
        fact = existing_facts[0]
        for field_name, value in defaults.items():
            setattr(fact, field_name, value)
        fact.save()
        if len(existing_facts) > 1:
            FactPurchaseOrderItem.objects.filter(pk__in=[item.pk for item in existing_facts[1:]]).delete()
        return fact

    return FactPurchaseOrderItem.objects.create(**lookup, **defaults)


def _upsert_price_change_event(purchase_order, payload):
    defaults = {
        "po_number": purchase_order.po_number,
        "vendor": purchase_order.vendor,
        "changed_at": payload["changed_at"],
        "old_price": payload["old_price"],
        "new_price": payload["new_price"],
        "price_difference": payload["price_difference"],
        "price_change_percent": payload["price_change_percent"],
        "direction": payload["direction"],
        "accepted_flag": payload["accepted_flag"],
        "last_synced_at": payload["last_synced_at"],
    }
    lookup = {
        "po": purchase_order,
        "product": payload["product"],
    }

    existing_events = list(FactPriceChangeEvent.objects.filter(**lookup).order_by("id"))
    if existing_events:
        event = existing_events[0]
        for field_name, value in defaults.items():
            setattr(event, field_name, value)
        event.save()
        if len(existing_events) > 1:
            FactPriceChangeEvent.objects.filter(pk__in=[item.pk for item in existing_events[1:]]).delete()
        return event

    return FactPriceChangeEvent.objects.create(**lookup, **defaults)


def sync_purchase_order_fact(purchase_order):
    synced_at = timezone.now()
    item_payloads = _build_item_payloads(purchase_order)
    if not item_payloads:
        return None

    defaults = _build_purchase_order_fact_defaults(purchase_order, item_payloads, synced_at)
    return _upsert_fact_purchase_order(defaults, purchase_order)


def sync_purchase_order_item_facts(purchase_order):
    synced_at = timezone.now()
    item_payloads = _build_item_payloads(purchase_order)
    if not item_payloads:
        return []

    synced_items = []
    synced_product_ids = set()
    for payload in item_payloads:
        synced_items.append(_upsert_fact_item(purchase_order, payload, synced_at))
        synced_product_ids.add(payload["product"].id)

    FactPurchaseOrderItem.objects.filter(po=purchase_order).exclude(
        product_id__in=synced_product_ids
    ).delete()
    return synced_items


def sync_price_change_events(purchase_order):
    synced_at = timezone.now()
    item_payloads = _build_item_payloads(purchase_order)
    if not item_payloads:
        return []

    event_payloads = _build_price_event_payloads(purchase_order, item_payloads, synced_at)
    synced_events = []
    synced_product_ids = set()

    for payload in event_payloads:
        synced_events.append(_upsert_price_change_event(purchase_order, payload))
        synced_product_ids.add(payload["product"].id)

    stale_events = FactPriceChangeEvent.objects.filter(po=purchase_order)
    if synced_product_ids:
        stale_events = stale_events.exclude(product_id__in=synced_product_ids)
    stale_events.delete()

    return synced_events


@transaction.atomic
def sync_all_po_analytics(purchase_order):
    item_payloads = _build_item_payloads(purchase_order)
    if not item_payloads:
        return {
            "purchase_order_fact": None,
            "item_facts": [],
            "price_change_events": [],
        }

    synced_at = timezone.now()
    purchase_order_fact = _upsert_fact_purchase_order(
        _build_purchase_order_fact_defaults(purchase_order, item_payloads, synced_at),
        purchase_order,
    )

    item_facts = []
    synced_product_ids = set()
    for payload in item_payloads:
        item_facts.append(_upsert_fact_item(purchase_order, payload, synced_at))
        synced_product_ids.add(payload["product"].id)

    FactPurchaseOrderItem.objects.filter(po=purchase_order).exclude(
        product_id__in=synced_product_ids
    ).delete()

    price_change_events = []
    price_event_payloads = _build_price_event_payloads(purchase_order, item_payloads, synced_at)
    price_event_product_ids = set()
    for payload in price_event_payloads:
        price_change_events.append(_upsert_price_change_event(purchase_order, payload))
        price_event_product_ids.add(payload["product"].id)

    stale_events = FactPriceChangeEvent.objects.filter(po=purchase_order)
    if price_event_product_ids:
        stale_events = stale_events.exclude(product_id__in=price_event_product_ids)
    stale_events.delete()

    return {
        "purchase_order_fact": purchase_order_fact,
        "item_facts": item_facts,
        "price_change_events": price_change_events,
    }
