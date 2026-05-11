from fastapi import FastAPI, Request, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from twilio.rest import Client
from psycopg.types.json import Jsonb
from datetime import datetime, timedelta, timezone
import psycopg
import jwt
import os
import time
import re
import logging
import json
import hashlib
import hmac
import secrets


app = FastAPI(title="Gosonic MVP API", version="0.2.5")

# -------------------------------------------------
# LOGGING
# -------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("gosonic")

# Keep third-party SDK/client logs quiet in production.
logging.getLogger("twilio").setLevel(logging.WARNING)
logging.getLogger("twilio.http_client").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

LOG_SENSITIVE_DATA = os.getenv("LOG_SENSITIVE_DATA", "false").lower() == "true"

# -------------------------------------------------
# PLATFORM TIMEZONES
# -------------------------------------------------
# Store IANA timezone names in the database. Display friendly labels in the dashboard.
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

DEFAULT_CLIENT_TIMEZONE = os.getenv("DEFAULT_CLIENT_TIMEZONE", "America/New_York")


def normalize_timezone(value: str):
    if value in SUPPORTED_TIMEZONES:
        return value
    return DEFAULT_CLIENT_TIMEZONE if DEFAULT_CLIENT_TIMEZONE in SUPPORTED_TIMEZONES else "America/New_York"


def timezone_label(value: str):
    return SUPPORTED_TIMEZONES.get(value, value or "Unknown")


# -------------------------------------------------
# CORS CONFIGURATION
# -------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.getenv("CORS_ALLOWED_ORIGINS", "https://client.gosonic.com").split(",")
        if origin.strip()
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# -------------------------------------------------
# ADMIN AUTH
# -------------------------------------------------
def require_admin(x_admin_key: str):
    admin_key = os.getenv("ADMIN_API_KEY")

    if not admin_key:
        raise HTTPException(
            status_code=500,
            detail="ADMIN_API_KEY not configured"
        )

    if x_admin_key != admin_key:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )

    return True


def require_webhook_secret(x_webhook_secret: str):
    """
    Optional shared-secret protection for Retell-facing webhooks.

    Set WEBHOOK_SHARED_SECRET in Render and pass the same value from Retell
    as the X-Webhook-Secret header. If WEBHOOK_SHARED_SECRET is not set,
    this check is skipped to avoid breaking the current MVP flow.
    """
    expected_secret = os.getenv("WEBHOOK_SHARED_SECRET")

    if not expected_secret:
        return True

    if not x_webhook_secret or x_webhook_secret != expected_secret:
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook secret"
        )

    return True



def verify_retell_signature(raw_body: str, signature: str, enforce_env: str = "RETELL_VERIFY_TRIAGE_SIGNATURE"):
    """
    Verifies Retell-signed Custom Function requests using X-Retell-Signature.

    This is intentionally opt-in per endpoint. Set RETELL_VERIFY_TRIAGE_SIGNATURE=true
    only after confirming Retell is sending X-Retell-Signature to /webhook/triage.
    """
    should_enforce = os.getenv(enforce_env, "false").lower() == "true"

    if not should_enforce:
        return True

    api_key = os.getenv("RETELL_API_KEY")

    if not api_key:
        logger.error("[RETELL SIGNATURE] RETELL_API_KEY not configured")
        raise HTTPException(status_code=500, detail="Retell verification not configured")

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



