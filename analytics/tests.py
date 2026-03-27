from datetime import datetime
import shutil
import tempfile
from decimal import Decimal
from io import StringIO
from unittest.mock import DEFAULT, call, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone

from inventory.models import PriceHistory, Product, Vendor, VendorProductPrice
from purchases.models import PurchaseOrder, PurchaseOrderItem
from purchases.services import apply_financial_validation, classify_invoice_against_purchase_order

from .constants import PriceChangeType, PriceDirection, QuantityChangeType
from .models import FactPriceChangeEvent, FactPurchaseOrder, FactPurchaseOrderItem
from .selectors import (
    get_average_po_value,
    get_frequent_price_changes,
    get_product_price_trend,
    get_products_with_price_trend,
    get_top_products,
    get_total_po_count,
    get_total_products_count,
    get_total_spend_metrics,
    get_vendor_price_change_movement_by_vendor,
    get_vendor_order_frequency,
    get_vendor_wise_spend,
)
from .services import sync_all_po_analytics
from .utils import resolve_period_range


class AnalyticsSyncTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_root = tempfile.mkdtemp()
        cls._media_override = override_settings(MEDIA_ROOT=cls._media_root)
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._media_override.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)
        super().tearDownClass()

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="analytics-user",
            password="testpass123",
        )
        cls.vendor = Vendor.objects.create(
            name="Analytics Vendor",
            email="analytics@example.com",
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

    def _invoice_item(self, *, name, quantity, unit_price, amount=None):
        item = {
            "name": name,
            "normalized_name": " ".join(name.lower().split()),
            "quantity": str(quantity),
            "unit_price": str(unit_price),
        }
        if amount is not None:
            item["amount"] = str(amount)
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
        purchase_order.save(update_fields=["status", "validation_note", "validation_data", "updated_at"])

    def _attach_invoice_file(self, purchase_order):
        purchase_order.invoice_file = SimpleUploadedFile(
            "invoice.pdf",
            b"%PDF-1.4 analytics test invoice",
            content_type="application/pdf",
        )
        purchase_order.status = PurchaseOrder.STATUS_INVOICE_UPLOADED
        purchase_order.validation_note = "Invoice uploaded. Click Validate to compare against the PO."
        purchase_order.save(update_fields=["invoice_file", "status", "validation_note", "updated_at"])

    def _build_validation_result(
        self,
        purchase_order,
        invoice_items,
        *,
        invoice_net_total=None,
        invoice_tax_total=None,
        invoice_grand_total=None,
        apply_financial=False,
    ):
        validation_result = classify_invoice_against_purchase_order(purchase_order, invoice_items)
        validation_result["invoice_net_total"] = invoice_net_total
        validation_result["invoice_tax_total"] = invoice_tax_total
        validation_result["invoice_grand_total"] = invoice_grand_total
        validation_result["invoice_items"] = invoice_items

        if apply_financial or not validation_result.get("has_any_mismatch"):
            validation_result = apply_financial_validation(purchase_order, validation_result)

        return validation_result

    def _make_dt(self, year, month, day, hour=12, minute=0):
        return timezone.make_aware(
            datetime(year, month, day, hour, minute),
            timezone.get_current_timezone(),
        )

    def _create_fact_purchase_order(
        self,
        *,
        vendor=None,
        created_at,
        closed_at,
        status=PurchaseOrder.STATUS_CLOSED,
        original_po_total,
        final_net_total,
        final_tax_total,
        final_grand_total,
        invoice_net_total=None,
        invoice_tax_total=None,
        invoice_grand_total=None,
        po_item_count=1,
    ):
        vendor = vendor or self.vendor
        purchase_order = PurchaseOrder.objects.create(
            vendor=vendor,
            created_by=self.user,
            status=status,
            closed_at=closed_at,
            validated_at=closed_at,
        )
        PurchaseOrder.objects.filter(pk=purchase_order.pk).update(
            created_at=created_at,
            closed_at=closed_at,
            validated_at=closed_at,
            status=status,
        )
        purchase_order.refresh_from_db()

        return FactPurchaseOrder.objects.create(
            po=purchase_order,
            po_number=purchase_order.po_number,
            vendor=vendor,
            created_by=self.user,
            created_at=created_at,
            closed_at=closed_at,
            status=status,
            original_po_total=Decimal(str(original_po_total)),
            final_net_total=Decimal(str(final_net_total)),
            final_tax_total=Decimal(str(final_tax_total)),
            final_grand_total=Decimal(str(final_grand_total)),
            invoice_net_total=None if invoice_net_total is None else Decimal(str(invoice_net_total)),
            invoice_tax_total=None if invoice_tax_total is None else Decimal(str(invoice_tax_total)),
            invoice_grand_total=None if invoice_grand_total is None else Decimal(str(invoice_grand_total)),
            po_item_count=po_item_count,
            last_synced_at=closed_at or created_at,
        )

    def _create_fact_purchase_order_item(
        self,
        *,
        po,
        product,
        vendor=None,
        created_at,
        closed_at,
        po_quantity,
        po_unit_price,
        po_line_total,
        invoice_quantity,
        invoice_unit_price,
        invoice_line_total,
        accepted_quantity,
        accepted_unit_price,
        accepted_line_total,
        quantity_change_type=QuantityChangeType.NONE,
        price_change_type=PriceChangeType.NONE,
        name_reconciled_flag=False,
        quantity_reconciled_flag=False,
        price_reconciled_flag=False,
    ):
        vendor = vendor or po.vendor
        return FactPurchaseOrderItem.objects.create(
            po=po,
            po_number=po.po_number,
            vendor=vendor,
            product=product,
            product_display_name_snapshot=product.display_name or product.product_name,
            created_at=created_at,
            closed_at=closed_at,
            po_quantity=Decimal(str(po_quantity)),
            po_unit_price=Decimal(str(po_unit_price)),
            po_line_total=Decimal(str(po_line_total)),
            invoice_quantity=Decimal(str(invoice_quantity)),
            invoice_unit_price=Decimal(str(invoice_unit_price)),
            invoice_line_total=Decimal(str(invoice_line_total)),
            accepted_quantity=Decimal(str(accepted_quantity)),
            accepted_unit_price=Decimal(str(accepted_unit_price)),
            accepted_line_total=Decimal(str(accepted_line_total)),
            name_reconciled_flag=name_reconciled_flag,
            quantity_reconciled_flag=quantity_reconciled_flag,
            price_reconciled_flag=price_reconciled_flag,
            quantity_change_type=quantity_change_type,
            price_change_type=price_change_type,
            quantity_difference=Decimal(str(accepted_quantity)) - Decimal(str(po_quantity)),
            unit_price_difference=Decimal(str(accepted_unit_price)) - Decimal(str(po_unit_price)),
            line_total_difference=Decimal(str(accepted_line_total)) - Decimal(str(po_line_total)),
            last_synced_at=closed_at or created_at,
        )

    def _create_fact_price_change_event(
        self,
        *,
        po,
        product,
        vendor=None,
        changed_at,
        old_price,
        new_price,
        price_difference,
        price_change_percent,
        direction,
        accepted_flag=True,
    ):
        vendor = vendor or po.vendor
        return FactPriceChangeEvent.objects.create(
            po=po,
            po_number=po.po_number,
            vendor=vendor,
            product=product,
            changed_at=changed_at,
            old_price=Decimal(str(old_price)),
            new_price=Decimal(str(new_price)),
            price_difference=Decimal(str(price_difference)),
            price_change_percent=(
                None if price_change_percent is None else Decimal(str(price_change_percent))
            ),
            direction=direction,
            accepted_flag=accepted_flag,
            last_synced_at=changed_at,
        )

    def _create_price_history_entry(
        self,
        *,
        product,
        vendor=None,
        date_value,
        price,
        previous_price=None,
        new_price=None,
        change_source=PriceHistory.SOURCE_MASTER_INVENTORY,
    ):
        vendor = vendor or self.vendor
        entry = PriceHistory.objects.create(
            product=product,
            vendor=vendor,
            price=Decimal(str(price)),
            previous_price=(
                None if previous_price is None else Decimal(str(previous_price))
            ),
            new_price=None if new_price is None else Decimal(str(new_price)),
            change_source=change_source,
        )
        PriceHistory.objects.filter(pk=entry.pk).update(date=date_value)
        entry.refresh_from_db()
        return entry


class AnalyticsSyncTests(AnalyticsSyncTestCase):
    def test_successful_validation_creates_purchase_order_fact_and_derives_tax(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product)
        self._attach_invoice_file(purchase_order)

        validation_result = self._build_validation_result(
            purchase_order,
            [
                self._invoice_item(
                    name=product.display_name,
                    quantity=2,
                    unit_price="10.50",
                    amount="21.00",
                )
            ],
            invoice_net_total="21.00",
            invoice_tax_total=None,
            invoice_grand_total="25.00",
            apply_financial=True,
        )

        with patch("purchases.views.analyze_purchase_order_invoice", return_value=validation_result):
            response = self.client.post(reverse("purchases:validate_invoice", args=[purchase_order.id]))

        self.assertRedirects(response, reverse("purchases:po_list"))
        purchase_order.refresh_from_db()

        fact = FactPurchaseOrder.objects.get(po=purchase_order)
        self.assertEqual(purchase_order.status, PurchaseOrder.STATUS_CLOSED)
        self.assertEqual(fact.final_net_total, Decimal("21.00"))
        self.assertEqual(fact.invoice_net_total, Decimal("21.00"))
        self.assertEqual(fact.invoice_tax_total, Decimal("4.00"))
        self.assertEqual(fact.final_tax_total, Decimal("4.00"))
        self.assertEqual(fact.final_grand_total, Decimal("25.00"))
        self.assertTrue(fact.was_auto_validated)
        self.assertFalse(fact.was_manually_reconciled)

    def test_manual_shortage_reconciliation_syncs_final_values(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=3, unit_price="10.50")
        po_item = purchase_order.items.get()
        validation_result = self._build_validation_result(
            purchase_order,
            [
                self._invoice_item(
                    name=product.display_name,
                    quantity=2,
                    unit_price="10.50",
                    amount="21.00",
                )
            ],
            invoice_net_total="21.00",
            invoice_tax_total="0.00",
            invoice_grand_total="21.00",
        )
        self._set_validation_state(purchase_order, validation_result)

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={f"quantity_resolution_{po_item.id}": "shortage"},
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        fact = FactPurchaseOrder.objects.get(po=purchase_order)
        item_fact = FactPurchaseOrderItem.objects.get(po=purchase_order, product=product)

        self.assertEqual(fact.final_net_total, Decimal("21.00"))
        self.assertTrue(fact.has_quantity_reconciliation)
        self.assertEqual(item_fact.accepted_quantity, Decimal("2.00"))
        self.assertEqual(item_fact.accepted_line_total, Decimal("21.00"))
        self.assertEqual(item_fact.quantity_change_type, QuantityChangeType.SHORTAGE_CONFIRMED)
        self.assertEqual(item_fact.price_change_type, PriceChangeType.NONE)

    def test_extra_quantity_accepted_populates_item_fact(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=2, unit_price="10.50")
        po_item = purchase_order.items.get()
        validation_result = self._build_validation_result(
            purchase_order,
            [
                self._invoice_item(
                    name=product.display_name,
                    quantity=3,
                    unit_price="10.50",
                    amount="31.50",
                )
            ],
            invoice_net_total="31.50",
            invoice_tax_total="0.00",
            invoice_grand_total="31.50",
        )
        self._set_validation_state(purchase_order, validation_result)

        self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={f"quantity_resolution_{po_item.id}": "use_invoice_quantity"},
        )

        item_fact = FactPurchaseOrderItem.objects.get(po=purchase_order, product=product)
        self.assertEqual(item_fact.accepted_quantity, Decimal("3.00"))
        self.assertEqual(item_fact.accepted_line_total, Decimal("31.50"))
        self.assertEqual(item_fact.quantity_change_type, QuantityChangeType.EXTRA_QTY_ACCEPTED)

    def test_extra_quantity_rejected_populates_item_fact(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=2, unit_price="10.50")
        po_item = purchase_order.items.get()
        validation_result = self._build_validation_result(
            purchase_order,
            [
                self._invoice_item(
                    name=product.display_name,
                    quantity=3,
                    unit_price="10.50",
                    amount="31.50",
                )
            ],
            invoice_net_total="31.50",
            invoice_tax_total="0.00",
            invoice_grand_total="31.50",
        )
        self._set_validation_state(purchase_order, validation_result)

        self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={f"quantity_resolution_{po_item.id}": "use_po_quantity"},
        )

        item_fact = FactPurchaseOrderItem.objects.get(po=purchase_order, product=product)
        po_fact = FactPurchaseOrder.objects.get(po=purchase_order)
        self.assertEqual(item_fact.accepted_quantity, Decimal("2.00"))
        self.assertEqual(item_fact.accepted_line_total, Decimal("21.00"))
        self.assertEqual(item_fact.quantity_change_type, QuantityChangeType.EXTRA_QTY_REJECTED)
        self.assertEqual(po_fact.final_net_total, Decimal("21.00"))
        self.assertEqual(po_fact.invoice_net_total, Decimal("31.50"))

    def test_confirm_price_update_creates_item_fact_and_price_event(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=2, unit_price="10.50")
        VendorProductPrice.objects.create(
            vendor=self.vendor,
            product=product,
            price=Decimal("10.50"),
            currency="EUR",
            is_active=True,
        )
        validation_result = self._build_validation_result(
            purchase_order,
            [
                self._invoice_item(
                    name=product.display_name,
                    quantity=2,
                    unit_price="11.00",
                    amount="22.00",
                )
            ],
            invoice_net_total="22.00",
            invoice_tax_total="0.00",
            invoice_grand_total="22.00",
        )
        validation_result["can_update_prices"] = True
        self._set_validation_state(purchase_order, validation_result)

        response = self.client.post(reverse("purchases:confirm_price_update", args=[purchase_order.id]))

        self.assertRedirects(response, reverse("purchases:history_list"))
        item_fact = FactPurchaseOrderItem.objects.get(po=purchase_order, product=product)
        price_event = FactPriceChangeEvent.objects.get(po=purchase_order, product=product)

        self.assertEqual(item_fact.accepted_unit_price, Decimal("11.00"))
        self.assertEqual(item_fact.price_change_type, PriceChangeType.PRICE_INCREASE_ACCEPTED)
        self.assertEqual(price_event.old_price, Decimal("10.50"))
        self.assertEqual(price_event.new_price, Decimal("11.00"))
        self.assertEqual(price_event.direction, PriceDirection.INCREASE)

    def test_rejected_price_change_keeps_old_price_and_creates_no_event(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=2, unit_price="10.50")
        po_item = purchase_order.items.get()
        VendorProductPrice.objects.create(
            vendor=self.vendor,
            product=product,
            price=Decimal("10.50"),
            currency="EUR",
            is_active=True,
        )
        validation_result = self._build_validation_result(
            purchase_order,
            [
                self._invoice_item(
                    name=product.display_name,
                    quantity=2,
                    unit_price="11.00",
                    amount="22.00",
                )
            ],
            invoice_net_total="22.00",
            invoice_tax_total="0.00",
            invoice_grand_total="22.00",
        )
        validation_result["can_update_prices"] = True
        self._set_validation_state(purchase_order, validation_result)

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={f"price_decision_{po_item.id}": "keep_old"},
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        item_fact = FactPurchaseOrderItem.objects.get(po=purchase_order, product=product)

        self.assertEqual(item_fact.accepted_unit_price, Decimal("10.50"))
        self.assertEqual(item_fact.price_change_type, PriceChangeType.INVOICE_PRICE_REJECTED)
        self.assertFalse(FactPriceChangeEvent.objects.filter(po=purchase_order).exists())

    def test_manual_name_mapping_sets_name_reconciled_flag(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=2, unit_price="10.50")
        validation_result = self._build_validation_result(
            purchase_order,
            [
                self._invoice_item(
                    name="Basmty",
                    quantity=2,
                    unit_price="10.50",
                    amount="21.00",
                )
            ],
            invoice_net_total="21.00",
            invoice_tax_total="0.00",
            invoice_grand_total="21.00",
        )
        self._set_validation_state(purchase_order, validation_result)

        self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                "map_product_0": str(product.id),
                "save_alias_0": "on",
            },
        )

        item_fact = FactPurchaseOrderItem.objects.get(po=purchase_order, product=product)
        po_fact = FactPurchaseOrder.objects.get(po=purchase_order)
        self.assertTrue(item_fact.name_reconciled_flag)
        self.assertTrue(po_fact.has_name_reconciliation)

    def test_sync_all_po_analytics_is_idempotent(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=2, unit_price="10.50")
        VendorProductPrice.objects.create(
            vendor=self.vendor,
            product=product,
            price=Decimal("10.50"),
            currency="EUR",
            is_active=True,
        )
        validation_result = self._build_validation_result(
            purchase_order,
            [
                self._invoice_item(
                    name=product.display_name,
                    quantity=2,
                    unit_price="11.00",
                    amount="22.00",
                )
            ],
            invoice_net_total="22.00",
            invoice_tax_total="0.00",
            invoice_grand_total="22.00",
        )
        validation_result["can_update_prices"] = True
        self._set_validation_state(purchase_order, validation_result)

        response = self.client.post(reverse("purchases:confirm_price_update", args=[purchase_order.id]))
        self.assertRedirects(response, reverse("purchases:history_list"))
        purchase_order.refresh_from_db()
        sync_all_po_analytics(purchase_order)
        sync_all_po_analytics(purchase_order)

        self.assertEqual(FactPurchaseOrder.objects.filter(po=purchase_order).count(), 1)
        self.assertEqual(FactPurchaseOrderItem.objects.filter(po=purchase_order).count(), 1)
        self.assertEqual(FactPriceChangeEvent.objects.filter(po=purchase_order).count(), 1)

    def test_sync_does_not_require_removed_calculation_logic(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=2, unit_price="10.50")
        po_item = purchase_order.items.get()
        validation_result = self._build_validation_result(
            purchase_order,
            [
                self._invoice_item(
                    name=product.display_name,
                    quantity=3,
                    unit_price="11.00",
                    amount="40.00",
                )
            ],
            invoice_net_total="40.00",
            invoice_tax_total="0.00",
            invoice_grand_total="40.00",
        )
        self._set_validation_state(purchase_order, validation_result)

        response = self.client.post(
            reverse("purchases:manual_validate_po", args=[purchase_order.id]),
            data={
                f"quantity_resolution_{po_item.id}": "use_invoice_quantity",
                f"price_decision_{po_item.id}": "accepted",
            },
        )

        self.assertRedirects(response, reverse("purchases:history_list"))
        po_fact = FactPurchaseOrder.objects.get(po=purchase_order)
        item_fact = FactPurchaseOrderItem.objects.get(po=purchase_order, product=product)

        self.assertEqual(po_fact.final_net_total, Decimal("33.00"))
        self.assertEqual(po_fact.invoice_net_total, Decimal("40.00"))
        self.assertEqual(item_fact.accepted_line_total, Decimal("33.00"))

    def test_manual_close_without_final_reconciled_data_skips_sync_safely(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product, quantity=3, unit_price="10.50")
        validation_result = self._build_validation_result(
            purchase_order,
            [
                self._invoice_item(
                    name=product.display_name,
                    quantity=2,
                    unit_price="10.50",
                    amount="21.00",
                )
            ],
            invoice_net_total="21.00",
            invoice_tax_total="0.00",
            invoice_grand_total="21.00",
        )
        self._set_validation_state(purchase_order, validation_result)

        response = self.client.post(reverse("purchases:close_po_manually", args=[purchase_order.id]))

        self.assertRedirects(response, reverse("purchases:history_list"))
        self.assertFalse(FactPurchaseOrder.objects.filter(po=purchase_order).exists())
        self.assertFalse(FactPurchaseOrderItem.objects.filter(po=purchase_order).exists())
        self.assertFalse(FactPriceChangeEvent.objects.filter(po=purchase_order).exists())


