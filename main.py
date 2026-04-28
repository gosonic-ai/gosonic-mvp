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
# STRONG DEDUP (ONE CALL = ONE PROCESS ONLY)
# -------------------------------------------------
PROCESSED_CALLS = set()
PROCESSED_META = {}
PROCESSED_TTL = 60 * 10  # 10 min cleanup


# -------------------------------------------------
# HEALTH CHECK
# -------------------------------------------------
@app.get("/")
def root():
    return {"status": "Gosonic MVP running"}


# -------------------------------------------------
# 🔥 NEW: RETELL TRIAGE ENDPOINT (FIX FOR YOUR 404)
# -------------------------------------------------
@app.post("/webhook/triage")
async def triage(request: Request):
    data = await request.json()

    print("🔥 TRIAGE REQUEST RECEIVED")
    print(json.dumps(data, indent=2))

    issue_text = data.get("issue_text", "").lower()

    # -----------------------------
    # SIMPLE HVAC TRIAGE LOGIC
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

    route = "STANDARD"

    if any(keyword in issue_text for keyword in urgent_keywords):
        route = "URGENT"

    print("🧭 ROUTE DECISION:", route)

    return {
        "route": route
    }


# -------------------------------------------------
# CORE WEBHOOK (CALL SUMMARY / TWILIO FLOW)
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
            print("❌ Missing call_id, skipping")
            return {"status": "error", "message": "missing call_id"}

        now = time.time()

        for k in list(PROCESSED_META.keys()):
            if now - PROCESSED_META[k] > PROCESSED_TTL:
                PROCESSED_META.pop(k, None)
                PROCESSED_CALLS.discard(k)

        if call_id in PROCESSED_CALLS:
            print("⚠️ Duplicate call ignored:", call_id)
            return {"status": "duplicate_ignored"}

        PROCESSED_CALLS.add(call_id)
        PROCESSED_META[call_id] = now

        print("🧷 PROCESSING CALL ONCE:", call_id)

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

        print("🧠 USER TEXT:", user_text)

        client_id = (
            data.get("client_id")
            or data.get("call", {}).get("metadata", {}).get("client_id")
            or data.get("call", {}).get("client")
            or "hvac_toronto_001"
        )

        print("🧾 CLIENT ID:", client_id)

        call_obj = data.get("call", {}) or {}

        caller_name = data.get("caller_name") or "Unknown"
        caller_phone = (
            data.get("caller_phone")
            or call_obj.get("from_number")
        )

        summary = (
            data.get("summary")
            or data.get("call_summary")
            or call_obj.get("summary")
            or user_text
        )

        urgency = data.get("urgency") or "normal"

        client = CLIENTS.get(client_id)

        if not client:
            print("❌ Invalid client_id:", client_id)
            return {"status": "error", "message": "invalid client_id"}

        print(f"[GOSONIC] client={client_id} caller={caller_name}")

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
                print("📤 Sending business SMS...")
                twilio_client.messages.create(
                    body=business_message,
                    from_=TWILIO_PHONE,
                    to=client["business_phone"]
                )
                business_sent = True
                print("✅ Business SMS sent")
            except Exception as e:
                print("[TWILIO BUSINESS ERROR]", str(e))

        caller_sent = False

        if caller_phone and client.get("caller_enabled"):
            caller_message = (
                f"Hi {caller_name}, we’ve received your request.\n"
                f"{client['business_name']} will contact you shortly."
            )

            if twilio_client and TWILIO_PHONE:
                try:
                    print("📤 Sending caller SMS...")
                    twilio_client.messages.create(
                        body=caller_message,
                        from_=TWILIO_PHONE,
                        to=caller_phone
                    )
                    caller_sent = True
                    print("✅ Caller SMS sent")
                except Exception as e:
                    print("[TWILIO CALLER ERROR]", str(e))

        return {
            "status": "processed",
            "event_type": event_type,
            "call_id": call_id,
            "client_id": client_id,
            "business": client["business_name"],
            "user_text": user_text,
            "business_notified": business_sent,
            "caller_notified": caller_sent
        }

    except Exception as e:
        print("[WEBHOOK ERROR]", str(e))
        return {"status": "error", "message": str(e)}