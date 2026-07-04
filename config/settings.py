"""Django settings for the CIndex project."""

from __future__ import annotations

import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Any

import structlog
from django.core.exceptions import AppRegistryNotReady, ImproperlyConfigured
from django.db import OperationalError, ProgrammingError
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class AppSettings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    debug: bool = False
    secret_key: str = ""
    allowed_hosts: str = "localhost,127.0.0.1"
    database_url: str = "sqlite:///db.sqlite3"
    redis_url: str = "redis://redis:6379/0"
    search_final_top_k: int = 30
    search_query_freshness_days: int = 14
    exa_quota_sync_interval_seconds: int = 0
    local_import_directory: str = str(BASE_DIR / "local_imports")
    local_import_scan_interval_seconds: int = 30
    crossref_mailto: str = ""
    openalex_api_key: str = ""


APP = AppSettings()
IS_TESTING = "pytest" in sys.argv or bool(os.environ.get("PYTEST_CURRENT_TEST"))
USE_LOCAL_CACHE = IS_TESTING or APP.database_url.startswith("sqlite")


def _read_docker_secret(name: str) -> str | None:
    """Read a Docker secret from /run/secrets/{name}.txt or _FILE env var."""
    env_file = os.environ.get(f"{name.upper()}_FILE")
    path = env_file or f"/run/secrets/{name}.txt"
    try:
        return Path(path).read_text().strip()
    except FileNotFoundError:
        return None


def _load_secret_key() -> str:
    """Load SECRET_KEY from env, Docker secret, DB, or fail loudly."""
    if APP.secret_key:
        return APP.secret_key
    secret_from_file = _read_docker_secret("secret_key")
    if secret_from_file:
        return secret_from_file
    if IS_TESTING:
        return secrets.token_urlsafe(64)
    try:
        # lazy import: app registry not ready at settings load time
        from apps.core.models import StoredSecretKey  # noqa: PLC0415

        return StoredSecretKey.get_or_generate()
    except (ImportError, AppRegistryNotReady, OperationalError, ProgrammingError):
        pass
    if os.environ.get("DJANGO_SETTINGS_MODULE") or "manage.py" in sys.argv[0]:
        logging.getLogger(__name__).warning(
            "SECRET_KEY: DB unavailable, generating ephemeral key. "
            "Run 'manage.py generate_secret_key' or set DJANGO_SECRET_KEY.",
        )
        return secrets.token_urlsafe(64)
    msg = (
        "SECRET_KEY is not set. Provide it via .env, run 'manage.py "
        "generate_secret_key', or set DJANGO_SECRET_KEY environment variable."
    )
    raise ImproperlyConfigured(
        msg,
    )


SECRET_KEY = _load_secret_key()
SECRET_KEY_FALLBACKS: list[str] = []
DEBUG = APP.debug
ALLOWED_HOSTS = [h.strip() for h in APP.allowed_hosts.split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "django_structlog",
    "apps.core",
    "apps.users",
    "apps.articles",
    "apps.ingestion",
    "apps.search",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django_structlog.middlewares.RequestMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "apps.core.middleware.APICsrfExemptMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

if APP.database_url.startswith("postgres"):
    from urllib.parse import urlparse

    parsed = urlparse(APP.database_url)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": parsed.path[1:],
            "USER": parsed.username,
            "PASSWORD": parsed.password,
            "HOST": parsed.hostname,
            "PORT": parsed.port or 5432,
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
        },
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        },
    }

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
        ),
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ru"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "users.User"

if USE_LOCAL_CACHE:
    CACHES: dict[str, dict[str, Any]] = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "cindex-test-cache",
        },
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": APP.redis_url,
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
        },
    }

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {"anon": "60/min", "user": "600/min"},
    "EXCEPTION_HANDLER": "apps.core.exceptions.custom_exception_handler",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "CIndex API",
    "DESCRIPTION": "Scholarly citation search API",
    "VERSION": "1.0.0",
}

SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_CSP = {
    "default-src": "'self'",
    "script-src": "'self'",
    "style-src": "'self' 'unsafe-inline'",
    "img-src": "'self' data:",
    "font-src": "'self'",
    "connect-src": "'self'",
    "frame-ancestors": "'none'",
}
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

DJANGO_STRUCTLOG_CELERY_ENABLED = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json_formatter": {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": structlog.processors.JSONRenderer(),
            "foreign_pre_chain": [
                structlog.contextvars.merge_contextvars,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
            ],
        },
        "plain_console": {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": structlog.dev.ConsoleRenderer(),
            "foreign_pre_chain": [
                structlog.contextvars.merge_contextvars,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
            ],
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "plain_console" if DEBUG else "json_formatter",
        },
    },
    "loggers": {
        "django_structlog": {
            "handlers": ["console"],
            "level": "INFO",
        },
        "apps": {
            "handlers": ["console"],
            "level": "INFO",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

CELERY_BROKER_URL = APP.redis_url
CELERY_RESULT_BACKEND = APP.redis_url
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
