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
# DEDUP
# -------------------------------------------------
PROCESSED_CALLS = set()
PROCESSED_META = {}
PROCESSED_TTL = 60 * 10


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

    patterns = [
        r"(my name is|this is|i am|i'm)\s+([a-zA-Z]+\s+[a-zA-Z]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
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
            content = m.get("content") or ""
            if content:
                user_parts.append(content)

    return " ".join(user_parts).strip()


def classify_hvac_issue(text: str):
    text = (text or "").lower()

    issue_type = "other"

    if any(k in text for k in ["heat", "heating", "heater", "furnace"]):
        issue_type = "no_heat"
    elif any(k in text for k in ["cool", "cooling", "ac", "a/c"]):
        issue_type = "no_cooling"
    elif "leak" in text:
        issue_type = "leak"
    elif any(k in text for k in ["service", "maintenance"]):
        issue_type = "maintenance"

    if any(k in text for k in ["no heat", "not working", "down", "emergency"]):
        urgency = "urgent"
    else:
        urgency = "standard"

    return urgency, issue_type


def build_short_summary(urgency, issue_type):
    if urgency == "urgent":
        return "EMERGENCY SERVICE REQUEST — Urgent HVAC issue."
    return "HVAC SERVICE REQUEST — Standard HVAC service request."


# -------------------------------------------------
# CALL SUMMARY WEBHOOK
# -------------------------------------------------
@app.post("/webhook/call-summary")
async def call_summary(request: Request):
    try:
        data = await request.json()

        event_type = data.get("event") or data.get("type")

        # ✅ CRITICAL FIX: ONLY process call_analyzed
        if event_type != "call_analyzed":
            return {"status": "ignored_event", "event_type": event_type}

        call_id = data.get("call_id") or data.get("id")
        if not call_id:
            return {"status": "error", "message": "missing call_id"}

        if call_id in PROCESSED_CALLS:
            return {"status": "duplicate_ignored"}

        PROCESSED_CALLS.add(call_id)

        call = data.get("call", {})
        analysis = call.get("analysis", {})
        custom = analysis.get("custom_analysis_data", {})

        messages = call.get("transcript_object", [])
        user_text = build_transcript_text(messages)

        # -------------------------------------------------
        # PRIMARY SOURCE (Retell extraction)
        # -------------------------------------------------
        caller_name = custom.get("full_name") or "Unknown"
        service_address = custom.get("service_address") or "Unknown"
        issue_description = custom.get("issue_description") or user_text
        urgency = clean_urgency(custom.get("urgency"))

        # -------------------------------------------------
        # FALLBACKS
        # -------------------------------------------------
        if caller_name == "Unknown":
            caller_name = extract_name(user_text) or "Unknown"

        caller_phone_raw = (
            data.get("caller_phone")
            or call.get("from_number")
            or ""
        )

        formatted_phone = normalize_phone(caller_phone_raw)

        if not urgency:
            urgency, _ = classify_hvac_issue(issue_description)

        _, issue_type = classify_hvac_issue(issue_description)

        short_summary = build_short_summary(urgency, issue_type)

        # -------------------------------------------------
        # DEBUG
        # -------------------------------------------------
        print("[CALL SUMMARY DEBUG]")
        print("event_type:", event_type)
        print("custom_analysis:", custom)
        print("caller_name:", caller_name)
        print("service_address:", service_address)
        print("phone:", formatted_phone)
        print("urgency:", urgency)

        # -------------------------------------------------
        # BUSINESS SMS
        # -------------------------------------------------
        business_message = (
            "📞 Gosonic Call Alert\n"
            "----------------------\n"
            f"Business: Toronto HVAC\n"
            f"Urgency: {urgency.upper()}\n"
            f"Caller: {caller_name}\n"
            f"Phone: {formatted_phone or 'Unknown'}\n"
            f"Address: {service_address}\n\n"
            f"{short_summary}"
        )

        if twilio_client:
            twilio_client.messages.create(
                body=business_message,
                from_=TWILIO_PHONE,
                to=CLIENTS["hvac_toronto_001"]["business_phone"]
            )
            print("[TWILIO BUSINESS] Sent")

        # -------------------------------------------------
        # CALLER SMS
        # -------------------------------------------------
        if formatted_phone:
            caller_message = (
                f"Hi {caller_name}, "
                "we’ve received your HVAC service request. "
                "Toronto HVAC has been notified."
            )

            twilio_client.messages.create(
                body=caller_message,
                from_=TWILIO_PHONE,
                to=formatted_phone
            )
            print("[TWILIO CALLER] Sent")

        return {"status": "processed"}

    except Exception as e:
        print("[WEBHOOK ERROR]", str(e))
        return {"status": "error"}