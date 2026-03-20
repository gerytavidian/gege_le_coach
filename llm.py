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


# ── Extraction du blaze ────────────────────────────────────────────────────────

async def extract_name(user_message: str) -> str | None:
    """
    Extrait le blaze/prénom de l'utilisateur depuis un message libre.
    Ex: "mon blaze c'est Greg la terreur" → "Greg la terreur"
    Ex: "Greg" → "Greg"
    """
    prompt = f"""L'utilisateur répond à la question "c'est quoi ton blaze ?".
Message : "{user_message}"

Extrait uniquement le blaze ou prénom mentionné. Retourne UNIQUEMENT le blaze, sans guillemets, sans texte autour.
Si tu ne trouves pas de blaze clair, retourne le message tel quel (sans les mots parasites comme "mon blaze c'est", "je m'appelle", etc.)
Ne retourne que le blaze, rien d'autre."""
    raw = await _generate(prompt)
    name = raw.strip().strip('"').strip("'")
    return name if name else None


# ── Parsing du planning hebdo ──────────────────────────────────────────────────

async def parse_weekly_plan(user_message: str) -> dict | None:
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


# ── Réponse check-in : séance faite ───────────────────────────────────────────

async def generate_checkin_done_message(name: str, sports: list[str]) -> str:
    sports_str = ", ".join(sports)
    prompt = f"""Tu es un pote coach sportif qui parle un français relâché, familier, avec des fautes légères et un ton de quartier bienveillant.
L'utilisateur {name} vient de confirmer qu'il a fait ses séances : {sports_str}.

Génère un message court (2-3 phrases max) qui :
1. Le félicite avec l'esprit de "t'es trop chaud, tu mourriras vieux" (varie la formulation, garde l'énergie)
2. Lui demande s'il a des ptits commentaires sur sa séance ou des stats (temps, distance, poids, etc.)

Utilise son prénom {name}. Ton familier, 1-2 emojis max. Varie les formulations à chaque fois.
"""
    return await _generate(prompt)


# ── Réponse check-in : séance ratée ───────────────────────────────────────────

async def generate_checkin_missed_message(name: str, sports: list[str]) -> str:
    sports_str = ", ".join(sports)
    prompt = f"""Tu es un pote coach sportif qui parle un français relâché, familier, avec des fautes légères et un ton de quartier bienveillant.
L'utilisateur {name} n'a pas fait ses séances : {sports_str}.

Génère un message en 2 parties :
1. Une phrase qui pioche ALÉATOIREMENT parmi ces trois registres (varie à chaque fois, n'utilise pas toujours le même) :
   - Déception du coach : genre "tu déçois ton coach là mon gars", taquin et bienveillant
   - C'est grave : genre "non mais sérieux c'est pas possible", un peu dramatique mais sympa
   - Tant que tu fais la prochaine : genre "ok c'est raté mais la prochaine tu assures hein", encourageant
   Utilise le prénom {name}. Ton familier, naturel.
2. Une stat choc et vraie sur l'obésité ou la sédentarité en France ou dans le monde (différente à chaque fois, courte, percutante). Commence par "Stat du jour : ".

1 emoji max au total. Ne commence pas tous les messages pareil.
"""
    return await _generate(prompt)


# ── Parsing du commentaire de séance ──────────────────────────────────────────

async def parse_session_comment(user_message: str, sport: str) -> str | None:
    """
    Tente d'extraire des stats structurées du commentaire.
    Retourne un JSON string si des stats sont trouvées, None sinon.
    """
    prompt = f"""L'utilisateur commente sa séance de {sport}.
Message : "{user_message}"

Tente d'extraire des statistiques sportives (distance, temps, vitesse, poids, répétitions, etc.).
Si tu trouves des stats, retourne un JSON compact. Exemples :
- {{"distance_km": 10, "temps_min": 55}}
- {{"poids_kg": 80, "series": 4, "reps": 12}}
- {{"temps_min": 45}}

Si le message est vague, "RAS", "rien", "non", "pas grand chose" ou ne contient pas de stats mesurables, retourne exactement : null

Ne retourne que le JSON ou null, rien d'autre.
"""
    raw = await _generate(prompt)
    raw = raw.strip()
    if raw.lower() == "null" or not raw:
        return None
    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        return None


# ── Message d'encouragement pré-séance ────────────────────────────────────────

async def generate_encouragement(name: str, sport: str, planned_time: str) -> str:
    prompt = f"""Tu es un pote coach sportif qui parle un français relâché et familier.
Génère un message d'encouragement court (2-3 phrases max) en français pour {name} qui va faire du {sport} dans 30 minutes (à {planned_time}).
Ton familier et dynamique, 1-2 emojis. Mentionne son prénom.
"""
    return await _generate(prompt)


# ── Message de bilan du soir ──────────────────────────────────────────────────

async def generate_checkin_message(name: str, sessions: list[dict]) -> str:
    sports = ", ".join(s["sport"] for s in sessions)
    prompt = f"""Tu es un pote coach sportif qui parle un français relâché et familier.
Génère une question courte et sympa (1-2 phrases) pour demander à {name} s'il a fait ses séances du jour : {sports}.
Mentionne son prénom. Ton décontracté, 1 emoji.
"""
    return await _generate(prompt)


# ── Rapport hebdomadaire ──────────────────────────────────────────────────────

