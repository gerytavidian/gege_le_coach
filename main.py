"""
main.py — FastAPI + webhook Twilio WhatsApp
"""

import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

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

    replies = await dispatch(phone, text, week, user)

    if isinstance(replies, list):
        for r in replies:
            send_whatsapp(phone, r)
    elif replies:
        send_whatsapp(phone, replies)

    return Response(content="", media_type="text/xml")


# ── Dispatch ───────────────────────────────────────────────────────────────────

_AIDE_KEYWORDS = {"aide", "help", "sos", "?", "quoi", "comment", "c quoi"}

def _is_aide(text: str) -> bool:
    return text.lower().strip() in _AIDE_KEYWORDS


_AIDE_MESSAGE = (
    "Voilà comment je fonctionne 👋\n\n"
    "*Mon rôle :* Je suis ton coach perso. Chaque semaine tu me dis ton planning sport, "
    "je t'envoie un rappel 30 min avant chaque séance, et le soir je vérifie si tu l'as faite.\n\n"
    "*Comment me donner ton planning :*\n"
    "Envoie-moi tes séances en un message, ex :\n"
    "\"Lundi 7h30 running, mercredi 19h muscu, samedi 10h vélo\"\n\n"
    "*Ce que je fais automatiquement :*\n"
    "• Reminder 30 min avant chaque séance\n"
    "• Check-in le soir pour valider si c'est fait\n"
    "• Bilan hebdo le dimanche soir\n"
    "• Bilan mensuel en fin de mois\n\n"
    "*Commandes :*\n"
    "• *aide* — affiche ce guide\n"
    "• *pause* — coupe les rappels (vacances, blessure)\n"
    "• *reprendre* — relance les rappels\n\n"
    "Pour tout le reste, parle-moi de ton sport ! 💪"
)


def _is_pause(text: str) -> bool:
    t = text.lower().strip()
    return t in {"pause", "en pause", "stop reminders", "vacances", "blessure"}


def _is_reprendre(text: str) -> bool:
    t = text.lower().strip()
    return t in {"reprendre", "retour", "je reprends", "relance", "j'y retourne", "c'est reparti"}


async def dispatch(phone: str, text: str, week: str, user) -> list[str] | str | None:

    # 0. Commandes toujours disponibles : aide, pause, reprendre
    if _is_aide(text):
        return _AIDE_MESSAGE

    if _is_reprendre(text):
        db.set_user_paused(phone, False)
        name = user["name"] or "chef"
        return (
            f"C'est reparti {name} 💪 Les rappels et check-ins sont de nouveau actifs. "
            f"Dis-moi ton planning sport pour la semaine !"
        )

    if _is_pause(text):
        db.set_user_paused(phone, True)
        name = user["name"] or "chef"
        return (
            f"Ok {name}, je mets tout en pause 🛑 Plus de rappels ni de check-ins jusqu'à ce que tu m'envoies *reprendre*."
        )

    # Si l'utilisateur est en pause, on l'informe
    if user.get("paused"):
        name = user["name"] or "chef"
        return f"T'es en pause {name} 🔕 Envoie *reprendre* quand tu veux relancer."

    # 1. Pas encore de nom — premier contact
    if not user["name"] and not user["awaiting_name"]:
        db.set_awaiting_name(phone, True)
        return "Salut moi c'est Gege le coach, donnes moi ton blaze à toi ?"

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

    # 5. En attente de détails sur le planning (sport ou heure manquant) ?
    awaiting_details = db.get_awaiting_plan_details(phone)
    if awaiting_details:
        return await handle_plan_detail_response(phone, name, text, week, awaiting_details)

    # 6. Pas encore de plan cette semaine ?
    plan = db.get_weekly_plan(phone, week)
    if plan is None:
        return await handle_weekly_plan(phone, name, text, week)

    # 7. Message libre
    return await handle_free_message(phone, name, text, week)


# ── Handlers ───────────────────────────────────────────────────────────────────

async def handle_name_response(phone: str, text: str) -> list[str] | str:
    name = await llm.extract_name(text)
    if not name:
        return "J'ai pas capté ton blaze là, tu peux redire ?"
    db.set_user_name(phone, name)

    now = datetime.now(ZoneInfo("Europe/Paris"))
    days_fr = {
        "Monday": "lundi", "Tuesday": "mardi", "Wednesday": "mercredi",
        "Thursday": "jeudi", "Friday": "vendredi", "Saturday": "samedi", "Sunday": "dimanche",
    }
    day_fr = days_fr[now.strftime("%A")]
    time_str = now.strftime("%Hh%M")

    return [
        f"Moi c'est Gege ton coach 💪 J'suis là pour surveiller et tracker si tu tiens tes engagements de sport chaque semaine. "
        f"Chaque semaine tu devras me répondre si oui ou non tu l'as bien fait, avec un ptit commentaire pour l'histoire.",
        f"On va commencer : cette semaine on est {day_fr} et il est {time_str}. "
        f"Quand compte tu faire du sport avant dimanche soir {name} ?",
    ]


