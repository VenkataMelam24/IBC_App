from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from inventory.models import Product, Vendor, VendorProductPrice

from .models import Cart, CartItem


class CartAjaxTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="cart-user",
            password="testpass123",
        )
        cls.vendor = Vendor.objects.create(
            name="Krishna Supplies",
            email="krishna@example.com",
            whatsapp_number="1234567890",
        )
        cls.product = Product.objects.create(
            product_name="Rice bag",
            pack_type="bag",
            quantity_per_pack="20",
            quantity_unit="Kg",
            display_name="Rice bag 20 Kg",
            is_active=True,
        )
        cls.vendor_price = VendorProductPrice.objects.create(
            vendor=cls.vendor,
            product=cls.product,
            price=Decimal("10.50"),
            currency="EUR",
            is_active=True,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _ajax_headers(self):
        return {
            "HTTP_X_REQUESTED_WITH": "XMLHttpRequest",
            "HTTP_ACCEPT": "application/json",
        }

    def _open_cart(self):
        return Cart.objects.get(user=self.user, status=Cart.STATUS_OPEN)

    def test_ajax_add_to_cart_returns_correct_json(self):
        response = self.client.post(
            reverse("carting:add_to_cart", args=[self.product.id, self.vendor.id]),
            **self._ajax_headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")

        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["product_id"], self.product.id)
        self.assertEqual(payload["vendor_id"], self.vendor.id)
        self.assertEqual(payload["quantity"], 1)
        self.assertEqual(payload["cart_count"], 1)
        self.assertEqual(payload["line_total"], "10.50")
        self.assertEqual(payload["vendor_subtotal"], "10.50")
        self.assertEqual(payload["grand_total"], "10.50")
        self.assertIn("added to cart", payload["message"])

    def test_ajax_quantity_update_returns_correct_json(self):
        cart = Cart.objects.create(user=self.user, status=Cart.STATUS_OPEN)
        cart_item = CartItem.objects.create(
            cart=cart,
            product=self.product,
            vendor=self.vendor,
            quantity=2,
            unit_price=Decimal("10.50"),
        )

        response = self.client.post(
            reverse("carting:increase_cart_item", args=[cart_item.id]),
            **self._ajax_headers(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        cart_item.refresh_from_db()

        self.assertEqual(payload["cart_item_id"], cart_item.id)
        self.assertEqual(payload["quantity"], 3)
        self.assertEqual(payload["cart_count"], 3)
        self.assertEqual(payload["line_total"], "31.50")
        self.assertEqual(payload["vendor_subtotal"], "31.50")
        self.assertEqual(payload["grand_total"], "31.50")
        self.assertEqual(cart_item.quantity, 3)

    def test_ajax_remove_item_returns_correct_json(self):
        cart = Cart.objects.create(user=self.user, status=Cart.STATUS_OPEN)
        cart_item = CartItem.objects.create(
            cart=cart,
            product=self.product,
            vendor=self.vendor,
            quantity=1,
            unit_price=Decimal("10.50"),
        )

        response = self.client.post(
            reverse("carting:remove_cart_item", args=[cart_item.id]),
            **self._ajax_headers(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertTrue(payload["removed"])
        self.assertEqual(payload["cart_item_id"], cart_item.id)
        self.assertEqual(payload["cart_count"], 0)
        self.assertEqual(payload["grand_total"], "0.00")
        self.assertTrue(payload["vendor_empty"])
        self.assertTrue(payload["cart_empty"])
        self.assertFalse(CartItem.objects.filter(pk=cart_item.id).exists())

    def test_ajax_cart_totals_reflect_multiple_items(self):
        cart = Cart.objects.create(user=self.user, status=Cart.STATUS_OPEN)
        second_product = Product.objects.create(
            product_name="Beans",
            pack_type="bag",
            quantity_per_pack="10",
            quantity_unit="Kg",
            display_name="Beans 10 Kg",
            is_active=True,
        )
        VendorProductPrice.objects.create(
            vendor=self.vendor,
            product=second_product,
            price=Decimal("7.25"),
            currency="EUR",
            is_active=True,
        )
        cart_item = CartItem.objects.create(
            cart=cart,
            product=self.product,
            vendor=self.vendor,
            quantity=1,
            unit_price=Decimal("10.50"),
        )
        CartItem.objects.create(
            cart=cart,
            product=second_product,
            vendor=self.vendor,
            quantity=2,
            unit_price=Decimal("7.25"),
        )

        response = self.client.post(
            reverse("carting:increase_cart_item", args=[cart_item.id]),
            **self._ajax_headers(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["cart_count"], 4)
        self.assertEqual(payload["line_total"], "21.00")
        self.assertEqual(payload["vendor_subtotal"], "35.50")
        self.assertEqual(payload["grand_total"], "35.50")

    def test_non_ajax_add_to_cart_fallback_still_redirects(self):
        response = self.client.post(
            reverse("carting:add_to_cart", args=[self.product.id, self.vendor.id]),
        )

        self.assertRedirects(response, reverse("inventory:product_list"))
        cart = self._open_cart()
        self.assertEqual(cart.items.get().quantity, 1)
