from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from analytics.services import sync_all_po_analytics
from carting.models import Cart

from .forms import PurchaseOrderInvoiceForm
from .models import PurchaseOrder, PurchaseOrderItem
from .pdf import build_purchase_order_pdf
from .presentation import prepare_purchase_orders
from .price_updates import (
    get_validation_price_mismatches,
    manual_close_note_for_purchase_order,
    update_vendor_prices_from_validation,
)
from .reconciliation import (
    MANUAL_PRICE_ACCEPTED,
    MANUAL_PRICE_KEEP_OLD,
    MANUAL_QUANTITY_USE_INVOICE,
    MANUAL_QUANTITY_USE_PO,
    clean_reconciliation_name,
    format_money,
    format_quantity_note_value,
    maybe_create_manual_alias,
    normalize_quantity_resolution,
    reconciliation_field_name,
)
from .services import (
    InvoiceAnalysisError,
    InvoiceConfigurationError,
    PRESERVED_FINANCIAL_KEYS,
    apply_financial_validation,
    analyze_purchase_order_invoice,
    classify_invoice_against_purchase_order,
)
from .workflows import apply_validation_result, reset_purchase_order_invoice_state


@login_required
def po_list(request):
    purchase_orders = list(
        PurchaseOrder.objects.select_related("vendor", "created_by")
        .prefetch_related("items__product")
        .exclude(status=PurchaseOrder.STATUS_CLOSED)
        .order_by("-created_at")
    )

    return render(
        request,
        "purchases/po_list.html",
        {
            "purchase_orders": prepare_purchase_orders(purchase_orders),
        },
    )


@login_required
@require_POST
def create_po_from_cart_vendor(request, vendor_id):
    cart = Cart.objects.filter(
        user=request.user,
        status=Cart.STATUS_OPEN,
    ).first()

    if cart is None:
        messages.error(request, "Cart is empty.")
        return redirect("carting:cart_detail")

    vendor_items = list(
        cart.items.select_related("product", "vendor")
        .filter(vendor_id=vendor_id)
        .order_by("product__display_name")
    )

    if not vendor_items:
        messages.error(request, "No cart items found for that vendor.")
        return redirect("carting:cart_detail")

    with transaction.atomic():
        purchase_order = PurchaseOrder.objects.create(
            vendor=vendor_items[0].vendor,
            created_by=request.user,
        )
        PurchaseOrderItem.objects.bulk_create(
            [
                PurchaseOrderItem(
                    purchase_order=purchase_order,
                    product=item.product,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    line_total=item.unit_price * item.quantity,
                )
                for item in vendor_items
            ]
        )
        cart.items.filter(pk__in=[item.pk for item in vendor_items]).delete()

    messages.success(
        request,
        f"PO {purchase_order.po_number} created for {purchase_order.vendor.name}",
    )
    return redirect("purchases:po_list")


@login_required
def download_po_pdf(request, po_id):
    purchase_order = get_object_or_404(
        PurchaseOrder.objects.select_related("vendor", "created_by").prefetch_related("items__product"),
        pk=po_id,
    )
    response = HttpResponse(
        build_purchase_order_pdf(purchase_order),
        content_type="application/pdf",
    )
    response["Content-Disposition"] = f'attachment; filename="{purchase_order.po_number}.pdf"'
    return response


@login_required
@require_POST
def upload_invoice(request, po_id):
    purchase_order = get_object_or_404(PurchaseOrder, pk=po_id)

    if purchase_order.is_closed:
        messages.error(request, "Closed POs cannot be updated from the active PO page.")
        return redirect("purchases:po_list")

    uploaded_invoice = request.FILES.get("invoice_file")
    if not uploaded_invoice:
        messages.error(request, "Select a PDF or invoice image to upload.")
        return redirect("purchases:po_list")

    form = PurchaseOrderInvoiceForm(request.POST, request.FILES, instance=purchase_order)
    if not form.is_valid():
        messages.error(
            request,
            form.errors.get("invoice_file", ["Select a PDF or invoice image to upload."])[0],
        )
        return redirect("purchases:po_list")

    purchase_order = form.save(commit=False)
    purchase_order.status = PurchaseOrder.STATUS_INVOICE_UPLOADED
    purchase_order.validation_note = "Invoice uploaded. Click Validate to compare against the PO."
    purchase_order.validation_data = {}
    purchase_order.validated_at = None
    purchase_order.closed_at = None
    purchase_order.save()

    messages.success(request, "Invoice uploaded. Click Validate to compare against the PO.")
    return redirect("purchases:po_list")


