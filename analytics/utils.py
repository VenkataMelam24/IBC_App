from datetime import datetime, time, timedelta

from django.utils import timezone


SUPPORTED_PERIODS = {
    "weekly",
    "monthly",
    "quarterly",
    "half_year",
    "yearly",
}


def _normalize_reference_datetime(reference=None):
    reference = reference or timezone.now()
    if timezone.is_naive(reference):
        reference = timezone.make_aware(reference, timezone.get_current_timezone())
    return timezone.localtime(reference)


def resolve_period_range(period, reference=None):
    if period not in SUPPORTED_PERIODS:
        raise ValueError(f"Unsupported period '{period}'. Expected one of: {', '.join(sorted(SUPPORTED_PERIODS))}.")

    reference_dt = _normalize_reference_datetime(reference)
    reference_date = reference_dt.date()

    if period == "weekly":
        start_date = reference_date - timedelta(days=reference_date.weekday())
    elif period == "monthly":
        start_date = reference_date.replace(day=1)
    elif period == "quarterly":
        start_month = ((reference_date.month - 1) // 3) * 3 + 1
        start_date = reference_date.replace(month=start_month, day=1)
    elif period == "half_year":
        start_month = 1 if reference_date.month <= 6 else 7
        start_date = reference_date.replace(month=start_month, day=1)
    else:
        start_date = reference_date.replace(month=1, day=1)

    date_from = timezone.make_aware(
        datetime.combine(start_date, time.min),
        timezone.get_current_timezone(),
    )

    return {
        "period": period,
        "date_from": date_from,
        "date_to": reference_dt,
    }


__all__ = ["SUPPORTED_PERIODS", "resolve_period_range"]
