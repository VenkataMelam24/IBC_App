from pathlib import Path

from django import forms
from django.core.exceptions import ValidationError

from .models import PurchaseOrder


ALLOWED_INVOICE_FILE_TYPES = {
    ".pdf": {"application/pdf"},
    ".jpg": {"image/jpeg", "image/jpg"},
    ".jpeg": {"image/jpeg", "image/jpg"},
    ".png": {"image/png"},
}
INVOICE_FILE_HELP_TEXT = "Upload PDF or invoice image"
INVOICE_FILE_ERROR_MESSAGE = "Upload a PDF or invoice image (.pdf, .jpg, .jpeg, .png)."

MAX_INVOICE_FILE_SIZE_MB = 10
MAX_INVOICE_FILE_SIZE_BYTES = MAX_INVOICE_FILE_SIZE_MB * 1024 * 1024


class PurchaseOrderInvoiceForm(forms.ModelForm):
    def clean_invoice_file(self):
        invoice_file = self.cleaned_data.get("invoice_file")
        if invoice_file is None:
            return invoice_file

        # Check file extension.
        extension = Path(invoice_file.name or "").suffix.lower()
        allowed_content_types = ALLOWED_INVOICE_FILE_TYPES.get(extension)
        if allowed_content_types is None:
            raise ValidationError(INVOICE_FILE_ERROR_MESSAGE)

        # Check Content-Type header (best-effort; browsers set this).
        content_type = (getattr(invoice_file, "content_type", "") or "").lower()
        if content_type and content_type != "application/octet-stream" and content_type not in allowed_content_types:
            raise ValidationError(INVOICE_FILE_ERROR_MESSAGE)

        # Guard against excessively large uploads.
        if invoice_file.size > MAX_INVOICE_FILE_SIZE_BYTES:
            raise ValidationError(
                f"Invoice file is too large. Maximum allowed size is {MAX_INVOICE_FILE_SIZE_MB} MB."
            )

        return invoice_file

    class Meta:
        model = PurchaseOrder
        fields = ["invoice_file"]
        help_texts = {
            "invoice_file": INVOICE_FILE_HELP_TEXT,
        }
        widgets = {
            "invoice_file": forms.FileInput(
                attrs={
                    "accept": ".pdf,.jpg,.jpeg,.png",
                }
            ),
        }
