import logging
import re
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests
from django.conf import settings

from inventory.product_matching import get_normalized_product_names, normalize_product_name

from .models import PurchaseOrder


logger = logging.getLogger(__name__)

INVOICE_TOTAL_LABELS = {
    "invoice_net_total": (
        "gesamt netto",
        "net total",
        "subtotal",
        "sub total",
        "netto",
    ),
    "invoice_tax_total": (
        "ust",
        "ust.",
        "mwst",
        "vat",
        "tax",
    ),
    "invoice_grand_total": (
        "gesamtbetrag",
        "grand total",
        "invoice total",
        "amount due",
        "total due",
        "brutto",
    ),
}

PRESERVED_INVOICE_METADATA_KEYS = (
    "ocr_source",
    "ocr_text",
    "invoice_net_total",
    "invoice_tax_total",
    "invoice_grand_total",
    "invoice_tax_breakdown",
)
PRESERVED_FINANCIAL_KEYS = PRESERVED_INVOICE_METADATA_KEYS + (
    "effective_items",
    "effective_net_total",
    "effective_tax_total",
    "effective_grand_total",
)


class InvoiceAnalysisError(Exception):
    pass


class InvoiceConfigurationError(InvoiceAnalysisError):
    pass


def _quantize_money(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _normalize_decimal_text(value):
    text = str(value).strip().replace(" ", "").replace("\xa0", "")
    text = re.sub(r"[^0-9,.\-]", "", text)
    if not text:
        return ""

    comma_index = text.rfind(",")
    dot_index = text.rfind(".")

    if comma_index != -1 and dot_index != -1:
        if comma_index > dot_index:
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif comma_index != -1:
        decimals = len(text) - comma_index - 1
        if decimals in {1, 2}:
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif text.count(".") > 1:
        last_dot_index = text.rfind(".")
        decimals = len(text) - last_dot_index - 1
        if decimals in {1, 2}:
            text = text[:last_dot_index].replace(".", "") + "." + text[last_dot_index + 1 :]
        else:
            text = text.replace(".", "")

    return text


def _to_decimal(value, default=None):
    if value in (None, ""):
        return default
    if isinstance(value, Decimal):
        return value
    try:
        cleaned = _normalize_decimal_text(value)
        if not cleaned:
            return default
        return Decimal(cleaned)
    except (InvalidOperation, TypeError, ValueError):
        return default


def _change_label(difference):
    if difference > 0:
        return "Increased"
    if difference < 0:
        return "Decreased"
    return "Same"


def _field_value(field):
    if field is None:
        return None
    if not isinstance(field, dict):
        return field

    if "valueObject" in field and field["valueObject"] is not None:
        return field.get("valueObject")

    if "valueArray" in field and field["valueArray"] is not None:
        return field.get("valueArray")

    if "valueCurrency" in field:
        value_currency = field.get("valueCurrency") or {}
        if value_currency.get("amount") not in (None, ""):
            return value_currency.get("amount")

    for key in (
        "valueString",
        "valueNumber",
        "valueInteger",
        "valueDate",
        "valueTime",
        "valuePhoneNumber",
        "valueBoolean",
        "value",
    ):
        if key in field and field[key] not in (None, ""):
            return field[key]

    if "content" in field and field["content"] not in (None, ""):
        return field.get("content")

    return None


def _money_matches(left_value, right_value):
    if left_value is None or right_value is None:
        return False
    return _quantize_money(left_value) == _quantize_money(right_value)


def _derive_invoice_tax_total(invoice_net_total, invoice_grand_total, fallback_tax_total=None):
    if invoice_net_total is not None and invoice_grand_total is not None:
        return _quantize_money(invoice_grand_total - invoice_net_total)
    return _quantize_money(fallback_tax_total) if fallback_tax_total is not None else None


def _payload_structure_summary(ocr_payload):
    analyze_result = ocr_payload.get("analyzeResult", ocr_payload) if isinstance(ocr_payload, dict) else {}
    documents = []
    if isinstance(analyze_result, dict):
        documents = analyze_result.get("documents") or analyze_result.get("documentResults") or []

    document = documents[0] if documents else None
    fields = document.get("fields") if isinstance(document, dict) else None
    items_field = fields.get("Items") or fields.get("items") if isinstance(fields, dict) else None
    value_array = items_field.get("valueArray") if isinstance(items_field, dict) else None
    first_item = value_array[0] if isinstance(value_array, list) and value_array else None

    return {
        "top_level_keys": list(ocr_payload.keys()) if isinstance(ocr_payload, dict) else [],
        "analyze_result_keys": list(analyze_result.keys()) if isinstance(analyze_result, dict) else [],
        "document_type": type(document).__name__ if document is not None else None,
        "items_field_type": type(items_field).__name__ if items_field is not None else None,
        "items_field_keys": list(items_field.keys()) if isinstance(items_field, dict) else None,
        "items_count": len(value_array) if isinstance(value_array, list) else 0,
        "first_item_type": type(first_item).__name__ if first_item is not None else None,
        "first_item_keys": list(first_item.keys()) if isinstance(first_item, dict) else None,
    }


def _document_fields_from_payload(ocr_payload):
    analyze_result = ocr_payload.get("analyzeResult", ocr_payload) if isinstance(ocr_payload, dict) else {}
    documents = analyze_result.get("documents") or analyze_result.get("documentResults") or []
    if not documents:
        return {}, analyze_result

    document = documents[0] if isinstance(documents[0], dict) else {}
    return document.get("fields") or {}, analyze_result


def _item_field(item_object, *field_names):
    if not isinstance(item_object, dict):
        return None
    for field_name in field_names:
        if field_name in item_object:
            return _field_value(item_object.get(field_name))
    return None


def _raw_item_text(raw_item, item_value):
    if isinstance(item_value, str):
        return item_value
    if isinstance(raw_item, str):
        return raw_item
    if isinstance(raw_item, dict):
        content = raw_item.get("content")
        if isinstance(content, str):
            return content
    return ""


def _format_decimal_for_storage(value):
    if value is None:
        return None
    return str(_quantize_money(value))


def _extract_amount_from_line(line):
    matches = re.findall(r"[-+]?\d[\d.,\s]*\d", line or "")
    if not matches:
        return None
    return _to_decimal(matches[-1])


def _normalize_tax_rate_label(rate_value, fallback_label=None):
    normalized_fallback = " ".join(str(fallback_label or "").split()).strip()
    rate_decimal = _to_decimal(rate_value)
    if rate_decimal is not None:
        if Decimal("0") < rate_decimal <= Decimal("1"):
            rate_decimal *= Decimal("100")
        rate_decimal = _quantize_money(rate_decimal)
        if rate_decimal == rate_decimal.to_integral():
            return f"{int(rate_decimal)}%"
        return f"{rate_decimal.normalize()}%"

    rate_text = " ".join(str(rate_value or "").split()).strip()
    if "%" in rate_text:
        return rate_text

    return normalized_fallback or "Tax"


def _extract_structured_tax_breakdown(fields):
    tax_breakdown = {}
    for field_name in ("TaxDetails", "Taxes", "TaxLines"):
        raw_entries = _field_value(fields.get(field_name)) or []
        if not isinstance(raw_entries, list):
            continue

        for raw_entry in raw_entries:
            item_value = _field_value(raw_entry)
            item_object = item_value if isinstance(item_value, dict) else {}
            if not item_object:
                continue

            amount = _to_decimal(
                _item_field(item_object, "Amount", "TaxAmount", "Value", "Total"),
            )
            if amount is None:
                continue

            rate_label = _normalize_tax_rate_label(
                _item_field(item_object, "TaxRate", "Rate", "Percentage", "Percent", "VatRate"),
                fallback_label=_item_field(item_object, "Description", "Name", "Type"),
            )
            tax_breakdown[rate_label] = tax_breakdown.get(rate_label, Decimal("0.00")) + amount

    return tax_breakdown


def _extract_text_tax_breakdown(raw_text):
    tax_breakdown = {}
    for line in (raw_text or "").splitlines():
        lowered_line = line.lower()
        if not any(label in lowered_line for label in INVOICE_TOTAL_LABELS["invoice_tax_total"]):
            continue

        amount = _extract_amount_from_line(line)
        if amount is None:
            continue

        rate_match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", line)
        rate_label = _normalize_tax_rate_label(rate_match.group(1) if rate_match else None)
        tax_breakdown[rate_label] = tax_breakdown.get(rate_label, Decimal("0.00")) + amount

    return tax_breakdown


def _extract_total_from_fields(fields, *field_names):
    for field_name in field_names:
        value = _to_decimal(_field_value(fields.get(field_name)))
        if value is not None:
            return value
    return None


def _extract_totals_from_text(raw_text):
    extracted_totals = {
        "invoice_net_total": None,
        "invoice_tax_total": None,
        "invoice_grand_total": None,
        "invoice_tax_breakdown": None,
    }

    lines = [line.strip() for line in (raw_text or "").splitlines() if line.strip()]
    saw_tax_line = False

    for line in lines:
        lowered_line = line.lower()
        amount = _extract_amount_from_line(line)
        if amount is None:
            continue

        if any(label in lowered_line for label in INVOICE_TOTAL_LABELS["invoice_grand_total"]):
            extracted_totals["invoice_grand_total"] = amount
            continue

        if any(label in lowered_line for label in INVOICE_TOTAL_LABELS["invoice_net_total"]):
            extracted_totals["invoice_net_total"] = amount
            continue

        if any(label in lowered_line for label in INVOICE_TOTAL_LABELS["invoice_tax_total"]):
            saw_tax_line = True

    if saw_tax_line:
        tax_breakdown = _extract_text_tax_breakdown(raw_text)
        extracted_totals["invoice_tax_breakdown"] = tax_breakdown or None

    return extracted_totals


def _extract_invoice_totals(ocr_payload):
    fields, analyze_result = _document_fields_from_payload(ocr_payload)
    raw_text = (analyze_result.get("content") or "").strip() if isinstance(analyze_result, dict) else ""

    invoice_net_total = _extract_total_from_fields(
        fields,
        "SubTotal",
        "Subtotal",
        "NetTotal",
        "TotalExcludingTax",
        "AmountExcludingTax",
    )
    invoice_tax_total = _extract_total_from_fields(
        fields,
        "TotalTax",
        "TaxTotal",
        "VAT",
        "Tax",
    )
    invoice_grand_total = _extract_total_from_fields(
        fields,
        "InvoiceTotal",
        "AmountDue",
        "Total",
        "GrandTotal",
    )

    tax_breakdown = _extract_structured_tax_breakdown(fields)

    text_totals = _extract_totals_from_text(raw_text)
    invoice_net_total = invoice_net_total if invoice_net_total is not None else text_totals["invoice_net_total"]
    invoice_grand_total = (
        invoice_grand_total
        if invoice_grand_total is not None
        else text_totals["invoice_grand_total"]
    )
    if not tax_breakdown:
        tax_breakdown = text_totals["invoice_tax_breakdown"] or {}
    invoice_tax_total = _derive_invoice_tax_total(
        invoice_net_total,
        invoice_grand_total,
        fallback_tax_total=invoice_tax_total,
    )

    return {
        "invoice_net_total": _format_decimal_for_storage(invoice_net_total),
        "invoice_tax_total": _format_decimal_for_storage(invoice_tax_total),
        "invoice_grand_total": _format_decimal_for_storage(invoice_grand_total),
        "invoice_tax_breakdown": (
            {
                rate_label: _format_decimal_for_storage(amount)
                for rate_label, amount in tax_breakdown.items()
            }
            if tax_breakdown
            else None
        ),
    }


def _extract_invoice_items(ocr_payload):
    summary = _payload_structure_summary(ocr_payload)
    logger.warning("Azure OCR payload structure summary: %s", summary)

    fields, analyze_result = _document_fields_from_payload(ocr_payload)
    documents = analyze_result.get("documents") or analyze_result.get("documentResults") or []

    if not documents:
        return []

    items_field = fields.get("Items") or fields.get("items")
    raw_items = _field_value(items_field) or []

    extracted_items = []
    for raw_item in raw_items:
        item_value = _field_value(raw_item)
        item_object = item_value if isinstance(item_value, dict) else {}
        raw_text = _raw_item_text(raw_item, item_value)

        description = (
            _item_field(item_object, "Description", "Name", "ProductCode", "ItemCode", "Item")
            or raw_text
            or ""
        )
        quantity = _to_decimal(
            _item_field(item_object, "Quantity", "Qty"),
            default=Decimal("0"),
        )
        unit_price = _to_decimal(_item_field(item_object, "UnitPrice", "Price"))
        amount = _to_decimal(_item_field(item_object, "Amount", "LineTotal", "TotalPrice"))
        amount_source = "invoice" if amount is not None else "derived"

        if unit_price is None and amount is not None and quantity not in (None, Decimal("0")):
            unit_price = amount / quantity

        if not description and not raw_text and amount is None and quantity in (None, Decimal("0")):
            logger.warning(
                "Skipping unparseable Azure OCR invoice item. raw_type=%s item_value_type=%s",
                type(raw_item).__name__,
                type(item_value).__name__,
            )
            continue

        extracted_items.append(
            {
                "name": description or "Unrecognized invoice item",
                "normalized_name": normalize_product_name(description),
                "quantity": str(quantity or Decimal("0")),
                "unit_price": str(_quantize_money(unit_price or Decimal("0"))),
                "amount": str(_quantize_money(amount or (unit_price or Decimal("0")) * (quantity or Decimal("0")))),
                "line_total_source": amount_source,
                "raw_text": raw_text,
            }
        )

    return extracted_items


def _guess_content_type(file_name):
    lowered = (file_name or "").lower()
    if lowered.endswith(".pdf"):
        return "application/pdf"
    if lowered.endswith(".png"):
        return "image/png"
    if lowered.endswith(".jpg") or lowered.endswith(".jpeg"):
        return "image/jpeg"
    if lowered.endswith(".tif") or lowered.endswith(".tiff"):
        return "image/tiff"
    return "application/octet-stream"


def extract_invoice_data_with_azure(invoice_file):
    endpoint = (getattr(settings, "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "") or "").strip()
    api_key = (getattr(settings, "AZURE_DOCUMENT_INTELLIGENCE_KEY", "") or "").strip()
    api_version = (
        getattr(settings, "AZURE_DOCUMENT_INTELLIGENCE_API_VERSION", "")
        or "2023-07-31"
    ).strip()
    model_id = (getattr(settings, "AZURE_DOCUMENT_INTELLIGENCE_MODEL", "") or "").strip()

    missing_settings = []
    if not endpoint:
        missing_settings.append("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
    if not api_key:
        missing_settings.append("AZURE_DOCUMENT_INTELLIGENCE_KEY")
    if not model_id:
        missing_settings.append("AZURE_DOCUMENT_INTELLIGENCE_MODEL")

    if missing_settings:
        raise InvoiceConfigurationError(
            "Azure Document Intelligence is not configured. Missing: "
            + ", ".join(missing_settings)
            + "."
        )

    base_endpoint = endpoint.rstrip("/")
    if not base_endpoint.endswith("/formrecognizer"):
        base_endpoint = f"{base_endpoint}/formrecognizer"

    analyze_url = (
        f"{base_endpoint}/documentModels/{model_id}:analyze"
        f"?api-version={api_version}"
    )
    headers = {
        "Ocp-Apim-Subscription-Key": api_key,
        "Content-Type": _guess_content_type(invoice_file.name),
    }

    invoice_file.open("rb")
    try:
        response = requests.post(
            analyze_url,
            headers=headers,
            data=invoice_file.read(),
            timeout=60,
        )
    finally:
        invoice_file.close()

    response.raise_for_status()
    operation_location = response.headers.get("operation-location")
    if not operation_location:
        raise InvoiceAnalysisError("Azure OCR did not return an operation status URL.")

    poll_headers = {"Ocp-Apim-Subscription-Key": api_key}
    for _ in range(60):
        poll_response = requests.get(operation_location, headers=poll_headers, timeout=60)
        poll_response.raise_for_status()
        poll_payload = poll_response.json()
        status = (poll_payload.get("status") or "").lower()
        if status == "succeeded":
            return {
                "source": "azure_document_intelligence",
                "raw_text": (poll_payload.get("analyzeResult", {}) or {}).get("content", ""),
                "invoice_items": _extract_invoice_items(poll_payload),
                **_extract_invoice_totals(poll_payload),
            }
        if status == "failed":
            raise InvoiceAnalysisError("Azure OCR failed to analyze the uploaded invoice.")
        time.sleep(1)

    raise InvoiceAnalysisError("Azure OCR timed out while analyzing the uploaded invoice.")

def _build_financial_source_map(validation_result):
    source_map = {}

    for key in (
        "matched_items",
        "quantity_mismatches",
        "price_mismatches",
        "resolved_quantity_mismatches",
        "confirmed_shortages",
        "accepted_additional_quantities",
        "resolved_price_mismatches",
    ):
        for item in validation_result.get(key) or []:
            po_item_id = item.get("po_item_id")
            if po_item_id is None:
                continue

            source_entry = source_map.setdefault(po_item_id, {})
            if source_entry.get("invoice_item_index") is None and item.get("invoice_item_index") is not None:
                source_entry["invoice_item_index"] = item.get("invoice_item_index")

            if (
                key in {
                    "resolved_quantity_mismatches",
                    "confirmed_shortages",
                    "accepted_additional_quantities",
                }
                and source_entry.get("quantity_resolution") is None
                and item.get("resolution") is not None
            ):
                source_entry["quantity_resolution"] = item.get("resolution")

            if (
                key == "resolved_price_mismatches"
                and source_entry.get("price_decision") is None
                and item.get("decision") is not None
            ):
                source_entry["price_decision"] = item.get("decision")

    return source_map


def apply_financial_validation(
    purchase_order,
    validation_result,
    *,
    quantity_resolutions=None,
    price_decisions=None,
):
    enriched_result = dict(validation_result)
    invoice_items = enriched_result.get("invoice_items") or []
    source_map = _build_financial_source_map(enriched_result)
    quantity_resolutions = {int(key): value for key, value in (quantity_resolutions or {}).items()}
    price_decisions = {int(key): value for key, value in (price_decisions or {}).items()}

    invoice_net_total = _to_decimal(enriched_result.get("invoice_net_total"))
    invoice_grand_total = _to_decimal(enriched_result.get("invoice_grand_total"))
    invoice_tax_total = _derive_invoice_tax_total(
        invoice_net_total,
        invoice_grand_total,
        fallback_tax_total=_to_decimal(enriched_result.get("invoice_tax_total")),
    )

    po_items_by_id = {
        item.id: item
        for item in purchase_order.items.select_related("product").all()
    }
    effective_items = []
    effective_net_total = Decimal("0.00")

    for po_item_id, source_entry in source_map.items():
        po_item = po_items_by_id.get(po_item_id)
        invoice_item_index = source_entry.get("invoice_item_index")
        if po_item is None or invoice_item_index is None or invoice_item_index >= len(invoice_items):
            continue

        invoice_item = invoice_items[invoice_item_index]
        original_quantity = Decimal(str(po_item.quantity))
        original_unit_price = _quantize_money(po_item.unit_price)
        invoice_quantity = _to_decimal(invoice_item.get("quantity"), default=original_quantity)
        invoice_unit_price = _quantize_money(
            _to_decimal(invoice_item.get("unit_price"), default=original_unit_price)
        )
        invoice_line_total = _to_decimal(invoice_item.get("amount") or invoice_item.get("line_total"))
        if invoice_line_total is not None:
            invoice_line_total = _quantize_money(invoice_line_total)

        quantity_resolution = quantity_resolutions.get(
            po_item_id,
            source_entry.get("quantity_resolution"),
        )
        price_decision = price_decisions.get(
            po_item_id,
            source_entry.get("price_decision"),
        )

        effective_quantity = (
            invoice_quantity
            if quantity_resolution in {"shortage", "use_invoice_quantity"}
            else original_quantity
        )
        effective_unit_price = (
            invoice_unit_price
            if price_decision == "accepted"
            else original_unit_price
        )
        effective_line_total = _quantize_money(effective_quantity * effective_unit_price)
        effective_net_total += effective_line_total

        effective_items.append(
            {
                "invoice_item_index": invoice_item_index,
                "po_item_id": po_item.id,
                "product_id": po_item.product_id,
                "product_name": po_item.product.display_name,
                "original_quantity": str(original_quantity),
                "original_unit_price": str(original_unit_price),
                "original_line_total": str(_quantize_money(po_item.line_total)),
                "invoice_quantity": str(invoice_quantity),
                "invoice_unit_price": str(invoice_unit_price),
                "invoice_line_total": None if invoice_line_total is None else str(invoice_line_total),
                "effective_quantity": str(effective_quantity),
                "effective_unit_price": str(effective_unit_price),
                "effective_line_total": str(effective_line_total),
                "quantity_resolution": quantity_resolution or "",
                "price_decision": price_decision or "",
            }
        )
    effective_tax_total = _quantize_money(invoice_tax_total or Decimal("0.00"))
    effective_grand_total = (
        _quantize_money(effective_net_total + effective_tax_total)
        if invoice_tax_total is not None
        else None
    )

    enriched_result["effective_items"] = effective_items
    enriched_result["effective_net_total"] = str(_quantize_money(effective_net_total))
    enriched_result["effective_tax_total"] = (
        str(_quantize_money(invoice_tax_total))
        if invoice_tax_total is not None
        else None
    )
    enriched_result["effective_grand_total"] = (
        str(effective_grand_total)
        if effective_grand_total is not None
        else None
    )
    enriched_result["has_any_mismatch"] = any(
        (
            enriched_result.get("has_product_mismatch"),
            enriched_result.get("has_quantity_mismatch"),
            enriched_result.get("has_price_mismatch"),
        )
    )
    enriched_result["requires_manual_close"] = any(
        (
            enriched_result.get("has_product_mismatch"),
            enriched_result.get("has_quantity_mismatch"),
        )
    )
    enriched_result["can_update_prices"] = False

    return enriched_result


def classify_invoice_against_purchase_order(
    purchase_order,
    invoice_items,
    manual_product_mappings=None,
):
    po_items = list(
        purchase_order.items.select_related("product").prefetch_related("product__aliases")
    )
    manual_product_mappings = {
        int(invoice_item_index): int(product_id)
        for invoice_item_index, product_id in (manual_product_mappings or {}).items()
    }
    unmatched_po_items = []
    matched_pairs = []
    remaining_invoice_items = []

    available_po_items = []
    for po_item in po_items:
        available_po_items.append(
            {
                "po_item": po_item,
                "candidate_names": get_normalized_product_names(po_item.product),
            }
        )

    for invoice_item_index, invoice_item in enumerate(invoice_items):
        normalized_name = invoice_item.get("normalized_name") or normalize_product_name(
            invoice_item.get("name")
        )
        manually_mapped_product_id = manual_product_mappings.get(invoice_item_index)

        if manually_mapped_product_id is not None:
            match = next(
                (
                    available_po_item
                    for available_po_item in available_po_items
                    if available_po_item["po_item"].product_id == manually_mapped_product_id
                ),
                None,
            )
        else:
            match = next(
                (
                    available_po_item
                    for available_po_item in available_po_items
                    if normalized_name and normalized_name in available_po_item["candidate_names"]
                ),
                None,
            )

        if match is None:
            remaining_invoice_items.append(
                {
                    "invoice_item_index": invoice_item_index,
                    "invoice_item": invoice_item,
                }
            )
            continue

        matched_pairs.append(
            {
                "po_item": match["po_item"],
                "invoice_item": invoice_item,
                "invoice_item_index": invoice_item_index,
            }
        )
        available_po_items.remove(match)

    unmatched_po_items = [entry["po_item"] for entry in available_po_items]

    result = {
        "classification": "matched",
        "invoice_items": invoice_items,
        "missing_products": [],
        "extra_products": [],
        "quantity_mismatches": [],
        "price_mismatches": [],
        "matched_items": [],
        "effective_items": [],
        "effective_net_total": None,
        "effective_tax_total": None,
        "effective_grand_total": None,
        "has_product_mismatch": False,
        "has_quantity_mismatch": False,
        "has_price_mismatch": False,
        "has_any_mismatch": False,
        "requires_manual_close": False,
        "can_update_prices": False,
    }

    if unmatched_po_items or remaining_invoice_items:
        result["missing_products"] = [
            {
                "product_id": po_item.product_id,
                "product_name": po_item.product.display_name,
                "quantity": str(po_item.quantity),
            }
            for po_item in unmatched_po_items
        ]
        result["extra_products"] = [
            {
                "invoice_item_index": entry["invoice_item_index"],
                "product_name": entry["invoice_item"].get("name") or "Unrecognized invoice item",
                "quantity": entry["invoice_item"].get("quantity", "0"),
                "unit_price": entry["invoice_item"].get("unit_price", "0.00"),
                "line_total": entry["invoice_item"].get("amount") or entry["invoice_item"].get("line_total"),
                "line_total_source": entry["invoice_item"].get("line_total_source", ""),
            }
            for entry in remaining_invoice_items
        ]

    quantity_mismatches = []
    price_mismatches = []
    matched_items = []

    for matched_pair in matched_pairs:
        po_item = matched_pair["po_item"]
        invoice_item = matched_pair["invoice_item"]
        invoice_item_index = matched_pair["invoice_item_index"]
        po_quantity = Decimal(str(po_item.quantity))
        invoice_quantity = _to_decimal(invoice_item.get("quantity"), default=Decimal("0"))
        quantity_difference = invoice_quantity - po_quantity
        has_quantity_mismatch = po_quantity != invoice_quantity

        if has_quantity_mismatch:
            quantity_mismatches.append(
                {
                    "invoice_item_index": invoice_item_index,
                    "po_item_id": po_item.id,
                    "product_id": po_item.product_id,
                    "product_name": po_item.product.display_name,
                    "po_quantity": str(po_quantity),
                    "invoice_quantity": str(invoice_quantity),
                    "difference": str(quantity_difference),
                    "change_label": _change_label(quantity_difference),
                    "quantity_case": (
                        "over_delivery"
                        if invoice_quantity > po_quantity
                        else "shortage"
                    ),
                }
            )

        before_price = _quantize_money(po_item.unit_price)
        updated_price = _quantize_money(
            _to_decimal(invoice_item.get("unit_price"), default=Decimal("0"))
        )
        price_difference = _quantize_money(updated_price - before_price)
        percentage = None
        if before_price != Decimal("0.00"):
            percentage = _quantize_money((price_difference / before_price) * Decimal("100"))

        has_price_mismatch = before_price != updated_price
        if has_price_mismatch:
            price_mismatches.append(
                {
                    "invoice_item_index": invoice_item_index,
                    "po_item_id": po_item.id,
                    "product_id": po_item.product_id,
                    "product_name": po_item.product.display_name,
                    "before_price": str(before_price),
                    "updated_price": str(updated_price),
                    "difference": str(price_difference),
                    "percentage": "" if percentage is None else str(percentage),
                    "change_label": _change_label(price_difference),
                }
            )

        if not has_quantity_mismatch and not has_price_mismatch:
            matched_items.append(
                {
                    "invoice_item_index": invoice_item_index,
                    "po_item_id": po_item.id,
                    "product_id": po_item.product_id,
                    "product_name": po_item.product.display_name,
                    "quantity": str(po_quantity),
                    "unit_price": str(before_price),
                    "line_total": str(_quantize_money(po_item.line_total)),
                }
            )

    result["quantity_mismatches"] = quantity_mismatches
    result["price_mismatches"] = price_mismatches
    result["matched_items"] = matched_items
    result["has_product_mismatch"] = bool(result["missing_products"] or result["extra_products"])
    result["has_quantity_mismatch"] = bool(quantity_mismatches)
    result["has_price_mismatch"] = bool(price_mismatches)

    if result["has_product_mismatch"]:
        result["classification"] = "product_mismatch"
    elif result["has_quantity_mismatch"]:
        result["classification"] = "quantity_mismatch"
    elif result["has_price_mismatch"]:
        result["classification"] = "price_mismatch"

    result["has_any_mismatch"] = any(
        (
            result["has_product_mismatch"],
            result["has_quantity_mismatch"],
            result["has_price_mismatch"],
        )
    )
    result["requires_manual_close"] = bool(
        result["has_product_mismatch"] or result["has_quantity_mismatch"]
    )
    result["can_update_prices"] = False
    return result


def analyze_purchase_order_invoice(purchase_order):
    extracted_data = extract_invoice_data_with_azure(purchase_order.invoice_file)
    validation_result = classify_invoice_against_purchase_order(
        purchase_order,
        extracted_data["invoice_items"],
    )
    validation_result["ocr_source"] = extracted_data.get("source")
    validation_result["ocr_text"] = extracted_data.get("raw_text", "")
    validation_result["invoice_net_total"] = extracted_data.get("invoice_net_total")
    validation_result["invoice_tax_total"] = extracted_data.get("invoice_tax_total")
    validation_result["invoice_grand_total"] = extracted_data.get("invoice_grand_total")
    validation_result["invoice_tax_breakdown"] = extracted_data.get("invoice_tax_breakdown")
    if validation_result.get("has_any_mismatch"):
        return validation_result
    return apply_financial_validation(purchase_order, validation_result)
