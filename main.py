from fastapi import FastAPI, Request
from twilio.rest import Client
import os

app = FastAPI()

# -------------------------------------------------
# TWILIO SETUP (SAFE INITIALIZATION)
# -------------------------------------------------
twilio_client = None

TWILIO_PHONE = os.getenv("TWILIO_PHONE")

if os.getenv("TWILIO_SID") and os.getenv("TWILIO_AUTH_TOKEN"):
    twilio_client = Client(
        os.getenv("TWILIO_SID"),
        os.getenv("TWILIO_AUTH_TOKEN")
    )


# -------------------------------------------------
# SIMPLE CLIENT MAP (v1 multi-tenant layer)
# -------------------------------------------------
CLIENTS = {
    "hvac_toronto_001": {
        "business_name": "Toronto HVAC",
        "business_phone": "+14383896310",
        "caller_enabled": True
    },
    "plumbing_001": {
        "business_name": "Best Plumbing",
        "business_phone": "+14160000002",
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
# CORE WEBHOOK (Retell-style event ingestion)
# -------------------------------------------------
@app.post("/webhook/call-summary")
async def call_summary(request: Request):
    try:
        data = await request.json()

        # -------------------------
        # VALIDATION
        # -------------------------
        client_id = data.get("client_id")

        if not client_id:
            return {"status": "error", "message": "client_id required"}

        client = CLIENTS.get(client_id)

        if not client:
            return {"status": "error", "message": "invalid client_id"}

        caller_name = data.get("caller_name", "Unknown")
        caller_phone = data.get("caller_phone")
        summary = data.get("summary", "")
        urgency = data.get("urgency", "normal")

        print(f"[GOSONIC] client={client_id} caller={caller_name}")

        # -------------------------
        # BUSINESS MESSAGE
        # -------------------------
        business_message = f"""
📞 Gosonic Call Alert

Business: {client['business_name']}
Caller: {caller_name}
Phone: {caller_phone}
Summary: {summary}
Urgency: {urgency}
"""

        # Send to business
        if twilio_client:
            twilio_client.messages.create(
                body=business_message,
                from_=TWILIO_PHONE,
                to=client["business_phone"]
            )
        else:
            print("[TWILIO] Business SMS skipped (not configured)")

        # -------------------------
        # CALLER CONFIRMATION MESSAGE
        # -------------------------
        caller_sent = False

        if caller_phone and client["caller_enabled"]:
            caller_message = f"""
Hi {caller_name}, we’ve received your request.
{client['business_name']} will contact you shortly.
"""

            if twilio_client:
                twilio_client.messages.create(
                    body=caller_message,
                    from_=TWILIO_PHONE,
                    to=caller_phone
                )
                caller_sent = True
            else:
                print("[TWILIO] Caller SMS skipped (not configured)")

        # -------------------------
        # RESPONSE
        # -------------------------
        return {
            "status": "processed",
            "client_id": client_id,
            "business": client["business_name"],
            "business_notified": True,
            "caller_notified": caller_sent
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }
