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
# DEDUP STORE (call_id + event safety)
# -------------------------------------------------
PROCESSED_KEYS = set()
PROCESSED_TTL = 60 * 10  # 10 min cleanup window

# simple in-memory timestamp store
PROCESSED_META = {}


# -------------------------------------------------
# HEALTH CHECK
# -------------------------------------------------
@app.get("/")
def root():
    return {"status": "Gosonic MVP running"}


# -------------------------------------------------
# CORE WEBHOOK
# -------------------------------------------------
@app.post("/webhook/call-summary")
async def call_summary(request: Request):

    try:
        data = await request.json()

        print("🔥 RAW PAYLOAD RECEIVED")
        print(json.dumps(data, indent=2))

        event_type = data.get("event") or data.get("type") or "unknown"
        print("📡 EVENT TYPE:", event_type)

        # -------------------------------------------------
        # ONLY PROCESS FINAL CALL EVENT (CRITICAL FIX)
        # -------------------------------------------------
        FINAL_EVENTS = {"call_analyzed", "call_ended", "call_summary"}

        if event_type not in FINAL_EVENTS:
            print("⏭ Ignoring non-final event:", event_type)
            return {"status": "ignored_event", "event": event_type}

        # -------------------------------------------------
        # CALL ID + DEDUP (event + call_id combo)
        # -------------------------------------------------
        call_id = (
            data.get("call", {}).get("call_id")
            or data.get("call_id")
        )

        dedup_key = f"{call_id}:{event_type}"

        now = time.time()

        # cleanup old entries
        for k in list(PROCESSED_META.keys()):
            if now - PROCESSED_META[k] > PROCESSED_TTL:
                PROCESSED_META.pop(k, None)
                PROCESSED_KEYS.discard(k)

        if dedup_key in PROCESSED_KEYS:
            print("⚠️ Duplicate webhook ignored:", dedup_key)
            return {"status": "duplicate_ignored"}

        PROCESSED_KEYS.add(dedup_key)
        PROCESSED_META[dedup_key] = now

        print("🧷 CALL KEY:", dedup_key)

        # -------------------------------------------------
        # TRANSCRIPT EXTRACTION (ROBUST)
        # -------------------------------------------------
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

        # -------------------------------------------------
        # CLIENT ID RESOLUTION
        # -------------------------------------------------
        client_id = (
            data.get("client_id")
            or data.get("call", {}).get("metadata", {}).get("client_id")
            or data.get("call", {}).get("client")
            or "hvac_toronto_001"
        )

        print("🧾 CLIENT ID:", client_id)

        # -------------------------------------------------
        # CALLER INFO
        # -------------------------------------------------
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

        # -------------------------------------------------
        # VALIDATION
        # -------------------------------------------------
        client = CLIENTS.get(client_id)

        if not client:
            print("❌ Invalid client_id:", client_id)
            return {"status": "error", "message": "invalid client_id"}

        print(f"[GOSONIC] client={client_id} caller={caller_name}")

        # -------------------------------------------------
        # BUSINESS MESSAGE (clean formatting)
        # -------------------------------------------------
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
        else:
            print("[TWILIO] Business SMS skipped")

        # -------------------------------------------------
        # CALLER SMS (only if valid phone)
        # -------------------------------------------------
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
            else:
                print("[TWILIO] Caller SMS skipped")

        # -------------------------------------------------
        # RESPONSE
        # -------------------------------------------------
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