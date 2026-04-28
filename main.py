from fastapi import FastAPI, Request
from twilio.rest import Client
import os
import json
import time

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
# 🔥 FIXED TRIAGE ENDPOINT (FULL EXTRACTION)
# -------------------------------------------------
@app.post("/webhook/triage")
async def triage(request: Request):
    data = await request.json()

    print("🔥 TRIAGE REQUEST RECEIVED")
    print(json.dumps(data, indent=2))

    # -----------------------------
    # INPUT NORMALIZATION
    # -----------------------------
    transcript = (
        data.get("transcript")
        or data.get("issue_text")
        or ""
    ).lower()

    caller_name = data.get("caller_name", "")
    caller_phone = data.get("caller_phone", "")
    call_id = data.get("call_id", "")

    # -----------------------------
    # URGENCY LOGIC
    # -----------------------------
    urgent_keywords = [
        "no heat",
        "no heating",
        "gas",
        "gas smell",
        "leak",
        "water leak",
        "broken",
        "not working",
        "failure",
        "completely down"
    ]

    route = "standard"

    if any(k in transcript for k in urgent_keywords):
        route = "urgent"

    # -----------------------------
    # ISSUE CLASSIFICATION
    # -----------------------------
    issue_type = "other"

    if "heat" in transcript or "heating" in transcript:
        issue_type = "no_heat"
    elif "cool" in transcript or "ac" in transcript:
        issue_type = "no_cooling"
    elif "leak" in transcript:
        issue_type = "leak"
    elif "maintenance" in transcript:
        issue_type = "maintenance"

    # -----------------------------
    # SUMMARY GENERATION (SIMPLE MVP)
    # -----------------------------
    summary = data.get("summary")

    if not summary:
        summary = f"Caller reports: {transcript}" if transcript else "No transcript provided"

    # -----------------------------
    # CONFIDENCE (HEURISTIC)
    # -----------------------------
    confidence = 0.6

    if route == "urgent":
        confidence = 0.85
    if caller_phone:
        confidence += 0.05

    # -----------------------------
    # FINAL RESPONSE
    # -----------------------------
    response = {
        "route": route,
        "summary": summary,
        "issue_type": issue_type,
        "customer_name": caller_name,
        "customer_phone": caller_phone,
        "location": data.get("location", ""),
        "confidence": confidence
    }

    print("🧭 FINAL TRIAGE RESPONSE:")
    print(json.dumps(response, indent=2))

    return response


# -------------------------------------------------
# CALL SUMMARY WEBHOOK (UNCHANGED)
# -------------------------------------------------
@app.post("/webhook/call-summary")
async def call_summary(request: Request):

    try:
        data = await request.json()

        print("🔥 RAW PAYLOAD RECEIVED")
        print(json.dumps(data, indent=2))

        event_type = data.get("event") or data.get("type") or "unknown"
        print("📡 EVENT TYPE:", event_type)

        FINAL_EVENTS = {"call_analyzed", "call_ended", "call_summary"}

        if event_type not in FINAL_EVENTS:
            print("⏭ Ignored non-final event:", event_type)
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

        caller_name = data.get("caller_name") or "Unknown"
        caller_phone = data.get("caller_phone")

        summary = (
            data.get("summary")
            or data.get("call_summary")
            or user_text
        )

        urgency = data.get("urgency") or "normal"

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
            except Exception as e:
                print("[TWILIO ERROR]", str(e))

        return {
            "status": "processed",
            "client_id": client_id,
            "summary": summary,
            "business_notified": business_sent
        }

    except Exception as e:
        print("[WEBHOOK ERROR]", str(e))
        return {"status": "error", "message": str(e)}