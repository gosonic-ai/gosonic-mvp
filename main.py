from fastapi import FastAPI, Request
from twilio.rest import Client
import os
import json

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
# HEALTH CHECK
# -------------------------------------------------
@app.get("/")
def root():
    return {"status": "Gosonic MVP running"}


# -------------------------------------------------
# CORE WEBHOOK (Retell ingestion)
# -------------------------------------------------
@app.post("/webhook/call-summary")
async def call_summary(request: Request):

    try:
        data = await request.json()

        # -------------------------------------------------
        # RAW DEBUG (CRITICAL FOR NOW)
        # -------------------------------------------------
        print("🔥 RAW PAYLOAD RECEIVED")
        print("🔥 RAW WEBHOOK PAYLOAD:\n", json.dumps(data, indent=2))

        # Event detection
        event_type = data.get("event") or data.get("type") or "unknown"
        print("📡 EVENT TYPE:", event_type)

        # -------------------------------------------------
        # EXTRACT TRANSCRIPT MESSAGES (CORE INTENT LAYER)
        # -------------------------------------------------
        messages = (
            data.get("transcript_object")
            or data.get("call", {}).get("transcript_object")
            or []
        )

        user_text = ""
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "user":
                user_text += " " + (m.get("content") or "")

        user_text = user_text.strip()
        print("🧠 USER TEXT:", user_text)

        # -------------------------------------------------
        # SAFE FIELD EXTRACTION
        # -------------------------------------------------
        client_id = data.get("client_id")

        caller_name = data.get("caller_name") or "Unknown"
        caller_phone = data.get("caller_phone")

        summary = data.get("summary") or data.get("call_summary") or user_text
        urgency = data.get("urgency") or "normal"

        # Nested fallback (Retell call object)
        call_obj = data.get("call")
        if isinstance(call_obj, dict):
            caller_phone = caller_phone or call_obj.get("from_number")
            summary = summary or call_obj.get("summary") or user_text

        # -------------------------------------------------
        # VALIDATION
        # -------------------------------------------------
        if not client_id:
            return {"status": "error", "message": "client_id required"}

        client = CLIENTS.get(client_id)

        if not client:
            return {"status": "error", "message": "invalid client_id"}

        print(f"[GOSONIC] client={client_id} caller={caller_name}")

        # -------------------------------------------------
        # BUSINESS MESSAGE
        # -------------------------------------------------
        business_message = (
            "📞 Gosonic Call Alert\n\n"
            f"Business: {client['business_name']}\n"
            f"Caller: {caller_name}\n"
            f"Phone: {caller_phone}\n"
            f"Summary: {summary}\n"
            f"Urgency: {urgency}"
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
                print("[TWILIO BUSINESS ERROR]", str(e))
        else:
            print("[TWILIO] Business SMS skipped")


        # -------------------------------------------------
        # CALLER CONFIRMATION
        # -------------------------------------------------
        caller_sent = False

        if caller_phone and client.get("caller_enabled"):
            caller_message = (
                f"Hi {caller_name}, we’ve received your request.\n"
                f"{client['business_name']} will contact you shortly."
            )

            if twilio_client and TWILIO_PHONE:
                try:
                    twilio_client.messages.create(
                        body=caller_message,
                        from_=TWILIO_PHONE,
                        to=caller_phone
                    )
                    caller_sent = True
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
            "client_id": client_id,
            "business": client["business_name"],
            "user_text": user_text,
            "business_notified": business_sent,
            "caller_notified": caller_sent
        }

    except Exception as e:
        print("[WEBHOOK ERROR]", str(e))
        return {
            "status": "error",
            "message": str(e)
        }