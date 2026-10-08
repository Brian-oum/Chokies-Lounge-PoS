from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from Chokies.views import pin_login, pos_page

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("Chokies.urls")),
    path("manager/", include("Chokies.manager_urls")),
    path("login/", pin_login, name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", pos_page, name="pos"),
]