@login_required
@require_POST
def validate_invoice(request, po_id):
    purchase_order = get_object_or_404(PurchaseOrder, pk=po_id)

    if purchase_order.is_closed:
        messages.error(request, "Closed POs cannot be validated from the active PO page.")
        return redirect("purchases:po_list")

    if not purchase_order.invoice_file:
        messages.error(request, "Upload an invoice before running validation.")
        return redirect("purchases:po_list")

    try:
        validation_result = analyze_purchase_order_invoice(purchase_order)
    except InvoiceConfigurationError as exc:
        purchase_order.status = PurchaseOrder.STATUS_INVOICE_UPLOADED
        purchase_order.validation_note = str(exc)
        purchase_order.validation_data = {}
        purchase_order.validated_at = None
        purchase_order.closed_at = None
        purchase_order.save()
        messages.error(request, str(exc))
        return redirect("purchases:po_list")
    except InvoiceAnalysisError as exc:
        purchase_order.status = PurchaseOrder.STATUS_INVOICE_UPLOADED
        purchase_order.validation_note = f"Invoice uploaded but validation failed: {exc}"
        purchase_order.validation_data = {}
        purchase_order.validated_at = None
        purchase_order.closed_at = None
        purchase_order.save()
        messages.error(request, str(exc))
        return redirect("purchases:po_list")

    apply_validation_result(purchase_order, validation_result)

    has_product_mismatch = bool(validation_result.get("has_product_mismatch"))
    has_quantity_mismatch = bool(validation_result.get("has_quantity_mismatch"))
    has_price_mismatch = bool(validation_result.get("has_price_mismatch"))

    if not validation_result.get("has_any_mismatch"):
        messages.success(
            request,
            f"Invoice validated successfully. {purchase_order.po_number} moved to History.",
        )
    elif has_product_mismatch:
        if has_product_mismatch and has_quantity_mismatch and has_price_mismatch:
            messages.error(
                request,
                f"Validation found product, quantity, and price differences for {purchase_order.po_number}. Manual reconciliation is required.",
            )
        elif has_product_mismatch and has_quantity_mismatch:
            messages.error(
                request,
                f"Validation found product and quantity mismatches for {purchase_order.po_number}. Manual reconciliation is required.",
            )
        elif has_product_mismatch and has_price_mismatch:
            messages.error(
                request,
                f"Validation found product mismatches for {purchase_order.po_number}. Price differences are also shown for review. Manual reconciliation is required.",
            )
        elif has_product_mismatch:
            messages.error(
                request,
                f"Product mismatch detected for {purchase_order.po_number}. Manual reconciliation is required.",
            )
        else:
            messages.error(
                request,
                f"Validation found differences for {purchase_order.po_number}. Review the PO details.",
            )
    elif has_quantity_mismatch:
        if has_price_mismatch:
            messages.error(
                request,
                f"Validation found quantity mismatches for {purchase_order.po_number}. Price differences are also shown for review. Manual reconciliation is required.",
            )
        else:
            messages.error(
                request,
                f"Quantity mismatch detected for {purchase_order.po_number}. Manual reconciliation is required.",
            )
    elif has_price_mismatch:
        messages.error(
            request,
            f"Price mismatch detected for {purchase_order.po_number}. Review whether to update the stored price or keep the current price before manually validating.",
        )
    else:
        messages.error(
            request,
            f"Validation found differences for {purchase_order.po_number}. Review the PO details.",
        )

    return redirect("purchases:po_list")


