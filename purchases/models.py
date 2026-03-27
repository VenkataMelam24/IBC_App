import re

from django.conf import settings
from django.db import models
from django.db.models import Sum
from django.utils.text import slugify
from django.utils import timezone


def invoice_upload_path(instance, filename):
    po_number = instance.po_number or "pending"
    return f"invoices/{po_number}/{filename}"


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class PurchaseOrder(TimeStampedModel):
    PO_NUMBER_VENDOR_PART_MAX_LENGTH = 40

    STATUS_SENT = "sent"
    STATUS_INVOICE_UPLOADED = "invoice_uploaded"
    STATUS_PRODUCT_MISMATCH = "product_mismatch"
    STATUS_QUANTITY_MISMATCH = "quantity_mismatch"
    STATUS_PRICE_MISMATCH = "price_mismatch"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_SENT, "Sent"),
        (STATUS_INVOICE_UPLOADED, "Invoice Uploaded"),
        (STATUS_PRODUCT_MISMATCH, "Product Mismatch"),
        (STATUS_QUANTITY_MISMATCH, "Quantity Mismatch"),
        (STATUS_PRICE_MISMATCH, "Price Review Required"),
        (STATUS_CLOSED, "Closed"),
    ]

    NOTE_VALIDATED_SUCCESSFULLY = "Validated successfully"
    NOTE_PRODUCT_MISMATCH_MANUAL = "Product mismatched, closed manually"
    NOTE_QUANTITY_MISMATCH_MANUAL = "Quantity mismatched, closed manually"
    NOTE_PRODUCT_MISMATCH_MANUAL_WITH_PRICE_UPDATE = (
        "Product mismatched, closed manually. Matched product prices updated from invoice."
    )
    NOTE_QUANTITY_MISMATCH_MANUAL_WITH_PRICE_UPDATE = (
        "Quantity mismatched, closed manually. Matched product prices updated from invoice."
    )
    NOTE_PRICE_UPDATED_AND_CLOSED = "Price updated from invoice and PO closed"

    vendor = models.ForeignKey(
        "inventory.Vendor",
        on_delete=models.CASCADE,
        related_name="purchase_orders",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="purchase_orders",
    )
    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_SENT,
    )
    po_number = models.CharField(max_length=64, unique=True, blank=True)
    invoice_file = models.FileField(
        upload_to=invoice_upload_path,
        blank=True,
        null=True,
    )
    validation_note = models.TextField(blank=True)
    validation_data = models.JSONField(default=dict, blank=True)
    validated_at = models.DateTimeField(blank=True, null=True)
    closed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.po_number or f"PO {self.pk}"

    def save(self, *args, **kwargs):
        if not self.po_number:
            self.po_number = self.generate_po_number()
        super().save(*args, **kwargs)

    @classmethod
    def normalize_vendor_identifier(cls, vendor_name):
        normalized = slugify(vendor_name or "").upper().strip("-")
        normalized = re.sub(r"-{2,}", "-", normalized)
        return normalized or "VENDOR"

    @classmethod
    def build_po_number(cls, po_date, vendor_identifier, sequence):
        vendor_part = vendor_identifier[: cls.PO_NUMBER_VENDOR_PART_MAX_LENGTH]
        date_part = po_date.strftime("%Y%m%d")
        return f"PO-{date_part}-{vendor_part}-{sequence:02d}"

    def generate_po_number(self):
        po_date = timezone.localdate(self.created_at) if self.created_at else timezone.localdate()
        vendor_name = self.vendor.name if self.vendor_id else ""
        vendor_identifier = self.normalize_vendor_identifier(vendor_name)
        prefix = f"PO-{po_date:%Y%m%d}-{vendor_identifier[: self.PO_NUMBER_VENDOR_PART_MAX_LENGTH]}-"
        sequence_pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")

        next_sequence = 1
        existing_po_numbers = type(self).objects.filter(
            po_number__startswith=prefix
        ).values_list("po_number", flat=True)

        for existing_po_number in existing_po_numbers:
            match = sequence_pattern.match(existing_po_number)
            if match:
                next_sequence = max(next_sequence, int(match.group(1)) + 1)

        po_number = self.build_po_number(po_date, vendor_identifier, next_sequence)
        while type(self).objects.filter(po_number=po_number).exists():
            next_sequence += 1
            po_number = self.build_po_number(po_date, vendor_identifier, next_sequence)

        return po_number

    @property
    def is_closed(self):
        return self.status == self.STATUS_CLOSED

    @property
    def needs_manual_close(self):
        return self.status in {
            self.STATUS_PRODUCT_MISMATCH,
            self.STATUS_QUANTITY_MISMATCH,
        }

    @property
    def needs_price_confirmation(self):
        return self.status == self.STATUS_PRICE_MISMATCH

    @property
    def has_manual_mismatch_note(self):
        note = (self.validation_note or "").strip()
        return any(
            note.startswith(prefix)
            for prefix in {
                self.NOTE_PRODUCT_MISMATCH_MANUAL,
                self.NOTE_QUANTITY_MISMATCH_MANUAL,
            }
        )

    @property
    def total_amount(self):
        return self.items.aggregate(total=Sum("line_total")).get("total") or 0


class PurchaseOrderItem(TimeStampedModel):
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="items",
    )
    product = models.ForeignKey(
        "inventory.Product",
        on_delete=models.CASCADE,
        related_name="purchase_order_items",
    )
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.purchase_order.po_number} - {self.product.display_name}"
