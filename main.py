"""
main.py — FastAPI + webhook Twilio WhatsApp
"""

import json
import logging
from datetime import date, datetime, timedelta

from fastapi import FastAPI, Form, Response
from fastapi.responses import PlainTextResponse
from contextlib import asynccontextmanager

import database as db
import llm
from scheduler import start_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Numéro unique pour l'instant ───────────────────────────────────────────────
# Si plusieurs users à terme, on le tire du webhook.
SINGLE_USER_PHONE = None   # sera rempli au premier message reçu


def current_week_start() -> str:
    """Retourne le lundi de la semaine courante au format ISO (YYYY-MM-DD)."""
    today = date.today()
    return (today - timedelta(days=today.weekday())).isoformat()


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    start_scheduler()
    log.info("Bot démarré — BDD et scheduler initialisés.")
    yield


app = FastAPI(lifespan=lifespan)


# ── Envoi de message sortant ───────────────────────────────────────────────────

def send_whatsapp(to: str, body: str):
    """Envoie un message WhatsApp via Twilio REST."""
    import os
    from twilio.rest import Client

    client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    from_number = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")

    message = client.messages.create(
        from_=from_number,
        to=f"whatsapp:{to}" if not to.startswith("whatsapp:") else to,
        body=body,
    )
    log.info("Message envoyé → %s | sid=%s", to, message.sid)
    return message.sid


# ── Webhook Twilio ─────────────────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(
    From: str = Form(...),
    Body: str = Form(...),
):
    """
    Twilio appelle ce endpoint à chaque message entrant.
    From  : 'whatsapp:+33XXXXXXXXX'
    Body  : texte du message
    """
    global SINGLE_USER_PHONE

    phone = From.replace("whatsapp:", "")
    text  = Body.strip()
    week  = current_week_start()

    log.info("Message reçu de %s : %r", phone, text)

    # Enregistre l'utilisateur si nouveau
    db.upsert_user(phone)
    SINGLE_USER_PHONE = phone

    # Détermine le contexte : est-ce une réponse au plan hebdo ?
    plan = db.get_weekly_plan(phone, week)
    pending = db.get_pending_checkin_sessions(phone, week)

    reply = await dispatch(phone, text, week, plan, pending)

    if reply:
        send_whatsapp(phone, reply)

    # Twilio attend un 200 vide (on a déjà envoyé via REST)
    return Response(content="", media_type="text/xml")


# ── Dispatch ───────────────────────────────────────────────────────────────────

async def dispatch(
    phone: str,
    text: str,
    week: str,
    plan,
    pending_checkins: list,
) -> str | None:
    """
    Routing conversationnel minimaliste basé sur le contexte DB.
    Retourne le texte de réponse, ou None si pas de réponse immédiate.
    """

    # 1. Des check-ins en attente de réponse ? → traiter comme réponse de bilan
    if pending_checkins:
        return await handle_checkin_response(phone, text, week, pending_checkins)

    # 2. Pas encore de plan cette semaine ? → traiter comme planning
    if plan is None:
        return await handle_weekly_plan(phone, text, week)

    # 3. Plan déjà enregistré : message libre / question
    return await handle_free_message(phone, text, week)


# ── Handlers ───────────────────────────────────────────────────────────────────

async def handle_weekly_plan(phone: str, text: str, week: str) -> str:
    """L'utilisateur envoie son planning de la semaine."""
    parsed = await llm.parse_weekly_plan(text)

    if not parsed or not parsed.get("sessions"):
        return (
            "Je n'ai pas bien compris ton planning 😅\n"
            "Dis-moi quelque chose comme :\n"
            "\"Lundi 7h30 running, mercredi 19h muscu, samedi 10h vélo\""
        )

    raw_json = json.dumps(parsed, ensure_ascii=False)
    db.upsert_weekly_plan(phone, week, text, raw_json)
    db.insert_sessions(phone, week, parsed["sessions"])

    sessions = parsed["sessions"]
    lines = "\n".join(
        f"  • {s['sport'].capitalize()} — {_day_fr(s['day'])} à {s['time']}"
        for s in sessions
    )
    return (
        f"Super ! J'ai noté ton programme pour cette semaine 💪\n\n"
        f"{lines}\n\n"
        f"Je t'enverrai un message d'encouragement 30 min avant chaque séance, "
        f"et je vérifierai le soir si tu l'as faite. Let's go ! 🔥"
    )


async def handle_checkin_response(
    phone: str, text: str, week: str, pending: list
) -> str:
    """L'utilisateur répond au bilan du soir."""
    result = await llm.parse_checkin_response(text, [dict(s) for s in pending])

    if not result:
        return "Je n'ai pas compris ta réponse. Tu as pu t'entraîner aujourd'hui ? 🙂"

    updated = []
    for item in result:
        db.mark_session_done(item["session_id"], 1 if item["done"] else -1)
        updated.append(item)

    done_count = sum(1 for i in updated if i["done"])
    missed_count = len(updated) - done_count

    if missed_count == 0:
        return "Excellent ! Toutes tes séances cochées ✅ Continue comme ça !"
    elif done_count == 0:
        return "Pas de souci, ça arrive 💙 L'important c'est de repartir demain !"
    else:
        return f"{done_count} séance(s) faite(s) ✅, {missed_count} manquée(s). Bien essayé, on continue !"


async def handle_free_message(phone: str, text: str, week: str) -> str:
    """Message hors contexte : question libre, modification de planning, etc."""
    sessions = db.get_sessions_for_week(phone, week)
    context = json.dumps([dict(s) for s in sessions], ensure_ascii=False, default=str)
    return await llm.handle_free_message(text, context)


# ── Helpers ────────────────────────────────────────────────────────────────────

_DAYS_FR = {
    "monday": "Lundi", "tuesday": "Mardi", "wednesday": "Mercredi",
    "thursday": "Jeudi", "friday": "Vendredi", "saturday": "Samedi", "sunday": "Dimanche",
}

def _day_fr(day: str) -> str:
    return _DAYS_FR.get(day.lower(), day)


# ── Point d'entrée local ───────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
