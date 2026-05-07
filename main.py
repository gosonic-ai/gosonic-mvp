from fastapi import FastAPI, Request, Header, HTTPException
from twilio.rest import Client
from psycopg.types.json import Jsonb
import psycopg
import os
import time
import re

app = FastAPI()

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


# -------------------------------------------------
# ENV / TWILIO SETUP
# -------------------------------------------------
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE = os.getenv("TWILIO_PHONE")

twilio_client = None

if TWILIO_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)
    print("✅ Twilio client initialized")
else:
    print("⚠️ Twilio not configured")


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
# HEALTH CHECK
# -------------------------------------------------
@app.get("/")
def root():
    return {"status": "Gosonic MVP running"}


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
        print("[DB CHECK ERROR]", str(e))
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
                        timezone TEXT NOT NULL DEFAULT 'America/Toronto',
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
                        caller_notified BOOLEAN NOT NULL DEFAULT FALSE,
                        raw_payload JSONB,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
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

            conn.commit()

        return {
            "status": "ok",
            "message": "Database initialized",
            "tables_created": [
                "clients",
                "client_settings",
                "calls"
            ],
            "routing_enabled": True,
            "settings_enabled": True,
            "seed_client": "hvac_toronto_001"
        }

    except Exception as e:
        print("[INIT DB ERROR]", str(e))

        return {
            "status": "error",
            "message": str(e)
        }


# -------------------------------------------------
# CLIENTS READ ENDPOINT
# -------------------------------------------------
@app.get("/clients")
def get_clients(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {
            "status": "error",
            "message": "DATABASE_URL not configured"
        }

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
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
                    ORDER BY created_at ASC;
                """)

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
                "created_at": row[9].isoformat() if row[9] else None,
                "updated_at": row[10].isoformat() if row[10] else None
            })

        return {
            "status": "ok",
            "count": len(clients),
            "clients": clients
        }

    except Exception as e:
        print("[CLIENTS READ ERROR]", str(e))
        return {
            "status": "error",
            "message": str(e)
        }


# -------------------------------------------------
# CALLS READ ENDPOINT
# -------------------------------------------------
@app.get("/calls")
def get_calls(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {
            "status": "error",
            "message": "DATABASE_URL not configured"
        }

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
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
                        caller_notified,
                        created_at
                    FROM calls
                    ORDER BY created_at DESC
                    LIMIT 50;
                """)

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
                "caller_notified": row[11],
                "created_at": row[12].isoformat() if row[12] else None
            })

        return {
            "status": "ok",
            "count": len(calls),
            "calls": calls
        }

    except Exception as e:
        print("[CALLS READ ERROR]", str(e))
        return {
            "status": "error",
            "message": str(e)
        }

