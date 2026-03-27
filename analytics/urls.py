from django.urls import path

from . import views

app_name = "analytics"

urlpatterns = [
    path("", views.dashboard_view, name="index"),
    path("dashboard/", views.dashboard_view, name="analytics_dashboard"),
    path("dashboard/product-price-trend-data/", views.product_price_trend_data_view, name="product_price_trend_data"),
]
