"""Forms del dashboard.

ScheduleForm parsea la lista de horas (ej: '1,8,20') y la valida contra el
formato que espera CrontabSchedule.hour (string CSV de enteros 0-23).
RecipientForm valida un email para agregar a la lista de destinatarios.
"""
from django import forms

from app.models import AppConfig, Recipient


_INPUT_CLASS = (
    "w-full px-3 py-2 border border-slate-300 rounded "
    "focus:outline-none focus:ring-2 focus:ring-sky-400 font-mono"
)


def _validate_hours_csv(raw: str) -> str:
    """Parsea '1,8,20' a '1,8,20' normalizado (unique, sorted, 0-23)."""
    raw = raw.replace(" ", "")
    if not raw:
        raise forms.ValidationError("You must set at least one hour.")
    parts = raw.split(",")
    normalized = []
    for p in parts:
        try:
            n = int(p)
        except ValueError:
            raise forms.ValidationError(f"'{p}' is not an integer.")
        if not 0 <= n <= 23:
            raise forms.ValidationError(f"'{p}' is out of range (must be 0-23).")
        normalized.append(n)
    return ",".join(str(n) for n in sorted(set(normalized)))


class ScheduleForm(forms.Form):
    hours = forms.CharField(
        label="Hours (UTC)",
        help_text="Hours of day separated by commas. e.g. '8' or '1,8,20'. Range 0-23.",
        max_length=200,
        widget=forms.TextInput(attrs={"class": _INPUT_CLASS, "id": "id_hours"}),
    )

    def clean_hours(self) -> str:
        return _validate_hours_csv(self.cleaned_data["hours"])


class WeeklyScheduleForm(forms.Form):
    weekly_hours = forms.CharField(
        label="Hours (UTC)",
        max_length=200,
        widget=forms.TextInput(
            attrs={"class": _INPUT_CLASS, "id": "id_weekly_hours"}
        ),
    )

    def clean_weekly_hours(self) -> str:
        return _validate_hours_csv(self.cleaned_data["weekly_hours"])


class RecipientForm(forms.ModelForm):
    class Meta:
        model = Recipient
        fields = ["email"]
        widgets = {
            "email": forms.EmailInput(
                attrs={"class": _INPUT_CLASS, "placeholder": "name@example.com"}
            ),
        }


_CHECKBOX_CLASS = "h-4 w-4 rounded border-slate-300 text-sky-600 focus:ring-sky-400"


class DailyReportsConfigForm(forms.ModelForm):
    class Meta:
        model = AppConfig
        fields = ["report_1_enabled", "report_2_enabled"]
        widgets = {
            "report_1_enabled": forms.CheckboxInput(attrs={"class": _CHECKBOX_CLASS}),
            "report_2_enabled": forms.CheckboxInput(attrs={"class": _CHECKBOX_CLASS}),
        }


class WeeklyReportConfigForm(forms.ModelForm):
    class Meta:
        model = AppConfig
        fields = ["report_3_enabled", "date_range_chunks"]
        widgets = {
            "report_3_enabled": forms.CheckboxInput(attrs={"class": _CHECKBOX_CLASS}),
            "date_range_chunks": forms.NumberInput(
                attrs={"class": _INPUT_CLASS, "min": 1, "max": 20}
            ),
        }

    def clean_date_range_chunks(self) -> int:
        n = self.cleaned_data["date_range_chunks"]
        if not 1 <= n <= 20:
            raise forms.ValidationError("Must be between 1 and 20.")
        return n


class DCICredentialsForm(forms.ModelForm):
    """DCI portal credentials. The password field is optional: blank = keep the
    currently stored one (not re-prompted). When set, the model encrypts it via
    the `dci_password` property."""

    dci_password = forms.CharField(
        label="Password",
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": _INPUT_CLASS,
                "placeholder": "•••••••• (leave blank to keep current)",
                "autocomplete": "new-password",
                "id": "id_dci_password",
            }
        ),
        help_text="Leave blank to keep the current password.",
    )

    class Meta:
        model = AppConfig
        fields = ["dci_username"]
        widgets = {
            "dci_username": forms.TextInput(
                attrs={
                    "class": _INPUT_CLASS,
                    "autocomplete": "username",
                    "placeholder": "portal username",
                }
            ),
        }

    def save(self, commit: bool = True) -> AppConfig:
        obj = super().save(commit=False)
        raw = self.cleaned_data.get("dci_password")
        if raw:
            obj.dci_password = raw  # property setter -> cifra
        if commit:
            obj.save()
        return obj
