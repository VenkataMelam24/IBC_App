import re
import shutil
import tempfile
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone

from carting.models import Cart, CartItem
from inventory.models import Product, Vendor, VendorProductPrice
from inventory.product_matching import normalize_product_name

from .models import PurchaseOrder, PurchaseOrderItem
from .services import (
    _extract_invoice_totals,
    analyze_purchase_order_invoice,
    classify_invoice_against_purchase_order,
)


class PurchaseOrderValidationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="purchases-admin",
            password="testpass123",
        )
        cls.vendor = Vendor.objects.create(
            name="Krishna Supplies",
            email="krishna@example.com",
            whatsapp_number="1234567890",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _create_product(
        self,
        *,
        product_name="Rice bag",
        display_name="Rice bag 20 Kg",
        quantity_per_pack="20",
        quantity_unit="Kg",
    ):
        return Product.objects.create(
            product_name=product_name,
            pack_type="bag",
            quantity_per_pack=quantity_per_pack,
            quantity_unit=quantity_unit,
            display_name=display_name,
            is_active=True,
        )

    def _create_purchase_order(self, product, *, quantity=2, unit_price="10.50"):
        purchase_order = PurchaseOrder.objects.create(
            vendor=self.vendor,
            created_by=self.user,
        )
        PurchaseOrderItem.objects.create(
            purchase_order=purchase_order,
            product=product,
            quantity=quantity,
            unit_price=Decimal(unit_price),
            line_total=Decimal(unit_price) * quantity,
        )
        return purchase_order

    def _invoice_item(self, *, name, quantity, unit_price, amount=None, line_total_source=None):
        item = {
            "name": name,
            "normalized_name": normalize_product_name(name),
            "quantity": str(quantity),
            "unit_price": str(unit_price),
        }
        if amount is not None:
            item["amount"] = str(amount)
            item["line_total_source"] = line_total_source or "invoice"
        elif line_total_source is not None:
            item["line_total_source"] = line_total_source
        return item

    def _set_validation_state(self, purchase_order, validation_result):
        if validation_result.get("has_product_mismatch"):
            purchase_order.status = PurchaseOrder.STATUS_PRODUCT_MISMATCH
            purchase_order.validation_note = "Product mismatch detected. Manual reconciliation required."
        elif validation_result.get("has_quantity_mismatch"):
            purchase_order.status = PurchaseOrder.STATUS_QUANTITY_MISMATCH
            purchase_order.validation_note = "Quantity mismatch detected. Manual reconciliation required."
        elif validation_result.get("has_price_mismatch"):
            purchase_order.status = PurchaseOrder.STATUS_PRICE_MISMATCH
            purchase_order.validation_note = (
                "Price mismatch detected. Review whether to update the stored price or keep the current price."
            )
        else:
            purchase_order.status = PurchaseOrder.STATUS_INVOICE_UPLOADED
            purchase_order.validation_note = "Invoice uploaded. Click Validate to compare against the PO."

        purchase_order.validation_data = validation_result
        purchase_order.save()


class InvoiceAliasMatchingTests(PurchaseOrderValidationTestCase):
    def test_invoice_item_matches_purchase_order_product_by_alias(self):
        product = self._create_product(display_name="Shrimp 24 G", quantity_per_pack="24", quantity_unit="G")
        product.aliases.create(alias_name="Shrimp24G")
        purchase_order = self._create_purchase_order(product)
        po_item = purchase_order.items.get()

        result = classify_invoice_against_purchase_order(
            purchase_order,
            [
                {
                    "name": "Shrimp24G",
                    "normalized_name": "shrimp24g",
                    "quantity": "2",
                    "unit_price": "10.50",
                }
            ],
        )

        self.assertEqual(result["classification"], "matched")
        self.assertFalse(result["has_product_mismatch"])
        self.assertEqual(result["missing_products"], [])
        self.assertEqual(result["extra_products"], [])
        self.assertEqual(
            result["matched_items"],
            [
                {
                    "invoice_item_index": 0,
                    "po_item_id": po_item.id,
                    "product_id": po_item.product_id,
                    "product_name": "Shrimp 24 G",
                    "quantity": "2",
                    "unit_price": "10.50",
                    "line_total": "21.00",
                }
            ],
        )

    def test_unmatched_invoice_item_still_remains_for_manual_review(self):
        product = self._create_product(display_name="Shrimp 24 G", quantity_per_pack="24", quantity_unit="G")
        purchase_order = self._create_purchase_order(product)

        result = classify_invoice_against_purchase_order(
            purchase_order,
            [
                {
                    "name": "Prawn 24 G",
                    "normalized_name": "prawn 24 g",
                    "quantity": "2",
                    "unit_price": "10.50",
                }
            ],
        )

        self.assertEqual(result["classification"], "product_mismatch")
        self.assertTrue(result["has_product_mismatch"])
        self.assertEqual(len(result["missing_products"]), 1)
        self.assertEqual(
            result["extra_products"],
            [
                {
                    "invoice_item_index": 0,
                    "product_name": "Prawn 24 G",
                    "quantity": "2",
                    "unit_price": "10.50",
                    "line_total": None,
                    "line_total_source": "",
                }
            ],
        )


class InvoiceTotalsExtractionTests(PurchaseOrderValidationTestCase):
    def test_invoice_parsing_captures_net_tax_and_grand_total_from_common_labels(self):
        ocr_payload = {
            "analyzeResult": {
                "content": "\n".join(
                    [
                        "Gesamt Netto 127,16",
                        "19% USt. 24,16",
                        "Gesamtbetrag 151,32",
                    ]
                ),
                "documents": [
                    {
                        "fields": {
                            "Items": {"valueArray": []},
                        }
                    }
                ],
            }
        }

        totals = _extract_invoice_totals(ocr_payload)

        self.assertEqual(totals["invoice_net_total"], "127.16")
        self.assertEqual(totals["invoice_tax_total"], "24.16")
        self.assertEqual(totals["invoice_grand_total"], "151.32")

    def test_multiple_tax_rows_keep_breakdown_but_tax_total_is_derived_from_net_and_grand(self):
        ocr_payload = {
            "analyzeResult": {
                "documents": [
                    {
                        "fields": {
                            "Items": {"valueArray": []},
                            "SubTotal": {"valueCurrency": {"amount": 100}},
                            "InvoiceTotal": {"valueCurrency": {"amount": 130}},
                            "TaxDetails": {
                                "valueArray": [
                                    {
                                        "valueObject": {
                                            "TaxRate": {"valueString": "19%"},
                                            "Amount": {"valueCurrency": {"amount": 24.16}},
                                        }
                                    },
                                    {
                                        "valueObject": {
                                            "TaxRate": {"valueString": "7%"},
                                            "Amount": {"valueCurrency": {"amount": 50.12}},
                                        }
                                    },
                                ]
                            },
                        }
                    }
                ]
            }
        }

        totals = _extract_invoice_totals(ocr_payload)

        self.assertEqual(totals["invoice_net_total"], "100.00")
        self.assertEqual(totals["invoice_tax_total"], "30.00")
        self.assertEqual(totals["invoice_grand_total"], "130.00")
        self.assertEqual(
            totals["invoice_tax_breakdown"],
            {
                "19%": "24.16",
                "7%": "50.12",
            },
        )

    @patch("purchases.services.extract_invoice_data_with_azure")
    def test_missing_totals_do_not_break_validation_flow(self, extract_invoice_data_mock):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product)
        extract_invoice_data_mock.return_value = {
            "source": "azure_document_intelligence",
            "raw_text": "invoice text",
            "invoice_items": [self._invoice_item(name=product.display_name, quantity=2, unit_price="10.50")],
        }

        validation_result = analyze_purchase_order_invoice(purchase_order)

        self.assertEqual(validation_result["classification"], "matched")
        self.assertIsNone(validation_result["invoice_net_total"])
        self.assertIsNone(validation_result["invoice_tax_total"])
        self.assertIsNone(validation_result["invoice_grand_total"])
        self.assertIsNone(validation_result["invoice_tax_breakdown"])

    def test_line_total_difference_does_not_create_a_separate_validation_blocker(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product)

        validation_result = classify_invoice_against_purchase_order(
            purchase_order,
            [
                self._invoice_item(
                    name=product.display_name,
                    quantity=2,
                    unit_price="10.50",
                    amount="25.00",
                )
            ],
        )

        self.assertEqual(validation_result["classification"], "matched")
        self.assertFalse(validation_result["has_any_mismatch"])
        self.assertNotIn("calculation_mismatches", validation_result)

    @patch("purchases.services.extract_invoice_data_with_azure")
    def test_invoice_total_difference_does_not_block_validation(self, extract_invoice_data_mock):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product)
        extract_invoice_data_mock.return_value = {
            "source": "azure_document_intelligence",
            "raw_text": "invoice text",
            "invoice_items": [
                self._invoice_item(
                    name=product.display_name,
                    quantity=2,
                    unit_price="10.50",
                    amount="21.00",
                )
            ],
            "invoice_net_total": "21.00",
            "invoice_tax_total": "1.00",
            "invoice_grand_total": "25.00",
        }

        validation_result = analyze_purchase_order_invoice(purchase_order)

        self.assertEqual(validation_result["classification"], "matched")
        self.assertFalse(validation_result["has_any_mismatch"])
        self.assertEqual(validation_result["invoice_grand_total"], "25.00")


