from fastapi import FastAPI, Request, Header, HTTPException, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from twilio.rest import Client
from psycopg.types.json import Jsonb
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import psycopg
import os
import time
import re
import logging
import json
import secrets
import hashlib

from app.config import (
    APP_NAME,
    APP_VERSION,
    LOG_LEVEL,
    LOG_SENSITIVE_DATA,
    CORS_ALLOWED_ORIGINS,
    SUPPORTED_TIMEZONES,
    DEFAULT_CLIENT_TIMEZONE,
    TWILIO_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_PHONE,
    DATABASE_URL,
    RETELL_API_KEY,
    WEBHOOK_SHARED_SECRET,
    ALLOW_DB_INIT,
)

from app.database import get_connection

from app.auth import (
    require_admin,
    hash_password,
    verify_password,
    create_session_token,
    require_auth_token,
    fallback_env_admin_user,
)

app = FastAPI(title=APP_NAME, version=APP_VERSION)

# -------------------------------------------------
# LOGGING
# -------------------------------------------------
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("gosonic")

# Keep third-party SDK/client logs quiet in production.
logging.getLogger("twilio").setLevel(logging.WARNING)
logging.getLogger("twilio.http_client").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# -------------------------------------------------
# PLATFORM TIMEZONES
# -------------------------------------------------
# Store IANA timezone names in the database. Display friendly labels in the dashboard.


def normalize_timezone(value: str):
    if value in SUPPORTED_TIMEZONES:
        return value
    return (
        DEFAULT_CLIENT_TIMEZONE
        if DEFAULT_CLIENT_TIMEZONE in SUPPORTED_TIMEZONES
        else "America/New_York"
    )


def timezone_label(value: str):
    return SUPPORTED_TIMEZONES.get(value, value or "Unknown")


# -------------------------------------------------
# CORS CONFIGURATION
# -------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# -------------------------------------------------
# ADMIN AUTH
# -------------------------------------------------


def require_webhook_secret(x_webhook_secret: str):
    """
    Optional shared-secret protection for Retell-facing webhooks.

    Set WEBHOOK_SHARED_SECRET in Render and pass the same value from Retell
    as the X-Webhook-Secret header. If WEBHOOK_SHARED_SECRET is not set,
    this check is skipped to avoid breaking the current MVP flow.
    """
    expected_secret = WEBHOOK_SHARED_SECRET

    if not expected_secret:
        return True

    if not x_webhook_secret or x_webhook_secret != expected_secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    return True


def verify_retell_signature(
    raw_body: str, signature: str, enforce_env: str = "RETELL_VERIFY_TRIAGE_SIGNATURE"
):
    """
    Verifies Retell-signed Custom Function requests using X-Retell-Signature.

    This is intentionally opt-in per endpoint. Set RETELL_VERIFY_TRIAGE_SIGNATURE=true
    only after confirming Retell is sending X-Retell-Signature to /webhook/triage.
    """
    should_enforce = os.getenv(enforce_env, "false").lower() == "true"

    if not should_enforce:
        return True

    api_key = RETELL_API_KEY

    if not api_key:
        logger.error("[RETELL SIGNATURE] RETELL_API_KEY not configured")
        raise HTTPException(
            status_code=500, detail="Retell verification not configured"
        )

    if not signature:
        logger.warning("[RETELL SIGNATURE] Missing X-Retell-Signature")
        raise HTTPException(status_code=401, detail="Missing Retell signature")

    try:
        from retell import Retell

        retell_client = Retell(api_key=api_key)
        valid_signature = retell_client.verify(
            raw_body,
            api_key=str(api_key),
            signature=str(signature),
        )

    except ImportError:
        logger.error("[RETELL SIGNATURE] retell package not installed")
        raise HTTPException(status_code=500, detail="Retell SDK not installed")

    except Exception:
        logger.exception("[RETELL SIGNATURE] Verification error")
        raise HTTPException(status_code=401, detail="Invalid Retell signature")

    if not valid_signature:
        logger.warning("[RETELL SIGNATURE] Invalid signature")
        raise HTTPException(status_code=401, detail="Invalid Retell signature")

    logger.info("[RETELL SIGNATURE] Verified")
    return True


def observe_retell_signature(
    raw_body: str, signature: str, label: str = "[RETELL SIGNATURE OBSERVE]"
):
    """
    Passive Retell signature check for endpoints we are not enforcing yet.

    This never blocks request processing. It only logs whether Retell is
    sending X-Retell-Signature and whether verification succeeds.
    """
    api_key = RETELL_API_KEY

    if not signature:
        logger.info("%s Missing X-Retell-Signature", label)
        return {"present": False, "valid": None, "reason": "missing_signature"}

    if not api_key:
        logger.warning("%s RETELL_API_KEY not configured", label)
        return {"present": True, "valid": None, "reason": "missing_api_key"}

    try:
        from retell import Retell

        retell_client = Retell(api_key=api_key)
        valid_signature = retell_client.verify(
            raw_body,
            api_key=str(api_key),
            signature=str(signature),
        )

    except ImportError:
        logger.error("%s retell package not installed", label)
        return {"present": True, "valid": None, "reason": "retell_sdk_missing"}

    except Exception:
        logger.exception("%s Verification error", label)
        return {"present": True, "valid": False, "reason": "verification_error"}

    if valid_signature:
        logger.info("%s Verified", label)
        return {"present": True, "valid": True, "reason": "verified"}

    logger.warning("%s Invalid signature", label)
    return {"present": True, "valid": False, "reason": "invalid_signature"}


def mask_phone(phone: str):
    if not phone:
        return None

    text = str(phone)
    digits = re.findall(r"\d", text)

    if len(digits) < 4:
        return "***"

    return f"***-***-{''.join(digits[-4:])}"


def mask_text(value: str, max_chars: int = 80):
    if not value:
        return None

    text = str(value).replace("\n", " ").strip()

    if LOG_SENSITIVE_DATA:
        return text[:max_chars]

    return "[redacted]"


def log_info(label: str, **fields):
    safe_fields = {}

    sensitive_keys = {
        "transcript",
        "full_transcript",
        "metadata",
        "custom_analysis",
        "client_settings",
        "service_address",
        "issue_description",
        "raw_payload",
        "caller_name",
    }

    for key, value in fields.items():
        key_lower = key.lower()

        if "phone" in key_lower or "number" in key_lower:
            safe_fields[key] = mask_phone(value)
        elif key_lower in sensitive_keys:
            safe_fields[key] = mask_text(value)
        else:
            safe_fields[key] = value

    logger.info("%s %s", label, safe_fields)


# -------------------------------------------------
# ENV / TWILIO SETUP
# -------------------------------------------------
twilio_client = None

if TWILIO_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)
    logger.info("Twilio client initialized")
else:
    logger.warning("Twilio not configured")


# -------------------------------------------------
# CLIENT MAP — FALLBACK ONLY
# -------------------------------------------------
CLIENTS = {
    "hvac_toronto_001": {
        "business_name": "Toronto HVAC",
        "business_phone": "+14383896310",
        "caller_enabled": True,
    }
}


# -------------------------------------------------
# STATE / DEDUP
# -------------------------------------------------
PROCESSED_CALLS = set()
PROCESSED_META = {}
PROCESSED_TTL = 60 * 10

CALL_PHONE_MAP = {}
CALL_PHONE_META = {}

# -------------------------------------------------
# PASSWORD + SESSION HELPERS
# -------------------------------------------------


def get_user_by_email(email: str):
    database_url = DATABASE_URL

    if not database_url or not email:
        return None

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        u.id,
                        u.client_key,
                        u.full_name,
                        u.email,
                        u.password_hash,
                        u.role,
                        u.status,
                        u.last_login_at,
                        c.business_name,
                        c.timezone
                    FROM users u
                    LEFT JOIN clients c ON c.client_key = u.client_key
                    WHERE LOWER(u.email) = LOWER(%s)
                    LIMIT 1;
                """,
                    (email,),
                )

                row = cur.fetchone()

        if not row:
            return None

        return {
            "user_id": row[0],
            "client_key": row[1],
            "full_name": row[2],
            "email": row[3],
            "password_hash": row[4],
            "role": row[5],
            "status": row[6],
            "last_login_at": row[7],
            "business_name": row[8],
            "timezone": row[9],
            "timezone_label": timezone_label(row[9]),
            "auth_source": "database",
        }

    except Exception as e:
        # Users table may not exist until the next /init-db migration is run.
        logger.warning("[AUTH DB LOOKUP SKIPPED] %s", str(e))
        return None


def update_user_last_login(user_id: int):
    database_url = os.getenv("DATABASE_URL")

    if not database_url or not user_id:
        return None

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE users
                    SET
                        last_login_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING last_login_at;
                """,
                    (user_id,),
                )

                row = cur.fetchone()

            conn.commit()

        return row[0] if row else None

    except Exception:
        logger.exception("[AUTH LAST LOGIN UPDATE FAILED]")
        return None


# -------------------------------------------------
# AUTH LOGIN ENDPOINT
# -------------------------------------------------
@app.post("/auth/login")
async def auth_login(request: Request):
    data = await request.json()

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user = get_user_by_email(email)

    if user:
        if user.get("status") != "active":
            raise HTTPException(status_code=403, detail="User account is inactive")

        if not verify_password(password, user.get("password_hash")):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        last_login_at = update_user_last_login(user.get("user_id"))
        if last_login_at:
            user["last_login_at"] = last_login_at

    else:
        # Temporary fallback for platform access until all dashboard users are migrated.
        admin_password = os.getenv("ADMIN_PASSWORD")
        user = fallback_env_admin_user(email)

        if not user or not admin_password or password != admin_password:
            raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_session_token(user)

    return {
        "status": "ok",
        "token_type": "Bearer",
        "expires_in_seconds": 43200,
        "token": token,
        "user": {
            "user_id": user.get("user_id"),
            "client_key": user.get("client_key"),
            "full_name": user.get("full_name"),
            "email": user.get("email"),
            "role": user.get("role"),
            "business_name": user.get("business_name"),
            "timezone": user.get("timezone"),
            "timezone_label": user.get("timezone_label"),
            "last_login_at": (
                user.get("last_login_at").isoformat()
                if user.get("last_login_at")
                else None
            ),
            "auth_source": user.get("auth_source"),
        },
        "email": user.get("email"),
    }


# -------------------------------------------------
# ADMIN SESSION TOKEN ENDPOINT
# -------------------------------------------------
@app.post("/auth/admin-token")
def auth_admin_token(x_admin_key: str = Header(None)):
    """
    Generates a normal JWT platform_admin session using ADMIN_API_KEY.

    This is useful for PowerShell/API testing. Most dashboard traffic should
    still use /auth/login with ADMIN_EMAIL + ADMIN_PASSWORD.
    """
    require_admin(x_admin_key)

    admin_email = os.getenv("ADMIN_EMAIL", "admin@gosonic.com")

    user = {
        "user_id": None,
        "client_key": None,
        "full_name": os.getenv("ADMIN_FULL_NAME", "Gosonic Admin"),
        "email": admin_email,
        "role": "platform_admin",
        "status": "active",
        "last_login_at": None,
        "business_name": os.getenv("ADMIN_COMPANY_NAME", "Gosonic"),
        "timezone": DEFAULT_CLIENT_TIMEZONE,
        "timezone_label": timezone_label(DEFAULT_CLIENT_TIMEZONE),
        "auth_source": "admin_api_key",
    }

    token = create_session_token(user)

    return {
        "status": "ok",
        "token_type": "Bearer",
        "expires_in_seconds": 43200,
        "token": token,
        "user": {
            "user_id": user.get("user_id"),
            "client_key": user.get("client_key"),
            "full_name": user.get("full_name"),
            "email": user.get("email"),
            "role": user.get("role"),
            "business_name": user.get("business_name"),
            "timezone": user.get("timezone"),
            "timezone_label": user.get("timezone_label"),
            "auth_source": user.get("auth_source"),
        },
        "email": user.get("email"),
    }


# -------------------------------------------------
# AUTH SESSION CHECK
# -------------------------------------------------
@app.get("/auth/me")
def auth_me(authorization: str = Header(None)):
    payload = require_auth_token(authorization)

    return {
        "status": "ok",
        "authenticated": True,
        "user": {
            "user_id": payload.get("user_id"),
            "client_key": payload.get("client_key"),
            "full_name": payload.get("full_name"),
            "email": payload.get("email"),
            "role": payload.get("role"),
            "business_name": payload.get("business_name"),
            "timezone": payload.get("timezone"),
            "timezone_label": timezone_label(payload.get("timezone")),
        },
        "email": payload.get("email"),
    }


# -------------------------------------------------
# SUPPORTED TIMEZONES
# -------------------------------------------------
@app.get("/timezones")
def get_supported_timezones(authorization: str = Header(None)):
    require_auth_token(authorization)

    return {
        "status": "ok",
        "default_timezone": DEFAULT_CLIENT_TIMEZONE,
        "timezones": [
            {"value": key, "label": label} for key, label in SUPPORTED_TIMEZONES.items()
        ],
    }


