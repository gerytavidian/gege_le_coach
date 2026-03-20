"""
scheduler.py — Tâches planifiées avec APScheduler
"""

import json
import logging
import os
from datetime import date, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import database as db
import llm

log = logging.getLogger(__name__)

TIMEZONE = "Europe/Paris"

scheduler = AsyncIOScheduler(timezone=TIMEZONE)


def _send(phone: str, body: str):
    """Import tardif pour éviter la dépendance circulaire avec main.py."""
    from main import send_whatsapp
    send_whatsapp(phone, body)


def current_week_start() -> str:
    today = date.today()
    return (today - timedelta(days=today.weekday())).isoformat()


def next_week_start() -> str:
    today = date.today()
    days_until_next_monday = (7 - today.weekday()) % 7 or 7
    return (today + timedelta(days=days_until_next_monday)).isoformat()


# ── Tâche 1 : Dimanche 22h — rapport hebdo ────────────────────────────────────

async def send_weekly_report():
    week  = current_week_start()
    users = _get_all_users()
    for user in users:
        phone = user["phone"]
        name  = user["name"] or "chef"
        sessions = db.get_sessions_for_week(phone, week)
        if not sessions:
            continue
        report = await llm.generate_weekly_report(name, [dict(s) for s in sessions])
        _send(phone, f"📊 *Bilan de ta semaine* :\n\n{report}")
        log.info("Rapport hebdo envoyé → %s", phone)


# ── Tâche 2 : Dimanche 22h10 — demande le planning de la semaine suivante ─────

async def ask_weekly_plan():
    next_week = next_week_start()
    users = _get_all_users()
    for user in users:
        phone = user["phone"]
        name  = user["name"] or "chef"
        plan = db.get_weekly_plan(phone, next_week)
        if plan is None:
            _send(
                phone,
                f"Eh {name}, t'es prêt pour la semaine prochaine ? 💪\n\n"
                "Dis-moi quels sports tu prévois et à quelle heure.\n"
                "Ex : \"Lundi 7h30 running, mercredi 19h muscu, samedi 10h vélo\"",
            )
            log.info("Demande planning semaine suivante envoyée → %s", phone)


# ── Tâche 3 : Toutes les minutes — reminders 30 min avant ────────────────────

async def send_reminders():
    now    = datetime.now()
    week   = current_week_start()
    day    = now.strftime("%A").lower()
    target = now + timedelta(minutes=30)

    sessions = db.get_sessions_for_day(day, week)
    for s in sessions:
        s = dict(s)
        planned_dt = datetime.strptime(
            f"{date.today().isoformat()} {s['planned_time']}", "%Y-%m-%d %H:%M"
        )
        diff = abs((planned_dt - target).total_seconds())
        if diff <= 60 and not s["reminder_sent"]:
            user = db.get_user(s["phone"])
            name = user["name"] if user and user["name"] else "chef"
            msg = await llm.generate_encouragement(name, s["sport"], s["planned_time"])
            _send(s["phone"], msg)
            db.mark_reminder_sent(s["id"])
            log.info("Reminder envoyé → %s pour %s", s["phone"], s["sport"])


# ── Tâche 4 : 21h chaque soir — bilan du jour ────────────────────────────────

async def send_evening_checkin():
    week = current_week_start()
    day  = datetime.now().strftime("%A").lower()

    # Pas de check-in le dimanche : rapport hebdo à la place
    if day == "sunday":
        return

    sessions = db.get_sessions_for_day(day, week)
    by_user: dict[str, list] = {}
    for s in sessions:
        s = dict(s)
        if not s["checkin_sent"]:
            by_user.setdefault(s["phone"], []).append(s)

    for phone, user_sessions in by_user.items():
        user = db.get_user(phone)
        name = user["name"] if user and user["name"] else "chef"
        msg = await llm.generate_checkin_message(name, user_sessions)
        _send(phone, msg)
        for s in user_sessions:
            db.mark_checkin_sent(s["id"])
        log.info("Check-in envoyé → %s (%d séances)", phone, len(user_sessions))


# ── Tâche 5 : Dernier jour du mois 22h30 — rapport mensuel ───────────────────

async def send_monthly_report():
    now   = datetime.now()
    year  = now.year
    month = now.month

    # Label lisible : "mars 2026"
    months_fr = [
        "", "janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre",
    ]
    month_label = f"{months_fr[month]} {year}"

    users = _get_all_users()
    for user in users:
        phone = user["phone"]
        name  = user["name"] or "chef"
        sessions = db.get_sessions_for_month(phone, year, month)
        report = await llm.generate_monthly_report(name, [dict(s) for s in sessions], month_label)
        _send(phone, f"📅 *Bilan du mois de {month_label}* :\n\n{report}")
        log.info("Rapport mensuel envoyé → %s (%s)", phone, month_label)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_all_users() -> list[dict]:
    with db.get_db() as cur:
        cur.execute("SELECT phone, name FROM users")
        rows = cur.fetchall()
    return [dict(r) for r in rows]


# ── Initialisation ─────────────────────────────────────────────────────────────

def start_scheduler():
    # Dimanche 22h00 : rapport hebdo
    scheduler.add_job(
        send_weekly_report,
        CronTrigger(day_of_week="sun", hour=22, minute=0, timezone=TIMEZONE),
        id="weekly_report",
    )

    # Dimanche 22h10 : demande le planning de la semaine prochaine
    scheduler.add_job(
        ask_weekly_plan,
        CronTrigger(day_of_week="sun", hour=22, minute=10, timezone=TIMEZONE),
        id="ask_weekly_plan",
    )

    # Toutes les minutes : vérification des reminders 30 min avant
    scheduler.add_job(
        send_reminders,
        CronTrigger(minute="*", timezone=TIMEZONE),
        id="reminders",
    )

    # Tous les soirs à 21h (sauf dimanche) : check-in du soir
    scheduler.add_job(
        send_evening_checkin,
        CronTrigger(hour=21, minute=0, timezone=TIMEZONE),
        id="evening_checkin",
    )

    # Dernier jour du mois à 22h30 : rapport mensuel
    scheduler.add_job(
        send_monthly_report,
        CronTrigger(day="last", hour=22, minute=30, timezone=TIMEZONE),
        id="monthly_report",
    )

    scheduler.start()
    log.info(
        "Scheduler démarré : %s",
        ", ".join(j.id for j in scheduler.get_jobs()),
    )
