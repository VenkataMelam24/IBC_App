from django.contrib import admin

from .models import FactPriceChangeEvent, FactPurchaseOrder, FactPurchaseOrderItem


@admin.register(FactPurchaseOrder)
class FactPurchaseOrderAdmin(admin.ModelAdmin):
    list_display = (
        "po_number",
        "vendor",
        "status",
        "final_net_total",
        "final_tax_total",
        "final_grand_total",
        "last_synced_at",
    )
    list_filter = ("status", "vendor", "was_auto_validated", "was_manually_reconciled")
    search_fields = ("po_number", "vendor__name")


@admin.register(FactPurchaseOrderItem)
class FactPurchaseOrderItemAdmin(admin.ModelAdmin):
    list_display = (
        "po_number",
        "vendor",
        "product_display_name_snapshot",
        "accepted_quantity",
        "accepted_unit_price",
        "accepted_line_total",
        "last_synced_at",
    )
    list_filter = ("vendor", "name_reconciled_flag", "quantity_reconciled_flag", "price_reconciled_flag")
    search_fields = ("po_number", "product_display_name_snapshot", "vendor__name")


@admin.register(FactPriceChangeEvent)
class FactPriceChangeEventAdmin(admin.ModelAdmin):
    list_display = (
        "po_number",
        "vendor",
        "product",
        "old_price",
        "new_price",
        "direction",
        "changed_at",
        "last_synced_at",
    )
    list_filter = ("vendor", "direction", "accepted_flag")
    search_fields = ("po_number", "product__display_name", "vendor__name")

