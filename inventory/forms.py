from django import forms
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

import zipfile

from .currency import (
    DEFAULT_CURRENCY,
    format_price_for_currency,
    get_currency_choices,
    get_price_input_example,
    parse_localized_price_input,
)
from .models import Product, Vendor, VendorProductPrice


class ProductBulkImportForm(forms.Form):
    workbook = forms.FileField(label="Excel File")

    def clean_workbook(self):
        workbook = self.cleaned_data["workbook"]
        filename = (getattr(workbook, "name", "") or "").strip()

        if not filename.lower().endswith(".xlsx"):
            raise forms.ValidationError("Upload an .xlsx Excel file.")

        try:
            if hasattr(workbook, "seek"):
                workbook.seek(0)
            parsed_workbook = load_workbook(
                filename=workbook,
                read_only=True,
                data_only=True,
            )
        except (InvalidFileException, OSError, ValueError, zipfile.BadZipFile):
            raise forms.ValidationError("Upload a valid .xlsx workbook.")
        finally:
            if "parsed_workbook" in locals():
                parsed_workbook.close()
            if hasattr(workbook, "seek"):
                workbook.seek(0)

        return workbook


class ProductForm(forms.ModelForm):
    custom_pack_type = forms.CharField(required=False, label="New Pack Type")

    class Meta:
        model = Product
        fields = [
            "product_name",
            "pack_type",
            "quantity_per_pack",
            "quantity_unit",
            "display_name",
            "image",
            "is_active",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["custom_pack_type"].required = False

        pack_type_field = Product._meta.get_field("pack_type")
        self.fields["custom_pack_type"].max_length = pack_type_field.max_length
        self.fields["pack_type"].choices = self._build_pack_type_choices()
        self.fields["pack_type"].widget.attrs["id"] = "id_pack_type"
        self.fields["custom_pack_type"].widget.attrs["id"] = "id_custom_pack_type"

        if self.instance.pk and self._is_custom_pack_type(self.instance.pack_type):
            self.fields["custom_pack_type"].initial = self.instance.pack_type

        self.order_fields(
            [
                "product_name",
                "pack_type",
                "custom_pack_type",
                "quantity_per_pack",
                "quantity_unit",
                "display_name",
                "image",
                "is_active",
            ]
        )

    @staticmethod
    def _normalize_whitespace(value):
        return " ".join((value or "").split()).strip()

    @classmethod
    def _default_pack_type_choices(cls):
        return list(Product.PACK_TYPE_CHOICES)

    @classmethod
    def _default_pack_type_values(cls):
        return {value for value, _label in Product.PACK_TYPE_CHOICES}

    @classmethod
    def _default_pack_type_map(cls):
        mapping = {}
        for value, label in Product.PACK_TYPE_CHOICES:
            mapping[value.lower()] = value
            mapping[label.lower()] = value
        return mapping

    @classmethod
    def _existing_custom_pack_types(cls):
        custom_values = []
        seen = set()
        default_values = cls._default_pack_type_values()

        for pack_type in Product.objects.order_by("pack_type").values_list("pack_type", flat=True).distinct():
            normalized = cls._normalize_whitespace(pack_type)
            if not normalized or normalized.lower() in default_values:
                continue

            key = normalized.lower()
            if key in seen:
                continue

            seen.add(key)
            custom_values.append(normalized)

        return custom_values

    @classmethod
    def _is_custom_pack_type(cls, value):
        normalized = cls._normalize_whitespace(value)
        if not normalized:
            return False
        return normalized.lower() not in cls._default_pack_type_values()

    def _build_pack_type_choices(self):
        default_choices = self._default_pack_type_choices()
        standard_choices = [(value, label) for value, label in default_choices if value != "other"]
        custom_choices = [(value, value) for value in self._existing_custom_pack_types()]
        other_choice = next((choice for choice in default_choices if choice[0] == "other"), ("other", "Other"))
        return standard_choices + custom_choices + [other_choice]

    def clean_custom_pack_type(self):
        custom_pack_type = self._normalize_whitespace(self.cleaned_data.get("custom_pack_type"))
        if not custom_pack_type:
            return ""

        max_length = Product._meta.get_field("pack_type").max_length
        if len(custom_pack_type) > max_length:
            raise forms.ValidationError(f"New Pack Type must be {max_length} characters or fewer.")

        default_map = self._default_pack_type_map()
        if custom_pack_type.lower() in default_map:
            return default_map[custom_pack_type.lower()]

        for existing_custom_value in self._existing_custom_pack_types():
            if existing_custom_value.lower() == custom_pack_type.lower():
                return existing_custom_value

        return custom_pack_type

    def clean(self):
        cleaned_data = super().clean()
        selected_pack_type = self._normalize_whitespace(cleaned_data.get("pack_type"))
        custom_pack_type = cleaned_data.get("custom_pack_type", "")

        if selected_pack_type == "other":
            if not custom_pack_type:
                self.add_error("custom_pack_type", "Enter a new pack type when selecting Other.")
            else:
                cleaned_data["pack_type"] = custom_pack_type
                current_choices = list(self.fields["pack_type"].choices)
                if custom_pack_type not in {value for value, _label in current_choices}:
                    other_choice = next(
                        (choice for choice in current_choices if choice[0] == "other"),
                        ("other", "Other"),
                    )
                    updated_choices = [choice for choice in current_choices if choice[0] != "other"]
                    updated_choices.append((custom_pack_type, custom_pack_type))
                    updated_choices.append(other_choice)
                    self.fields["pack_type"].choices = updated_choices

        return cleaned_data

    def _post_clean(self):
        pack_type_field = self.instance._meta.get_field("pack_type")
        original_choices = pack_type_field.choices
        pack_type_field.choices = self.fields["pack_type"].choices
        try:
            super()._post_clean()
        finally:
            pack_type_field.choices = original_choices


class VendorForm(forms.ModelForm):
    class Meta:
        model = Vendor
        fields = [
            "name",
            "email",
            "whatsapp_number",
            "address",
            "is_active",
        ]


class VendorProductPriceForm(forms.ModelForm):
    price = forms.CharField(widget=forms.TextInput(attrs={"inputmode": "decimal"}))
    currency = forms.ChoiceField(choices=get_currency_choices())

    class Meta:
        model = VendorProductPrice
        fields = [
            "product",
            "vendor",
            "price",
            "currency",
            "is_active",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        selected_currency = self.initial.get("currency") or getattr(
            self.instance,
            "currency",
            DEFAULT_CURRENCY,
        )

        self.fields["currency"].choices = get_currency_choices()
        self.fields["price"].help_text = (
            "Enter the price using the decimal style for the selected currency."
        )
        self.fields["price"].widget.attrs["placeholder"] = get_price_input_example(selected_currency)

        if self.instance.pk:
            self.initial["price"] = format_price_for_currency(
                self.instance.price,
                self.instance.currency,
            )

    def clean(self):
        cleaned_data = super().clean()
        price_value = cleaned_data.get("price")
        currency = cleaned_data.get("currency") or self.data.get("currency") or DEFAULT_CURRENCY

        if price_value in {None, ""}:
            return cleaned_data

        try:
            cleaned_data["price"] = parse_localized_price_input(price_value, currency)
        except forms.ValidationError as exc:
            self.add_error("price", exc)

        return cleaned_data