# -------------------------------------------------
# USERS READ ENDPOINT
# -------------------------------------------------
@app.get("/users")
def get_users(authorization: str = Header(None)):
    payload = require_auth_token(authorization)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                if is_platform_admin(payload):
                    cur.execute("""
                        SELECT
                            u.id,
                            u.client_key,
                            u.full_name,
                            u.email,
                            u.role,
                            u.status,
                            u.last_login_at,
                            u.created_at,
                            u.updated_at,
                            c.business_name,
                            c.timezone
                        FROM users u
                        LEFT JOIN clients c ON c.client_key = u.client_key
                        ORDER BY u.created_at ASC;
                    """)
                elif payload.get("role") == "client_admin":
                    effective_client_key = resolve_effective_client_key(payload)
                    cur.execute(
                        """
                        SELECT
                            u.id,
                            u.client_key,
                            u.full_name,
                            u.email,
                            u.role,
                            u.status,
                            u.last_login_at,
                            u.created_at,
                            u.updated_at,
                            c.business_name,
                            c.timezone
                        FROM users u
                        LEFT JOIN clients c ON c.client_key = u.client_key
                        WHERE u.client_key = %s
                        ORDER BY u.created_at ASC;
                    """,
                        (effective_client_key,),
                    )
                elif payload.get("role") == "client_user":
                    cur.execute(
                        """
                        SELECT
                            u.id,
                            u.client_key,
                            u.full_name,
                            u.email,
                            u.role,
                            u.status,
                            u.last_login_at,
                            u.created_at,
                            u.updated_at,
                            c.business_name,
                            c.timezone
                        FROM users u
                        LEFT JOIN clients c ON c.client_key = u.client_key
                        WHERE u.id = %s
                        LIMIT 1;
                    """,
                        (payload.get("user_id"),),
                    )
                else:
                    raise HTTPException(status_code=403, detail="Unsupported user role")

                rows = cur.fetchall()

        users = []

        for row in rows:
            users.append(
                {
                    "user_id": row[0],
                    "client_key": row[1],
                    "full_name": row[2],
                    "email": row[3],
                    "role": row[4],
                    "status": row[5],
                    "last_login_at": row[6].isoformat() if row[6] else None,
                    "created_at": row[7].isoformat() if row[7] else None,
                    "updated_at": row[8].isoformat() if row[8] else None,
                    "business_name": row[9],
                    "timezone": row[10],
                    "timezone_label": timezone_label(row[10]),
                }
            )

        return {
            "status": "ok",
            "count": len(users),
            "scope": (
                "platform" if is_platform_admin(payload) else payload.get("client_key")
            ),
            "users": users,
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Users read failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# USER MANAGEMENT HELPERS
# -------------------------------------------------
ALLOWED_USER_ROLES = {"platform_admin", "client_admin", "client_user"}
ALLOWED_USER_STATUSES = {"active", "inactive"}


def require_platform_admin_payload(authorization: str):
    payload = require_auth_token(authorization)

    if payload.get("role") != "platform_admin":
        raise HTTPException(status_code=403, detail="Platform admin access required")

    return payload


def is_platform_admin(payload: dict):
    return payload.get("role") == "platform_admin"


def is_client_role(payload: dict):
    return payload.get("role") in {"client_admin", "client_user"}


def require_client_scoped_payload(authorization: str):
    payload = require_auth_token(authorization)

    if not is_client_role(payload):
        raise HTTPException(status_code=403, detail="Client account access required")

    if not payload.get("client_key"):
        raise HTTPException(
            status_code=403, detail="Client account is not scoped to a client"
        )

    return payload


def resolve_effective_client_key(payload: dict, requested_client_key: str = None):
    """
    Central tenant isolation guard.

    platform_admin: may request any client_key or omit it for global access.
    client_admin/client_user: always limited to their own client_key.
    """
    requested_client_key = (requested_client_key or "").strip() or None

    if is_platform_admin(payload):
        return requested_client_key

    if not is_client_role(payload):
        raise HTTPException(status_code=403, detail="Unsupported user role")

    user_client_key = payload.get("client_key")

    if not user_client_key:
        raise HTTPException(status_code=403, detail="User is not assigned to a client")

    if requested_client_key and requested_client_key != user_client_key:
        raise HTTPException(
            status_code=403, detail="Access denied for requested client"
        )

    return user_client_key


def require_client_admin_or_platform(payload: dict, client_key: str = None):
    effective_client_key = resolve_effective_client_key(payload, client_key)

    if is_platform_admin(payload):
        return effective_client_key

    if payload.get("role") != "client_admin":
        raise HTTPException(status_code=403, detail="Client admin access required")

    return effective_client_key


def scoped_where_clause(table_alias: str, effective_client_key: str):
    if effective_client_key:
        return f"WHERE {table_alias}.client_key = %s", [effective_client_key]

    return "", []


def normalize_email(value: str):
    email = (value or "").strip().lower()

    if not email or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return None

    return email


def validate_password_strength(password: str):
    if not password or len(password) < 12:
        raise HTTPException(
            status_code=400, detail="Password must be at least 12 characters"
        )

    return True


def client_exists(client_key: str):
    database_url = os.getenv("DATABASE_URL")

    if not database_url or not client_key:
        return False

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM clients
                    WHERE client_key = %s
                    LIMIT 1;
                """,
                    (client_key,),
                )

                return cur.fetchone() is not None

    except Exception:
        logger.exception("Client existence check failed")
        return False


def build_user_response(row):
    return {
        "user_id": row[0],
        "client_key": row[1],
        "full_name": row[2],
        "email": row[3],
        "role": row[4],
        "status": row[5],
        "last_login_at": row[6].isoformat() if row[6] else None,
        "created_at": row[7].isoformat() if row[7] else None,
        "updated_at": row[8].isoformat() if row[8] else None,
        "business_name": row[9],
        "timezone": row[10],
        "timezone_label": timezone_label(row[10]),
    }


# -------------------------------------------------
# USER CREATE ENDPOINT
# -------------------------------------------------
@app.post("/users/create")
async def create_user(request: Request, authorization: str = Header(None)):
    require_platform_admin_payload(authorization)

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    data = await request.json()

    full_name = (data.get("full_name") or "").strip()
    email = normalize_email(data.get("email"))
    password = data.get("password") or ""
    role = (data.get("role") or "client_admin").strip()
    status = (data.get("status") or "active").strip()
    client_key = (data.get("client_key") or "").strip() or None

    if not full_name:
        raise HTTPException(status_code=400, detail="full_name is required")

    if not email:
        raise HTTPException(status_code=400, detail="valid email is required")

    if role not in ALLOWED_USER_ROLES:
        raise HTTPException(status_code=400, detail="invalid role")

    if status not in ALLOWED_USER_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status")

    validate_password_strength(password)

    if role in {"client_admin", "client_user"}:
        if not client_key:
            raise HTTPException(
                status_code=400, detail="client_key is required for client users"
            )

        if not client_exists(client_key):
            raise HTTPException(status_code=404, detail="client_key not found")

    if role == "platform_admin":
        client_key = None

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (
                        client_key,
                        full_name,
                        email,
                        password_hash,
                        role,
                        status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id;
                """,
                    (
                        client_key,
                        full_name,
                        email,
                        hash_password(password),
                        role,
                        status,
                    ),
                )

                created = cur.fetchone()

                cur.execute(
                    """
                    SELECT
                        u.id,
                        u.client_key,
                        u.full_name,
                        u.email,
                        u.role,
                        u.status,
                        u.last_login_at,
                        u.created_at,
                        u.updated_at,
                        c.business_name,
                        c.timezone
                    FROM users u
                    LEFT JOIN clients c ON c.client_key = u.client_key
                    WHERE u.id = %s
                    LIMIT 1;
                """,
                    (created[0],),
                )

                row = cur.fetchone()

            conn.commit()

        logger.info(
            "[USER CREATED] user_id=%s role=%s client_key=%s", row[0], role, client_key
        )

        return {
            "status": "ok",
            "message": "User created",
            "user": build_user_response(row),
        }

    except psycopg.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="email already exists")

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("User create failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# USER STATUS UPDATE ENDPOINT
# -------------------------------------------------
@app.post("/users/update-status")
async def update_user_status(request: Request, authorization: str = Header(None)):
    require_platform_admin_payload(authorization)

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    data = await request.json()

    user_id = data.get("user_id")
    status = (data.get("status") or "").strip()

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    if status not in ALLOWED_USER_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status")

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE users
                    SET
                        status = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING id;
                """,
                    (status, user_id),
                )

                updated = cur.fetchone()

            conn.commit()

        if not updated:
            raise HTTPException(status_code=404, detail="user not found")

        logger.info("[USER STATUS UPDATED] user_id=%s status=%s", user_id, status)

        return {
            "status": "ok",
            "message": "User status updated",
            "user_id": user_id,
            "user_status": status,
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("User status update failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# USER PASSWORD RESET ENDPOINT
# -------------------------------------------------
@app.post("/users/reset-password")
async def reset_user_password(request: Request, authorization: str = Header(None)):
    require_platform_admin_payload(authorization)

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    data = await request.json()

    user_id = data.get("user_id")
    new_password = data.get("password") or ""

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    validate_password_strength(new_password)

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE users
                    SET
                        password_hash = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING id;
                """,
                    (hash_password(new_password), user_id),
                )

                updated = cur.fetchone()

            conn.commit()

        if not updated:
            raise HTTPException(status_code=404, detail="user not found")

        logger.info("[USER PASSWORD RESET] user_id=%s", user_id)

        return {"status": "ok", "message": "User password updated", "user_id": user_id}

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("User password reset failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# HEALTH CHECK
# -------------------------------------------------
@app.get("/")
def root():
    return {"status": "ok", "service": "Gosonic MVP API"}


# -------------------------------------------------
# DATABASE CHECK
# -------------------------------------------------
@app.get("/db-check")
def db_check():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {
            "status": "error",
            "database": "not_configured",
            "message": "DATABASE_URL not configured",
        }

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                result = cur.fetchone()

        return {"status": "ok", "database": "connected", "result": result[0]}

    except Exception as e:
        logger.exception("DB check failed")
        return {"status": "error", "database": "connection_failed", "message": str(e)}


# -------------------------------------------------
# DATABASE INITIALIZATION
# -------------------------------------------------
@app.post("/init-db")
def init_db(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)

    if os.getenv("ALLOW_DB_INIT", "false").lower() != "true":
        raise HTTPException(
            status_code=403,
            detail="Database initialization endpoint is disabled. Set ALLOW_DB_INIT=true temporarily to use it.",
        )

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:

                # -------------------------------------------------
                # CLIENTS TABLE
                # -------------------------------------------------
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS clients (
                        id SERIAL PRIMARY KEY,
                        client_key TEXT UNIQUE NOT NULL,
                        business_name TEXT NOT NULL,
                        vertical TEXT NOT NULL DEFAULT 'hvac',
                        plan_tier TEXT NOT NULL DEFAULT 'lite',
                        inbound_phone TEXT UNIQUE,
                        business_phone TEXT,
                        caller_sms_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        status TEXT NOT NULL DEFAULT 'active',
                        timezone TEXT NOT NULL DEFAULT 'America/New_York',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)

                cur.execute("""
                    ALTER TABLE clients
                    ADD COLUMN IF NOT EXISTS inbound_phone TEXT;
                """)

                # -------------------------------------------------
                # CLIENT SETTINGS TABLE
                # -------------------------------------------------
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS client_settings (
                        id SERIAL PRIMARY KEY,
                        client_key TEXT UNIQUE NOT NULL
                            REFERENCES clients(client_key)
                            ON DELETE CASCADE,

                        greeting_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        custom_greeting TEXT,

                        end_call_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        custom_end_call TEXT,

                        caller_confirmation_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        business_sms_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        caller_sms_enabled BOOLEAN NOT NULL DEFAULT TRUE,

                        emergency_detection_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        after_hours_enabled BOOLEAN NOT NULL DEFAULT FALSE,

                        calendar_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                        crm_enabled BOOLEAN NOT NULL DEFAULT FALSE,

                        retell_agent_id TEXT,
                        twilio_inbound_number TEXT,
                        twilio_outbound_number TEXT,

                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)

                # -------------------------------------------------
                # CLIENT CONTACTS TABLE
                # -------------------------------------------------
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS client_contacts (
                        id SERIAL PRIMARY KEY,
                        client_key TEXT NOT NULL
                            REFERENCES clients(client_key)
                            ON DELETE CASCADE,

                        first_name TEXT,
                        last_name TEXT,
                        email TEXT,
                        phone TEXT,
                        role TEXT,
                        is_primary BOOLEAN NOT NULL DEFAULT TRUE,

                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)

                # -------------------------------------------------
                # CLIENT ADDRESSES TABLE
                # -------------------------------------------------
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS client_addresses (
                        id SERIAL PRIMARY KEY,
                        client_key TEXT NOT NULL
                            REFERENCES clients(client_key)
                            ON DELETE CASCADE,

                        address_line_1 TEXT,
                        address_line_2 TEXT,
                        city TEXT,
                        state_province TEXT,
                        postal_code TEXT,
                        country TEXT NOT NULL DEFAULT 'CA',
                        is_primary BOOLEAN NOT NULL DEFAULT TRUE,

                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)

                # -------------------------------------------------
                # USERS TABLE
                # -------------------------------------------------
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        client_key TEXT REFERENCES clients(client_key) ON DELETE SET NULL,
                        full_name TEXT NOT NULL,
                        email TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        role TEXT NOT NULL DEFAULT 'client_admin',
                        status TEXT NOT NULL DEFAULT 'active',
                        last_login_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_users_client_key
                    ON users(client_key);
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_users_email_lower
                    ON users(LOWER(email));
                """)

                # -------------------------------------------------
                # CALLS TABLE
                # -------------------------------------------------
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS calls (
                        id SERIAL PRIMARY KEY,
                        call_id TEXT UNIQUE NOT NULL,
                        client_key TEXT NOT NULL REFERENCES clients(client_key),
                        caller_name TEXT,
                        caller_phone TEXT,
                        service_address TEXT,
                        issue_description TEXT,
                        issue_type TEXT,
                        urgency TEXT,
                        call_outcome TEXT,
                        sms_policy_reason TEXT,
                        business_notified BOOLEAN NOT NULL DEFAULT FALSE,
                        business_error TEXT,
                        caller_notified BOOLEAN NOT NULL DEFAULT FALSE,
                        caller_error TEXT,
                        raw_payload JSONB,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)

                cur.execute("""
                    ALTER TABLE calls
                    ADD COLUMN IF NOT EXISTS business_error TEXT;
                """)

                cur.execute("""
                    ALTER TABLE calls
                    ADD COLUMN IF NOT EXISTS caller_error TEXT;
                """)

                cur.execute("""
                    ALTER TABLE calls
                    ADD COLUMN IF NOT EXISTS call_duration_seconds INTEGER;
                """)

                cur.execute("""
                    ALTER TABLE calls
                    ADD COLUMN IF NOT EXISTS billable_minutes NUMERIC(10,2) NOT NULL DEFAULT 0;
                """)

                cur.execute("""
                    ALTER TABLE calls
                    ADD COLUMN IF NOT EXISTS call_status TEXT;
                """)

                cur.execute("""
                    ALTER TABLE calls
                    ADD COLUMN IF NOT EXISTS webhook_status TEXT;
                """)

                cur.execute("""
                    ALTER TABLE calls
                    ADD COLUMN IF NOT EXISTS agent_id TEXT;
                """)

                cur.execute("""
                    ALTER TABLE calls
                    ADD COLUMN IF NOT EXISTS call_direction TEXT;
                """)

                cur.execute("""
                    ALTER TABLE calls
                    ADD COLUMN IF NOT EXISTS confidence NUMERIC(5,4);
                """)

                cur.execute("""
                    ALTER TABLE calls
                    ADD COLUMN IF NOT EXISTS processing_latency_ms INTEGER;
                """)

                cur.execute("""
                    ALTER TABLE calls
                    ADD COLUMN IF NOT EXISTS escalation_reason TEXT;
                """)

                cur.execute("""
                    ALTER TABLE calls
                    ADD COLUMN IF NOT EXISTS transcript TEXT;
                """)

                cur.execute("""
                    ALTER TABLE calls
                    ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ;
                """)

                cur.execute("""
                    ALTER TABLE calls
                    ADD COLUMN IF NOT EXISTS caller_phone_source TEXT;
                """)

                cur.execute("""
                    ALTER TABLE calls
                    ADD COLUMN IF NOT EXISTS caller_identity_status TEXT;
                """)

                cur.execute("""
                    ALTER TABLE calls
                    ADD COLUMN IF NOT EXISTS caller_phone_verified BOOLEAN NOT NULL DEFAULT FALSE;
                """)

                # -------------------------------------------------
                # CALL EVENTS TABLE
                # -------------------------------------------------
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS call_events (
                        id SERIAL PRIMARY KEY,

                        call_id TEXT NOT NULL,
                        client_key TEXT NOT NULL REFERENCES clients(client_key),

                        event_type TEXT NOT NULL,
                        event_timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                        event_metadata JSONB,

                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_call_events_call_id
                    ON call_events(call_id);
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_call_events_client_key
                    ON call_events(client_key);
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_call_events_event_type
                    ON call_events(event_type);
                """)

                # -------------------------------------------------
                # WORKFLOW INSTANCES TABLE
                # -------------------------------------------------
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS workflow_instances (
                        id SERIAL PRIMARY KEY,

                        workflow_id TEXT UNIQUE NOT NULL,
                        client_key TEXT NOT NULL REFERENCES clients(client_key),

                        source_type TEXT NOT NULL DEFAULT 'call',
                        source_id TEXT NOT NULL,

                        workflow_type TEXT NOT NULL DEFAULT 'service_request',
                        workflow_status TEXT NOT NULL DEFAULT 'created',

                        urgency TEXT,
                        current_stage TEXT NOT NULL DEFAULT 'intake_completed',

                        last_event_type TEXT,
                        last_event_at TIMESTAMPTZ,
                        notification_state TEXT,
                        service_state TEXT,

                        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        completed_at TIMESTAMPTZ,

                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)

                cur.execute("""
                    ALTER TABLE workflow_instances
                    ADD COLUMN IF NOT EXISTS last_event_type TEXT;
                """)

                cur.execute("""
                    ALTER TABLE workflow_instances
                    ADD COLUMN IF NOT EXISTS last_event_at TIMESTAMPTZ;
                """)

                cur.execute("""
                    ALTER TABLE workflow_instances
                    ADD COLUMN IF NOT EXISTS notification_state TEXT;
                """)

                cur.execute("""
                    ALTER TABLE workflow_instances
                    ADD COLUMN IF NOT EXISTS service_state TEXT;
                """)

                cur.execute("""
                    ALTER TABLE workflow_instances
                    ADD COLUMN IF NOT EXISTS ownership_state TEXT;
                """)

                cur.execute("""
                    ALTER TABLE workflow_instances
                    ADD COLUMN IF NOT EXISTS assigned_operator TEXT;
                """)

                cur.execute("""
                    ALTER TABLE workflow_instances
                    ADD COLUMN IF NOT EXISTS assigned_team TEXT;
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_workflow_instances_client_key
                    ON workflow_instances(client_key);
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_workflow_instances_source
                    ON workflow_instances(source_type, source_id);
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_workflow_instances_status
                    ON workflow_instances(workflow_status);
                """)

                # -------------------------------------------------
                # OPERATIONAL EVENTS TABLE
                # -------------------------------------------------
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS operational_events (
                        id SERIAL PRIMARY KEY,

                        event_id TEXT UNIQUE NOT NULL,
                        workflow_id TEXT REFERENCES workflow_instances(workflow_id),

                        client_key TEXT NOT NULL REFERENCES clients(client_key),

                        source_type TEXT NOT NULL DEFAULT 'system',
                        source_id TEXT,

                        event_type TEXT NOT NULL,
                        event_stage TEXT,

                        event_status TEXT NOT NULL DEFAULT 'recorded',
                        event_metadata JSONB,

                        occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_operational_events_workflow_id
                    ON operational_events(workflow_id);
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_operational_events_client_key
                    ON operational_events(client_key);
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_operational_events_event_type
                    ON operational_events(event_type);
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_operational_events_occurred_at
                    ON operational_events(occurred_at);
                """)

                # -------------------------------------------------
                # OPERATOR ACTION TOKENS TABLE
                # -------------------------------------------------
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS operator_action_tokens (
                        id SERIAL PRIMARY KEY,

                        workflow_id TEXT NOT NULL
                            REFERENCES workflow_instances(workflow_id)
                            ON DELETE CASCADE,

                        client_key TEXT NOT NULL
                            REFERENCES clients(client_key)
                            ON DELETE CASCADE,

                        action_type TEXT NOT NULL,
                        token_hash TEXT UNIQUE NOT NULL,

                        expires_at TIMESTAMPTZ NOT NULL,
                        used_at TIMESTAMPTZ,

                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_operator_action_tokens_workflow_id
                    ON operator_action_tokens(workflow_id);
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_operator_action_tokens_token_hash
                    ON operator_action_tokens(token_hash);
                """)

                # -------------------------------------------------
                # CLIENT PLANS TABLE
                # -------------------------------------------------
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS client_plans (
                        id SERIAL PRIMARY KEY,
                        client_key TEXT NOT NULL
                            REFERENCES clients(client_key)
                            ON DELETE CASCADE,

                        plan_name TEXT NOT NULL DEFAULT 'Lite Voice Intake',
                        concurrent_call_limit INTEGER NOT NULL DEFAULT 1,

                        included_minutes INTEGER,
                        overage_rate NUMERIC(10,4),

                        billing_anchor_day INTEGER NOT NULL DEFAULT 1,
                        activation_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                        active BOOLEAN NOT NULL DEFAULT TRUE,

                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_client_plans_client_key
                    ON client_plans(client_key);
                """)

                cur.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_client_plans_one_active
                    ON client_plans(client_key)
                    WHERE active = TRUE;
                """)

                # -------------------------------------------------
                # INVOICES TABLE
                # -------------------------------------------------
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS invoices (
                        id SERIAL PRIMARY KEY,
                        invoice_number TEXT UNIQUE NOT NULL,

                        client_key TEXT NOT NULL
                            REFERENCES clients(client_key)
                            ON DELETE CASCADE,

                        billing_period_start TIMESTAMPTZ NOT NULL,
                        billing_period_end TIMESTAMPTZ NOT NULL,

                        issue_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        due_date TIMESTAMPTZ,

                        subtotal NUMERIC(10,2) NOT NULL DEFAULT 0,
                        tax NUMERIC(10,2) NOT NULL DEFAULT 0,
                        total NUMERIC(10,2) NOT NULL DEFAULT 0,

                        status TEXT NOT NULL DEFAULT 'draft',

                        minutes_included INTEGER,
                        minutes_used NUMERIC(10,2) NOT NULL DEFAULT 0,
                        overage_minutes NUMERIC(10,2) NOT NULL DEFAULT 0,
                        overage_rate NUMERIC(10,4),

                        pdf_url TEXT,

                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_invoices_client_key
                    ON invoices(client_key);
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_invoices_billing_period
                    ON invoices(client_key, billing_period_start, billing_period_end);
                """)

                # -------------------------------------------------
                # INVOICE LINE ITEMS TABLE
                # -------------------------------------------------
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS invoice_line_items (
                        id SERIAL PRIMARY KEY,

                        invoice_id INTEGER NOT NULL
                            REFERENCES invoices(id)
                            ON DELETE CASCADE,

                        description TEXT NOT NULL,
                        quantity NUMERIC(10,2) NOT NULL DEFAULT 1,
                        unit_price NUMERIC(10,4) NOT NULL DEFAULT 0,
                        amount NUMERIC(10,2) NOT NULL DEFAULT 0,

                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_invoice_line_items_invoice_id
                    ON invoice_line_items(invoice_id);
                """)

                # -------------------------------------------------
                # SEED CLIENT
                # -------------------------------------------------
                cur.execute("""
                    INSERT INTO clients (
                        client_key,
                        business_name,
                        vertical,
                        plan_tier,
                        inbound_phone,
                        business_phone,
                        caller_sms_enabled,
                        status,
                        timezone
                    )
                    VALUES (
                        'hvac_toronto_001',
                        'Toronto HVAC',
                        'hvac',
                        'lite',
                        '+17059108234',
                        '+14383896310',
                        TRUE,
                        'active',
                        'America/Toronto'
                    )
                    ON CONFLICT (client_key)
                    DO UPDATE SET
                        inbound_phone = EXCLUDED.inbound_phone;
                """)

                # -------------------------------------------------
                # SEED CLIENT SETTINGS
                # -------------------------------------------------
                cur.execute("""
                    INSERT INTO client_settings (
                        client_key,
                        greeting_enabled,
                        end_call_enabled,
                        caller_confirmation_enabled,
                        business_sms_enabled,
                        caller_sms_enabled,
                        emergency_detection_enabled,
                        after_hours_enabled,
                        calendar_enabled,
                        crm_enabled,
                        twilio_inbound_number,
                        twilio_outbound_number
                    )
                    VALUES (
                        'hvac_toronto_001',
                        TRUE,
                        TRUE,
                        TRUE,
                        TRUE,
                        TRUE,
                        TRUE,
                        FALSE,
                        FALSE,
                        FALSE,
                        '+17059108234',
                        '+14383896310'
                    )
                    ON CONFLICT (client_key)
                    DO NOTHING;
                """)

                # -------------------------------------------------
                # SEED CLIENT PLAN
                # -------------------------------------------------
                cur.execute("""
                    INSERT INTO client_plans (
                        client_key,
                        plan_name,
                        concurrent_call_limit,
                        included_minutes,
                        overage_rate,
                        billing_anchor_day,
                        activation_date,
                        active
                    )
                    VALUES (
                        'hvac_toronto_001',
                        'Lite Voice Intake',
                        1,
                        300,
                        0.18,
                        11,
                        NOW(),
                        TRUE
                    )
                    ON CONFLICT DO NOTHING;
                """)

                # -------------------------------------------------
                # SEED PLATFORM ADMIN USER FROM ENVIRONMENT
                # -------------------------------------------------
                admin_email = os.getenv("ADMIN_EMAIL")
                admin_password = os.getenv("ADMIN_PASSWORD")
                admin_full_name = os.getenv("ADMIN_FULL_NAME", "Gosonic Admin")

                if admin_email and admin_password:
                    cur.execute(
                        """
                        SELECT id
                        FROM users
                        WHERE LOWER(email) = LOWER(%s)
                        LIMIT 1;
                    """,
                        (admin_email,),
                    )

                    existing_user = cur.fetchone()

                    if not existing_user:
                        cur.execute(
                            """
                            INSERT INTO users (
                                client_key,
                                full_name,
                                email,
                                password_hash,
                                role,
                                status
                            )
                            VALUES (%s, %s, %s, %s, %s, %s);
                        """,
                            (
                                None,
                                admin_full_name,
                                admin_email.lower(),
                                hash_password(admin_password),
                                "platform_admin",
                                "active",
                            ),
                        )

            conn.commit()

        return {
            "status": "ok",
            "message": "Database initialized",
            "tables_created": [
                "clients",
                "client_settings",
                "client_contacts",
                "client_addresses",
                "users",
                "calls",
            ],
            "routing_enabled": True,
            "settings_enabled": True,
            "seed_client": "hvac_toronto_001",
        }

    except Exception as e:
        logger.exception("Database initialization failed")

        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# CLIENTS READ ENDPOINT