@login_required
@require_POST
def manual_validate_po(request, po_id):
    purchase_order = get_object_or_404(
        PurchaseOrder.objects.select_related("vendor", "created_by").prefetch_related(
            "items__product__aliases"
        ),
        pk=po_id,
    )

    if purchase_order.is_closed:
        messages.error(request, "Closed POs cannot be manually reconciled from the active PO page.")
        return redirect("purchases:po_list")

    validation_data = purchase_order.validation_data or {}
    invoice_items = validation_data.get("invoice_items") or []
    extra_products = validation_data.get("extra_products") or []
    quantity_mismatches = validation_data.get("quantity_mismatches") or []
    price_mismatches = validation_data.get("price_mismatches") or []
    stored_manual_product_mappings = {
        int(invoice_item_index): int(product_id)
        for invoice_item_index, product_id in (validation_data.get("manual_product_mappings") or {}).items()
    }
    stored_quantity_resolutions = {
        int(item["po_item_id"]): normalize_quantity_resolution(item.get("resolution"))
        for item in (validation_data.get("resolved_quantity_mismatches") or [])
        if item.get("po_item_id") is not None
    }
    stored_quantity_resolutions.update(
        {
            int(item["po_item_id"]): normalize_quantity_resolution(item.get("resolution"))
            for item in (validation_data.get("confirmed_shortages") or [])
            if item.get("po_item_id") is not None
        }
    )
    stored_quantity_resolutions.update(
        {
            int(item["po_item_id"]): normalize_quantity_resolution(item.get("resolution"))
            for item in (validation_data.get("accepted_additional_quantities") or [])
            if item.get("po_item_id") is not None
        }
    )
    stored_price_decisions = {
        int(item["po_item_id"]): item.get("decision")
        for item in (validation_data.get("resolved_price_mismatches") or [])
        if item.get("po_item_id") is not None
    }

    if not invoice_items or (not extra_products and not quantity_mismatches and not price_mismatches):
        messages.error(request, "There is no manual reconciliation work pending for this PO.")
        return redirect("purchases:po_list")

    po_items = list(purchase_order.items.select_related("product").all())
    po_items_by_id = {item.id: item for item in po_items}
    po_items_by_product_id = {item.product_id: item for item in po_items}

    manual_product_mappings = dict(stored_manual_product_mappings)
    alias_requests = []
    # Gap 1: track which extra invoice items the user is disputing.
    disputed_invoice_item_indexes = set()

    for extra_product in extra_products:
        invoice_item_index = extra_product.get("invoice_item_index")
        if invoice_item_index is None or invoice_item_index >= len(invoice_items):
            messages.error(
                request,
                "The invoice item mapping data is no longer valid. Please run validation again.",
            )
            return redirect("purchases:po_list")

        # Gap 1: "Dispute this charge" takes precedence over mapping.
        if request.POST.get(f"dispute_{invoice_item_index}") == "on":
            disputed_invoice_item_indexes.add(invoice_item_index)
            continue

        selected_product_raw = (request.POST.get(f"map_product_{invoice_item_index}") or "").strip()
        if not selected_product_raw.isdigit():
            messages.error(
                request,
                "For each unmatched invoice item, either map it to a PO product or dispute the charge.",
            )
            return redirect("purchases:po_list")

        selected_product_id = int(selected_product_raw)
        if selected_product_id not in po_items_by_product_id:
            messages.error(
                request,
                "Manual validation can only map invoice items to products that already exist on this PO.",
            )
            return redirect("purchases:po_list")

        manual_product_mappings[invoice_item_index] = selected_product_id
        alias_requests.append(
            {
                "invoice_item_index": invoice_item_index,
                "product_id": selected_product_id,
                "save_alias": request.POST.get(f"save_alias_{invoice_item_index}") == "on",
            }
        )

    # Gap 2: Save aliases EAGERLY — before entering the transaction — so they survive
    # even if the PO closure later fails for any reason (e.g. a different validation
    # error) or the user eventually uses "Close Only" as an escape hatch.
    eager_alias_cache = {}
    for alias_req in alias_requests:
        if alias_req["save_alias"]:
            po_item = po_items_by_product_id[alias_req["product_id"]]
            invoice_item = invoice_items[alias_req["invoice_item_index"]]
            invoice_item_name = clean_reconciliation_name(
                invoice_item.get("name") or "Unrecognized invoice item"
            )
            maybe_create_manual_alias(po_item.product, invoice_item_name, eager_alias_cache)

    # Build disputed item records for audit trail and history display.
    # Note: invoice items store the line total as "amount" (from the parser),
    # with "line_total" only present on extra_products entries.
    disputed_items_data = []
    for inv_idx in sorted(disputed_invoice_item_indexes):
        inv_item = invoice_items[inv_idx]
        disputed_items_data.append(
            {
                "invoice_item_index": inv_idx,
                "product_name": clean_reconciliation_name(inv_item.get("name") or "Unknown item"),
                "quantity": inv_item.get("quantity", "?"),
                "unit_price": inv_item.get("unit_price", "?"),
                "line_total": inv_item.get("line_total") or inv_item.get("amount", "?"),
            }
        )

    mapped_invoice_item_indexes = {
        item["invoice_item_index"]
        for item in extra_products
        if item.get("invoice_item_index") is not None
        and item.get("invoice_item_index") not in disputed_invoice_item_indexes
    }

    with transaction.atomic():
        reconciled_result = classify_invoice_against_purchase_order(
            purchase_order,
            invoice_items,
            manual_product_mappings=manual_product_mappings,
        )
        for metadata_key in PRESERVED_FINANCIAL_KEYS:
            reconciled_result[metadata_key] = validation_data.get(metadata_key)

        if reconciled_result.get("has_product_mismatch"):
            # Gap 1: if every remaining unmatched item is being disputed, that is
            # a valid resolution — the buyer is formally rejecting those charges.
            remaining_unmatched = [
                ep
                for ep in (reconciled_result.get("extra_products") or [])
                if ep.get("invoice_item_index") not in disputed_invoice_item_indexes
            ]
            if remaining_unmatched:
                messages.error(
                    request,
                    "Manual validation could not reconcile all unmatched invoice items. Please review the selected mappings.",
                )
                return redirect("purchases:po_list")
            # All unmatched items are disputed — override the mismatch flag.
            reconciled_result["has_product_mismatch"] = False

        quantity_resolutions = {}
        for quantity_mismatch in reconciled_result.get("quantity_mismatches") or []:
            field_name = reconciliation_field_name(
                "quantity_resolution",
                quantity_mismatch,
                mapped_invoice_item_indexes,
            )
            resolution = stored_quantity_resolutions.get(quantity_mismatch["po_item_id"])
            if resolution not in {MANUAL_QUANTITY_USE_PO, MANUAL_QUANTITY_USE_INVOICE}:
                resolution = normalize_quantity_resolution((request.POST.get(field_name) or "").strip())
            if resolution not in {MANUAL_QUANTITY_USE_PO, MANUAL_QUANTITY_USE_INVOICE}:
                messages.error(
                    request,
                    "Confirm the accepted quantity for each quantity mismatch before manually validating.",
                )
                return redirect("purchases:po_list")
            quantity_resolutions[quantity_mismatch["po_item_id"]] = resolution

        price_decisions = {}
        for price_mismatch in reconciled_result.get("price_mismatches") or []:
            field_name = reconciliation_field_name(
                "price_decision",
                price_mismatch,
                mapped_invoice_item_indexes,
            )
            decision = stored_price_decisions.get(price_mismatch["po_item_id"])
            if decision not in {MANUAL_PRICE_ACCEPTED, MANUAL_PRICE_KEEP_OLD}:
                decision = (request.POST.get(field_name) or "").strip()
            if decision not in {MANUAL_PRICE_ACCEPTED, MANUAL_PRICE_KEEP_OLD}:
                messages.error(
                    request,
                    "Review each price mismatch and choose whether to update to the new price or keep the old price before manually validating.",
                )
                return redirect("purchases:po_list")
            price_decisions[price_mismatch["po_item_id"]] = decision

        existing_audit_notes = list(validation_data.get("audit_notes") or [])
        manual_audit_notes = []
        normalized_names_by_product_id = {}

        for alias_request in alias_requests:
            invoice_item = invoice_items[alias_request["invoice_item_index"]]
            po_item = po_items_by_product_id[alias_request["product_id"]]
            invoice_item_name = clean_reconciliation_name(
                invoice_item.get("name") or "Unrecognized invoice item"
            )
            alias_created = False

            if alias_request["save_alias"]:
                alias_created = maybe_create_manual_alias(
                    po_item.product,
                    invoice_item_name,
                    normalized_names_by_product_id,
                )

            note = (
                f'Name mismatch detected: invoice item "{invoice_item_name}" was manually confirmed '
                f'as product "{po_item.product.display_name}".'
            )
            if alias_created:
                note += f' "{invoice_item_name}" was added as an alternative name.'

            manual_audit_notes.append(note)

        # Gap 1: record each disputed charge in the audit trail.
        for disputed_item in disputed_items_data:
            manual_audit_notes.append(
                f'Invoice item "{disputed_item["product_name"]}" (qty: {disputed_item["quantity"]}, '
                f'unit price: {disputed_item["unit_price"]}) was NOT on this PO and was formally '
                f'disputed. This charge was not accepted.'
            )

        resolved_quantity_mismatches = []
        confirmed_shortages = []
        accepted_additional_quantities = []
        resolved_price_mismatches = []
        accepted_price_updates = []

        for quantity_mismatch in reconciled_result.get("quantity_mismatches") or []:
            po_item = po_items_by_id[quantity_mismatch["po_item_id"]]
            resolution = quantity_resolutions[quantity_mismatch["po_item_id"]]
            po_quantity = format_quantity_note_value(quantity_mismatch["po_quantity"])
            invoice_quantity = format_quantity_note_value(quantity_mismatch["invoice_quantity"])
            quantity_case = quantity_mismatch.get("quantity_case") or (
                "over_delivery"
                if Decimal(str(quantity_mismatch["invoice_quantity"]))
                > Decimal(str(quantity_mismatch["po_quantity"]))
                else "shortage"
            )
            resolution_already_recorded = (
                stored_quantity_resolutions.get(quantity_mismatch["po_item_id"]) == resolution
            )

            resolved_entry = {
                **quantity_mismatch,
                "resolution": resolution,
            }

            if resolution == MANUAL_QUANTITY_USE_PO:
                resolved_quantity_mismatches.append(resolved_entry)
                if not resolution_already_recorded:
                    if quantity_case == "over_delivery":
                        manual_audit_notes.append(
                            f'Quantity mismatch detected for "{quantity_mismatch["product_name"]}": '
                            f"PO qty {po_quantity}, invoice qty {invoice_quantity}. "
                            "User did not accept the additional quantity. Accepted quantity stayed at the PO quantity."
                        )
                        manual_audit_notes.append(
                            f'Effective PO values for "{quantity_mismatch["product_name"]}" remained at quantity {po_quantity} '
                            f"and line total {format_money(Decimal(str(po_item.quantity)) * Decimal(str(po_item.unit_price)))} "
                            "after rejecting the additional quantity."
                        )
                    else:
                        manual_audit_notes.append(
                            f'Quantity mismatch detected for "{quantity_mismatch["product_name"]}": '
                            f"PO qty {po_quantity}, invoice qty {invoice_quantity}. "
                            f"User physically verified {po_quantity} units were received and manually validated."
                        )
                        manual_audit_notes.append(
                            f'Effective PO values for "{quantity_mismatch["product_name"]}" remained at quantity {po_quantity} '
                            f"and line total {format_money(Decimal(str(po_item.quantity)) * Decimal(str(po_item.unit_price)))} "
                            "after the physical check."
                        )
            elif quantity_case == "over_delivery":
                accepted_additional_quantities.append(resolved_entry)
                if not resolution_already_recorded:
                    manual_audit_notes.append(
                        f'Quantity mismatch detected for "{quantity_mismatch["product_name"]}": '
                        f"PO qty {po_quantity}, invoice qty {invoice_quantity}. "
                        "User accepted the additional quantity from the invoice."
                    )
                    manual_audit_notes.append(
                        f'Effective PO values for "{quantity_mismatch["product_name"]}" were updated to quantity {invoice_quantity} '
                        f"and line total {format_money(Decimal(str(quantity_mismatch['invoice_quantity'])) * Decimal(str(po_item.unit_price)))} "
                        "after accepting the additional quantity."
                    )
            else:
                confirmed_shortages.append(resolved_entry)
                shortage_quantity = format_quantity_note_value(
                    abs(Decimal(str(quantity_mismatch["difference"])))
                )
                if not resolution_already_recorded:
                    manual_audit_notes.append(
                        f'Quantity mismatch detected for "{quantity_mismatch["product_name"]}": '
                        f"PO qty {po_quantity}, invoice qty {invoice_quantity}. "
                        f"User physically verified shortage remains. PO closed with missing quantity of {shortage_quantity}."
                    )
                    manual_audit_notes.append(
                        f'Effective PO values for "{quantity_mismatch["product_name"]}" were reduced to quantity {invoice_quantity} '
                        f"and line total {format_money(Decimal(str(quantity_mismatch['invoice_quantity'])) * Decimal(str(po_item.unit_price)))} "
                        "after confirming the shortage."
                    )

        for price_mismatch in reconciled_result.get("price_mismatches") or []:
            decision = price_decisions[price_mismatch["po_item_id"]]
            before_price = price_mismatch["before_price"]
            updated_price = price_mismatch["updated_price"]

            resolved_price_mismatches.append(
                {
                    **price_mismatch,
                    "decision": decision,
                }
            )

            if decision == MANUAL_PRICE_ACCEPTED:
                accepted_price_updates.append(price_mismatch)
                manual_audit_notes.append(
                    f'Price mismatch detected for "{price_mismatch["product_name"]}": '
                    f"PO unit price {before_price}, invoice unit price {updated_price}. "
                    "User confirmed the price changed and the stored price was updated to the new invoice price."
                )
                manual_audit_notes.append(
                    f'Effective PO values for "{price_mismatch["product_name"]}" were recalculated using unit price '
                    f"{updated_price}."
                )
            else:
                manual_audit_notes.append(
                    f'Price mismatch detected for "{price_mismatch["product_name"]}": '
                    f"PO unit price {before_price}, invoice unit price {updated_price}. "
                    "User marked the invoice price as a mistake. The existing price was kept unchanged."
                )

        if accepted_price_updates:
            update_vendor_prices_from_validation(purchase_order, accepted_price_updates)

        reconciled_result["resolved_quantity_mismatches"] = resolved_quantity_mismatches
        reconciled_result["confirmed_shortages"] = confirmed_shortages
        reconciled_result["accepted_additional_quantities"] = accepted_additional_quantities
        reconciled_result["resolved_price_mismatches"] = resolved_price_mismatches
        reconciled_result["quantity_mismatches"] = []
        reconciled_result["price_mismatches"] = []
        reconciled_result["manual_product_mappings"] = manual_product_mappings
        reconciled_result["audit_notes"] = existing_audit_notes + manual_audit_notes
        reconciled_result["has_product_mismatch"] = False
        reconciled_result["has_quantity_mismatch"] = False
        reconciled_result["has_price_mismatch"] = False
        reconciled_result["has_any_mismatch"] = False
        reconciled_result["requires_manual_close"] = False
        reconciled_result["can_update_prices"] = False
        reconciled_result["manual_reconciliation_complete"] = True
        reconciled_result = apply_financial_validation(
            purchase_order,
            reconciled_result,
            quantity_resolutions=quantity_resolutions,
            price_decisions=price_decisions,
        )
        reconciled_result["matched_items"] = [
            {
                "invoice_item_index": item.get("invoice_item_index"),
                "po_item_id": item["po_item_id"],
                "product_id": item["product_id"],
                "product_name": item["product_name"],
                "quantity": item["effective_quantity"],
                "unit_price": item["effective_unit_price"],
                "line_total": item["effective_line_total"],
            }
            for item in reconciled_result.get("effective_items") or []
        ]

        # Gap 1: persist disputed items so History can display them.
        if disputed_items_data:
            reconciled_result["disputed_invoice_items"] = disputed_items_data

        now = timezone.now()
        purchase_order.validation_data = reconciled_result
        purchase_order.validated_at = now
        purchase_order.status = PurchaseOrder.STATUS_CLOSED
        purchase_order.closed_at = now

        has_shortages = bool(confirmed_shortages)
        has_disputes = bool(disputed_items_data)

        if has_shortages and has_disputes:
            purchase_order.validation_note = (
                "Manual reconciliation completed with confirmed shortage and disputed charges."
            )
            success_message = (
                f"{purchase_order.po_number} was manually reconciled and moved to History "
                "with shortage and disputed charges recorded."
            )
        elif has_shortages:
            purchase_order.validation_note = "Manual reconciliation completed with confirmed shortage."
            success_message = (
                f"{purchase_order.po_number} was manually reconciled and moved to History "
                "with the confirmed shortage recorded."
            )
        elif has_disputes:
            purchase_order.validation_note = (
                "Manual reconciliation completed with disputed charges."
            )
            success_message = (
                f"{purchase_order.po_number} was manually validated and moved to History "
                "with disputed charges recorded."
            )
        else:
            purchase_order.validation_note = "Validated successfully after manual reconciliation."
            success_message = f"{purchase_order.po_number} was manually validated and moved to History."

        purchase_order.save()
        sync_all_po_analytics(purchase_order)

    messages.success(request, success_message)
    return redirect("purchases:history_list")