class HistoryInvoiceTotalsDisplayTests(PurchaseOrderValidationTestCase):
    def test_history_page_renders_invoice_totals_and_derives_tax_from_grand_minus_net(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product)
        purchase_order.status = PurchaseOrder.STATUS_CLOSED
        purchase_order.validation_note = PurchaseOrder.NOTE_VALIDATED_SUCCESSFULLY
        purchase_order.closed_at = timezone.now()
        purchase_order.validated_at = timezone.now()
        purchase_order.validation_data = {
            "invoice_net_total": "127.16",
            "invoice_tax_total": "99.99",
            "invoice_grand_total": "151.32",
            "invoice_tax_breakdown": {
                "19%": "24.16",
            },
        }
        purchase_order.save()

        response = self.client.get(reverse("purchases:history_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Net Total: 127.16")
        self.assertContains(response, "Tax: 24.16")
        self.assertContains(response, "Grand Total: 151.32")
        self.assertContains(response, "Tax breakdown:")
        self.assertContains(response, "19%: 24.16")
        self.assertNotContains(response, "Tax: 99.99")


class InvoiceUploadTests(PurchaseOrderValidationTestCase):
    def setUp(self):
        super().setUp()
        self.temp_media_root = tempfile.mkdtemp()
        self.override_settings = override_settings(MEDIA_ROOT=self.temp_media_root)
        self.override_settings.enable()

    def tearDown(self):
        self.override_settings.disable()
        shutil.rmtree(self.temp_media_root, ignore_errors=True)
        super().tearDown()

    def _uploaded_invoice(self, file_name, *, content_type):
        if file_name.endswith(".pdf"):
            content = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"
        elif file_name.endswith(".png"):
            content = b"\x89PNG\r\n\x1a\ninvoice"
        elif file_name.endswith(".jpg") or file_name.endswith(".jpeg"):
            content = b"\xff\xd8\xff\xe0invoice"
        else:
            content = b"invoice"
        return SimpleUploadedFile(file_name, content, content_type=content_type)

    def _validation_result(self):
        return {
            "classification": "matched",
            "invoice_items": [],
            "missing_products": [],
            "extra_products": [],
            "quantity_mismatches": [],
            "price_mismatches": [],
            "matched_items": [],
            "has_product_mismatch": False,
            "has_quantity_mismatch": False,
            "has_price_mismatch": False,
            "has_any_mismatch": False,
            "requires_manual_close": False,
            "can_update_prices": False,
        }

    def test_po_list_upload_input_accepts_pdf_and_image_files(self):
        product = self._create_product()
        self._create_purchase_order(product)

        response = self.client.get(reverse("purchases:po_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'accept=".pdf,.jpg,.jpeg,.png"')
        self.assertContains(response, "Upload PDF or invoice image")

    def test_pdf_upload_still_works(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product)

        response = self.client.post(
            reverse("purchases:upload_invoice", args=[purchase_order.id]),
            data={
                "invoice_file": self._uploaded_invoice("invoice.pdf", content_type="application/pdf"),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        purchase_order.refresh_from_db()
        self.assertEqual(purchase_order.status, PurchaseOrder.STATUS_INVOICE_UPLOADED)
        self.assertTrue(purchase_order.invoice_file.name.endswith(".pdf"))
        self.assertContains(response, "Invoice uploaded. Click Validate to compare against the PO.")

    def test_jpg_and_png_uploads_are_accepted(self):
        for file_name, content_type in (
            ("invoice.jpg", "image/jpeg"),
            ("invoice.png", "image/png"),
        ):
            with self.subTest(file_name=file_name):
                product = self._create_product(display_name=f"Product for {file_name}")
                purchase_order = self._create_purchase_order(product)

                response = self.client.post(
                    reverse("purchases:upload_invoice", args=[purchase_order.id]),
                    data={
                        "invoice_file": self._uploaded_invoice(file_name, content_type=content_type),
                    },
                    follow=True,
                )

                self.assertEqual(response.status_code, 200)
                purchase_order.refresh_from_db()
                self.assertEqual(purchase_order.status, PurchaseOrder.STATUS_INVOICE_UPLOADED)
                self.assertTrue(purchase_order.invoice_file.name.endswith(file_name.split(".")[-1]))
                self.assertContains(response, "Invoice uploaded. Click Validate to compare against the PO.")

    def test_unsupported_invoice_file_types_are_rejected_safely(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product)

        response = self.client.post(
            reverse("purchases:upload_invoice", args=[purchase_order.id]),
            data={
                "invoice_file": self._uploaded_invoice("invoice.txt", content_type="text/plain"),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        purchase_order.refresh_from_db()
        self.assertFalse(bool(purchase_order.invoice_file))
        self.assertEqual(purchase_order.status, PurchaseOrder.STATUS_SENT)
        self.assertContains(response, "Upload a PDF or invoice image (.pdf, .jpg, .jpeg, .png).")

    @patch("purchases.views.analyze_purchase_order_invoice")
    def test_validation_path_receives_uploaded_pdf_and_image_files(self, analyze_invoice_mock):
        analyze_invoice_mock.return_value = self._validation_result()

        for file_name, content_type in (
            ("invoice.pdf", "application/pdf"),
            ("invoice.png", "image/png"),
        ):
            with self.subTest(file_name=file_name):
                analyze_invoice_mock.reset_mock()
                product = self._create_product(display_name=f"Validation product {file_name}")
                purchase_order = self._create_purchase_order(product)

                self.client.post(
                    reverse("purchases:upload_invoice", args=[purchase_order.id]),
                    data={
                        "invoice_file": self._uploaded_invoice(file_name, content_type=content_type),
                    },
                )

                response = self.client.post(
                    reverse("purchases:validate_invoice", args=[purchase_order.id]),
                    follow=True,
                )

                self.assertEqual(response.status_code, 200)
                analyze_invoice_mock.assert_called_once()
                analyzed_purchase_order = analyze_invoice_mock.call_args[0][0]
                self.assertTrue(analyzed_purchase_order.invoice_file.name.endswith(file_name))
                self.assertContains(response, "Invoice validated successfully.")


class PurchaseOrderCreationWorkflowTests(PurchaseOrderValidationTestCase):
    def test_create_po_from_cart_vendor_still_creates_po_and_clears_vendor_cart_items(self):
        product = self._create_product()
        vendor_price = VendorProductPrice.objects.create(
            product=product,
            vendor=self.vendor,
            price=Decimal("8.50"),
            currency="EUR",
            is_active=True,
        )
        cart = Cart.objects.create(user=self.user, status=Cart.STATUS_OPEN)
        CartItem.objects.create(
            cart=cart,
            product=product,
            vendor=self.vendor,
            quantity=3,
            unit_price=vendor_price.price,
        )

        response = self.client.post(
            reverse("purchases:create_po_from_cart_vendor", args=[self.vendor.id])
        )

        self.assertRedirects(response, reverse("purchases:po_list"))
        purchase_order = PurchaseOrder.objects.get()
        po_item = purchase_order.items.get()
        self.assertEqual(po_item.product, product)
        self.assertEqual(po_item.quantity, 3)
        self.assertEqual(po_item.unit_price, Decimal("8.50"))
        self.assertEqual(po_item.line_total, Decimal("25.50"))
        self.assertFalse(cart.items.exists())


class ManualReconciliationTests(PurchaseOrderValidationTestCase):
    def _create_name_quantity_price_mismatch(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=3, unit_price="10.50")
        po_item = purchase_order.items.get()
        invoice_items = [self._invoice_item(name="Basmty", quantity=2, unit_price="11.00")]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        self._set_validation_state(purchase_order, validation_result)
        return product, purchase_order, po_item, invoice_items

    def _create_quantity_price_mismatch(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=3, unit_price="10.50")
        po_item = purchase_order.items.get()
        invoice_items = [self._invoice_item(name="Rice bag 20 Kg", quantity=2, unit_price="11.00")]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        self._set_validation_state(purchase_order, validation_result)
        return product, purchase_order, po_item, invoice_items

    def _create_quantity_shortage_with_invoice_totals(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=3, unit_price="10.50")
        invoice_items = [
            self._invoice_item(
                name="Rice bag 20 Kg",
                quantity=2,
                unit_price="10.50",
                amount="21.00",
            )
        ]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        validation_result["invoice_net_total"] = "21.00"
        validation_result["invoice_tax_total"] = "0.00"
        validation_result["invoice_grand_total"] = "21.00"
        self._set_validation_state(purchase_order, validation_result)
        return product, purchase_order, purchase_order.items.get(), invoice_items

    def _create_price_change_with_invoice_totals(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=2, unit_price="10.50")
        invoice_items = [
            self._invoice_item(
                name="Rice bag 20 Kg",
                quantity=2,
                unit_price="11.00",
                amount="22.00",
            )
        ]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        validation_result["invoice_net_total"] = "22.00"
        validation_result["invoice_tax_total"] = "0.00"
        validation_result["invoice_grand_total"] = "22.00"
        self._set_validation_state(purchase_order, validation_result)
        return product, purchase_order, purchase_order.items.get(), invoice_items

    def _create_quantity_and_price_change_with_invoice_totals(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=3, unit_price="10.50")
        invoice_items = [
            self._invoice_item(
                name="Rice bag 20 Kg",
                quantity=2,
                unit_price="11.00",
                amount="22.00",
            )
        ]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        validation_result["invoice_net_total"] = "22.00"
        validation_result["invoice_tax_total"] = "0.00"
        validation_result["invoice_grand_total"] = "22.00"
        self._set_validation_state(purchase_order, validation_result)
        return product, purchase_order, purchase_order.items.get(), invoice_items

    def _create_calculation_mismatch_with_invoice_totals(self):
        product = self._create_product(
            product_name="Oil Tin",
            display_name="Oil Tin 10 Liters",
            quantity_per_pack="10",
            quantity_unit="Liters",
        )
        purchase_order = self._create_purchase_order(product, quantity=2, unit_price="30.00")
        invoice_items = [
            self._invoice_item(
                name="Oil Tin 10 Liters",
                quantity=2,
                unit_price="30.00",
                amount="70.00",
            )
        ]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        validation_result["invoice_net_total"] = "70.00"
        validation_result["invoice_tax_total"] = "10.00"
        validation_result["invoice_grand_total"] = "80.00"
        self._set_validation_state(purchase_order, validation_result)
        return product, purchase_order, purchase_order.items.get(), invoice_items

    def _create_over_delivery_with_invoice_totals(
        self,
        *,
        invoice_quantity=3,
        invoice_unit_price="10.50",
        amount=None,
        invoice_net_total=None,
        invoice_tax_total="0.00",
        invoice_grand_total=None,
    ):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=2, unit_price="10.50")
        resolved_amount = amount or str(Decimal(str(invoice_quantity)) * Decimal(str(invoice_unit_price)))
        resolved_net_total = invoice_net_total or resolved_amount
        resolved_grand_total = invoice_grand_total or (
            str(Decimal(str(resolved_net_total)) + Decimal(str(invoice_tax_total)))
        )
        invoice_items = [
            self._invoice_item(
                name="Rice bag 20 Kg",
                quantity=invoice_quantity,
                unit_price=invoice_unit_price,
                amount=resolved_amount,
            )
        ]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        validation_result["invoice_net_total"] = resolved_net_total
        validation_result["invoice_tax_total"] = invoice_tax_total
        validation_result["invoice_grand_total"] = resolved_grand_total
        self._set_validation_state(purchase_order, validation_result)
        return product, purchase_order, purchase_order.items.get(), invoice_items

    def _create_mixed_over_delivery_price_calculation_mismatch(self):
        return self._create_over_delivery_with_invoice_totals(
            invoice_quantity=3,
            invoice_unit_price="11.00",
            amount="40.00",
            invoice_net_total="40.00",
            invoice_tax_total="0.00",
            invoice_grand_total="40.00",
        )

    def test_po_list_renders_manual_reconciliation_controls(self):
        product = self._create_product()
        unrelated_product = self._create_product(
            product_name="Sugar bag",
            display_name="Sugar bag 25 Kg",
            quantity_per_pack="25",
        )
        purchase_order = self._create_purchase_order(product)
        po_item = purchase_order.items.get()
        invoice_items = [self._invoice_item(name="Basmty", quantity=2, unit_price="10.50")]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        self._set_validation_state(purchase_order, validation_result)

        response = self.client.get(reverse("purchases:po_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Manual Reconciliation")
        self.assertContains(response, "Manual Validate")
        self.assertContains(response, "Save as new alternative name")
        self.assertContains(
            response,
            (
                f'<option value="{product.id}" data-group="po" '
                f'data-po-item-id="{po_item.id}" data-po-quantity="2" '
                f'data-po-unit-price="10.50">{product.display_name}</option>'
            ),
            html=True,
        )
        self.assertNotContains(response, unrelated_product.display_name)
        self.assertNotContains(response, "Master Products")

    def test_manual_name_mismatch_validation_creates_alias_and_closes_po(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product)
        invoice_items = [self._invoice_item(name="Basmty", quantity=2, unit_price="10.50")]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        self._set_validation_state(purchase_order, validation_result)

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                "map_product_0": str(product.id),
                "save_alias_0": "on",
            },
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()

        self.assertEqual(purchase_order.status, PurchaseOrder.STATUS_CLOSED)
        self.assertTrue(product.aliases.filter(alias_name="Basmty").exists())
        self.assertIn(
            'Name mismatch detected: invoice item "Basmty" was manually confirmed as product "Rice bag 20 Kg". "Basmty" was added as an alternative name.',
            purchase_order.validation_data.get("audit_notes", []),
        )

    def test_repeated_invoice_uses_new_alias_for_future_auto_matching(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product)
        invoice_items = [self._invoice_item(name="Basmty", quantity=2, unit_price="10.50")]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        self._set_validation_state(purchase_order, validation_result)

        self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                "map_product_0": str(product.id),
                "save_alias_0": "on",
            },
        )

        repeated_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)

        self.assertEqual(repeated_result["classification"], "matched")
        self.assertFalse(repeated_result["has_product_mismatch"])

    def test_name_quantity_and_price_mismatch_requires_all_decisions(self):
        product, purchase_order, _, _ = self._create_name_quantity_price_mismatch()

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                "map_product_0": str(product.id),
                "save_alias_0": "on",
            },
        )

        self.assertRedirects(response, reverse("purchases:po_list"))
        purchase_order.refresh_from_db()

        self.assertEqual(purchase_order.status, PurchaseOrder.STATUS_PRODUCT_MISMATCH)
        self.assertFalse(product.aliases.filter(alias_name="Basmty").exists())
        self.assertEqual(purchase_order.validation_data.get("audit_notes"), None)

    def test_sidebar_po_badge_counts_only_active_purchase_orders(self):
        active_product = self._create_product()
        closed_product = self._create_product(
            product_name="Sugar bag",
            display_name="Sugar bag 25 Kg",
            quantity_per_pack="25",
        )
        self._create_purchase_order(active_product)
        another_active_po = self._create_purchase_order(active_product)
        closed_po = self._create_purchase_order(closed_product)
        closed_po.status = PurchaseOrder.STATUS_CLOSED
        closed_po.save(update_fields=["status"])

        response = self.client.get(reverse("purchases:po_list"))

        self.assertEqual(response.status_code, 200)
        self.assertInHTML(
            (
                f'<a href="{reverse("purchases:po_list")}" class="active">'
                '<span class="nav-label">PO</span>'
                '<span class="cart-badge">2</span>'
                "</a>"
            ),
            response.content.decode(),
        )
        self.assertNotContains(response, '<span class="cart-badge">3</span>', html=True)
        self.assertEqual(another_active_po.status, PurchaseOrder.STATUS_SENT)

    def test_sidebar_po_badge_hides_zero_count(self):
        closed_product = self._create_product()
        closed_po = self._create_purchase_order(closed_product)
        closed_po.status = PurchaseOrder.STATUS_CLOSED
        closed_po.save(update_fields=["status"])

        response = self.client.get(reverse("purchases:history_list"))

        self.assertEqual(response.status_code, 200)
        self.assertInHTML(
            (
                f'<a href="{reverse("purchases:po_list")}" class="">'
                '<span class="nav-label">PO</span>'
                "</a>"
            ),
            response.content.decode(),
        )
        self.assertNotContains(response, '<span class="cart-badge">0</span>', html=True)

    def test_quantity_and_price_mismatch_together_requires_both_decisions(self):
        _, purchase_order, po_item, _ = self._create_quantity_price_mismatch()

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                f"quantity_resolution_{po_item.id}": "full_received",
            },
        )

        self.assertRedirects(response, reverse("purchases:po_list"))
        purchase_order.refresh_from_db()

        self.assertEqual(purchase_order.status, PurchaseOrder.STATUS_QUANTITY_MISMATCH)
        self.assertEqual(purchase_order.validation_data.get("audit_notes"), None)

    def test_po_list_renders_new_price_labels_and_comparison_block(self):
        _, purchase_order, _, _ = self._create_quantity_price_mismatch()

        response = self.client.get(reverse("purchases:po_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Yes, price changed — update to new price")
        self.assertContains(response, "No, invoice mistake — keep old price")
        self.assertContains(response, "Current / PO price")
        self.assertContains(response, "Invoice / New price")
        self.assertContains(response, "Difference")
        self.assertContains(response, "+0.50")
        self.assertContains(response, "+4.76%")
        self.assertContains(response, purchase_order.items.get().product.display_name)

    def test_po_list_does_not_render_calculation_or_invoice_total_review_ui(self):
        _, purchase_order, _, _ = self._create_calculation_mismatch_with_invoice_totals()

        response = self.client.get(reverse("purchases:po_list"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Calculation mismatch reconciliation")
        self.assertNotContains(response, "Invoice Total Mismatches")
        self.assertNotContains(response, "Vendor confirmed corrected amount")
        self.assertContains(response, purchase_order.items.get().product.display_name)

    def test_over_delivery_shows_accept_additional_quantity_choices(self):
        _, purchase_order, po_item, _ = self._create_over_delivery_with_invoice_totals()

        response = self.client.get(reverse("purchases:po_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Accept additional quantity")
        self.assertContains(response, "Do not accept additional quantity")
        self.assertNotRegex(
            response.content.decode(),
            re.compile(
                rf'name="quantity_resolution_{po_item.id}" value="use_invoice_quantity" required>\s*'
                r"<span>Quantity still missing physically</span>"
            ),
        )

    def test_manual_validate_is_blocked_if_price_decision_is_missing(self):
        product, purchase_order, _, _ = self._create_name_quantity_price_mismatch()

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                "map_product_0": str(product.id),
                "save_alias_0": "on",
                "mapped_quantity_resolution_0": "full_received",
            },
        )

        self.assertRedirects(response, reverse("purchases:po_list"))
        purchase_order.refresh_from_db()

        self.assertEqual(purchase_order.status, PurchaseOrder.STATUS_PRODUCT_MISMATCH)
        self.assertFalse(product.aliases.filter(alias_name="Basmty").exists())
        self.assertEqual(purchase_order.validation_data.get("audit_notes"), None)

    def test_download_pdf_shows_only_serial_product_and_quantity_columns(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=2, unit_price="10.50")
        second_product = self._create_product(
            product_name="Beans",
            display_name="Beans 10 Kg",
            quantity_per_pack="10",
        )
        PurchaseOrderItem.objects.create(
            purchase_order=purchase_order,
            product=second_product,
            quantity=4,
            unit_price=Decimal("7.25"),
            line_total=Decimal("29.00"),
        )

        response = self.client.get(reverse("purchases:download_po_pdf", args=[purchase_order.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(b"(IBC)", response.content)
        self.assertIn(b"(Purchase Order)", response.content)
        self.assertIn(b"(S.No)", response.content)
        self.assertIn(b"(Product)", response.content)
        self.assertIn(b"(Quantity)", response.content)
        self.assertIn(b"(1)", response.content)
        self.assertIn(b"(2)", response.content)
        self.assertNotIn(b"(Unit Price)", response.content)
        self.assertNotIn(b"(Line Total)", response.content)
        self.assertNotIn(b"(Subtotal:)", response.content)
        self.assertNotIn(b"(Total:)", response.content)

    def test_manual_validate_is_blocked_if_quantity_decision_is_missing(self):
        _, purchase_order, po_item, _ = self._create_quantity_price_mismatch()

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                f"price_decision_{po_item.id}": "accepted",
            },
        )

        self.assertRedirects(response, reverse("purchases:po_list"))
        purchase_order.refresh_from_db()

        self.assertEqual(purchase_order.status, PurchaseOrder.STATUS_QUANTITY_MISMATCH)
        self.assertEqual(purchase_order.validation_data.get("audit_notes"), None)

    def test_quantity_mismatch_can_be_manually_validated_after_physical_check(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=3)
        po_item = purchase_order.items.get()
        invoice_items = [self._invoice_item(name="Rice bag 20 Kg", quantity=2, unit_price="10.50")]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        self._set_validation_state(purchase_order, validation_result)

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                f"quantity_resolution_{po_item.id}": "full_received",
            },
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()

        self.assertEqual(purchase_order.status, PurchaseOrder.STATUS_CLOSED)
        self.assertEqual(purchase_order.validation_data.get("quantity_mismatches"), [])
        self.assertIn(
            'Quantity mismatch detected for "Rice bag 20 Kg": PO qty 3, invoice qty 2. User physically verified 3 units were received and manually validated.',
            purchase_order.validation_data.get("audit_notes", []),
        )

    def test_quantity_shortage_can_be_manually_confirmed_and_closed(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=3)
        po_item = purchase_order.items.get()
        invoice_items = [self._invoice_item(name="Rice bag 20 Kg", quantity=2, unit_price="10.50")]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        self._set_validation_state(purchase_order, validation_result)

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                f"quantity_resolution_{po_item.id}": "shortage",
            },
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()

        self.assertEqual(purchase_order.status, PurchaseOrder.STATUS_CLOSED)
        self.assertEqual(
            purchase_order.validation_note,
            "Manual reconciliation completed with confirmed shortage.",
        )
        self.assertIn(
            'Quantity mismatch detected for "Rice bag 20 Kg": PO qty 3, invoice qty 2. User physically verified shortage remains. PO closed with missing quantity of 1.',
            purchase_order.validation_data.get("audit_notes", []),
        )

    def test_quantity_shortage_confirmed_reduces_effective_line_total_and_po_total(self):
        _, purchase_order, po_item, _ = self._create_quantity_shortage_with_invoice_totals()

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                f"quantity_resolution_{po_item.id}": "shortage",
            },
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()

        effective_item = purchase_order.validation_data["effective_items"][0]
        self.assertEqual(effective_item["effective_quantity"], "2")
        self.assertEqual(effective_item["effective_unit_price"], "10.50")
        self.assertEqual(effective_item["effective_line_total"], "21.00")
        self.assertEqual(purchase_order.validation_data["effective_net_total"], "21.00")
        self.assertIn(
            'Effective PO values for "Rice bag 20 Kg" were reduced to quantity 2 and line total 21.00 after confirming the shortage.',
            purchase_order.validation_data.get("audit_notes", []),
        )

    def test_price_acceptance_path_updates_vendor_price_and_closes_po(self):
        product, purchase_order, po_item, _ = self._create_quantity_price_mismatch()

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                f"quantity_resolution_{po_item.id}": "full_received",
                f"price_decision_{po_item.id}": "accepted",
            },
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()

        vendor_price = VendorProductPrice.objects.get(vendor=self.vendor, product=product)
        self.assertEqual(purchase_order.status, PurchaseOrder.STATUS_CLOSED)
        self.assertEqual(vendor_price.price, Decimal("11.00"))
        self.assertEqual(purchase_order.validation_data.get("price_mismatches"), [])
        self.assertIn(
            'Price mismatch detected for "Rice bag 20 Kg": PO unit price 10.50, invoice unit price 11.00. User confirmed the price changed and the stored price was updated to the new invoice price.',
            purchase_order.validation_data.get("audit_notes", []),
        )

    def test_accepted_price_increase_updates_effective_unit_price_and_totals(self):
        product, purchase_order, po_item, _ = self._create_price_change_with_invoice_totals()

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                f"price_decision_{po_item.id}": "accepted",
            },
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()

        effective_item = purchase_order.validation_data["effective_items"][0]
        self.assertEqual(effective_item["effective_quantity"], "2")
        self.assertEqual(effective_item["effective_unit_price"], "11.00")
        self.assertEqual(effective_item["effective_line_total"], "22.00")
        self.assertEqual(purchase_order.validation_data["effective_net_total"], "22.00")
        self.assertEqual(
            VendorProductPrice.objects.get(vendor=self.vendor, product=product).price,
            Decimal("11.00"),
        )
        self.assertIn(
            'Effective PO values for "Rice bag 20 Kg" were recalculated using unit price 11.00.',
            purchase_order.validation_data.get("audit_notes", []),
        )

    def test_keep_old_price_path_preserves_existing_price_and_closes_po(self):
        product, purchase_order, po_item, _ = self._create_quantity_price_mismatch()
        VendorProductPrice.objects.create(
            vendor=self.vendor,
            product=product,
            price=Decimal("10.50"),
            currency="EUR",
            is_active=True,
        )

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                f"quantity_resolution_{po_item.id}": "full_received",
                f"price_decision_{po_item.id}": "keep_old",
            },
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()

        self.assertEqual(purchase_order.status, PurchaseOrder.STATUS_CLOSED)
        self.assertEqual(purchase_order.validation_data.get("price_mismatches"), [])
        self.assertEqual(
            VendorProductPrice.objects.get(vendor=self.vendor, product=product).price,
            Decimal("10.50"),
        )
        self.assertIn(
            'Price mismatch detected for "Rice bag 20 Kg": PO unit price 10.50, invoice unit price 11.00. User marked the invoice price as a mistake. The existing price was kept unchanged.',
            purchase_order.validation_data.get("audit_notes", []),
        )

    def test_quantity_and_price_changes_recalculate_effective_values_together(self):
        _, purchase_order, po_item, _ = self._create_quantity_and_price_change_with_invoice_totals()

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                f"quantity_resolution_{po_item.id}": "shortage",
                f"price_decision_{po_item.id}": "accepted",
            },
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()

        effective_item = purchase_order.validation_data["effective_items"][0]
        self.assertEqual(effective_item["effective_quantity"], "2")
        self.assertEqual(effective_item["effective_unit_price"], "11.00")
        self.assertEqual(effective_item["effective_line_total"], "22.00")
        self.assertEqual(purchase_order.validation_data["effective_net_total"], "22.00")

    def test_accepting_additional_quantity_uses_invoice_quantity(self):
        _, purchase_order, po_item, _ = self._create_over_delivery_with_invoice_totals()

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                f"quantity_resolution_{po_item.id}": "use_invoice_quantity",
            },
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()

        effective_item = purchase_order.validation_data["effective_items"][0]
        self.assertEqual(purchase_order.status, PurchaseOrder.STATUS_CLOSED)
        self.assertEqual(effective_item["effective_quantity"], "3")
        self.assertEqual(effective_item["effective_line_total"], "31.50")
        self.assertEqual(purchase_order.validation_data["effective_net_total"], "31.50")

    def test_rejecting_additional_quantity_keeps_po_quantity_and_uses_accepted_totals(self):
        _, purchase_order, po_item, _ = self._create_over_delivery_with_invoice_totals(
            invoice_net_total="31.50",
            invoice_grand_total="31.50",
        )

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                f"quantity_resolution_{po_item.id}": "use_po_quantity",
            },
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()

        effective_item = purchase_order.validation_data["effective_items"][0]
        self.assertEqual(purchase_order.status, PurchaseOrder.STATUS_CLOSED)
        self.assertEqual(effective_item["effective_quantity"], "2")
        self.assertEqual(effective_item["effective_line_total"], "21.00")
        self.assertNotIn("calculation_mismatches", purchase_order.validation_data)
        self.assertNotIn("invoice_total_mismatches", purchase_order.validation_data)

    def test_po_can_close_without_line_total_or_invoice_total_validation(self):
        _, purchase_order, po_item, _ = self._create_mixed_over_delivery_price_calculation_mismatch()

        purchase_order.refresh_from_db()
        self.assertTrue(purchase_order.validation_data["has_quantity_mismatch"])
        self.assertTrue(purchase_order.validation_data["has_price_mismatch"])

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                f"quantity_resolution_{po_item.id}": "use_invoice_quantity",
                f"price_decision_{po_item.id}": "accepted",
            },
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()

        effective_item = purchase_order.validation_data["effective_items"][0]
        self.assertEqual(purchase_order.status, PurchaseOrder.STATUS_CLOSED)
        self.assertEqual(effective_item["effective_quantity"], "3")
        self.assertEqual(effective_item["effective_unit_price"], "11.00")
        self.assertEqual(effective_item["effective_line_total"], "33.00")
        self.assertEqual(purchase_order.validation_data["effective_net_total"], "33.00")
        self.assertNotIn("calculation_mismatches", purchase_order.validation_data)
        self.assertNotIn("invoice_total_mismatches", purchase_order.validation_data)

    def test_audit_notes_reflect_po_value_adjustments_without_invoice_total_review(self):
        _, purchase_order, po_item, _ = self._create_quantity_shortage_with_invoice_totals()
        purchase_order.validation_data["invoice_grand_total"] = "25.00"
        purchase_order.save(update_fields=["validation_data"])

        self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                f"quantity_resolution_{po_item.id}": "shortage",
            },
        )

        purchase_order.refresh_from_db()
        audit_notes = purchase_order.validation_data.get("audit_notes", [])

        self.assertIn(
            'Effective PO values for "Rice bag 20 Kg" were reduced to quantity 2 and line total 21.00 after confirming the shortage.',
            audit_notes,
        )
        self.assertFalse(any("Invoice total mismatch detected" in note for note in audit_notes))

    def test_invalid_manual_submission_without_mapping_keeps_po_safe(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product)
        invoice_items = [self._invoice_item(name="Basmty", quantity=2, unit_price="10.50")]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        self._set_validation_state(purchase_order, validation_result)

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={},
        )

        self.assertRedirects(response, reverse("purchases:po_list"))
        purchase_order.refresh_from_db()

        self.assertEqual(purchase_order.status, PurchaseOrder.STATUS_PRODUCT_MISMATCH)
        self.assertFalse(product.aliases.filter(alias_name="Basmty").exists())
        self.assertEqual(purchase_order.validation_data.get("audit_notes"), None)


