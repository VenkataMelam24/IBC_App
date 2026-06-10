from django.contrib.auth import get_user_model, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import EmailLoginForm

User = get_user_model()


def _redirect_authenticated_user(request):
    if request.user.is_authenticated:
        return redirect("inventory:product_list")
    return None


def _clean_next_url(request, next_url):
    candidate = (next_url or "").strip()
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return ""


def landing_view(request):
    if request.user.is_authenticated:
        return redirect("inventory:product_list")
    return render(request, "accounts/landing.html", {
        "page_title": "Indian Biryani Company",
        "page_subtitle": "Internal procurement tool.",
    })


def login_view(request):
    redirect_response = _redirect_authenticated_user(request)
    if redirect_response:
        return redirect_response

    form = EmailLoginForm(request.POST or None)
    next_value = _clean_next_url(
        request, request.POST.get("next") or request.GET.get("next")
    )

    if request.method == "POST" and form.is_valid():
        user = form.cleaned_data["user"]
        auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(request, "Welcome back to IBC.")
        return redirect(next_value or "inventory:product_list")

    return render(request, "accounts/login.html", {
        "form": form,
        "next_value": next_value,
        "page_title": "Log in to IBC",
        "page_subtitle": "Enter your email and password to continue.",
    })


@login_required
@require_POST
def logout_view(request):
    auth_logout(request)
    return redirect("accounts:landing")