@login_required
@require_POST
def clear_invoice(request, po_id):
    purchase_order = get_object_or_404(PurchaseOrder, pk=po_id)

    if purchase_order.is_closed:
        messages.error(request, "Closed POs cannot be reset from the active PO page.")
        return redirect("purchases:po_list")

    if not purchase_order.invoice_file:
        messages.error(request, "There is no uploaded invoice to clear.")
        return redirect("purchases:po_list")

    reset_purchase_order_invoice_state(purchase_order, delete_file=True)
    purchase_order.save()

    messages.success(request, "Invoice cleared. You can upload a new invoice.")
    return redirect("purchases:po_list")


@login_required
@require_POST
def close_po_manually(request, po_id):
    purchase_order = get_object_or_404(PurchaseOrder, pk=po_id)

    if purchase_order.status not in {
        PurchaseOrder.STATUS_PRODUCT_MISMATCH,
        PurchaseOrder.STATUS_QUANTITY_MISMATCH,
        PurchaseOrder.STATUS_PRICE_MISMATCH,
    }:
        messages.error(request, "This PO cannot be closed manually from its current state.")
        return redirect("purchases:po_list")

    purchase_order.validation_note = manual_close_note_for_purchase_order(purchase_order)
    purchase_order.status = PurchaseOrder.STATUS_CLOSED
    purchase_order.closed_at = timezone.now()
    purchase_order.save()
    sync_all_po_analytics(purchase_order)

    messages.success(
        request,
        f"{purchase_order.po_number} was moved to History for manual review closure.",
    )
    return redirect("purchases:history_list")