class AnalyticsSelectorTests(AnalyticsSyncTestCase):
    def setUp(self):
        super().setUp()
        self.reference_dt = self._make_dt(2026, 8, 19, 15, 30)
        self.vendor_b = Vendor.objects.create(
            name="Beta Supplies",
            email="beta@example.com",
            whatsapp_number="9999999999",
        )
        self.product_a = self._create_product()
        self.product_b = self._create_product(
            product_name="Beans",
            display_name="Beans 10 Kg",
            quantity_per_pack="10",
        )
        self.product_c = self._create_product(
            product_name="Sugar",
            display_name="Sugar bag 25 Kg",
            quantity_per_pack="25",
        )
        self.inactive_product = Product.objects.create(
            product_name="Inactive item",
            pack_type="bag",
            quantity_per_pack="1",
            quantity_unit="Kg",
            display_name="Inactive item 1 Kg",
            is_active=False,
        )

        self.fact_po_1 = self._create_fact_purchase_order(
            created_at=self._make_dt(2026, 8, 10, 9, 0),
            closed_at=self._make_dt(2026, 8, 10, 11, 0),
            original_po_total="100.00",
            final_net_total="100.00",
            final_tax_total="20.00",
            final_grand_total="120.00",
            invoice_net_total="100.00",
            invoice_tax_total="20.00",
            invoice_grand_total="120.00",
        )
        self._create_fact_purchase_order_item(
            po=self.fact_po_1.po,
            product=self.product_a,
            created_at=self.fact_po_1.created_at,
            closed_at=self.fact_po_1.closed_at,
            po_quantity="10.00",
            po_unit_price="10.00",
            po_line_total="100.00",
            invoice_quantity="10.00",
            invoice_unit_price="10.00",
            invoice_line_total="100.00",
            accepted_quantity="10.00",
            accepted_unit_price="10.00",
            accepted_line_total="100.00",
        )

        self.fact_po_2 = self._create_fact_purchase_order(
            created_at=self._make_dt(2026, 8, 15, 9, 0),
            closed_at=self._make_dt(2026, 8, 15, 12, 0),
            original_po_total="50.00",
            final_net_total="50.00",
            final_tax_total="10.00",
            final_grand_total="60.00",
            invoice_net_total="50.00",
            invoice_tax_total="10.00",
            invoice_grand_total="60.00",
        )
        self._create_fact_purchase_order_item(
            po=self.fact_po_2.po,
            product=self.product_a,
            created_at=self.fact_po_2.created_at,
            closed_at=self.fact_po_2.closed_at,
            po_quantity="5.00",
            po_unit_price="10.00",
            po_line_total="50.00",
            invoice_quantity="5.00",
            invoice_unit_price="10.00",
            invoice_line_total="50.00",
            accepted_quantity="5.00",
            accepted_unit_price="10.00",
            accepted_line_total="50.00",
        )

        self.fact_po_3 = self._create_fact_purchase_order(
            vendor=self.vendor_b,
            created_at=self._make_dt(2026, 8, 18, 8, 0),
            closed_at=self._make_dt(2026, 8, 18, 16, 0),
            original_po_total="80.00",
            final_net_total="80.00",
            final_tax_total="16.00",
            final_grand_total="96.00",
            invoice_net_total="80.00",
            invoice_tax_total="16.00",
            invoice_grand_total="96.00",
        )
        self._create_fact_purchase_order_item(
            po=self.fact_po_3.po,
            product=self.product_b,
            vendor=self.vendor_b,
            created_at=self.fact_po_3.created_at,
            closed_at=self.fact_po_3.closed_at,
            po_quantity="8.00",
            po_unit_price="10.00",
            po_line_total="80.00",
            invoice_quantity="8.00",
            invoice_unit_price="10.00",
            invoice_line_total="80.00",
            accepted_quantity="8.00",
            accepted_unit_price="10.00",
            accepted_line_total="80.00",
        )

        self.fact_po_4 = self._create_fact_purchase_order(
            created_at=self._make_dt(2026, 8, 5, 10, 0),
            closed_at=None,
            original_po_total="40.00",
            final_net_total="40.00",
            final_tax_total="8.00",
            final_grand_total="48.00",
            invoice_net_total="40.00",
            invoice_tax_total="8.00",
            invoice_grand_total="48.00",
        )
        self._create_fact_purchase_order_item(
            po=self.fact_po_4.po,
            product=self.product_c,
            created_at=self.fact_po_4.created_at,
            closed_at=self.fact_po_4.closed_at,
            po_quantity="4.00",
            po_unit_price="10.00",
            po_line_total="40.00",
            invoice_quantity="4.00",
            invoice_unit_price="10.00",
            invoice_line_total="40.00",
            accepted_quantity="4.00",
            accepted_unit_price="10.00",
            accepted_line_total="40.00",
        )

        self.fact_po_5 = self._create_fact_purchase_order(
            vendor=self.vendor_b,
            created_at=self._make_dt(2026, 7, 5, 9, 0),
            closed_at=self._make_dt(2026, 7, 5, 11, 0),
            original_po_total="30.00",
            final_net_total="30.00",
            final_tax_total="6.00",
            final_grand_total="36.00",
            invoice_net_total="30.00",
            invoice_tax_total="6.00",
            invoice_grand_total="36.00",
        )
        self._create_fact_purchase_order_item(
            po=self.fact_po_5.po,
            product=self.product_b,
            vendor=self.vendor_b,
            created_at=self.fact_po_5.created_at,
            closed_at=self.fact_po_5.closed_at,
            po_quantity="3.00",
            po_unit_price="10.00",
            po_line_total="30.00",
            invoice_quantity="3.00",
            invoice_unit_price="10.00",
            invoice_line_total="30.00",
            accepted_quantity="3.00",
            accepted_unit_price="10.00",
            accepted_line_total="30.00",
        )

        self._create_fact_price_change_event(
            po=self.fact_po_1.po,
            product=self.product_a,
            changed_at=self._make_dt(2026, 8, 10, 11, 0),
            old_price="5.00",
            new_price="5.50",
            price_difference="0.50",
            price_change_percent="10.00",
            direction=PriceDirection.INCREASE,
        )
        self._create_fact_price_change_event(
            po=self.fact_po_2.po,
            product=self.product_a,
            changed_at=self._make_dt(2026, 8, 15, 12, 0),
            old_price="5.50",
            new_price="6.00",
            price_difference="0.50",
            price_change_percent="9.09",
            direction=PriceDirection.INCREASE,
        )
        self._create_fact_price_change_event(
            po=self.fact_po_3.po,
            product=self.product_b,
            vendor=self.vendor_b,
            changed_at=self._make_dt(2026, 8, 18, 16, 0),
            old_price="12.00",
            new_price="10.00",
            price_difference="-2.00",
            price_change_percent="-16.67",
            direction=PriceDirection.DECREASE,
        )
        self._create_fact_price_change_event(
            po=self.fact_po_4.po,
            product=self.product_c,
            changed_at=self._make_dt(2026, 8, 12, 10, 0),
            old_price="7.00",
            new_price="8.00",
            price_difference="1.00",
            price_change_percent="14.29",
            direction=PriceDirection.INCREASE,
            accepted_flag=False,
        )

        self._create_price_history_entry(
            product=self.product_a,
            vendor=self.vendor,
            date_value=self._make_dt(2026, 8, 1, 9, 0).date(),
            price="4.75",
            previous_price="4.50",
            new_price="4.75",
            change_source=PriceHistory.SOURCE_MASTER_INVENTORY,
        )
        self._create_price_history_entry(
            product=self.product_a,
            vendor=self.vendor,
            date_value=self._make_dt(2026, 8, 10, 11, 0).date(),
            price="5.50",
            previous_price="5.00",
            new_price="5.50",
            change_source=PriceHistory.SOURCE_INVOICE_VALIDATION,
        )
        self._create_price_history_entry(
            product=self.product_a,
            vendor=self.vendor,
            date_value=self._make_dt(2026, 8, 15, 12, 0).date(),
            price="6.00",
            previous_price="5.50",
            new_price="6.00",
            change_source=PriceHistory.SOURCE_INVOICE_VALIDATION,
        )
        self._create_price_history_entry(
            product=self.product_b,
            vendor=self.vendor_b,
            date_value=self._make_dt(2026, 7, 5, 11, 0).date(),
            price="12.00",
            previous_price="11.50",
            new_price="12.00",
            change_source=PriceHistory.SOURCE_MASTER_INVENTORY,
        )
        self._create_price_history_entry(
            product=self.product_b,
            vendor=self.vendor_b,
            date_value=self._make_dt(2026, 8, 18, 16, 0).date(),
            price="10.00",
            previous_price="12.00",
            new_price="10.00",
            change_source=PriceHistory.SOURCE_INVOICE_VALIDATION,
        )

    def test_resolve_period_range_supports_expected_periods(self):
        expected_starts = {
            "weekly": self._make_dt(2026, 8, 17, 0, 0),
            "monthly": self._make_dt(2026, 8, 1, 0, 0),
            "quarterly": self._make_dt(2026, 7, 1, 0, 0),
            "half_year": self._make_dt(2026, 7, 1, 0, 0),
            "yearly": self._make_dt(2026, 1, 1, 0, 0),
        }

        for period, expected_start in expected_starts.items():
            resolved = resolve_period_range(period, reference=self.reference_dt)
            self.assertEqual(resolved["date_from"], expected_start)
            self.assertEqual(resolved["date_to"], self.reference_dt)

    def test_total_spend_metrics_return_correct_values(self):
        metrics = get_total_spend_metrics("monthly", reference=self.reference_dt)

        self.assertEqual(metrics["gross_spend"], Decimal("324.00"))
        self.assertEqual(metrics["net_spend"], Decimal("270.00"))
        self.assertEqual(metrics["tax_spend"], Decimal("54.00"))

    def test_average_po_value_returns_correct_value(self):
        metrics = get_average_po_value("monthly", reference=self.reference_dt)

        self.assertEqual(metrics["average_po_value"], Decimal("81.00"))

    def test_total_products_count_uses_active_products(self):
        metrics = get_total_products_count()

        self.assertEqual(metrics["active_product_count"], 3)

    def test_total_po_count_uses_period_filter_and_created_at_fallback(self):
        metrics = get_total_po_count("monthly", reference=self.reference_dt)

        self.assertEqual(metrics["po_count"], 4)

    def test_vendor_wise_spend_groups_correctly(self):
        rows = get_vendor_wise_spend("monthly", reference=self.reference_dt)

        self.assertEqual(
            rows,
            [
                {
                    "vendor_id": self.vendor.id,
                    "vendor_name": self.vendor.name,
                    "total_spend": Decimal("228.00"),
                },
                {
                    "vendor_id": self.vendor_b.id,
                    "vendor_name": self.vendor_b.name,
                    "total_spend": Decimal("96.00"),
                },
            ],
        )

    def test_vendor_order_frequency_groups_correctly(self):
        rows = get_vendor_order_frequency("monthly", reference=self.reference_dt)

        self.assertEqual(
            rows,
            [
                {
                    "vendor_id": self.vendor.id,
                    "vendor_name": self.vendor.name,
                    "po_count": 3,
                },
                {
                    "vendor_id": self.vendor_b.id,
                    "vendor_name": self.vendor_b.name,
                    "po_count": 1,
                },
            ],
        )

    def test_vendor_order_frequency_excludes_zero_spend_purchase_orders(self):
        vendor_zero = Vendor.objects.create(
            name="Zero Spend Vendor",
            email="zero-spend@example.com",
            whatsapp_number="9999999999",
        )
        self._create_fact_purchase_order(
            vendor=vendor_zero,
            created_at=self._make_dt(2026, 8, 20, 10, 0),
            closed_at=self._make_dt(2026, 8, 20, 12, 0),
            original_po_total="0.00",
            final_net_total="0.00",
            final_tax_total="0.00",
            final_grand_total="0.00",
            invoice_net_total="0.00",
            invoice_tax_total="0.00",
            invoice_grand_total="0.00",
        )

        rows = get_vendor_order_frequency("monthly", reference=self.reference_dt)

        self.assertEqual([row["vendor_name"] for row in rows], [self.vendor.name, self.vendor_b.name])
        self.assertNotIn(vendor_zero.id, [row["vendor_id"] for row in rows])

    def test_top_products_by_quantity_uses_accepted_quantity(self):
        rows = get_top_products("monthly", by="quantity", reference=self.reference_dt)

        self.assertEqual(
            rows[:3],
            [
                {
                    "product_id": self.product_a.id,
                    "product_name": self.product_a.display_name,
                    "metric_value": Decimal("15.00"),
                },
                {
                    "product_id": self.product_b.id,
                    "product_name": self.product_b.display_name,
                    "metric_value": Decimal("8.00"),
                },
                {
                    "product_id": self.product_c.id,
                    "product_name": self.product_c.display_name,
                    "metric_value": Decimal("4.00"),
                },
            ],
        )

    def test_top_products_can_filter_by_vendor(self):
        rows = get_top_products(
            "monthly",
            by="quantity",
            reference=self.reference_dt,
            vendor_id=self.vendor_b.id,
        )

        self.assertEqual(
            rows,
            [
                {
                    "product_id": self.product_b.id,
                    "product_name": self.product_b.display_name,
                    "metric_value": Decimal("8.00"),
                }
            ],
        )

    def test_top_products_by_value_uses_accepted_line_total(self):
        rows = get_top_products("monthly", by="value", reference=self.reference_dt)

        self.assertEqual(
            rows[:3],
            [
                {
                    "product_id": self.product_a.id,
                    "product_name": self.product_a.display_name,
                    "metric_value": Decimal("150.00"),
                },
                {
                    "product_id": self.product_b.id,
                    "product_name": self.product_b.display_name,
                    "metric_value": Decimal("80.00"),
                },
                {
                    "product_id": self.product_c.id,
                    "product_name": self.product_c.display_name,
                    "metric_value": Decimal("40.00"),
                },
            ],
        )

    def test_frequent_price_changes_uses_only_accepted_events(self):
        rows = get_frequent_price_changes("monthly", reference=self.reference_dt)

        self.assertEqual(len(rows), 2)
        self.assertEqual(
            rows[0],
            {
                "product_id": self.product_a.id,
                "product_name": self.product_a.display_name,
                "vendor_id": self.vendor.id,
                "vendor_name": self.vendor.name,
                "change_count": 2,
                "latest_old_price": Decimal("5.50"),
                "latest_new_price": Decimal("6.00"),
                "latest_difference": Decimal("0.50"),
                "latest_percent": Decimal("9.09"),
                "direction": PriceDirection.INCREASE,
            },
        )
        self.assertEqual(rows[1]["product_id"], self.product_b.id)
        self.assertEqual(rows[1]["direction"], PriceDirection.DECREASE)

    def test_products_with_price_trend_returns_products_with_price_history(self):
        rows = get_products_with_price_trend()

        self.assertEqual(
            rows,
            [
                {
                    "product_id": self.product_b.id,
                    "product_name": self.product_b.display_name,
                },
                {
                    "product_id": self.product_a.id,
                    "product_name": self.product_a.display_name,
                },
            ],
        )

    def test_product_price_trend_matches_price_tracker_history_for_single_vendor(self):
        trend = get_product_price_trend(self.product_a.id)

        self.assertEqual(trend["product_id"], self.product_a.id)
        self.assertEqual(trend["product_name"], self.product_a.display_name)
        self.assertEqual(trend["labels"], ["2026-08-01", "2026-08-10", "2026-08-15"])
        self.assertEqual(len(trend["datasets"]), 1)
        self.assertEqual(
            trend["datasets"][0],
            {
                "vendor_id": self.vendor.id,
                "vendor_name": self.vendor.name,
                "prices": [Decimal("4.75"), Decimal("5.50"), Decimal("6.00")],
                "old_prices": [Decimal("4.50"), Decimal("5.00"), Decimal("5.50")],
                "new_prices": [Decimal("4.75"), Decimal("5.50"), Decimal("6.00")],
            },
        )

    def test_product_price_trend_multi_vendor_returns_multiple_lines(self):
        self._create_price_history_entry(
            product=self.product_a,
            vendor=self.vendor_b,
            date_value=self._make_dt(2026, 8, 18, 16, 30).date(),
            price="6.20",
            previous_price="6.00",
            new_price="6.20",
            change_source=PriceHistory.SOURCE_INVOICE_VALIDATION,
        )

        trend = get_product_price_trend(self.product_a.id)

        self.assertEqual(len(trend["datasets"]), 2)
        self.assertEqual(trend["labels"], ["2026-08-01", "2026-08-10", "2026-08-15", "2026-08-18"])
        self.assertEqual(trend["datasets"][0]["vendor_name"], self.vendor.name)
        self.assertEqual(
            trend["datasets"][0]["prices"],
            [Decimal("4.75"), Decimal("5.50"), Decimal("6.00"), None],
        )
        self.assertEqual(trend["datasets"][1]["vendor_name"], self.vendor_b.name)
        self.assertEqual(trend["datasets"][1]["prices"], [None, None, None, Decimal("6.20")])

    def test_product_price_trend_can_filter_price_history_by_period_when_requested(self):
        trend = get_product_price_trend(self.product_b.id, "monthly", reference=self.reference_dt)

        self.assertEqual(trend["labels"], ["2026-08-18"])
        self.assertEqual(trend["datasets"][0]["prices"], [Decimal("10.00")])

    def test_vendor_price_change_movement_uses_signed_net_and_absolute_movement(self):
        rows = get_vendor_price_change_movement_by_vendor("monthly", reference=self.reference_dt)

        self.assertEqual(
            rows,
            [
                {
                    "vendor_id": self.vendor_b.id,
                    "vendor_name": self.vendor_b.name,
                    "increase_count": 0,
                    "decrease_count": 1,
                    "net_impact": Decimal("-16.00"),
                    "total_movement": Decimal("16.00"),
                    "movement_share_percent": Decimal("68.09"),
                },
                {
                    "vendor_id": self.vendor.id,
                    "vendor_name": self.vendor.name,
                    "increase_count": 2,
                    "decrease_count": 0,
                    "net_impact": Decimal("7.50"),
                    "total_movement": Decimal("7.50"),
                    "movement_share_percent": Decimal("31.91"),
                },
            ],
        )

    def test_vendor_price_change_movement_uses_old_and_new_prices_not_stored_difference(self):
        event = FactPriceChangeEvent.objects.get(po=self.fact_po_1.po, product=self.product_a)
        event.price_difference = Decimal("99.99")
        event.save(update_fields=["price_difference"])

        rows = get_vendor_price_change_movement_by_vendor("monthly", reference=self.reference_dt)

        self.assertEqual(
            rows[1],
            {
                "vendor_id": self.vendor.id,
                "vendor_name": self.vendor.name,
                "increase_count": 2,
                "decrease_count": 0,
                "net_impact": Decimal("7.50"),
                "total_movement": Decimal("7.50"),
                "movement_share_percent": Decimal("31.91"),
            },
        )

    def test_vendor_price_change_movement_aggregates_by_vendor_and_share(self):
        self._create_fact_price_change_event(
            po=self.fact_po_3.po,
            product=self.product_b,
            vendor=self.vendor_b,
            changed_at=self._make_dt(2026, 8, 18, 17, 0),
            old_price="10.00",
            new_price="11.00",
            price_difference="1.00",
            price_change_percent="10.00",
            direction=PriceDirection.INCREASE,
        )

        rows = get_vendor_price_change_movement_by_vendor("monthly", reference=self.reference_dt)

        self.assertEqual(
            rows,
            [
                {
                    "vendor_id": self.vendor_b.id,
                    "vendor_name": self.vendor_b.name,
                    "increase_count": 1,
                    "decrease_count": 1,
                    "net_impact": Decimal("-8.00"),
                    "total_movement": Decimal("24.00"),
                    "movement_share_percent": Decimal("76.19"),
                },
                {
                    "vendor_id": self.vendor.id,
                    "vendor_name": self.vendor.name,
                    "increase_count": 2,
                    "decrease_count": 0,
                    "net_impact": Decimal("7.50"),
                    "total_movement": Decimal("7.50"),
                    "movement_share_percent": Decimal("23.81"),
                },
            ],
        )

    def test_vendor_price_change_movement_respects_selected_period(self):
        self._create_fact_price_change_event(
            po=self.fact_po_5.po,
            product=self.product_b,
            vendor=self.vendor_b,
            changed_at=self._make_dt(2026, 7, 5, 11, 30),
            old_price="9.00",
            new_price="10.00",
            price_difference="1.00",
            price_change_percent="11.11",
            direction=PriceDirection.INCREASE,
        )

        monthly_rows = get_vendor_price_change_movement_by_vendor("monthly", reference=self.reference_dt)
        quarterly_rows = get_vendor_price_change_movement_by_vendor("quarterly", reference=self.reference_dt)

        self.assertEqual([row["vendor_id"] for row in monthly_rows], [self.vendor_b.id, self.vendor.id])
        self.assertEqual(
            quarterly_rows,
            [
                {
                    "vendor_id": self.vendor_b.id,
                    "vendor_name": self.vendor_b.name,
                    "increase_count": 1,
                    "decrease_count": 1,
                    "net_impact": Decimal("-13.00"),
                    "total_movement": Decimal("19.00"),
                    "movement_share_percent": Decimal("71.70"),
                },
                {
                    "vendor_id": self.vendor.id,
                    "vendor_name": self.vendor.name,
                    "increase_count": 2,
                    "decrease_count": 0,
                    "net_impact": Decimal("7.50"),
                    "total_movement": Decimal("7.50"),
                    "movement_share_percent": Decimal("28.30"),
                },
            ],
        )


