from django.db.models import Sum

from .models import Cart, CartItem


def cart_item_count(request):
    if not request.user.is_authenticated:
        return {"cart_item_count": 0}

    cart = Cart.objects.filter(
        user=request.user,
        status=Cart.STATUS_OPEN,
    ).only("id").first()

    if cart is None:
        return {"cart_item_count": 0}

    total_quantity = (
        CartItem.objects.filter(cart=cart)
        .aggregate(total_quantity=Sum("quantity"))
        .get("total_quantity")
        or 0
    )

    return {"cart_item_count": total_quantity}
