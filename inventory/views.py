from datetime import date

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from carting.selectors import get_open_cart_quantities

from .bulk_import import BulkImportError, REQUIRED_IMPORT_HEADERS, import_products_from_workbook
from .forms import ProductBulkImportForm, ProductForm, VendorForm, VendorProductPriceForm
from .models import PriceHistory, Product, Vendor, VendorProductPrice
from .product_aliases import sync_product_aliases


def _get_product_alias_input_values(request, product=None):
    if request.method == "POST":
        alias_values = request.POST.getlist("alternative_names[]")
    elif product is not None:
        alias_values = list(product.aliases.values_list("alias_name", flat=True))
    else:
        alias_values = []

    return alias_values or [""]


@login_required
def product_list(request):
    raw_search_query = request.GET.get("q")
    search_query = (raw_search_query or "").strip()
    selected_vendor_ids = []

    if raw_search_query is not None and not search_query:
        cleaned_query = request.GET.copy()
        cleaned_query.pop("q", None)
        redirect_url = reverse("inventory:product_list")

        if cleaned_query:
            redirect_url = f"{redirect_url}?{cleaned_query.urlencode()}"

        return redirect(redirect_url)

    for vendor_id in request.GET.getlist("vendor"):
        try:
            selected_vendor_ids.append(int(vendor_id))
        except (TypeError, ValueError):
            continue

    selected_vendor_ids = sorted(set(selected_vendor_ids))

    vendors = Vendor.objects.filter(is_active=True)

    vendor_prices_queryset = VendorProductPrice.objects.filter(is_active=True).select_related("vendor")

    if selected_vendor_ids:
        vendor_prices_queryset = vendor_prices_queryset.filter(vendor_id__in=selected_vendor_ids)

    products = Product.objects.filter(is_active=True)

    if search_query:
        products = products.filter(
            Q(display_name__icontains=search_query) |
            Q(product_name__icontains=search_query)
        )

    if selected_vendor_ids:
        products = products.annotate(
            matched_vendor_count=Count(
                "vendor_prices__vendor_id",
                filter=Q(
                    vendor_prices__is_active=True,
                    vendor_prices__vendor_id__in=selected_vendor_ids,
                ),
                distinct=True,
            )
        ).filter(matched_vendor_count=len(selected_vendor_ids))

    products = products.prefetch_related(
        Prefetch(
            "vendor_prices",
            queryset=vendor_prices_queryset,
            to_attr="active_vendor_prices",
        )
    )
    products = list(products)

    cart_quantities = get_open_cart_quantities(request.user)

    for product in products:
        for vendor_price in getattr(product, "active_vendor_prices", []):
            vendor_price.cart_quantity = cart_quantities.get(
                (product.id, vendor_price.vendor_id),
                0,
            )

    return render(
        request,
        "products/product_list.html",
        {
            "products": products,
            "vendors": vendors,
            "search_query": search_query,
            "selected_vendor_ids": selected_vendor_ids,
        },
    )


@login_required
def master_inventory(request):
    return render(request, "inventory/master_inventory.html")


@login_required
def price_tracker(request):
    selected_product_id = (request.GET.get("product") or "").strip()
    selected_vendor_id = (request.GET.get("vendor") or "").strip()
    date_from = (request.GET.get("date_from") or "").strip()
    date_to = (request.GET.get("date_to") or "").strip()

    history_entries = PriceHistory.objects.select_related("product", "vendor")

    if selected_product_id.isdigit():
        history_entries = history_entries.filter(product_id=int(selected_product_id))

    if selected_vendor_id.isdigit():
        history_entries = history_entries.filter(vendor_id=int(selected_vendor_id))

    if date_from:
        try:
            history_entries = history_entries.filter(date__gte=date.fromisoformat(date_from))
        except ValueError:
            date_from = ""

    if date_to:
        try:
            history_entries = history_entries.filter(date__lte=date.fromisoformat(date_to))
        except ValueError:
            date_to = ""

    products = Product.objects.filter(price_history__isnull=False).distinct().order_by("display_name")
    vendors = Vendor.objects.filter(price_history__isnull=False).distinct().order_by("name")

    return render(
        request,
        "inventory/price_tracker.html",
        {
            "history_entries": history_entries.order_by("-date", "-id"),
            "products": products,
            "vendors": vendors,
            "selected_product_id": selected_product_id,
            "selected_vendor_id": selected_vendor_id,
            "date_from": date_from,
            "date_to": date_to,
        },
    )


@login_required
def manage_products(request):
    return _render_manage_products(request)


def _render_manage_products(request, *, import_form=None, import_summary=None):
    products = Product.objects.all()
    return render(
        request,
        "inventory/manage_products.html",
        {
            "products": products,
            "import_form": import_form or ProductBulkImportForm(),
            "import_summary": import_summary,
            "bulk_import_headers": REQUIRED_IMPORT_HEADERS,
        },
    )