class AnalyticsDashboardViewTests(AnalyticsSyncTestCase):
    def _selector_patches(self):
        return patch.multiple(
            "analytics.views",
            get_total_spend_metrics=DEFAULT,
            get_average_po_value=DEFAULT,
            get_total_products_count=DEFAULT,
            get_total_po_count=DEFAULT,
            get_vendor_wise_spend=DEFAULT,
            get_vendor_order_frequency=DEFAULT,
            get_top_products=DEFAULT,
            get_products_with_price_trend=DEFAULT,
            get_product_price_trend=DEFAULT,
            get_vendor_price_change_movement_by_vendor=DEFAULT,
        )

    def _mock_dashboard_selector_returns(self, mocks):
        mocks["get_total_spend_metrics"].return_value = {
            "gross_spend": Decimal("324.00"),
            "net_spend": Decimal("270.00"),
            "tax_spend": Decimal("54.00"),
        }
        mocks["get_average_po_value"].return_value = {
            "average_po_value": Decimal("81.00"),
        }
        mocks["get_total_products_count"].return_value = {
            "active_product_count": 12,
        }
        mocks["get_total_po_count"].return_value = {
            "po_count": 9,
        }
        mocks["get_vendor_wise_spend"].return_value = [
            {
                "vendor_id": 1,
                "vendor_name": "Krishna Supplies",
                "total_spend": Decimal("228.00"),
            }
        ]
        mocks["get_vendor_order_frequency"].return_value = [
            {
                "vendor_id": 1,
                "vendor_name": "Krishna Supplies",
                "po_count": 3,
            }
        ]

        def top_products_side_effect(period, by="quantity", vendor_id=None):
            if by == "quantity" and vendor_id is None:
                return [
                    {
                        "product_id": 11,
                        "product_name": "Rice bag 20 Kg",
                        "metric_value": Decimal("15.00"),
                    }
                ]
            if by == "value" and vendor_id is None:
                return [
                    {
                        "product_id": 11,
                        "product_name": "Rice bag 20 Kg",
                        "metric_value": Decimal("150.00"),
                    }
                ]
            return []

        mocks["get_top_products"].side_effect = top_products_side_effect
        mocks["get_products_with_price_trend"].return_value = [
            {
                "product_id": 11,
                "product_name": "Rice bag 20 Kg",
            },
            {
                "product_id": 12,
                "product_name": "Beans 10 Kg",
            }
        ]
        mocks["get_product_price_trend"].return_value = {
            "product_id": 11,
            "product_name": "Rice bag 20 Kg",
            "labels": ["2026-08-10", "2026-08-15"],
            "datasets": [
                {
                    "vendor_id": 1,
                    "vendor_name": "Krishna Supplies",
                    "prices": [Decimal("5.50"), Decimal("6.00")],
                    "old_prices": [Decimal("5.00"), Decimal("5.50")],
                    "new_prices": [Decimal("5.50"), Decimal("6.00")],
                }
            ],
        }
        mocks["get_vendor_price_change_movement_by_vendor"].return_value = [
            {
                "vendor_id": 1,
                "vendor_name": "Krishna Supplies",
                "increase_count": 2,
                "decrease_count": 1,
                "net_impact": Decimal("4.00"),
                "total_movement": Decimal("10.00"),
                "movement_share_percent": Decimal("100.00"),
            }
        ]

    def test_dashboard_view_loads_successfully_with_default_period(self):
        with self._selector_patches() as mocks:
            self._mock_dashboard_selector_returns(mocks)

            response = self.client.get(reverse("analytics:analytics_dashboard"))

        self.assertEqual(response.status_code, 200)
        mocks["get_total_spend_metrics"].assert_called_once_with("monthly")
        mocks["get_average_po_value"].assert_called_once_with("monthly")
        mocks["get_total_products_count"].assert_called_once_with()
        mocks["get_total_po_count"].assert_called_once_with("monthly")
        mocks["get_vendor_wise_spend"].assert_called_once_with("monthly")
        mocks["get_vendor_order_frequency"].assert_called_once_with("monthly")
        self.assertEqual(
            mocks["get_top_products"].call_args_list,
            [
                call("monthly", by="quantity"),
                call("monthly", by="value"),
            ],
        )
        mocks["get_products_with_price_trend"].assert_called_once_with()
        mocks["get_product_price_trend"].assert_called_once_with(11)
        mocks["get_vendor_price_change_movement_by_vendor"].assert_called_once_with("monthly")
        self.assertContains(response, "Analytics Dashboard")
        self.assertContains(response, "Total Spend")
        self.assertContains(response, "Monthly")
        self.assertEqual(response.context["selected_period"], "monthly")
        self.assertEqual(response.context["selected_trend_product_id"], 11)

    def test_dashboard_view_uses_requested_period(self):
        with self._selector_patches() as mocks:
            self._mock_dashboard_selector_returns(mocks)

            response = self.client.get(
                reverse("analytics:analytics_dashboard"),
                {"period": "quarterly"},
            )

        self.assertEqual(response.status_code, 200)
        mocks["get_total_spend_metrics"].assert_called_once_with("quarterly")
        mocks["get_average_po_value"].assert_called_once_with("quarterly")
        mocks["get_total_po_count"].assert_called_once_with("quarterly")
        mocks["get_vendor_wise_spend"].assert_called_once_with("quarterly")
        mocks["get_vendor_order_frequency"].assert_called_once_with("quarterly")
        self.assertEqual(
            mocks["get_top_products"].call_args_list,
            [
                call("quarterly", by="quantity"),
                call("quarterly", by="value"),
            ],
        )
        mocks["get_products_with_price_trend"].assert_called_once_with()
        mocks["get_product_price_trend"].assert_called_once_with(11)
        mocks["get_vendor_price_change_movement_by_vendor"].assert_called_once_with("quarterly")
        self.assertEqual(response.context["selected_period"], "quarterly")
        self.assertContains(response, "Quarterly")

    def test_dashboard_view_uses_selected_trend_product(self):
        with self._selector_patches() as mocks:
            self._mock_dashboard_selector_returns(mocks)

            response = self.client.get(
                reverse("analytics:analytics_dashboard"),
                {"period": "monthly", "trend_product": "12"},
            )

        self.assertEqual(response.status_code, 200)
        mocks["get_product_price_trend"].assert_called_once_with(12)
        self.assertEqual(response.context["selected_trend_product_id"], 12)

    def test_product_price_trend_data_view_returns_json_for_selected_product(self):
        with self._selector_patches() as mocks:
            self._mock_dashboard_selector_returns(mocks)
            mocks["get_product_price_trend"].return_value = {
                "product_id": 12,
                "product_name": "Beans 10 Kg",
                "labels": ["2026-08-09", "2026-08-16"],
                "datasets": [
                    {
                        "vendor_id": 2,
                        "vendor_name": "Beta Supplies",
                        "prices": [9.5, 10.0],
                        "old_prices": [9.0, 9.5],
                        "new_prices": [9.5, 10.0],
                    }
                ],
            }

            response = self.client.get(
                reverse("analytics:product_price_trend_data"),
                {"trend_product": "12"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "selected_trend_product_id": 12,
                "product_price_trend_chart": {
                    "product_id": 12,
                    "product_name": "Beans 10 Kg",
                    "labels": ["2026-08-09", "2026-08-16"],
                    "datasets": [
                        {
                            "vendor_id": 2,
                            "vendor_name": "Beta Supplies",
                            "prices": [9.5, 10.0],
                            "old_prices": [9.0, 9.5],
                            "new_prices": [9.5, 10.0],
                        }
                    ],
                },
                "product_price_trend_has_data": True,
                "product_price_trend_empty_message": "No price history available for this product.",
            },
        )
        mocks["get_products_with_price_trend"].assert_called_once_with()
        mocks["get_product_price_trend"].assert_called_once_with(12)

    def test_dashboard_view_renders_selector_data_into_cards_charts_and_trend_visual(self):
        with self._selector_patches() as mocks:
            self._mock_dashboard_selector_returns(mocks)
            mocks["get_vendor_order_frequency"].return_value = [
                {
                    "vendor_id": 2,
                    "vendor_name": "Beta Supplies",
                    "po_count": 1,
                },
                {
                    "vendor_id": 1,
                    "vendor_name": "Krishna Supplies",
                    "po_count": 3,
                },
            ]

            response = self.client.get(reverse("analytics:analytics_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "€ 324.00")
        self.assertContains(response, "Product:")
        self.assertContains(response, "€ 270.00")
        self.assertContains(response, "Tax")
        self.assertContains(response, "€ 54.00")
        self.assertContains(response, "€ 81.00")
        self.assertContains(response, "Krishna Supplies")
        self.assertContains(response, "Rice bag 20 Kg")
        self.assertContains(response, "Vendor-wise Spend")
        self.assertContains(response, "Orders per Vendor")
        self.assertContains(response, "Purchase order count grouped by vendor.")
        self.assertContains(response, "Top Products")
        self.assertContains(response, "Top 5")
        self.assertContains(response, '<option value="5" selected>Top 5</option>', html=True)
        self.assertNotContains(response, "All Vendors")
        self.assertContains(response, "Product Price Trend")
        self.assertContains(response, "Full price history over time for the selected product across vendors.")
        self.assertContains(response, "Rice bag 20 Kg")
        self.assertContains(response, '<option value="11" selected>Rice bag 20 Kg</option>', html=True)
        self.assertContains(response, '<option value="12">Beans 10 Kg</option>', html=True)
        self.assertContains(response, reverse("analytics:product_price_trend_data"))
        self.assertContains(response, 'type: "line"', html=False)
        self.assertContains(response, "product-price-trend-chart-data", html=False)
        self.assertContains(response, "function buildCurrentDashboardUrl(searchParams)", html=False)
        self.assertContains(response, "window.location.pathname", html=False)
        self.assertContains(response, 'new URL(link.getAttribute("href"), window.location.href)', html=False)
        self.assertNotContains(response, 'new URL(link.getAttribute("href"), window.location.origin)', html=False)
        self.assertContains(response, "Vendor: ${firstItem.dataset.label}", html=False)
        self.assertContains(response, 'Date: ${firstItem.label || ""}', html=False)
        self.assertContains(response, 'Price: ${formatEuroAmount(typeof context.parsed.y === "number" ? context.parsed.y : 0)}', html=False)
        self.assertContains(response, "fetchAndApplyProductPriceTrend(productPriceTrendSelect.value)", html=False)
        self.assertContains(response, 'fetch(productPriceTrendUrl.toString()', html=False)
        self.assertNotContains(response, "productPriceTrendForm.submit()", html=False)
        self.assertContains(response, 'type: "category"', html=False)
        self.assertContains(response, 'type: "linear"', html=False)
        self.assertContains(response, "function getPriceTrendValueAxisConfig", html=False)
        self.assertContains(response, "min: minValue", html=False)
        self.assertContains(response, "max: maxValue", html=False)
        self.assertContains(response, "Price Change Movement by Vendor (€)")
        self.assertContains(response, "Share of accepted price-change movement by vendor. Tooltip shows net impact.")
        self.assertContains(response, 'type: "doughnut"', html=False)
        self.assertContains(response, "Movement (€)", html=False)
        self.assertContains(response, "analytics-chart-tooltip", html=False)
        self.assertContains(response, "analytics-chart-tooltip-title", html=False)
        self.assertContains(response, "analytics-chart-tooltip-line--net", html=False)
        self.assertContains(response, "analytics-chart-tooltip-line--share", html=False)
        self.assertContains(response, "createVendorPriceChangeTooltipRenderer", html=False)
        self.assertContains(response, 'enabled: false', html=False)
        self.assertContains(response, "external: createVendorPriceChangeTooltipRenderer(vendorPriceChangeData)", html=False)
        self.assertContains(response, "extra spend", html=False)
        self.assertContains(response, "savings", html=False)
        self.assertContains(response, "No impact", html=False)
        self.assertContains(response, "movement share", html=False)
        self.assertContains(response, "https://cdn.jsdelivr.net/npm/chart.js")
        self.assertNotContains(response, "Accepted quantities or final accepted value by product.")
        self.assertContains(response, "function getSharedYAxisScaleConfig")
        self.assertContains(response, "function getSharedAxisTickFont", html=False)
        self.assertContains(response, "font: getSharedAxisTickFont(layout.fontSize)", html=False)
        self.assertContains(response, "const vendorFrequencyBarColors", html=False)
        self.assertContains(response, 'anchor: "center"', html=False)
        self.assertContains(response, 'align: "center"', html=False)
        self.assertContains(response, 'color: "#ffffff"', html=False)
        self.assertContains(response, "function getAdaptiveCategoryLabelLayout")
        self.assertContains(response, "function formatAdaptiveCategoryLabel")
        self.assertContains(response, "title: getCategoryTooltipTitle")
        self.assertEqual(
            response.context["vendor_frequency_chart"],
            {
                "labels": ["Krishna Supplies", "Beta Supplies"],
                "values": [3, 1],
                "ids": [1, 2],
            },
        )
        self.assertEqual(
            response.context["product_price_trend_chart"],
            {
                "product_id": 11,
                "product_name": "Rice bag 20 Kg",
                "labels": ["2026-08-10", "2026-08-15"],
                "datasets": [
                    {
                        "vendor_id": 1,
                        "vendor_name": "Krishna Supplies",
                        "prices": [5.5, 6.0],
                        "old_prices": [5.0, 5.5],
                        "new_prices": [5.5, 6.0],
                    }
                ],
            },
        )
        self.assertEqual(
            response.context["vendor_price_change_chart"],
            {
                "labels": ["Krishna Supplies"],
                "values": [10.0],
                "ids": [1],
                "increase_counts": [2],
                "decrease_counts": [1],
                "net_impacts": [4.0],
                "shares": [100.0],
            },
        )

    def test_dashboard_view_shows_empty_state_when_no_price_trend_data_exists(self):
        with self._selector_patches() as mocks:
            self._mock_dashboard_selector_returns(mocks)
            mocks["get_products_with_price_trend"].return_value = []
            mocks["get_product_price_trend"].return_value = {
                "product_id": None,
                "product_name": "",
                "labels": [],
                "datasets": [],
            }

            response = self.client.get(reverse("analytics:analytics_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No products with price history are available yet.")
        self.assertContains(response, "id=\"productPriceTrendChart\"", html=False)
        self.assertContains(response, "data-product-price-trend-empty-state", html=False)

    def test_dashboard_view_filters_zero_spend_vendors_from_vendor_spend_chart(self):
        with self._selector_patches() as mocks:
            self._mock_dashboard_selector_returns(mocks)
            mocks["get_vendor_wise_spend"].return_value = [
                {
                    "vendor_id": 1,
                    "vendor_name": "Krishna Supplies",
                    "total_spend": Decimal("228.00"),
                },
                {
                    "vendor_id": 2,
                    "vendor_name": "Zero Spend Vendor",
                    "total_spend": Decimal("0.00"),
                },
            ]

            response = self.client.get(reverse("analytics:analytics_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["vendor_spend_chart"],
            {
                "labels": ["Krishna Supplies"],
                "values": [228.0],
                "ids": [1],
            },
        )
        self.assertContains(response, 'display: (context) => Number(context.dataset.data[context.dataIndex] || 0) > 0')
        self.assertNotContains(response, "Zero Spend Vendor")


class AnalyticsBackfillCommandTests(AnalyticsSyncTestCase):
    def _mark_po_closed_with_syncable_data(
        self,
        purchase_order,
        *,
        invoice_items,
        invoice_net_total,
        invoice_tax_total,
        invoice_grand_total,
    ):
        validation_result = self._build_validation_result(
            purchase_order,
            invoice_items,
            invoice_net_total=invoice_net_total,
            invoice_tax_total=invoice_tax_total,
            invoice_grand_total=invoice_grand_total,
            apply_financial=True,
        )
        closed_at = timezone.now()
        purchase_order.status = PurchaseOrder.STATUS_CLOSED
        purchase_order.validation_note = PurchaseOrder.NOTE_VALIDATED_SUCCESSFULLY
        purchase_order.validation_data = validation_result
        purchase_order.validated_at = closed_at
        purchase_order.closed_at = closed_at
        purchase_order.save()
        return purchase_order

    def test_backfill_analytics_syncs_existing_closed_purchase_orders(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product)
        self._mark_po_closed_with_syncable_data(
            purchase_order,
            invoice_items=[
                self._invoice_item(
                    name=product.display_name,
                    quantity=2,
                    unit_price="10.50",
                    amount="21.00",
                )
            ],
            invoice_net_total="21.00",
            invoice_tax_total="4.00",
            invoice_grand_total="25.00",
        )

        skipped_po = self._create_purchase_order(product)
        skipped_po.status = PurchaseOrder.STATUS_CLOSED
        skipped_po.validation_note = "Closed manually without validation data."
        skipped_po.closed_at = timezone.now()
        skipped_po.save()

        stdout = StringIO()
        call_command("backfill_analytics", stdout=stdout)
        output = stdout.getvalue()

        self.assertTrue(FactPurchaseOrder.objects.filter(po=purchase_order).exists())
        self.assertIn(f"SYNCED {purchase_order.po_number}", output)
        self.assertIn(f"SKIP {skipped_po.po_number}", output)
        self.assertIn("Total POs inspected: 2", output)
        self.assertIn("Total POs synced: 1", output)
        self.assertIn("Total POs skipped: 1", output)
        self.assertIn("Total failures: 0", output)

    def test_backfill_analytics_dry_run_does_not_create_facts(self):
        product = self._create_product()
        purchase_order = self._create_purchase_order(product)
        self._mark_po_closed_with_syncable_data(
            purchase_order,
            invoice_items=[
                self._invoice_item(
                    name=product.display_name,
                    quantity=2,
                    unit_price="10.50",
                    amount="21.00",
                )
            ],
            invoice_net_total="21.00",
            invoice_tax_total="4.00",
            invoice_grand_total="25.00",
        )

        stdout = StringIO()
        call_command("backfill_analytics", "--dry-run", stdout=stdout)
        output = stdout.getvalue()

        self.assertFalse(FactPurchaseOrder.objects.filter(po=purchase_order).exists())
        self.assertIn(f"WOULD SYNC {purchase_order.po_number}", output)
        self.assertIn("Total POs synced: 1", output)
        self.assertIn("Total failures: 0", output)

    def test_backfill_analytics_can_target_single_po_number(self):
        product = self._create_product()
        first_po = self._create_purchase_order(product)
        second_po = self._create_purchase_order(product)
        self._mark_po_closed_with_syncable_data(
            first_po,
            invoice_items=[
                self._invoice_item(
                    name=product.display_name,
                    quantity=2,
                    unit_price="10.50",
                    amount="21.00",
                )
            ],
            invoice_net_total="21.00",
            invoice_tax_total="4.00",
            invoice_grand_total="25.00",
        )
        self._mark_po_closed_with_syncable_data(
            second_po,
            invoice_items=[
                self._invoice_item(
                    name=product.display_name,
                    quantity=2,
                    unit_price="10.50",
                    amount="21.00",
                )
            ],
            invoice_net_total="21.00",
            invoice_tax_total="4.00",
            invoice_grand_total="25.00",
        )

        stdout = StringIO()
        call_command("backfill_analytics", "--po-number", first_po.po_number, stdout=stdout)
        output = stdout.getvalue()

        self.assertTrue(FactPurchaseOrder.objects.filter(po=first_po).exists())
        self.assertFalse(FactPurchaseOrder.objects.filter(po=second_po).exists())
        self.assertIn(f"SYNCED {first_po.po_number}", output)
        self.assertNotIn(second_po.po_number, output)
        self.assertIn("Total POs inspected: 1", output)
