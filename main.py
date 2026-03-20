"""
main.py — FastAPI + webhook Twilio WhatsApp
"""

import json
import logging
from datetime import date, datetime, timedelta

from fastapi import FastAPI, Form, Response
from contextlib import asynccontextmanager

import database as db
import llm
from scheduler import start_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def current_week_start() -> str:
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
    phone = From.replace("whatsapp:", "")
    text  = Body.strip()
    week  = current_week_start()

    log.info("Message reçu de %s : %r", phone, text)

    db.upsert_user(phone)
    user = db.get_user(phone)

    reply = await dispatch(phone, text, week, user)

    if reply:
        send_whatsapp(phone, reply)

    return Response(content="", media_type="text/xml")


# ── Dispatch ───────────────────────────────────────────────────────────────────

async def dispatch(phone: str, text: str, week: str, user) -> str | None:

    # 1. Pas encore de nom — premier contact
    if not user["name"] and not user["awaiting_name"]:
        db.set_awaiting_name(phone, True)
        return "Yo ! C'est quoi déjà ton blaze chef ? 👊"

    # 2. En attente du blaze
    if user["awaiting_name"]:
        return await handle_name_response(phone, text)

    name = user["name"]

    # 3. En attente d'un commentaire de séance
    sessions_awaiting_comment = db.get_sessions_awaiting_comment(phone)
    if sessions_awaiting_comment:
        return await handle_comment_response(text, sessions_awaiting_comment)

    # 4. Des check-ins en attente de réponse ?
    pending = db.get_pending_checkin_sessions(phone, week)
    if pending:
        return await handle_checkin_response(phone, name, text, pending)

    # 5. Pas encore de plan cette semaine ?
    plan = db.get_weekly_plan(phone, week)
    if plan is None:
        return await handle_weekly_plan(phone, name, text, week)

    # 6. Message libre
    return await handle_free_message(phone, name, text, week)


# ── Handlers ───────────────────────────────────────────────────────────────────

async def handle_name_response(phone: str, text: str) -> str:
    name = await llm.extract_name(text)
    if not name:
        return "J'ai pas capté ton blaze là, tu peux redire ?"
    db.set_user_name(phone, name)
    return (
        f"C'est noté {name} 💪 Bienvenue dans l'équipe !\n\n"
        f"Maintenant dis-moi ton programme sportif de la semaine et j'gère tout.\n"
        f"Ex : \"Lundi 7h30 running, mercredi 19h muscu, samedi 10h vélo\""
    )


async def handle_weekly_plan(phone: str, name: str, text: str, week: str) -> str:
    parsed = await llm.parse_weekly_plan(text)

    if not parsed or not parsed.get("sessions"):
        return (
            f"J'ai pas bien compris ton planning {name} 😅\n"
            f"Dis-moi quelque chose comme :\n"
            f"\"Lundi 7h30 running, mercredi 19h muscu, samedi 10h vélo\""
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
        f"C'est bon {name}, j'ai tout noté 💪\n\n"
        f"{lines}\n\n"
        f"Je t'enverrai un message 30 min avant chaque séance, "
        f"et je vérifierai le soir si tu l'as faite. Let's go ! 🔥"
    )


async def handle_checkin_response(
    phone: str, name: str, text: str, pending: list
) -> str:
    result = await llm.parse_checkin_response(text, [dict(s) for s in pending])

    if not result:
        return f"J'ai pas compris {name}, t'as pu t'entraîner aujourd'hui ? 🙂"

    done_sessions   = []
    missed_sessions = []

    for item in result:
        db.mark_session_done(item["session_id"], 1 if item["done"] else -1)
        session = next((s for s in pending if s["id"] == item["session_id"]), None)
        if session:
            if item["done"]:
                done_sessions.append(dict(session))
            else:
                missed_sessions.append(dict(session))

    if done_sessions and missed_sessions:
        done_sports   = [s["sport"] for s in done_sessions]
        missed_sports = [s["sport"] for s in missed_sessions]
        for s in done_sessions:
            db.mark_comment_requested(s["id"])
        msg_done   = await llm.generate_checkin_done_message(name, done_sports)
        msg_missed = await llm.generate_checkin_missed_message(name, missed_sports)
        return f"{msg_done}\n\n---\n{msg_missed}"

    if done_sessions:
        done_sports = [s["sport"] for s in done_sessions]
        for s in done_sessions:
            db.mark_comment_requested(s["id"])
        return await llm.generate_checkin_done_message(name, done_sports)

    # Toutes ratées
    missed_sports = [s["sport"] for s in missed_sessions]
    return await llm.generate_checkin_missed_message(name, missed_sports)


async def handle_comment_response(text: str, sessions: list) -> str:
    for session in sessions:
        parsed_stats = await llm.parse_session_comment(text, session["sport"])
        db.save_session_comment(session["id"], text, parsed_stats)

    sports = ", ".join(s["sport"] for s in sessions)
    return f"C'est noté pour {sports} 👍 On continue !"


async def handle_free_message(phone: str, name: str, text: str, week: str) -> str:
    sessions = db.get_sessions_for_week(phone, week)
    context = json.dumps([dict(s) for s in sessions], ensure_ascii=False, default=str)
    return await llm.handle_free_message(name, text, context)


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
