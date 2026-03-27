from django.conf import settings
from django.contrib.auth import get_user_model, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import (
    EmailLoginForm,
    ForgotPasswordForm,
    OTPVerificationForm,
    ResetPasswordForm,
    SignupForm,
)
from .models import EmailOTP
from .services import (
    activate_signup_user,
    prepare_inactive_signup_user,
    resend_otp,
    reset_user_password,
    verify_otp,
)


PENDING_AUTH_SESSION_KEY = "pending_auth_flow"
User = get_user_model()


def _redirect_authenticated_user(request):
    if request.user.is_authenticated:
        return redirect("inventory:product_list")
    return None


def _store_pending_auth_flow(request, *, purpose, email):
    request.session[PENDING_AUTH_SESSION_KEY] = {
        "purpose": purpose,
        "email": email,
        "otp_verified": False,
    }


def _get_pending_auth_flow(request, expected_purpose):
    pending_flow = request.session.get(PENDING_AUTH_SESSION_KEY, {})
    if pending_flow.get("purpose") != expected_purpose:
        return None
    return pending_flow


def _mark_pending_auth_flow_verified(request, *, purpose):
    pending_flow = _get_pending_auth_flow(request, purpose)
    if not pending_flow:
        return
    pending_flow["otp_verified"] = True
    request.session[PENDING_AUTH_SESSION_KEY] = pending_flow


def _clear_pending_auth_flow(request):
    request.session.pop(PENDING_AUTH_SESSION_KEY, None)


def _render_auth_page(request, template_name, context):
    return render(request, template_name, context)


def _get_login_user_for_email(email):
    return User.objects.filter(email__iexact=email, is_active=True).order_by("id").first()


def _clean_next_url(request, next_url):
    candidate = (next_url or "").strip()
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return ""


def _get_requested_next_url(request):
    return _clean_next_url(
        request,
        request.POST.get("next") or request.GET.get("next"),
    )


def _resolve_login_success_redirect(request, pending_flow, fallback):
    next_url = _clean_next_url(request, (pending_flow or {}).get("next_url"))
    return next_url or fallback


def _get_default_auth_backend():
    backends = getattr(settings, "AUTHENTICATION_BACKENDS", None) or [
        "django.contrib.auth.backends.ModelBackend"
    ]
    return backends[0]


def _redirect_for_missing_pending_flow(request, *, message, redirect_to):
    messages.info(request, message)
    return redirect(redirect_to)


def landing_view(request):
    redirect_response = _redirect_authenticated_user(request)
    if redirect_response:
        return redirect_response

    return _render_auth_page(
        request,
        "accounts/landing.html",
        {
            "page_title": "Indian Biryani Company",
            "page_subtitle": "Secure procurement access with email-first authentication and OTP verification.",
        },
    )


def login_view(request):
    redirect_response = _redirect_authenticated_user(request)
    if redirect_response:
        return redirect_response

    form = EmailLoginForm(request.POST or None)
    next_value = _get_requested_next_url(request)
    if request.method == "POST" and form.is_valid():
        resend_otp(email=form.cleaned_data["email"], purpose=EmailOTP.Purpose.LOGIN)
        _store_pending_auth_flow(request, purpose="login", email=form.cleaned_data["email"])
        if next_value:
            pending_flow = request.session.get(PENDING_AUTH_SESSION_KEY, {})
            pending_flow["next_url"] = next_value
            request.session[PENDING_AUTH_SESSION_KEY] = pending_flow
        messages.success(request, "A 6-digit login OTP has been sent to your email.")
        return redirect("accounts:login_otp")

    return _render_auth_page(
        request,
        "accounts/login.html",
        {
            "form": form,
            "next_value": next_value,
            "page_title": "Log in to IBC",
            "page_subtitle": "Use your email and password first, then confirm access with a 6-digit OTP.",
        },
    )


def signup_view(request):
    redirect_response = _redirect_authenticated_user(request)
    if redirect_response:
        return redirect_response

    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            prepare_inactive_signup_user(
                email=form.cleaned_data["email"],
                password=form.cleaned_data["password"],
            )
        except ValueError as exc:
            form.add_error("email", str(exc))
        else:
            resend_otp(
                email=form.cleaned_data["email"],
                purpose=EmailOTP.Purpose.SIGNUP,
            )
            _store_pending_auth_flow(
                request,
                purpose="signup",
                email=form.cleaned_data["email"],
            )
            messages.success(request, "A 6-digit signup OTP has been sent to your email.")
            return redirect("accounts:signup_otp")

    return _render_auth_page(
        request,
        "accounts/signup.html",
        {
            "form": form,
            "page_title": "Create your IBC account",
            "page_subtitle": "Start with email and password, then verify your account with a one-time code.",
        },
    )


