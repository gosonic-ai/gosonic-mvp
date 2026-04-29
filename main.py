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
    print("⚠️ Twilio not configured (missing env vars)")


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
# STRONG DEDUP
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
# PHONE NORMALIZATION
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


# -------------------------------------------------
# NAME EXTRACTION
# -------------------------------------------------
def extract_name(text):
    if not text:
        return None

    match = re.search(
        r"(my name is|name is)\s+([a-zA-Z]+\s+[a-zA-Z]+)",
        text,
        re.IGNORECASE
    )

    if match:
        return match.group(2).title()

    return None


# -------------------------------------------------
# TRIAGE CLASSIFICATION
# -------------------------------------------------
def classify_hvac_issue(transcript: str):
    text = (transcript or "").lower()

    urgent_keywords = [
        "no heat",
        "no heating",
        "no hot air",
        "not heating",
        "heater is out",
        "heat is out",
        "heating is out",
        "furnace is out",
        "furnace stopped",
        "furnace not working",
        "not putting out heat",
        "not putting out any heat",
        "gas leak",
        "gas smell",
        "smell gas",
        "carbon monoxide",
        "water leak",
        "leak",
        "flood",
        "broken",
        "not working",
        "failure",
        "completely down",
        "system down",
        "emergency",
        "freezing",
        "urgent"
    ]

    # TEMP MVP OVERRIDE:
    # Any heating failure or heat-related issue routes urgent.
    heating_override_keywords = [
        "heat",
        "heating",
        "heater",
        "furnace"
    ]

    issue_type = "other"

    if any(k in text for k in ["heat", "heating", "heater", "furnace"]):
        issue_type = "no_heat"
    elif any(k in text for k in ["cool", "cooling", "ac", "a/c", "air conditioning"]):
        issue_type = "no_cooling"
    elif "leak" in text:
        issue_type = "leak"
    elif "maintenance" in text or "tune up" in text or "service check" in text:
        issue_type = "maintenance"

    if any(k in text for k in urgent_keywords):
        urgency = "urgent"
    elif any(k in text for k in heating_override_keywords):
        urgency = "urgent"
    else:
        urgency = "standard"

    return urgency, issue_type


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
        or ""
    )

    caller_name = data.get("caller_name") or ""
    caller_phone = data.get("caller_phone") or ""

    urgency, issue_type = classify_hvac_issue(transcript_raw)

    summary = data.get("summary") or f"Caller reports HVAC issue: {transcript_raw}"

    confidence = 0.9 if urgency == "urgent" else 0.75
    if caller_phone:
        confidence += 0.05
    confidence = round(min(confidence, 0.95), 2)

    response = {
        # CRITICAL FOR RETELL EQUATION TRANSITIONS
        "urgency": urgency,

        # Kept for backwards compatibility / debugging
        "route": urgency,

        "summary": summary,
        "issue_type": issue_type,
        "customer_name": caller_name,
        "customer_phone": caller_phone,
        "location": data.get("location", ""),
        "confidence": confidence
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

        event_type = data.get("event") or data.get("type") or "unknown"

        FINAL_EVENTS = {"call_analyzed", "call_ended", "call_summary"}

        if event_type not in FINAL_EVENTS:
            return {"status": "ignored_event", "event_type": event_type}

        call_id = (
            data.get("call", {}).get("call_id")
            or data.get("call_id")
        )

        if not call_id:
            return {"status": "error", "message": "missing call_id"}

        now = time.time()

        for k in list(PROCESSED_META.keys()):
            if now - PROCESSED_META[k] > PROCESSED_TTL:
                PROCESSED_META.pop(k, None)
                PROCESSED_CALLS.discard(k)

        if call_id in PROCESSED_CALLS:
            return {"status": "duplicate_ignored", "call_id": call_id}

        PROCESSED_CALLS.add(call_id)
        PROCESSED_META[call_id] = now

        messages = (
            data.get("transcript_object")
            or data.get("call", {}).get("transcript_object")
            or []
        )

        user_text = " ".join(
            m.get("content", "")
            for m in messages
            if isinstance(m, dict) and m.get("role") == "user"
        ).strip()

        client_id = (
            data.get("client_id")
            or data.get("call", {}).get("metadata", {}).get("client_id")
            or "hvac_toronto_001"
        )

        client = CLIENTS.get(client_id)

        if not client:
            return {"status": "error", "message": "invalid client_id"}

        # -----------------------------
        # EXTRACTION
        # -----------------------------
        caller_name = data.get("caller_name") or "Unknown"

        caller_phone_raw = (
            data.get("caller_phone")
            or data.get("call", {}).get("from_number")
            or data.get("from_number")
            or ""
        )

        if not caller_phone_raw:
            caller_phone_raw = normalize_phone(user_text) or ""

        if not caller_name or caller_name == "Unknown":
            extracted = extract_name(user_text)
            if extracted:
                caller_name = extracted

        formatted_phone = normalize_phone(caller_phone_raw)

        summary = (
            data.get("summary")
            or data.get("call_summary")
            or user_text
            or "No summary available."
        )

        urgency = (
            data.get("urgency")
            or data.get("route")
            or "standard"
        )

        if urgency == "normal":
            urgency = "standard"

        print("[CALL SUMMARY DEBUG]")
        print("event_type:", event_type)
        print("call_id:", call_id)
        print("caller_name:", caller_name)
        print("caller_phone_raw:", caller_phone_raw)
        print("formatted_phone:", formatted_phone)
        print("urgency:", urgency)
        print("user_text:", user_text)

        # -----------------------------
        # BUSINESS SMS
        # -----------------------------
        business_message = (
            "📞 Gosonic Call Alert\n"
            "----------------------\n"
            f"Business: {client['business_name']}\n"
            f"Caller: {caller_name}\n"
            f"Phone: {formatted_phone or caller_phone_raw or 'Unknown'}\n"
            f"Urgency: {urgency}\n\n"
            f"Summary:\n{summary}"
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

        # -----------------------------
        # CALLER SMS
        # -----------------------------
        caller_sent = False
        caller_error = None

        if formatted_phone and client.get("caller_enabled"):
            display_name = caller_name if caller_name != "Unknown" else "there"

            caller_message = (
                f"Hi {display_name}, "
                "we’ve received your HVAC service request. "
                "A confirmation text will be sent with the details shortly. "
                "Thank you for choosing Toronto HVAC."
            )

            if twilio_client and TWILIO_PHONE:
                try:
                    twilio_client.messages.create(
                        body=caller_message,
                        from_=TWILIO_PHONE,
                        to=formatted_phone
                    )
                    caller_sent = True
                    print("[TWILIO CALLER] Sent to", formatted_phone)
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

        return {
            "status": "processed",
            "client_id": client_id,
            "caller_name": caller_name,
            "caller_phone_raw": caller_phone_raw,
            "caller_phone": formatted_phone,
            "urgency": urgency,
            "business_notified": business_sent,
            "business_error": business_error,
            "caller_notified": caller_sent,
            "caller_error": caller_error
        }

    except Exception as e:
        print("[WEBHOOK ERROR]", str(e))
        return {"status": "error", "message": str(e)}