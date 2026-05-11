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


class ScheduleForm(forms.Form):
    hours = forms.CharField(
        label="Hours (UTC)",
        help_text="Hours of day separated by commas. e.g. '8' or '1,8,20'. Range 0-23.",
        max_length=200,
        widget=forms.TextInput(attrs={"class": _INPUT_CLASS, "id": "id_hours"}),
    )

    def clean_hours(self) -> str:
        raw = self.cleaned_data["hours"].replace(" ", "")
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
        unique_sorted = sorted(set(normalized))
        return ",".join(str(n) for n in unique_sorted)


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


class AppConfigForm(forms.ModelForm):
    class Meta:
        model = AppConfig
        fields = [
            "report_1_enabled",
            "report_2_enabled",
            "report_3_enabled",
            "vendor_authorization_accrual_chunks",
        ]
        widgets = {
            "report_1_enabled": forms.CheckboxInput(attrs={"class": _CHECKBOX_CLASS}),
            "report_2_enabled": forms.CheckboxInput(attrs={"class": _CHECKBOX_CLASS}),
            "report_3_enabled": forms.CheckboxInput(attrs={"class": _CHECKBOX_CLASS}),
            "vendor_authorization_accrual_chunks": forms.NumberInput(
                attrs={"class": _INPUT_CLASS, "min": 1, "max": 20}
            ),
        }

    def clean_vendor_authorization_accrual_chunks(self) -> int:
        n = self.cleaned_data["vendor_authorization_accrual_chunks"]
        if not 1 <= n <= 20:
            raise forms.ValidationError("Must be between 1 and 20.")
        return n
