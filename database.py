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
                phone           TEXT PRIMARY KEY,
                name            TEXT,
                awaiting_name   BOOLEAN DEFAULT FALSE,
                created_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        # Migration si la table existe déjà sans awaiting_name / awaiting_plan_details
        cur.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS awaiting_name BOOLEAN DEFAULT FALSE
        """)
        cur.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS awaiting_plan_details TEXT
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
                id                  SERIAL PRIMARY KEY,
                phone               TEXT NOT NULL,
                week_start          TEXT NOT NULL,
                sport               TEXT NOT NULL,
                planned_day         TEXT NOT NULL,
                planned_time        TEXT NOT NULL,
                done                INTEGER DEFAULT 0,
                reminder_sent       INTEGER DEFAULT 0,
                checkin_sent        INTEGER DEFAULT 0,
                comment             TEXT,
                parsed_stats        TEXT,
                comment_requested   INTEGER DEFAULT 0,
                created_at          TIMESTAMP DEFAULT NOW()
            )
        """)
        # Migration si la table sessions existe déjà sans les nouvelles colonnes
        for col, definition in [
            ("comment",           "TEXT"),
            ("parsed_stats",      "TEXT"),
            ("comment_requested", "INTEGER DEFAULT 0"),
        ]:
            cur.execute(f"ALTER TABLE sessions ADD COLUMN IF NOT EXISTS {col} {definition}")


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


def set_user_name(phone: str, name: str):
    with get_db() as cur:
        cur.execute(
            "UPDATE users SET name = %s, awaiting_name = FALSE WHERE phone = %s",
            (name, phone),
        )


def set_awaiting_name(phone: str, value: bool):
    with get_db() as cur:
        cur.execute(
            "UPDATE users SET awaiting_name = %s WHERE phone = %s",
            (value, phone),
        )


def get_awaiting_plan_details(phone: str) -> str | None:
    with get_db() as cur:
        cur.execute("SELECT awaiting_plan_details FROM users WHERE phone = %s", (phone,))
        row = cur.fetchone()
        return row["awaiting_plan_details"] if row else None


def set_awaiting_plan_details(phone: str, details: str | None):
    with get_db() as cur:
        cur.execute(
            "UPDATE users SET awaiting_plan_details = %s WHERE phone = %s",
            (details, phone),
        )


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


def append_sessions(phone: str, week_start: str, sessions: list[dict]):
    """Ajoute des sessions sans supprimer les existantes."""
    with get_db() as cur:
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


def mark_comment_requested(session_id: int):
    with get_db() as cur:
        cur.execute("UPDATE sessions SET comment_requested = 1 WHERE id = %s", (session_id,))


def get_sessions_awaiting_comment(phone: str) -> list:
    """Sessions faites pour lesquelles un commentaire a été demandé mais pas encore reçu."""
    with get_db() as cur:
        cur.execute(
            """SELECT * FROM sessions
               WHERE phone = %s AND done = 1 AND comment_requested = 1 AND comment IS NULL""",
            (phone,),
        )
        return cur.fetchall()


def save_session_comment(session_id: int, comment: str, parsed_stats: str | None):
    with get_db() as cur:
        cur.execute(
            "UPDATE sessions SET comment = %s, parsed_stats = %s, comment_requested = 0 WHERE id = %s",
            (comment, parsed_stats, session_id),
        )


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
