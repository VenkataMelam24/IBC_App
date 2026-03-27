from django.conf import settings
from django.db import models

from .constants import PriceChangeType, PriceDirection, QuantityChangeType


class FactPurchaseOrder(models.Model):
    po = models.OneToOneField(
        "purchases.PurchaseOrder",
        on_delete=models.CASCADE,
        related_name="analytics_fact",
    )
    po_number = models.CharField(max_length=64)
    vendor = models.ForeignKey(
        "inventory.Vendor",
        on_delete=models.CASCADE,
        related_name="analytics_purchase_orders",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="analytics_purchase_orders",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=30)

    original_po_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    final_net_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    final_tax_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    final_grand_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    invoice_net_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    invoice_tax_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    invoice_grand_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    po_item_count = models.PositiveIntegerField(default=0)
    was_auto_validated = models.BooleanField(default=False)
    was_manually_reconciled = models.BooleanField(default=False)
    has_name_reconciliation = models.BooleanField(default=False)
    has_quantity_reconciliation = models.BooleanField(default=False)
    has_price_reconciliation = models.BooleanField(default=False)

    total_value_variance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    quantity_variance_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    price_variance_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["created_at"], name="analytics_fpo_created_idx"),
            models.Index(fields=["closed_at"], name="analytics_fpo_closed_idx"),
            models.Index(fields=["vendor"], name="analytics_fpo_vendor_idx"),
            models.Index(fields=["status"], name="analytics_fpo_status_idx"),
        ]

    def __str__(self):
        return self.po_number


class FactPurchaseOrderItem(models.Model):
    po = models.ForeignKey(
        "purchases.PurchaseOrder",
        on_delete=models.CASCADE,
        related_name="analytics_fact_items",
    )
    po_number = models.CharField(max_length=64)
    vendor = models.ForeignKey(
        "inventory.Vendor",
        on_delete=models.CASCADE,
        related_name="analytics_purchase_order_items",
    )
    product = models.ForeignKey(
        "inventory.Product",
        on_delete=models.CASCADE,
        related_name="analytics_purchase_order_items",
    )
    product_display_name_snapshot = models.CharField(max_length=200)
    created_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)

    po_quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    po_unit_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    po_line_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    invoice_quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    invoice_unit_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    invoice_line_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    accepted_quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    accepted_unit_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    accepted_line_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    name_reconciled_flag = models.BooleanField(default=False)
    quantity_reconciled_flag = models.BooleanField(default=False)
    price_reconciled_flag = models.BooleanField(default=False)

    quantity_change_type = models.CharField(
        max_length=40,
        choices=QuantityChangeType.choices,
        default=QuantityChangeType.NONE,
    )
    price_change_type = models.CharField(
        max_length=30,
        choices=PriceChangeType.choices,
        default=PriceChangeType.NONE,
    )

    quantity_difference = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    unit_price_difference = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    line_total_difference = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["product"], name="analytics_fpoi_product_idx"),
            models.Index(fields=["vendor"], name="analytics_fpoi_vendor_idx"),
            models.Index(fields=["created_at"], name="analytics_fpoi_created_idx"),
        ]

    def __str__(self):
        return f"{self.po_number} - {self.product_display_name_snapshot}"


class FactPriceChangeEvent(models.Model):
    po = models.ForeignKey(
        "purchases.PurchaseOrder",
        on_delete=models.CASCADE,
        related_name="analytics_price_change_events",
    )
    po_number = models.CharField(max_length=64)
    vendor = models.ForeignKey(
        "inventory.Vendor",
        on_delete=models.CASCADE,
        related_name="analytics_price_change_events",
    )
    product = models.ForeignKey(
        "inventory.Product",
        on_delete=models.CASCADE,
        related_name="analytics_price_change_events",
    )
    changed_at = models.DateTimeField()
    old_price = models.DecimalField(max_digits=14, decimal_places=2)
    new_price = models.DecimalField(max_digits=14, decimal_places=2)
    price_difference = models.DecimalField(max_digits=14, decimal_places=2)
    price_change_percent = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    direction = models.CharField(max_length=10, choices=PriceDirection.choices)
    accepted_flag = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-changed_at", "-id"]
        indexes = [
            models.Index(fields=["product"], name="analytics_fpce_product_idx"),
            models.Index(fields=["vendor"], name="analytics_fpce_vendor_idx"),
            models.Index(fields=["changed_at"], name="analytics_fpce_changed_idx"),
        ]

    def __str__(self):
        return f"{self.product} @ {self.new_price}"