# -------------------------------------------------
@app.get("/clients")
def get_clients(client_key: str = Query(None), authorization: str = Header(None)):
    payload = require_auth_token(authorization)
    effective_client_key = resolve_effective_client_key(payload, client_key)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                where_sql, params = scoped_where_clause("clients", effective_client_key)
                cur.execute(
                    f"""
                    SELECT
                        client_key,
                        business_name,
                        vertical,
                        plan_tier,
                        inbound_phone,
                        business_phone,
                        caller_sms_enabled,
                        status,
                        timezone,
                        created_at,
                        updated_at
                    FROM clients
                    {where_sql}
                    ORDER BY created_at ASC;
                """,
                    params,
                )

                rows = cur.fetchall()

        clients = []

        for row in rows:
            clients.append(
                {
                    "client_key": row[0],
                    "business_name": row[1],
                    "vertical": row[2],
                    "plan_tier": row[3],
                    "inbound_phone": row[4],
                    "business_phone": row[5],
                    "caller_sms_enabled": row[6],
                    "status": row[7],
                    "timezone": row[8],
                    "timezone_label": timezone_label(row[8]),
                    "created_at": row[9].isoformat() if row[9] else None,
                    "updated_at": row[10].isoformat() if row[10] else None,
                }
            )

        return {
            "status": "ok",
            "count": len(clients),
            "scope": "platform" if is_platform_admin(payload) else effective_client_key,
            "client_key_filter": effective_client_key,
            "clients": clients,
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Clients read failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# CLIENT CREATE ENDPOINT
# -------------------------------------------------
@app.post("/clients/create")
async def create_client(request: Request, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    data = await request.json()

    client_key = data.get("client_key")
    business_name = data.get("business_name")
    vertical = data.get("vertical", "hvac")
    plan_tier = data.get("plan_tier", "lite")
    inbound_phone = normalize_phone(data.get("inbound_phone"))
    business_phone = normalize_phone(data.get("business_phone"))
    timezone = normalize_timezone(data.get("timezone"))

    # -------------------------------------------------
    # CONTACT FIELDS
    # -------------------------------------------------
    first_name = data.get("first_name")
    last_name = data.get("last_name")
    email = data.get("email")
    contact_phone = normalize_phone(data.get("contact_phone"))
    role = data.get("role", "Owner")

    # -------------------------------------------------
    # ADDRESS FIELDS
    # -------------------------------------------------
    address_line_1 = data.get("address_line_1")
    address_line_2 = data.get("address_line_2")
    city = data.get("city")
    state_province = data.get("state_province")
    postal_code = data.get("postal_code")
    country = data.get("country", "CA")

    if not client_key or not business_name:
        return {
            "status": "error",
            "message": "client_key and business_name are required",
        }

    if not inbound_phone:
        return {"status": "error", "message": "valid inbound_phone is required"}

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO clients (
                        client_key,
                        business_name,
                        vertical,
                        plan_tier,
                        inbound_phone,
                        business_phone,
                        caller_sms_enabled,
                        status,
                        timezone
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'active', %s)
                    ON CONFLICT (client_key) DO NOTHING;
                """,
                    (
                        client_key,
                        business_name,
                        vertical,
                        plan_tier,
                        inbound_phone,
                        business_phone,
                        timezone,
                    ),
                )

                client_created = cur.rowcount

                if client_created == 0:
                    return {
                        "status": "error",
                        "message": "client_key already exists",
                        "client_key": client_key,
                    }

                cur.execute(
                    """
                    INSERT INTO client_settings (
                        client_key,
                        greeting_enabled,
                        end_call_enabled,
                        caller_confirmation_enabled,
                        business_sms_enabled,
                        caller_sms_enabled,
                        emergency_detection_enabled,
                        after_hours_enabled,
                        calendar_enabled,
                        crm_enabled,
                        twilio_inbound_number,
                        twilio_outbound_number
                    )
                    VALUES (
                        %s,
                        TRUE,
                        TRUE,
                        TRUE,
                        TRUE,
                        TRUE,
                        TRUE,
                        FALSE,
                        FALSE,
                        FALSE,
                        %s,
                        %s
                    )
                    ON CONFLICT (client_key) DO NOTHING;
                """,
                    (client_key, inbound_phone, TWILIO_PHONE),
                )

                # -------------------------------------------------
                # CREATE PRIMARY CLIENT CONTACT
                # -------------------------------------------------
                if first_name or last_name or email or contact_phone:
                    cur.execute(
                        """
                        INSERT INTO client_contacts (
                            client_key,
                            first_name,
                            last_name,
                            email,
                            phone,
                            role,
                            is_primary
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, TRUE);
                    """,
                        (client_key, first_name, last_name, email, contact_phone, role),
                    )

                # -------------------------------------------------
                # CREATE PRIMARY CLIENT ADDRESS
                # -------------------------------------------------
                if address_line_1 or city or state_province or postal_code:
                    cur.execute(
                        """
                        INSERT INTO client_addresses (
                            client_key,
                            address_line_1,
                            address_line_2,
                            city,
                            state_province,
                            postal_code,
                            country,
                            is_primary
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE);
                    """,
                        (
                            client_key,
                            address_line_1,
                            address_line_2,
                            city,
                            state_province,
                            postal_code,
                            country,
                        ),
                    )

            conn.commit()

        return {
            "status": "ok",
            "message": "Client created",
            "client": {
                "client_key": client_key,
                "business_name": business_name,
                "vertical": vertical,
                "plan_tier": plan_tier,
                "inbound_phone": inbound_phone,
                "business_phone": business_phone,
                "timezone": timezone,
                "contact": {
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": email,
                    "phone": contact_phone,
                    "role": role,
                },
                "address": {
                    "address_line_1": address_line_1,
                    "address_line_2": address_line_2,
                    "city": city,
                    "state_province": state_province,
                    "postal_code": postal_code,
                    "country": country,
                },
            },
        }

    except Exception as e:
        logger.exception("Client create failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# CALLS READ ENDPOINT
# -------------------------------------------------
@app.get("/calls")
def get_calls(client_key: str = Query(None), authorization: str = Header(None)):
    payload = require_auth_token(authorization)
    effective_client_key = resolve_effective_client_key(payload, client_key)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                where_sql, params = scoped_where_clause("calls", effective_client_key)
                cur.execute(
                    f"""
                    SELECT
                        calls.call_id,
                        calls.client_key,
                        calls.caller_name,
                        calls.caller_phone,
                        calls.service_address,
                        calls.issue_description,
                        calls.issue_type,
                        calls.urgency,
                        calls.call_outcome,
                        calls.sms_policy_reason,
                        calls.business_notified,
                        calls.business_error,
                        calls.caller_notified,
                        calls.caller_error,
                        calls.call_duration_seconds,
                        calls.billable_minutes,
                        calls.call_status,
                        calls.webhook_status,
                        calls.agent_id,
                        calls.call_direction,
                        calls.confidence,
                        calls.processing_latency_ms,
                        calls.escalation_reason,
                        calls.transcript,
                        calls.ended_at,
                        calls.caller_phone_source,
                        calls.caller_identity_status,
                        calls.caller_phone_verified,
                        calls.created_at,
                        calls.raw_payload,
                        workflow_instances.workflow_id,
                        workflow_instances.workflow_status,
                        workflow_instances.current_stage,
                        workflow_instances.last_event_type,
                        workflow_instances.last_event_at,
                        workflow_instances.notification_state,
                        workflow_instances.service_state,
                        workflow_instances.ownership_state,
                        workflow_instances.assigned_operator,
                        workflow_instances.assigned_team
                    FROM calls
                    LEFT JOIN workflow_instances
                    ON workflow_instances.source_type = 'call'
                    AND workflow_instances.source_id = calls.call_id
                    {where_sql}
                    ORDER BY calls.created_at DESC
                    LIMIT 50;
                """,
                    params,
                )

                rows = cur.fetchall()

        calls = []

        for row in rows:
            # -------------------------------------------------
            # CANONICAL QUEUE STATE
            # -------------------------------------------------
            workflow_status = row[31]
            current_stage = row[32]
            notification_state = row[35]
            service_state = row[36]
            ownership_state = row[37]
            assigned_operator = row[38]
            assigned_team = row[39]
            urgency = row[7]

            queue_state = compute_queue_state(
                urgency=urgency,
                workflow_status=workflow_status,
                notification_state=notification_state,
                service_state=service_state,
            )

            operator_actions = build_operator_actions(
                workflow_status=workflow_status,
                service_state=service_state,
                notification_state=notification_state,
                last_event_type=row[33],
            )

            operator_action_state = build_operator_action_state(
                workflow_status=workflow_status,
                service_state=service_state,
                last_event_type=row[33],
            )

            calls.append(
                {
                    "call_id": row[0],
                    "client_key": row[1],
                    "caller_name": row[2],
                    "caller_phone": row[3],
                    "service_address": row[4],
                    "issue_description": row[5],
                    "issue_type": row[6],
                    "urgency": row[7],
                    "call_outcome": row[8],
                    "sms_policy_reason": row[9],
                    "business_notified": row[10],
                    "business_error": row[11],
                    "caller_notified": row[12],
                    "caller_error": row[13],
                    "call_duration_seconds": row[14],
                    "billable_minutes": float(row[15] or 0),
                    "call_status": row[16],
                    "webhook_status": row[17],
                    "agent_id": row[18],
                    "call_direction": row[19],
                    "confidence": float(row[20]) if row[20] is not None else None,
                    "processing_latency_ms": row[21],
                    "escalation_reason": row[22],
                    "transcript": row[23],
                    "ended_at": row[24].isoformat() if row[24] else None,
                    "caller_phone_source": row[25],
                    "caller_identity_status": row[26],
                    "caller_phone_verified": row[27],
                    "created_at": row[28].isoformat() if row[28] else None,
                    "raw_payload": row[29],
                    "workflow_id": row[30],
                    "workflow_status": workflow_status,
                    "queue_state": queue_state,
                    "current_stage": current_stage,
                    "last_event_type": row[33],
                    "last_event_at": row[34].isoformat() if row[34] else None,
                    "notification_state": row[35],
                    "service_state": row[36],
                    "ownership_state": ownership_state,
                    "assigned_operator": assigned_operator,
                    "assigned_team": assigned_team,
                    "operator_actions": operator_actions,
                    "operator_action_state": operator_action_state,
                }
            )

        return {
            "status": "ok",
            "count": len(calls),
            "scope": "platform" if is_platform_admin(payload) else effective_client_key,
            "client_key_filter": effective_client_key,
            "calls": calls,
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Calls read failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# CALL EVENTS READ ENDPOINT
# -------------------------------------------------
@app.get("/calls/{call_id}/events")
def get_call_events(
    call_id: str,
    client_key: str = Query(None),
    authorization: str = Header(None),
):
    payload = require_auth_token(authorization)
    effective_client_key = resolve_effective_client_key(payload, client_key)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    if not call_id:
        raise HTTPException(status_code=400, detail="call_id is required")

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                if effective_client_key:
                    cur.execute(
                        """
                        SELECT
                            id,
                            call_id,
                            client_key,
                            event_type,
                            event_timestamp,
                            event_metadata,
                            created_at
                        FROM call_events
                        WHERE call_id = %s
                          AND client_key = %s
                        ORDER BY event_timestamp ASC, id ASC;
                        """,
                        (call_id, effective_client_key),
                    )
                else:
                    cur.execute(
                        """
                        SELECT
                            id,
                            call_id,
                            client_key,
                            event_type,
                            event_timestamp,
                            event_metadata,
                            created_at
                        FROM call_events
                        WHERE call_id = %s
                        ORDER BY event_timestamp ASC, id ASC;
                        """,
                        (call_id,),
                    )

                rows = cur.fetchall()

        events = []

        for row in rows:
            events.append(
                {
                    "event_id": row[0],
                    "call_id": row[1],
                    "client_key": row[2],
                    "event_type": row[3],
                    "event_timestamp": row[4].isoformat() if row[4] else None,
                    "event_metadata": row[5] or {},
                    "created_at": row[6].isoformat() if row[6] else None,
                }
            )

        return {
            "status": "ok",
            "count": len(events),
            "call_id": call_id,
            "scope": "platform" if is_platform_admin(payload) else effective_client_key,
            "client_key_filter": effective_client_key,
            "events": events,
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Call events read failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# WORKFLOWS READ ENDPOINT
# -------------------------------------------------
@app.get("/workflows")
def get_workflows(client_key: str = Query(None), authorization: str = Header(None)):
    payload = require_auth_token(authorization)
    effective_client_key = resolve_effective_client_key(payload, client_key)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                where_sql, params = scoped_where_clause(
                    "workflow_instances",
                    effective_client_key,
                )

                cur.execute(
                    f"""
                    SELECT
                        workflow_id,
                        client_key,
                        source_type,
                        source_id,
                        workflow_type,
                        workflow_status,
                        urgency,
                        current_stage,
                        started_at,
                        completed_at,
                        created_at,
                        updated_at
                    FROM workflow_instances
                    {where_sql}
                    ORDER BY created_at DESC
                    LIMIT 100;
                """,
                    params,
                )

                rows = cur.fetchall()

        workflows = []

        for row in rows:
            workflows.append(
                {
                    "workflow_id": row[0],
                    "client_key": row[1],
                    "source_type": row[2],
                    "source_id": row[3],
                    "workflow_type": row[4],
                    "workflow_status": row[5],
                    "urgency": row[6],
                    "current_stage": row[7],
                    "started_at": row[8].isoformat() if row[8] else None,
                    "completed_at": row[9].isoformat() if row[9] else None,
                    "created_at": row[10].isoformat() if row[10] else None,
                    "updated_at": row[11].isoformat() if row[11] else None,
                }
            )

        return {
            "status": "ok",
            "count": len(workflows),
            "scope": "platform" if is_platform_admin(payload) else effective_client_key,
            "client_key_filter": effective_client_key,
            "workflows": workflows,
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Workflows read failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# WORKFLOW EVENTS READ ENDPOINT
# -------------------------------------------------
@app.get("/workflows/{workflow_id}/events")
def get_workflow_events(
    workflow_id: str,
    client_key: str = Query(None),
    authorization: str = Header(None),
):
    payload = require_auth_token(authorization)
    effective_client_key = resolve_effective_client_key(payload, client_key)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    if not workflow_id:
        raise HTTPException(status_code=400, detail="workflow_id is required")

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                if effective_client_key:
                    cur.execute(
                        """
                        SELECT
                            event_id,
                            workflow_id,
                            client_key,
                            source_type,
                            source_id,
                            event_type,
                            event_stage,
                            event_status,
                            event_metadata,
                            occurred_at,
                            created_at
                        FROM operational_events
                        WHERE workflow_id = %s
                          AND client_key = %s
                        ORDER BY occurred_at ASC, id ASC;
                        """,
                        (workflow_id, effective_client_key),
                    )
                else:
                    cur.execute(
                        """
                        SELECT
                            event_id,
                            workflow_id,
                            client_key,
                            source_type,
                            source_id,
                            event_type,
                            event_stage,
                            event_status,
                            event_metadata,
                            occurred_at,
                            created_at
                        FROM operational_events
                        WHERE workflow_id = %s
                        ORDER BY occurred_at ASC, id ASC;
                        """,
                        (workflow_id,),
                    )

                rows = cur.fetchall()

        events = []

        for row in rows:
            events.append(
                {
                    "event_id": row[0],
                    "workflow_id": row[1],
                    "client_key": row[2],
                    "source_type": row[3],
                    "source_id": row[4],
                    "event_type": row[5],
                    "event_stage": row[6],
                    "event_status": row[7],
                    "event_metadata": row[8] or {},
                    "occurred_at": row[9].isoformat() if row[9] else None,
                    "created_at": row[10].isoformat() if row[10] else None,
                }
            )

        return {
            "status": "ok",
            "count": len(events),
            "workflow_id": workflow_id,
            "scope": "platform" if is_platform_admin(payload) else effective_client_key,
            "client_key_filter": effective_client_key,
            "events": events,
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Workflow events read failed")
        return {"status": "error", "message": str(e)}

# -------------------------------------------------
# OPERATOR ACKNOWLEDGEMENT ENDPOINTS
# -------------------------------------------------
@app.get("/operator/ack/{token}", response_class=HTMLResponse)
def acknowledge_operator_request(token: str):
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise HTTPException(status_code=500, detail="DATABASE_URL not configured")

    token_hash = hash_operator_token(token)

    if not token_hash:
        raise HTTPException(status_code=400, detail="Invalid acknowledgement token")

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        expires_at,
                        used_at
                    FROM operator_action_tokens
                    WHERE token_hash = %s
                    LIMIT 1;
                    """,
                    (token_hash,),
                )

                row = cur.fetchone()

        if not row:
            raise HTTPException(
                status_code=404,
                detail="Acknowledgement link not found",
            )

        used_at = row[2]

        if used_at:
            return """
            <html>
                <head>
                    <meta name="viewport" content="width=device-width, initial-scale=1">
                </head>
                <body style="font-family: system-ui; padding: 48px 28px; font-size: 22px; line-height: 1.45;">
                    <h1 style="font-size: 36px; margin-bottom: 16px;">Already acknowledged</h1>
                    <p style="max-width: 620px;">
                        This service request has already been confirmed as received.
                    </p>
                </body>
            </html>
            """

        return f"""
        <html>
            <head>
                <meta name="viewport" content="width=device-width, initial-scale=1">
            </head>
            <body style="font-family: system-ui; padding: 48px 28px; font-size: 22px; line-height: 1.45;">
                <h1 style="font-size: 36px; margin-bottom: 16px;">Confirm request received</h1>
                <p style="max-width: 620px;">
                    Tap the button below to confirm this service request was received.
                </p>

                <form method="post" action="/operator/ack/{token}/confirm">
                    <button
                        type="submit"
                        style="font-size: 22px; padding: 18px 24px; border-radius: 12px; border: 0; background: #111; color: #fff; margin-top: 18px;"
                    >
                        Confirm received
                    </button>
                </form>
            </body>
        </html>
        """

    except HTTPException:
        raise

    except Exception:
        logger.exception("Operator acknowledgement page failed")
        raise HTTPException(
            status_code=500,
            detail="Operator acknowledgement page failed",
        )


@app.post("/operator/ack/{token}/confirm", response_class=HTMLResponse)
def confirm_operator_acknowledgement(token: str):
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise HTTPException(status_code=500, detail="DATABASE_URL not configured")

    token_hash = hash_operator_token(token)

    if not token_hash:
        raise HTTPException(status_code=400, detail="Invalid acknowledgement token")

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        workflow_id,
                        client_key,
                        action_type,
                        expires_at,
                        used_at
                    FROM operator_action_tokens
                    WHERE token_hash = %s
                    LIMIT 1;
                    """,
                    (token_hash,),
                )

                row = cur.fetchone()

                if not row:
                    raise HTTPException(
                        status_code=404,
                        detail="Acknowledgement link not found",
                    )

                token_id = row[0]
                workflow_id = row[1]
                client_key = row[2]
                action_type = row[3]
                expires_at = row[4]
                used_at = row[5]

                if used_at:
                    return """
                    <html>
                        <head>
                            <meta name="viewport" content="width=device-width, initial-scale=1">
                        </head>
                        <body style="font-family: system-ui; padding: 48px 28px; font-size: 22px; line-height: 1.45;">
                            <h1 style="font-size: 36px; margin-bottom: 16px;">Already acknowledged</h1>
                            <p style="max-width: 620px;">
                                This service request has already been confirmed as received.
                            </p>
                        </body>
                    </html>
                    """

                if expires_at and expires_at < datetime.now(timezone.utc):
                    raise HTTPException(
                        status_code=410,
                        detail="Acknowledgement link expired",
                    )

                event_id = append_workflow_event(
                    cur=cur,
                    workflow_id=workflow_id,
                    client_key=client_key,
                    event_type="ownership.acknowledged",
                    event_stage="acknowledged",
                    source_type="operator",
                    source_id=workflow_id,
                    metadata={
                        "action_type": action_type,
                        "action_source": "sms_link_button",
                        "ownership_state": "acknowledged",
                    },
                )

                if not event_id:
                    raise HTTPException(
                        status_code=500,
                        detail="Operator acknowledgement failed before event persistence",
                    )

                update_workflow_state(
                    cur=cur,
                    workflow_id=workflow_id,
                    workflow_status="active",
                    current_stage="acknowledged",
                    last_event_type="ownership.acknowledged",
                )

                cur.execute(
                    """
                    UPDATE workflow_instances
                    SET
                        ownership_state = %s,
                        updated_at = NOW()
                    WHERE workflow_id = %s;
                    """,
                    (
                        "acknowledged",
                        workflow_id,
                    ),
                )

                cur.execute(
                    """
                    UPDATE operator_action_tokens
                    SET used_at = NOW()
                    WHERE id = %s;
                    """,
                    (token_id,),
                )

            conn.commit()

        return """
        <html>
            <head>
                <meta name="viewport" content="width=device-width, initial-scale=1">
            </head>
            <body style="font-family: system-ui; padding: 48px 28px; font-size: 22px; line-height: 1.45;">
                <h1 style="font-size: 36px; margin-bottom: 16px;">Request acknowledged</h1>
                <p style="max-width: 620px;">
                    Gosonic has recorded that this service request was received.
                </p>
            </body>
        </html>
        """

    except HTTPException:
        raise

    except Exception:
        logger.exception("Operator acknowledgement confirmation failed")
        raise HTTPException(
            status_code=500,
            detail="Operator acknowledgement confirmation failed",
        )

# -------------------------------------------------
# OPERATOR ACTIONS
# -------------------------------------------------
CANONICAL_OPERATOR_ACTION_TYPE = "service.transition"

CANONICAL_OPERATOR_ACTIONS = {
    "advance_to_awaiting_dispatch": {
        "label": "Move To Awaiting Dispatch",
        "target_service_state": "awaiting_dispatch",
        "requires_acknowledgement": True,
        "governance_policy": "acknowledgement_required_before_dispatch",
        "terminal": False,
    },
    "advance_to_scheduled": {
        "label": "Schedule Service",
        "target_service_state": "scheduled",
        "requires_acknowledgement": False,
        "governance_policy": "standard_service_progression",
        "terminal": False,
    },
    "advance_to_assigned": {
        "label": "Assign Technician",
        "target_service_state": "assigned",
        "requires_acknowledgement": False,
        "governance_policy": "standard_service_progression",
        "terminal": False,
    },
    "advance_to_in_progress": {
        "label": "Mark In Progress",
        "target_service_state": "in_progress",
        "requires_acknowledgement": False,
        "governance_policy": "standard_service_progression",
        "terminal": False,
    },
    "resolve_workflow": {
        "label": "Resolve Workflow",
        "target_service_state": "resolved",
        "requires_acknowledgement": False,
        "governance_policy": "workflow_resolution",
        "terminal": True,
    },
}


def build_service_transition_action(
    action_id: str,
    label: str,
    target_service_state: str,
    requires_acknowledgement: bool,
    acknowledgement_recorded: bool,
    governance_policy: str,
    terminal: bool = False,
):
    return {
        "action_id": action_id,
        "action_type": CANONICAL_OPERATOR_ACTION_TYPE,
        "label": label,
        "target_service_state": target_service_state,
        "requires_acknowledgement": requires_acknowledgement,
        "acknowledgement_recorded": acknowledgement_recorded,
        "governance_policy": governance_policy,
        "terminal": terminal,
    }

def build_canonical_operator_action(
    action_id: str,
    acknowledgement_recorded: bool,
):
    action = CANONICAL_OPERATOR_ACTIONS.get(action_id)

    if not action:
        return None

    return build_service_transition_action(
        action_id=action_id,
        label=action["label"],
        target_service_state=action["target_service_state"],
        requires_acknowledgement=action["requires_acknowledgement"],
        acknowledgement_recorded=acknowledgement_recorded,
        governance_policy=action["governance_policy"],
        terminal=action["terminal"],
    )

def is_acknowledgement_recorded(last_event_type: str):
    return (last_event_type or "").strip().lower() in {
        "ownership.acknowledged",
        "operator.acknowledged",
    }

def build_operator_actions(
    workflow_status: str,
    service_state: str,
    notification_state: str = None,
    last_event_type: str = None,
):
    """
    Backend-authoritative operator action generator.

    Frontend should eventually render these actions directly instead of
    hardcoding lifecycle semantics in React.

    This helper intentionally exposes only operationally valid next actions.
    """

    workflow_status = (workflow_status or "").strip().lower()
    service_state = normalize_service_state(service_state)
    notification_state = (notification_state or "").strip().lower()
    last_event_type = (last_event_type or "").strip().lower()

    acknowledgement_recorded = is_acknowledgement_recorded(last_event_type)

    if workflow_status in TERMINAL_WORKFLOW_STATUSES:
        return []

    if workflow_status != "active":
        return []

    actions = []

    action_id = SERVICE_STATE_ACTION_MAP.get(service_state)

    if not action_id:
        return actions

    action_definition = CANONICAL_OPERATOR_ACTIONS.get(action_id)

    if not action_definition:
        return actions

    requires_acknowledgement = action_definition.get(
        "requires_acknowledgement",
        False,
    )

    if requires_acknowledgement and not acknowledgement_recorded:
        return actions

    actions.append(
        build_canonical_operator_action(
            action_id=action_id,
            acknowledgement_recorded=acknowledgement_recorded,
        )
    )

    return actions

def build_operator_action_state(
    workflow_status: str,
    service_state: str,
    last_event_type: str = None,
):
    workflow_status = (workflow_status or "").strip().lower()
    service_state = normalize_service_state(service_state)
    acknowledgement_recorded = is_acknowledgement_recorded(last_event_type)

    if workflow_status in TERMINAL_WORKFLOW_STATUSES:
        return {
            "status": "none",
            "reason": "terminal_workflow",
            "label": None,
            "governance_policy": "terminal_workflows_expose_no_actions",
        }

    if workflow_status != "active":
        return {
            "status": "none",
            "reason": "workflow_not_active",
            "label": None,
            "governance_policy": "active_workflows_only",
        }

    if service_state == "triaged" and not acknowledgement_recorded:
        return {
            "status": "blocked",
            "reason": "awaiting_acknowledgement",
            "label": "Waiting for operator acknowledgement",
            "governance_policy": "acknowledgement_required_before_dispatch",
        }

    return {
        "status": "available",
        "reason": None,
        "label": None,
        "governance_policy": "standard_lite_service_lifecycle",
    }

# -------------------------------------------------
# OPERATOR ACTION RESOLUTION
# -------------------------------------------------
def resolve_operator_action(action_id: str):
    """
    Resolves a canonical operator action_id into the lifecycle transition
    it is allowed to request.

    This is the bridge between backend-generated action objects and future
    action execution endpoints. It intentionally does not perform the action.
    It only resolves canonical action identity into requested lifecycle intent.
    """

    action_id = (action_id or "").strip().lower()

    action = CANONICAL_OPERATOR_ACTIONS.get(action_id)

    if not action:
        return None

    return {
        "action_type": CANONICAL_OPERATOR_ACTION_TYPE,
        "target_service_state": action["target_service_state"],
        "terminal": action["terminal"],
    }

# -------------------------------------------------
# SERVICE STATE TRANSITION VALIDATION
# -------------------------------------------------
SERVICE_STATE_TRANSITIONS = {
    "pending": {"triaged", "failed"},
    "intake_completed": {"triaged", "failed"},
    "triaged": {"awaiting_dispatch", "failed"},
    "awaiting_dispatch": {"scheduled", "failed"},
    "scheduled": {"assigned", "failed"},
    "assigned": {"in_progress", "failed"},
    "in_progress": {"resolved", "failed"},
}

TERMINAL_WORKFLOW_STATUSES = {"resolved", "failed"}
TERMINAL_SERVICE_STATES = {"resolved", "failed"}

SERVICE_STATE_ACTION_MAP = {
    "triaged": "advance_to_awaiting_dispatch",
    "awaiting_dispatch": "advance_to_scheduled",
    "scheduled": "advance_to_assigned",
    "assigned": "advance_to_in_progress",
    "in_progress": "resolve_workflow",
}


def normalize_service_state(value):
    return (value or "pending").strip().lower()


def get_workflow_state_for_transition(workflow_id: str, client_key: str):
    """
    Fetch the current workflow snapshot before allowing a service-state transition.

    This protects the event stream and mutable workflow snapshot from invalid
    backward transitions, skipped lifecycle states, or reopening terminal workflows.
    """
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        logger.warning("Workflow transition validation skipped: DATABASE_URL not configured")
        return None

    if not workflow_id or not client_key:
        return None

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        workflow_status,
                        service_state
                    FROM workflow_instances
                    WHERE workflow_id = %s
                      AND client_key = %s
                    LIMIT 1;
                    """,
                    (workflow_id, client_key),
                )

                row = cur.fetchone()

        if not row:
            return None

        return {
            "workflow_status": row[0],
            "service_state": row[1],
        }

    except Exception:
        logger.exception("Workflow transition state lookup failed")
        return None


