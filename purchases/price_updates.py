from decimal import Decimal

from inventory.models import PriceHistory, VendorProductPrice

from .models import PurchaseOrder


def get_validation_price_mismatches(purchase_order):
    validation_data = purchase_order.validation_data or {}
    return validation_data, validation_data.get("price_mismatches") or []


def update_vendor_prices_from_validation(purchase_order, price_mismatches):
    po_items_by_id = {item.id: item for item in purchase_order.items.all()}
    updated_count = 0

    for mismatch in price_mismatches:
        po_item = po_items_by_id.get(mismatch.get("po_item_id"))
        updated_price_raw = mismatch.get("updated_price")

        if po_item is None or updated_price_raw in {None, ""}:
            continue

        updated_price = Decimal(str(updated_price_raw))
        vendor_price = VendorProductPrice.objects.select_for_update().filter(
            vendor=purchase_order.vendor,
            product=po_item.product,
        ).first()

        if vendor_price is None:
            vendor_price = VendorProductPrice(
                vendor=purchase_order.vendor,
                product=po_item.product,
                price=updated_price,
                currency="EUR",
                is_active=True,
            )
            vendor_price.save(change_source=PriceHistory.SOURCE_INVOICE_VALIDATION)
        else:
            vendor_price.price = updated_price
            vendor_price.is_active = True
            vendor_price.save(change_source=PriceHistory.SOURCE_INVOICE_VALIDATION)

        updated_count += 1

    return updated_count


def manual_close_note_for_purchase_order(purchase_order, include_price_update=False):
    if purchase_order.status == PurchaseOrder.STATUS_PRODUCT_MISMATCH:
        if include_price_update:
            return PurchaseOrder.NOTE_PRODUCT_MISMATCH_MANUAL_WITH_PRICE_UPDATE
        return PurchaseOrder.NOTE_PRODUCT_MISMATCH_MANUAL

    if purchase_order.status == PurchaseOrder.STATUS_PRICE_MISMATCH:
        if include_price_update:
            return "Financial differences reviewed, closed manually. Matched product prices updated from invoice."
        return "Financial differences reviewed, closed manually"

    if include_price_update:
        return PurchaseOrder.NOTE_QUANTITY_MISMATCH_MANUAL_WITH_PRICE_UPDATE
    return PurchaseOrder.NOTE_QUANTITY_MISMATCH_MANUAL
