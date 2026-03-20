import os
import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")


@contextmanager
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def init_db():
    with get_db() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                phone        TEXT PRIMARY KEY,
                name         TEXT,
                created_at   TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS weekly_plans (
                id           SERIAL PRIMARY KEY,
                phone        TEXT NOT NULL,
                week_start   TEXT NOT NULL,
                raw_message  TEXT,
                parsed_json  TEXT,
                created_at   TIMESTAMP DEFAULT NOW(),
                UNIQUE(phone, week_start)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id              SERIAL PRIMARY KEY,
                phone           TEXT NOT NULL,
                week_start      TEXT NOT NULL,
                sport           TEXT NOT NULL,
                planned_day     TEXT NOT NULL,
                planned_time    TEXT NOT NULL,
                done            INTEGER DEFAULT 0,
                reminder_sent   INTEGER DEFAULT 0,
                checkin_sent    INTEGER DEFAULT 0,
                created_at      TIMESTAMP DEFAULT NOW()
            )
        """)


# ── Users ─────────────────────────────────────────────────────────────────────

def upsert_user(phone: str, name: str | None = None):
    with get_db() as cur:
        cur.execute(
            """INSERT INTO users (phone, name) VALUES (%s, %s)
               ON CONFLICT(phone) DO UPDATE SET name = COALESCE(EXCLUDED.name, users.name)""",
            (phone, name),
        )


def get_user(phone: str):
    with get_db() as cur:
        cur.execute("SELECT * FROM users WHERE phone = %s", (phone,))
        return cur.fetchone()


# ── Weekly plans ───────────────────────────────────────────────────────────────

def upsert_weekly_plan(phone: str, week_start: str, raw_message: str, parsed_json: str):
    with get_db() as cur:
        cur.execute(
            """INSERT INTO weekly_plans (phone, week_start, raw_message, parsed_json)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT(phone, week_start) DO UPDATE SET
                   raw_message = EXCLUDED.raw_message,
                   parsed_json = EXCLUDED.parsed_json""",
            (phone, week_start, raw_message, parsed_json),
        )


def get_weekly_plan(phone: str, week_start: str):
    with get_db() as cur:
        cur.execute(
            "SELECT * FROM weekly_plans WHERE phone = %s AND week_start = %s",
            (phone, week_start),
        )
        return cur.fetchone()


# ── Sessions ───────────────────────────────────────────────────────────────────

def insert_sessions(phone: str, week_start: str, sessions: list[dict]):
    """Remplace toutes les sessions de la semaine pour cet utilisateur."""
    with get_db() as cur:
        cur.execute(
            "DELETE FROM sessions WHERE phone = %s AND week_start = %s",
            (phone, week_start),
        )
        cur.executemany(
            """INSERT INTO sessions (phone, week_start, sport, planned_day, planned_time)
               VALUES (%(phone)s, %(week_start)s, %(sport)s, %(planned_day)s, %(planned_time)s)""",
            [
                {
                    "phone": phone,
                    "week_start": week_start,
                    "sport": s["sport"],
                    "planned_day": s["day"],
                    "planned_time": s["time"],
                }
                for s in sessions
            ],
        )


def get_sessions_for_week(phone: str, week_start: str) -> list:
    with get_db() as cur:
        cur.execute(
            "SELECT * FROM sessions WHERE phone = %s AND week_start = %s ORDER BY planned_day, planned_time",
            (phone, week_start),
        )
        return cur.fetchall()


def get_sessions_for_day(day_name: str, week_start: str) -> list:
    """Retourne toutes les sessions planifiées un jour donné (tous users)."""
    with get_db() as cur:
        cur.execute(
            "SELECT * FROM sessions WHERE planned_day = %s AND week_start = %s",
            (day_name, week_start),
        )
        return cur.fetchall()


def mark_reminder_sent(session_id: int):
    with get_db() as cur:
        cur.execute("UPDATE sessions SET reminder_sent = 1 WHERE id = %s", (session_id,))


def mark_checkin_sent(session_id: int):
    with get_db() as cur:
        cur.execute("UPDATE sessions SET checkin_sent = 1 WHERE id = %s", (session_id,))


def mark_session_done(session_id: int, done: int):
    """done: 1=fait, -1=raté"""
    with get_db() as cur:
        cur.execute("UPDATE sessions SET done = %s WHERE id = %s", (done, session_id))


def get_sessions_for_month(phone: str, year: int, month: int) -> list:
    prefix = f"{year:04d}-{month:02d}"
    with get_db() as cur:
        cur.execute(
            """SELECT * FROM sessions
               WHERE phone = %s AND week_start LIKE %s
               ORDER BY week_start, planned_day, planned_time""",
            (phone, f"{prefix}%"),
        )
        return cur.fetchall()


def get_pending_checkin_sessions(phone: str, week_start: str) -> list:
    """Sessions dont le check-in n'a pas encore reçu de réponse (done=0)."""
    with get_db() as cur:
        cur.execute(
            """SELECT * FROM sessions
               WHERE phone = %s AND week_start = %s AND checkin_sent = 1 AND done = 0""",
            (phone, week_start),
        )
        return cur.fetchall()
