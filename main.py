from fastapi import FastAPI, Request
from twilio.rest import Client
import os
import json
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
# TRIAGE ENDPOINT (UNCHANGED CORE)
# -------------------------------------------------
@app.post("/webhook/triage")
async def triage(request: Request):
    data = await request.json()

    transcript = (
        data.get("transcript")
        or data.get("issue_text")
        or data.get("summary")
        or ""
    ).lower()

    caller_name = data.get("caller_name") or ""
    caller_phone = data.get("caller_phone") or ""

    urgent_keywords = [
        "no heat","no heating","furnace","gas leak","gas smell",
        "water leak","leak","broken","not working","failure",
        "completely down","emergency","freezing","urgent"
    ]

    route = "urgent" if any(k in transcript for k in urgent_keywords) else "standard"

    issue_type = "other"

    if any(k in transcript for k in ["heat", "heating", "furnace"]):
        issue_type = "no_heat"
    elif any(k in transcript for k in ["cool", "ac", "air conditioning"]):
        issue_type = "no_cooling"
    elif "leak" in transcript:
        issue_type = "leak"
    elif "maintenance" in transcript:
        issue_type = "maintenance"

    summary = data.get("summary") or f"Caller reports HVAC issue: {transcript}"

    confidence = 0.85 if route == "urgent" else 0.7
    if caller_phone:
        confidence += 0.05
    confidence = round(min(confidence, 0.95), 2)

    return {
        "route": route,
        "summary": summary,
        "issue_type": issue_type,
        "customer_name": caller_name,
        "customer_phone": caller_phone,
        "location": data.get("location", ""),
        "confidence": confidence
    }


# -------------------------------------------------
# HELPER: PHONE EXTRACTION (FIXED + ROBUST)
# -------------------------------------------------
def extract_phone(text):
    if not text:
        return None

    text = text.lower()

    word_map = {
        "zero":"0","one":"1","two":"2","three":"3","four":"4",
        "five":"5","six":"6","seven":"7","eight":"8","nine":"9"
    }

    # 1. PRIORITY: extract raw digits directly (MOST RELIABLE)
    digits = re.findall(r'\d', text)
    if len(digits) >= 10:
        phone = "".join(digits[-10:])
        return phone

    # 2. FALLBACK: spoken numbers
    tokens = text.split()
    converted = []

    for t in tokens:
        if t in word_map:
            converted.append(word_map[t])
        elif t.isdigit():
            converted.append(t)

    phone2 = "".join(converted)

    if len(phone2) >= 10:
        return phone2[-10:]

    return None


# -------------------------------------------------
# HELPER: NAME EXTRACTION
# -------------------------------------------------
def extract_name(text):
    match = re.search(r"(my name is|name is)\s+([a-zA-Z]+\s+[a-zA-Z]+)", text, re.IGNORECASE)
    if match:
        return match.group(2).title()
    return None


# -------------------------------------------------
# HELPER: FORMAT PHONE FOR TWILIO
# -------------------------------------------------
def format_phone(phone):
    if not phone:
        return None

    phone = phone.strip()

    # already valid
    if phone.startswith("+"):
        return phone

    # normalize 10-digit US/CA numbers
    if len(phone) == 10 and phone.isdigit():
        return f"+1{phone}"

    return None


# -------------------------------------------------
# CALL SUMMARY WEBHOOK (FIXED + CALLER SMS)
# -------------------------------------------------
@app.post("/webhook/call-summary")
async def call_summary(request: Request):

    try:
        data = await request.json()

        print("🔥 RAW PAYLOAD RECEIVED")
        print(json.dumps(data, indent=2))

        event_type = data.get("event") or data.get("type") or "unknown"

        FINAL_EVENTS = {"call_analyzed", "call_ended", "call_summary"}

        if event_type not in FINAL_EVENTS:
            return {"status": "ignored_event"}

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
            return {"status": "duplicate_ignored"}

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
        caller_phone = data.get("caller_phone")

        if not caller_phone:
            caller_phone = extract_phone(user_text)

        if not caller_name or caller_name == "Unknown":
            extracted = extract_name(user_text)
            if extracted:
                caller_name = extracted

        formatted_phone = format_phone(caller_phone)

        summary = (
            data.get("summary")
            or data.get("call_summary")
            or user_text
        )

        urgency = data.get("urgency") or "normal"

        # -----------------------------
        # BUSINESS SMS
        # -----------------------------
        business_message = (
            "📞 Gosonic Call Alert\n"
            "----------------------\n"
            f"Business: {client['business_name']}\n"
            f"Caller: {caller_name}\n"
            f"Phone: {caller_phone or 'Unknown'}\n"
            f"Urgency: {urgency}\n\n"
            f"Summary:\n{summary}"
        )

        business_sent = False

        if twilio_client and TWILIO_PHONE:
            try:
                twilio_client.messages.create(
                    body=business_message,
                    from_=TWILIO_PHONE,
                    to=client["business_phone"]
                )
                business_sent = True
                print("✅ Business SMS sent")
            except Exception as e:
                print("[TWILIO BUSINESS ERROR]", str(e))

        # -----------------------------
        # CALLER SMS
        # -----------------------------
        caller_sent = False

        if formatted_phone and client.get("caller_enabled"):
            caller_message = (
                f"Hi {caller_name if caller_name != 'Unknown' else ''}, "
                "we’ve received your HVAC service request. "
                "A technician will contact you shortly. "
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
                    print("✅ Caller SMS sent")
                except Exception as e:
                    print("[TWILIO CALLER ERROR]", str(e))

        return {
            "status": "processed",
            "client_id": client_id,
            "caller_name": caller_name,
            "caller_phone": caller_phone,
            "business_notified": business_sent,
            "caller_notified": caller_sent
        }

    except Exception as e:
        print("[WEBHOOK ERROR]", str(e))
        return {"status": "error", "message": str(e)}