import os
import uuid
import random
import string
import hashlib
import time
import requests
import psycopg2
import psycopg2.extras
from psycopg2.pool import SimpleConnectionPool
from datetime import datetime, timezone
from flask import Flask, request, jsonify, g
from flask_cors import CORS

# ---------- Configuración ----------
# Connection string de Supabase (Settings -> Database -> Connection string -> modo "Transaction" con pooler)
# Ejemplo: postgresql://postgres.xxxx:PASSWORD@aws-0-region.pooler.supabase.com:6543/postgres
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Falta la env var DATABASE_URL con la connection string de Supabase")

BOT_SECRET = os.environ.get("BOT_SECRET", "5bb35bd2e3317744f2630e6e53d74c3f")  # secreto compartido con el bot
FB_PIXEL_ID = os.environ.get("FB_PIXEL_ID", "")
FB_ACCESS_TOKEN = os.environ.get("FB_ACCESS_TOKEN", "")
FB_API_VERSION = os.environ.get("FB_API_VERSION", "v20.0")
LANDING_URL = os.environ.get("LANDING_URL", "palace-landing-chi.vercel.app/")
# En prod, restringí CORS a tu dominio real de landing
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGIN}})

# ---------- Pool de conexiones ----------
# minconn/maxconn moderados: Render + gunicorn con pocos workers no necesita mucho más
pool = SimpleConnectionPool(1, 10, dsn=DATABASE_URL)


def get_db():
    if "db" not in g:
        g.db = pool.getconn()
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        if exception is not None:
            db.rollback()
        pool.putconn(db)


def dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id SERIAL PRIMARY KEY,
                    session_id TEXT UNIQUE NOT NULL,
                    code TEXT UNIQUE NOT NULL,
                    fbp TEXT,
                    fbc TEXT,
                    fbclid TEXT,
                    client_ip TEXT,
                    user_agent TEXT,
                    created_at TEXT NOT NULL,
                    discord_id TEXT,
                    discord_username TEXT,
                    verified_at TEXT,
                    purchased_at TEXT
                )
                """
            )
        conn.commit()
    finally:
        conn.close()


def generate_code(conn, length=4):
    alphabet = string.digits
    with dict_cursor(conn) as cur:
        while True:
            code = "".join(random.choice(alphabet) for _ in range(length))
            cur.execute("SELECT 1 FROM sessions WHERE code = %s", (code,))
            exists = cur.fetchone()
            if not exists:
                return code


def sha256_hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


# ---------- Endpoints usados por la LANDING ----------
@app.route("/api/session", methods=["POST"])
def create_session():
    data = request.get_json(force=True) or {}
    fbp = data.get("fbp")
    fbc = data.get("fbc")
    fbclid = data.get("fbclid")

    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    user_agent = request.headers.get("User-Agent", "")

    conn = get_db()
    session_id = str(uuid.uuid4())
    code = generate_code(conn)
    created_at = datetime.now(timezone.utc).isoformat()

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO sessions
               (session_id, code, fbp, fbc, fbclid, client_ip, user_agent, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (session_id, code, fbp, fbc, fbclid, client_ip, user_agent, created_at),
        )
    conn.commit()

    return jsonify({"session_id": session_id, "code": code})


# ---------- Endpoints usados por el BOT ----------
def check_secret(req):
    return req.headers.get("X-Bot-Secret") == BOT_SECRET


@app.route("/api/verify", methods=["POST"])
def verify_code():
    if not check_secret(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True) or {}
    code = (data.get("code") or "").strip().upper()
    discord_id = str(data.get("discord_id"))
    discord_username = data.get("discord_username", "")

    conn = get_db()
    with dict_cursor(conn) as cur:
        cur.execute("SELECT * FROM sessions WHERE code = %s", (code,))
        row = cur.fetchone()

        if row is None:
            return jsonify({"ok": False, "reason": "code_not_found"}), 404

        if row["verified_at"] is not None:
            return jsonify({"ok": False, "reason": "code_already_used"}), 409

        cur.execute(
            """UPDATE sessions SET discord_id = %s, discord_username = %s, verified_at = %s
               WHERE code = %s""",
            (discord_id, discord_username, datetime.now(timezone.utc).isoformat(), code),
        )
    conn.commit()

    return jsonify({"ok": True})


@app.route("/api/purchase", methods=["POST"])
def register_purchase():
    if not check_secret(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True) or {}
    discord_id = str(data.get("discord_id"))
    value = data.get("value")
    currency = data.get("currency", "ARS")
    order_id = data.get("order_id", str(uuid.uuid4()))
    email = data.get("email")  # opcional, si lo tenés

    conn = get_db()
    with dict_cursor(conn) as cur:
        cur.execute(
            """SELECT * FROM sessions WHERE discord_id = %s AND verified_at IS NOT NULL
               ORDER BY verified_at DESC LIMIT 1""",
            (discord_id,),
        )
        row = cur.fetchone()

        if row is None:
            return jsonify({"ok": False, "reason": "no_verified_session_for_discord_id"}), 404

        result = send_purchase_event(row, value, currency, order_id, email)

        cur.execute(
            "UPDATE sessions SET purchased_at = %s WHERE id = %s",
            (datetime.now(timezone.utc).isoformat(), row["id"]),
        )
    conn.commit()

    return jsonify({"ok": True, "meta_response": result})


def send_purchase_event(row, value, currency, order_id, email=None):
    if not FB_PIXEL_ID or not FB_ACCESS_TOKEN:
        return {"skipped": "FB_PIXEL_ID / FB_ACCESS_TOKEN no configurados"}

    user_data = {
        "client_ip_address": row["client_ip"],
        "client_user_agent": row["user_agent"],
        "external_id": [
            sha256_hash(row["discord_id"])
        ]
    }
    if row["fbp"]:
        user_data["fbp"] = row["fbp"]
    if row["fbc"]:
        user_data["fbc"] = row["fbc"]
    if email:
        user_data["em"] = [sha256_hash(email)]

    payload = {
        "data": [
            {
                "event_name": "Purchase",
                "event_time": int(time.time()),
                "event_id": order_id,  # dedup, por si en algún momento sumás pixel client-side
                "action_source": "website",
                "event_source_url": LANDING_URL,
                "user_data": user_data,
                "custom_data": {
                    "currency": currency,
                    "value": value,
                    "order_id": order_id,
                },
            }
        ],
    }

    url = f"https://graph.facebook.com/{FB_API_VERSION}/{FB_PIXEL_ID}/events"
    resp = requests.post(url, params={"access_token": FB_ACCESS_TOKEN}, json=payload, timeout=10)
    try:
        return resp.json()
    except Exception:
        return {"status_code": resp.status_code, "text": resp.text}


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
else:
    # Cuando corre bajo gunicorn (Render), __main__ nunca se ejecuta,
    # así que inicializamos la DB al importar el módulo.
    init_db()
