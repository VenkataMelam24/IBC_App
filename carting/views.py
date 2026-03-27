from collections import OrderedDict
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inventory.models import Product, VendorProductPrice

from .models import Cart, CartItem


def _money(value):
    return f"{Decimal(value):.2f}"


def _wants_json_response(request):
    accept_header = (request.headers.get("Accept") or "").lower()
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in accept_header
    )


def _build_cart_response_payload(
    cart,
    *,
    cart_item=None,
    cart_item_id=None,
    product_id=None,
    vendor_id=None,
    removed=False,
    message="",
):
    cart_items = list(cart.items.select_related("vendor"))
    cart_count = 0
    grand_total = Decimal("0.00")
    vendor_subtotal = Decimal("0.00")
    vendor_has_items = False

    for existing_item in cart_items:
        line_total = existing_item.unit_price * existing_item.quantity
        cart_count += existing_item.quantity
        grand_total += line_total

        if vendor_id is not None and existing_item.vendor_id == vendor_id:
            vendor_subtotal += line_total
            vendor_has_items = True

    if cart_item is not None and not removed:
        quantity = cart_item.quantity
        unit_price = _money(cart_item.unit_price)
        line_total = _money(cart_item.unit_price * cart_item.quantity)
        cart_item_id = cart_item.id
        product_id = cart_item.product_id
    else:
        quantity = 0
        unit_price = None
        line_total = "0.00"

    payload = {
        "success": True,
        "cart_item_id": cart_item_id,
        "product_id": product_id,
        "vendor_id": vendor_id,
        "quantity": quantity,
        "unit_price": unit_price,
        "line_total": line_total,
        "vendor_subtotal": _money(vendor_subtotal),
        "grand_total": _money(grand_total),
        "cart_count": cart_count,
        "removed": removed,
        "vendor_empty": vendor_id is not None and not vendor_has_items,
        "cart_empty": not cart_items,
        "message": message,
    }
    return payload


@login_required
def cart_detail(request):
    cart, _ = Cart.objects.get_or_create(
        user=request.user,
        status=Cart.STATUS_OPEN,
    )

    cart_items = (
        cart.items.select_related("product", "vendor")
        .order_by("vendor__name", "product__display_name")
    )

    grouped_items = OrderedDict()
    grand_total = Decimal("0.00")

    for item in cart_items:
        item.line_total = item.unit_price * item.quantity
        grand_total += item.line_total

        vendor_group = grouped_items.setdefault(
            item.vendor_id,
            {
                "vendor": item.vendor,
                "items": [],
                "subtotal": Decimal("0.00"),
            },
        )
        vendor_group["items"].append(item)
        vendor_group["subtotal"] += item.line_total

    vendor_groups = list(grouped_items.values())

    return render(
        request,
        "carting/cart_detail.html",
        {
            "cart": cart,
            "vendor_groups": vendor_groups,
            "grand_total": grand_total,
        },
    )


@login_required
@require_POST
def add_to_cart(request, product_id, vendor_id):
    product = get_object_or_404(Product, pk=product_id, is_active=True)
    vendor_price = get_object_or_404(
        VendorProductPrice.objects.select_related("vendor"),
        product=product,
        vendor_id=vendor_id,
        is_active=True,
    )

    cart = Cart.objects.filter(
        user=request.user,
        status=Cart.STATUS_OPEN,
    ).first()

    if cart is None:
        cart = Cart.objects.create(user=request.user, status=Cart.STATUS_OPEN)

    cart_item, created = CartItem.objects.get_or_create(
        cart=cart,
        product=product,
        vendor=vendor_price.vendor,
        defaults={
            "quantity": 1,
            "unit_price": vendor_price.price,
        },
    )

    if not created:
        cart_item.quantity += 1
        cart_item.save(update_fields=["quantity", "updated_at"])

    success_message = f"{product.display_name} added to cart"
    if _wants_json_response(request):
        return JsonResponse(
            _build_cart_response_payload(
                cart,
                cart_item=cart_item,
                vendor_id=vendor_price.vendor_id,
                message=success_message,
            )
        )

    messages.success(request, success_message)

    return redirect("inventory:product_list")


@login_required
@require_POST
def increase_cart_item(request, item_id):
    cart_item = get_object_or_404(
        CartItem,
        pk=item_id,
        cart__user=request.user,
        cart__status=Cart.STATUS_OPEN,
    )
    cart_item.quantity += 1
    cart_item.save(update_fields=["quantity", "updated_at"])

    if _wants_json_response(request):
        return JsonResponse(
            _build_cart_response_payload(
                cart_item.cart,
                cart_item=cart_item,
                vendor_id=cart_item.vendor_id,
                message="Cart item quantity updated.",
            )
        )

    return redirect("carting:cart_detail")


@login_required
@require_POST
def decrease_cart_item(request, item_id):
    cart_item = get_object_or_404(
        CartItem,
        pk=item_id,
        cart__user=request.user,
        cart__status=Cart.STATUS_OPEN,
    )

    removed = False
    cart = cart_item.cart
    cart_item_id = cart_item.id
    product_id = cart_item.product_id
    vendor_id = cart_item.vendor_id

    if cart_item.quantity > 1:
        cart_item.quantity -= 1
        cart_item.save(update_fields=["quantity", "updated_at"])
    else:
        cart_item.delete()
        removed = True

    if _wants_json_response(request):
        return JsonResponse(
            _build_cart_response_payload(
                cart,
                cart_item=None if removed else cart_item,
                cart_item_id=cart_item_id,
                product_id=product_id,
                vendor_id=vendor_id,
                removed=removed,
                message="Cart item quantity updated.",
            )
        )

    return redirect("carting:cart_detail")


@login_required
@require_POST
def remove_cart_item(request, item_id):
    cart_item = get_object_or_404(
        CartItem,
        pk=item_id,
        cart__user=request.user,
        cart__status=Cart.STATUS_OPEN,
    )
    cart = cart_item.cart
    cart_item_id = cart_item.id
    product_id = cart_item.product_id
    vendor_id = cart_item.vendor_id
    cart_item.delete()

    if _wants_json_response(request):
        return JsonResponse(
            _build_cart_response_payload(
                cart,
                cart_item_id=cart_item_id,
                product_id=product_id,
                vendor_id=vendor_id,
                removed=True,
                message="Cart item removed.",
            )
        )

    return redirect("carting:cart_detail")
