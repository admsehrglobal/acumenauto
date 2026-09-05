"""Forms del dashboard.

ScheduleForm parsea la lista de horas (ej: '1,8,20') y la valida contra el
formato que espera CrontabSchedule.hour (string CSV de enteros 0-23).
RecipientForm valida un email para agregar a la lista de destinatarios.
"""
from django import forms

from app.file_exceptions import ParsedUpload, ReportSpec, normalize_key
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


class FileExceptionKeyForm(forms.Form):
    """Add one File Exceptions entry by hand; one field per key column."""

    def __init__(self, spec: ReportSpec, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.spec = spec
        for i, column in enumerate(spec.columns, start=1):
            self.fields[f"key_{i}"] = forms.CharField(
                label=column,
                max_length=100,
                widget=forms.TextInput(
                    attrs={"class": _INPUT_CLASS, "placeholder": column}
                ),
            )

    def clean(self):
        """Build the normalised key. Every field is already required, so a
        missing one has reported itself under its own label by now."""
        data = super().clean()
        key = tuple(
            normalize_key(data.get(f"key_{i}", ""))
            for i in range(1, len(self.spec.columns) + 1)
        )
        if all(key):
            self.cleaned_key = key
        return data


class FileExceptionConfirmForm(forms.Form):
    """The keys read from an upload, carried to the confirming POST.

    The uploaded file cannot be read a second time - calamine consumes the
    stream - so the page that asks "add these?" hands the keys themselves back
    rather than the file. The counts travel with them so the message after the
    write says the same numbers the page showed.
    """

    filename = forms.CharField(max_length=255, widget=forms.HiddenInput)
    keys = forms.CharField(widget=forms.HiddenInput)
    blank = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    duplicates = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    too_long = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, spec: ReportSpec, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.spec = spec

    @staticmethod
    def pack(keys) -> str:
        return "\n".join("\t".join(key) for key in keys)

    def parsed(self) -> ParsedUpload:
        # maxsplit keeps a tab inside the last part from splitting the key.
        width = len(self.spec.columns)
        keys = [
            tuple(line.split("\t", width - 1))
            for line in self.cleaned_data["keys"].splitlines()
            if line
        ]
        return ParsedUpload(
            keys,
            self.cleaned_data["blank"],
            self.cleaned_data["duplicates"],
            self.cleaned_data["too_long"],
        )


class FileExceptionUploadForm(forms.Form):
    file = forms.FileField(
        label="Excel file",
        widget=forms.FileInput(
            attrs={
                "accept": ".xlsx,.xlsm,.xls",
                "class": "block w-full text-sm text-slate-600 file:mr-3 file:px-3 "
                         "file:py-1.5 file:rounded file:border file:border-slate-300 "
                         "file:bg-white file:text-sm hover:file:bg-slate-100",
            }
        ),
    )

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if not upload.name.lower().endswith((".xlsx", ".xlsm", ".xls")):
            raise forms.ValidationError(
                "Please upload an Excel file (.xlsx). A .csv has to be opened "
                "in Excel and saved as .xlsx first."
            )
        return upload
