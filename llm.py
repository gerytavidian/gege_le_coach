"""
llm.py — Appels à Google Gemini 1.5 Flash
"""

import json
import logging
import os
import re
from datetime import date, timedelta

import google.generativeai as genai

log = logging.getLogger(__name__)

_model = None


def _get_model():
    global _model
    if _model is None:
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        _model = genai.GenerativeModel("gemini-1.5-flash")
    return _model


def _extract_json(text: str) -> dict | list | None:
    """Extrait le premier bloc JSON valide d'une réponse Gemini."""
    # Gemini entoure parfois le JSON de ```json ... ```
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if match:
        text = match.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("JSON invalide reçu de Gemini : %r", text[:300])
        return None


async def _generate(prompt: str) -> str:
    model = _get_model()
    response = model.generate_content(prompt)
    return response.text.strip()


# ── Parsing du planning hebdo ──────────────────────────────────────────────────

async def parse_weekly_plan(user_message: str) -> dict | None:
    """
    Extrait les sessions sportives d'un message libre.
    Retourne : {"sessions": [{"sport": str, "day": str, "time": "HH:MM"}, ...]}
    Les jours sont en anglais minuscule : monday, tuesday, ..., sunday.
    """
    today = date.today()
    week_start = today - timedelta(days=today.weekday())

    prompt = f"""Tu es un assistant coach sportif. L'utilisateur t'envoie son programme sportif de la semaine en français.

Message de l'utilisateur : "{user_message}"

Date du lundi de cette semaine : {week_start.isoformat()}
Aujourd'hui : {today.strftime('%A %d %B %Y')} (en français)

Extrais toutes les séances sportives mentionnées et retourne UNIQUEMENT un JSON valide, sans texte autour, au format :
{{
  "sessions": [
    {{"sport": "running", "day": "monday", "time": "07:30"}},
    {{"sport": "muscu", "day": "wednesday", "time": "19:00"}}
  ]
}}

Règles :
- "day" doit être en anglais minuscule (monday, tuesday, wednesday, thursday, friday, saturday, sunday)
- "time" au format HH:MM (24h)
- Si l'heure n'est pas précisée, mets "08:00" par défaut
- Si le message ne contient aucune séance sportive, retourne {{"sessions": []}}
- Ne retourne que le JSON, rien d'autre
"""
    raw = await _generate(prompt)
    return _extract_json(raw)


# ── Parsing de la réponse au bilan du soir ────────────────────────────────────

async def parse_checkin_response(user_message: str, pending_sessions: list[dict]) -> list[dict] | None:
    """
    Interprète la réponse de l'utilisateur au bilan du soir.
    pending_sessions : liste de dicts avec au moins {id, sport, planned_time}
    Retourne : [{"session_id": int, "done": bool}, ...]
    """
    sessions_desc = "\n".join(
        f"- id={s['id']}, sport={s['sport']}, heure prévue={s['planned_time']}"
        for s in pending_sessions
    )

    prompt = f"""Tu es un assistant coach sportif. L'utilisateur répond au bilan de ses séances du jour.

Séances en attente de confirmation :
{sessions_desc}

Réponse de l'utilisateur : "{user_message}"

Pour chaque séance, détermine si elle a été faite ou non d'après la réponse.
Retourne UNIQUEMENT un JSON valide au format :
[
  {{"session_id": 1, "done": true}},
  {{"session_id": 2, "done": false}}
]

Si la réponse est ambiguë pour une séance, suppose qu'elle n'a pas été faite (done: false).
Ne retourne que le JSON, rien d'autre.
"""
    raw = await _generate(prompt)
    result = _extract_json(raw)
    if isinstance(result, list):
        return result
    return None


# ── Message d'encouragement pré-séance ────────────────────────────────────────

async def generate_encouragement(sport: str, planned_time: str) -> str:
    prompt = f"""Tu es un coach sportif bienveillant et motivant.
Génère un message d'encouragement court (2-3 phrases max) en français pour quelqu'un qui va faire du {sport} dans 30 minutes (à {planned_time}).
Le message doit être chaleureux, dynamique, avec 1-2 emojis. Pas de salutation formelle.
"""
    return await _generate(prompt)


# ── Message de bilan du soir ──────────────────────────────────────────────────

async def generate_checkin_message(sessions: list[dict]) -> str:
    sports = ", ".join(s["sport"] for s in sessions)
    prompt = f"""Tu es un coach sportif bienveillant.
Génère une question courte et sympathique (1-2 phrases) en français pour demander à l'utilisateur s'il a fait ses séances du jour : {sports}.
Utilise 1 emoji. Sois décontracté(e).
"""
    return await _generate(prompt)


# ── Rapport hebdomadaire ──────────────────────────────────────────────────────