def _handle_otp_view(request, *, purpose, heading, subtitle, success_message, success_redirect):
    redirect_response = _redirect_authenticated_user(request)
    if redirect_response:
        return redirect_response

    pending_flow = _get_pending_auth_flow(request, purpose)
    if not pending_flow:
        redirect_map = {
            "signup": "accounts:signup",
            "login": "accounts:login",
            "reset_password": "accounts:forgot_password",
        }
        return _redirect_for_missing_pending_flow(
            request,
            message="Start this flow from the matching auth page before entering an OTP.",
            redirect_to=redirect_map[purpose],
        )

    form = OTPVerificationForm(request.POST or None)

    if request.method == "POST":
        if "resend_otp" in request.POST:
            resend_otp(email=pending_flow["email"], purpose=purpose)
            messages.success(request, "A new 6-digit OTP has been sent to your email.")
            return redirect(request.path)
        if form.is_valid():
            verification = verify_otp(
                email=pending_flow["email"],
                purpose=purpose,
                code=form.cleaned_data["otp"],
            )
            if verification.success:
                if purpose == EmailOTP.Purpose.SIGNUP:
                    try:
                        activate_signup_user(email=pending_flow["email"])
                    except ValueError as exc:
                        messages.error(request, str(exc))
                        return redirect("accounts:signup")
                    _clear_pending_auth_flow(request)
                elif purpose == EmailOTP.Purpose.LOGIN:
                    login_user = _get_login_user_for_email(pending_flow["email"])
                    if login_user is None:
                        _clear_pending_auth_flow(request)
                        messages.error(
                            request,
                            "This account is no longer available for login. Try again from the login page.",
                        )
                        return redirect("accounts:login")
                    auth_login(request, login_user, backend=_get_default_auth_backend())
                    success_redirect = _resolve_login_success_redirect(
                        request,
                        pending_flow,
                        success_redirect,
                    )
                    _clear_pending_auth_flow(request)
                elif purpose == EmailOTP.Purpose.RESET_PASSWORD:
                    _mark_pending_auth_flow_verified(request, purpose=purpose)
                else:
                    _clear_pending_auth_flow(request)
                messages.success(request, success_message)
                return redirect(success_redirect)
            messages.error(request, verification.message)

    return _render_auth_page(
        request,
        "accounts/verify_otp.html",
        {
            "form": form,
            "page_title": heading,
            "page_subtitle": subtitle,
            "otp_purpose": purpose,
            "otp_email": pending_flow.get("email") if pending_flow else "",
            "otp_resend_available": True,
        },
    )


def signup_otp_view(request):
    return _handle_otp_view(
        request,
        purpose=EmailOTP.Purpose.SIGNUP,
        heading="Verify your signup OTP",
        subtitle="Enter the 6-digit code sent to your email to activate your account.",
        success_message="Your account is now active. Log in to continue.",
        success_redirect="accounts:login",
    )


def login_otp_view(request):
    return _handle_otp_view(
        request,
        purpose=EmailOTP.Purpose.LOGIN,
        heading="Verify your login OTP",
        subtitle="Enter the 6-digit code sent to your email to complete sign-in.",
        success_message="Login successful. Welcome back to IBC.",
        success_redirect="inventory:product_list",
    )


def forgot_password_view(request):
    redirect_response = _redirect_authenticated_user(request)
    if redirect_response:
        return redirect_response

    form = ForgotPasswordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        resend_otp(
            email=form.cleaned_data["email"],
            purpose=EmailOTP.Purpose.RESET_PASSWORD,
        )
        _store_pending_auth_flow(
            request,
            purpose="reset_password",
            email=form.cleaned_data["email"],
        )
        messages.success(request, "A 6-digit password-reset OTP has been sent to your email.")
        return redirect("accounts:forgot_password_otp")

    return _render_auth_page(
        request,
        "accounts/forgot_password.html",
        {
            "form": form,
            "page_title": "Forgot your password?",
            "page_subtitle": "We will verify your email with an OTP before allowing a password reset.",
        },
    )


def forgot_password_otp_view(request):
    return _handle_otp_view(
        request,
        purpose=EmailOTP.Purpose.RESET_PASSWORD,
        heading="Verify your reset OTP",
        subtitle="Enter the 6-digit code sent to your email to continue to password reset.",
        success_message="OTP verified. You can now choose a new password.",
        success_redirect="accounts:reset_password",
    )


def reset_password_view(request):
    redirect_response = _redirect_authenticated_user(request)
    if redirect_response:
        return redirect_response

    pending_flow = _get_pending_auth_flow(request, "reset_password")
    if not pending_flow or not pending_flow.get("otp_verified"):
        return _redirect_for_missing_pending_flow(
            request,
            message="Verify your reset OTP before setting a new password.",
            redirect_to="accounts:forgot_password",
        )

    form = ResetPasswordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            reset_user_password(
                email=pending_flow["email"],
                password=form.cleaned_data["password"],
            )
        except ValueError as exc:
            _clear_pending_auth_flow(request)
            messages.error(request, str(exc))
            return redirect("accounts:forgot_password")

        _clear_pending_auth_flow(request)
        messages.success(request, "Your password has been updated. Log in with your new password.")
        return redirect("accounts:login")

    return _render_auth_page(
        request,
        "accounts/reset_password.html",
        {
            "form": form,
            "page_title": "Set a new password",
            "page_subtitle": "Choose a new password after OTP verification.",
        },
    )


@login_required
@require_POST
def logout_view(request):
    auth_logout(request)
    return redirect("accounts:landing")
