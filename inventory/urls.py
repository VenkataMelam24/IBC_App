from django.urls import path

from .views import (
    add_product,
    add_vendor,
    add_vendor_price,
    delete_product,
    delete_vendor,
    delete_vendor_price,
    edit_product,
    edit_vendor,
    edit_vendor_price,
    import_products_workbook,
    manage_products,
    manage_vendor_prices,
    manage_vendors,
    master_inventory,
    price_tracker,
    product_list,
)

app_name = "inventory"

urlpatterns = [
    path("master-inventory/", master_inventory, name="master_inventory"),
    path("master-inventory/products/", manage_products, name="manage_products"),
    path("master-inventory/products/import/", import_products_workbook, name="import_products_workbook"),
    path("master-inventory/products/add/", add_product, name="add_product"),
    path("master-inventory/products/<int:pk>/edit/", edit_product, name="edit_product"),
    path("master-inventory/products/<int:pk>/delete/", delete_product, name="delete_product"),
    path("master-inventory/vendors/", manage_vendors, name="manage_vendors"),
    path("master-inventory/vendors/add/", add_vendor, name="add_vendor"),
    path("master-inventory/vendors/<int:pk>/edit/", edit_vendor, name="edit_vendor"),
    path("master-inventory/vendors/<int:pk>/delete/", delete_vendor, name="delete_vendor"),
    path("master-inventory/vendor-prices/", manage_vendor_prices, name="manage_vendor_prices"),
    path("master-inventory/vendor-prices/add/", add_vendor_price, name="add_vendor_price"),
    path("master-inventory/vendor-prices/<int:pk>/edit/", edit_vendor_price, name="edit_vendor_price"),
    path("master-inventory/vendor-prices/<int:pk>/delete/", delete_vendor_price, name="delete_vendor_price"),
    path("price-tracker/", price_tracker, name="price_tracker"),
    path("products/", product_list, name="product_list"),
]