async def generate_weekly_report(sessions: list[dict]) -> str:
    done    = [s for s in sessions if s["done"] == 1]
    missed  = [s for s in sessions if s["done"] == -1]
    unknown = [s for s in sessions if s["done"] == 0]
    total   = len(sessions)
    done_n  = len(done)

    # Calcul du streak (jours consécutifs avec au moins 1 séance faite)
    days_with_done = set(s["planned_day"] for s in done)
    day_order = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    streak = 0
    for d in reversed(day_order):
        if d in days_with_done:
            streak += 1
        else:
            break

    done_list   = ", ".join(s["sport"] for s in done)   or "aucune"
    missed_list = ", ".join(s["sport"] for s in missed) or "aucune"

    prompt = f"""Tu es un coach sportif bienveillant et motivant.
Génère un rapport hebdomadaire en français pour un utilisateur.

Données de la semaine :
- Séances prévues : {total}
- Séances faites : {done_n} ({done_list})
- Séances manquées : {len(missed)} ({missed_list})
- Séances non confirmées : {len(unknown)}
- Streak de jours actifs en fin de semaine : {streak}

Le rapport doit :
1. Féliciter ou encourager selon les résultats (2-3 phrases)
2. Donner 1 conseil personnalisé court
3. Terminer par une phrase motivante pour la semaine prochaine
4. Utiliser 2-3 emojis au total
5. Être concis (6-8 lignes max)
"""
    return await _generate(prompt)


# ── Rapport mensuel ───────────────────────────────────────────────────────────

def _compute_monthly_stats(sessions: list[dict]) -> dict:
    """Calcule les stats agrégées pour un ensemble de sessions."""
    done    = [s for s in sessions if s["done"] == 1]
    missed  = [s for s in sessions if s["done"] == -1]
    total   = len(sessions)
    done_n  = len(done)

    # Sports pratiqués et leur fréquence
    sport_count: dict[str, int] = {}
    for s in done:
        sport_count[s["sport"]] = sport_count.get(s["sport"], 0) + 1
    top_sport = max(sport_count, key=sport_count.get) if sport_count else None

    # Semaines actives (au moins 1 séance faite)
    weeks_active = len({s["week_start"] for s in done})
    weeks_total  = len({s["week_start"] for s in sessions})

    # Taux de complétion
    rate = round(done_n / total * 100) if total else 0

    return {
        "total": total,
        "done": done_n,
        "missed": len(missed),
        "rate": rate,
        "top_sport": top_sport,
        "sport_count": sport_count,
        "weeks_active": weeks_active,
        "weeks_total": weeks_total,
    }


async def generate_monthly_report(sessions: list[dict], month_label: str) -> str:
    """
    month_label : ex. "mars 2026"
    """
    if not sessions:
        return f"Pas de données pour {month_label} — on commence fort le mois prochain ! 💪"

    stats = _compute_monthly_stats(sessions)

    sport_lines = "\n".join(
        f"  - {sport} : {count} séance(s)"
        for sport, count in sorted(stats["sport_count"].items(), key=lambda x: -x[1])
    ) or "  - aucune"

    prompt = f"""Tu es un coach sportif bienveillant et motivant.
Génère un rapport mensuel détaillé en français pour le mois de {month_label}.

Statistiques du mois :
- Séances prévues au total : {stats['total']}
- Séances réalisées : {stats['done']} ({stats['rate']}%)
- Séances manquées : {stats['missed']}
- Semaines avec au moins 1 séance faite : {stats['weeks_active']} / {stats['weeks_total']}
- Répartition par sport :
{sport_lines}
- Sport le plus pratiqué : {stats['top_sport'] or 'N/A'}

Le rapport mensuel doit :
1. Commencer par un bilan chiffré clair (2 phrases)
2. Souligner le point fort du mois (régularité, sport favori, etc.)
3. Identifier 1 axe d'amélioration concret pour le mois suivant
4. Terminer par un défi ou objectif motivant pour le mois prochain
5. Utiliser 3-4 emojis bien placés
6. Rester lisible sur WhatsApp (sauts de ligne, pas de markdown complexe)
7. Maximum 12 lignes
"""
    return await _generate(prompt)


# ── Message libre ─────────────────────────────────────────────────────────────

async def handle_free_message(user_message: str, sessions_context: str) -> str:
    prompt = f"""Tu es Géré, un coach sportif virtuel bienveillant et motivant sur WhatsApp.
Tu réponds en français, de manière courte et sympathique (3-4 phrases max).
Tu peux utiliser 1-2 emojis.

Contexte – séances de l'utilisateur cette semaine (JSON) :
{sessions_context}

Message de l'utilisateur : "{user_message}"

Réponds directement sans te présenter à nouveau.
"""
    return await _generate(prompt)
