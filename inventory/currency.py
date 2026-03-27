import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import ValidationError


DEFAULT_CURRENCY = "EUR"
DECIMAL_PRECISION = Decimal("0.01")

CURRENCY_CHOICES = [
    ("EUR", "EUR — Euro"),
    ("GBP", "GBP — British Pound"),
    ("CHF", "CHF — Swiss Franc"),
    ("PLN", "PLN — Polish Zloty"),
    ("CZK", "CZK — Czech Koruna"),
    ("SEK", "SEK — Swedish Krona"),
    ("NOK", "NOK — Norwegian Krone"),
    ("DKK", "DKK — Danish Krone"),
    ("RON", "RON — Romanian Leu"),
    ("HUF", "HUF — Hungarian Forint"),
    ("BGN", "BGN — Bulgarian Lev"),
    ("INR", "INR — Indian Rupee"),
    ("USD", "USD — US Dollar"),
]

COMMA_DECIMAL_CURRENCIES = {
    "EUR",
    "CHF",
    "PLN",
    "CZK",
    "SEK",
    "NOK",
    "DKK",
    "RON",
    "HUF",
    "BGN",
}

DOT_DECIMAL_CURRENCIES = {
    "GBP",
    "INR",
    "USD",
}


def get_currency_choices():
    return list(CURRENCY_CHOICES)


def currency_uses_comma_decimal(currency):
    return (currency or DEFAULT_CURRENCY) in COMMA_DECIMAL_CURRENCIES


def currency_uses_dot_decimal(currency):
    return not currency_uses_comma_decimal(currency)


def get_price_input_example(currency):
    return "50,25" if currency_uses_comma_decimal(currency) else "50.25"


def get_price_format_label(currency):
    return "comma" if currency_uses_comma_decimal(currency) else "dot"


def parse_localized_price_input(raw_value, currency):
    value = str(raw_value or "").strip()
    if not value:
        raise ValidationError("Enter a price.")

    uses_comma_decimal = currency_uses_comma_decimal(currency)
    expected_separator = "," if uses_comma_decimal else "."
    invalid_separator = "." if uses_comma_decimal else ","
    format_label = get_price_format_label(currency)
    example = get_price_input_example(currency)

    if "," in value and "." in value:
        raise ValidationError(
            f"Enter a valid price using {format_label} decimals, for example {example}."
        )

    if invalid_separator in value:
        raise ValidationError(
            f"Enter a valid price using {format_label} decimals, for example {example}."
        )

    pattern = r"^\d+(,\d{1,2})?$" if uses_comma_decimal else r"^\d+(\.\d{1,2})?$"
    if not re.fullmatch(pattern, value):
        raise ValidationError(
            f"Enter a valid price using {format_label} decimals, for example {example}."
        )

    normalized_value = value.replace(expected_separator, ".")
    try:
        return Decimal(normalized_value).quantize(DECIMAL_PRECISION, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValidationError(
            f"Enter a valid price using {format_label} decimals, for example {example}."
        )


def format_price_for_currency(value, currency):
    if value in {None, ""}:
        return ""

    amount = Decimal(str(value)).quantize(DECIMAL_PRECISION, rounding=ROUND_HALF_UP)
    formatted = f"{amount:.2f}"
    if currency_uses_comma_decimal(currency):
        return formatted.replace(".", ",")
    return formatted
