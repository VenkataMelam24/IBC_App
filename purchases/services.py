import logging
import re
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


def _derive_invoice_tax_total(invoice_net_total, invoice_grand_total, fallback_tax_total=None):
    if invoice_net_total is not None and invoice_grand_total is not None:
        return _quantize_money(invoice_grand_total - invoice_net_total)
    return _quantize_money(fallback_tax_total) if fallback_tax_total is not None else None


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


# ---------------------------------------------------------------------------
# Invoice extraction — pdfplumber (digital PDFs) + OCR.space (scanned/images)
# Replaces Azure Document Intelligence
# ---------------------------------------------------------------------------

# Keywords that identify a column-header row in an invoice table
_ITEM_HEADER_KEYWORDS = frozenset({
    # English
    "description", "item", "product", "article", "name", "details",
    "qty", "quantity", "units",
    "unit price", "unit cost", "price",
    "amount", "total", "line total",
    # German
    "bezeichnung", "artikel", "produkt", "pos", "position",
    "menge", "anzahl",
    "einzelpreis", "preis", "stückpreis",
    "gesamtpreis", "betrag",
})

# Keywords that signal the end of the line-item section (totals block)
_ITEM_SECTION_END_KEYWORDS = frozenset({
    # English
    "subtotal", "sub total", "net total", "net amount",
    "vat", "tax", "shipping", "delivery", "discount",
    "grand total", "invoice total", "amount due", "total due", "balance due",
    # German
    "zwischensumme", "netto", "gesamt netto", "nettobetrag",
    "mwst", "ust", "mehrwertsteuer", "steuer",
    "brutto", "gesamtbetrag", "rechnungsbetrag",
    "versand", "rabatt",
})


def _line_has_header_keywords(line):
    lowered = line.lower()
    return sum(1 for kw in _ITEM_HEADER_KEYWORDS if kw in lowered) >= 2


def _line_is_section_end(line):
    lowered = line.lower()
    return any(kw in lowered for kw in _ITEM_SECTION_END_KEYWORDS)


def _parse_line_items_from_text(raw_text):
    """
    Parse invoice line items from raw OCR/extracted text.

    Handles both German (comma as decimal separator) and English (dot) invoices
    with varying layouts. Returns a list of item dicts compatible with the
    existing reconciliation pipeline.

    Strategy:
      - Locate the column-header row; item rows begin on the next line.
      - Stop when hitting a totals/summary row.
      - For each candidate row: last number = line total, second-to-last =
        unit price, optional third-to-last validated as quantity.
    """
    items = []
    lines = [line.strip() for line in (raw_text or "").splitlines() if line.strip()]
    if not lines:
        return items

    # Skip past the column header row
    section_start = 0
    for idx, line in enumerate(lines):
        if _line_has_header_keywords(line):
            section_start = idx + 1
            break

    for line in lines[section_start:]:
        if _line_is_section_end(line):
            break

        if len(line) < 5:
            continue

        number_spans = list(re.finditer(r'\d+(?:[.,]\d+)*', line))
        if len(number_spans) < 2:
            continue

        decimal_values = []
        for span in number_spans:
            val = _to_decimal(span.group())
            if val is not None and val > 0:
                decimal_values.append(val)

        if len(decimal_values) < 2:
            continue

        # Product name: text before the first number; strip leading row-index prefix
        first_num_start = number_spans[0].start()
        name_part = line[:first_num_start].strip()
        name_part = re.sub(r'^\s*#?\d+[\.\)\s:]+', '', name_part).strip()
        name_part = name_part.strip('.-:,;').strip()

        if not name_part or len(name_part) < 2:
            continue

        line_total = decimal_values[-1]
        unit_price = decimal_values[-2]

        if len(decimal_values) >= 3:
            qty_candidate = decimal_values[-3]
            if unit_price > 0:
                # Accept candidate if qty * unit_price matches line_total within 2%
                tolerance = max(qty_candidate * unit_price * Decimal("0.02"), Decimal("0.10"))
                quantity = qty_candidate if abs(qty_candidate * unit_price - line_total) <= tolerance else Decimal("1")
            else:
                quantity = Decimal("1")
        else:
            if unit_price > 0 and line_total >= unit_price:
                qty_derived = _quantize_money(line_total / unit_price)
                quantity = (
                    qty_derived
                    if qty_derived == qty_derived.to_integral() and Decimal("1") <= qty_derived <= Decimal("9999")
                    else Decimal("1")
                )
            else:
                quantity = Decimal("1")

        items.append({
            "name": name_part,
            "normalized_name": normalize_product_name(name_part),
            "quantity": str(_quantize_money(quantity)),
            "unit_price": str(_quantize_money(unit_price)),
            "amount": str(_quantize_money(line_total)),
            "line_total_source": "invoice",
            "raw_text": line,
        })

    return items