class HistoryReconciliationSummaryTests(PurchaseOrderValidationTestCase):
    def _get_history_content(self):
        response = self.client.get(reverse("purchases:history_list"))
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def _create_closed_over_delivery_purchase_order(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=2, unit_price="10.50")
        po_item = purchase_order.items.get()
        invoice_items = [
            self._invoice_item(
                name="Rice bag 20 Kg",
                quantity=3,
                unit_price="10.50",
                amount="31.50",
            )
        ]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        validation_result["invoice_net_total"] = "31.50"
        validation_result["invoice_tax_total"] = "0.00"
        validation_result["invoice_grand_total"] = "31.50"
        self._set_validation_state(purchase_order, validation_result)

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={f"quantity_resolution_{po_item.id}": "use_invoice_quantity"},
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()
        return purchase_order

    def _create_closed_price_change_purchase_order(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=2, unit_price="10.50")
        po_item = purchase_order.items.get()
        invoice_items = [
            self._invoice_item(
                name="Rice bag 20 Kg",
                quantity=2,
                unit_price="11.00",
                amount="22.00",
            )
        ]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        validation_result["invoice_net_total"] = "22.00"
        validation_result["invoice_tax_total"] = "0.00"
        validation_result["invoice_grand_total"] = "22.00"
        self._set_validation_state(purchase_order, validation_result)

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={f"price_decision_{po_item.id}": "accepted"},
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()
        return purchase_order

    def _create_closed_quantity_and_price_change_purchase_order(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=3, unit_price="10.50")
        po_item = purchase_order.items.get()
        invoice_items = [
            self._invoice_item(
                name="Rice bag 20 Kg",
                quantity=2,
                unit_price="11.00",
                amount="22.00",
            )
        ]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        validation_result["invoice_net_total"] = "22.00"
        validation_result["invoice_tax_total"] = "0.00"
        validation_result["invoice_grand_total"] = "22.00"
        self._set_validation_state(purchase_order, validation_result)

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                f"quantity_resolution_{po_item.id}": "shortage",
                f"price_decision_{po_item.id}": "accepted",
            },
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()
        return purchase_order

    def test_history_summary_includes_quantity_before_after_and_value_change(self):
        purchase_order = self._create_closed_over_delivery_purchase_order()

        response, content = self._get_history_content()

        expected_summary = (
            "Rice bag 20 Kg: Extra quantity was received. PO quantity was 2, invoice quantity was 3. "
            "User verified and accepted 3. Line value increased from 21.00 to 31.50."
        )
        self.assertContains(response, expected_summary)
        self.assertEqual(content.count(expected_summary), 1)
        self.assertTrue(
            any(
                "User accepted the additional quantity from the invoice."
                in note
                for note in purchase_order.validation_data.get("audit_notes", [])
            )
        )

    def test_history_summary_includes_price_before_after_and_value_change(self):
        self._create_closed_price_change_purchase_order()

        response, content = self._get_history_content()

        expected_summary = (
            "Rice bag 20 Kg: Price changed. PO unit price was 10.50, invoice unit price was 11.00. "
            "User verified and accepted the new price. Line value increased from 21.00 to 22.00."
        )
        self.assertContains(response, expected_summary)
        self.assertEqual(content.count(expected_summary), 1)

    def test_history_summary_combines_quantity_and_price_changes_into_one_bullet(self):
        self._create_closed_quantity_and_price_change_purchase_order()

        response, content = self._get_history_content()

        expected_summary = (
            "Rice bag 20 Kg: Shortage was confirmed. PO quantity was 3, invoice quantity was 2. "
            "User verified and accepted 2. Price changed. PO unit price was 10.50, "
            "invoice unit price was 11.00. User verified and accepted the new price. "
            "Line value decreased from 31.50 to 22.00."
        )
        self.assertContains(response, expected_summary)
        self.assertEqual(content.count(expected_summary), 1)

    def test_history_summary_keeps_raw_audit_notes_internal_without_rendering_verbose_lines(self):
        purchase_order = self._create_closed_price_change_purchase_order()

        response, content = self._get_history_content()

        self.assertTrue(
            any(
                "Effective PO values for" in note
                for note in purchase_order.validation_data.get("audit_notes", [])
            )
        )
        self.assertNotContains(response, "Effective PO values for")
        self.assertNotContains(response, "recalculated using unit price")
        self.assertIn("history-summary-list", content)


