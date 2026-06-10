from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from .selectors import (
    get_average_po_value,
    get_product_price_trend,
    get_products_with_price_trend,
    get_top_products,
    get_total_po_count,
    get_total_products_count,
    get_total_spend_metrics,
    get_vendor_order_frequency,
    get_vendor_price_change_movement_by_vendor,
    get_vendor_wise_spend,
)


def _parse_date_from_param(value):
    """Parse a YYYY-MM-DD string to start-of-day aware datetime. Returns None if invalid."""
    if not value:
        return None
    try:
        d = date.fromisoformat(value.strip())
        return timezone.make_aware(datetime.combine(d, time.min), timezone.get_current_timezone())
    except (ValueError, AttributeError):
        return None


def _parse_date_to_param(value):
    """Parse a YYYY-MM-DD string to end-of-day aware datetime. Returns None if invalid."""
    if not value:
        return None
    try:
        d = date.fromisoformat(value.strip())
        return timezone.make_aware(datetime.combine(d, time.max), timezone.get_current_timezone())
    except (ValueError, AttributeError):
        return None


def _to_decimal(value, default=Decimal("0.00")):
    if value in (None, ""):
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _euro_display(value):
    return f"€ {_to_decimal(value):,.2f}"


def _json_number(value):
    return float(_to_decimal(value))


def _json_chart_rows(rows, *, label_key, value_key, id_key):
    return {
        "labels": [row[label_key] for row in rows],
        "values": [_json_number(row[value_key]) for row in rows],
        "ids": [row[id_key] for row in rows],
    }


def _json_vendor_price_change_rows(rows):
    return {
        "labels": [row["vendor_name"] for row in rows],
        "values": [_json_number(row["total_movement"]) for row in rows],
        "ids": [row["vendor_id"] for row in rows],
        "increase_counts": [int(row["increase_count"]) for row in rows],
        "decrease_counts": [int(row["decrease_count"]) for row in rows],
        "net_impacts": [_json_number(row["net_impact"]) for row in rows],
        "shares": [_json_number(row["movement_share_percent"]) for row in rows],
    }


def _json_integer_rows(rows, *, label_key, value_key, id_key):
    return {
        "labels": [row[label_key] for row in rows],
        "values": [int(row[value_key]) for row in rows],
        "ids": [row[id_key] for row in rows],
    }


def _sort_rows_desc(rows, *, value_key, label_key):
    return sorted(
        rows,
        key=lambda row: (-int(row[value_key]), str(row[label_key])),
    )


def _json_product_price_trend(trend):
    return {
        "product_id": trend["product_id"],
        "product_name": trend["product_name"],
        "labels": trend["labels"],
        "datasets": [
            {
                "vendor_id": dataset["vendor_id"],
                "vendor_name": dataset["vendor_name"],
                "prices": [
                    None if price is None else _json_number(price)
                    for price in dataset["prices"]
                ],
                "old_prices": [
                    None if price is None else _json_number(price)
                    for price in dataset["old_prices"]
                ],
                "new_prices": [
                    None if price is None else _json_number(price)
                    for price in dataset["new_prices"]
                ],
            }
            for dataset in trend["datasets"]
        ],
    }


def _positive_money_rows(rows, *, value_key):
    return [row for row in rows if _to_decimal(row[value_key]) > Decimal("0.00")]


def _build_top_products_chart_dataset(rows):
    return _json_chart_rows(
        rows,
        label_key="product_name",
        value_key="metric_value",
        id_key="product_id",
    )


def _resolve_selected_trend_product_id(request, trend_product_options):
    available_trend_product_ids = {
        str(row["product_id"]): row["product_id"]
        for row in trend_product_options
    }
    requested_trend_product = (request.GET.get("trend_product") or "").strip()
    if requested_trend_product in available_trend_product_ids:
        return available_trend_product_ids[requested_trend_product]
    if trend_product_options:
        return trend_product_options[0]["product_id"]
    return None