def _extract_text_with_pdfplumber(invoice_file):
    """
    Extract embedded text from a digital PDF using pdfplumber.
    Returns the full text string, or None if the PDF has no text layer
    (i.e. it is a scanned image PDF).
    """
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber is not installed — skipping digital PDF extraction.")
        return None

    try:
        invoice_file.open("rb")
        try:
            with pdfplumber.open(invoice_file) as pdf:
                pages_text = [
                    page.extract_text().strip()
                    for page in pdf.pages
                    if page.extract_text()
                ]
                full_text = "\n".join(pages_text).strip()
        finally:
            invoice_file.close()

        return full_text if full_text else None

    except Exception as exc:
        logger.warning("pdfplumber extraction failed: %s", exc)
        return None


def _extract_text_with_ocr_space(invoice_file):
    """
    Extract text from a scanned or image-based invoice using the OCR.space API.

    Free tier limits: 500 requests/day, 25,000/month, max 1 MB per file.
    Get a free API key (no credit card) at https://ocr.space/ocrapi

    Uses OCR Engine 2 with German language setting, which also correctly
    reads English text on the same invoice.
    """
    api_key = (getattr(settings, "OCR_SPACE_API_KEY", "") or "").strip()
    if not api_key:
        raise InvoiceConfigurationError(
            "OCR.space is not configured. "
            "Add OCR_SPACE_API_KEY to your .env file. "
            "Get a free key at https://ocr.space/ocrapi"
        )

    content_type = _guess_content_type(invoice_file.name)
    invoice_file.open("rb")
    try:
        file_content = invoice_file.read()
    finally:
        invoice_file.close()

    try:
        response = requests.post(
            "https://api.ocr.space/parse/image",
            data={
                "apikey": api_key,
                "language": "ger",            # German + Latin alphabet; reads English too
                "isTable": "true",             # preserve table layout for line items
                "OCREngine": "2",              # Engine 2: more accurate on complex layouts
                "scale": "true",               # auto-scale low-resolution scans
                "detectOrientation": "true",   # fix rotated scans automatically
                "isCreateSearchablePdf": "false",
            },
            files={"file": (invoice_file.name, file_content, content_type)},
            timeout=60,
        )
    except requests.RequestException as exc:
        raise InvoiceAnalysisError(f"OCR.space request failed: {exc}") from exc

    if not response.ok:
        raise InvoiceAnalysisError(
            f"OCR.space returned HTTP {response.status_code}. "
            "Check your API key and ensure the file is under 1 MB."
        )

    result = response.json()

    if result.get("IsErroredOnProcessing"):
        error_messages = result.get("ErrorMessage") or []
        error_text = (
            error_messages[0]
            if isinstance(error_messages, list) and error_messages
            else str(error_messages or "Unknown OCR error")
        )
        raise InvoiceAnalysisError(f"OCR.space failed to process the invoice: {error_text}")

    parsed_results = result.get("ParsedResults") or []
    if not parsed_results:
        raise InvoiceAnalysisError("OCR.space returned no results for the invoice.")

    raw_text = "\n".join(
        page.get("ParsedText", "").strip()
        for page in parsed_results
    ).strip()

    if not raw_text:
        raise InvoiceAnalysisError(
            "OCR.space extracted no text from the invoice. "
            "The file may be corrupted or unreadable."
        )

    return raw_text


def extract_invoice_data_with_ocr_space(invoice_file):
    """
    Extract structured invoice data using a two-step approach:
      1. pdfplumber  — free, zero-API-call path for digital PDFs
      2. OCR.space   — free OCR API fallback for scanned / image invoices

    Returns the same structure as the former Azure function so the entire
    reconciliation pipeline works without any changes.
    """
    # Step 1: try free digital-PDF extraction first
    raw_text = _extract_text_with_pdfplumber(invoice_file)
    source = "pdfplumber"

    # Step 2: fall back to OCR.space for scanned / image-based invoices
    if not raw_text:
        raw_text = _extract_text_with_ocr_space(invoice_file)
        source = "ocr_space"

    invoice_items = _parse_line_items_from_text(raw_text)
    totals = _extract_totals_from_text(raw_text)

    invoice_net_total = _to_decimal(totals.get("invoice_net_total"))
    invoice_grand_total = _to_decimal(totals.get("invoice_grand_total"))
    invoice_tax_total = _derive_invoice_tax_total(
        invoice_net_total,
        invoice_grand_total,
        fallback_tax_total=_to_decimal(totals.get("invoice_tax_total")),
    )
    tax_breakdown = totals.get("invoice_tax_breakdown") or {}

    return {
        "source": source,
        "raw_text": raw_text,
        "invoice_items": invoice_items,
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


# ---------------------------------------------------------------------------
# Financial validation & reconciliation (unchanged)
# ---------------------------------------------------------------------------

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
    extracted_data = extract_invoice_data_with_ocr_space(purchase_order.invoice_file)
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
