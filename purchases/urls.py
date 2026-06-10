from django.urls import path

from .views import (
    clear_invoice,
    close_po_manually,
    close_po_manually_with_prices,
    confirm_price_update,
    create_po_from_cart_vendor,
    delete_po,
    download_po_pdf,
    history_list,
    manual_validate_po,
    po_list,
    upload_invoice,
    validate_invoice,
)

app_name = "purchases"

urlpatterns = [
    path("po/", po_list, name="po_list"),
    path(
        "po/create-from-cart/<int:vendor_id>/",
        create_po_from_cart_vendor,
        name="create_po_from_cart_vendor",
    ),
    path("po/<int:po_id>/download/", download_po_pdf, name="download_po_pdf"),
    path("po/<int:po_id>/upload-invoice/", upload_invoice, name="upload_invoice"),
    path("po/<int:po_id>/validate/", validate_invoice, name="validate_invoice"),
    path("po/<int:po_id>/manual-validate/", manual_validate_po, name="manual_validate_po"),
    path("po/<int:po_id>/clear-invoice/", clear_invoice, name="clear_invoice"),
    path("po/<int:po_id>/close-manually/", close_po_manually, name="close_po_manually"),
    path(
        "po/<int:po_id>/close-manually-with-prices/",
        close_po_manually_with_prices,
        name="close_po_manually_with_prices",
    ),
    path("po/<int:po_id>/confirm-prices/", confirm_price_update, name="confirm_price_update"),
    path("po/<int:po_id>/delete/", delete_po, name="delete_po"),
    path("history/", history_list, name="history_list"),
]
