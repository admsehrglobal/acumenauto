from django.db import models


class Run(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    file_url = models.URLField(blank=True)
    error_message = models.TextField(blank=True)
    attempt_number = models.PositiveIntegerField(default=1)

    def __str__(self) -> str:
        return f"Run {self.pk} ({self.status})"
