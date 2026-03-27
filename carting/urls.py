from django.urls import path

from .views import (
    add_to_cart,
    cart_detail,
    decrease_cart_item,
    increase_cart_item,
    remove_cart_item,
)

app_name = "carting"

urlpatterns = [
    path("", cart_detail, name="cart_detail"),
    path("add/<int:product_id>/<int:vendor_id>/", add_to_cart, name="add_to_cart"),
    path("item/<int:item_id>/increase/", increase_cart_item, name="increase_cart_item"),
    path("item/<int:item_id>/decrease/", decrease_cart_item, name="decrease_cart_item"),
    path("item/<int:item_id>/remove/", remove_cart_item, name="remove_cart_item"),
]