async def generate_weekly_report(name: str, sessions: list[dict]) -> str:
    done    = [s for s in sessions if s["done"] == 1]
    missed  = [s for s in sessions if s["done"] == -1]
    unknown = [s for s in sessions if s["done"] == 0]
    total   = len(sessions)
    done_n  = len(done)

    day_order = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    days_with_done = set(s["planned_day"] for s in done)
    streak = 0
    for d in reversed(day_order):
        if d in days_with_done:
            streak += 1
        else:
            break

    done_list   = ", ".join(s["sport"] for s in done)   or "aucune"
    missed_list = ", ".join(s["sport"] for s in missed) or "aucune"

    # Résumé des commentaires
    comments_with_data = [s for s in done if s.get("comment") and s["comment"].lower() not in ("ras", "rien", "non", "non ras")]
    no_comment_count   = len([s for s in done if not s.get("comment") or s["comment"].lower() in ("ras", "rien", "non")])

    comments_lines = ""
    if comments_with_data:
        lines = []
        for s in comments_with_data:
            stats = f" (stats: {s['parsed_stats']})" if s.get("parsed_stats") else ""
            lines.append(f"  - {s['sport']}: {s['comment']}{stats}")
        comments_lines = "\nCommentaires des séances :\n" + "\n".join(lines)
    if no_comment_count:
        comments_lines += f"\n  - {no_comment_count} séance(s) sans commentaire"

    prompt = f"""Tu es un pote coach sportif qui parle un français relâché et familier.
Génère un bilan de semaine pour {name}.

Données :
- Séances prévues : {total}
- Séances faites : {done_n} ({done_list})
- Séances manquées : {len(missed)} ({missed_list})
- Séances non confirmées : {len(unknown)}
- Streak de jours actifs en fin de semaine : {streak}
{comments_lines}

Le bilan doit :
1. Féliciter ou taquiner selon les résultats, dans le ton pote familier
2. Mentionner les stats/commentaires marquants s'il y en a
3. Terminer par un défi ou encouragement pour la semaine suivante
4. Mentionner le prénom {name}
5. 2-3 emojis, 6-8 lignes max, lisible sur WhatsApp
"""
    return await _generate(prompt)


# ── Rapport mensuel ───────────────────────────────────────────────────────────

def _compute_monthly_stats(sessions: list[dict]) -> dict:
    done    = [s for s in sessions if s["done"] == 1]
    missed  = [s for s in sessions if s["done"] == -1]
    total   = len(sessions)
    done_n  = len(done)

    sport_count: dict[str, int] = {}
    for s in done:
        sport_count[s["sport"]] = sport_count.get(s["sport"], 0) + 1
    top_sport = max(sport_count, key=sport_count.get) if sport_count else None

    weeks_active = len({s["week_start"] for s in done})
    weeks_total  = len({s["week_start"] for s in sessions})
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


async def generate_monthly_report(name: str, sessions: list[dict], month_label: str) -> str:
    if not sessions:
        return f"Pas de données pour {month_label} {name} — on commence fort le mois prochain ! 💪"

    stats = _compute_monthly_stats(sessions)

    sport_lines = "\n".join(
        f"  - {sport} : {count} séance(s)"
        for sport, count in sorted(stats["sport_count"].items(), key=lambda x: -x[1])
    ) or "  - aucune"

    # Résumé des commentaires du mois
    done_sessions = [s for s in sessions if s["done"] == 1]
    comments_with_data = [s for s in done_sessions if s.get("comment") and s["comment"].lower() not in ("ras", "rien", "non")]
    no_comment_count   = len([s for s in done_sessions if not s.get("comment") or s["comment"].lower() in ("ras", "rien", "non")])

    comments_lines = ""
    if comments_with_data:
        lines = []
        for s in comments_with_data:
            stats_str = f" (stats: {s['parsed_stats']})" if s.get("parsed_stats") else ""
            lines.append(f"  - {s['sport']}: {s['comment']}{stats_str}")
        comments_lines = "\nCommentaires notables du mois :\n" + "\n".join(lines[:5])  # max 5
    if no_comment_count:
        comments_lines += f"\n  - {no_comment_count} séance(s) sans commentaire ce mois"

    prompt = f"""Tu es un pote coach sportif qui parle un français relâché et familier.
Génère un bilan mensuel pour {name} pour le mois de {month_label}.

Statistiques :
- Séances prévues : {stats['total']}
- Séances réalisées : {stats['done']} ({stats['rate']}%)
- Séances manquées : {stats['missed']}
- Semaines actives : {stats['weeks_active']} / {stats['weeks_total']}
- Répartition par sport :
{sport_lines}
- Sport le plus pratiqué : {stats['top_sport'] or 'N/A'}
{comments_lines}

Le bilan doit :
1. Bilan chiffré clair (2 phrases)
2. Mentionner les stats/commentaires marquants s'il y en a
3. Identifier 1 truc à améliorer le mois prochain
4. Terminer par un défi motivant
5. Mentionner le prénom {name}, ton familier
6. 3-4 emojis, 12 lignes max, lisible sur WhatsApp
"""
    return await _generate(prompt)


# ── Message libre ─────────────────────────────────────────────────────────────

async def handle_free_message(name: str, user_message: str, sessions_context: str) -> str:
    prompt = f"""Tu es Géré, un pote coach sportif virtuel sur WhatsApp. Tu parles en français familier et relâché, comme un copain de quartier bienveillant.
Tu réponds de manière courte et sympa (3-4 phrases max). 1-2 emojis.
Mentionne le prénom {name} si c'est naturel.

Contexte – séances de {name} cette semaine (JSON) :
{sessions_context}

Message de {name} : "{user_message}"

Réponds directement sans te présenter à nouveau.
"""
    return await _generate(prompt)
