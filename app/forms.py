"""Forms del dashboard.

ScheduleForm parsea la lista de horas (ej: '1,8,20') y la valida contra el
formato que espera CrontabSchedule.hour (string CSV de enteros 0-23).
"""
from django import forms


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
            raise forms.ValidationError("Tenes que poner al menos una hora.")
        parts = raw.split(",")
        normalized = []
        for p in parts:
            try:
                n = int(p)
            except ValueError:
                raise forms.ValidationError(f"'{p}' no es un numero entero.")
            if not 0 <= n <= 23:
                raise forms.ValidationError(f"'{p}' fuera de rango (debe ser 0-23).")
            normalized.append(n)
        unique_sorted = sorted(set(normalized))
        return ",".join(str(n) for n in unique_sorted)