def observe_retell_signature(raw_body: str, signature: str, label: str = "[RETELL SIGNATURE OBSERVE]"):
    """
    Passive Retell signature check for endpoints we are not enforcing yet.

    This never blocks request processing. It only logs whether Retell is
    sending X-Retell-Signature and whether verification succeeds.
    """
    api_key = os.getenv("RETELL_API_KEY")

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
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE = os.getenv("TWILIO_PHONE")

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
        "caller_enabled": True
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
def hash_password(password: str):
    """
    PBKDF2 password hashing using the Python standard library.
    Format: pbkdf2_sha256$iterations$salt$hash
    """
    if not password:
        return None

    iterations = 260000
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations
    ).hex()

    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def verify_password(password: str, password_hash: str):
    if not password or not password_hash:
        return False

    try:
        algorithm, iterations, salt, expected_digest = password_hash.split("$", 3)

        if algorithm != "pbkdf2_sha256":
            return False

        candidate_digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations)
        ).hex()

        return hmac.compare_digest(candidate_digest, expected_digest)

    except Exception:
        return False


def create_session_token(user_profile: dict):
    session_secret = os.getenv("SESSION_SECRET")

    if not session_secret:
        raise HTTPException(
            status_code=500,
            detail="SESSION_SECRET not configured"
        )

    payload = {
        "email": user_profile.get("email"),
        "user_id": user_profile.get("user_id"),
        "client_key": user_profile.get("client_key"),
        "role": user_profile.get("role"),
        "full_name": user_profile.get("full_name"),
        "business_name": user_profile.get("business_name"),
        "timezone": user_profile.get("timezone"),
        "exp": datetime.now(timezone.utc) + timedelta(hours=12)
    }

    token = jwt.encode(
        payload,
        session_secret,
        algorithm="HS256"
    )

    return token


