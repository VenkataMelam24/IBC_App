from decimal import Decimal

from django.db import models

from .currency import DEFAULT_CURRENCY, format_price_for_currency


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Vendor(TimeStampedModel):
    name = models.CharField(max_length=150)
    email = models.EmailField()
    whatsapp_number = models.CharField(max_length=20)
    address = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Product(TimeStampedModel):
    PACK_TYPE_CHOICES = [
        ("bag", "Bag"),
        ("tin", "Tin"),
        ("pack", "Pack"),
        ("box", "Box"),
        ("loose", "Loose"),
        ("other", "Other"),
    ]

    product_name = models.CharField(max_length=150)
    pack_type = models.CharField(max_length=20, choices=PACK_TYPE_CHOICES)
    quantity_per_pack = models.DecimalField(max_digits=10, decimal_places=2)
    quantity_unit = models.CharField(max_length=20)
    display_name = models.CharField(max_length=200, unique=True)
    image = models.ImageField(upload_to="products/", blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["product_name", "quantity_per_pack"]

    def __str__(self):
        return self.display_name


class ProductAlias(TimeStampedModel):
    product = models.ForeignKey(
        Product,
        related_name="aliases",
        on_delete=models.CASCADE,
    )
    alias_name = models.CharField(max_length=200)

    class Meta:
        ordering = ["alias_name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "alias_name"],
                name="inventory_productalias_unique_product_alias",
            )
        ]

    def __str__(self):
        return f"{self.product.display_name} - {self.alias_name}"


class PriceHistory(models.Model):
    SOURCE_MASTER_INVENTORY = "master_inventory"
    SOURCE_INVOICE_VALIDATION = "invoice_validation"
    CHANGE_SOURCE_CHOICES = [
        (SOURCE_MASTER_INVENTORY, "Master Inventory Update"),
        (SOURCE_INVOICE_VALIDATION, "Invoice Validation Update"),
    ]

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="price_history"
    )
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.CASCADE,
        related_name="price_history"
    )
    price = models.DecimalField(max_digits=10, decimal_places=2)
    previous_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    new_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    change_source = models.CharField(
        max_length=30,
        choices=CHANGE_SOURCE_CHOICES,
        default=SOURCE_MASTER_INVENTORY,
    )
    date = models.DateField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return (
            f"{self.date} - {self.product.display_name} - {self.vendor.name} - "
            f"{self.effective_new_price}"
        )

    @property
    def effective_new_price(self):
        return self.new_price if self.new_price is not None else self.price

    @property
    def has_previous_price(self):
        return self.previous_price is not None

    @property
    def difference(self):
        if self.previous_price is None or self.effective_new_price is None:
            return None
        return self.effective_new_price - self.previous_price

    @property
    def has_difference(self):
        return self.difference is not None

    @property
    def change_percentage(self):
        if self.previous_price in {None, Decimal("0.00")} or self.difference is None:
            return None
        return (self.difference / self.previous_price) * Decimal("100")

    @property
    def has_change_percentage(self):
        return self.change_percentage is not None


class VendorProductPrice(TimeStampedModel):
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.CASCADE,
        related_name="product_prices"
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="vendor_prices"
    )
    price = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=10, default=DEFAULT_CURRENCY)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["product__product_name", "vendor__name"]
        unique_together = ("vendor", "product")

    def __str__(self):
        return (
            f"{self.product.display_name} - {self.vendor.name} - "
            f"{self.formatted_price} {self.currency}"
        )

    @property
    def formatted_price(self):
        return format_price_for_currency(self.price, self.currency)

    def save(self, *args, **kwargs):
        change_source = kwargs.pop("change_source", PriceHistory.SOURCE_MASTER_INVENTORY)
        is_new = self.pk is None
        old_price = None

        if not is_new:
            old_price = type(self).objects.filter(pk=self.pk).values_list("price", flat=True).first()

        super().save(*args, **kwargs)

        if is_new or old_price != self.price:
            PriceHistory.objects.create(
                product=self.product,
                vendor=self.vendor,
                price=self.price,
                previous_price=old_price,
                new_price=self.price,
                change_source=change_source,
            )
