from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from django.urls import reverse

from .models import EmailOTP
from .services import (
    generate_otp_for_email,
    invalidate_existing_otps,
    resend_otp,
    reset_user_password,
    verify_otp,
)


User = get_user_model()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AccountsViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="ibc-owner",
            email="owner@ibc.example",
            password="testpass123",
        )

    def test_landing_page_loads(self):
        response = self.client.get(reverse("accounts:landing"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Indian Biryani Company")
        self.assertContains(response, "Log In")
        self.assertContains(response, "Create Account")

    def test_logged_in_user_visiting_auth_pages_is_redirected_to_app(self):
        self.client.force_login(self.user)

        for route_name in (
            "accounts:landing",
            "accounts:login",
            "accounts:signup",
            "accounts:forgot_password",
        ):
            response = self.client.get(reverse(route_name))
            self.assertRedirects(response, reverse("inventory:product_list"))

    def test_login_page_loads(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Continue to OTP")
        self.assertContains(response, "Forgot password?")

    def test_signup_post_redirects_to_signup_otp_page(self):
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "email": "newuser@ibc.example",
                "password": "strong-pass-123",
                "confirm_password": "strong-pass-123",
            },
        )
        self.assertRedirects(response, reverse("accounts:signup_otp"))
        created_user = User.objects.get(email="newuser@ibc.example")
        self.assertFalse(created_user.is_active)
        self.assertTrue(created_user.check_password("strong-pass-123"))
        otp = EmailOTP.objects.get(email="newuser@ibc.example", purpose=EmailOTP.Purpose.SIGNUP)
        self.assertEqual(len(otp.code), 6)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            self.client.session["pending_auth_flow"],
            {
                "purpose": "signup",
                "email": "newuser@ibc.example",
                "otp_verified": False,
            },
        )

    def test_signup_otp_activates_inactive_user(self):
        self.client.post(
            reverse("accounts:signup"),
            {
                "email": "newuser@ibc.example",
                "password": "strong-pass-123",
                "confirm_password": "strong-pass-123",
            },
        )
        otp = EmailOTP.objects.get(email="newuser@ibc.example", purpose=EmailOTP.Purpose.SIGNUP)

        response = self.client.post(reverse("accounts:signup_otp"), {"otp": otp.code})

        self.assertRedirects(response, reverse("accounts:login"))
        activated_user = User.objects.get(email="newuser@ibc.example")
        otp.refresh_from_db()
        self.assertTrue(activated_user.is_active)
        self.assertTrue(otp.is_used)
        self.assertNotIn("pending_auth_flow", self.client.session)

    def test_invalid_signup_otp_does_not_activate_user(self):
        self.client.post(
            reverse("accounts:signup"),
            {
                "email": "newuser@ibc.example",
                "password": "strong-pass-123",
                "confirm_password": "strong-pass-123",
            },
        )

        response = self.client.post(reverse("accounts:signup_otp"), {"otp": "999999"})

        self.assertEqual(response.status_code, 200)
        pending_user = User.objects.get(email="newuser@ibc.example")
        self.assertFalse(pending_user.is_active)

    def test_expired_signup_otp_does_not_activate_user(self):
        self.client.post(
            reverse("accounts:signup"),
            {
                "email": "newuser@ibc.example",
                "password": "strong-pass-123",
                "confirm_password": "strong-pass-123",
            },
        )
        otp = EmailOTP.objects.get(email="newuser@ibc.example", purpose=EmailOTP.Purpose.SIGNUP)
        otp.expires_at = timezone.now() - timedelta(seconds=1)
        otp.save(update_fields=["expires_at"])

        response = self.client.post(reverse("accounts:signup_otp"), {"otp": otp.code})

        self.assertEqual(response.status_code, 200)
        pending_user = User.objects.get(email="newuser@ibc.example")
        self.assertFalse(pending_user.is_active)

    def test_duplicate_active_email_is_rejected(self):
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "email": self.user.email,
                "password": "strong-pass-123",
                "confirm_password": "strong-pass-123",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "An account with this email already exists.")
        self.assertFalse(
            EmailOTP.objects.filter(email=self.user.email, purpose=EmailOTP.Purpose.SIGNUP).exists()
        )

    def test_repeat_signup_reuses_existing_inactive_user(self):
        pending_user = User.objects.create_user(
            username="pending@ibc.example",
            email="pending@ibc.example",
            password="old-pass-123",
            is_active=False,
        )

        response = self.client.post(
            reverse("accounts:signup"),
            {
                "email": "pending@ibc.example",
                "password": "new-pass-123",
                "confirm_password": "new-pass-123",
            },
        )

        self.assertRedirects(response, reverse("accounts:signup_otp"))
        self.assertEqual(User.objects.filter(email="pending@ibc.example").count(), 1)
        pending_user.refresh_from_db()
        self.assertFalse(pending_user.is_active)
        self.assertTrue(pending_user.check_password("new-pass-123"))
        self.assertTrue(
            EmailOTP.objects.filter(email="pending@ibc.example", purpose=EmailOTP.Purpose.SIGNUP).exists()
        )

    def test_login_post_redirects_to_login_otp_page(self):
        response = self.client.post(
            reverse("accounts:login"),
            {
                "email": self.user.email,
                "password": "testpass123",
            },
        )
        self.assertRedirects(response, reverse("accounts:login_otp"))
        self.assertTrue(
            EmailOTP.objects.filter(email=self.user.email, purpose=EmailOTP.Purpose.LOGIN).exists()
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            self.client.session["pending_auth_flow"],
            {
                "purpose": "login",
                "email": self.user.email,
                "otp_verified": False,
            },
        )
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_correct_login_otp_logs_user_in(self):
        self.client.post(
            reverse("accounts:login"),
            {
                "email": self.user.email,
                "password": "testpass123",
            },
        )
        otp = EmailOTP.objects.get(email=self.user.email, purpose=EmailOTP.Purpose.LOGIN)

        response = self.client.post(reverse("accounts:login_otp"), {"otp": otp.code})

        self.assertRedirects(response, reverse("inventory:product_list"))
        otp.refresh_from_db()
        self.assertTrue(otp.is_used)
        self.assertEqual(str(self.user.pk), self.client.session.get("_auth_user_id"))
        self.assertNotIn("pending_auth_flow", self.client.session)

    def test_login_next_value_redirects_back_to_requested_page_after_otp(self):
        next_url = reverse("analytics:analytics_dashboard")
        response = self.client.post(
            reverse("accounts:login"),
            {
                "email": self.user.email,
                "password": "testpass123",
                "next": next_url,
            },
        )
        self.assertRedirects(response, reverse("accounts:login_otp"))
        otp = EmailOTP.objects.get(email=self.user.email, purpose=EmailOTP.Purpose.LOGIN)

        response = self.client.post(reverse("accounts:login_otp"), {"otp": otp.code})

        self.assertRedirects(response, next_url)

    def test_invalid_login_otp_does_not_log_user_in(self):
        self.client.post(
            reverse("accounts:login"),
            {
                "email": self.user.email,
                "password": "testpass123",
            },
        )

        response = self.client.post(reverse("accounts:login_otp"), {"otp": "999999"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The OTP you entered is not valid.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_expired_login_otp_does_not_log_user_in(self):
        self.client.post(
            reverse("accounts:login"),
            {
                "email": self.user.email,
                "password": "testpass123",
            },
        )
        otp = EmailOTP.objects.get(email=self.user.email, purpose=EmailOTP.Purpose.LOGIN)
        otp.expires_at = timezone.now() - timedelta(seconds=1)
        otp.save(update_fields=["expires_at"])

        response = self.client.post(reverse("accounts:login_otp"), {"otp": otp.code})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This OTP has expired.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_inactive_user_cannot_start_login_flow(self):
        inactive_user = User.objects.create_user(
            username="inactive-user",
            email="inactive@ibc.example",
            password="inactive-pass-123",
            is_active=False,
        )

        response = self.client.post(
            reverse("accounts:login"),
            {
                "email": inactive_user.email,
                "password": "inactive-pass-123",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "This account is inactive. Complete signup verification before logging in.",
        )
        self.assertFalse(
            EmailOTP.objects.filter(email=inactive_user.email, purpose=EmailOTP.Purpose.LOGIN).exists()
        )

    def test_wrong_password_does_not_send_login_otp(self):
        response = self.client.post(
            reverse("accounts:login"),
            {
                "email": self.user.email,
                "password": "wrong-pass-123",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a valid email and password.")
        self.assertFalse(
            EmailOTP.objects.filter(email=self.user.email, purpose=EmailOTP.Purpose.LOGIN).exists()
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_signup_otp_page_uses_pending_email(self):
        session = self.client.session
        session["pending_auth_flow"] = {
            "purpose": "signup",
            "email": "newuser@ibc.example",
        }
        session.save()

        response = self.client.get(reverse("accounts:signup_otp"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "newuser@ibc.example")
        self.assertContains(response, "Verify OTP")

    def test_reset_password_requires_pending_reset_flow(self):
        response = self.client.get(reverse("accounts:reset_password"))
        self.assertRedirects(response, reverse("accounts:forgot_password"))

    def test_forgot_password_post_redirects_to_otp_page(self):
        response = self.client.post(
            reverse("accounts:forgot_password"),
            {"email": self.user.email},
        )
        self.assertRedirects(response, reverse("accounts:forgot_password_otp"))
        self.assertTrue(
            EmailOTP.objects.filter(
                email=self.user.email,
                purpose=EmailOTP.Purpose.RESET_PASSWORD,
            ).exists()
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            self.client.session["pending_auth_flow"],
            {
                "purpose": "reset_password",
                "email": self.user.email,
                "otp_verified": False,
            },
        )

    def test_resend_otp_replaces_existing_signup_otp(self):
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "email": "newuser@ibc.example",
                "password": "strong-pass-123",
                "confirm_password": "strong-pass-123",
            },
        )
        self.assertRedirects(response, reverse("accounts:signup_otp"))

        first_otp = EmailOTP.objects.get(email="newuser@ibc.example", purpose=EmailOTP.Purpose.SIGNUP)
        response = self.client.post(reverse("accounts:signup_otp"), {"resend_otp": "1"})
        self.assertRedirects(response, reverse("accounts:signup_otp"))

        first_otp.refresh_from_db()
        latest_otp = EmailOTP.objects.filter(
            email="newuser@ibc.example",
            purpose=EmailOTP.Purpose.SIGNUP,
        ).first()
        self.assertNotEqual(first_otp.id, latest_otp.id)
        self.assertTrue(first_otp.is_expired)
        self.assertEqual(len(mail.outbox), 2)

    def test_reset_password_otp_verification_unlocks_reset_page(self):
        self.client.post(reverse("accounts:forgot_password"), {"email": self.user.email})
        otp = EmailOTP.objects.get(email=self.user.email, purpose=EmailOTP.Purpose.RESET_PASSWORD)

        response = self.client.post(reverse("accounts:forgot_password_otp"), {"otp": otp.code})
        self.assertRedirects(response, reverse("accounts:reset_password"))

        session = self.client.session
        self.assertTrue(session["pending_auth_flow"]["otp_verified"])

    def test_invalid_reset_otp_does_not_allow_password_reset(self):
        self.client.post(reverse("accounts:forgot_password"), {"email": self.user.email})

        response = self.client.post(reverse("accounts:forgot_password_otp"), {"otp": "999999"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The OTP you entered is not valid.")
        self.assertRedirects(
            self.client.get(reverse("accounts:reset_password")),
            reverse("accounts:forgot_password"),
        )

    def test_expired_reset_otp_does_not_allow_password_reset(self):
        self.client.post(reverse("accounts:forgot_password"), {"email": self.user.email})
        otp = EmailOTP.objects.get(email=self.user.email, purpose=EmailOTP.Purpose.RESET_PASSWORD)
        otp.expires_at = timezone.now() - timedelta(seconds=1)
        otp.save(update_fields=["expires_at"])

        response = self.client.post(reverse("accounts:forgot_password_otp"), {"otp": otp.code})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This OTP has expired.")
        self.assertRedirects(
            self.client.get(reverse("accounts:reset_password")),
            reverse("accounts:forgot_password"),
        )

    def test_password_is_changed_after_successful_reset(self):
        original_password = "testpass123"
        new_password = "new-reset-pass-123"
        self.assertTrue(self.user.check_password(original_password))

        self.client.post(reverse("accounts:forgot_password"), {"email": self.user.email})
        otp = EmailOTP.objects.get(email=self.user.email, purpose=EmailOTP.Purpose.RESET_PASSWORD)
        self.client.post(reverse("accounts:forgot_password_otp"), {"otp": otp.code})

        response = self.client.post(
            reverse("accounts:reset_password"),
            {
                "password": new_password,
                "confirm_password": new_password,
            },
        )

        self.assertRedirects(response, reverse("accounts:login"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new_password))
        self.assertFalse(self.user.check_password(original_password))

    def test_reset_session_state_is_cleared_after_successful_password_change(self):
        self.client.post(reverse("accounts:forgot_password"), {"email": self.user.email})
        otp = EmailOTP.objects.get(email=self.user.email, purpose=EmailOTP.Purpose.RESET_PASSWORD)
        self.client.post(reverse("accounts:forgot_password_otp"), {"otp": otp.code})

        response = self.client.post(
            reverse("accounts:reset_password"),
            {
                "password": "brand-new-pass-123",
                "confirm_password": "brand-new-pass-123",
            },
        )

        self.assertRedirects(response, reverse("accounts:login"))
        self.assertNotIn("pending_auth_flow", self.client.session)

    def test_logout_redirects_to_landing_and_revokes_protected_access(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse("accounts:logout"))

        self.assertRedirects(response, reverse("accounts:landing"))
        protected_response = self.client.get(reverse("inventory:product_list"))
        self.assertRedirects(
            protected_response,
            f"{reverse('accounts:login')}?next={reverse('inventory:product_list')}",
        )


class AuthProtectionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="protected-user",
            email="protected@ibc.example",
            password="testpass123",
        )

    def test_unauthenticated_access_to_protected_pages_redirects_to_login(self):
        protected_routes = [
            "inventory:master_inventory",
            "inventory:manage_products",
            "inventory:product_list",
            "inventory:price_tracker",
            "carting:cart_detail",
            "purchases:po_list",
            "purchases:history_list",
            "analytics:analytics_dashboard",
        ]

        for route_name in protected_routes:
            path = reverse(route_name)
            response = self.client.get(path)
            self.assertRedirects(response, f"{reverse('accounts:login')}?next={path}")

    def test_unauthenticated_access_to_protected_ajax_endpoint_redirects_to_login(self):
        path = reverse("analytics:product_price_trend_data")
        response = self.client.get(path, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{reverse('accounts:login')}?next={path}")

    def test_authenticated_users_can_access_protected_pages(self):
        self.client.force_login(self.user)

        for route_name in (
            "inventory:product_list",
            "carting:cart_detail",
            "purchases:po_list",
            "purchases:history_list",
            "analytics:analytics_dashboard",
        ):
            response = self.client.get(reverse(route_name))
            self.assertEqual(response.status_code, 200)

    def test_authenticated_user_can_access_protected_ajax_endpoint(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("analytics:product_price_trend_data"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)

    def test_old_password_no_longer_works_after_reset(self):
        self.client.post(reverse("accounts:forgot_password"), {"email": self.user.email})
        otp = EmailOTP.objects.get(email=self.user.email, purpose=EmailOTP.Purpose.RESET_PASSWORD)
        self.client.post(reverse("accounts:forgot_password_otp"), {"otp": otp.code})
        self.client.post(
            reverse("accounts:reset_password"),
            {
                "password": "brand-new-pass-123",
                "confirm_password": "brand-new-pass-123",
            },
        )

        old_password_response = self.client.post(
            reverse("accounts:login"),
            {
                "email": self.user.email,
                "password": "testpass123",
            },
        )
        new_password_response = self.client.post(
            reverse("accounts:login"),
            {
                "email": self.user.email,
                "password": "brand-new-pass-123",
            },
        )

        self.assertEqual(old_password_response.status_code, 200)
        self.assertContains(old_password_response, "Enter a valid email and password.")
        self.assertRedirects(new_password_response, reverse("accounts:login_otp"))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class OTPServiceTests(TestCase):
    def test_generate_otp_creates_6_digit_code_with_expiry(self):
        otp = generate_otp_for_email(email="chef@ibc.example", purpose=EmailOTP.Purpose.SIGNUP)
        self.assertEqual(len(otp.code), 6)
        self.assertTrue(otp.code.isdigit())
        self.assertFalse(otp.is_used)
        self.assertAlmostEqual(
            (otp.expires_at - otp.created_at).total_seconds(),
            600,
            delta=2,
        )

    def test_generate_otp_invalidates_older_unused_codes(self):
        older = generate_otp_for_email(email="chef@ibc.example", purpose=EmailOTP.Purpose.SIGNUP)
        newer = generate_otp_for_email(email="chef@ibc.example", purpose=EmailOTP.Purpose.SIGNUP)

        older.refresh_from_db()
        self.assertNotEqual(older.id, newer.id)
        self.assertTrue(older.is_expired)
        self.assertFalse(newer.is_expired)

    def test_invalidate_existing_otps_expires_active_codes(self):
        otp = generate_otp_for_email(email="chef@ibc.example", purpose=EmailOTP.Purpose.LOGIN)
        invalidate_existing_otps(email="chef@ibc.example", purpose=EmailOTP.Purpose.LOGIN)

        otp.refresh_from_db()
        self.assertTrue(otp.is_expired)

    def test_resend_otp_creates_new_code_and_sends_email(self):
        first_otp = generate_otp_for_email(email="chef@ibc.example", purpose=EmailOTP.Purpose.LOGIN)
        resent_otp = resend_otp(email="chef@ibc.example", purpose=EmailOTP.Purpose.LOGIN)

        first_otp.refresh_from_db()
        self.assertNotEqual(first_otp.id, resent_otp.id)
        self.assertTrue(first_otp.is_expired)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(resent_otp.code, mail.outbox[0].body)

    def test_verify_otp_succeeds_once(self):
        otp = generate_otp_for_email(email="chef@ibc.example", purpose=EmailOTP.Purpose.LOGIN)

        first_attempt = verify_otp(
            email="chef@ibc.example",
            purpose=EmailOTP.Purpose.LOGIN,
            code=otp.code,
        )
        second_attempt = verify_otp(
            email="chef@ibc.example",
            purpose=EmailOTP.Purpose.LOGIN,
            code=otp.code,
        )

        otp.refresh_from_db()
        self.assertTrue(first_attempt.success)
        self.assertEqual(first_attempt.reason, "verified")
        self.assertTrue(otp.is_used)
        self.assertEqual(otp.attempt_count, 1)
        self.assertFalse(second_attempt.success)
        self.assertEqual(second_attempt.reason, "missing")

    def test_verify_otp_fails_for_expired_code(self):
        otp = generate_otp_for_email(email="chef@ibc.example", purpose=EmailOTP.Purpose.LOGIN)
        otp.expires_at = timezone.now() - timedelta(seconds=1)
        otp.save(update_fields=["expires_at"])

        result = verify_otp(
            email="chef@ibc.example",
            purpose=EmailOTP.Purpose.LOGIN,
            code=otp.code,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "expired")

    def test_verify_otp_fails_for_invalid_code_and_tracks_attempts(self):
        otp = generate_otp_for_email(email="chef@ibc.example", purpose=EmailOTP.Purpose.LOGIN)

        result = verify_otp(
            email="chef@ibc.example",
            purpose=EmailOTP.Purpose.LOGIN,
            code="999999",
        )

        otp.refresh_from_db()
        self.assertFalse(result.success)
        self.assertEqual(result.reason, "invalid")
        self.assertEqual(otp.attempt_count, 1)
        self.assertFalse(otp.is_used)

    def test_reset_user_password_updates_hashed_password(self):
        user = User.objects.create_user(
            username="reset-user",
            email="reset@ibc.example",
            password="old-pass-123",
        )

        reset_user_password(email=user.email, password="new-pass-123")

        user.refresh_from_db()
        self.assertTrue(user.check_password("new-pass-123"))
        self.assertFalse(user.check_password("old-pass-123"))
