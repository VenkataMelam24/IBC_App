from django.utils import timezone

from analytics.services import sync_all_po_analytics

from .models import PurchaseOrder


def reset_purchase_order_invoice_state(purchase_order, delete_file=False):
    if delete_file and purchase_order.invoice_file:
        purchase_order.invoice_file.delete(save=False)

    purchase_order.invoice_file = None
    purchase_order.status = PurchaseOrder.STATUS_SENT
    purchase_order.validation_note = ""
    purchase_order.validation_data = {}
    purchase_order.validated_at = None
    purchase_order.closed_at = None


def apply_validation_result(purchase_order, validation_result):
    now = timezone.now()
    purchase_order.validation_data = validation_result
    purchase_order.validated_at = now
    purchase_order.closed_at = None

    has_product_mismatch = bool(validation_result.get("has_product_mismatch"))
    has_quantity_mismatch = bool(validation_result.get("has_quantity_mismatch"))
    has_price_mismatch = bool(validation_result.get("has_price_mismatch"))

    if not validation_result.get("has_any_mismatch"):
        purchase_order.status = PurchaseOrder.STATUS_CLOSED
        purchase_order.validation_note = PurchaseOrder.NOTE_VALIDATED_SUCCESSFULLY
        purchase_order.closed_at = now
    elif has_product_mismatch:
        purchase_order.status = PurchaseOrder.STATUS_PRODUCT_MISMATCH
        if has_quantity_mismatch and has_price_mismatch:
            purchase_order.validation_note = (
                "Product, quantity, and price differences detected. Manual reconciliation required."
            )
        elif has_quantity_mismatch:
            purchase_order.validation_note = (
                "Product and quantity mismatches detected. Manual reconciliation required."
            )
        elif has_price_mismatch:
            purchase_order.validation_note = (
                "Product mismatches detected. Price differences were also found. Manual reconciliation required."
            )
        else:
            purchase_order.validation_note = "Product mismatch detected. Manual reconciliation required."
    elif has_quantity_mismatch:
        purchase_order.status = PurchaseOrder.STATUS_QUANTITY_MISMATCH
        if has_price_mismatch:
            purchase_order.validation_note = (
                "Quantity mismatches detected. Price differences were also found. Manual reconciliation required."
            )
        else:
            purchase_order.validation_note = "Quantity mismatch detected. Manual reconciliation required."
    elif has_price_mismatch:
        purchase_order.status = PurchaseOrder.STATUS_PRICE_MISMATCH
        purchase_order.validation_note = (
            "Price mismatch detected. Review whether to update the stored price or keep the current price."
        )
    else:
        purchase_order.status = PurchaseOrder.STATUS_PRICE_MISMATCH
        purchase_order.validation_note = "Validation found differences that require manual review."

    purchase_order.save()
    if purchase_order.status == PurchaseOrder.STATUS_CLOSED:
        sync_all_po_analytics(purchase_order)
