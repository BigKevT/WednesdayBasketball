from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path
from time import monotonic
from urllib.parse import quote
import os
import secrets
import sqlite3

from flask import Flask, g, has_request_context, jsonify, make_response, redirect, request

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


APP_NAME = "Wednesday Basketball"
DB_PATH = Path(__file__).with_name("basketball.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8789"))
TZ = ZoneInfo("Asia/Taipei") if ZoneInfo else None

CONFIRMED_CAPACITY = 15
WAITLIST_CAPACITY = 5
START_TIME = time(19, 30)
END_TIME = time(21, 30)
DEADLINE_TIME = time(12, 0)
LOCATION = "木柵國中"

app = Flask(__name__)
DB_INITIALIZED = False
SHARED_DB_CONN = None
STATE_CACHE = {}
CACHE_TTL_SECONDS = 15


class RequestConnection:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, exc_type, exc, traceback):
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        return False


def using_postgres():
    return bool(DATABASE_URL)


def cache_get(key):
    cached = STATE_CACHE.get(key)
    if not cached:
        return None
    timestamp, value = cached
    if monotonic() - timestamp > CACHE_TTL_SECONDS:
        STATE_CACHE.pop(key, None)
        return None
    return value


def cache_set(key, value):
    STATE_CACHE[key] = (monotonic(), value)
    return value


def invalidate_cache():
    STATE_CACHE.clear()


def now():
    return datetime.now(TZ) if TZ else datetime.now()


def iso(dt):
    return dt.isoformat(timespec="seconds")


def sql(statement):
    if using_postgres():
        return statement.replace("?", "%s")
    return statement


def new_connection():
    if using_postgres():
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(DATABASE_URL, row_factory=dict_row)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def shared_postgres_connection():
    global SHARED_DB_CONN
    if SHARED_DB_CONN is None or SHARED_DB_CONN.closed:
        SHARED_DB_CONN = new_connection()
    return SHARED_DB_CONN


def connect():
    if not has_request_context():
        return new_connection()

    if "db_conn" not in g:
        if using_postgres():
            g.db_conn = shared_postgres_connection()
            g.db_conn_shared = True
        else:
            g.db_conn = new_connection()
            g.db_conn_shared = False
    return RequestConnection(g.db_conn)


@app.teardown_appcontext
def close_db(error=None):
    conn = g.pop("db_conn", None)
    shared = g.pop("db_conn_shared", False)
    if conn is not None and not shared:
        conn.close()


