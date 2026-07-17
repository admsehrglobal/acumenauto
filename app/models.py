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
            "Starting number of date-range chunks for the chunked reports "
            "(Vendor Payment Activity and Vendor Authorization Accrual "
            "Balances). This is a floor: any chunk that approaches Acumen's "
            "150k row export cap is auto-subdivided, so you rarely need to "
            "change it."
        ),
    )

    # --- DCI portal credentials (editable from /settings, no redeploy needed) ---
    # Username is stored in clear (not secret). Password is stored encrypted with
    # Fernet (see app/crypto.py) in dci_password_encrypted and accessed through the
    # `dci_password` property. Both empty by default -> the scraper falls back to
    # the env credentials (settings.DCI_USERNAME/DCI_PASSWORD).
    dci_username = models.CharField(max_length=255, blank=True, default="")
    dci_password_encrypted = models.TextField(blank=True, default="")

    class TestStatus(models.TextChoices):
        UNTESTED = "untested", "Untested"
        RUNNING = "running", "Testing"
        OK = "ok", "OK"
        FAILED = "failed", "Failed"

    # Result of the last "Test connection" (written by app.tasks.test_dci_login;
    # shown on /settings).
    dci_test_status = models.CharField(
        max_length=20, choices=TestStatus.choices, default=TestStatus.UNTESTED
    )
    dci_test_message = models.TextField(blank=True, default="")
    dci_test_at = models.DateTimeField(null=True, blank=True)

    @property
    def dci_password(self) -> str:
        """Decrypted DCI password. '' if none is stored or it can't be decrypted
        (missing/rotated key) -> the caller falls back to the env credentials."""
        if not self.dci_password_encrypted:
            return ""
        from app import crypto

        try:
            return crypto.decrypt(self.dci_password_encrypted)
        except crypto.EncryptionUnavailable:
            return ""

    @dci_password.setter
    def dci_password(self, raw: str) -> None:
        """Encrypt and store it (or clear it if raw is empty). Raises
        EncryptionUnavailable if FIELD_ENCRYPTION_KEY is unset (check is_available
        first)."""
        from app import crypto

        self.dci_password_encrypted = crypto.encrypt(raw) if raw else ""

    def effective_dci_credentials(self) -> tuple[str, str]:
        """(username, password) as an ATOMIC pair: if a complete pair is stored in
        the DB use it, otherwise fall back to the env pair. Never mixes sources (a
        rotated key or a half-filled form can't produce a mismatched pair)."""
        from django.conf import settings

        if self.dci_username and self.dci_password:
            return self.dci_username, self.dci_password
        return settings.DCI_USERNAME, settings.DCI_PASSWORD

    @classmethod
    def load(cls) -> "AppConfig":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs) -> None:
        self.pk = 1  # enforce singleton
        super().save(*args, **kwargs)