# -------------------------------------------------
# CLIENT SETTINGS READ ENDPOINT
# -------------------------------------------------
@app.get("/client-settings")
def get_client_settings(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {
            "status": "error",
            "message": "DATABASE_URL not configured"
        }

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
                        twilio_outbound_number,
                        created_at,
                        updated_at
                    FROM client_settings
                    ORDER BY created_at ASC;
                """)

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
            "client_settings": settings
        }

    except Exception as e:
        print("[CLIENT SETTINGS READ ERROR]", str(e))
        return {
            "status": "error",
            "message": str(e)
        }

# -------------------------------------------------
# CLIENT SETTINGS UPDATE ENDPOINT
# -------------------------------------------------
@app.post("/client-settings/update-sms-number")
async def update_sms_number(request: Request, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {
            "status": "error",
            "message": "DATABASE_URL not configured"
        }

    data = await request.json()

    client_key = data.get("client_key")
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

            conn.commit()

        return {
            "status": "ok",
            "client_key": client_key,
            "twilio_outbound_number": twilio_outbound_number
        }

    except Exception as e:
        print("[SMS NUMBER UPDATE ERROR]", str(e))
        return {
            "status": "error",
            "message": str(e)
        }

# -------------------------------------------------
# CLIENT SMS SETTINGS UPDATE ENDPOINT
# -------------------------------------------------
@app.post("/client-settings/update-sms-settings")
async def update_sms_settings(request: Request, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        return {
            "status": "error",
            "message": "DATABASE_URL not configured"
        }

    data = await request.json()

    client_key = data.get("client_key")
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

    except Exception as e:
        print("[SMS SETTINGS UPDATE ERROR]", str(e))
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
                    print(f"[CLIENT INACTIVE] {client_key}")
                    return None

                return client

            print(f"[CLIENT DB MISS] {client_key}")

        except Exception as e:
            print("[CLIENT DB LOOKUP ERROR]", str(e))

    fallback_client = CLIENTS.get(client_key)

    if fallback_client:
        print(f"[CLIENT FALLBACK USED] {client_key}")

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
            print(f"[INBOUND ROUTING MISS] {formatted_phone}")
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
            print(f"[INBOUND CLIENT INACTIVE] {formatted_phone}")
            return None

        print(f"[INBOUND ROUTED] {formatted_phone} -> {client['client_key']}")

        return client

    except Exception as e:
        print("[INBOUND ROUTING ERROR]", str(e))
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
            print(f"[CLIENT SETTINGS MISS] {client_key}")
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
        print("[CLIENT SETTINGS LOOKUP ERROR]", str(e))
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
    caller_notified,
    raw_payload
):
    """
    Persists analyzed call results to PostgreSQL.

    Uses ON CONFLICT so Retell retries or duplicate webhook events
    do not create duplicate call records.
    """

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        print("[CALL SAVE SKIPPED] DATABASE_URL not configured")
        return {
            "saved": False,
            "error": "DATABASE_URL not configured"
        }

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
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
                        caller_notified,
                        raw_payload
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
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
                        caller_notified = EXCLUDED.caller_notified,
                        raw_payload = EXCLUDED.raw_payload;
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
                    caller_notified,
                    Jsonb(raw_payload)
                ))

            conn.commit()

        print(f"[CALL SAVED] {call_id}")

        return {
            "saved": True,
            "error": None
        }

    except Exception as e:
        error = str(e)
        print("[CALL SAVE ERROR]", error)

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
async def inbound_webhook(request: Request):
    try:
        data = await request.json()

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

        print("[INBOUND WEBHOOK]")
        print("from_number:", from_number)
        print("formatted_from_phone:", formatted_from_phone)
        print("to_number:", to_number)
        print("formatted_to_phone:", formatted_to_phone)
        print("client_id:", client_id)
        print("routing_source:", routing_source)

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

    except Exception as e:
        print("[INBOUND WEBHOOK ERROR]", str(e))

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
async def triage(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        print("[TRIAGE ERROR] Invalid JSON:", str(e))
        return {
            "urgency": "standard",
            "route": "standard",
            "summary": "Unable to parse triage payload.",
            "issue_type": "other",
            "confidence": 0.5
        }

    transcript_raw = (
        data.get("transcript")
        or data.get("issue_text")
        or data.get("summary")
        or data.get("message")
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

    print("[TRIAGE INPUT]", transcript_raw)
    print("[TRIAGE RESPONSE]", response)

    return response


# -------------------------------------------------
# CALL SUMMARY WEBHOOK
# -------------------------------------------------
@app.post("/webhook/call-summary")
async def call_summary(request: Request):
    try:
        data = await request.json()

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

            print("[CALL STARTED DEBUG]")
            print("call_id:", call_id)
            print("caller_phone_raw:", caller_phone_raw)
            print("formatted_phone:", formatted_phone)
            print("metadata:", metadata)
            print("call_keys:", list(call.keys()) if isinstance(call, dict) else None)

            if formatted_phone:
                CALL_PHONE_MAP[call_id] = formatted_phone
                CALL_PHONE_META[call_id] = time.time()
                print(f"[PHONE STORED] {call_id} -> {formatted_phone}")
            else:
                print(f"[PHONE NOT FOUND ON CALL_STARTED] {call_id}")

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
            print(f"[CLIENT SETTINGS FALLBACK] {client_id}")

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

        print("[CALL SUMMARY DEBUG]")
        print("event_type:", event_type)
        print("call_id:", call_id)
        print("custom_analysis:", custom)
        print("metadata:", metadata)
        print("client_id:", client_id)
        print("client_source:", client.get("source"))
        print("client_business_name:", client.get("business_name"))
        print("client_settings:", client_settings)
        print("caller_name:", caller_name)
        print("service_address:", service_address)
        print("caller_phone_raw:", caller_phone_raw)
        print("stored_phone:", CALL_PHONE_MAP.get(call_id))
        print("formatted_phone:", formatted_phone)
        print("issue_description:", issue_description)
        print("urgency:", urgency)
        print("issue_type:", issue_type)
        print("short_summary:", short_summary)
        print("call_outcome:", call_outcome)
        print("required_fields_present:", required_fields_present)
        print("sms_policy:", sms_policy)

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
                print("[TWILIO BUSINESS] Sent")
            except Exception as e:
                business_error = str(e)
                print("[TWILIO BUSINESS ERROR]", business_error)
        else:
            if not send_business_sms:
                business_error = f"Business SMS suppressed: {sms_policy_reason}"
            elif not twilio_client or not sms_from_number:
                business_error = "Twilio client or SMS sender number missing"
                

            print("[TWILIO BUSINESS SKIPPED]", business_error)

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
                    print("[TWILIO CALLER] Sent")

                except Exception as e:
                    caller_error = str(e)
                    print("[TWILIO CALLER ERROR]", caller_error)

            else:
                caller_error = "Twilio client or SMS sender number missing"
                print("[TWILIO CALLER SKIPPED]", caller_error)

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

            print("[TWILIO CALLER SKIPPED]", caller_error)

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
            caller_notified=caller_sent,
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
        print("[WEBHOOK ERROR]", str(e))
        return {"status": "error", "message": str(e)}