@login_required
@require_POST
def close_po_manually_with_prices(request, po_id):
    purchase_order = get_object_or_404(
        PurchaseOrder.objects.select_related("vendor").prefetch_related("items__product"),
        pk=po_id,
    )

    if purchase_order.status not in {
        PurchaseOrder.STATUS_PRODUCT_MISMATCH,
        PurchaseOrder.STATUS_QUANTITY_MISMATCH,
        PurchaseOrder.STATUS_PRICE_MISMATCH,
    }:
        messages.error(request, "This PO cannot be closed manually from its current state.")
        return redirect("purchases:po_list")

    validation_data, price_mismatches = get_validation_price_mismatches(purchase_order)
    if not validation_data.get("requires_manual_close") or not price_mismatches:
        messages.error(
            request,
            "There are no matched invoice price updates available for this manual close action.",
        )
        return redirect("purchases:po_list")

    with transaction.atomic():
        updated_count = update_vendor_prices_from_validation(purchase_order, price_mismatches)
        if not updated_count:
            messages.error(request, "No matched vendor prices were available to update.")
            return redirect("purchases:po_list")

        purchase_order.validation_note = manual_close_note_for_purchase_order(
            purchase_order,
            include_price_update=True,
        )
        purchase_order.status = PurchaseOrder.STATUS_CLOSED
        purchase_order.closed_at = timezone.now()
        purchase_order.save()
        sync_all_po_analytics(purchase_order)

    messages.success(
        request,
        f"Matched vendor prices were updated from the invoice and {purchase_order.po_number} moved to History.",
    )
    return redirect("purchases:history_list")


