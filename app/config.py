import os

# -------------------------------------------------
# APP
# -------------------------------------------------
APP_NAME = "Gosonic MVP API"
APP_VERSION = "0.2.6"

# -------------------------------------------------
# ENV
# -------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

LOG_SENSITIVE_DATA = (
    os.getenv("LOG_SENSITIVE_DATA", "false").lower() == "true"
)

DATABASE_URL = os.getenv("DATABASE_URL")

SESSION_SECRET = os.getenv("SESSION_SECRET")

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

ADMIN_FULL_NAME = os.getenv(
    "ADMIN_FULL_NAME",
    "Gosonic Admin"
)

ADMIN_COMPANY_NAME = os.getenv(
    "ADMIN_COMPANY_NAME",
    "Gosonic"
)

WEBHOOK_SHARED_SECRET = os.getenv(
    "WEBHOOK_SHARED_SECRET"
)

RETELL_API_KEY = os.getenv("RETELL_API_KEY")

ALLOW_DB_INIT = (
    os.getenv("ALLOW_DB_INIT", "false").lower() == "true"
)

# -------------------------------------------------
# TWILIO
# -------------------------------------------------
TWILIO_SID = os.getenv("TWILIO_SID")

TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

TWILIO_PHONE = os.getenv("TWILIO_PHONE")

# -------------------------------------------------
# CORS
# -------------------------------------------------
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "https://client.gosonic.com"
    ).split(",")
    if origin.strip()
]

# -------------------------------------------------
# TIMEZONES
# -------------------------------------------------
SUPPORTED_TIMEZONES = {
    "America/New_York": "Eastern Time",
    "America/Chicago": "Central Time",
    "America/Denver": "Mountain Time",
    "America/Phoenix": "Mountain Time — Arizona",
    "America/Los_Angeles": "Pacific Time",
    "America/Anchorage": "Alaska Time",
    "Pacific/Honolulu": "Hawaii Time",
    "America/Toronto": "Eastern Time — Toronto",
    "America/Vancouver": "Pacific Time — Vancouver",
}

DEFAULT_CLIENT_TIMEZONE = os.getenv(
    "DEFAULT_CLIENT_TIMEZONE",
    "America/New_York"
)