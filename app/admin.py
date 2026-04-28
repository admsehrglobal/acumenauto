from django.contrib import admin

from app.models import Recipient, Run


@admin.register(Recipient)
class RecipientAdmin(admin.ModelAdmin):
    list_display = ("email", "active", "created_at")
    list_filter = ("active",)
    search_fields = ("email",)


@admin.register(Run)
class RunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "status",
        "started_at",
        "finished_at",
        "attempt_number",
    )
    list_filter = ("status",)
    readonly_fields = (
        "started_at",
        "finished_at",
        "status",
        "filenames",
        "error_message",
        "attempt_number",
    )
