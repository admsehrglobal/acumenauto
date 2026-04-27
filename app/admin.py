from django.contrib import admin

from app.models import Run


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