@login_required
@require_POST
def confirm_price_update(request, po_id):
    purchase_order = get_object_or_404(
        PurchaseOrder.objects.select_related("vendor").prefetch_related("items__product"),
        pk=po_id,
    )

    if purchase_order.status != PurchaseOrder.STATUS_PRICE_MISMATCH:
        messages.error(request, "This PO does not have a price-only mismatch to confirm.")
        return redirect("purchases:po_list")

    validation_data, price_mismatches = get_validation_price_mismatches(purchase_order)
    if not price_mismatches:
        messages.error(request, "No price updates are available for confirmation.")
        return redirect("purchases:po_list")

    if not validation_data.get("can_update_prices"):
        messages.error(
            request,
            "Price updates cannot be confirmed while product or quantity mismatches still require manual review.",
        )
        return redirect("purchases:po_list")

    with transaction.atomic():
        updated_count = update_vendor_prices_from_validation(purchase_order, price_mismatches)
        if not updated_count:
            messages.error(request, "No matched vendor prices were available to update.")
            return redirect("purchases:po_list")

        now = timezone.now()
        purchase_order.status = PurchaseOrder.STATUS_CLOSED
        purchase_order.validation_note = PurchaseOrder.NOTE_PRICE_UPDATED_AND_CLOSED
        purchase_order.closed_at = now
        purchase_order.validated_at = now
        purchase_order.save()
        sync_all_po_analytics(purchase_order)

    messages.success(
        request,
        f"Vendor prices were updated from the invoice and {purchase_order.po_number} moved to History.",
    )
    return redirect("purchases:history_list")


