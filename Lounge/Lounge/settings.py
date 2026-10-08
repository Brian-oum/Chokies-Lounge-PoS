import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "Chokies",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "Lounge.urls"
WSGI_APPLICATION = "Lounge.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

# SQLite for development. Set POSTGRES_DB (and friends) for production.
if os.environ.get("POSTGRES_DB"):
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ["POSTGRES_DB"],
        "USER": os.environ.get("POSTGRES_USER", "postgres"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }}
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

AUTH_USER_MODEL = "chokies.User"
AUTH_PASSWORD_VALIDATORS = []  # tighten for production

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Nairobi"  # set to the restaurant's timezone
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "EXCEPTION_HANDLER": "Chokies.api.handlers.exception_handler",
}

# ---------------- POS business settings ----------------
# Allow stock to go below zero (warn, fix at next stock take) or hard-block.
ALLOW_NEGATIVE_STOCK = True
# "warn": menu API flags low/out of stock. "block": adding an item is refused when out of stock.
STOCK_AVAILABILITY_MODE = "warn"
# Menu item station -> inventory Location.code that its ingredients are drawn from.
STATION_LOCATIONS = {"KITCHEN": "kitchen", "BAR": "bar"}
# True: menu prices already include tax. False: tax is added on top.
PRICES_INCLUDE_TAX = True
# Service charge % added on the discounted net amount (not taxed). 0 disables it.
from decimal import Decimal as _D
SERVICE_CHARGE_PERCENT = _D("0")

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "pos"
LOGOUT_REDIRECT_URL = "login"
POS_LOGIN_URL = "/login/"