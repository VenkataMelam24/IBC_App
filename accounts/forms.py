from django import forms
from django.contrib.auth import get_user_model


User = get_user_model()


class BaseStyledForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css_class = "auth-input"
            existing_class = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = f"{existing_class} {css_class}".strip()


class EmailLoginForm(BaseStyledForm):
    email = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"placeholder": "chef@ibc.example"}),
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={"placeholder": "Enter your password"}),
    )

    def clean(self):
        cleaned_data = super().clean()
        email = (cleaned_data.get("email") or "").strip().lower()
        password = cleaned_data.get("password")

        if not email or not password:
            return cleaned_data

        user = User.objects.filter(email__iexact=email).first()
        if user is None or not user.check_password(password):
            raise forms.ValidationError("Enter a valid email and password.")
        if not user.is_active:
            raise forms.ValidationError("This account is inactive. Complete signup verification before logging in.")

        cleaned_data["email"] = email
        cleaned_data["user"] = user
        return cleaned_data


class SignupForm(BaseStyledForm):
    email = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"placeholder": "owner@ibc.example"}),
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={"placeholder": "Create a password"}),
    )
    confirm_password = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"placeholder": "Re-enter your password"}),
    )

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        existing_user = User.objects.filter(email__iexact=email).order_by("id").first()
        if existing_user and existing_user.is_active:
            raise forms.ValidationError("An account with this email already exists.")
        self.existing_user = existing_user
        return email

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")
        if password and confirm_password and password != confirm_password:
            self.add_error("confirm_password", "Passwords do not match.")
        return cleaned_data


class OTPVerificationForm(BaseStyledForm):
    otp = forms.CharField(
        label="6-digit OTP",
        max_length=6,
        min_length=6,
        widget=forms.TextInput(
            attrs={
                "placeholder": "000000",
                "inputmode": "numeric",
                "autocomplete": "one-time-code",
            }
        ),
    )

    def clean_otp(self):
        otp = (self.cleaned_data.get("otp") or "").strip()
        if not otp.isdigit():
            raise forms.ValidationError("Enter the 6-digit OTP using numbers only.")
        return otp


class ForgotPasswordForm(BaseStyledForm):
    email = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"placeholder": "owner@ibc.example"}),
    )

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if not User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("No account was found for this email.")
        return email


class ResetPasswordForm(BaseStyledForm):
    password = forms.CharField(
        label="New password",
        widget=forms.PasswordInput(attrs={"placeholder": "Enter a new password"}),
    )
    confirm_password = forms.CharField(
        label="Confirm new password",
        widget=forms.PasswordInput(attrs={"placeholder": "Re-enter the new password"}),
    )

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")
        if password and confirm_password and password != confirm_password:
            self.add_error("confirm_password", "Passwords do not match.")
        return cleaned_data