async def handle_weekly_plan(phone: str, name: str, text: str, week: str) -> str:
    parsed = await llm.parse_weekly_plan(text)

    if not parsed or not parsed.get("sessions"):
        return (
            f"J'ai pas bien compris ton planning {name} 😅\n"
            f"Dis-moi quelque chose comme :\n"
            f"\"Lundi 7h30 running, mercredi 19h muscu, samedi 10h vélo\""
        )

    question = _missing_question(parsed["sessions"])
    if question:
        db.set_awaiting_plan_details(phone, json.dumps({"original": text}))
        return question

    return _add_sessions_and_ask_more(phone, name, week, parsed, first=True)


async def handle_plan_detail_response(
    phone: str, name: str, text: str, week: str, awaiting_json: str
) -> str:
    awaiting = json.loads(awaiting_json)

    # Mode : on attend une réponse "oui/non" à "t'as une autre séance ?"
    if awaiting.get("mode") == "awaiting_more_sessions":
        if _is_no(text):
            db.set_awaiting_plan_details(phone, None)
            return _final_recap(phone, name, week)
        # L'utilisateur décrit une nouvelle séance (ou dit juste "oui")
        if _is_yes(text):
            return "C'est quoi ?"
        # Il décrit directement la séance
        parsed = await llm.parse_weekly_plan(text)
        if not parsed or not parsed.get("sessions"):
            return "C'est quoi comme sport, quel jour et à quelle heure ?"
        question = _missing_question(parsed["sessions"])
        if question:
            db.set_awaiting_plan_details(phone, json.dumps({"original": text}))
            return question
        return _add_sessions_and_ask_more(phone, name, week, parsed)

    # Mode : on complète un message incomplet (sport ou heure manquant)
    combined = awaiting["original"] + " " + text
    parsed = await llm.parse_weekly_plan(combined)

    if not parsed or not parsed.get("sessions"):
        db.set_awaiting_plan_details(phone, None)
        return (
            f"J'ai pas compris {name}, dis-moi tout en un message :\n"
            f"Ex : \"samedi 11h running\""
        )

    question = _missing_question(parsed["sessions"])
    if question:
        db.set_awaiting_plan_details(phone, json.dumps({"original": combined}))
        return question

    db.set_awaiting_plan_details(phone, None)
    return _add_sessions_and_ask_more(phone, name, week, parsed)


def _missing_question(sessions: list) -> str | None:
    days_fr = {
        "monday": "lundi", "tuesday": "mardi", "wednesday": "mercredi",
        "thursday": "jeudi", "friday": "vendredi", "saturday": "samedi", "sunday": "dimanche",
    }
    missing_time  = [s for s in sessions if not s.get("time")]
    missing_sport = [s for s in sessions if not s.get("sport")]

    if missing_sport and missing_time:
        days = ", ".join(days_fr.get(s.get("day", ""), s.get("day", "")) for s in missing_sport)
        return f"C'est quel sport et à quelle heure {days} ?"
    if missing_sport:
        parts = [f"{days_fr.get(s.get('day',''), s.get('day',''))} à {s['time']}" for s in missing_sport]
        return f"C'est quel sport — {', '.join(parts)} ?"
    if missing_time:
        days = ", ".join(days_fr.get(s.get("day", ""), s.get("day", "")) for s in missing_time)
        return f"À quelle heure {days} ?"
    return None


def _add_sessions_and_ask_more(
    phone: str, name: str, week: str, parsed: dict, first: bool = False
) -> str:
    raw_json = json.dumps(parsed, ensure_ascii=False)
    if first:
        db.upsert_weekly_plan(phone, week, "", raw_json)
        db.insert_sessions(phone, week, parsed["sessions"])
    else:
        db.append_sessions(phone, week, parsed["sessions"])

    lines = "\n".join(
        f"  • {s['sport'].capitalize()} — {_day_fr(s['day'])} à {s['time']}"
        for s in parsed["sessions"]
    )
    db.set_awaiting_plan_details(phone, json.dumps({"mode": "awaiting_more_sessions"}))
    return f"Noté 👊 {lines}\n\nT'as une autre séance de prévue cette semaine ?"


def _final_recap(phone: str, name: str, week: str) -> str:
    sessions = db.get_sessions_for_week(phone, week)
    lines = "\n".join(
        f"  • {s['sport'].capitalize()} — {_day_fr(s['planned_day'])} à {s['planned_time']}"
        for s in sessions
    )
    return (
        f"C'est bon {name}, j'ai tout noté 💪\n\n"
        f"{lines}\n\n"
        f"Je t'enverrai un message 30 min avant chaque séance, "
        f"et je vérifierai le soir si tu l'as faite. Let's go ! 🔥"
    )


def _is_no(text: str) -> bool:
    t = text.lower().strip()
    return t in {"non", "no", "nope", "nan", "nah", "c'est bon", "c bon", "ça suffit", "pas d'autre", "rien d'autre"} \
        or t.startswith("non") or t.startswith("c'est tout")


def _is_yes(text: str) -> bool:
    t = text.lower().strip()
    return t in {"oui", "yes", "ouais", "yep", "ouep", "yop", "ok", "oké"}


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
