from django.contrib.auth import views as auth_views
from django.urls import path

from app import views

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", views.dashboard, name="dashboard"),
    path("runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("run-now/", views.run_now, name="run_now"),
    path("settings/", views.settings_view, name="settings"),
]
