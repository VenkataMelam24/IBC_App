from decimal import Decimal, ROUND_HALF_UP

from django.db.models import (
    Avg,
    Count,
    DateTimeField,
    DecimalField,
    ExpressionWrapper,
    F,
    Max,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Abs, Coalesce

from analytics.constants import PriceDirection
from analytics.models import FactPriceChangeEvent, FactPurchaseOrder, FactPurchaseOrderItem
from analytics.utils import resolve_period_range
from inventory.models import PriceHistory, Product


MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)
QUANTITY_FIELD = DecimalField(max_digits=12, decimal_places=2)
PERCENT_FIELD = DecimalField(max_digits=10, decimal_places=2)
ZERO_MONEY = Value(Decimal("0.00"), output_field=MONEY_FIELD)
ZERO_QUANTITY = Value(Decimal("0.00"), output_field=QUANTITY_FIELD)


def _quantize_money(value):
    return Decimal(str(value or Decimal("0.00"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _quantize_percent(value):
    return Decimal(str(value or Decimal("0.00"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _resolved_period_range(period, reference=None):
    return resolve_period_range(period, reference=reference)


def _filter_fact_queryset_for_period(queryset, period, reference=None):
    period_range = _resolved_period_range(period, reference=reference)
    return queryset.annotate(
        reporting_timestamp=Coalesce("closed_at", "created_at", output_field=DateTimeField())
    ).filter(
        reporting_timestamp__gte=period_range["date_from"],
        reporting_timestamp__lte=period_range["date_to"],
    )


def _filter_price_event_queryset_for_period(queryset, period, reference=None):
    period_range = _resolved_period_range(period, reference=reference)
    return queryset.filter(
        changed_at__gte=period_range["date_from"],
        changed_at__lte=period_range["date_to"],
    )


def get_total_spend_metrics(period, reference=None):
    queryset = _filter_fact_queryset_for_period(FactPurchaseOrder.objects.all(), period, reference=reference)
    aggregates = queryset.aggregate(
        gross_spend=Coalesce(Sum("final_grand_total"), ZERO_MONEY),
        net_spend=Coalesce(Sum("final_net_total"), ZERO_MONEY),
        tax_spend=Coalesce(Sum("final_tax_total"), ZERO_MONEY),
    )
    return {
        "gross_spend": _quantize_money(aggregates["gross_spend"]),
        "net_spend": _quantize_money(aggregates["net_spend"]),
        "tax_spend": _quantize_money(aggregates["tax_spend"]),
    }


def get_average_po_value(period, reference=None):
    queryset = _filter_fact_queryset_for_period(FactPurchaseOrder.objects.all(), period, reference=reference)
    aggregate = queryset.aggregate(average_po_value=Avg("final_grand_total"))
    return {
        "average_po_value": _quantize_money(aggregate["average_po_value"]),
    }


def get_total_products_count():
    return {"active_product_count": Product.objects.filter(is_active=True).count()}


def get_total_po_count(period, reference=None):
    queryset = _filter_fact_queryset_for_period(FactPurchaseOrder.objects.all(), period, reference=reference)
    return {"po_count": queryset.count()}


def get_vendor_wise_spend(period, reference=None):
    queryset = _filter_fact_queryset_for_period(FactPurchaseOrder.objects.select_related("vendor"), period, reference=reference)
    rows = queryset.values("vendor_id", "vendor__name").annotate(
        total_spend=Coalesce(Sum("final_grand_total"), ZERO_MONEY)
    ).order_by("-total_spend", "vendor__name")

    return [
        {
            "vendor_id": row["vendor_id"],
            "vendor_name": row["vendor__name"],
            "total_spend": _quantize_money(row["total_spend"]),
        }
        for row in rows
    ]


def get_vendor_order_frequency(period, reference=None):
    queryset = _filter_fact_queryset_for_period(
        FactPurchaseOrder.objects.select_related("vendor"),
        period,
        reference=reference,
    ).filter(final_grand_total__gt=Decimal("0.00"))
    rows = queryset.values("vendor_id", "vendor__name").annotate(
        po_count=Count("id")
    ).order_by("-po_count", "vendor__name")

    return [
        {
            "vendor_id": row["vendor_id"],
            "vendor_name": row["vendor__name"],
            "po_count": row["po_count"],
        }
        for row in rows
    ]


def get_top_products(period, by="quantity", reference=None, vendor_id=None):
    metric_field = {
        "quantity": "accepted_quantity",
        "value": "accepted_line_total",
    }.get(by)
    if metric_field is None:
        raise ValueError("Unsupported top-products metric. Expected 'quantity' or 'value'.")

    queryset = _filter_fact_queryset_for_period(
        FactPurchaseOrderItem.objects.select_related("product"),
        period,
        reference=reference,
    )
    if vendor_id not in (None, "", "all"):
        queryset = queryset.filter(vendor_id=vendor_id)
    rows = queryset.values("product_id").annotate(
        product_name=Coalesce("product__display_name", "product_display_name_snapshot"),
        metric_value=Coalesce(Sum(metric_field), ZERO_MONEY),
    ).order_by("-metric_value", "product_name")

    return [
        {
            "product_id": row["product_id"],
            "product_name": row["product_name"],
            "metric_value": _quantize_money(row["metric_value"]),
        }
        for row in rows
    ]


def get_frequent_price_changes(period, reference=None):
    base_queryset = _filter_price_event_queryset_for_period(
        FactPriceChangeEvent.objects.filter(accepted_flag=True).select_related("product", "vendor"),
        period,
        reference=reference,
    )
    latest_event_queryset = base_queryset.filter(
        vendor_id=OuterRef("vendor_id"),
        product_id=OuterRef("product_id"),
    ).order_by("-changed_at", "-id")

    rows = base_queryset.values("product_id", "vendor_id", "product__display_name", "vendor__name").annotate(
        change_count=Count("id"),
        latest_changed_at=Max("changed_at"),
        latest_old_price=Subquery(latest_event_queryset.values("old_price")[:1]),
        latest_new_price=Subquery(latest_event_queryset.values("new_price")[:1]),
        latest_difference=Subquery(latest_event_queryset.values("price_difference")[:1]),
        latest_percent=Subquery(latest_event_queryset.values("price_change_percent")[:1]),
        direction=Subquery(latest_event_queryset.values("direction")[:1]),
    ).order_by("-change_count", "-latest_changed_at", "vendor__name", "product__display_name")

    results = []
    for row in rows:
        results.append(
            {
                "product_id": row["product_id"],
                "product_name": row["product__display_name"],
                "vendor_id": row["vendor_id"],
                "vendor_name": row["vendor__name"],
                "change_count": row["change_count"],
                "latest_old_price": _quantize_money(row["latest_old_price"]),
                "latest_new_price": _quantize_money(row["latest_new_price"]),
                "latest_difference": _quantize_money(row["latest_difference"]),
                "latest_percent": (
                    None
                    if row["latest_percent"] is None
                    else _quantize_money(row["latest_percent"])
                ),
                "direction": row["direction"],
            }
        )

    return results


def get_products_with_price_trend(period=None, reference=None):
    queryset = Product.objects.filter(price_history__isnull=False)
    if period:
        period_range = _resolved_period_range(period, reference=reference)
        queryset = queryset.filter(
            price_history__date__gte=period_range["date_from"].date(),
            price_history__date__lte=period_range["date_to"].date(),
        )

    rows = queryset.distinct().order_by("display_name", "product_name", "id").values(
        "id",
        "display_name",
        "product_name",
    )

    return [
        {
            "product_id": row["id"],
            "product_name": row["display_name"] or row["product_name"],
        }
        for row in rows
    ]


def get_product_price_trend(product_id, period=None, reference=None):
    if not product_id:
        return {
            "product_id": None,
            "product_name": "",
            "labels": [],
            "datasets": [],
        }

    queryset = PriceHistory.objects.filter(
        product_id=product_id,
    ).select_related("product", "vendor").order_by("date", "id")
    if period:
        period_range = _resolved_period_range(period, reference=reference)
        queryset = queryset.filter(
            date__gte=period_range["date_from"].date(),
            date__lte=period_range["date_to"].date(),
        )

    history_entries = list(queryset)
    product = Product.objects.filter(pk=product_id).first()
    product_name = ""
    if history_entries:
        product_name = history_entries[0].product.display_name or history_entries[0].product.product_name
    elif product is not None:
        product_name = product.display_name or product.product_name

    if not history_entries:
        return {
            "product_id": product_id,
            "product_name": product_name,
            "labels": [],
            "datasets": [],
        }

    labels = [
        entry.date.strftime("%Y-%m-%d")
        for entry in history_entries
    ]
    vendor_order = []
    vendor_map = {}
    total_points = len(history_entries)

    for index, entry in enumerate(history_entries):
        vendor_id = entry.vendor_id
        if vendor_id not in vendor_map:
            vendor_map[vendor_id] = {
                "vendor_id": vendor_id,
                "vendor_name": entry.vendor.name,
                "prices": [None] * total_points,
                "old_prices": [None] * total_points,
                "new_prices": [None] * total_points,
            }
            vendor_order.append(vendor_id)

        effective_new_price = entry.effective_new_price
        vendor_map[vendor_id]["prices"][index] = _quantize_money(effective_new_price)
        vendor_map[vendor_id]["old_prices"][index] = (
            None if entry.previous_price is None else _quantize_money(entry.previous_price)
        )
        vendor_map[vendor_id]["new_prices"][index] = (
            None if effective_new_price is None else _quantize_money(effective_new_price)
        )

    return {
        "product_id": product_id,
        "product_name": product_name,
        "labels": labels,
        "datasets": [
            vendor_map[vendor_id]
            for vendor_id in vendor_order
        ],
    }


def get_vendor_price_change_movement_by_vendor(period, reference=None):
    accepted_quantity_subquery = FactPurchaseOrderItem.objects.filter(
        po_id=OuterRef("po_id"),
        product_id=OuterRef("product_id"),
    ).values("po_id", "product_id").annotate(
        total_accepted_quantity=Coalesce(Sum("accepted_quantity"), ZERO_QUANTITY)
    ).values("total_accepted_quantity")[:1]

    queryset = _filter_price_event_queryset_for_period(
        FactPriceChangeEvent.objects.filter(
            accepted_flag=True,
        ).select_related("vendor"),
        period,
        reference=reference,
    ).annotate(
        accepted_quantity=Coalesce(
            Subquery(accepted_quantity_subquery, output_field=QUANTITY_FIELD),
            ZERO_QUANTITY,
        )
    ).filter(
        accepted_quantity__gt=Decimal("0.00")
    ).annotate(
        signed_impact=ExpressionWrapper(
            F("new_price") - F("old_price"),
            output_field=MONEY_FIELD,
        )
    ).annotate(
        net_event_impact=ExpressionWrapper(
            F("signed_impact") * F("accepted_quantity"),
            output_field=MONEY_FIELD,
        ),
        movement_impact=Abs(
            ExpressionWrapper(
                F("signed_impact") * F("accepted_quantity"),
                output_field=MONEY_FIELD,
            )
        ),
    ).filter(
        ~Q(net_event_impact=Decimal("0.00")),
    )

    rows = list(
        queryset.values("vendor_id", "vendor__name").annotate(
            increase_count=Count("id", filter=Q(direction=PriceDirection.INCREASE)),
            decrease_count=Count("id", filter=Q(direction=PriceDirection.DECREASE)),
            net_impact=Coalesce(Sum("net_event_impact"), ZERO_MONEY),
            total_movement=Coalesce(Sum("movement_impact"), ZERO_MONEY),
        ).order_by("-total_movement", "-increase_count", "vendor__name")
    )
    total_movement_all = sum(
        (Decimal(str(row["total_movement"] or Decimal("0.00"))) for row in rows),
        Decimal("0.00"),
    )

    results = []
    for row in rows:
        vendor_total_movement = _quantize_money(row["total_movement"])
        vendor_net_impact = _quantize_money(row["net_impact"])
        if total_movement_all > Decimal("0.00"):
            movement_share_percent = _quantize_percent((vendor_total_movement / total_movement_all) * Decimal("100"))
        else:
            movement_share_percent = Decimal("0.00")

        results.append(
            {
                "vendor_id": row["vendor_id"],
                "vendor_name": row["vendor__name"],
                "increase_count": row["increase_count"],
                "decrease_count": row["decrease_count"],
                "net_impact": vendor_net_impact,
                "total_movement": vendor_total_movement,
                "movement_share_percent": movement_share_percent,
            }
        )

    return results


def get_vendor_price_increase_impact_by_vendor(period, reference=None):
    movement_rows = get_vendor_price_change_movement_by_vendor(period, reference=reference)
    return [
        {
            "vendor_id": row["vendor_id"],
            "vendor_name": row["vendor_name"],
            "total_impact": row["net_impact"],
            "increase_count": row["increase_count"],
            "impact_share_percent": row["movement_share_percent"],
            "decrease_count": row["decrease_count"],
            "total_movement": row["total_movement"],
            "net_impact": row["net_impact"],
            "movement_share_percent": row["movement_share_percent"],
        }
        for row in movement_rows
    ]


def get_vendor_price_increase_distribution(period, reference=None):
    return get_vendor_price_change_movement_by_vendor(period, reference=reference)


__all__ = [
    "get_total_spend_metrics",
    "get_average_po_value",
    "get_total_products_count",
    "get_total_po_count",
    "get_vendor_wise_spend",
    "get_vendor_order_frequency",
    "get_top_products",
    "get_frequent_price_changes",
    "get_products_with_price_trend",
    "get_product_price_trend",
    "get_vendor_price_change_movement_by_vendor",
    "get_vendor_price_increase_impact_by_vendor",
    "get_vendor_price_increase_distribution",
]
