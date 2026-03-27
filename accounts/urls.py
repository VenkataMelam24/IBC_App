from django.urls import path

from . import views


app_name = "accounts"

urlpatterns = [
    path("", views.landing_view, name="landing"),
    path("login/", views.login_view, name="login"),
    path("login/verify-otp/", views.login_otp_view, name="login_otp"),
    path("logout/", views.logout_view, name="logout"),
    path("signup/", views.signup_view, name="signup"),
    path("signup/verify-otp/", views.signup_otp_view, name="signup_otp"),
    path("forgot-password/", views.forgot_password_view, name="forgot_password"),
    path("forgot-password/verify-otp/", views.forgot_password_otp_view, name="forgot_password_otp"),
    path("forgot-password/reset/", views.reset_password_view, name="reset_password"),
]