def get_user_by_email(email: str):
    database_url = os.getenv("DATABASE_URL")

    if not database_url or not email:
        return None

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
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
                """, (email,))

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
            "auth_source": "database"
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
                cur.execute("""
                    UPDATE users
                    SET
                        last_login_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING last_login_at;
                """, (user_id,))

                row = cur.fetchone()

            conn.commit()

        return row[0] if row else None

    except Exception:
        logger.exception("[AUTH LAST LOGIN UPDATE FAILED]")
        return None


def fallback_env_admin_user(email: str):
    admin_email = os.getenv("ADMIN_EMAIL")

    if not admin_email or email != admin_email:
        return None

    return {
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
        "auth_source": "environment"
    }

# -------------------------------------------------
# REQUIRE AUTH TOKEN
# -------------------------------------------------
def require_auth_token(authorization: str):
    session_secret = os.getenv("SESSION_SECRET")

    if not session_secret:
        raise HTTPException(
            status_code=500,
            detail="SESSION_SECRET not configured"
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid authorization header"
        )

    token = authorization.replace("Bearer ", "").strip()

    try:
        payload = jwt.decode(
            token,
            session_secret,
            algorithms=["HS256"]
        )

        return payload

    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Session expired"
        )

    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=401,
            detail="Invalid session token"
        )


# -------------------------------------------------
# AUTH LOGIN ENDPOINT
# -------------------------------------------------
@app.post("/auth/login")
async def auth_login(request: Request):
    data = await request.json()

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials"
        )

    user = get_user_by_email(email)

    if user:
        if user.get("status") != "active":
            raise HTTPException(
                status_code=403,
                detail="User account is inactive"
            )

        if not verify_password(password, user.get("password_hash")):
            raise HTTPException(
                status_code=401,
                detail="Invalid credentials"
            )

        last_login_at = update_user_last_login(user.get("user_id"))
        if last_login_at:
            user["last_login_at"] = last_login_at

    else:
        # Temporary fallback for platform access until all dashboard users are migrated.
        admin_password = os.getenv("ADMIN_PASSWORD")
        user = fallback_env_admin_user(email)

        if not user or not admin_password or password != admin_password:
            raise HTTPException(
                status_code=401,
                detail="Invalid credentials"
            )

    token = create_session_token(user)

    return {
        "status": "ok",
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
            "last_login_at": user.get("last_login_at").isoformat() if user.get("last_login_at") else None,
            "auth_source": user.get("auth_source")
        },
        "email": user.get("email")
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
            "timezone_label": timezone_label(payload.get("timezone"))
        },
        "email": payload.get("email")
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
            {"value": key, "label": label}
            for key, label in SUPPORTED_TIMEZONES.items()
        ]
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
                        WHERE u.client_key = %s
                        ORDER BY u.created_at ASC;
                    """, (effective_client_key,))
                elif payload.get("role") == "client_user":
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
                        WHERE u.id = %s
                        LIMIT 1;
                    """, (payload.get("user_id"),))
                else:
                    raise HTTPException(status_code=403, detail="Unsupported user role")

                rows = cur.fetchall()

        users = []

        for row in rows:
            users.append({
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
                "timezone_label": timezone_label(row[10])
            })

        return {
            "status": "ok",
            "count": len(users),
            "scope": "platform" if is_platform_admin(payload) else payload.get("client_key"),
            "users": users
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
        raise HTTPException(status_code=403, detail="Client account is not scoped to a client")

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
        raise HTTPException(status_code=403, detail="Access denied for requested client")

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
            status_code=400,
            detail="Password must be at least 12 characters"
        )

    return True


def client_exists(client_key: str):
    database_url = os.getenv("DATABASE_URL")

    if not database_url or not client_key:
        return False

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1
                    FROM clients
                    WHERE client_key = %s
                    LIMIT 1;
                """, (client_key,))

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
        "timezone_label": timezone_label(row[10])
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
            raise HTTPException(status_code=400, detail="client_key is required for client users")

        if not client_exists(client_key):
            raise HTTPException(status_code=404, detail="client_key not found")

    if role == "platform_admin":
        client_key = None

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
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
                """, (
                    client_key,
                    full_name,
                    email,
                    hash_password(password),
                    role,
                    status
                ))

                created = cur.fetchone()

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
                    WHERE u.id = %s
                    LIMIT 1;
                """, (created[0],))

                row = cur.fetchone()

            conn.commit()

        logger.info("[USER CREATED] user_id=%s role=%s client_key=%s", row[0], role, client_key)

        return {
            "status": "ok",
            "message": "User created",
            "user": build_user_response(row)
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
                cur.execute("""
                    UPDATE users
                    SET
                        status = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING id;
                """, (status, user_id))

                updated = cur.fetchone()

            conn.commit()

        if not updated:
            raise HTTPException(status_code=404, detail="user not found")

        logger.info("[USER STATUS UPDATED] user_id=%s status=%s", user_id, status)

        return {
            "status": "ok",
            "message": "User status updated",
            "user_id": user_id,
            "user_status": status
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
                cur.execute("""
                    UPDATE users
                    SET
                        password_hash = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING id;
                """, (hash_password(new_password), user_id))

                updated = cur.fetchone()

            conn.commit()

        if not updated:
            raise HTTPException(status_code=404, detail="user not found")

        logger.info("[USER PASSWORD RESET] user_id=%s", user_id)

        return {
            "status": "ok",
            "message": "User password updated",
            "user_id": user_id
        }

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
            "message": "DATABASE_URL not configured"
        }

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                result = cur.fetchone()

        return {
            "status": "ok",
            "database": "connected",
            "result": result[0]
        }

    except Exception as e:
        logger.exception("DB check failed")
        return {
            "status": "error",
            "database": "connection_failed",
            "message": str(e)
        }


# -------------------------------------------------
# DATABASE INITIALIZATION
# -------------------------------------------------
@app.post("/init-db")
def init_db(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)

    if os.getenv("ALLOW_DB_INIT", "false").lower() != "true":
        raise HTTPException(
            status_code=403,
            detail="Database initialization endpoint is disabled. Set ALLOW_DB_INIT=true temporarily to use it."
        )

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {
            "status": "error",
            "message": "DATABASE_URL not configured"
        }

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
                    cur.execute("""
                        SELECT id
                        FROM users
                        WHERE LOWER(email) = LOWER(%s)
                        LIMIT 1;
                    """, (admin_email,))

                    existing_user = cur.fetchone()

                    if not existing_user:
                        cur.execute("""
                            INSERT INTO users (
                                client_key,
                                full_name,
                                email,
                                password_hash,
                                role,
                                status
                            )
                            VALUES (%s, %s, %s, %s, %s, %s);
                        """, (
                            None,
                            admin_full_name,
                            admin_email.lower(),
                            hash_password(admin_password),
                            "platform_admin",
                            "active"
                        ))

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
                "calls"
            ],
            "routing_enabled": True,
            "settings_enabled": True,
            "seed_client": "hvac_toronto_001"
        }

    except Exception as e:
        logger.exception("Database initialization failed")

        return {
            "status": "error",
            "message": str(e)
        }


