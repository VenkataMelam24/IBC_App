from dataclasses import dataclass
from datetime import timedelta
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.utils import timezone

from .models import EmailOTP


OTP_LENGTH = 6
User = get_user_model()


@dataclass(frozen=True)
class OTPVerificationResult:
    success: bool
    reason: str
    message: str
    otp: EmailOTP | None = None


def normalize_email(email):
    return (email or "").strip().lower()


def _get_signup_user_queryset(*, email):
    normalized_email = normalize_email(email)
    return User.objects.filter(email__iexact=normalized_email).order_by("id")


def _build_available_username(email, *, exclude_user_id=None):
    username_field_name = getattr(User, "USERNAME_FIELD", "username")
    username_field = User._meta.get_field(username_field_name)
    max_length = getattr(username_field, "max_length", 150) or 150
    base_username = normalize_email(email)[:max_length]
    candidate = base_username
    suffix_index = 2

    queryset = User.objects.all()
    if exclude_user_id is not None:
        queryset = queryset.exclude(pk=exclude_user_id)

    while queryset.filter(**{username_field_name: candidate}).exists():
        suffix = f"-{suffix_index}"
        candidate = f"{base_username[:max_length - len(suffix)]}{suffix}"
        suffix_index += 1

    return candidate


def get_otp_expiry_minutes():
    return getattr(settings, "ACCOUNTS_OTP_EXPIRY_MINUTES", 10)


def get_otp_expiry_delta():
    return timedelta(minutes=get_otp_expiry_minutes())


def _generate_numeric_code():
    return f"{secrets.randbelow(10**OTP_LENGTH):0{OTP_LENGTH}d}"


def invalidate_existing_otps(*, email, purpose):
    normalized_email = normalize_email(email)
    now = timezone.now()
    return EmailOTP.objects.filter(
        email__iexact=normalized_email,
        purpose=purpose,
        is_used=False,
        expires_at__gt=now,
    ).update(expires_at=now)


def generate_otp_for_email(*, email, purpose):
    normalized_email = normalize_email(email)
    invalidate_existing_otps(email=normalized_email, purpose=purpose)
    return EmailOTP.objects.create(
        email=normalized_email,
        code=_generate_numeric_code(),
        purpose=purpose,
        expires_at=timezone.now() + get_otp_expiry_delta(),
    )


def _get_otp_email_subject(purpose):
    return {
        EmailOTP.Purpose.SIGNUP: "Your IBC signup OTP",
        EmailOTP.Purpose.LOGIN: "Your IBC login OTP",
        EmailOTP.Purpose.RESET_PASSWORD: "Your IBC password reset OTP",
    }[purpose]


def _build_otp_email_message(otp):
    return (
        f"Your Indian Biryani Company OTP is {otp.code}.\n\n"
        f"Purpose: {otp.get_purpose_display()}\n"
        f"This code expires in {get_otp_expiry_minutes()} minutes.\n"
        "If you did not request this code, you can ignore this email."
    )


def send_otp_email(*, otp):
    return send_mail(
        subject=_get_otp_email_subject(otp.purpose),
        message=_build_otp_email_message(otp),
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@ibc.local"),
        recipient_list=[otp.email],
        fail_silently=False,
    )


def resend_otp(*, email, purpose):
    otp = generate_otp_for_email(email=email, purpose=purpose)
    send_otp_email(otp=otp)
    return otp


def prepare_inactive_signup_user(*, email, password):
    normalized_email = normalize_email(email)
    user = _get_signup_user_queryset(email=normalized_email).first()

    if user and user.is_active:
        raise ValueError("An active account with this email already exists.")

    created = user is None
    if created:
        user = User(email=normalized_email, is_active=False)

    username_field_name = getattr(User, "USERNAME_FIELD", "username")
    if hasattr(user, username_field_name):
        current_username = getattr(user, username_field_name, "")
        if created or not current_username:
            setattr(
                user,
                username_field_name,
                _build_available_username(normalized_email, exclude_user_id=user.pk),
            )

    user.email = normalized_email
    user.is_active = False
    user.set_password(password)
    user.save()
    return user, created


def activate_signup_user(*, email):
    normalized_email = normalize_email(email)
    user = _get_signup_user_queryset(email=normalized_email).first()
    if user is None:
        raise ValueError("No signup account is available for activation.")

    if not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active"])

    return user


def reset_user_password(*, email, password):
    normalized_email = normalize_email(email)
    user = User.objects.filter(email__iexact=normalized_email).order_by("id").first()
    if user is None:
        raise ValueError("No account is available for password reset.")

    user.set_password(password)
    user.save(update_fields=["password"])
    return user


def _get_latest_pending_otp(*, email, purpose):
    normalized_email = normalize_email(email)
    return (
        EmailOTP.objects.filter(
            email__iexact=normalized_email,
            purpose=purpose,
            is_used=False,
        )
        .order_by("-created_at")
        .first()
    )


def verify_otp(*, email, purpose, code):
    otp = _get_latest_pending_otp(email=email, purpose=purpose)
    if otp is None:
        return OTPVerificationResult(
            success=False,
            reason="missing",
            message="No active OTP is available for this email and flow.",
        )

    if otp.is_expired:
        return OTPVerificationResult(
            success=False,
            reason="expired",
            message="This OTP has expired. Request a new code and try again.",
        )

    otp.attempt_count += 1

    submitted_code = (code or "").strip()
    if submitted_code != otp.code:
        otp.save(update_fields=["attempt_count"])
        return OTPVerificationResult(
            success=False,
            reason="invalid",
            message="The OTP you entered is not valid.",
        )

    otp.is_used = True
    otp.save(update_fields=["attempt_count", "is_used"])
    return OTPVerificationResult(
        success=True,
        reason="verified",
        message="OTP verified successfully.",
        otp=otp,
    )