def validate_service_state_transition(
    current_workflow_status: str,
    current_service_state: str,
    requested_service_state: str,
):
    """
    Enforce canonical service lifecycle progression.

    The service-state endpoint must not allow arbitrary state jumps or reopening
    terminal workflows. UI controls should reflect these backend rules.
    """
    workflow_status = (current_workflow_status or "").strip().lower()
    current_state = normalize_service_state(current_service_state)
    requested_state = normalize_service_state(requested_service_state)

    if workflow_status in TERMINAL_WORKFLOW_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"workflow is already terminal: {workflow_status}",
        )

    if current_state in TERMINAL_SERVICE_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"service workflow is already terminal: {current_state}",
        )

    allowed_next_states = SERVICE_STATE_TRANSITIONS.get(current_state, set())

    if requested_state not in allowed_next_states:
        raise HTTPException(
            status_code=409,
            detail=(
                "invalid service_state transition: "
                f"{current_state} -> {requested_state}"
            ),
        )

    return True


def execute_workflow_service_state_update(
    workflow_id: str,
    client_key: str,
    service_state: str,
    reason: str,
    requested_by: str,
    authorization: str,
):
    payload = require_auth_token(authorization)

    if not is_platform_admin(payload):
        raise HTTPException(status_code=403, detail="Platform admin access required")

    workflow_snapshot = get_workflow_state_for_transition(
        workflow_id=workflow_id,
        client_key=client_key,
    )

    if not workflow_snapshot:
        raise HTTPException(status_code=404, detail="workflow not found")

    validate_service_state_transition(
        current_workflow_status=workflow_snapshot.get("workflow_status"),
        current_service_state=workflow_snapshot.get("service_state"),
        requested_service_state=service_state,
    )

    ownership_update = None

    if service_state == "assigned":
        ownership_update = {
            "ownership_state": "assigned",
            "assigned_operator": requested_by or payload.get("email") or "platform_admin",
            "assigned_team": "operations",
        }

    event_metadata = {
        "reason": reason,
        "actor": "platform_admin",
        "source": "operator_action",
        "requested_by": requested_by or payload.get("email"),
        "previous_workflow_status": workflow_snapshot.get("workflow_status"),
        "previous_service_state": workflow_snapshot.get("service_state"),
        "ownership_update": ownership_update,
    }

    if service_state == "resolved":
        event_id = resolve_workflow_by_workflow_id(
            workflow_id=workflow_id,
            client_key=client_key,
            source_id=workflow_id,
            resolution_reason=reason,
            event_metadata=event_metadata,
        )

        if not event_id:
            raise HTTPException(
                status_code=500,
                detail="Workflow resolution failed before event persistence",
            )

        return {
            "status": "ok",
            "message": "Workflow resolved",
            "workflow_id": workflow_id,
            "client_key": client_key,
            "service_state": "resolved",
            "workflow_status": "resolved",
            "event_id": event_id,
        }

    if service_state == "failed":
        event_id = fail_workflow_by_workflow_id(
            workflow_id=workflow_id,
            client_key=client_key,
            source_id=workflow_id,
            failure_reason=reason,
            event_metadata=event_metadata,
        )

        if not event_id:
            raise HTTPException(
                status_code=500,
                detail="Workflow failure failed before event persistence",
            )

        return {
            "status": "ok",
            "message": "Workflow failed",
            "workflow_id": workflow_id,
            "client_key": client_key,
            "service_state": "failed",
            "workflow_status": "failed",
            "event_id": event_id,
        }

    event_id = advance_service_state_by_workflow_id(
        workflow_id=workflow_id,
        client_key=client_key,
        service_state=service_state,
        source_id=workflow_id,
        event_metadata=event_metadata,
    )

    if not event_id:
        raise HTTPException(
            status_code=500,
            detail="Workflow service state update failed before event persistence",
        )

    return {
        "status": "ok",
        "message": "Workflow service state updated",
        "workflow_id": workflow_id,
        "client_key": client_key,
        "service_state": service_state,
        "event_id": event_id,
    }


# -------------------------------------------------
# WORKFLOW SERVICE STATE UPDATE ENDPOINT
# -------------------------------------------------
@app.post("/workflows/{workflow_id}/actions/{action_id}")
def execute_operator_action(
    workflow_id: str,
    action_id: str,
    payload: dict = Body(default={}),
    authorization: str = Header(default=None),
):
    """
    Canonical operator action execution endpoint.

    This endpoint resolves backend-generated canonical action IDs into
    validated lifecycle transitions.

    Current implementation intentionally delegates to the existing
    service-state orchestration path so lifecycle validation,
    immutable event persistence, and workflow semantics remain centralized.
    """

    require_auth_token(authorization)

    resolved_action = resolve_operator_action(action_id)

    if not resolved_action:
        raise HTTPException(
            status_code=404,
            detail="Unknown operator action",
        )

    action_type = resolved_action.get("action_type")

    if action_type != CANONICAL_OPERATOR_ACTION_TYPE:
        raise HTTPException(
            status_code=400,
            detail="Unsupported operator action type",
        )

    target_service_state = resolved_action.get(
        "target_service_state"
    )

    if not target_service_state:
        raise HTTPException(
            status_code=400,
            detail="Operator action missing target service state",
        )

    requested_by = payload.get(
        "requested_by",
        "platform_admin",
    )

    reason = payload.get(
        "reason",
        "canonical operator action",
    )

    client_key = (payload.get("client_key") or "").strip()

    if not client_key:
        raise HTTPException(status_code=400, detail="client_key is required")

    return execute_workflow_service_state_update(
        workflow_id=workflow_id,
        client_key=client_key,
        service_state=target_service_state,
        reason=reason,
        requested_by=requested_by,
        authorization=authorization,
    )

@app.post("/workflows/{workflow_id}/service-state")
async def update_workflow_service_state(
    workflow_id: str,
    request: Request,
    authorization: str = Header(None),
):
    payload = require_auth_token(authorization)

    if not is_platform_admin(payload):
        raise HTTPException(status_code=403, detail="Platform admin access required")

    data = await request.json()

    client_key = (data.get("client_key") or "").strip()
    service_state = (data.get("service_state") or "").strip()
    reason = (data.get("reason") or "manual_admin_update").strip()

    if not workflow_id:
        raise HTTPException(status_code=400, detail="workflow_id is required")

    if not client_key:
        raise HTTPException(status_code=400, detail="client_key is required")

    if not service_state:
        raise HTTPException(status_code=400, detail="service_state is required")

    allowed_service_states = {
        "pending",
        "triaged",
        "awaiting_dispatch",
        "scheduled",
        "assigned",
        "in_progress",
        "resolved",
        "failed",
    }

    if service_state not in allowed_service_states:
        raise HTTPException(status_code=400, detail="invalid service_state")

    return execute_workflow_service_state_update(
        workflow_id=workflow_id,
        client_key=client_key,
        service_state=service_state,
        reason=reason,
        requested_by=payload.get("email"),
        authorization=authorization,
    )


