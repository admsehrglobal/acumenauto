from django.db import models


class Recipient(models.Model):
    email = models.EmailField(unique=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["email"]

    def __str__(self) -> str:
        return self.email


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
    filenames = models.TextField(blank=True, default="")
    error_message = models.TextField(blank=True)
    attempt_number = models.PositiveIntegerField(default=1)

    def __str__(self) -> str:
        return f"Run {self.pk} ({self.status})"

    @property
    def file_names(self) -> list[str]:
        return [n for n in (self.filenames or "").split(";") if n]


class AppConfig(models.Model):
    """Singleton de config editable desde el dashboard. Para valores que
    cambian "live" (vs env vars que requieren redeploy)."""

    # Toggle por reporte. R1 y R2 vienen activos (es lo que el cliente ya
    # recibia); R3 chunked off-by-default — se activa solo cuando Paul lo
    # confirma porque el volumen del email cambia.
    report_1_enabled = models.BooleanField(default=True)
    report_2_enabled = models.BooleanField(default=True)
    report_3_enabled = models.BooleanField(default=False)

    date_range_chunks = models.PositiveSmallIntegerField(
        default=4,
        help_text=(
            "Number of date-range chunks for the chunked reports (Vendor "
            "Payment Activity and Vendor Authorization Accrual Balances). "
            "Workaround for Acumen's 150k row export cap."
        ),
    )

    @classmethod
    def load(cls) -> "AppConfig":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs) -> None:
        self.pk = 1  # enforce singleton
        super().save(*args, **kwargs)