# -------------------------------------------------
# CLIENTS READ ENDPOINT
# -------------------------------------------------
@app.get("/clients")
def get_clients(
    client_key: str = Query(None),
    authorization: str = Header(None)
):
    payload = require_auth_token(authorization)
    effective_client_key = resolve_effective_client_key(payload, client_key)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {
            "status": "error",
            "message": "DATABASE_URL not configured"
        }

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                where_sql, params = scoped_where_clause("clients", effective_client_key)
                cur.execute(f"""
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
                """, params)

                rows = cur.fetchall()

        clients = []

        for row in rows:
            clients.append({
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
                "updated_at": row[10].isoformat() if row[10] else None
            })

        return {
            "status": "ok",
            "count": len(clients),
            "scope": "platform" if is_platform_admin(payload) else effective_client_key,
            "client_key_filter": effective_client_key,
            "clients": clients
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Clients read failed")
        return {
            "status": "error",
            "message": str(e)
        }


# -------------------------------------------------
# CLIENT CREATE ENDPOINT
# -------------------------------------------------
@app.post("/clients/create")
async def create_client(request: Request, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {
            "status": "error",
            "message": "DATABASE_URL not configured"
        }

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
            "message": "client_key and business_name are required"
        }

    if not inbound_phone:
        return {
            "status": "error",
            "message": "valid inbound_phone is required"
        }

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
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
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'active', %s)
                    ON CONFLICT (client_key) DO NOTHING;
                """, (
                    client_key,
                    business_name,
                    vertical,
                    plan_tier,
                    inbound_phone,
                    business_phone,
                    timezone
                ))

                client_created = cur.rowcount

                if client_created == 0:
                    return {
                        "status": "error",
                        "message": "client_key already exists",
                        "client_key": client_key
                    }

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
                """, (
                    client_key,
                    inbound_phone,
                    TWILIO_PHONE
                ))

                # -------------------------------------------------
                # CREATE PRIMARY CLIENT CONTACT
                # -------------------------------------------------
                if first_name or last_name or email or contact_phone:
                    cur.execute("""
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
                    """, (
                        client_key,
                        first_name,
                        last_name,
                        email,
                        contact_phone,
                        role
                    ))

                # -------------------------------------------------
                # CREATE PRIMARY CLIENT ADDRESS
                # -------------------------------------------------
                if address_line_1 or city or state_province or postal_code:
                    cur.execute("""
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
                    """, (
                        client_key,
                        address_line_1,
                        address_line_2,
                        city,
                        state_province,
                        postal_code,
                        country
                    ))

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
                    "role": role
                },
                "address": {
                    "address_line_1": address_line_1,
                    "address_line_2": address_line_2,
                    "city": city,
                    "state_province": state_province,
                    "postal_code": postal_code,
                    "country": country
                }
            }
        }

    except Exception as e:
        logger.exception("Client create failed")
        return {
            "status": "error",
            "message": str(e)
        }