def init_db():
    with connect() as conn:
        if using_postgres():
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id BIGSERIAL PRIMARY KEY,
                    date TEXT NOT NULL UNIQUE,
                    weekday TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    location TEXT NOT NULL,
                    confirmed_capacity INTEGER NOT NULL,
                    waitlist_capacity INTEGER NOT NULL,
                    registration_deadline TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS registrations (
                    id BIGSERIAL PRIMARY KEY,
                    event_id BIGINT NOT NULL REFERENCES events(id),
                    name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('confirmed', 'waitlisted', 'cancelled')),
                    position INTEGER NOT NULL,
                    created_by TEXT NOT NULL CHECK (created_by IN ('player', 'admin')),
                    updated_by TEXT CHECK (updated_by IN ('player', 'admin')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_event_name
                ON registrations(event_id, lower(name))
                WHERE status != 'cancelled'
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recurring_players (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_recurring_player_name
                ON recurring_players(lower(name))
                WHERE enabled = 1
                """
            )
            return

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                weekday TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                location TEXT NOT NULL,
                confirmed_capacity INTEGER NOT NULL,
                waitlist_capacity INTEGER NOT NULL,
                registration_deadline TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS registrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('confirmed', 'waitlisted', 'cancelled')),
                position INTEGER NOT NULL,
                created_by TEXT NOT NULL CHECK (created_by IN ('player', 'admin')),
                updated_by TEXT CHECK (updated_by IN ('player', 'admin')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (event_id) REFERENCES events(id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_event_name
            ON registrations(event_id, lower(name))
            WHERE status != 'cancelled';

            CREATE TABLE IF NOT EXISTS recurring_players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                position INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_recurring_player_name
            ON recurring_players(lower(name))
            WHERE enabled = 1;
            """
        )


@app.before_request
def setup_db():
    global DB_INITIALIZED
    if request.path in {"/", "/event", "/favicon.ico"}:
        return None
    if not DB_INITIALIZED:
        init_db()
        ensure_current_event()
        DB_INITIALIZED = True


@app.get("/favicon.ico")
def favicon():
    return ("", 204)


def next_wednesday_event_date():
    current = now()
    today = current.date()
    days_until_wednesday = (2 - today.weekday()) % 7
    event_day = today + timedelta(days=days_until_wednesday)
    event_end = datetime.combine(event_day, END_TIME, tzinfo=TZ)
    if current > event_end:
        event_day += timedelta(days=7)
    return event_day


def registration_deadline_for(event_day):
    return datetime.combine(event_day - timedelta(days=1), DEADLINE_TIME, tzinfo=TZ)


def ensure_current_event():
    event_day = next_wednesday_event_date()
    with connect() as conn:
        row = conn.execute(sql("SELECT * FROM events WHERE date = ?"), (event_day.isoformat(),)).fetchone()
        if row:
            expected_deadline = iso(registration_deadline_for(event_day))
            if row["registration_deadline"] != expected_deadline:
                conn.execute(
                    sql("UPDATE events SET registration_deadline = ?, updated_at = ? WHERE id = ?"),
                    (expected_deadline, iso(now()), row["id"]),
                )
                row = conn.execute(sql("SELECT * FROM events WHERE id = ?"), (row["id"],)).fetchone()
                invalidate_cache()
            return row

        created = iso(now())
        deadline = registration_deadline_for(event_day)
        conn.execute(
            sql(
                """
                INSERT INTO events (
                    date, weekday, start_time, end_time, location, confirmed_capacity,
                    waitlist_capacity, registration_deadline, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            (
                event_day.isoformat(),
                "Wednesday",
                START_TIME.strftime("%H:%M"),
                END_TIME.strftime("%H:%M"),
                LOCATION,
                CONFIRMED_CAPACITY,
                WAITLIST_CAPACITY,
                iso(deadline),
                created,
                created,
            ),
        )
        row = conn.execute(sql("SELECT * FROM events WHERE date = ?"), (event_day.isoformat(),)).fetchone()
    invalidate_cache()
    auto_register_recurring_players(row)
    return row


def get_current_event_with_counts():
    cached = cache_get("current_event_with_counts")
    if cached is not None:
        return cached

    event = ensure_current_event()
    with connect() as conn:
        counts = conn.execute(
            sql(
                """
                SELECT
                    SUM(CASE WHEN status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed_count,
                    SUM(CASE WHEN status = 'waitlisted' THEN 1 ELSE 0 END) AS waitlisted_count
                FROM registrations
                WHERE event_id = ?
                """
            ),
            (event["id"],),
        ).fetchone()
        return cache_set(
            "current_event_with_counts",
            (event, counts["confirmed_count"] or 0, counts["waitlisted_count"] or 0),
        )


def list_registrations(event_id, status=None):
    cache_key = ("registrations", event_id, status or "all")
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    query = "SELECT * FROM registrations WHERE event_id = ?"
    params = [event_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY position ASC, created_at ASC"
    with connect() as conn:
        return cache_set(cache_key, conn.execute(sql(query), tuple(params)).fetchall())


def list_recurring_players():
    cached = cache_get("recurring_players")
    if cached is not None:
        return cached

    with connect() as conn:
        return cache_set(
            "recurring_players",
            conn.execute(
                sql(
                    """
                    SELECT * FROM recurring_players
                    WHERE enabled = 1
                    ORDER BY position ASC, created_at ASC
                    """
                )
            ).fetchall(),
        )


def count_status(conn, event_id, status):
    row = conn.execute(
        sql("SELECT COUNT(*) AS total FROM registrations WHERE event_id = ? AND status = ?"),
        (event_id, status),
    ).fetchone()
    return row["total"]


def next_recurring_position(conn):
    row = conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 AS next_pos FROM recurring_players WHERE enabled = 1"
    ).fetchone()
    return row["next_pos"]


def recurring_name_exists(conn, name):
    return (
        conn.execute(
            sql("SELECT 1 FROM recurring_players WHERE lower(name) = lower(?) AND enabled = 1"),
            (name,),
        ).fetchone()
        is not None
    )


def active_name_exists(conn, event_id, name):
    return (
        conn.execute(
            sql(
                """
                SELECT 1 FROM registrations
                WHERE event_id = ? AND lower(name) = lower(?) AND status != 'cancelled'
                """
            ),
            (event_id, name),
        ).fetchone()
        is not None
    )


def add_recurring_player(name):
    clean_name = " ".join(name.strip().split())
    if not clean_name:
        return False, "請輸入固定報名者名字。"

    with connect() as conn:
        if recurring_name_exists(conn, clean_name):
            return False, "這個名字已經在固定報名名單。"

        created = iso(now())
        conn.execute(
            sql(
                """
                INSERT INTO recurring_players (name, position, enabled, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                """
            ),
            (clean_name, next_recurring_position(conn), created, created),
        )
    invalidate_cache()

    event = ensure_current_event()
    ok, roster_msg = add_registration(event, clean_name, "admin")
    if ok:
        return True, f"{clean_name} 已加入固定報名名單，並已套用到本週。"
    return True, f"{clean_name} 已加入固定報名名單。本週未套用：{roster_msg}"


def delete_recurring_player(player_id):
    with connect() as conn:
        row = conn.execute(
            sql("SELECT * FROM recurring_players WHERE id = ? AND enabled = 1"),
            (player_id,),
        ).fetchone()
        if not row:
            return False, "找不到固定報名者。"

        conn.execute(
            sql("UPDATE recurring_players SET enabled = 0, updated_at = ? WHERE id = ?"),
            (iso(now()), player_id),
        )
    invalidate_cache()
    return True, f"{row['name']} 已從固定報名名單移除。"


def auto_register_recurring_players(event):
    for player in list_recurring_players():
        add_registration(event, player["name"], "admin")


def next_position(conn, event_id):
    row = conn.execute(
        sql("SELECT COALESCE(MAX(position), 0) + 1 AS next_pos FROM registrations WHERE event_id = ?"),
        (event_id,),
    ).fetchone()
    return row["next_pos"]


def add_registration(event, name, created_by="player", force_status=None):
    clean_name = " ".join(name.strip().split())
    if not clean_name:
        return False, "請輸入名字。"

    current = now()
    deadline = datetime.fromisoformat(event["registration_deadline"])
    if created_by == "player" and current > deadline:
        return False, "本週報名已截止。"

    with connect() as conn:
        if active_name_exists(conn, event["id"], clean_name):
            return False, "這個名字已經報名了。"

        confirmed = count_status(conn, event["id"], "confirmed")
        waitlisted = count_status(conn, event["id"], "waitlisted")
        status = force_status
        if status is None:
            if confirmed < event["confirmed_capacity"]:
                status = "confirmed"
            elif waitlisted < event["waitlist_capacity"]:
                status = "waitlisted"
            else:
                return False, "本週已滿。"

        if status == "confirmed" and confirmed >= event["confirmed_capacity"]:
            return False, "正式名單已滿。"
        if status == "waitlisted" and waitlisted >= event["waitlist_capacity"]:
            return False, "候補名單已滿。"

        created = iso(current)
        conn.execute(
            sql(
                """
                INSERT INTO registrations (event_id, name, status, position, created_by, updated_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                """
            ),
            (event["id"], clean_name, status, next_position(conn, event["id"]), created_by, created, created),
        )
    invalidate_cache()

    if status == "confirmed":
        return True, f"{clean_name} 已加入正式名單。"
    return True, f"{clean_name} 已加入候補。"


def cancel_registration(registration_id, actor="player"):
    with connect() as conn:
        reg = conn.execute(sql("SELECT * FROM registrations WHERE id = ?"), (registration_id,)).fetchone()
        if not reg or reg["status"] == "cancelled":
            return False, "找不到可取消的報名。"

        was_confirmed = reg["status"] == "confirmed"
        updated = iso(now())
        conn.execute(
            sql("UPDATE registrations SET status = 'cancelled', updated_by = ?, updated_at = ? WHERE id = ?"),
            (actor, updated, registration_id),
        )

        promoted_name = None
        if was_confirmed:
            candidate = conn.execute(
                sql(
                    """
                    SELECT * FROM registrations
                    WHERE event_id = ? AND status = 'waitlisted'
                    ORDER BY position ASC, created_at ASC
                    LIMIT 1
                    """
                ),
                (reg["event_id"],),
            ).fetchone()
            if candidate:
                conn.execute(
                    sql("UPDATE registrations SET status = 'confirmed', updated_by = ?, updated_at = ? WHERE id = ?"),
                    (actor, updated, candidate["id"]),
                )
                promoted_name = candidate["name"]
    invalidate_cache()

    if promoted_name:
        return True, f"已取消 {reg['name']}，{promoted_name} 已自動遞補。"
    return True, f"已取消 {reg['name']}。"


def update_status(registration_id, new_status):
    if new_status not in {"confirmed", "waitlisted", "cancelled"}:
        return False, "狀態不正確。"

    with connect() as conn:
        reg = conn.execute(sql("SELECT * FROM registrations WHERE id = ?"), (registration_id,)).fetchone()
        if not reg:
            return False, "找不到報名資料。"

        if new_status != "cancelled":
            event = conn.execute(sql("SELECT * FROM events WHERE id = ?"), (reg["event_id"],)).fetchone()
            capacity = event["confirmed_capacity"] if new_status == "confirmed" else event["waitlist_capacity"]
            total = count_status(conn, reg["event_id"], new_status)
            if reg["status"] != new_status and total >= capacity:
                return False, "目標名單已滿。"

        conn.execute(
            sql("UPDATE registrations SET status = ?, updated_by = 'admin', updated_at = ? WHERE id = ?"),
            (new_status, iso(now()), registration_id),
        )
    invalidate_cache()
    return True, "管理者已更新名單。"


def update_event_settings(event_id, confirmed_capacity, waitlist_capacity):
    try:
        confirmed_capacity = int(confirmed_capacity)
        waitlist_capacity = int(waitlist_capacity)
    except ValueError:
        return False, "名額必須是數字。"

    if confirmed_capacity < 1 or waitlist_capacity < 0:
        return False, "正式名額至少 1 人，候補名額不可小於 0。"

    with connect() as conn:
        confirmed = count_status(conn, event_id, "confirmed")
        waitlisted = count_status(conn, event_id, "waitlisted")
        if confirmed_capacity < confirmed:
            return False, f"正式名額不能小於目前正式人數 {confirmed}。"
        if waitlist_capacity < waitlisted:
            return False, f"候補名額不能小於目前候補人數 {waitlisted}。"

        conn.execute(
            sql(
                """
                UPDATE events
                SET confirmed_capacity = ?, waitlist_capacity = ?, updated_at = ?
                WHERE id = ?
                """
            ),
            (confirmed_capacity, waitlist_capacity, iso(now()), event_id),
        )
    invalidate_cache()
    return True, "名額設定已更新。"


def move_registration(registration_id, direction):
    with connect() as conn:
        reg = conn.execute(sql("SELECT * FROM registrations WHERE id = ?"), (registration_id,)).fetchone()
        if not reg:
            return False, "找不到報名資料。"
        operator = "<" if direction == "up" else ">"
        order = "DESC" if direction == "up" else "ASC"
        target = conn.execute(
            sql(
                f"""
                SELECT * FROM registrations
                WHERE event_id = ? AND status = ? AND position {operator} ?
                ORDER BY position {order}
                LIMIT 1
                """
            ),
            (reg["event_id"], reg["status"], reg["position"]),
        ).fetchone()
        if not target:
            return True, "順位沒有變更。"
        updated = iso(now())
        conn.execute(
            sql("UPDATE registrations SET position = ?, updated_by = 'admin', updated_at = ? WHERE id = ?"),
            (target["position"], updated, reg["id"]),
        )
        conn.execute(
            sql("UPDATE registrations SET position = ?, updated_by = 'admin', updated_at = ? WHERE id = ?"),
            (reg["position"], updated, target["id"]),
        )
    invalidate_cache()
    return True, "順位已更新。"


def event_status(event, confirmed_count, waitlisted_count):
    if now() > datetime.fromisoformat(event["registration_deadline"]):
        return "closed", "本週報名已截止"
    if confirmed_count < event["confirmed_capacity"]:
        return "open", "可報名"
    if waitlisted_count < event["waitlist_capacity"]:
        return "waitlist_open", "正式已滿，可候補"
    return "full", "本週已滿"


def format_event_date(event):
    event_date = date.fromisoformat(event["date"])
    return f"{event_date.month}/{event_date.day}（三）"


def format_deadline(event):
    deadline = datetime.fromisoformat(event["registration_deadline"])
    weekday_labels = ["一", "二", "三", "四", "五", "六", "日"]
    return f"{deadline.month}/{deadline.day}（{weekday_labels[deadline.weekday()]}） {deadline.strftime('%H:%M')}"


def layout(title, body, flash=""):
    message = f'<div class="flash">{escape(flash)}</div>' if flash else ""
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} | {APP_NAME}</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #171717;
      --muted: #66615a;
      --line: #ded8cf;
      --court: #c75a24;
      --court-dark: #883816;
      --paint: #155e75;
      --paper: #fffaf3;
      --panel: #ffffff;
      --danger: #b42318;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        linear-gradient(135deg, rgba(199, 90, 36, .12), transparent 34%),
        linear-gradient(180deg, #fffaf3 0%, #f4efe7 100%);
      min-height: 100vh;
    }}
    a {{ color: inherit; text-decoration: none; }}
    .shell {{ width: min(960px, 100%); margin: 0 auto; padding: 18px 16px 40px; }}
    .topbar {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 20px; }}
    .brand {{ font-weight: 900; font-size: 18px; letter-spacing: 0; }}
    .nav {{ display: flex; gap: 8px; align-items: center; }}
    .nav a {{ color: var(--muted); font-weight: 800; font-size: 14px; }}
    .hero {{
      border: 1px solid rgba(136, 56, 22, .18);
      background: var(--panel);
      border-radius: 8px;
      padding: 20px;
      box-shadow: 0 16px 36px rgba(52, 36, 22, .08);
      position: relative;
      overflow: hidden;
    }}
    .hero:before {{
      content: "";
      position: absolute;
      inset: 0 0 auto auto;
      width: 96px;
      height: 96px;
      border-left: 2px solid rgba(199, 90, 36, .18);
      border-bottom: 2px solid rgba(199, 90, 36, .18);
      border-radius: 0 0 0 96px;
      pointer-events: none;
    }}
    h1 {{ margin: 0; font-size: clamp(30px, 9vw, 52px); line-height: .98; letter-spacing: 0; }}
    h2 {{ margin: 0 0 14px; font-size: 22px; letter-spacing: 0; }}
    .sub {{ color: var(--muted); margin: 10px 0 0; font-weight: 700; }}
    .stats {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 18px 0; }}
    .stats.with-deadline {{ grid-template-columns: 1fr; }}
    .stat {{ border: 1px solid var(--line); border-radius: 8px; padding: 14px; background: #fffdf8; }}
    .stat span {{ display: block; color: var(--muted); font-size: 13px; font-weight: 800; }}
    .stat strong {{ display: block; font-size: 28px; margin-top: 4px; }}
    .stat.deadline strong {{ font-size: 22px; line-height: 1.2; }}
    .badge {{ display: inline-flex; align-items: center; min-height: 34px; padding: 6px 10px; border-radius: 8px; background: #f2e7db; color: var(--court-dark); font-weight: 900; }}
    .btn, button {{
      border: 0;
      min-height: 44px;
      border-radius: 8px;
      padding: 11px 14px;
      font: inherit;
      font-weight: 900;
      cursor: pointer;
      background: var(--court);
      color: #fff;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
    }}
    .btn.secondary, button.secondary {{ background: #f0e7dc; color: var(--ink); }}
    .btn.danger, button.danger {{ background: var(--danger); }}
    .btn.small, button.small {{ min-height: 36px; padding: 8px 10px; font-size: 13px; }}
    button:disabled {{ opacity: .5; cursor: not-allowed; }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .grid {{ display: grid; grid-template-columns: 1fr; gap: 14px; margin-top: 14px; }}
    .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }}
    .form {{ display: grid; gap: 10px; margin-top: 12px; }}
    label {{ font-size: 13px; font-weight: 900; color: var(--muted); }}
    input, select {{
      width: 100%;
      min-height: 44px;
      border: 1px solid #cfc6bb;
      border-radius: 8px;
      padding: 10px 12px;
      font: inherit;
      background: #fff;
    }}
    .roster {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }}
    .person {{
      min-height: 48px;
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 10px;
      align-items: center;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fffdf8;
    }}
    .num {{ color: var(--muted); font-weight: 900; min-width: 28px; }}
    .name {{ font-weight: 900; overflow-wrap: anywhere; }}
    .empty {{ color: var(--muted); font-weight: 800; margin: 0; }}
    .flash {{ border-left: 4px solid var(--paint); background: #e8f3f5; padding: 12px; border-radius: 8px; margin-bottom: 14px; font-weight: 800; }}
    .admin-controls {{ display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }}
    @media (min-width: 740px) {{
      .shell {{ padding-top: 28px; }}
      .hero {{ padding: 32px; }}
      .grid.two {{ grid-template-columns: 1fr 1fr; }}
      .stats.with-deadline {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <div class="topbar">
      <a class="brand" href="/">Wednesday Basketball</a>
      <nav class="nav">
        <a href="/event">報名</a>
        <a href="/admin">管理</a>
      </nav>
    </div>
    {message}
    {body}
  </main>
</body>
</html>"""


def home_page(flash=""):
    body = """
    <section class="hero">
      <span class="badge" id="status-label">讀取中</span>
      <h1><span id="event-date">本週三</span><br>晚上上場</h1>
      <p class="sub" id="event-meta">19:30-21:30 · 木柵國中</p>
      <div class="stats with-deadline">
        <div class="stat"><span>正式名單</span><strong id="confirmed-count">-- / --</strong></div>
        <div class="stat"><span>候補名單</span><strong id="waitlisted-count">-- / --</strong></div>
        <div class="stat deadline"><span>報名截止</span><strong id="deadline">讀取中</strong></div>
      </div>
      <div class="actions">
        <a class="btn" href="/event">查看 / 報名</a>
      </div>
    </section>
    <script>
      async function loadSummary() {
        try {
          const res = await fetch('/api/state', { cache: 'no-store' });
          const data = await res.json();
          document.getElementById('status-label').textContent = data.status_label;
          document.getElementById('event-date').textContent = data.event_date;
          document.getElementById('event-meta').textContent = `${data.start_time}-${data.end_time} · ${data.location}`;
          document.getElementById('confirmed-count').textContent = `${data.confirmed_count} / ${data.confirmed_capacity}`;
          document.getElementById('waitlisted-count').textContent = `${data.waitlisted_count} / ${data.waitlist_capacity}`;
          document.getElementById('deadline').textContent = data.deadline;
        } catch (error) {
          document.getElementById('status-label').textContent = '暫時無法讀取';
        }
      }
      loadSummary();
    </script>
    """
    return layout("首頁", body, flash)


def roster_html(rows, prefix, admin=False):
    if not rows:
        return '<p class="empty">目前沒有人。</p>'
    items = []
    for index, row in enumerate(rows, start=1):
        label = str(index) if prefix == "正式" else f"候補 {index}"
        if admin:
            controls = f"""
            <div class="admin-controls">
              <form method="post" action="/admin/move"><input type="hidden" name="id" value="{row['id']}"><input type="hidden" name="direction" value="up"><button class="small secondary">上移</button></form>
              <form method="post" action="/admin/move"><input type="hidden" name="id" value="{row['id']}"><input type="hidden" name="direction" value="down"><button class="small secondary">下移</button></form>
              <form method="post" action="/admin/status"><input type="hidden" name="id" value="{row['id']}"><input type="hidden" name="status" value="{'waitlisted' if row['status'] == 'confirmed' else 'confirmed'}"><button class="small secondary">{'轉候補' if row['status'] == 'confirmed' else '轉正式'}</button></form>
              <form method="post" action="/admin/status"><input type="hidden" name="id" value="{row['id']}"><input type="hidden" name="status" value="cancelled"><button class="small danger">取消</button></form>
            </div>
            """
        else:
            controls = f"""
            <form method="post" action="/cancel" onsubmit="return confirm('確定取消 {escape(row['name'])} 的報名嗎？')">
              <input type="hidden" name="id" value="{row['id']}">
              <button class="small danger">取消</button>
            </form>
            """
        items.append(f'<li class="person"><span class="num">{label}</span><span class="name">{escape(row["name"])}</span>{controls}</li>')
    return f'<ol class="roster">{"".join(items)}</ol>'


def recurring_players_html(rows):
    if not rows:
        return '<p class="empty">目前沒有固定報名者。</p>'
    items = []
    for index, row in enumerate(rows, start=1):
        controls = f"""
        <form method="post" action="/admin/recurring/delete" onsubmit="return confirm('確定移除 {escape(row['name'])} 的固定報名設定嗎？')">
          <input type="hidden" name="id" value="{row['id']}">
          <button class="small danger">移除</button>
        </form>
        """
        items.append(
            f'<li class="person"><span class="num">{index}</span><span class="name">{escape(row["name"])}</span>{controls}</li>'
        )
    return f'<ol class="roster">{"".join(items)}</ol>'


def event_page(flash=""):
    form = """
    <form class="form" method="post" action="/register" id="registration-form">
      <label for="name">名字</label>
      <input id="name" name="name" autocomplete="name" maxlength="40" placeholder="輸入你的名字">
      <button id="register-button">報名</button>
    </form>
    """
    body = """
    <section class="hero">
      <span class="badge" id="status-label">讀取中</span>
      <h1><span id="event-date">本週三</span><br><span id="event-location">木柵國中</span></h1>
      <p class="sub" id="event-time">19:30-21:30</p>
      <div class="stats with-deadline">
        <div class="stat"><span>正式名單</span><strong id="confirmed-count">-- / --</strong></div>
        <div class="stat"><span>候補名單</span><strong id="waitlisted-count">-- / --</strong></div>
        <div class="stat deadline"><span>報名截止</span><strong id="deadline">讀取中</strong></div>
      </div>
      __REGISTRATION_FORM__
    </section>
    <section class="grid two">
      <div class="panel"><h2>正式名單</h2><div id="confirmed-roster"><p class="empty">讀取中。</p></div></div>
      <div class="panel"><h2>候補名單</h2><div id="waitlisted-roster"><p class="empty">讀取中。</p></div></div>
    </section>
    <script>
      const flash = document.querySelector('.flash');

      function showMessage(message) {
        if (!message) return;
        if (flash) {
          flash.textContent = message;
          return;
        }
        const box = document.createElement('div');
        box.className = 'flash';
        box.textContent = message;
        document.querySelector('.topbar').after(box);
      }

      function escapeHtml(value) {
        return String(value).replace(/[&<>"']/g, (char) => ({
          '&': '&amp;',
          '<': '&lt;',
          '>': '&gt;',
          '"': '&quot;',
          "'": '&#039;'
        }[char]));
      }

      function rosterHtml(players, prefix) {
        if (!players.length) return '<p class="empty">目前沒有人。</p>';
        return `<ol class="roster">${players.map((player, index) => {
          const label = prefix === '正式' ? index + 1 : `候補 ${index + 1}`;
          return `<li class="person"><span class="num">${label}</span><span class="name">${escapeHtml(player.name)}</span><button class="small danger" data-cancel-id="${player.id}" data-name="${escapeHtml(player.name)}">取消</button></li>`;
        }).join('')}</ol>`;
      }

      async function loadEventState() {
        const res = await fetch('/api/state', { cache: 'no-store' });
        const data = await res.json();
        document.getElementById('status-label').textContent = data.status_label;
        document.getElementById('event-date').textContent = data.event_date;
        document.getElementById('event-location').textContent = data.location;
        document.getElementById('event-time').textContent = `${data.start_time}-${data.end_time}`;
        document.getElementById('confirmed-count').textContent = `${data.confirmed_count} / ${data.confirmed_capacity}`;
        document.getElementById('waitlisted-count').textContent = `${data.waitlisted_count} / ${data.waitlist_capacity}`;
        document.getElementById('deadline').textContent = data.deadline;
        document.getElementById('confirmed-roster').innerHTML = rosterHtml(data.confirmed, '正式');
        document.getElementById('waitlisted-roster').innerHTML = rosterHtml(data.waitlisted, '候補');

        const input = document.getElementById('name');
        const button = document.getElementById('register-button');
        input.disabled = data.form_disabled;
        button.disabled = data.form_disabled;
        button.textContent = data.button_text;
      }

      document.getElementById('registration-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const input = document.getElementById('name');
        const button = document.getElementById('register-button');
        button.disabled = true;
        button.textContent = '處理中';
        const res = await fetch('/api/register', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ name: input.value })
        });
        const data = await res.json();
        showMessage(data.message);
        if (data.ok) input.value = '';
        await loadEventState();
      });

      document.addEventListener('click', async (event) => {
        const button = event.target.closest('[data-cancel-id]');
        if (!button) return;
        if (!confirm(`確定取消 ${button.dataset.name} 的報名嗎？`)) return;
        button.disabled = true;
        const res = await fetch('/api/cancel', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ id: button.dataset.cancelId })
        });
        const data = await res.json();
        showMessage(data.message);
        await loadEventState();
      });

      loadEventState().catch(() => showMessage('暫時無法讀取名單，請重新整理。'));
    </script>
    """
    return layout("查看 / 報名", body.replace("__REGISTRATION_FORM__", form), flash)


def public_state():
    event, confirmed_count, waitlisted_count = get_current_event_with_counts()
    status, label = event_status(event, confirmed_count, waitlisted_count)
    return {
        "event_date": format_event_date(event),
        "start_time": event["start_time"],
        "end_time": event["end_time"],
        "location": event["location"],
        "deadline": format_deadline(event),
        "confirmed_count": confirmed_count,
        "waitlisted_count": waitlisted_count,
        "confirmed_capacity": event["confirmed_capacity"],
        "waitlist_capacity": event["waitlist_capacity"],
        "status": status,
        "status_label": label,
        "form_disabled": status in {"closed", "full"},
        "button_text": "本週報名已截止" if status == "closed" else "本週已滿" if status == "full" else "報名",
        "confirmed": [{"id": row["id"], "name": row["name"]} for row in list_registrations(event["id"], "confirmed")],
        "waitlisted": [{"id": row["id"], "name": row["name"]} for row in list_registrations(event["id"], "waitlisted")],
    }


def admin_authorized():
    return secrets.compare_digest(request.cookies.get("admin_session", ""), ADMIN_PASSWORD)


def admin_login_page(flash=""):
    body = """
    <section class="hero">
      <h1>管理後台</h1>
      <p class="sub">輸入管理密碼後可以調整正式名單與候補名單。</p>
      <form class="form" method="post" action="/admin/login">
        <label for="password">管理密碼</label>
        <input id="password" type="password" name="password">
        <button>登入</button>
      </form>
    </section>
    """
    return layout("管理登入", body, flash)


def admin_page(flash=""):
    event, confirmed_count, waitlisted_count = get_current_event_with_counts()
    confirmed = list_registrations(event["id"], "confirmed")
    waitlisted = list_registrations(event["id"], "waitlisted")
    cancelled = list_registrations(event["id"], "cancelled")
    recurring_players = list_recurring_players()
    body = f"""
    <section class="hero">
      <span class="badge">管理後台</span>
      <h1>{format_event_date(event)}<br>名單管理</h1>
      <p class="sub">{event['start_time']}-{event['end_time']} · {escape(event['location'])}</p>
      <div class="stats">
        <div class="stat"><span>正式名單</span><strong>{confirmed_count} / {event['confirmed_capacity']}</strong></div>
        <div class="stat"><span>候補名單</span><strong>{waitlisted_count} / {event['waitlist_capacity']}</strong></div>
      </div>
      <form class="form" method="post" action="/admin/settings">
        <label for="confirmed_capacity">正式名額</label>
        <input id="confirmed_capacity" name="confirmed_capacity" type="number" min="{confirmed_count}" max="99" value="{event['confirmed_capacity']}">
        <label for="waitlist_capacity">候補名額</label>
        <input id="waitlist_capacity" name="waitlist_capacity" type="number" min="{waitlisted_count}" max="99" value="{event['waitlist_capacity']}">
        <button class="secondary">更新名額設定</button>
      </form>
      <form class="form" method="post" action="/admin/add">
        <label for="name">新增球友</label>
        <input id="name" name="name" maxlength="40" placeholder="輸入名字">
        <select name="status">
          <option value="confirmed">加入正式名單</option>
          <option value="waitlisted">加入候補名單</option>
        </select>
        <button>新增</button>
      </form>
    </section>
    <section class="grid two">
      <div class="panel"><h2>正式名單</h2>{roster_html(confirmed, "正式", admin=True)}</div>
      <div class="panel"><h2>候補名單</h2>{roster_html(waitlisted, "候補", admin=True)}</div>
      <div class="panel"><h2>取消紀錄</h2>{roster_html(cancelled, "取消", admin=True)}</div>
      <div class="panel">
        <h2>固定報名者</h2>
        <p class="sub">每週新場次建立時，系統會自動幫這些人報名。</p>
        <form class="form" method="post" action="/admin/recurring/add">
          <label for="recurring_name">新增固定報名者</label>
          <input id="recurring_name" name="name" maxlength="40" placeholder="輸入名字">
          <button>加入固定名單</button>
        </form>
        {recurring_players_html(recurring_players)}
      </div>
    </section>
    """
    return layout("管理後台", body, flash)


@app.get("/")
def home():
    return home_page(request.args.get("msg", ""))


@app.get("/event")
def event():
    return event_page(request.args.get("msg", ""))


@app.get("/api/state")
def api_state():
    return jsonify(public_state())


@app.post("/api/register")
def api_register():
    ok, msg = add_registration(ensure_current_event(), request.form.get("name", ""), "player")
    return jsonify({"ok": ok, "message": msg})


@app.post("/api/cancel")
def api_cancel():
    ok, msg = cancel_registration(int(request.form.get("id", "0")), "player")
    return jsonify({"ok": ok, "message": msg})


@app.post("/register")
def register():
    ok, msg = add_registration(ensure_current_event(), request.form.get("name", ""), "player")
    return redirect(f"/event?msg={quote(msg)}")


@app.post("/cancel")
def cancel():
    ok, msg = cancel_registration(int(request.form.get("id", "0")), "player")
    return redirect(f"/event?msg={quote(msg)}")


@app.get("/admin")
def admin():
    flash = request.args.get("msg", "")
    if admin_authorized():
        return admin_page(flash)
    return admin_login_page(flash)


@app.post("/admin/login")
def admin_login():
    if secrets.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
        response = make_response(redirect("/admin"))
        response.set_cookie("admin_session", ADMIN_PASSWORD, httponly=True, samesite="Lax", secure=using_postgres())
        return response
    return redirect(f"/admin?msg={quote('管理密碼不正確。')}")


@app.post("/admin/add")
def admin_add():
    if not admin_authorized():
        return redirect(f"/admin?msg={quote('請先登入管理後台。')}")
    ok, msg = add_registration(
        ensure_current_event(),
        request.form.get("name", ""),
        "admin",
        request.form.get("status", "confirmed"),
    )
    return redirect(f"/admin?msg={quote(msg)}")


@app.post("/admin/settings")
def admin_settings():
    if not admin_authorized():
        return redirect(f"/admin?msg={quote('請先登入管理後台。')}")
    event = ensure_current_event()
    ok, msg = update_event_settings(
        event["id"],
        request.form.get("confirmed_capacity", ""),
        request.form.get("waitlist_capacity", ""),
    )
    return redirect(f"/admin?msg={quote(msg)}")


@app.post("/admin/recurring/add")
def admin_recurring_add():
    if not admin_authorized():
        return redirect(f"/admin?msg={quote('請先登入管理後台。')}")
    ok, msg = add_recurring_player(request.form.get("name", ""))
    return redirect(f"/admin?msg={quote(msg)}")


@app.post("/admin/recurring/delete")
def admin_recurring_delete():
    if not admin_authorized():
        return redirect(f"/admin?msg={quote('請先登入管理後台。')}")
    ok, msg = delete_recurring_player(int(request.form.get("id", "0")))
    return redirect(f"/admin?msg={quote(msg)}")


@app.post("/admin/status")
def admin_status():
    if not admin_authorized():
        return redirect(f"/admin?msg={quote('請先登入管理後台。')}")
    ok, msg = update_status(int(request.form.get("id", "0")), request.form.get("status", ""))
    return redirect(f"/admin?msg={quote(msg)}")


@app.post("/admin/move")
def admin_move():
    if not admin_authorized():
        return redirect(f"/admin?msg={quote('請先登入管理後台。')}")
    ok, msg = move_registration(int(request.form.get("id", "0")), request.form.get("direction", "down"))
    return redirect(f"/admin?msg={quote(msg)}")


if __name__ == "__main__":
    init_db()
    ensure_current_event()
    print(f"{APP_NAME} running at http://{HOST}:{PORT}")
    print(f"Admin password: {ADMIN_PASSWORD}")
    app.run(host=HOST, port=PORT, debug=True)
