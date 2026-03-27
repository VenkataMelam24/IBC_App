from django.contrib import admin

from .models import EmailOTP


@admin.register(EmailOTP)
class EmailOTPAdmin(admin.ModelAdmin):
    list_display = ("email", "purpose", "code", "created_at", "expires_at", "is_used", "attempt_count")
    list_filter = ("purpose", "is_used", "created_at")
    search_fields = ("email", "code")
    readonly_fields = ("created_at",)