# -------------------------------------------------
# CALLS READ ENDPOINT
# -------------------------------------------------
@app.get("/calls")
def get_calls(
    client_key: str = Query(None),
    authorization: str = Header(None)
):
    payload = require_auth_token(authorization)
    effective_client_key = resolve_effective_client_key(payload, client_key)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {
            "status": "error",
            "message": "DATABASE_URL not configured"
        }

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                where_sql, params = scoped_where_clause("calls", effective_client_key)
                cur.execute(f"""
                    SELECT
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
                        created_at
                    FROM calls
                    {where_sql}
                    ORDER BY created_at DESC
                    LIMIT 50;
                """, params)

                rows = cur.fetchall()

        calls = []

        for row in rows:
            calls.append({
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
                "created_at": row[14].isoformat() if row[14] else None
            })

        return {
            "status": "ok",
            "count": len(calls),
            "scope": "platform" if is_platform_admin(payload) else effective_client_key,
            "client_key_filter": effective_client_key,
            "calls": calls
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Calls read failed")
        return {
            "status": "error",
            "message": str(e)
        }

# -------------------------------------------------
# CLIENT SETTINGS READ ENDPOINT
# -------------------------------------------------
@app.get("/client-settings")
def get_client_settings(
    client_key: str = Query(None),
    authorization: str = Header(None)
):
    payload = require_auth_token(authorization)
    effective_client_key = resolve_effective_client_key(payload, client_key)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {
            "status": "error",
            "message": "DATABASE_URL not configured"
        }

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                where_sql, params = scoped_where_clause("client_settings", effective_client_key)
                cur.execute(f"""
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
                """, params)

                rows = cur.fetchall()

        settings = []

        for row in rows:
            settings.append({
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
                "updated_at": row[16].isoformat() if row[16] else None
            })

        return {
            "status": "ok",
            "count": len(settings),
            "scope": "platform" if is_platform_admin(payload) else effective_client_key,
            "client_key_filter": effective_client_key,
            "client_settings": settings
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Client settings read failed")
        return {
            "status": "error",
            "message": str(e)
        }

# -------------------------------------------------
# CLIENT CONTACTS READ ENDPOINT
# -------------------------------------------------
@app.get("/client-contacts")
def get_client_contacts(
    client_key: str = Query(None),
    authorization: str = Header(None)
):
    payload = require_auth_token(authorization)
    effective_client_key = resolve_effective_client_key(payload, client_key)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                where_sql, params = scoped_where_clause("client_contacts", effective_client_key)
                cur.execute(f"""
                    SELECT client_key, first_name, last_name, email, phone, role, is_primary, created_at, updated_at
                    FROM client_contacts
                    {where_sql}
                    ORDER BY created_at DESC;
                """, params)

                rows = cur.fetchall()

        contacts = [{
            "client_key": row[0],
            "first_name": row[1],
            "last_name": row[2],
            "email": row[3],
            "phone": row[4],
            "role": row[5],
            "is_primary": row[6],
            "created_at": row[7].isoformat() if row[7] else None,
            "updated_at": row[8].isoformat() if row[8] else None
        } for row in rows]

        return {
            "status": "ok",
            "count": len(contacts),
            "scope": "platform" if is_platform_admin(payload) else effective_client_key,
            "client_key_filter": effective_client_key,
            "contacts": contacts
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
    client_key: str = Query(None),
    authorization: str = Header(None)
):
    payload = require_auth_token(authorization)
    effective_client_key = resolve_effective_client_key(payload, client_key)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {"status": "error", "message": "DATABASE_URL not configured"}

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                where_sql, params = scoped_where_clause("client_addresses", effective_client_key)
                cur.execute(f"""
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
                """, params)

                rows = cur.fetchall()

        addresses = []

        for row in rows:
            addresses.append({
                "client_key": row[0],
                "address_line_1": row[1],
                "address_line_2": row[2],
                "city": row[3],
                "state_province": row[4],
                "postal_code": row[5],
                "country": row[6],
                "is_primary": row[7],
                "created_at": row[8].isoformat() if row[8] else None,
                "updated_at": row[9].isoformat() if row[9] else None
            })

        return {
            "status": "ok",
            "count": len(addresses),
            "scope": "platform" if is_platform_admin(payload) else effective_client_key,
            "client_key_filter": effective_client_key,
            "addresses": addresses
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
        return {
            "status": "error",
            "message": "DATABASE_URL not configured"
        }

    data = await request.json()

    requested_client_key = (data.get("client_key") or "").strip()
    client_key = require_client_admin_or_platform(payload, requested_client_key)
    twilio_outbound_number = normalize_phone(data.get("twilio_outbound_number"))

    if not client_key or not twilio_outbound_number:
        return {
            "status": "error",
            "message": "client_key and valid twilio_outbound_number are required"
        }

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE client_settings
                    SET
                        twilio_outbound_number = %s,
                        updated_at = NOW()
                    WHERE client_key = %s;
                """, (
                    twilio_outbound_number,
                    client_key
                ))

                updated = cur.rowcount

            conn.commit()

        if updated == 0:
            raise HTTPException(status_code=404, detail="client_settings record not found")

        return {
            "status": "ok",
            "client_key": client_key,
            "twilio_outbound_number": twilio_outbound_number
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("SMS number update failed")
        return {
            "status": "error",
            "message": str(e)
        }

# -------------------------------------------------
# CLIENT SMS SETTINGS UPDATE ENDPOINT
# -------------------------------------------------
@app.post("/client-settings/update-sms-settings")
async def update_sms_settings(request: Request, authorization: str = Header(None)):
    payload = require_auth_token(authorization)

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {
            "status": "error",
            "message": "DATABASE_URL not configured"
        }

    data = await request.json()

    requested_client_key = (data.get("client_key") or "").strip()
    client_key = require_client_admin_or_platform(payload, requested_client_key)
    business_sms_enabled = data.get("business_sms_enabled")
    caller_sms_enabled = data.get("caller_sms_enabled")

    if not client_key:
        return {
            "status": "error",
            "message": "client_key is required"
        }

    if not isinstance(business_sms_enabled, bool) or not isinstance(caller_sms_enabled, bool):
        return {
            "status": "error",
            "message": "business_sms_enabled and caller_sms_enabled must be true or false"
        }

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE client_settings
                    SET
                        business_sms_enabled = %s,
                        caller_sms_enabled = %s,
                        updated_at = NOW()
                    WHERE client_key = %s;
                """, (
                    business_sms_enabled,
                    caller_sms_enabled,
                    client_key
                ))

                updated = cur.rowcount

            conn.commit()

        if updated == 0:
            return {
                "status": "error",
                "message": "client_settings record not found",
                "client_key": client_key
            }

        return {
            "status": "ok",
            "client_key": client_key,
            "business_sms_enabled": business_sms_enabled,
            "caller_sms_enabled": caller_sms_enabled
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("SMS settings update failed")
        return {
            "status": "error",
            "message": str(e)
        }

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
        r"(my name is|this is|i am|i'm)\s+([a-zA-Z]+\s+[a-zA-Z]+)",
        text,
        re.IGNORECASE
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
        "unknown"
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
    elif any(k in text for k in ["service", "maintenance", "checkup", "check up", "tune up", "routine"]):
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
        "urgent"
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
        "schedule service"
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
                    cur.execute("""
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
                    """, (client_key,))

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
                    "source": "database"
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
            "source": "fallback"
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
                cur.execute("""
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
                """, (formatted_phone,))

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
            "source": "database_inbound_phone"
        }

        if client["status"] != "active":
            log_info("[INBOUND CLIENT INACTIVE]", inbound_phone=formatted_phone)
            return None

        log_info("[INBOUND ROUTED]", inbound_phone=formatted_phone, client_key=client["client_key"])

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
                cur.execute("""
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
                """, (client_key,))

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
            "twilio_outbound_number": row[14]
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
    raw_payload
):
    """
    Persists analyzed call results to PostgreSQL.

    Uses ON CONFLICT so Retell retries or duplicate webhook events
    do not create duplicate call records.
    """

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        logger.warning("Call save skipped: DATABASE_URL not configured")
        return {
            "saved": False,
            "error": "DATABASE_URL not configured"
        }

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:             
                # -------------------------------------------------
                # BILLABLE USAGE
                # -------------------------------------------------
                call_duration_seconds = (
                    raw_payload.get("duration_ms", 0) / 1000
                    if raw_payload.get("duration_ms")
                    else 0
                )

                billable_minutes = round(call_duration_seconds / 60, 2)

                cur.execute("""
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
                        billable_minutes
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
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
                        billable_minutes = EXCLUDED.billable_minutes;
                """, (
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
                    billable_minutes
                ))

            conn.commit()

        logger.info("[CALL SAVED] call_id=%s", call_id)

        return {
            "saved": True,
            "error": None
        }

    except Exception as e:
        error = str(e)
        logger.exception("Call save failed")

        return {
            "saved": False,
            "error": error
        }


# -------------------------------------------------
# SMS ELIGIBILITY ENGINE
# -------------------------------------------------
def get_sms_policy(call_outcome, required_fields_present):
    if call_outcome == "confirmed" and required_fields_present:
        return {
            "business": True,
            "caller": True,
            "reason": "confirmed_request"
        }

    if call_outcome == "address_fallback":
        return {
            "business": True,
            "caller": True,
            "reason": "address_fallback"
        }

    return {
        "business": False,
        "caller": False,
        "reason": f"sms_suppressed_for_{call_outcome}"
    }


# -------------------------------------------------
# RETELL INBOUND WEBHOOK
# -------------------------------------------------
@app.post("/webhook/inbound")
async def inbound_webhook(
    request: Request,
    x_webhook_secret: str = Header(None),
    x_retell_signature: str = Header(None)
):
    require_webhook_secret(x_webhook_secret)

    try:
        raw_body = (await request.body()).decode("utf-8")
        verify_retell_signature(
            raw_body,
            x_retell_signature,
            enforce_env="RETELL_VERIFY_INBOUND_SIGNATURE"
        )
        logger.info("[RETELL INBOUND SIGNATURE] Verified")
        data = json.loads(raw_body or "{}")

        call_inbound = data.get("call_inbound") or {}

        from_number = (
            call_inbound.get("from_number")
            or data.get("from_number")
            or ""
        )

        to_number = (
            call_inbound.get("to_number")
            or data.get("to_number")
            or ""
        )

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
            routing_source=routing_source
        )

        return {
            "call_inbound": {
                "dynamic_variables": {
                    "caller_phone": formatted_from_phone or from_number,
                    "client_id": client_id
                },
                "metadata": {
                    "caller_phone": formatted_from_phone or from_number,
                    "client_id": client_id,
                    "to_number": formatted_to_phone or to_number,
                    "routing_source": routing_source
                }
            }
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Inbound webhook failed")

        return {
            "call_inbound": {
                "dynamic_variables": {
                    "client_id": "hvac_toronto_001"
                },
                "metadata": {
                    "client_id": "hvac_toronto_001",
                    "routing_source": "error_fallback"
                }
            }
        }


# -------------------------------------------------
# TRIAGE ENDPOINT
# -------------------------------------------------
@app.post("/webhook/triage")
async def triage(
    request: Request,
    x_webhook_secret: str = Header(None),
    x_retell_signature: str = Header(None)
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
            "confidence": 0.5
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
        "confidence": 0.9 if urgency == "urgent" else 0.75
    }

    log_info(
        "[TRIAGE RESPONSE]",
        urgency=response["urgency"],
        route=response["route"],
        issue_type=response["issue_type"],
        confidence=response["confidence"],
        transcript=transcript_raw
    )

    return response


# -------------------------------------------------
# CALL SUMMARY WEBHOOK
# -------------------------------------------------
@app.post("/webhook/call-summary")
async def call_summary(
    request: Request,
    x_webhook_secret: str = Header(None),
    x_retell_signature: str = Header(None)
):
    require_webhook_secret(x_webhook_secret)

    try:
        raw_body = (await request.body()).decode("utf-8")
        verify_retell_signature(
            raw_body,
            x_retell_signature,
            enforce_env="RETELL_VERIFY_CALL_SUMMARY_SIGNATURE"
        )
        logger.info("[RETELL CALL SUMMARY SIGNATURE] Verified")
        data = json.loads(raw_body or "{}")

        cleanup_state()

        event_type = data.get("event") or data.get("type")
        call = data.get("call") or {}

        call_id = (
            data.get("call_id")
            or data.get("id")
            or call.get("call_id")
        )

        if not call_id:
            return {"status": "error", "message": "missing call_id"}

        metadata = call.get("metadata") or {}

        if event_type == "call_started":
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
                call_key_count=len(call.keys()) if isinstance(call, dict) else 0
            )

            if formatted_phone:
                CALL_PHONE_MAP[call_id] = formatted_phone
                CALL_PHONE_META[call_id] = time.time()
                log_info("[PHONE STORED]", call_id=call_id, caller_phone=formatted_phone)
            else:
                logger.warning("[PHONE NOT FOUND ON CALL_STARTED] call_id=%s", call_id)

            return {
                "status": "phone_capture_processed",
                "call_id": call_id,
                "caller_phone": formatted_phone
            }

        if event_type != "call_analyzed":
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

        messages = (
            call.get("transcript_object")
            or data.get("transcript_object")
            or []
        )

        user_text = build_transcript_text(messages)

        full_transcript = (
            call.get("transcript")
            or data.get("transcript")
            or user_text
            or ""
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
                "twilio_outbound_number": TWILIO_PHONE
            }

        if not client:
            return {
                "status": "error",
                "message": "invalid or inactive client_id",
                "client_id": client_id
            }

        caller_name = (
            custom.get("full_name")
            or custom.get("caller_name")
            or "Unknown"
        )

        service_address = (
            custom.get("service_address")
            or custom.get("address")
            or "Unknown"
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
            formatted_phone = normalize_phone(user_text) or normalize_phone(full_transcript)

        classified_urgency, issue_type = classify_hvac_issue(issue_description)

        if not custom.get("urgency"):
            urgency = classified_urgency

        issue_type = (
            custom.get("issue_type")
            or issue_type
        )

        short_summary = build_short_summary(urgency, issue_type)

        required_fields_present = all([
            caller_name and caller_name != "Unknown",
            formatted_phone,
            service_address and service_address != "Unknown",
            issue_description and issue_description != "No issue description available."
        ])

        sms_policy = get_sms_policy(call_outcome, required_fields_present)

        send_business_sms = (
            sms_policy["business"]
            and client_settings.get("business_sms_enabled", True)
        )

        send_caller_sms = (
            sms_policy["caller"]
            and client_settings.get("caller_sms_enabled", True)
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
            sms_policy_reason=sms_policy_reason
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

        sms_from_number = (
            client_settings.get("twilio_outbound_number")
            or TWILIO_PHONE
        )

        if send_business_sms and twilio_client and sms_from_number:
            try:
                twilio_client.messages.create(
                    body=business_message,
                    from_=sms_from_number,
                    to=client["business_phone"]
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

        if send_caller_sms and formatted_phone and client_settings.get("caller_sms_enabled", True):
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
                        body=caller_message,
                        from_=sms_from_number,
                        to=formatted_phone
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
            raw_payload=data
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
            "call_saved": call_save_result["saved"],
            "call_save_error": call_save_result["error"]
        }

    except Exception as e:
        logger.exception("Call summary webhook failed")
        return {"status": "error", "message": str(e)}