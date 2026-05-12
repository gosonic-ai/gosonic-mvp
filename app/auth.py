from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import jwt
import secrets

from fastapi import Header, HTTPException

from app.config import (
    ADMIN_API_KEY,
    ADMIN_COMPANY_NAME,
    ADMIN_EMAIL,
    ADMIN_FULL_NAME,
    ADMIN_PASSWORD,
    DEFAULT_CLIENT_TIMEZONE,
    SESSION_SECRET,
)


# -------------------------------------------------
# ADMIN AUTH
# -------------------------------------------------
def require_admin(x_admin_key: str):
    if not ADMIN_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="ADMIN_API_KEY not configured"
        )

    if x_admin_key != ADMIN_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )

    return True


# -------------------------------------------------
# PASSWORD HASHING
# -------------------------------------------------
def hash_password(password: str):
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
        algorithm, iterations, salt, expected_digest = (
            password_hash.split("$", 3)
        )

        if algorithm != "pbkdf2_sha256":
            return False

        candidate_digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations)
        ).hex()

        return hmac.compare_digest(
            candidate_digest,
            expected_digest
        )

    except Exception:
        return False


# -------------------------------------------------
# SESSION TOKENS
# -------------------------------------------------
def create_session_token(user_profile: dict):
    if not SESSION_SECRET:
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
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc)
        + timedelta(hours=12),
    }

    token = jwt.encode(
        payload,
        SESSION_SECRET,
        algorithm="HS256"
    )

    return token


def require_auth_token(
    authorization: str = Header(None)
):
    if not SESSION_SECRET:
        raise HTTPException(
            status_code=500,
            detail="SESSION_SECRET not configured"
        )

    if (
        not authorization
        or not authorization.startswith("Bearer ")
    ):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid authorization header"
        )

    token = authorization.replace(
        "Bearer ",
        ""
    ).strip()

    try:
        payload = jwt.decode(
            token,
            SESSION_SECRET,
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
# FALLBACK ADMIN
# -------------------------------------------------
def fallback_env_admin_user(email: str):
    if not ADMIN_EMAIL or email != ADMIN_EMAIL:
        return None

    return {
        "user_id": None,
        "client_key": None,
        "full_name": ADMIN_FULL_NAME,
        "email": ADMIN_EMAIL,
        "role": "platform_admin",
        "status": "active",
        "last_login_at": None,
        "business_name": ADMIN_COMPANY_NAME,
        "timezone": DEFAULT_CLIENT_TIMEZONE,
        "auth_source": "environment"
    }