# -------------------------------------------------
# CLIENT ACCOUNT ENDPOINT
# -------------------------------------------------
@app.get("/client/account")
def get_client_account(
    client_key: str = Query(None), authorization: str = Header(None)
):
    payload = require_auth_token(authorization)
    effective_client_key = resolve_effective_client_key(payload, client_key)
    database_url = os.getenv("DATABASE_URL")

    if not effective_client_key:
        raise HTTPException(
            status_code=400, detail="client_key is required for account view"
        )

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                # -------------------------------------------------
                # CLIENT
                # -------------------------------------------------
                cur.execute(
                    """
                    SELECT
                        client_key,
                        business_name,
                        vertical,
                        plan_tier,
                        status,
                        timezone,
                        created_at
                    FROM clients
                    WHERE client_key = %s
                    LIMIT 1;
                """,
                    (effective_client_key,),
                )

                client_row = cur.fetchone()

                if not client_row:
                    raise HTTPException(status_code=404, detail="client not found")

                # -------------------------------------------------
                # ACTIVE PLAN
                # -------------------------------------------------
                cur.execute(
                    """
                    SELECT
                        plan_name,
                        concurrent_call_limit,
                        included_minutes,
                        overage_rate,
                        billing_anchor_day,
                        activation_date
                    FROM client_plans
                    WHERE client_key = %s
                      AND active = TRUE
                    ORDER BY created_at DESC
                    LIMIT 1;
                """,
                    (effective_client_key,),
                )

                plan_row = cur.fetchone()

                if not plan_row:
                    raise HTTPException(
                        status_code=404, detail="active client plan not found"
                    )

                plan_name = plan_row[0]
                concurrent_call_limit = plan_row[1]
                included_minutes = plan_row[2]
                overage_rate = float(plan_row[3]) if plan_row[3] is not None else None
                billing_anchor_day = plan_row[4]
                activation_date = plan_row[5]

                # -------------------------------------------------
                # BILLING PERIOD
                # -------------------------------------------------
                now = datetime.now(timezone.utc)

                anchor_day = int(billing_anchor_day or 1)
                anchor_day = max(1, min(anchor_day, 28))

                current_start = datetime(
                    now.year, now.month, anchor_day, tzinfo=timezone.utc
                )

                if now < current_start:
                    previous_month = now.month - 1
                    previous_year = now.year

                    if previous_month == 0:
                        previous_month = 12
                        previous_year -= 1

                    current_start = datetime(
                        previous_year, previous_month, anchor_day, tzinfo=timezone.utc
                    )

                next_month = current_start.month + 1
                next_year = current_start.year

                if next_month == 13:
                    next_month = 1
                    next_year += 1

                next_start = datetime(
                    next_year, next_month, anchor_day, tzinfo=timezone.utc
                )

                current_end = next_start - timedelta(seconds=1)

                # -------------------------------------------------
                # USAGE SUMMARY
                # -------------------------------------------------
                cur.execute(
                    """
                    SELECT
                        COALESCE(SUM(billable_minutes), 0),
                        COUNT(*)
                    FROM calls
                    WHERE client_key = %s
                      AND created_at >= %s
                      AND created_at < %s;
                """,
                    (effective_client_key, current_start, next_start),
                )

                usage_row = cur.fetchone()

                minutes_used = float(usage_row[0] or 0)
                call_count = int(usage_row[1] or 0)

                if included_minutes is None:
                    remaining_minutes = None
                    overage_minutes = 0
                    estimated_overage = 0
                else:
                    remaining_minutes = max(float(included_minutes) - minutes_used, 0)
                    overage_minutes = max(minutes_used - float(included_minutes), 0)
                    estimated_overage = round(
                        overage_minutes * float(overage_rate or 0), 2
                    )

                # -------------------------------------------------
                # INVOICE HISTORY
                # -------------------------------------------------
                cur.execute(
                    """
                    SELECT
                        invoice_number,
                        billing_period_start,
                        billing_period_end,
                        issue_date,
                        due_date,
                        subtotal,
                        tax,
                        total,
                        status,
                        minutes_included,
                        minutes_used,
                        overage_minutes,
                        overage_rate,
                        pdf_url
                    FROM invoices
                    WHERE client_key = %s
                    ORDER BY issue_date DESC
                    LIMIT 12;
                """,
                    (effective_client_key,),
                )

                invoice_rows = cur.fetchall()

        invoices = []

        for row in invoice_rows:
            invoices.append(
                {
                    "invoice_number": row[0],
                    "billing_period_start": row[1].isoformat() if row[1] else None,
                    "billing_period_end": row[2].isoformat() if row[2] else None,
                    "issue_date": row[3].isoformat() if row[3] else None,
                    "due_date": row[4].isoformat() if row[4] else None,
                    "subtotal": float(row[5] or 0),
                    "tax": float(row[6] or 0),
                    "total": float(row[7] or 0),
                    "status": row[8],
                    "minutes_included": row[9],
                    "minutes_used": float(row[10] or 0),
                    "overage_minutes": float(row[11] or 0),
                    "overage_rate": float(row[12]) if row[12] is not None else None,
                    "pdf_url": row[13],
                }
            )

        return {
            "status": "ok",
            "client": {
                "client_key": client_row[0],
                "business_name": client_row[1],
                "vertical": client_row[2],
                "plan_tier": client_row[3],
                "status": client_row[4],
                "timezone": client_row[5],
                "timezone_label": timezone_label(client_row[5]),
                "created_at": client_row[6].isoformat() if client_row[6] else None,
            },
            "plan": {
                "plan_name": plan_name,
                "concurrent_call_limit": concurrent_call_limit,
                "included_minutes": included_minutes,
                "overage_rate": overage_rate,
                "billing_anchor_day": billing_anchor_day,
                "activation_date": (
                    activation_date.isoformat() if activation_date else None
                ),
            },
            "billing_period": {
                "start": current_start.isoformat(),
                "end": current_end.isoformat(),
                "next_start": next_start.isoformat(),
            },
            "usage": {
                "call_count": call_count,
                "minutes_used": round(minutes_used, 2),
                "included_minutes": included_minutes,
                "remaining_minutes": (
                    round(remaining_minutes, 2)
                    if remaining_minutes is not None
                    else None
                ),
                "overage_minutes": round(overage_minutes, 2),
                "estimated_overage": estimated_overage,
            },
            "invoice_history": invoices,
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Client account read failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# INVOICE GENERATION ENDPOINT
# -------------------------------------------------
@app.post("/invoices/generate-current")
async def generate_current_invoice(request: Request, authorization: str = Header(None)):
    payload = require_auth_token(authorization)

    if not is_platform_admin(payload):
        raise HTTPException(status_code=403, detail="Platform admin access required")

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    data = await request.json()
    client_key = (data.get("client_key") or "").strip()

    if not client_key:
        raise HTTPException(status_code=400, detail="client_key is required")

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        plan_name,
                        included_minutes,
                        overage_rate,
                        billing_anchor_day
                    FROM client_plans
                    WHERE client_key = %s
                      AND active = TRUE
                    ORDER BY created_at DESC
                    LIMIT 1;
                """,
                    (client_key,),
                )

                plan = cur.fetchone()

                if not plan:
                    raise HTTPException(
                        status_code=404, detail="active client plan not found"
                    )

                plan_name = plan[0]
                included_minutes = plan[1] or 0
                overage_rate = Decimal(str(plan[2] or 0))
                billing_anchor_day = int(plan[3] or 1)

                now = datetime.now(timezone.utc)
                anchor_day = max(1, min(billing_anchor_day, 28))

                period_start = datetime(
                    now.year, now.month, anchor_day, tzinfo=timezone.utc
                )

                if now < period_start:
                    previous_month = now.month - 1
                    previous_year = now.year

                    if previous_month == 0:
                        previous_month = 12
                        previous_year -= 1

                    period_start = datetime(
                        previous_year, previous_month, anchor_day, tzinfo=timezone.utc
                    )

                next_month = period_start.month + 1
                next_year = period_start.year

                if next_month == 13:
                    next_month = 1
                    next_year += 1

                period_end_exclusive = datetime(
                    next_year, next_month, anchor_day, tzinfo=timezone.utc
                )
                period_end_display = period_end_exclusive - timedelta(seconds=1)

                # Prevent duplicate invoice snapshots for same client + period.
                cur.execute(
                    """
                    SELECT id, invoice_number
                    FROM invoices
                    WHERE client_key = %s
                      AND billing_period_start = %s
                      AND billing_period_end = %s
                    LIMIT 1;
                """,
                    (client_key, period_start, period_end_display),
                )

                existing = cur.fetchone()

                if existing:
                    return {
                        "status": "ok",
                        "message": "Invoice already exists for this billing period",
                        "invoice_id": existing[0],
                        "invoice_number": existing[1],
                        "created": False,
                    }

                cur.execute(
                    """
                    SELECT
                        COALESCE(SUM(billable_minutes), 0),
                        COUNT(*)
                    FROM calls
                    WHERE client_key = %s
                      AND created_at >= %s
                      AND created_at < %s;
                """,
                    (client_key, period_start, period_end_exclusive),
                )

                usage = cur.fetchone()

                minutes_used = Decimal(str(usage[0] or 0))
                call_count = int(usage[1] or 0)

                overage_minutes = max(
                    minutes_used - Decimal(str(included_minutes)), Decimal("0")
                )
                overage_amount = (overage_minutes * overage_rate).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )

                subtotal = overage_amount
                tax = Decimal("0.00")
                total = subtotal + tax

                invoice_number = f"GOS-{client_key.upper()}-{period_start.strftime('%Y%m%d')}-{int(time.time())}"

                due_date = now + timedelta(days=15)

                cur.execute(
                    """
                    INSERT INTO invoices (
                        invoice_number,
                        client_key,
                        billing_period_start,
                        billing_period_end,
                        issue_date,
                        due_date,
                        subtotal,
                        tax,
                        total,
                        status,
                        minutes_included,
                        minutes_used,
                        overage_minutes,
                        overage_rate
                    )
                    VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, %s, 'issued', %s, %s, %s, %s)
                    RETURNING id;
                """,
                    (
                        invoice_number,
                        client_key,
                        period_start,
                        period_end_display,
                        due_date,
                        subtotal,
                        tax,
                        total,
                        included_minutes,
                        minutes_used,
                        overage_minutes,
                        overage_rate,
                    ),
                )

                invoice = cur.fetchone()
                invoice_id = invoice[0]

                cur.execute(
                    """
                    INSERT INTO invoice_line_items (
                        invoice_id,
                        description,
                        quantity,
                        unit_price,
                        amount
                    )
                    VALUES (%s, %s, %s, %s, %s);
                """,
                    (
                        invoice_id,
                        f"{plan_name} usage overage",
                        overage_minutes,
                        overage_rate,
                        overage_amount,
                    ),
                )

            conn.commit()

        return {
            "status": "ok",
            "message": "Invoice generated",
            "created": True,
            "invoice": {
                "invoice_id": invoice_id,
                "invoice_number": invoice_number,
                "client_key": client_key,
                "billing_period_start": period_start.isoformat(),
                "billing_period_end": period_end_display.isoformat(),
                "call_count": call_count,
                "minutes_included": included_minutes,
                "minutes_used": float(minutes_used),
                "overage_minutes": float(overage_minutes),
                "overage_rate": float(overage_rate),
                "subtotal": float(subtotal),
                "tax": float(tax),
                "total": float(total),
                "status": "issued",
            },
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Invoice generation failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# INVOICES READ ENDPOINT
# -------------------------------------------------
@app.get("/invoices")
def get_invoices(client_key: str = Query(None), authorization: str = Header(None)):
    payload = require_auth_token(authorization)
    effective_client_key = resolve_effective_client_key(payload, client_key)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                where_sql, params = scoped_where_clause(
                    "invoices", effective_client_key
                )

                cur.execute(
                    f"""
                    SELECT
                        id,
                        invoice_number,
                        client_key,
                        billing_period_start,
                        billing_period_end,
                        issue_date,
                        due_date,
                        subtotal,
                        tax,
                        total,
                        status,
                        minutes_included,
                        minutes_used,
                        overage_minutes,
                        overage_rate,
                        pdf_url,
                        created_at,
                        updated_at
                    FROM invoices
                    {where_sql}
                    ORDER BY issue_date DESC
                    LIMIT 100;
                """,
                    params,
                )

                rows = cur.fetchall()

        invoices = []

        for row in rows:
            invoices.append(
                {
                    "invoice_id": row[0],
                    "invoice_number": row[1],
                    "client_key": row[2],
                    "billing_period_start": row[3].isoformat() if row[3] else None,
                    "billing_period_end": row[4].isoformat() if row[4] else None,
                    "issue_date": row[5].isoformat() if row[5] else None,
                    "due_date": row[6].isoformat() if row[6] else None,
                    "subtotal": float(row[7] or 0),
                    "tax": float(row[8] or 0),
                    "total": float(row[9] or 0),
                    "status": row[10],
                    "minutes_included": row[11],
                    "minutes_used": float(row[12] or 0),
                    "overage_minutes": float(row[13] or 0),
                    "overage_rate": float(row[14]) if row[14] is not None else None,
                    "pdf_url": row[15],
                    "created_at": row[16].isoformat() if row[16] else None,
                    "updated_at": row[17].isoformat() if row[17] else None,
                }
            )

        return {
            "status": "ok",
            "count": len(invoices),
            "scope": "platform" if is_platform_admin(payload) else effective_client_key,
            "client_key_filter": effective_client_key,
            "invoices": invoices,
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Invoices read failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# INVOICE DETAIL ENDPOINT
# -------------------------------------------------
@app.get("/invoices/detail")
def get_invoice_detail(
    invoice_number: str = Query(None), authorization: str = Header(None)
):
    payload = require_auth_token(authorization)
    database_url = os.getenv("DATABASE_URL")

    if not invoice_number:
        raise HTTPException(status_code=400, detail="invoice_number is required")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        invoice_number,
                        client_key,
                        billing_period_start,
                        billing_period_end,
                        issue_date,
                        due_date,
                        subtotal,
                        tax,
                        total,
                        status,
                        minutes_included,
                        minutes_used,
                        overage_minutes,
                        overage_rate,
                        pdf_url,
                        created_at,
                        updated_at
                    FROM invoices
                    WHERE invoice_number = %s
                    LIMIT 1;
                """,
                    (invoice_number,),
                )

                row = cur.fetchone()

                if not row:
                    raise HTTPException(status_code=404, detail="invoice not found")

                invoice_client_key = row[2]
                effective_client_key = resolve_effective_client_key(
                    payload, invoice_client_key
                )

                if effective_client_key != invoice_client_key:
                    raise HTTPException(
                        status_code=403, detail="Access denied for invoice"
                    )

                invoice_id = row[0]

                cur.execute(
                    """
                    SELECT
                        id,
                        description,
                        quantity,
                        unit_price,
                        amount,
                        created_at
                    FROM invoice_line_items
                    WHERE invoice_id = %s
                    ORDER BY id ASC;
                """,
                    (invoice_id,),
                )

                line_rows = cur.fetchall()

        line_items = []

        for item in line_rows:
            line_items.append(
                {
                    "line_item_id": item[0],
                    "description": item[1],
                    "quantity": float(item[2] or 0),
                    "unit_price": float(item[3] or 0),
                    "amount": float(item[4] or 0),
                    "created_at": item[5].isoformat() if item[5] else None,
                }
            )

        return {
            "status": "ok",
            "invoice": {
                "invoice_id": row[0],
                "invoice_number": row[1],
                "client_key": row[2],
                "billing_period_start": row[3].isoformat() if row[3] else None,
                "billing_period_end": row[4].isoformat() if row[4] else None,
                "issue_date": row[5].isoformat() if row[5] else None,
                "due_date": row[6].isoformat() if row[6] else None,
                "subtotal": float(row[7] or 0),
                "tax": float(row[8] or 0),
                "total": float(row[9] or 0),
                "status": row[10],
                "minutes_included": row[11],
                "minutes_used": float(row[12] or 0),
                "overage_minutes": float(row[13] or 0),
                "overage_rate": float(row[14]) if row[14] is not None else None,
                "pdf_url": row[15],
                "created_at": row[16].isoformat() if row[16] else None,
                "updated_at": row[17].isoformat() if row[17] else None,
            },
            "line_items": line_items,
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Invoice detail read failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# CLIENT SETTINGS READ ENDPOINT
# -------------------------------------------------
@app.get("/client-settings")
def get_client_settings(
    client_key: str = Query(None), authorization: str = Header(None)
):
    payload = require_auth_token(authorization)
    effective_client_key = resolve_effective_client_key(payload, client_key)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                where_sql, params = scoped_where_clause(
                    "client_settings", effective_client_key
                )
                cur.execute(
                    f"""
                    SELECT
                        client_key,
                        greeting_enabled,
                        custom_greeting,
                        end_call_enabled,
                        custom_end_call,
                        caller_confirmation_enabled,
                        business_sms_enabled,
                        caller_sms_enabled,
                        emergency_detection_enabled,
                        after_hours_enabled,
                        calendar_enabled,
                        crm_enabled,
                        retell_agent_id,
                        twilio_inbound_number,
                        twilio_outbound_number,
                        created_at,
                        updated_at
                    FROM client_settings
                    {where_sql}
                    ORDER BY created_at ASC;
                """,
                    params,
                )

                rows = cur.fetchall()

        settings = []

        for row in rows:
            settings.append(
                {
                    "client_key": row[0],
                    "greeting_enabled": row[1],
                    "custom_greeting": row[2],
                    "end_call_enabled": row[3],
                    "custom_end_call": row[4],
                    "caller_confirmation_enabled": row[5],
                    "business_sms_enabled": row[6],
                    "caller_sms_enabled": row[7],
                    "emergency_detection_enabled": row[8],
                    "after_hours_enabled": row[9],
                    "calendar_enabled": row[10],
                    "crm_enabled": row[11],
                    "retell_agent_id": row[12],
                    "twilio_inbound_number": row[13],
                    "twilio_outbound_number": row[14],
                    "created_at": row[15].isoformat() if row[15] else None,
                    "updated_at": row[16].isoformat() if row[16] else None,
                }
            )

        return {
            "status": "ok",
            "count": len(settings),
            "scope": "platform" if is_platform_admin(payload) else effective_client_key,
            "client_key_filter": effective_client_key,
            "client_settings": settings,
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Client settings read failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# CLIENT CONTACTS READ ENDPOINT
# -------------------------------------------------
@app.get("/client-contacts")
def get_client_contacts(
    client_key: str = Query(None), authorization: str = Header(None)
):
    payload = require_auth_token(authorization)
    effective_client_key = resolve_effective_client_key(payload, client_key)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                where_sql, params = scoped_where_clause(
                    "client_contacts", effective_client_key
                )
                cur.execute(
                    f"""
                    SELECT client_key, first_name, last_name, email, phone, role, is_primary, created_at, updated_at
                    FROM client_contacts
                    {where_sql}
                    ORDER BY created_at DESC;
                """,
                    params,
                )

                rows = cur.fetchall()

        contacts = [
            {
                "client_key": row[0],
                "first_name": row[1],
                "last_name": row[2],
                "email": row[3],
                "phone": row[4],
                "role": row[5],
                "is_primary": row[6],
                "created_at": row[7].isoformat() if row[7] else None,
                "updated_at": row[8].isoformat() if row[8] else None,
            }
            for row in rows
        ]

        return {
            "status": "ok",
            "count": len(contacts),
            "scope": "platform" if is_platform_admin(payload) else effective_client_key,
            "client_key_filter": effective_client_key,
            "contacts": contacts,
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Client contacts read failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# CLIENT ADDRESSES READ ENDPOINT
# -------------------------------------------------
@app.get("/client-addresses")
def get_client_addresses(
    client_key: str = Query(None), authorization: str = Header(None)
):
    payload = require_auth_token(authorization)
    effective_client_key = resolve_effective_client_key(payload, client_key)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                where_sql, params = scoped_where_clause(
                    "client_addresses", effective_client_key
                )
                cur.execute(
                    f"""
                    SELECT
                        client_key,
                        address_line_1,
                        address_line_2,
                        city,
                        state_province,
                        postal_code,
                        country,
                        is_primary,
                        created_at,
                        updated_at
                    FROM client_addresses
                    {where_sql}
                    ORDER BY created_at DESC;
                """,
                    params,
                )

                rows = cur.fetchall()

        addresses = []

        for row in rows:
            addresses.append(
                {
                    "client_key": row[0],
                    "address_line_1": row[1],
                    "address_line_2": row[2],
                    "city": row[3],
                    "state_province": row[4],
                    "postal_code": row[5],
                    "country": row[6],
                    "is_primary": row[7],
                    "created_at": row[8].isoformat() if row[8] else None,
                    "updated_at": row[9].isoformat() if row[9] else None,
                }
            )

        return {
            "status": "ok",
            "count": len(addresses),
            "scope": "platform" if is_platform_admin(payload) else effective_client_key,
            "client_key_filter": effective_client_key,
            "addresses": addresses,
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Client addresses read failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# CLIENT SETTINGS UPDATE ENDPOINT
# -------------------------------------------------
@app.post("/client-settings/update-sms-number")
async def update_sms_number(request: Request, authorization: str = Header(None)):
    payload = require_auth_token(authorization)

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    data = await request.json()

    requested_client_key = (data.get("client_key") or "").strip()
    client_key = require_client_admin_or_platform(payload, requested_client_key)
    twilio_outbound_number = normalize_phone(data.get("twilio_outbound_number"))

    if not client_key or not twilio_outbound_number:
        return {
            "status": "error",
            "message": "client_key and valid twilio_outbound_number are required",
        }

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE client_settings
                    SET
                        twilio_outbound_number = %s,
                        updated_at = NOW()
                    WHERE client_key = %s;
                """,
                    (twilio_outbound_number, client_key),
                )

                updated = cur.rowcount

            conn.commit()

        if updated == 0:
            raise HTTPException(
                status_code=404, detail="client_settings record not found"
            )

        return {
            "status": "ok",
            "client_key": client_key,
            "twilio_outbound_number": twilio_outbound_number,
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("SMS number update failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# CLIENT SMS SETTINGS UPDATE ENDPOINT
# -------------------------------------------------
@app.post("/client-settings/update-sms-settings")
async def update_sms_settings(request: Request, authorization: str = Header(None)):
    payload = require_auth_token(authorization)

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    data = await request.json()

    requested_client_key = (data.get("client_key") or "").strip()
    client_key = require_client_admin_or_platform(payload, requested_client_key)
    business_sms_enabled = data.get("business_sms_enabled")
    caller_sms_enabled = data.get("caller_sms_enabled")

    if not client_key:
        return {"status": "error", "message": "client_key is required"}

    if not isinstance(business_sms_enabled, bool) or not isinstance(
        caller_sms_enabled, bool
    ):
        return {
            "status": "error",
            "message": "business_sms_enabled and caller_sms_enabled must be true or false",
        }

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE client_settings
                    SET
                        business_sms_enabled = %s,
                        caller_sms_enabled = %s,
                        updated_at = NOW()
                    WHERE client_key = %s;
                """,
                    (business_sms_enabled, caller_sms_enabled, client_key),
                )

                updated = cur.rowcount

            conn.commit()

        if updated == 0:
            return {
                "status": "error",
                "message": "client_settings record not found",
                "client_key": client_key,
            }

        return {
            "status": "ok",
            "client_key": client_key,
            "business_sms_enabled": business_sms_enabled,
            "caller_sms_enabled": caller_sms_enabled,
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("SMS settings update failed")
        return {"status": "error", "message": str(e)}


# -------------------------------------------------
# HELPERS
# -------------------------------------------------
def normalize_phone(text: str):
    if not text:
        return None

    digits = re.findall(r"\d", str(text))

    if len(digits) < 10:
        return None

    phone = "".join(digits[-10:])

    if len(set(phone)) == 1:
        return None

    return f"+1{phone}"


def extract_name(text: str):
    if not text:
        return None

    match = re.search(
        r"(my name is|this is|i am|i'm)\s+([a-zA-Z]+\s+[a-zA-Z]+)", text, re.IGNORECASE
    )

    if match:
        return match.group(2).title()

    return None


def clean_urgency(value):
    value = (value or "").lower().strip()

    if value == "normal":
        return "standard"

    if value not in ["urgent", "standard"]:
        return "standard"

    return value


def clean_call_outcome(value):
    value = (value or "").lower().strip()

    allowed = {
        "confirmed",
        "address_fallback",
        "failed_phone",
        "off_topic",
        "unable_to_complete",
        "unknown",
    }

    if value in allowed:
        return value

    return "unknown"


def build_transcript_text(messages):
    if not isinstance(messages, list):
        return ""

    user_parts = []

    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            content = m.get("content") or m.get("text") or ""
            if content:
                user_parts.append(str(content))

    return " ".join(user_parts).strip()


def cleanup_state():
    now = time.time()

    for k in list(PROCESSED_META.keys()):
        if now - PROCESSED_META[k] > PROCESSED_TTL:
            PROCESSED_META.pop(k, None)
            PROCESSED_CALLS.discard(k)

    for k in list(CALL_PHONE_META.keys()):
        if now - CALL_PHONE_META[k] > PROCESSED_TTL:
            CALL_PHONE_META.pop(k, None)
            CALL_PHONE_MAP.pop(k, None)


def classify_hvac_issue(text: str):
    text = (text or "").lower()

    issue_type = "other"
    cooling_pattern = r"\b(ac|a/c|air conditioning|cool|cooling)\b"

    if any(k in text for k in ["heat", "heating", "heater", "furnace"]):
        issue_type = "no_heat"
    elif re.search(cooling_pattern, text):
        issue_type = "no_cooling"
    elif "leak" in text:
        issue_type = "leak"
    elif any(
        k in text
        for k in ["service", "maintenance", "checkup", "check up", "tune up", "routine"]
    ):
        issue_type = "maintenance"

    urgent_keywords = [
        "no heat",
        "no heating",
        "no hot air",
        "not heating",
        "not blowing hot air",
        "heater is out",
        "heater is down",
        "heater down",
        "heat is out",
        "heating is out",
        "furnace is out",
        "furnace is down",
        "furnace down",
        "furnace stopped",
        "furnace not working",
        "gas leak",
        "gas smell",
        "smell gas",
        "carbon monoxide",
        "water leak",
        "flood",
        "emergency",
        "freezing",
        "urgent",
    ]

    routine_keywords = [
        "service call",
        "regular service",
        "routine service",
        "checkup",
        "check up",
        "maintenance",
        "tune up",
        "service on my hvac",
        "schedule service",
    ]

    if any(k in text for k in routine_keywords):
        urgency = "standard"
    elif any(k in text for k in urgent_keywords):
        urgency = "urgent"
    elif any(k in text for k in ["heat", "heating", "heater", "furnace"]):
        urgency = "urgent"
    else:
        urgency = "standard"

    return urgency, issue_type


def build_short_summary(urgency, issue_type):
    if urgency == "urgent":
        if issue_type == "no_heat":
            return "EMERGENCY SERVICE REQUEST — Heater/furnace down or not heating."
        if issue_type == "no_cooling":
            return "EMERGENCY SERVICE REQUEST — Cooling/AC issue."
        if issue_type == "leak":
            return "EMERGENCY SERVICE REQUEST — Leak reported."
        return "EMERGENCY SERVICE REQUEST — Urgent HVAC issue."

    if issue_type == "maintenance":
        return "HVAC SERVICE REQUEST — Routine service/checkup."
    if issue_type == "no_cooling":
        return "HVAC SERVICE REQUEST — Cooling/AC issue."
    if issue_type == "no_heat":
        return "HVAC SERVICE REQUEST — Heating issue."
    if issue_type == "leak":
        return "HVAC SERVICE REQUEST — Leak reported."

    return "HVAC SERVICE REQUEST — Standard HVAC service request."


# -------------------------------------------------
# DATABASE CLIENT LOOKUP
# -------------------------------------------------
def get_client_by_key(client_key: str):
    """
    Primary client lookup from PostgreSQL.

    Falls back to the hardcoded CLIENTS map if:
    - DATABASE_URL is missing
    - DB lookup fails
    - client row is not found
    """

    database_url = os.getenv("DATABASE_URL")

    if not client_key:
        return None

    if database_url:
        try:
            with psycopg.connect(database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            client_key,
                            business_name,
                            business_phone,
                            caller_sms_enabled,
                            status,
                            vertical,
                            plan_tier,
                            timezone,
                            inbound_phone
                        FROM clients
                        WHERE client_key = %s
                        LIMIT 1;
                    """,
                        (client_key,),
                    )

                    row = cur.fetchone()

            if row:
                client = {
                    "client_key": row[0],
                    "business_name": row[1],
                    "business_phone": row[2],
                    "caller_enabled": row[3],
                    "status": row[4],
                    "vertical": row[5],
                    "plan_tier": row[6],
                    "timezone": row[7],
                    "inbound_phone": row[8],
                    "source": "database",
                }

                if client["status"] != "active":
                    logger.warning("Client inactive: %s", client_key)
                    return None

                return client

            logger.warning("Client DB miss: %s", client_key)

        except Exception as e:
            logger.exception("Client DB lookup failed")

    fallback_client = CLIENTS.get(client_key)

    if fallback_client:
        logger.warning("Client fallback used: %s", client_key)

        return {
            **fallback_client,
            "client_key": client_key,
            "status": "active",
            "source": "fallback",
        }

    return None


# -------------------------------------------------
# DATABASE INBOUND PHONE ROUTING
# -------------------------------------------------
def get_client_by_inbound_phone(inbound_phone: str):
    """
    Routes inbound calls using the dedicated Gosonic
    inbound agent phone number.

    This becomes the primary multi-tenant routing layer.
    """

    database_url = os.getenv("DATABASE_URL")

    formatted_phone = normalize_phone(inbound_phone)

    if not database_url or not formatted_phone:
        return None

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        client_key,
                        business_name,
                        business_phone,
                        caller_sms_enabled,
                        status,
                        vertical,
                        plan_tier,
                        timezone,
                        inbound_phone
                    FROM clients
                    WHERE inbound_phone = %s
                    LIMIT 1;
                """,
                    (formatted_phone,),
                )

                row = cur.fetchone()

        if not row:
            log_info("[INBOUND ROUTING MISS]", inbound_phone=formatted_phone)
            return None

        client = {
            "client_key": row[0],
            "business_name": row[1],
            "business_phone": row[2],
            "caller_enabled": row[3],
            "status": row[4],
            "vertical": row[5],
            "plan_tier": row[6],
            "timezone": row[7],
            "inbound_phone": row[8],
            "source": "database_inbound_phone",
        }

        if client["status"] != "active":
            log_info("[INBOUND CLIENT INACTIVE]", inbound_phone=formatted_phone)
            return None

        log_info(
            "[INBOUND ROUTED]",
            inbound_phone=formatted_phone,
            client_key=client["client_key"],
        )

        return client

    except Exception as e:
        logger.exception("Inbound routing failed")
        return None


# -------------------------------------------------
# CLIENT SETTINGS LOOKUP
# -------------------------------------------------
def get_client_settings_by_key(client_key: str):
    """
    Runtime client behavior/settings lookup.

    This becomes the central configuration layer
    for platform behavior.
    """

    database_url = os.getenv("DATABASE_URL")

    if not database_url or not client_key:
        return None

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        client_key,
                        greeting_enabled,
                        custom_greeting,
                        end_call_enabled,
                        custom_end_call,
                        caller_confirmation_enabled,
                        business_sms_enabled,
                        caller_sms_enabled,
                        emergency_detection_enabled,
                        after_hours_enabled,
                        calendar_enabled,
                        crm_enabled,
                        retell_agent_id,
                        twilio_inbound_number,
                        twilio_outbound_number
                    FROM client_settings
                    WHERE client_key = %s
                    LIMIT 1;
                """,
                    (client_key,),
                )

                row = cur.fetchone()

        if not row:
            logger.warning("Client settings miss: %s", client_key)
            return None

        settings = {
            "client_key": row[0],
            "greeting_enabled": row[1],
            "custom_greeting": row[2],
            "end_call_enabled": row[3],
            "custom_end_call": row[4],
            "caller_confirmation_enabled": row[5],
            "business_sms_enabled": row[6],
            "caller_sms_enabled": row[7],
            "emergency_detection_enabled": row[8],
            "after_hours_enabled": row[9],
            "calendar_enabled": row[10],
            "crm_enabled": row[11],
            "retell_agent_id": row[12],
            "twilio_inbound_number": row[13],
            "twilio_outbound_number": row[14],
        }

        return settings

    except Exception as e:
        logger.exception("Client settings lookup failed")
        return None


# -------------------------------------------------
# DATABASE CALL PERSISTENCE
# -------------------------------------------------
def save_call_record(
    call_id,
    client_key,
    caller_name,
    caller_phone,
    service_address,
    issue_description,
    issue_type,
    urgency,
    call_outcome,
    sms_policy_reason,
    business_notified,
    business_error,
    caller_notified,
    caller_error,
    raw_payload,
    call_status=None,
    webhook_status=None,
    agent_id=None,
    call_direction=None,
    confidence=None,
    processing_latency_ms=None,
    escalation_reason=None,
    transcript=None,
    ended_at=None,
    caller_phone_source=None,
    caller_identity_status=None,
    caller_phone_verified=False,
):
    """
    Persists analyzed call results to PostgreSQL.

    Uses ON CONFLICT so Retell retries or duplicate webhook events
    do not create duplicate call records.
    """

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        logger.warning("Call save skipped: DATABASE_URL not configured")
        return {"saved": False, "error": "DATABASE_URL not configured"}

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                # -------------------------------------------------
                # BILLABLE USAGE
                # -------------------------------------------------
                call_payload = raw_payload.get("call") or {}

                duration_ms = (
                    call_payload.get("duration_ms")
                    or raw_payload.get("duration_ms")
                    or 0
                )

                try:
                    call_duration_seconds = (
                        round(int(duration_ms) / 1000) if duration_ms else 0
                    )
                except Exception:
                    call_duration_seconds = 0

                billable_minutes = round(call_duration_seconds / 60, 2)

                cur.execute(
                    """
                    INSERT INTO calls (
                        call_id,
                        client_key,
                        caller_name,
                        caller_phone,
                        service_address,
                        issue_description,
                        issue_type,
                        urgency,
                        call_outcome,
                        sms_policy_reason,
                        business_notified,
                        business_error,
                        caller_notified,
                        caller_error,
                        raw_payload,
                        call_duration_seconds,
                        billable_minutes,
                        call_status,
                        webhook_status,
                        agent_id,
                        call_direction,
                        confidence,
                        processing_latency_ms,
                        escalation_reason,
                        transcript,
                        ended_at,
                        caller_phone_source,
                        caller_identity_status,
                        caller_phone_verified
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (call_id) DO UPDATE SET
                        caller_name = EXCLUDED.caller_name,
                        caller_phone = EXCLUDED.caller_phone,
                        service_address = EXCLUDED.service_address,
                        issue_description = EXCLUDED.issue_description,
                        issue_type = EXCLUDED.issue_type,
                        urgency = EXCLUDED.urgency,
                        call_outcome = EXCLUDED.call_outcome,
                        sms_policy_reason = EXCLUDED.sms_policy_reason,
                        business_notified = EXCLUDED.business_notified,
                        business_error = EXCLUDED.business_error,
                        caller_notified = EXCLUDED.caller_notified,
                        caller_error = EXCLUDED.caller_error,
                        raw_payload = EXCLUDED.raw_payload,
                        call_duration_seconds = EXCLUDED.call_duration_seconds,
                        billable_minutes = EXCLUDED.billable_minutes,
                        call_status = EXCLUDED.call_status,
                        webhook_status = EXCLUDED.webhook_status,
                        agent_id = EXCLUDED.agent_id,
                        call_direction = EXCLUDED.call_direction,
                        confidence = EXCLUDED.confidence,
                        processing_latency_ms = EXCLUDED.processing_latency_ms,
                        escalation_reason = EXCLUDED.escalation_reason,
                        transcript = EXCLUDED.transcript,
                        ended_at = EXCLUDED.ended_at,
                        caller_phone_source = EXCLUDED.caller_phone_source,
                        caller_identity_status = EXCLUDED.caller_identity_status,
                        caller_phone_verified = EXCLUDED.caller_phone_verified;
                """,
                    (
                        call_id,
                        client_key,
                        caller_name,
                        caller_phone,
                        service_address,
                        issue_description,
                        issue_type,
                        urgency,
                        call_outcome,
                        sms_policy_reason,
                        business_notified,
                        business_error,
                        caller_notified,
                        caller_error,
                        Jsonb(raw_payload),
                        call_duration_seconds,
                        billable_minutes,
                        call_status,
                        webhook_status,
                        agent_id,
                        call_direction,
                        confidence,
                        processing_latency_ms,
                        escalation_reason,
                        transcript,
                        ended_at,
                        caller_phone_source,
                        caller_identity_status,
                        caller_phone_verified,
                    ),
                )

                # -------------------------------------------------
                # WORKFLOW PERSISTENCE
                # -------------------------------------------------
                workflow_id = create_workflow_for_call(
                    cur=cur,
                    call_id=call_id,
                    client_key=client_key,
                    urgency=urgency,
                    call_outcome=call_outcome,
                    metadata={
                        "issue_type": issue_type,
                        "sms_policy_reason": sms_policy_reason,
                        "call_status": call_status,
                        "webhook_status": webhook_status,
                    },
                )

            conn.commit()

        logger.info("[CALL SAVED] call_id=%s", call_id)

        return {
            "saved": True,
            "error": None,
            "workflow_id": workflow_id,
        }

    except Exception as e:
        error = str(e)
        logger.exception("Call save failed")

        return {"saved": False, "error": error}


# -------------------------------------------------
# CALL EVENT PERSISTENCE
# -------------------------------------------------
def log_call_event(
    call_id,
    client_key,
    event_type,
    event_metadata=None,
    event_timestamp=None,
):
    """
    Writes immutable operational call events.

    This is the foundation for lifecycle timelines, SLA tracking,
    workflow observability, billing intelligence, and enterprise audit trails.
    """

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        logger.warning("Call event skipped: DATABASE_URL not configured")
        return {"saved": False, "error": "DATABASE_URL not configured"}

    if not call_id or not client_key or not event_type:
        return {
            "saved": False,
            "error": "call_id, client_key, and event_type are required",
        }

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO call_events (
                        call_id,
                        client_key,
                        event_type,
                        event_timestamp,
                        event_metadata
                    )
                    VALUES (%s, %s, %s, COALESCE(%s, NOW()), %s);
                    """,
                    (
                        call_id,
                        client_key,
                        event_type,
                        event_timestamp,
                        Jsonb(event_metadata or {}),
                    ),
                )

            conn.commit()

        logger.info(
            "[CALL EVENT SAVED] call_id=%s event_type=%s",
            call_id,
            event_type,
        )

        return {"saved": True, "error": None}

    except Exception as e:
        logger.exception("Call event save failed")
        return {"saved": False, "error": str(e)}

# -------------------------------------------------
# WORKFLOW PERSISTENCE
# -------------------------------------------------
# -------------------------------------------------
# WORKFLOW LIFECYCLE CANON
# -------------------------------------------------
WORKFLOW_STATUSES = {
    "created",
    "active",
    "awaiting_external",
    "escalated",
    "paused",
    "completed",
    "resolved",
    "cancelled",
    "failed",
    "expired",
}

WORKFLOW_STAGES = {
    "initiated",
    "intake_in_progress",
    "intake_completed",
    "triage_pending",
    "triaged",
    "notification_pending",
    "notification_sent",
    "scheduling_pending",
    "scheduled",
    "dispatch_pending",
    "dispatched",
    "acknowledged",
    "resolution_pending",
    "completed",
    "resolved",
    "escalation_required",
    "failed",
    "cancelled",
    "service",
}

OPERATIONAL_EVENT_TYPES = {
    "call.received",
    "call.started",
    "call.completed",
    "call.analyzed",
    "call.failed",

    "workflow.created",
    "workflow.activated",
    "workflow.stage_changed",
    "workflow.status_changed",
    "workflow.completed",
    "workflow.failed",
    "workflow.cancelled",
    "workflow.expired",

    "intake.started",
    "intake.completed",
    "intake.incomplete",
    "intake.corrected",

    "triage.started",
    "triage.completed",
    "triage.urgent_detected",
    "triage.standard_detected",
    "triage.failed",

    "notification.business_sent",
    "notification.business_failed",
    "notification.caller_sent",
    "notification.caller_failed",

    "booking.requested",
    "booking.availability_checked",
    "booking.slot_offered",
    "booking.confirmed",
    "booking.failed",
    "booking.cancelled",

    "dispatch.requested",
    "dispatch.assigned",
    "dispatch.acknowledged",
    "dispatch.completed",
    "dispatch.failed",

    "operator.acknowledged",

    "ownership.acknowledged",
    "ownership.assigned",
    "ownership.reassigned",
    "ownership.released",
    "ownership.escalated",

    "escalation.required",
    "escalation.created",
    "escalation.notified",
    "escalation.resolved",

    "integration.connected",
    "integration.disconnected",
    "integration.sync_started",
    "integration.sync_completed",
    "integration.sync_failed",

    "system.webhook_received",
    "system.webhook_verified",
    "system.persistence_succeeded",
    "system.persistence_failed",

    "service.pending",
    "service.triaged",
    "service.awaiting_dispatch",
    "service.scheduled",
    "service.assigned",
    "service.in_progress",
    "service.resolved",
    "service.failed",
    "workflow.resolved",
    "workflow.failed",
}


def validate_workflow_status(status: str):
    if status is None:
        return None

    if status not in WORKFLOW_STATUSES:
        raise ValueError(f"Invalid workflow_status: {status}")

    return status


def validate_workflow_stage(stage: str):
    if stage is None:
        return None

    if stage not in WORKFLOW_STAGES:
        raise ValueError(f"Invalid workflow stage: {stage}")

    return stage


def validate_operational_event_type(event_type: str):
    if event_type is None:
        return None

    if event_type not in OPERATIONAL_EVENT_TYPES:
        raise ValueError(f"Invalid operational event_type: {event_type}")

    return event_type

def compute_queue_state(
    urgency: str = None,
    workflow_status: str = None,
    notification_state: str = None,
    service_state: str = None,
):
    """
    Computes the operational queue state exposed to the admin console.

    Priority/urgency is intentionally separate from workflow queue state.
    An urgent service request is not automatically escalated unless the
    workflow itself enters an escalation/failure/intervention condition.
    """
    workflow_status = (workflow_status or "").strip().lower()
    notification_state = (notification_state or "").strip().lower()
    service_state = (service_state or "").strip().lower()

    if workflow_status in {"failed", "error"} or service_state == "failed":
        return "failed"

    if workflow_status == "escalated":
        return "escalated"

    if workflow_status in {"resolved", "completed"} or service_state == "resolved":
        return "resolved"

    if service_state in {
        "triaged",
        "awaiting_dispatch",
        "scheduled",
        "assigned",
        "in_progress",
    }:
        return "awaiting_service"

    if notification_state in {"business_sent", "caller_sent"}:
        return "awaiting_service"

    if workflow_status in {"active", "created", "in_progress"}:
        return "active"

    return "new"

def append_workflow_event(
    cur,
    workflow_id: str,
    client_key: str,
    event_type: str,
    event_stage: str = None,
    source_type: str = "system",
    source_id: str = None,
    event_status: str = "recorded",
    metadata: dict = None,
):
    """
    Appends an immutable operational workflow event.

    This is the canonical event-writing primitive for workflow orchestration.
    Future dispatch, booking, escalation, retry, acknowledgement, and completion
    stages should use this helper instead of writing directly to operational_events.
    """

    if not workflow_id or not client_key or not event_type:
        return None
    
    validate_operational_event_type(event_type)
    validate_workflow_stage(event_stage)

    safe_metadata = metadata or {}

    event_id = f"{workflow_id}_{event_type}"

    if source_id:
        event_id = f"{event_id}_{source_id}"

    cur.execute(
        """
        INSERT INTO operational_events (
            event_id,
            workflow_id,
            client_key,
            source_type,
            source_id,
            event_type,
            event_stage,
            event_status,
            event_metadata
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_id) DO NOTHING;
        """,
        (
            event_id,
            workflow_id,
            client_key,
            source_type,
            source_id,
            event_type,
            event_stage,
            event_status,
            Jsonb(safe_metadata),
        ),
    )

    return event_id

def hash_operator_token(token: str):
    if not token:
        return None

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_operator_action_token(
    cur,
    workflow_id: str,
    client_key: str,
    action_type: str = "operator_acknowledge",
    expires_hours: int = 48,
):
    if not workflow_id or not client_key or not action_type:
        return None

    token = secrets.token_urlsafe(32)
    token_hash = hash_operator_token(token)

    cur.execute(
        """
        INSERT INTO operator_action_tokens (
            workflow_id,
            client_key,
            action_type,
            token_hash,
            expires_at
        )
        VALUES (%s, %s, %s, %s, NOW() + (%s || ' hours')::interval)
        RETURNING id;
        """,
        (
            workflow_id,
            client_key,
            action_type,
            token_hash,
            expires_hours,
        ),
    )

    row = cur.fetchone()

    if not row:
        return None

    return token

def build_operator_ack_url(token: str):
    if not token:
        return None

    base_url = os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")

    if not base_url:
        base_url = "https://gosonic-mvp.onrender.com"

    return f"{base_url.rstrip('/')}/operator/ack/{token}"

def update_workflow_state(
    cur,
    workflow_id: str,
    workflow_status: str = None,
    current_stage: str = None,
    last_event_type: str = None,
    notification_state: str = None,
    service_state: str = None,
    completed_at=None,
):
    """
    Updates the mutable workflow snapshot.

    The immutable event stream remains the source of truth.
    This row is the current operational state used for dashboards, filtering,
    client visibility, and future orchestration.
    """

    if not workflow_id:
        return None
    
    validate_workflow_status(workflow_status)
    validate_workflow_stage(current_stage)

    cur.execute(
        """
        UPDATE workflow_instances
        SET
            workflow_status = COALESCE(%s, workflow_status),
            current_stage = COALESCE(%s, current_stage),
            last_event_type = COALESCE(%s, last_event_type),
            last_event_at = CASE
                WHEN %s::text IS NOT NULL THEN NOW()
                ELSE last_event_at
            END,
            notification_state = COALESCE(%s, notification_state),
            service_state = COALESCE(%s, service_state),
            completed_at = COALESCE(%s, completed_at),
            updated_at = NOW()
        WHERE workflow_id = %s
        RETURNING workflow_id;
        """,
        (
            workflow_status,
            current_stage,
            last_event_type,
            last_event_type,
            notification_state,
            service_state,
            completed_at,
            workflow_id,
        ),
    )

    row = cur.fetchone()

    return row[0] if row else None

def update_workflow_ownership(
    cur,
    workflow_id: str,
    ownership_state: str = None,
    assigned_operator: str = None,
    assigned_team: str = None,
):
    """
    Updates workflow ownership snapshot fields.

    Ownership represents operational responsibility for the workflow.
    The immutable operational event stream remains the audit source of truth.
    """

    if not workflow_id:
        return None

    cur.execute(
        """
        UPDATE workflow_instances
        SET
            ownership_state = COALESCE(%s, ownership_state),
            assigned_operator = COALESCE(%s, assigned_operator),
            assigned_team = COALESCE(%s, assigned_team),
            updated_at = NOW()
        WHERE workflow_id = %s
        RETURNING workflow_id;
        """,
        (
            ownership_state,
            assigned_operator,
            assigned_team,
            workflow_id,
        ),
    )

    row = cur.fetchone()

    return row[0] if row else None

def advance_workflow_stage(
    cur,
    workflow_id: str,
    client_key: str,
    event_type: str,
    event_stage: str,
    workflow_status: str = None,
    source_type: str = "system",
    source_id: str = None,
    event_status: str = "recorded",
    metadata: dict = None,
    completed_at=None,
):
    """
    Canonical workflow transition primitive.

    Appends an immutable operational event and updates the mutable workflow
    snapshot in one controlled path so lifecycle state and event history remain
    synchronized.
    """

    if not workflow_id or not client_key or not event_type or not event_stage:
        return None
    
    validate_operational_event_type(event_type)
    validate_workflow_stage(event_stage)
    validate_workflow_status(workflow_status)

    notification_state = None
    service_state = None

    if event_type == "notification.business_sent":
        notification_state = "business_sent"

    elif event_type == "notification.caller_sent":
        notification_state = "caller_sent"

    elif event_type == "intake.completed":
        service_state = "intake_completed"

    elif event_type == "triage.completed":
        service_state = "triaged"

    event_id = append_workflow_event(
        cur=cur,
        workflow_id=workflow_id,
        client_key=client_key,
        event_type=event_type,
        event_stage=event_stage,
        source_type=source_type,
        source_id=source_id,
        event_status=event_status,
        metadata=metadata,
    )

    update_workflow_state(
        cur=cur,
        workflow_id=workflow_id,
        workflow_status=workflow_status,
        current_stage=event_stage,
        last_event_type=event_type,
        notification_state=notification_state,
        service_state=service_state,
        completed_at=completed_at,
    )

    return event_id


def advance_service_state(
    cur,
    workflow_id: str,
    client_key: str,
    service_state: str,
    source_id: str = None,
    event_metadata: dict = None,
):
    """
    Advance the canonical service lifecycle state for a workflow.

    This helper owns service_state progression and records an immutable
    service.* operational event for auditability.
    """

    allowed_service_states = {
        "pending",
        "triaged",
        "awaiting_dispatch",
        "scheduled",
        "assigned",
        "in_progress",
        "resolved",
        "failed",
    }

    if service_state not in allowed_service_states:
        raise ValueError(f"Invalid service_state: {service_state}")

    event_type = f"service.{service_state}"

    validate_operational_event_type(event_type)

    event_id = append_workflow_event(
        cur=cur,
        workflow_id=workflow_id,
        client_key=client_key,
        event_type=event_type,
        event_stage="service",
        source_type="workflow",
        source_id=source_id,
        metadata=event_metadata or {},
    )

    cur.execute(
        """
        UPDATE workflow_instances
        SET
            service_state = %s,
            last_event_type = %s,
            last_event_at = NOW(),
            updated_at = NOW()
        WHERE workflow_id = %s
            AND client_key = %s;
        """,
        (
            service_state,
            event_type,
            workflow_id,
            client_key,
        ),
    )

    ownership_update = (event_metadata or {}).get("ownership_update") or {}

    if ownership_update:
        ownership_workflow_id = update_workflow_ownership(
            cur=cur,
            workflow_id=workflow_id,
            ownership_state=ownership_update.get("ownership_state"),
            assigned_operator=ownership_update.get("assigned_operator"),
            assigned_team=ownership_update.get("assigned_team"),
        )

        if not ownership_workflow_id:
            raise RuntimeError("Workflow ownership update failed")

        append_workflow_event(
            cur=cur,
            workflow_id=workflow_id,
            client_key=client_key,
            event_type="ownership.assigned",
            event_stage="assigned",
            source_type="workflow",
            source_id=source_id,
            metadata={
                "ownership_state": ownership_update.get("ownership_state"),
                "assigned_operator": ownership_update.get("assigned_operator"),
                "assigned_team": ownership_update.get("assigned_team"),
                "source_event_type": event_type,
            },
        )

    return event_id


def resolve_workflow(
    cur,
    workflow_id: str,
    client_key: str,
    source_id: str = None,
    resolution_reason: str = "workflow_completed",
    event_metadata: dict = None,
):
    """
    Resolve a workflow when the operational lifecycle reaches a successful
    terminal state.

    This should not be used for simple call completion. It is reserved for
    actual service/workflow resolution.
    """

    event_type = "workflow.resolved"

    validate_operational_event_type(event_type)
    validate_workflow_status("resolved")

    metadata = {
        "resolution_reason": resolution_reason,
    }

    if event_metadata:
        metadata.update(event_metadata)

    event_id = append_workflow_event(
        cur=cur,
        workflow_id=workflow_id,
        client_key=client_key,
        event_type=event_type,
        event_stage="resolved",
        source_type="workflow",
        source_id=source_id,
        metadata=metadata,
    )

    cur.execute(
        """
        UPDATE workflow_instances
        SET
            workflow_status = 'resolved',
            service_state = 'resolved',
            current_stage = 'resolved',
            last_event_type = %s,
            last_event_at = NOW(),
            completed_at = COALESCE(completed_at, NOW()),
            updated_at = NOW()
        WHERE workflow_id = %s
          AND client_key = %s;
        """,
        (
            event_type,
            workflow_id,
            client_key,
        ),
    )

    return event_id


def fail_workflow(
    cur,
    workflow_id: str,
    client_key: str,
    source_id: str = None,
    failure_reason: str = "workflow_failed",
    event_metadata: dict = None,
):
    """
    Fail a workflow when the operational lifecycle reaches an unsuccessful
    terminal state.

    This should not be used for urgency, escalation, or incomplete intake.
    """

    event_type = "workflow.failed"

    validate_operational_event_type(event_type)
    validate_workflow_status("failed")

    metadata = {
        "failure_reason": failure_reason,
    }

    if event_metadata:
        metadata.update(event_metadata)

    event_id = append_workflow_event(
        cur=cur,
        workflow_id=workflow_id,
        client_key=client_key,
        event_type=event_type,
        event_stage="failed",
        source_type="workflow",
        source_id=source_id,
        metadata=metadata,
    )

    cur.execute(
        """
        UPDATE workflow_instances
        SET
            workflow_status = 'failed',
            service_state = 'failed',
            current_stage = 'failed',
            last_event_type = %s,
            last_event_at = NOW(),
            completed_at = COALESCE(completed_at, NOW()),
            updated_at = NOW()
        WHERE workflow_id = %s
          AND client_key = %s;
        """,
        (
            event_type,
            workflow_id,
            client_key,
        ),
    )

    return event_id


def advance_workflow_stage_by_workflow_id(
    workflow_id: str,
    client_key: str,
    event_type: str,
    event_stage: str,
    workflow_status: str = None,
    source_type: str = "system",
    source_id: str = None,
    event_status: str = "recorded",
    metadata: dict = None,
    completed_at=None,
):
    """
    DB-safe workflow transition helper.

    Opens its own database connection, then delegates to the canonical
    cursor-scoped advance_workflow_stage() primitive. Use this when workflow
    advancement is needed outside an existing database cursor scope, such as
    notification, booking, dispatch, integration, or resolution events.
    """

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        logger.warning("Workflow stage advancement skipped: DATABASE_URL not configured")
        return None

    if not workflow_id or not client_key or not event_type or not event_stage:
        return None

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                event_id = advance_workflow_stage(
                    cur=cur,
                    workflow_id=workflow_id,
                    client_key=client_key,
                    event_type=event_type,
                    event_stage=event_stage,
                    workflow_status=workflow_status,
                    source_type=source_type,
                    source_id=source_id,
                    event_status=event_status,
                    metadata=metadata,
                    completed_at=completed_at,
                )

            conn.commit()

        return event_id

    except Exception:
        logger.exception("Workflow stage advancement failed")
        return None


def advance_service_state_by_workflow_id(
    workflow_id: str,
    client_key: str,
    service_state: str,
    source_id: str = None,
    event_metadata: dict = None,
):
    """
    DB-safe service lifecycle advancement helper.

    Opens its own database connection, then delegates to the canonical
    cursor-scoped advance_service_state() primitive.
    """

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        logger.warning("Service state advancement skipped: DATABASE_URL not configured")
        return None

    if not workflow_id or not client_key or not service_state:
        return None

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                event_id = advance_service_state(
                    cur=cur,
                    workflow_id=workflow_id,
                    client_key=client_key,
                    service_state=service_state,
                    source_id=source_id,
                    event_metadata=event_metadata,
                )

            conn.commit()

        return event_id

    except Exception:
        logger.exception("Service state advancement failed")
        return None


def resolve_workflow_by_workflow_id(
    workflow_id: str,
    client_key: str,
    source_id: str = None,
    resolution_reason: str = "workflow_completed",
    event_metadata: dict = None,
):
    """
    DB-safe workflow resolution helper.

    Opens its own database connection, then delegates to the canonical
    cursor-scoped resolve_workflow() primitive.
    """

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        logger.warning("Workflow resolution skipped: DATABASE_URL not configured")
        return None

    if not workflow_id or not client_key:
        return None

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                event_id = resolve_workflow(
                    cur=cur,
                    workflow_id=workflow_id,
                    client_key=client_key,
                    source_id=source_id,
                    resolution_reason=resolution_reason,
                    event_metadata=event_metadata,
                )

            conn.commit()

        return event_id

    except Exception:
        logger.exception("Workflow resolution failed")
        return None


def fail_workflow_by_workflow_id(
    workflow_id: str,
    client_key: str,
    source_id: str = None,
    failure_reason: str = "workflow_failed",
    event_metadata: dict = None,
):
    """
    DB-safe workflow failure helper.

    Opens its own database connection, then delegates to the canonical
    cursor-scoped fail_workflow() primitive.
    """

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        logger.warning("Workflow failure skipped: DATABASE_URL not configured")
        return None

    if not workflow_id or not client_key:
        return None

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                event_id = fail_workflow(
                    cur=cur,
                    workflow_id=workflow_id,
                    client_key=client_key,
                    source_id=source_id,
                    failure_reason=failure_reason,
                    event_metadata=event_metadata,
                )

            conn.commit()

        return event_id

    except Exception:
        logger.exception("Workflow failure failed")
        return None


def create_workflow_for_call(
    cur,
    call_id: str,
    client_key: str,
    urgency: str = None,
    call_outcome: str = None,
    metadata: dict = None,
):
    """
    Creates the canonical workflow instance and initial operational events
    for an analyzed call.

    This is intentionally idempotent:
    - workflow_id is derived from call_id
    - inserts use ON CONFLICT DO NOTHING
    - repeated webhook delivery will not duplicate the workflow
    """

    if not call_id or not client_key:
        return None

    workflow_id = f"wf_{call_id}"

    event_metadata = {
        "call_id": call_id,
        "urgency": urgency,
        "call_outcome": call_outcome,
    }

    if metadata:
        event_metadata.update(metadata)

    cur.execute(
        """
        INSERT INTO workflow_instances (
            workflow_id,
            client_key,
            source_type,
            source_id,
            workflow_type,
            workflow_status,
            urgency,
            current_stage
        )
        VALUES (%s, %s, 'call', %s, 'service_request', 'active', %s, 'intake_completed')
        ON CONFLICT (workflow_id) DO NOTHING;
        """,
        (
            workflow_id,
            client_key,
            call_id,
            urgency,
        ),
    )

    append_workflow_event(
        cur=cur,
        workflow_id=workflow_id,
        client_key=client_key,
        event_type="workflow.created",
        event_stage="initiated",
        source_type="call",
        source_id=call_id,
        metadata=event_metadata,
    )

    advance_workflow_stage(
        cur=cur,
        workflow_id=workflow_id,
        client_key=client_key,
        event_type="intake.completed",
        event_stage="intake_completed",
        workflow_status="active",
        source_type="call",
        source_id=call_id,
        metadata=event_metadata,
    )

    advance_workflow_stage(
        cur=cur,
        workflow_id=workflow_id,
        client_key=client_key,
        event_type="triage.completed",
        event_stage="triaged",
        workflow_status="active",
        source_type="call",
        source_id=call_id,
        metadata=event_metadata,
    )

    advance_service_state(
        cur=cur,
        workflow_id=workflow_id,
        client_key=client_key,
        service_state="triaged",
        source_id=call_id,
        event_metadata=event_metadata,
    )

    return workflow_id

# -------------------------------------------------
# SMS ELIGIBILITY ENGINE
# -------------------------------------------------
def get_sms_policy(call_outcome, required_fields_present):
    if call_outcome == "confirmed" and required_fields_present:
        return {"business": True, "caller": True, "reason": "confirmed_request"}

    if call_outcome == "address_fallback":
        return {"business": True, "caller": True, "reason": "address_fallback"}

    return {
        "business": False,
        "caller": False,
        "reason": f"sms_suppressed_for_{call_outcome}",
    }


# -------------------------------------------------
# RETELL INBOUND WEBHOOK
# -------------------------------------------------
@app.post("/webhook/inbound")
async def inbound_webhook(
    request: Request,
    x_webhook_secret: str = Header(None),
    x_retell_signature: str = Header(None),
):
    require_webhook_secret(x_webhook_secret)

    try:
        raw_body = (await request.body()).decode("utf-8")
        verify_retell_signature(
            raw_body, x_retell_signature, enforce_env="RETELL_VERIFY_INBOUND_SIGNATURE"
        )
        logger.info("[RETELL INBOUND SIGNATURE] Verified")
        data = json.loads(raw_body or "{}")

        call_inbound = data.get("call_inbound") or {}

        from_number = call_inbound.get("from_number") or data.get("from_number") or ""

        to_number = call_inbound.get("to_number") or data.get("to_number") or ""

        formatted_from_phone = normalize_phone(from_number)
        formatted_to_phone = normalize_phone(to_number)

        routed_client = get_client_by_inbound_phone(formatted_to_phone)

        if routed_client:
            client_id = routed_client["client_key"]
            routing_source = routed_client.get("source")
        else:
            client_id = "hvac_toronto_001"
            routing_source = "fallback_default_client"

        log_info(
            "[INBOUND WEBHOOK]",
            from_number=formatted_from_phone or from_number,
            to_number=formatted_to_phone or to_number,
            client_id=client_id,
            routing_source=routing_source,
        )

        return {
            "call_inbound": {
                "dynamic_variables": {
                    "caller_phone": formatted_from_phone or from_number,
                    "client_id": client_id,
                },
                "metadata": {
                    "caller_phone": formatted_from_phone or from_number,
                    "client_id": client_id,
                    "to_number": formatted_to_phone or to_number,
                    "routing_source": routing_source,
                },
            }
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Inbound webhook failed")

        return {
            "call_inbound": {
                "dynamic_variables": {"client_id": "hvac_toronto_001"},
                "metadata": {
                    "client_id": "hvac_toronto_001",
                    "routing_source": "error_fallback",
                },
            }
        }


# -------------------------------------------------
# TRIAGE ENDPOINT
# -------------------------------------------------
@app.post("/webhook/triage")
async def triage(
    request: Request,
    x_webhook_secret: str = Header(None),
    x_retell_signature: str = Header(None),
):
    # Legacy optional shared-secret support remains available, but the preferred
    # production path is Retell signature verification using X-Retell-Signature.
    require_webhook_secret(x_webhook_secret)

    try:
        raw_body = (await request.body()).decode("utf-8")
        verify_retell_signature(raw_body, x_retell_signature)
        data = json.loads(raw_body or "{}")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Triage failed: invalid JSON")
        return {
            "urgency": "standard",
            "route": "standard",
            "summary": "Unable to parse triage payload.",
            "issue_type": "other",
            "confidence": 0.5,
        }

    # Retell Custom Functions may send either a flat args-only payload or
    # the documented wrapper: { name, call, args }. Support both.
    args = data.get("args") if isinstance(data.get("args"), dict) else data
    call = data.get("call") if isinstance(data.get("call"), dict) else {}

    transcript_raw = (
        args.get("transcript")
        or args.get("issue_text")
        or args.get("summary")
        or args.get("message")
        or call.get("transcript")
        or ""
    )

    urgency, issue_type = classify_hvac_issue(transcript_raw)

    response = {
        "urgency": urgency,
        "route": urgency,
        "summary": f"Caller reports HVAC issue: {transcript_raw}",
        "issue_type": issue_type,
        "confidence": 0.9 if urgency == "urgent" else 0.75,
    }

    log_info(
        "[TRIAGE RESPONSE]",
        urgency=response["urgency"],
        route=response["route"],
        issue_type=response["issue_type"],
        confidence=response["confidence"],
        transcript=transcript_raw,
    )

    return response


def extract_tool_confidence(call: dict):
    """
    Extract confidence score from Retell tool call results.
    """

    if not isinstance(call, dict):
        return None

    events = call.get("transcript_with_tool_calls") or []

    for event in events:

        if event.get("role") != "tool_call_result":
            continue

        content = event.get("content")

        if not content:
            continue

        try:
            parsed = json.loads(content)

            confidence = parsed.get("confidence")

            if confidence is not None:
                return confidence

        except Exception:
            continue

    return None


# -------------------------------------------------
# CALL SUMMARY WEBHOOK
# -------------------------------------------------
@app.post("/webhook/call-summary")
async def call_summary(
    request: Request,
    x_webhook_secret: str = Header(None),
    x_retell_signature: str = Header(None),
):
    require_webhook_secret(x_webhook_secret)

    try:
        raw_body = (await request.body()).decode("utf-8")
        verify_retell_signature(
            raw_body,
            x_retell_signature,
            enforce_env="RETELL_VERIFY_CALL_SUMMARY_SIGNATURE",
        )
        logger.info("[RETELL CALL SUMMARY SIGNATURE] Verified")
        data = json.loads(raw_body or "{}")

        cleanup_state()

        raw_event_type = data.get("event") or data.get("type")

        RETELL_EVENT_TYPE_MAP = {
            "call_started": "call.started",
            "call_ended": "call.completed",
            "call_analyzed": "call.analyzed",
        }

        event_type = RETELL_EVENT_TYPE_MAP.get(raw_event_type, raw_event_type)

        call = data.get("call") or {}

        call_id = data.get("call_id") or data.get("id") or call.get("call_id")

        if not call_id:
            return {"status": "error", "message": "missing call_id"}

        metadata = call.get("metadata") or {}

        if event_type == "call.started":
            caller_phone_raw = (
                data.get("caller_phone")
                or call.get("from_number")
                or (call.get("call_inbound") or {}).get("from_number")
                or data.get("from_number")
                or metadata.get("caller_phone")
                or ""
            )

            formatted_phone = normalize_phone(caller_phone_raw)

            log_info(
                "[CALL STARTED]",
                call_id=call_id,
                caller_phone=formatted_phone or caller_phone_raw,
                metadata_present=bool(metadata),
                call_key_count=len(call.keys()) if isinstance(call, dict) else 0,
            )

            if formatted_phone:
                CALL_PHONE_MAP[call_id] = formatted_phone
                CALL_PHONE_META[call_id] = time.time()
                log_info(
                    "[PHONE STORED]", call_id=call_id, caller_phone=formatted_phone
                )
            else:
                logger.warning("[PHONE NOT FOUND ON CALL.STARTED] call_id=%s", call_id)

            return {
                "status": "phone_capture_processed",
                "call_id": call_id,
                "caller_phone": formatted_phone,
            }

        if event_type != "call.analyzed":
            return {"status": "ignored_event", "event_type": event_type}

        now = time.time()

        if call_id in PROCESSED_CALLS:
            return {"status": "duplicate_ignored", "call_id": call_id}

        PROCESSED_CALLS.add(call_id)
        PROCESSED_META[call_id] = now

        analysis = (
            call.get("call_analysis")
            or call.get("analysis")
            or data.get("analysis")
            or {}
        )

        custom = (
            analysis.get("custom_analysis_data")
            or analysis.get("custom_analysis")
            or analysis.get("post_call_analysis_data")
            or data.get("custom_analysis_data")
            or data.get("post_call_analysis_data")
            or {}
        )

        messages = call.get("transcript_object") or data.get("transcript_object") or []

        user_text = build_transcript_text(messages)

        full_transcript = (
            call.get("transcript") or data.get("transcript") or user_text or ""
        )

        client_id = (
            data.get("client_id")
            or metadata.get("client_id")
            or custom.get("client_id")
            or "hvac_toronto_001"
        )

        client = get_client_by_key(client_id)

        client_settings = get_client_settings_by_key(client_id)

        if not client_settings:
            logger.warning("Client settings fallback used: %s", client_id)

            client_settings = {
                "business_sms_enabled": True,
                "caller_sms_enabled": True,
                "twilio_outbound_number": TWILIO_PHONE,
            }

        if not client:
            return {
                "status": "error",
                "message": "invalid or inactive client_id",
                "client_id": client_id,
            }

        caller_name = custom.get("full_name") or custom.get("caller_name") or "Unknown"

        service_address = (
            custom.get("service_address") or custom.get("address") or "Unknown"
        )

        issue_description = (
            custom.get("issue_description")
            or custom.get("summary")
            or user_text
            or full_transcript
            or "No issue description available."
        )

        urgency = clean_urgency(custom.get("urgency"))
        call_outcome = clean_call_outcome(custom.get("call_outcome"))

        caller_phone_raw = (
            custom.get("caller_phone")
            or metadata.get("caller_phone")
            or CALL_PHONE_MAP.get(call_id)
            or data.get("caller_phone")
            or call.get("from_number")
            or (call.get("call_inbound") or {}).get("from_number")
            or data.get("from_number")
            or ""
        )

        if caller_name == "Unknown":
            caller_name = extract_name(user_text) or "Unknown"

        formatted_phone = normalize_phone(caller_phone_raw)

        if not formatted_phone:
            formatted_phone = normalize_phone(user_text) or normalize_phone(
                full_transcript
            )

        classified_urgency, issue_type = classify_hvac_issue(issue_description)

        if not custom.get("urgency"):
            urgency = classified_urgency

        issue_type = custom.get("issue_type") or issue_type

        short_summary = build_short_summary(urgency, issue_type)

        # -------------------------------------------------
        # CALLER IDENTITY SEMANTICS
        # -------------------------------------------------
        caller_phone_source = "unknown"
        caller_identity_status = "anonymous"
        caller_phone_verified = False

        if formatted_phone:
            caller_phone_source = "ani"
            caller_phone_verified = True
            caller_identity_status = "partial"

        if caller_name and caller_name != "Unknown" and service_address and service_address != "Unknown":
            caller_identity_status = "known"

        required_fields_present = all(
            [
                caller_name and caller_name != "Unknown",
                formatted_phone,
                service_address and service_address != "Unknown",
                issue_description
                and issue_description != "No issue description available.",
            ]
        )

        sms_policy = get_sms_policy(call_outcome, required_fields_present)

        send_business_sms = sms_policy["business"] and client_settings.get(
            "business_sms_enabled", True
        )

        send_caller_sms = sms_policy["caller"] and client_settings.get(
            "caller_sms_enabled", True
        )

        sms_policy_reason = sms_policy["reason"]

        log_info(
            "[CALL ANALYZED]",
            event_type=event_type,
            call_id=call_id,
            client_id=client_id,
            client_source=client.get("source"),
            business_name=client.get("business_name"),
            caller_phone=formatted_phone,
            stored_phone=CALL_PHONE_MAP.get(call_id),
            urgency=urgency,
            issue_type=issue_type,
            call_outcome=call_outcome,
            required_fields_present=required_fields_present,
            sms_policy_reason=sms_policy_reason,
        )

        if call_outcome == "address_fallback":
            business_message = (
                "📞 Gosonic Call Alert\n"
                "----------------------\n"
                f"Business: {client['business_name']}\n"
                f"Outcome: ADDRESS NEEDS CONFIRMATION\n"
                f"Urgency: {urgency.upper()}\n"
                f"Caller: {caller_name}\n"
                f"Phone: {formatted_phone or 'Unknown'}\n"
                f"Address Provided: {service_address}\n\n"
                f"Summary:\n{short_summary}\n\n"
                "Action Required:\nCall the customer back to confirm the service address."
            )
        else:
            business_message = (
                "📞 Gosonic Call Alert\n"
                "----------------------\n"
                f"Business: {client['business_name']}\n"
                f"Outcome: {call_outcome.upper()}\n"
                f"Urgency: {urgency.upper()}\n"
                f"Caller: {caller_name}\n"
                f"Phone: {formatted_phone or 'Unknown'}\n"
                f"Address: {service_address}\n\n"
                f"Summary:\n{short_summary}"
            )

        business_sent = False
        business_error = None

        caller_sent = False
        caller_error = None

        operator_ack_url = None

        sms_from_number = client_settings.get("twilio_outbound_number") or TWILIO_PHONE

        if send_business_sms and twilio_client and sms_from_number:
            try:
                twilio_client.messages.create(
                    body=business_message,
                    from_=sms_from_number,
                    to=client["business_phone"],
                )
                business_sent = True
                logger.info("[TWILIO BUSINESS] Sent")
            except Exception as e:
                business_error = str(e)
                logger.error("[TWILIO BUSINESS ERROR] %s", business_error)
        else:
            if not send_business_sms:
                business_error = f"Business SMS suppressed: {sms_policy_reason}"
            elif not twilio_client or not sms_from_number:
                business_error = "Twilio client or SMS sender number missing"

            logger.info("[TWILIO BUSINESS SKIPPED] %s", business_error)

        caller_sent = False
        caller_error = None

        if (
            send_caller_sms
            and formatted_phone
            and client_settings.get("caller_sms_enabled", True)
        ):
            display_name = caller_name if caller_name != "Unknown" else "there"

            if call_outcome == "address_fallback":
                caller_message = (
                    f"Hi {display_name}, "
                    "we’ve received your HVAC service request. "
                    f"{client['business_name']} has been notified and will call you back "
                    "to confirm the service address. Thank you."
                )
            else:
                caller_message = (
                    f"Hi {display_name}, "
                    "we’ve received your HVAC service request. "
                    f"{client['business_name']} has been notified. "
                    "Thank you."
                )

            if twilio_client and sms_from_number:
                try:
                    twilio_client.messages.create(
                        body=caller_message, from_=sms_from_number, to=formatted_phone
                    )
                    caller_sent = True
                    logger.info("[TWILIO CALLER] Sent")

                except Exception as e:
                    caller_error = str(e)
                    logger.error("[TWILIO CALLER ERROR] %s", caller_error)

            else:
                caller_error = "Twilio client or SMS sender number missing"
                logger.info("[TWILIO CALLER SKIPPED] %s", caller_error)

        else:
            if not send_caller_sms:
                if not client_settings.get("caller_sms_enabled", True):
                    caller_error = "Caller SMS disabled by client settings"
                else:
                    caller_error = f"Caller SMS suppressed: {sms_policy_reason}"

            elif not formatted_phone:
                caller_error = "Missing or invalid caller phone"

            elif not client_settings.get("caller_sms_enabled", True):
                caller_error = "Caller SMS disabled for client"

            logger.info("[TWILIO CALLER SKIPPED] %s", caller_error)

        # -------------------------------------------------
        # PERSIST CALL RECORD
        # -------------------------------------------------
        call_save_result = save_call_record(
            call_id=call_id,
            client_key=client_id,
            caller_name=caller_name,
            caller_phone=formatted_phone,
            service_address=service_address,
            issue_description=issue_description,
            issue_type=issue_type,
            urgency=urgency,
            call_outcome=call_outcome,
            sms_policy_reason=sms_policy_reason,
            business_notified=business_sent,
            business_error=business_error,
            caller_notified=caller_sent,
            caller_error=caller_error,
            raw_payload=data,
            call_status="completed",
            webhook_status="received",
            agent_id=call.get("agent_id"),
            call_direction=call.get("direction"),
            confidence=extract_tool_confidence(call),
            processing_latency_ms=(call.get("latency", {}).get("e2e", {}).get("p50")),
            escalation_reason=("urgent_call" if urgency == "urgent" else None),
            transcript=full_transcript,
            ended_at=datetime.now(timezone.utc),
            caller_phone_source=caller_phone_source,
            caller_identity_status=caller_identity_status,
            caller_phone_verified=caller_phone_verified,
        )

        workflow_id = call_save_result.get("workflow_id")

        if workflow_id and send_business_sms:
            try:
                with psycopg.connect(os.getenv("DATABASE_URL")) as conn:
                    with conn.cursor() as cur:
                        operator_ack_token = create_operator_action_token(
                            cur=cur,
                            workflow_id=workflow_id,
                            client_key=client_id,
                            action_type="operator_acknowledge",
                            expires_hours=48,
                        )

                    conn.commit()

                operator_ack_url = build_operator_ack_url(operator_ack_token)

                if operator_ack_url and business_sent and twilio_client and sms_from_number:
                    twilio_client.messages.create(
                        body=(
                            "Gosonic acknowledgement link:\n"
                            f"{operator_ack_url}\n\n"
                            "Tap to confirm this request was received."
                        ),
                        from_=sms_from_number,
                        to=client["business_phone"],
                    )
                    logger.info("[TWILIO OPERATOR ACK LINK] Sent")

            except Exception:
                logger.exception(
                    "Operator acknowledgement token or SMS link delivery failed"
                )

        if call_save_result.get("saved"):
            log_call_event(
                call_id=call_id,
                client_key=client_id,
                event_type="call.analyzed",
                event_metadata={
                    "call_status": "completed",
                    "webhook_status": "received",
                    "urgency": urgency,
                    "call_outcome": call_outcome,
                    "issue_type": issue_type,
                    "business_notified": business_sent,
                    "caller_notified": caller_sent,
                    "confidence": extract_tool_confidence(call),
                    "processing_latency_ms": (
                        call.get("latency", {}).get("e2e", {}).get("p50")
                    ),
                },
                event_timestamp=datetime.now(timezone.utc),
            )

            if business_sent:
                log_call_event(
                    call_id=call_id,
                    client_key=client_id,
                    event_type="notification.business_sent",
                    event_metadata={
                        "channel": "sms",
                        "recipient_type": "business",
                        "sms_policy_reason": sms_policy_reason,
                    },
                    event_timestamp=datetime.now(timezone.utc),
                )

                if workflow_id:
                    advance_workflow_stage_by_workflow_id(
                        workflow_id=workflow_id,
                        client_key=client_id,
                        event_type="notification.business_sent",
                        event_stage="notification_sent",
                        workflow_status="active",
                        source_type="system",
                        source_id=call_id,
                        metadata={
                            "channel": "sms",
                            "recipient_type": "business",
                            "sms_policy_reason": sms_policy_reason,
                        },
                    )

            if caller_sent:
                log_call_event(
                    call_id=call_id,
                    client_key=client_id,
                    event_type="notification.caller_sent",
                    event_metadata={
                        "channel": "sms",
                        "recipient_type": "caller",
                        "sms_policy_reason": sms_policy_reason,
                    },
                    event_timestamp=datetime.now(timezone.utc),
                )

                if workflow_id:
                    advance_workflow_stage_by_workflow_id(
                        workflow_id=workflow_id,
                        client_key=client_id,
                        event_type="notification.caller_sent",
                        event_stage="notification_sent",
                        workflow_status="active",
                        source_type="system",
                        source_id=call_id,
                        metadata={
                            "channel": "sms",
                            "recipient_type": "caller",
                            "sms_policy_reason": sms_policy_reason,
                        },
                    )

            log_call_event(
                call_id=call_id,
                client_key=client_id,
                event_type="system.persistence_succeeded",
                event_metadata={
                    "storage": "postgresql",
                    "call_status": "completed",
                    "webhook_status": "received",
                },
                event_timestamp=datetime.now(timezone.utc),
            )

        CALL_PHONE_MAP.pop(call_id, None)
        CALL_PHONE_META.pop(call_id, None)

        return {
            "status": "processed",
            "client_id": client_id,
            "client_source": client.get("source"),
            "call_id": call_id,
            "call_outcome": call_outcome,
            "sms_policy_reason": sms_policy_reason,
            "caller_name": caller_name,
            "caller_phone": formatted_phone,
            "service_address": service_address,
            "urgency": urgency,
            "issue_type": issue_type,
            "summary": short_summary,
            "required_fields_present": required_fields_present,
            "business_notified": business_sent,
            "business_error": business_error,
            "caller_notified": caller_sent,
            "caller_error": caller_error,
            "persistence_succeeded": call_save_result["saved"],
            "persistence_error": call_save_result["error"],
        }

    except Exception as e:
        logger.exception("Call summary webhook failed")
        return {"status": "error", "message": str(e)}
