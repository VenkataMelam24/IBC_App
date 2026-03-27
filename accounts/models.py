from django.db import models
from django.utils import timezone


class EmailOTP(models.Model):
    class Purpose(models.TextChoices):
        SIGNUP = "signup", "Signup"
        LOGIN = "login", "Login"
        RESET_PASSWORD = "reset_password", "Reset Password"

    email = models.EmailField(db_index=True)
    code = models.CharField(max_length=6)
    purpose = models.CharField(max_length=32, choices=Purpose.choices, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    is_used = models.BooleanField(default=False, db_index=True)
    attempt_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["email", "purpose", "created_at"]),
            models.Index(fields=["email", "purpose", "is_used"]),
        ]

    def __str__(self):
        return f"{self.email} [{self.purpose}]"

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at
