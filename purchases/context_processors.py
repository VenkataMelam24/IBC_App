from .models import PurchaseOrder


def active_purchase_order_count(request):
    if not request.user.is_authenticated:
        return {"active_purchase_order_count": 0}

    return {
        "active_purchase_order_count": PurchaseOrder.objects.exclude(
            status=PurchaseOrder.STATUS_CLOSED
        ).count()
    }