@login_required
@require_POST
def delete_po(request, po_id):
    purchase_order = get_object_or_404(PurchaseOrder, pk=po_id)

    if purchase_order.is_closed:
        messages.error(request, "Closed POs cannot be deleted. They are permanently stored in History.")
        return redirect("purchases:po_list")

    po_number = purchase_order.po_number

    # Delete the invoice file from storage if one was uploaded
    if purchase_order.invoice_file:
        try:
            purchase_order.invoice_file.delete(save=False)
        except Exception:
            pass

    purchase_order.delete()

    messages.success(request, f"{po_number} has been permanently deleted.")
    return redirect("purchases:po_list")


@login_required
def history_list(request):
    search_query = (request.GET.get("q") or "").strip()
    selected_vendor_id = (request.GET.get("vendor") or "").strip()
    date_from = (request.GET.get("date_from") or "").strip()
    date_to = (request.GET.get("date_to") or "").strip()

    purchase_orders = (
        PurchaseOrder.objects.select_related("vendor", "created_by")
        .prefetch_related("items__product")
        .filter(status=PurchaseOrder.STATUS_CLOSED)
    )

    if search_query:
        purchase_orders = purchase_orders.filter(po_number__icontains=search_query)

    if selected_vendor_id.isdigit():
        purchase_orders = purchase_orders.filter(vendor_id=int(selected_vendor_id))

    if date_from:
        try:
            purchase_orders = purchase_orders.filter(closed_at__date__gte=date.fromisoformat(date_from))
        except ValueError:
            date_from = ""

    if date_to:
        try:
            purchase_orders = purchase_orders.filter(closed_at__date__lte=date.fromisoformat(date_to))
        except ValueError:
            date_to = ""

    purchase_orders = list(purchase_orders.order_by("-closed_at", "-created_at"))
    history_vendors = (
        PurchaseOrder.objects.filter(status=PurchaseOrder.STATUS_CLOSED)
        .select_related("vendor")
        .values("vendor_id", "vendor__name")
        .distinct()
        .order_by("vendor__name")
    )

    return render(
        request,
        "purchases/history_list.html",
        {
            "purchase_orders": prepare_purchase_orders(
                purchase_orders,
                include_invoice_form=False,
            ),
            "history_vendors": history_vendors,
            "search_query": search_query,
            "selected_vendor_id": selected_vendor_id,
            "date_from": date_from,
            "date_to": date_to,
        },
    )
