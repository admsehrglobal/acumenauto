from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from app import views

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "password/change/",
        auth_views.PasswordChangeView.as_view(
            template_name="password_change.html",
            success_url=reverse_lazy("password_change_done"),
        ),
        name="password_change",
    ),
    path(
        "password/change/done/",
        auth_views.PasswordChangeDoneView.as_view(
            template_name="password_change_done.html",
        ),
        name="password_change_done",
    ),
    path("", views.dashboard, name="dashboard"),
    path("runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("runs/<int:pk>/report/", views.report_error, name="report_error"),
    path("run-now/", views.run_now, name="run_now"),
    path("settings/", views.settings_view, name="settings"),
    path("settings/recipients/add/", views.recipient_add, name="recipient_add"),
    path("settings/recipients/<int:pk>/delete/", views.recipient_delete, name="recipient_delete"),
    path("settings/recipients/<int:pk>/toggle/", views.recipient_toggle, name="recipient_toggle"),
    path("settings/reports/", views.app_config_update, name="app_config_update"),
]
