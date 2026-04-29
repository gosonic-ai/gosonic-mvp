from fastapi import FastAPI, Request
from twilio.rest import Client
import os
import time
import re

app = FastAPI()

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
# CLIENT MAP
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

    # Do not match "ac" inside "hvac"
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

        formatted_phone = normalize_phone(from_number)

        print("[INBOUND WEBHOOK]")
        print("from_number:", from_number)
        print("formatted_phone:", formatted_phone)
        print("to_number:", to_number)

        return {
            "call_inbound": {
                "dynamic_variables": {
                    "caller_phone": formatted_phone or from_number,
                    "client_id": "hvac_toronto_001"
                },
                "metadata": {
                    "caller_phone": formatted_phone or from_number,
                    "client_id": "hvac_toronto_001"
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
                    "client_id": "hvac_toronto_001"
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

        # -------------------------------------------------
        # CALL_STARTED — STORE CALLER PHONE
        # -------------------------------------------------
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

        # -------------------------------------------------
        # ONLY PROCESS CALL_ANALYZED FOR SMS
        # -------------------------------------------------
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

        client = CLIENTS.get(client_id)

        if not client:
            return {
                "status": "error",
                "message": "invalid client_id",
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

        print("[CALL SUMMARY DEBUG]")
        print("event_type:", event_type)
        print("call_id:", call_id)
        print("custom_analysis:", custom)
        print("metadata:", metadata)
        print("caller_name:", caller_name)
        print("service_address:", service_address)
        print("caller_phone_raw:", caller_phone_raw)
        print("stored_phone:", CALL_PHONE_MAP.get(call_id))
        print("formatted_phone:", formatted_phone)
        print("issue_description:", issue_description)
        print("urgency:", urgency)
        print("issue_type:", issue_type)
        print("short_summary:", short_summary)

        # -------------------------------------------------
        # BUSINESS SMS
        # -------------------------------------------------
        business_message = (
            "📞 Gosonic Call Alert\n"
            "----------------------\n"
            f"Business: {client['business_name']}\n"
            f"Urgency: {urgency.upper()}\n"
            f"Caller: {caller_name}\n"
            f"Phone: {formatted_phone or 'Unknown'}\n"
            f"Address: {service_address}\n\n"
            f"Summary:\n{short_summary}"
        )

        business_sent = False
        business_error = None

        if twilio_client and TWILIO_PHONE:
            try:
                twilio_client.messages.create(
                    body=business_message,
                    from_=TWILIO_PHONE,
                    to=client["business_phone"]
                )
                business_sent = True
                print("[TWILIO BUSINESS] Sent")
            except Exception as e:
                business_error = str(e)
                print("[TWILIO BUSINESS ERROR]", business_error)
        else:
            business_error = "Twilio client or TWILIO_PHONE missing"
            print("[TWILIO BUSINESS SKIPPED]", business_error)

        # -------------------------------------------------
        # CALLER SMS
        # -------------------------------------------------
        caller_sent = False
        caller_error = None

        if formatted_phone and client.get("caller_enabled"):
            display_name = caller_name if caller_name != "Unknown" else "there"

            caller_message = (
                f"Hi {display_name}, "
                "we’ve received your HVAC service request. "
                "Toronto HVAC has been notified. "
                "Thank you."
            )

            if twilio_client and TWILIO_PHONE:
                try:
                    twilio_client.messages.create(
                        body=caller_message,
                        from_=TWILIO_PHONE,
                        to=formatted_phone
                    )
                    caller_sent = True
                    print("[TWILIO CALLER] Sent")
                except Exception as e:
                    caller_error = str(e)
                    print("[TWILIO CALLER ERROR]", caller_error)
            else:
                caller_error = "Twilio client or TWILIO_PHONE missing"
                print("[TWILIO CALLER SKIPPED]", caller_error)
        else:
            if not formatted_phone:
                caller_error = "Missing or invalid caller phone"
            elif not client.get("caller_enabled"):
                caller_error = "Caller SMS disabled for client"

            print("[TWILIO CALLER SKIPPED]", caller_error)

        CALL_PHONE_MAP.pop(call_id, None)
        CALL_PHONE_META.pop(call_id, None)

        return {
            "status": "processed",
            "client_id": client_id,
            "call_id": call_id,
            "caller_name": caller_name,
            "caller_phone": formatted_phone,
            "service_address": service_address,
            "urgency": urgency,
            "issue_type": issue_type,
            "summary": short_summary,
            "business_notified": business_sent,
            "business_error": business_error,
            "caller_notified": caller_sent,
            "caller_error": caller_error
        }

    except Exception as e:
        print("[WEBHOOK ERROR]", str(e))
        return {"status": "error", "message": str(e)}