from django.contrib import admin

from app.models import FileException, Recipient, Run


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
        "exceptions_summary",
    )


@admin.register(FileException)
class FileExceptionAdmin(admin.ModelAdmin):
    list_display = (
        "report",
        "key_1",
        "key_2",
        "created_by",
        "created_at",
        "removed_by",
        "removed_at",
    )
    list_filter = ("report",)
    search_fields = ("key_1", "key_2")
