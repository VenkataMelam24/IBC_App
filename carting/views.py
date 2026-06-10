from collections import OrderedDict
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import Case, DecimalField, ExpressionWrapper, F, Sum, Value, When
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from inventory.models import Product, VendorProductPrice

from .models import Cart, CartItem

_MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)


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
    cart_item_id=None,
    product_id=None,
    vendor_id=None,
    quantity=0,
    unit_price=None,
    removed=False,
    message="",
):
    line_expr = ExpressionWrapper(F("unit_price") * F("quantity"), output_field=_MONEY_FIELD)
    agg_kwargs = {
        "cart_count": Sum("quantity"),
        "grand_total": Sum(line_expr),
    }
    if vendor_id is not None:
        agg_kwargs["vendor_subtotal"] = Sum(
            Case(
                When(vendor_id=vendor_id, then=line_expr),
                default=Value(Decimal("0.00")),
                output_field=_MONEY_FIELD,
            )
        )
    agg = cart.items.aggregate(**agg_kwargs)

    cart_count = agg["cart_count"] or 0
    grand_total = agg["grand_total"] or Decimal("0.00")
    vendor_subtotal = agg.get("vendor_subtotal") or Decimal("0.00")

    return {
        "success": True,
        "cart_item_id": cart_item_id,
        "product_id": product_id,
        "vendor_id": vendor_id,
        "quantity": quantity,
        "unit_price": _money(unit_price) if unit_price is not None else None,
        "line_total": _money(unit_price * quantity) if unit_price is not None else "0.00",
        "vendor_subtotal": _money(vendor_subtotal),
        "grand_total": _money(grand_total),
        "cart_count": cart_count,
        "removed": removed,
        "vendor_empty": vendor_id is not None and vendor_subtotal == 0,
        "cart_empty": cart_count == 0,
        "message": message,
    }


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
    # One query: VendorProductPrice joined with Vendor and Product
    vendor_price = get_object_or_404(
        VendorProductPrice.objects.select_related("vendor", "product"),
        product_id=product_id,
        vendor_id=vendor_id,
        is_active=True,
        product__is_active=True,
    )
    product = vendor_price.product

    cart = Cart.objects.filter(user=request.user, status=Cart.STATUS_OPEN).first()
    if cart is None:
        cart = Cart.objects.create(user=request.user, status=Cart.STATUS_OPEN)

    # Single-query upsert: avoids get_or_create's SAVEPOINT/RELEASE round trips
    now = timezone.now()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO carting_cartitem
                (cart_id, product_id, vendor_id, quantity, unit_price, created_at, updated_at)
            VALUES (%s, %s, %s, 1, %s, %s, %s)
            ON CONFLICT (cart_id, product_id, vendor_id)
            DO UPDATE SET
                quantity   = carting_cartitem.quantity + 1,
                updated_at = EXCLUDED.updated_at
            RETURNING id, quantity
            """,
            [cart.id, product.id, vendor_price.vendor_id, vendor_price.price, now, now],
        )
        item_id, item_quantity = cursor.fetchone()

    success_message = f"{product.display_name} added to cart"
    if _wants_json_response(request):
        return JsonResponse(
            _build_cart_response_payload(
                cart,
                cart_item_id=item_id,
                product_id=product.id,
                vendor_id=vendor_price.vendor_id,
                quantity=item_quantity,
                unit_price=vendor_price.price,
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
                cart_item_id=cart_item.id,
                product_id=cart_item.product_id,
                vendor_id=cart_item.vendor_id,
                quantity=cart_item.quantity,
                unit_price=cart_item.unit_price,
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
    unit_price = cart_item.unit_price

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
                cart_item_id=cart_item_id,
                product_id=product_id,
                vendor_id=vendor_id,
                quantity=0 if removed else cart_item.quantity,
                unit_price=None if removed else unit_price,
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