class HistoryInvoiceReferenceTotalsTests(PurchaseOrderValidationTestCase):
    def _get_history_response(self):
        response = self.client.get(reverse("purchases:history_list"))
        self.assertEqual(response.status_code, 200)
        return response

    def _create_over_delivery_purchase_order(
        self,
        *,
        accept_extra_quantity,
        invoice_tax_total="10.00",
    ):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=2, unit_price="10.50")
        po_item = purchase_order.items.get()
        invoice_items = [
            self._invoice_item(
                name="Rice bag 20 Kg",
                quantity=3,
                unit_price="10.50",
                amount="31.50",
            )
        ]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        validation_result["invoice_net_total"] = "31.50"
        validation_result["invoice_tax_total"] = invoice_tax_total
        validation_result["invoice_grand_total"] = str(
            Decimal("31.50") + Decimal(invoice_tax_total)
        )
        self._set_validation_state(purchase_order, validation_result)

        data = {
            f"quantity_resolution_{po_item.id}": (
                "use_invoice_quantity" if accept_extra_quantity else "use_po_quantity"
            )
        }

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data=data,
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()
        return purchase_order

    def _create_shortage_purchase_order(
        self,
        *,
        invoice_tax_total="10.00",
        invoice_tax_breakdown=None,
    ):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=3, unit_price="10.50")
        po_item = purchase_order.items.get()
        invoice_items = [
            self._invoice_item(
                name="Rice bag 20 Kg",
                quantity=2,
                unit_price="10.50",
                amount="21.00",
            )
        ]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        validation_result["invoice_net_total"] = "21.00"
        validation_result["invoice_tax_total"] = invoice_tax_total
        validation_result["invoice_grand_total"] = str(
            Decimal("21.00") + Decimal(invoice_tax_total)
        )
        if invoice_tax_breakdown is not None:
            validation_result["invoice_tax_breakdown"] = invoice_tax_breakdown
        self._set_validation_state(purchase_order, validation_result)

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={f"quantity_resolution_{po_item.id}": "shortage"},
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()
        return purchase_order

    def _create_price_keep_old_purchase_order(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=2, unit_price="10.50")
        po_item = purchase_order.items.get()
        invoice_items = [
            self._invoice_item(
                name="Rice bag 20 Kg",
                quantity=2,
                unit_price="11.00",
                amount="22.00",
            )
        ]
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        validation_result["invoice_net_total"] = "22.00"
        validation_result["invoice_tax_total"] = "8.00"
        validation_result["invoice_grand_total"] = "30.00"
        self._set_validation_state(purchase_order, validation_result)

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                f"price_decision_{po_item.id}": "keep_old",
            },
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()
        return purchase_order

    def test_history_uses_invoice_totals_when_available(self):
        self._create_over_delivery_purchase_order(accept_extra_quantity=True)

        response = self._get_history_response()

        self.assertContains(response, "Net Total: 31.50")
        self.assertContains(response, "Tax: 10.00")
        self.assertContains(response, "Grand Total: 41.50")

    def test_history_keeps_invoice_reference_totals_when_extra_quantity_is_not_accepted(self):
        self._create_over_delivery_purchase_order(accept_extra_quantity=False)

        response = self._get_history_response()

        self.assertContains(response, "Net Total: 31.50")
        self.assertContains(response, "Tax: 10.00")
        self.assertContains(response, "Grand Total: 41.50")

    def test_history_keeps_invoice_reference_totals_when_shortage_is_confirmed(self):
        self._create_shortage_purchase_order()

        response = self._get_history_response()

        self.assertContains(response, "Net Total: 21.00")
        self.assertContains(response, "Tax: 10.00")
        self.assertContains(response, "Grand Total: 31.00")

    def test_history_keeps_invoice_reference_totals_when_price_reconciliation_changes_final_math(self):
        self._create_price_keep_old_purchase_order()

        response = self._get_history_response()

        self.assertContains(response, "Net Total: 22.00")
        self.assertContains(response, "Tax: 8.00")
        self.assertContains(response, "Grand Total: 30.00")

    def test_history_uses_grand_minus_net_for_mixed_tax_invoice(self):
        self._create_shortage_purchase_order(
            invoice_tax_total="99.99",
            invoice_tax_breakdown={"19%": "5.67", "7%": "6.67"},
        )

        purchase_order = PurchaseOrder.objects.latest("id")
        validation_data = purchase_order.validation_data
        validation_data["invoice_net_total"] = "21.00"
        validation_data["invoice_grand_total"] = "33.34"
        validation_data["invoice_tax_total"] = "99.99"
        purchase_order.validation_data = validation_data
        purchase_order.save(update_fields=["validation_data"])

        response = self._get_history_response()

        self.assertContains(response, "Net Total: 21.00")
        self.assertContains(response, "Tax: 12.34")
        self.assertContains(response, "Grand Total: 33.34")
        self.assertNotContains(response, "Tax: 99.99")

    def test_history_hides_totals_safely_when_invoice_net_and_grand_are_missing(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product)
        purchase_order.status = PurchaseOrder.STATUS_CLOSED
        purchase_order.closed_at = timezone.now()
        purchase_order.validated_at = timezone.now()
        purchase_order.validation_data = {
            "invoice_tax_total": "10.00",
        }
        purchase_order.save(update_fields=["status", "closed_at", "validated_at", "validation_data"])

        response = self._get_history_response()

        self.assertNotContains(response, "Net Total:")
        self.assertNotContains(response, "Tax: 10.00")
        self.assertNotContains(response, "Grand Total:")

    def test_history_product_table_remains_the_original_po_table(self):
        self._create_shortage_purchase_order()

        response = self._get_history_response()

        self.assertContains(
            response,
            "<tr><td>Rice bag 20 Kg</td><td>3</td><td>10.50</td><td>31.50</td></tr>",
            html=True,
        )
        self.assertNotContains(
            response,
            "<tr><td>Rice bag 20 Kg</td><td>2</td><td>10.50</td><td>21.00</td></tr>",
            html=True,
        )