@login_required
@require_POST
def import_products_workbook(request):
    import_form = ProductBulkImportForm(request.POST, request.FILES)
    import_summary = None

    if import_form.is_valid():
        try:
            import_summary = import_products_from_workbook(import_form.cleaned_data["workbook"])
            import_form = ProductBulkImportForm()
        except BulkImportError as exc:
            import_form.add_error("workbook", str(exc))

    return _render_manage_products(
        request,
        import_form=import_form,
        import_summary=import_summary,
    )


@login_required
def add_product(request):
    if request.method == "POST":
        form = ProductForm(request.POST, request.FILES)
        alternative_name_values = _get_product_alias_input_values(request)
        if form.is_valid():
            with transaction.atomic():
                product = form.save()
                sync_product_aliases(product, alternative_name_values)
            return redirect("inventory:manage_products")
    else:
        form = ProductForm()
        alternative_name_values = _get_product_alias_input_values(request)

    return render(
        request,
        "inventory/product_form.html",
        {
            "form": form,
            "alternative_name_values": alternative_name_values,
            "page_heading": "Add Product",
            "submit_label": "Save Product",
        },
    )


@login_required
def edit_product(request, pk):
    product = get_object_or_404(Product, pk=pk)

    if request.method == "POST":
        form = ProductForm(request.POST, request.FILES, instance=product)
        alternative_name_values = _get_product_alias_input_values(request, product=product)
        if form.is_valid():
            with transaction.atomic():
                product = form.save()
                sync_product_aliases(product, alternative_name_values)
            return redirect("inventory:manage_products")
    else:
        form = ProductForm(instance=product)
        alternative_name_values = _get_product_alias_input_values(request, product=product)

    return render(
        request,
        "inventory/product_form.html",
        {
            "form": form,
            "alternative_name_values": alternative_name_values,
            "product": product,
            "page_heading": "Edit Product",
            "submit_label": "Update Product",
        },
    )


@login_required
@require_POST
def delete_product(request, pk):
    product = get_object_or_404(Product, pk=pk)
    product.delete()
    return redirect("inventory:manage_products")


@login_required
def manage_vendors(request):
    vendors = Vendor.objects.all()
    return render(
        request,
        "inventory/manage_vendors.html",
        {
            "vendors": vendors,
        },
    )


@login_required
def add_vendor(request):
    if request.method == "POST":
        form = VendorForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("inventory:manage_vendors")
    else:
        form = VendorForm()

    return render(
        request,
        "inventory/vendor_form.html",
        {
            "form": form,
            "page_heading": "Add Vendor",
            "submit_label": "Save Vendor",
        },
    )


@login_required
def edit_vendor(request, pk):
    vendor = get_object_or_404(Vendor, pk=pk)

    if request.method == "POST":
        form = VendorForm(request.POST, instance=vendor)
        if form.is_valid():
            form.save()
            return redirect("inventory:manage_vendors")
    else:
        form = VendorForm(instance=vendor)

    return render(
        request,
        "inventory/vendor_form.html",
        {
            "form": form,
            "vendor": vendor,
            "page_heading": "Edit Vendor",
            "submit_label": "Update Vendor",
        },
    )


@login_required
@require_POST
def delete_vendor(request, pk):
    vendor = get_object_or_404(Vendor, pk=pk)
    vendor.delete()
    return redirect("inventory:manage_vendors")


@login_required
def manage_vendor_prices(request):
    vendor_prices = VendorProductPrice.objects.select_related("product", "vendor")
    return render(
        request,
        "inventory/manage_vendor_prices.html",
        {
            "vendor_prices": vendor_prices,
        },
    )


@login_required
def add_vendor_price(request):
    if request.method == "POST":
        form = VendorProductPriceForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("inventory:manage_vendor_prices")
    else:
        form = VendorProductPriceForm()

    return render(
        request,
        "inventory/vendor_price_form.html",
        {
            "form": form,
            "page_heading": "Add Vendor Price",
            "submit_label": "Save Vendor Price",
        },
    )


@login_required
def edit_vendor_price(request, pk):
    vendor_price = get_object_or_404(VendorProductPrice, pk=pk)

    if request.method == "POST":
        form = VendorProductPriceForm(request.POST, instance=vendor_price)
        if form.is_valid():
            form.save()
            return redirect("inventory:manage_vendor_prices")
    else:
        form = VendorProductPriceForm(instance=vendor_price)

    return render(
        request,
        "inventory/vendor_price_form.html",
        {
            "form": form,
            "vendor_price": vendor_price,
            "page_heading": "Edit Vendor Price",
            "submit_label": "Update Vendor Price",
        },
    )


@login_required
@require_POST
def delete_vendor_price(request, pk):
    vendor_price = get_object_or_404(VendorProductPrice, pk=pk)
    vendor_price.delete()
    return redirect("inventory:manage_vendor_prices")
