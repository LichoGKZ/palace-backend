import os
import sqlite3
import uuid
import random
import string
import hashlib
import time
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify, g
from flask_cors import CORS

# ---------- Configuración ----------
DB_PATH = os.environ.get("DB_PATH", "tracking.db")
BOT_SECRET = os.environ.get("BOT_SECRET", "cambiame")  # secreto compartido con el bot
FB_PIXEL_ID = os.environ.get("FB_PIXEL_ID", "")
FB_ACCESS_TOKEN = os.environ.get("FB_ACCESS_TOKEN", "")
FB_API_VERSION = os.environ.get("FB_API_VERSION", "v20.0")
LANDING_URL = os.environ.get("LANDING_URL", "https://tusitio.com/")
# En prod, restringí CORS a tu dominio real de landing
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGIN}})


# ---------- DB ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    conn.close()


def generate_code(conn, length=4):
    alphabet = string.ascii_uppercase + string.digits
    # evitar caracteres confusos
    alphabet = alphabet.replace("O", "").replace("0", "").replace("I", "").replace("1", "")
    while True:
        code = "".join(random.choice(alphabet) for _ in range(length))
        exists = conn.execute("SELECT 1 FROM sessions WHERE code = ?", (code,)).fetchone()
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

    conn.execute(
        """INSERT INTO sessions
           (session_id, code, fbp, fbc, fbclid, client_ip, user_agent, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
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
    row = conn.execute("SELECT * FROM sessions WHERE code = ?", (code,)).fetchone()

    if row is None:
        return jsonify({"ok": False, "reason": "code_not_found"}), 404

    if row["verified_at"] is not None:
        return jsonify({"ok": False, "reason": "code_already_used"}), 409

    conn.execute(
        """UPDATE sessions SET discord_id = ?, discord_username = ?, verified_at = ?
           WHERE code = ?""",
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
    row = conn.execute(
        """SELECT * FROM sessions WHERE discord_id = ? AND verified_at IS NOT NULL
           ORDER BY verified_at DESC LIMIT 1""",
        (discord_id,),
    ).fetchone()

    if row is None:
        return jsonify({"ok": False, "reason": "no_verified_session_for_discord_id"}), 404

    result = send_purchase_event(row, value, currency, order_id, email)

    conn.execute(
        "UPDATE sessions SET purchased_at = ? WHERE id = ?",
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
        ]
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
