from django.db import models


class QuantityChangeType(models.TextChoices):
    NONE = "none", "No Change"
    SHORTAGE_CONFIRMED = "shortage_confirmed", "Shortage Confirmed"
    FULL_QUANTITY_CONFIRMED = "full_quantity_confirmed", "Full Quantity Confirmed"
    EXTRA_QTY_ACCEPTED = "extra_qty_accepted", "Extra Quantity Accepted"
    EXTRA_QTY_REJECTED = "extra_qty_rejected", "Extra Quantity Rejected"


class PriceChangeType(models.TextChoices):
    NONE = "none", "No Change"
    PRICE_INCREASE_ACCEPTED = "price_increase_accepted", "Price Increase Accepted"
    PRICE_DECREASE_ACCEPTED = "price_decrease_accepted", "Price Decrease Accepted"
    INVOICE_PRICE_REJECTED = "invoice_price_rejected", "Invoice Price Rejected"


class PriceDirection(models.TextChoices):
    INCREASE = "increase", "Increase"
    DECREASE = "decrease", "Decrease"
