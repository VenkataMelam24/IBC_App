from .models import Cart


def get_open_cart_quantities(user):
    if not getattr(user, "is_authenticated", False):
        return {}

    open_cart = (
        Cart.objects.filter(user=user, status=Cart.STATUS_OPEN)
        .prefetch_related("items")
        .first()
    )
    if open_cart is None:
        return {}

    return {
        (cart_item.product_id, cart_item.vendor_id): cart_item.quantity
        for cart_item in open_cart.items.all()
    }
