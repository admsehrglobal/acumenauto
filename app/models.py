from django.db import models
from django.utils import timezone

from app.file_exceptions import REPORTS


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
    # What the File Exceptions lists removed from the emailed files, e.g.
    # "Invoices: 12 rows dropped (3 of 40 keys matched)". Empty when no list
    # applied to the files of this run.
    exceptions_summary = models.TextField(blank=True, default="")

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


class FileException(models.Model):
    """One key the run drops from an emailed file (Paul, 2026-08-31 / 09-02).

    `report` says which file; `key_1`/`key_2` hold the key parts in the order of
    `app.file_exceptions.REPORTS[report].columns` (invoices use only key_1).
    Keys are stored already normalised, so the unique constraint and the match
    at run time agree on what "the same key" is.

    A removed entry is not deleted: it keeps its row with `removed_at`/
    `removed_by` set. That is the record of changes Rob asked for, and it is
    what lets a removal be undone ("Restore"). Adding a key that was removed
    earlier restores that row.
    """

    report = models.CharField(
        max_length=20, choices=[(s.slug, s.label) for s in REPORTS.values()]
    )
    key_1 = models.CharField(max_length=100)
    key_2 = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.CharField(max_length=150, blank=True, default="")
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.CharField(max_length=150, blank=True, default="")
    # What the last run that read this file did with this entry. Both are
    # needed: without `last_checked_at` there is no telling "matched nothing"
    # apart from "no run has opened that file since you added it". A key that
    # is checked and never matches is a typo, and until now nothing said so.
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_matched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["report", "key_1", "key_2"], name="uniq_file_exception_key"
            )
        ]
        ordering = ["key_1", "key_2"]

    def __str__(self) -> str:
        return f"{self.report}: {self.key_display}"

    @property
    def active(self) -> bool:
        return self.removed_at is None

    @property
    def key_display(self) -> str:
        return f"{self.key_1} / {self.key_2}" if self.key_2 else self.key_1


class FileExceptionChange(models.Model):
    """One row per change to an entry. Never updated, never deleted.

    Ev promised Rob in writing on 2026-08-31: "keep a record of those changes so
    they can be reverted if needed". The entry itself could not be that record.
    It carried a single `created_at`/`removed_at` pair, so any key touched twice
    lost its history: add a key, remove it, add it again, and the removal was
    overwritten — the page then showed the key as freshly added, with nothing
    saying it had ever been taken off the list.

    Append-only is the point. The entry still says what is true NOW (`active`,
    who removed it, what the last run did with it); this says what happened, in
    order. `report` is denormalised so the page reads one report's history
    without a join.
    """

    ADDED = "added"
    REMOVED = "removed"
    RESTORED = "restored"
    ACTIONS = [(ADDED, "Added"), (REMOVED, "Removed"), (RESTORED, "Restored")]

    entry = models.ForeignKey(
        FileException, on_delete=models.CASCADE, related_name="changes"
    )
    report = models.CharField(max_length=20)
    action = models.CharField(max_length=10, choices=ACTIONS)
    at = models.DateTimeField(default=timezone.now)
    by = models.CharField(max_length=150, blank=True, default="")

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["report", "-at"])]

    def __str__(self) -> str:
        return f"{self.entry.key_display} {self.action} {self.at:%Y-%m-%d %H:%M}"


class PaSchedule(models.Model):
    """What the accrual matrix export does not carry, per PA number.

    Since 2026-09-03 the accrual file is rebuilt from the report's other tab
    (see `app.accrual_rebuild`), and that export has the PA number, the week and
    the amount but not the client id or the authorization's own dates. Those are
    kept here instead, because no single source has them all:

    - the authorization report, refreshed on every run that downloads it, holds
      only CURRENT authorizations — 63% of the PAs in the accrual data are not
      in it;
    - the historical ones come from the last accrual file produced before the
      portal changed, loaded once with `seed_pa_schedules`.

    Together they covered 4,066 of 4,079 PAs in a measured quarter. Rows for a
    PA that is in neither still go out, with these three columns empty and the
    count reported.
    """

    pa_number = models.CharField(max_length=64, primary_key=True)
    client_dddid = models.CharField(max_length=64, blank=True, default="")
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    # Which of the two sources last wrote this row, so a stale seeded value is
    # tellable from one the authorization report confirmed today.
    source = models.CharField(max_length=16, default="auth_report")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["pa_number"]

    def __str__(self) -> str:
        return f"PA {self.pa_number}"
