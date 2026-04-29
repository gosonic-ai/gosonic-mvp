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
        r"(my name is|name is|this is|i am|i'm)\s+([a-zA-Z]+\s+[a-zA-Z]+)",
        r"(my name is|name is|this is|i am|i'm)\s+([a-zA-Z]+)"
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(2).title()

    return None


def get_nested(data, path, default=None):
    current = data

    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)

    return current if current is not None else default


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
        if not isinstance(m, dict):
            continue

        role = m.get("role")
        content = m.get("content") or m.get("text") or ""

        if role == "user" and content:
            user_parts.append(str(content))

    return " ".join(user_parts).strip()


def classify_hvac_issue(text: str):
    text = (text or "").lower()

    issue_type = "other"

    if any(k in text for k in ["heat", "heating", "heater", "furnace"]):
        issue_type = "no_heat"
    elif any(k in text for k in ["cool", "cooling", "ac", "a/c", "air conditioning"]):
        issue_type = "no_cooling"
    elif "leak" in text:
        issue_type = "leak"
    elif any(k in text for k in ["maintenance", "tune up", "service check", "regular service", "routine service"]):
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

    heating_override_keywords = [
        "heat",
        "heating",
        "heater",
        "furnace"
    ]

    if any(k in text for k in urgent_keywords):
        urgency = "urgent"
    elif any(k in text for k in heating_override_keywords):
        urgency = "urgent"
    else:
        urgency = "standard"

    return urgency, issue_type


def build_short_summary(urgency, issue_type):
    if urgency == "urgent":
        if issue_type == "no_heat":
            return "EMERGENCY SERVICE REQUEST — Heater/furnace down or not heating."
        if issue_type == "leak":
            return "EMERGENCY SERVICE REQUEST — Leak reported."
        if issue_type == "no_cooling":
            return "EMERGENCY SERVICE REQUEST — Cooling/AC issue."
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

    caller_name = data.get("caller_name") or data.get("customer_name") or ""
    caller_phone = data.get("caller_phone") or data.get("customer_phone") or ""

    urgency, issue_type = classify_hvac_issue(transcript_raw)

    summary = data.get("summary") or f"Caller reports HVAC issue: {transcript_raw}"

    confidence = 0.9 if urgency == "urgent" else 0.75

    if caller_phone:
        confidence += 0.05

    confidence = round(min(confidence, 0.95), 2)

    response = {
        "urgency": urgency,
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
            get_nested(data, ["call", "call_id"])
            or data.get("call_id")
            or data.get("id")
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

        # -------------------------------------------------
        # RETELL / PAYLOAD EXTRACTION
        # -------------------------------------------------
        call_data = data.get("call") or {}
        metadata = call_data.get("metadata") or {}

        analysis = call_data.get("analysis") or data.get("analysis") or {}
        custom_analysis = analysis.get("custom_analysis_data") or {}

        dynamic_variables = (
            data.get("dynamic_variables")
            or call_data.get("dynamic_variables")
            or data.get("variables")
            or call_data.get("variables")
            or {}
        )

        messages = (
            data.get("transcript_object")
            or call_data.get("transcript_object")
            or []
        )

        user_text = build_transcript_text(messages)

        full_transcript = (
            data.get("transcript")
            or call_data.get("transcript")
            or user_text
            or ""
        )

        client_id = (
            data.get("client_id")
            or metadata.get("client_id")
            or custom_analysis.get("client_id")
            or dynamic_variables.get("client_id")
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
            dynamic_variables.get("full_name")
            or data.get("caller_name")
            or data.get("customer_name")
            or custom_analysis.get("caller_name")
            or custom_analysis.get("customer_name")
            or metadata.get("caller_name")
            or metadata.get("customer_name")
            or "Unknown"
        )

        if not caller_name or caller_name == "Unknown":
            extracted_name = extract_name(user_text) or extract_name(full_transcript)
            if extracted_name:
                caller_name = extracted_name

        service_address = (
            dynamic_variables.get("service_address")
            or data.get("service_address")
            or custom_analysis.get("service_address")
            or metadata.get("service_address")
            or "Unknown"
        )

        caller_phone_raw = (
            dynamic_variables.get("caller_phone")
            or data.get("caller_phone")
            or data.get("customer_phone")
            or custom_analysis.get("caller_phone")
            or custom_analysis.get("customer_phone")
            or metadata.get("caller_phone")
            or metadata.get("customer_phone")
            or call_data.get("from_number")
            or data.get("from_number")
            or ""
        )

        if not caller_phone_raw:
            caller_phone_raw = (
                normalize_phone(user_text)
                or normalize_phone(full_transcript)
                or ""
            )

        formatted_phone = normalize_phone(caller_phone_raw)

        issue_description = (
            dynamic_variables.get("issue_description")
            or data.get("issue_description")
            or custom_analysis.get("issue_description")
            or metadata.get("issue_description")
            or data.get("summary")
            or data.get("call_summary")
            or analysis.get("call_summary")
            or user_text
            or full_transcript
            or "No issue description available."
        )

        payload_urgency = (
            dynamic_variables.get("urgency")
            or data.get("urgency")
            or data.get("route")
            or custom_analysis.get("urgency")
            or custom_analysis.get("route")
            or metadata.get("urgency")
            or metadata.get("route")
            or ""
        )

        urgency = clean_urgency(payload_urgency)

        classification_text = " ".join([
            str(user_text or ""),
            str(full_transcript or ""),
            str(issue_description or "")
        ])

        classified_urgency, issue_type = classify_hvac_issue(classification_text)

        if classified_urgency == "urgent":
            urgency = "urgent"

        issue_type = (
            dynamic_variables.get("issue_type")
            or custom_analysis.get("issue_type")
            or data.get("issue_type")
            or issue_type
        )

        short_summary = build_short_summary(urgency, issue_type)

        # -------------------------------------------------
        # DEBUG LOGS
        # -------------------------------------------------
        print("[CALL SUMMARY DEBUG]")
        print("event_type:", event_type)
        print("call_id:", call_id)
        print("client_id:", client_id)
        print("dynamic_variables:", dynamic_variables)
        print("caller_name:", caller_name)
        print("caller_phone_raw:", caller_phone_raw)
        print("formatted_phone:", formatted_phone)
        print("service_address:", service_address)
        print("payload_urgency:", payload_urgency)
        print("final_urgency:", urgency)
        print("issue_type:", issue_type)
        print("issue_description:", issue_description)
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
            f"Phone: {formatted_phone or caller_phone_raw or 'Unknown'}\n"
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
                "A confirmation has been sent to Toronto HVAC. "
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
            "call_id": call_id,
            "caller_name": caller_name,
            "caller_phone_raw": caller_phone_raw,
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