def _build_product_price_trend_payload(selected_trend_product_id):
    product_price_trend = get_product_price_trend(selected_trend_product_id)
    return {
        "selected_trend_product_id": selected_trend_product_id,
        "product_price_trend_chart": _json_product_price_trend(product_price_trend),
        "product_price_trend_has_data": bool(product_price_trend["datasets"]),
        "product_price_trend_empty_message": (
            "No price history available for this product."
            if selected_trend_product_id
            else "No products with price history are available yet."
        ),
    }


@login_required
def product_price_trend_data_view(request):
    trend_product_options = get_products_with_price_trend()
    selected_trend_product_id = _resolve_selected_trend_product_id(request, trend_product_options)
    return JsonResponse(_build_product_price_trend_payload(selected_trend_product_id))


@login_required
def dashboard_view(request):
    # Default to current month when no dates are provided
    today = timezone.localdate()
    default_date_from_str = today.replace(day=1).isoformat()
    default_date_to_str = today.isoformat()

    date_from_str = (request.GET.get("date_from") or "").strip() or default_date_from_str
    date_to_str = (request.GET.get("date_to") or "").strip() or default_date_to_str

    date_from = _parse_date_from_param(date_from_str)
    date_to = _parse_date_to_param(date_to_str)

    trend_product_options = get_products_with_price_trend()
    selected_trend_product_id = _resolve_selected_trend_product_id(request, trend_product_options)

    spend_metrics = get_total_spend_metrics(date_from=date_from, date_to=date_to)
    average_po_value = get_average_po_value(date_from=date_from, date_to=date_to)
    total_products = get_total_products_count()
    total_po_count = get_total_po_count(date_from=date_from, date_to=date_to)
    vendor_wise_spend = get_vendor_wise_spend(date_from=date_from, date_to=date_to)
    vendor_order_frequency = get_vendor_order_frequency(date_from=date_from, date_to=date_to)
    top_products_quantity = get_top_products(by="quantity", date_from=date_from, date_to=date_to)
    top_products_value = get_top_products(by="value", date_from=date_from, date_to=date_to)
    vendor_price_change_movement = get_vendor_price_change_movement_by_vendor(date_from=date_from, date_to=date_to)
    vendor_wise_spend_chart_rows = _positive_money_rows(vendor_wise_spend, value_key="total_spend")
    vendor_order_frequency_chart_rows = _sort_rows_desc(
        vendor_order_frequency,
        value_key="po_count",
        label_key="vendor_name",
    )
    product_price_trend_payload = _build_product_price_trend_payload(selected_trend_product_id)

    context = {
        "date_from": date_from_str,
        "date_to": date_to_str,
        "selected_trend_product_id": selected_trend_product_id or "",
        "kpi_total_spend": _euro_display(spend_metrics["gross_spend"]),
        "kpi_total_spend_net": _euro_display(spend_metrics["net_spend"]),
        "kpi_total_spend_tax": _euro_display(spend_metrics["tax_spend"]),
        "kpi_average_po_value": _euro_display(average_po_value["average_po_value"]),
        "kpi_total_products": total_products["active_product_count"],
        "kpi_total_pos": total_po_count["po_count"],
        "vendor_spend_chart": _json_chart_rows(
            vendor_wise_spend_chart_rows,
            label_key="vendor_name",
            value_key="total_spend",
            id_key="vendor_id",
        ),
        "vendor_frequency_chart": _json_integer_rows(
            vendor_order_frequency_chart_rows,
            label_key="vendor_name",
            value_key="po_count",
            id_key="vendor_id",
        ),
        "top_products_chart": {
            "quantity": _build_top_products_chart_dataset(top_products_quantity),
            "value": _build_top_products_chart_dataset(top_products_value),
        },
        "vendor_price_change_chart": _json_vendor_price_change_rows(vendor_price_change_movement),
        "price_trend_product_options": trend_product_options,
        **product_price_trend_payload,
    }
    return render(request, "analytics/dashboard.html", context)
