from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import Vendor, Product, VendorProductPrice, PriceHistory


class AdminActionMixin:
    def edit_link(self, obj):
        app_label = obj._meta.app_label
        model_name = obj._meta.model_name
        url = reverse(f"admin:{app_label}_{model_name}_change", args=[obj.pk])
        return format_html('<a class="button" href="{}">Edit</a>', url)

    edit_link.short_description = "Edit"

    def delete_link(self, obj):
        app_label = obj._meta.app_label
        model_name = obj._meta.model_name
        url = reverse(f"admin:{app_label}_{model_name}_delete", args=[obj.pk])
        return format_html('<a class="button" style="color:red;" href="{}">Delete</a>', url)

    delete_link.short_description = "Delete"


@admin.register(Vendor)
class VendorAdmin(AdminActionMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "email",
        "whatsapp_number",
        "is_active",
        "created_at",
        "edit_link",
        "delete_link",
    )
    search_fields = ("name", "email", "whatsapp_number")
    list_filter = ("is_active",)
    ordering = ("name",)


@admin.register(Product)
class ProductAdmin(AdminActionMixin, admin.ModelAdmin):
    list_display = (
        "display_name",
        "product_name",
        "pack_type",
        "quantity_per_pack",
        "quantity_unit",
        "is_active",
        "created_at",
        "edit_link",
        "delete_link",
    )
    search_fields = ("display_name", "product_name")
    list_filter = ("pack_type", "is_active")
    ordering = ("product_name", "quantity_per_pack")


@admin.register(VendorProductPrice)
class VendorProductPriceAdmin(AdminActionMixin, admin.ModelAdmin):
    list_display = (
        "product",
        "vendor",
        "price",
        "currency",
        "is_active",
        "created_at",
        "edit_link",
        "delete_link",
    )
    search_fields = ("product__display_name", "vendor__name")
    list_filter = ("currency", "is_active", "vendor")
    ordering = ("product__product_name", "vendor__name")


@admin.register(PriceHistory)
class PriceHistoryAdmin(admin.ModelAdmin):
    list_display = ("date", "product", "vendor", "price")
    search_fields = ("product__display_name", "vendor__name")
    list_filter = ("vendor", "date")
    ordering = ("-date",)