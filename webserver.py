# -*- coding: utf-8 -*-
"""
POS IoT - Webserver Flask
- API REST con JWT (access + refresh, blacklist)
- Argon2 per password, SHA256 per PIN carta
- Rate limit anti bruteforce
- Sito esercente HTML (admin + esercente) con privacy stretta
- Decimal per calcoli monetari, FOR UPDATE su tutte le scritture su saldo
"""

import hashlib
import io
import base64
import os
import re
import secrets
import socket
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps

import jwt
import psycopg2
import psycopg2.extras
import qrcode
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError
from dotenv import load_dotenv
from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def require_env(name):
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Variabile ambiente mancante: {name}")
    return v


# ============================================================
# Config
# ============================================================

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "iot_db"),
    "user":     os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", ""),
}
if not DB_CONFIG["password"]:
    raise RuntimeError("DB_PASSWORD non impostata nel file .env")

JWT_SECRET           = require_env("JWT_SECRET")
FLASK_SECRET_KEY     = require_env("FLASK_SECRET_KEY")
JWT_ALGO             = os.getenv("JWT_ALGO", "HS256")
ACCESS_TOKEN_MINUTES = env_int("ACCESS_TOKEN_MINUTES", 1440)  # 24h per demo
REFRESH_TOKEN_DAYS   = env_int("REFRESH_TOKEN_DAYS", 14)
QR_VALIDITA_SECONDI  = env_int("QR_VALIDITA_SECONDI", 300)
LOGIN_RATE_LIMIT     = os.getenv("LOGIN_RATE_LIMIT", "10 per minute")

CATEGORIE_VALIDE = {
    "shopping", "transport", "food", "entertainment", "health", "travel",
    "utilities", "salary", "transfer", "education", "subscriptions", "other"
}

cors_raw = os.getenv("CORS_ORIGINS", "*").strip()
CORS_ORIGINS = "*" if cors_raw == "*" else [x.strip() for x in cors_raw.split(",") if x.strip()]


def _client_ip():
    xff = request.headers.get("X-Forwarded-For", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return get_remote_address()


# ============================================================
# App
# ============================================================

app = Flask(__name__)

with app.app_context():
    try:
        query("CREATE TABLE IF NOT EXISTS scan_nfc (id SERIAL PRIMARY KEY, stato TEXT NOT NULL DEFAULT 'WAITING', uid TEXT, created_at TIMESTAMP NOT NULL DEFAULT NOW(), scadenza TIMESTAMP NOT NULL DEFAULT NOW() + INTERVAL '60 seconds')")
    except Exception:
        pass

app.secret_key = FLASK_SECRET_KEY
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = env_bool("SESSION_COOKIE_SECURE", False)

CORS(app, resources={r"/api/*": {"origins": CORS_ORIGINS}},
     allow_headers=["Content-Type", "Authorization", "ngrok-skip-browser-warning"])
limiter = Limiter(key_func=_client_ip, app=app, default_limits=[])

ph = PasswordHasher()

# ============================================================
# Helper
# ============================================================

def db():
    return psycopg2.connect(**DB_CONFIG)


def query(sql, params=None, fetch=False, one=False):
    conn = db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        result = None
        if fetch:
            result = cur.fetchone() if one else cur.fetchall()
        conn.commit()
        cur.close()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def hash_pin(testo):
    return hashlib.sha256(testo.encode("utf-8")).hexdigest()


def verify_password(plain, hashed):
    try:
        return ph.verify(hashed, plain)
    except (VerifyMismatchError, InvalidHash):
        return False


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def to_decimal(value, default="0"):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def iban_normalize(iban: str) -> str:
    return iban.replace(" ", "").upper()


def iban_valido(iban):
    cleaned = iban_normalize(iban)
    return bool(re.match(r"^[A-Z]{2}[0-9A-Z]{13,32}$", cleaned))


def genera_iban(user_id: int) -> str:
    return f"IT60POS{user_id:014d}"


# ============================================================
# JWT
# ============================================================

def hash_refresh_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_access_token(user_id):
    now = datetime.now(timezone.utc)
    payload = {
        "sub":  str(user_id),
        "type": "access",
        "jti":  str(uuid.uuid4()),
        "iat":  int(now.timestamp()),
        "exp":  int((now + timedelta(minutes=ACCESS_TOKEN_MINUTES)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def create_refresh_token(user_id):
    raw = secrets.token_urlsafe(48)
    token_hash = hash_refresh_token(raw)
    expires_at = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_DAYS)
    query(
        "INSERT INTO refresh_tokens (user_id, token_hash, expires_at, revoked) VALUES (%s, %s, %s, FALSE)",
        (user_id, token_hash, expires_at),
    )
    return raw


def revoke_refresh_token(raw_token):
    query(
        "UPDATE refresh_tokens SET revoked=TRUE, revoked_at=NOW() WHERE token_hash=%s AND revoked=FALSE",
        (hash_refresh_token(raw_token),),
    )


def revoke_access_token_jti(jti, exp_ts):
    if not jti or not exp_ts:
        return
    exp_dt = datetime.fromtimestamp(exp_ts, tz=timezone.utc).replace(tzinfo=None)
    query(
        "INSERT INTO revoked_access_tokens (jti, expires_at) VALUES (%s, %s) ON CONFLICT (jti) DO NOTHING",
        (jti, exp_dt),
    )


def is_access_token_revoked(jti):
    row = query("SELECT id FROM revoked_access_tokens WHERE jti=%s LIMIT 1",
                (jti,), fetch=True, one=True)
    return row is not None


def cleanup_expired_tokens():
    query("DELETE FROM revoked_access_tokens WHERE expires_at < NOW()")
    query("DELETE FROM refresh_tokens WHERE expires_at < NOW()")


# ============================================================
# Decoratori
# ============================================================

def login_richiesto(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "esercente_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def admin_richiesto(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "esercente_id" not in session:
            return redirect(url_for("login"))
        if session.get("ruolo") != "admin":
            abort(403)
        return f(*args, **kwargs)
    return wrapper


def token_richiesto(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"success": False, "message": "Token mancante"}), 401
        token = auth.split(" ", 1)[1].strip()
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
            if payload.get("type") != "access":
                return jsonify({"success": False, "message": "Token non valido"}), 401
            jti = payload.get("jti")
            if is_access_token_revoked(jti):
                return jsonify({"success": False, "message": "Token revocato"}), 401
            user_id = int(payload["sub"])
            u = query("SELECT id, attiva FROM utenti WHERE id=%s",
                      (user_id,), fetch=True, one=True)
            if not u:
                return jsonify({"success": False, "message": "Utente non trovato"}), 401
            if not u["attiva"] and f.__name__ not in ("api_card_status", "api_me"):
                return jsonify({"success": False, "message": "Carta bloccata"}), 403
            g.user_id = user_id
            g.jwt_payload = payload
        except jwt.ExpiredSignatureError:
            return jsonify({"success": False, "message": "Token scaduto"}), 401
        except Exception:
            return jsonify({"success": False, "message": "Token non valido"}), 401
        return f(*args, **kwargs)
    return wrapper


@app.after_request
def add_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.context_processor
def inject_user():
    return dict(ruolo=session.get("ruolo"), esercente_nome=session.get("esercente_nome"))


# ============================================================
# SITO ESERCENTE (HTML)
# ============================================================

@app.route("/")
def home():
    if "esercente_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
@limiter.limit(LOGIN_RATE_LIMIT, methods=["POST"])
def login():
    errore = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        e = query("SELECT * FROM esercenti WHERE username=%s",
                  (username,), fetch=True, one=True)
        if e and verify_password(password, e["password_hash"]):
            session["esercente_id"]   = e["id"]
            session["esercente_nome"] = e["nome_negozio"]
            session["ruolo"]          = e["ruolo"]
            return redirect(url_for("dashboard"))
        errore = "Credenziali non valide"
    return render_template("login.html", errore=errore)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_richiesto
def dashboard():
    if session.get("ruolo") == "admin":
        return render_template(
            "dashboard.html",
            modalita="admin",
            num_utenti=query("SELECT COUNT(*) AS n FROM utenti",
                             fetch=True, one=True)["n"],
            num_trans=query("SELECT COUNT(*) AS n FROM transazioni WHERE esito='APPROVATA'",
                            fetch=True, one=True)["n"],
            num_ricar=query("SELECT COUNT(*) AS n FROM ricariche WHERE stato='COMPLETATA'",
                            fetch=True, one=True)["n"],
            num_esercenti=query("SELECT COUNT(*) AS n FROM esercenti",
                                fetch=True, one=True)["n"],
        )
    num_proprie = query(
        "SELECT COUNT(*) AS n FROM ricariche WHERE id_esercente=%s AND stato='COMPLETATA'",
        (session["esercente_id"],), fetch=True, one=True)["n"]
    tot_proprie = query(
        "SELECT COALESCE(SUM(importo),0) AS tot FROM ricariche WHERE id_esercente=%s AND stato='COMPLETATA'",
        (session["esercente_id"],), fetch=True, one=True)["tot"]
    pending_proprie = query(
        "SELECT COUNT(*) AS n FROM ricariche WHERE id_esercente=%s AND stato='PENDING' AND scadenza > NOW()",
        (session["esercente_id"],), fetch=True, one=True)["n"]
    return render_template("dashboard.html", modalita="esercente",
                           num_proprie=num_proprie, tot_proprie=float(tot_proprie),
                           pending_proprie=pending_proprie)


@app.route("/utenti")
@admin_richiesto
def utenti():
    # Solo admin, solo ID e nome (privacy: nessun saldo, nessun IBAN, nessuno storico)
    cerca = request.args.get("q", "").strip()
    if cerca:
        lista = query("SELECT id, nome FROM utenti WHERE LOWER(nome) LIKE %s ORDER BY nome",
                      (f"%{cerca.lower()}%",), fetch=True)
    else:
        lista = query("SELECT id, nome FROM utenti ORDER BY nome", fetch=True)
    return render_template("utenti.html", utenti=lista, cerca=cerca)


@app.route("/utenti/nuovo", methods=["GET", "POST"])
@login_richiesto
def nuovo_utente():
    errore = None
    if request.method == "POST":
        uid   = request.form.get("uid", "").strip()
        nome  = request.form.get("nome", "").strip()
        pin   = request.form.get("pin", "").strip()
        saldo = request.form.get("saldo", "0").strip()
        if not uid or not nome:
            errore = "UID e nome sono obbligatori"
        elif len(pin) != 4 or not pin.isdigit():
            errore = "Il PIN deve essere 4 cifre numeriche"
        else:
            try:
                saldo_n = Decimal(saldo)
                if saldo_n < 0:
                    raise ValueError()
            except (ValueError, InvalidOperation):
                errore = "Saldo non valido"
        if errore is None:
            if query("SELECT id FROM utenti WHERE uid=%s", (uid,), fetch=True, one=True):
                errore = "Esiste gia un utente con questo UID"
            else:
                username = nome.lower().replace(" ", ".").replace("'", "")
                base_username = username
                contatore = 1
                while query("SELECT id FROM utenti WHERE username=%s",
                            (username,), fetch=True, one=True):
                    contatore += 1
                    username = f"{base_username}{contatore}"
                # Inserisci utente, recupera id e genera IBAN deterministico
                row = query(
                    """INSERT INTO utenti (uid, nome, pin_hash, saldo, attiva, username, password_hash)
                       VALUES (%s, %s, %s, %s, TRUE, %s, %s) RETURNING id""",
                    (uid, nome, hash_pin(pin), saldo_n, username, ph.hash("password123")),
                    fetch=True, one=True,
                )
                new_id = row["id"]
                query("UPDATE utenti SET iban=%s WHERE id=%s", (genera_iban(new_id), new_id))
                return redirect(url_for("dashboard"))
    return render_template("nuovo_utente.html", errore=errore)

@app.route("/scan/start", methods=["POST"])
@login_richiesto
def scan_start():
    row = query(
        """INSERT INTO scan_nfc (stato, scadenza)
           VALUES ('WAITING', NOW() + INTERVAL '60 seconds')
           RETURNING id""",
        fetch=True, one=True,
    )
    return jsonify({"id": row["id"]})
 
 
@app.route("/scan/stato/<int:scan_id>")
@login_richiesto
def scan_stato(scan_id):
    row = query(
        "SELECT stato, uid, scadenza FROM scan_nfc WHERE id=%s",
        (scan_id,), fetch=True, one=True,
    )
    if not row:
        return jsonify({"stato": "NON_TROVATA"}), 404
    # Controlla scadenza
    if row["stato"] == "WAITING" and row["scadenza"] < datetime.now():
        query("UPDATE scan_nfc SET stato='TIMEOUT' WHERE id=%s", (scan_id,))
        return jsonify({"stato": "TIMEOUT", "uid": None})
    return jsonify({"stato": row["stato"], "uid": row["uid"]})
 
 
@app.route("/scan/annulla/<int:scan_id>", methods=["POST"])
@login_richiesto
def scan_annulla(scan_id):
    query(
        "UPDATE scan_nfc SET stato='ANNULLATA' WHERE id=%s AND stato='WAITING'",
        (scan_id,),
    )
    return jsonify({"ok": True})
 

@app.route("/transazioni")
@admin_richiesto
def transazioni():
    # Log anonimizzato: ID, data, importo, esito. NESSUN saldo, NESSUN nome.
    lista = query(
        "SELECT id, data_ora, importo, esito FROM transazioni ORDER BY data_ora DESC LIMIT 100",
        fetch=True)
    return render_template("transazioni.html", transazioni=lista)


@app.route("/ricarica", methods=["GET", "POST"])
@login_richiesto
def ricarica():
    errore = None
    if request.method == "POST":
        try:
            importo = Decimal(request.form.get("importo", "0"))
            if importo <= 0:
                raise ValueError()
        except (ValueError, InvalidOperation):
            errore = "Importo non valido"
            return render_template("ricarica.html", errore=errore)
        token = secrets.token_urlsafe(16)
        scadenza = datetime.now() + timedelta(seconds=QR_VALIDITA_SECONDI)
        query(
            "INSERT INTO ricariche (token, id_esercente, importo, scadenza) VALUES (%s, %s, %s, %s)",
            (token, session["esercente_id"], importo, scadenza),
        )
        return redirect(url_for("qr_attivo", token=token))
    return render_template("ricarica.html", errore=errore)


@app.route("/qr/<token>")
@login_richiesto
def qr_attivo(token):
    r = query("SELECT * FROM ricariche WHERE token=%s AND id_esercente=%s",
              (token, session["esercente_id"]), fetch=True, one=True)
    if not r:
        abort(404)
    img = qrcode.make(token)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return render_template("qr_attivo.html", ricarica=r, qr_b64=qr_b64,
                           url_pagamento=token, token=token)


@app.route("/qr/<token>/stato")
@login_richiesto
def qr_stato(token):
    r = query(
        "SELECT stato, data_completata FROM ricariche WHERE token=%s AND id_esercente=%s",
        (token, session["esercente_id"]), fetch=True, one=True)
    if not r:
        return jsonify({"errore": "non trovato"}), 404
    return jsonify({
        "stato":           r["stato"],
        "data_completata": r["data_completata"].strftime("%H:%M:%S") if r["data_completata"] else None,
    })


@app.route("/qr/<token>/annulla", methods=["POST"])
@login_richiesto
def qr_annulla(token):
    query(
        "UPDATE ricariche SET stato='ANNULLATA' WHERE token=%s AND id_esercente=%s AND stato='PENDING'",
        (token, session["esercente_id"]),
    )
    return redirect(url_for("ricariche"))


@app.route("/ricariche")
@login_richiesto
def ricariche():
    lista = query(
        """SELECT id, token, importo, stato, data_creazione, data_completata, scadenza
           FROM ricariche WHERE id_esercente=%s
           ORDER BY data_creazione DESC LIMIT 100""",
        (session["esercente_id"],), fetch=True)
    return render_template("ricariche.html", ricariche=lista)

@app.route("/richiesta-pagamento", methods=["GET", "POST"])
@login_richiesto
def richiesta_pagamento():
    errore = None
    if request.method == "POST":
        try:
            importo = Decimal(request.form.get("importo", "0"))
            if importo <= 0:
                raise ValueError()
        except (ValueError, InvalidOperation):
            errore = "Importo non valido"
            return render_template("richiesta_pagamento.html", errore=errore)
        descrizione = request.form.get("descrizione", "Pagamento POS").strip() or "Pagamento POS"
        scadenza = datetime.now() + timedelta(minutes=10)
        row = query(
            """INSERT INTO richieste_pagamento (id_esercente, importo, descrizione, scadenza)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (session["esercente_id"], importo, descrizione, scadenza),
            fetch=True, one=True,
        )
        return redirect(url_for("richiesta_attiva", id=row["id"]))
    return render_template("richiesta_pagamento.html", errore=errore)


@app.route("/richiesta-pagamento/<int:id>")
@login_richiesto
def richiesta_attiva(id):
    r = query(
        """SELECT r.*, e.nome_negozio FROM richieste_pagamento r
           JOIN esercenti e ON e.id = r.id_esercente
           WHERE r.id=%s AND r.id_esercente=%s""",
        (id, session["esercente_id"]), fetch=True, one=True)
    if not r:
        abort(404)
    return render_template("richiesta_attiva.html", richiesta=r)


@app.route("/richiesta-pagamento/<int:id>/stato")
@login_richiesto
def richiesta_stato(id):
    r = query(
        """SELECT stato, data_completata FROM richieste_pagamento
           WHERE id=%s AND id_esercente=%s""",
        (id, session["esercente_id"]), fetch=True, one=True)
    if not r:
        return jsonify({"errore": "non trovato"}), 404
    return jsonify({
        "stato": r["stato"],
        "data_completata": r["data_completata"].strftime("%H:%M:%S") if r["data_completata"] else None,
    })


@app.route("/richiesta-pagamento/<int:id>/annulla", methods=["POST"])
@login_richiesto
def richiesta_annulla(id):
    query(
        """UPDATE richieste_pagamento SET stato='ANNULLATA'
           WHERE id=%s AND id_esercente=%s AND stato IN ('PENDING','IN_CORSO')""",
        (id, session["esercente_id"]))
    return redirect(url_for("pagamenti"))


@app.route("/pagamenti")
@login_richiesto
def pagamenti():
    lista = query(
        """SELECT id, importo, descrizione, stato, data_creazione, data_completata
           FROM richieste_pagamento WHERE id_esercente=%s
           ORDER BY data_creazione DESC LIMIT 100""",
        (session["esercente_id"],), fetch=True)
    return render_template("pagamenti.html", pagamenti=lista)

@app.route("/pay/<token>", methods=["GET", "POST"])
def pay_cliente(token):
    # Fallback web: UID+PIN per chi non usa l'app Flutter
    r = query(
        """SELECT r.*, e.nome_negozio FROM ricariche r
           JOIN esercenti e ON e.id = r.id_esercente WHERE r.token=%s""",
        (token,), fetch=True, one=True)
    if not r:
        return render_template("pay_cliente.html", errore="Token non valido", ricarica=None)
    if r["stato"] != "PENDING":
        return render_template("pay_cliente.html",
                               errore="Ricarica gia completata o annullata", ricarica=r)
    if r["scadenza"] < datetime.now():
        query("UPDATE ricariche SET stato='SCADUTA' WHERE id=%s", (r["id"],))
        return render_template("pay_cliente.html", errore="Ricarica scaduta", ricarica=r)

    if request.method == "POST":
        uid = request.form.get("uid", "").strip()
        pin = request.form.get("pin", "").strip()
        u = query("SELECT * FROM utenti WHERE uid=%s", (uid,), fetch=True, one=True)
        if not u:
            return render_template("pay_cliente.html", errore="UID non trovato", ricarica=r)
        if u["pin_hash"] != hash_pin(pin):
            return render_template("pay_cliente.html", errore="PIN errato", ricarica=r)
        if not u["attiva"]:
            return render_template("pay_cliente.html", errore="Carta bloccata", ricarica=r)
        importo = Decimal(str(r["importo"]))
        saldo_prima = Decimal(str(u["saldo"]))
        saldo_dopo = saldo_prima + importo
        query("UPDATE utenti SET saldo=%s WHERE id=%s", (saldo_dopo, u["id"]))
        query(
            """UPDATE ricariche SET stato='COMPLETATA', id_utente_riceve=%s, data_completata=NOW()
               WHERE id=%s""",
            (u["id"], r["id"]),
        )
        query(
            """INSERT INTO transazioni (id_utente, uid_carta, nome_utente, titolo, tipo, categoria,
               importo, saldo_prima, saldo_dopo, esito)
               VALUES (%s,%s,%s,%s,'income','transfer',%s,%s,%s,'APPROVATA')""",
            (u["id"], u["uid"], u["nome"], f"Ricarica QR - {r['nome_negozio']}",
             importo, saldo_prima, saldo_dopo),
        )
        return render_template("pay_cliente.html", successo=True, ricarica=r,
                               nome_utente=u["nome"], nuovo_saldo=float(saldo_dopo))
    return render_template("pay_cliente.html", ricarica=r)


# ============================================================
# API REST per app Flutter
# ============================================================

@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"ok": True, "service": "pos-iot-api"})


@app.route("/api/login", methods=["POST"])
@limiter.limit(LOGIN_RATE_LIMIT)
def api_login():
    data = request.get_json() or {}
    username = str(data.get("username", "")).strip().lower()
    password = str(data.get("password", ""))
    if not username or not password:
        return jsonify({"success": False, "message": "Username/password mancanti"}), 400
    u = query(
        "SELECT id, nome, saldo, username, password_hash, attiva FROM utenti WHERE username=%s",
        (username,), fetch=True, one=True)
    if not u or not verify_password(password, u["password_hash"]):
        return jsonify({"success": False, "message": "Credenziali non valide"}), 401
    if not u["attiva"]:
        return jsonify({"success": False, "message": "Carta bloccata"}), 403
    return jsonify({
        "success":       True,
        "token":         create_access_token(int(u["id"])),
        "refresh_token": create_refresh_token(int(u["id"])),
        "user": {
            "id":       u["id"],
            "name":     u["nome"],
            "username": u["username"],
            "balance":  float(u["saldo"]),
        },
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    data = request.get_json() or {}
    raw_refresh = str(data.get("refresh_token", "")).strip()
    if not raw_refresh:
        return jsonify({"success": False, "message": "Refresh token mancante"}), 400
    row = query(
        "SELECT id, user_id, expires_at, revoked FROM refresh_tokens WHERE token_hash=%s",
        (hash_refresh_token(raw_refresh),), fetch=True, one=True)
    if not row or row["revoked"]:
        return jsonify({"success": False, "message": "Refresh token non valido"}), 401
    if row["expires_at"] < datetime.utcnow():
        return jsonify({"success": False, "message": "Refresh token scaduto"}), 401
    query("UPDATE refresh_tokens SET revoked=TRUE, revoked_at=NOW(), last_used_at=NOW() WHERE id=%s",
          (row["id"],))
    return jsonify({
        "success":       True,
        "token":         create_access_token(int(row["user_id"])),
        "refresh_token": create_refresh_token(int(row["user_id"])),
    })


@app.route("/api/logout", methods=["POST"])
@token_richiesto
def api_logout():
    payload = g.jwt_payload
    revoke_access_token_jti(payload.get("jti"), payload.get("exp"))
    data = request.get_json(silent=True) or {}
    raw_refresh = str(data.get("refresh_token", "")).strip()
    if raw_refresh:
        revoke_refresh_token(raw_refresh)
    return jsonify({"success": True})


@app.route("/api/me", methods=["GET"])
@token_richiesto
def api_me():
    u = query(
        "SELECT id, nome, username, saldo, iban, attiva FROM utenti WHERE id=%s",
        (g.user_id,), fetch=True, one=True)
    if not u:
        return jsonify({"success": False, "message": "Utente non trovato"}), 404
    # Se per qualche ragione l'IBAN manca (utenti vecchi), lo genera ora
    iban = u["iban"] or genera_iban(int(u["id"]))
    if not u["iban"]:
        query("UPDATE utenti SET iban=%s WHERE id=%s", (iban, u["id"]))
    return jsonify({
        "id":          u["id"],
        "name":        u["nome"],
        "username":    u["username"],
        "balance":     float(u["saldo"]),
        "iban":        iban,
        "card_active": bool(u["attiva"]),
    })


@app.route("/api/balance", methods=["GET"])
@token_richiesto
def api_balance():
    u = query("SELECT saldo FROM utenti WHERE id=%s", (g.user_id,), fetch=True, one=True)
    if not u:
        return jsonify({"success": False, "message": "Utente non trovato"}), 404
    stats = query(
        """SELECT
              COALESCE(SUM(CASE WHEN tipo='income'  THEN importo ELSE 0 END),0) AS income_month,
              COALESCE(SUM(CASE WHEN tipo='expense' THEN importo ELSE 0 END),0) AS expense_month
           FROM transazioni
           WHERE id_utente=%s
             AND date_trunc('month', data_ora)=date_trunc('month', NOW())
             AND esito='APPROVATA'""",
        (g.user_id,), fetch=True, one=True)
    return jsonify({
        "balance":       float(u["saldo"]),
        "income_month":  float(stats["income_month"]),
        "expense_month": float(stats["expense_month"]),
    })


@app.route("/api/transactions", methods=["GET"])
@token_richiesto
def api_transactions():
    rows = query(
        """SELECT titolo, importo, data_ora, tipo, categoria FROM transazioni
           WHERE id_utente=%s AND esito='APPROVATA'
           ORDER BY data_ora DESC LIMIT 200""",
        (g.user_id,), fetch=True)
    return jsonify([{
        "title":    r["titolo"],
        "amount":   float(r["importo"]),
        "date":     r["data_ora"].isoformat(),
        "type":     r["tipo"]     if r["tipo"]     in ("income", "expense") else "expense",
        "category": r["categoria"] if r["categoria"] in CATEGORIE_VALIDE       else "other",
    } for r in rows])


@app.route("/api/qr/<token>", methods=["GET"])
@token_richiesto
def api_qr_info(token):
    r = query(
        """SELECT r.id, r.importo, r.stato, r.scadenza, e.nome_negozio FROM ricariche r
           JOIN esercenti e ON e.id=r.id_esercente WHERE r.token=%s""",
        (token,), fetch=True, one=True)
    if not r:
        return jsonify({"success": False, "message": "QR non trovato"}), 404
    if r["stato"] != "PENDING":
        return jsonify({"success": False, "message": "QR non disponibile"}), 400
    if r["scadenza"] < datetime.now():
        query("UPDATE ricariche SET stato='SCADUTA' WHERE id=%s", (r["id"],))
        return jsonify({"success": False, "message": "QR scaduto"}), 400
    return jsonify({"amount": float(r["importo"]), "merchant_name": r["nome_negozio"]})


@app.route("/api/qr/confirm", methods=["POST"])
@token_richiesto
def api_qr_confirm():
    data = request.get_json() or {}
    qr_token = str(data.get("qr_token", "")).strip()
    if not qr_token:
        return jsonify({"success": False, "message": "qr_token mancante"}), 400
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT id, uid, nome, saldo, attiva FROM utenti WHERE id=%s FOR UPDATE",
                    (g.user_id,))
        u = cur.fetchone()
        if not u or not u["attiva"]:
            conn.rollback()
            return jsonify({"success": False, "message": "Utente non valido o carta bloccata"}), 403
        cur.execute(
            """SELECT r.id, r.importo, r.stato, r.scadenza, e.nome_negozio FROM ricariche r
               JOIN esercenti e ON e.id=r.id_esercente WHERE r.token=%s FOR UPDATE""",
            (qr_token,))
        r = cur.fetchone()
        if not r:
            conn.rollback()
            return jsonify({"success": False, "message": "QR non trovato"}), 404
        if r["stato"] != "PENDING":
            conn.rollback()
            return jsonify({"success": False, "message": "QR non disponibile"}), 400
        if r["scadenza"] < datetime.now():
            cur.execute("UPDATE ricariche SET stato='SCADUTA' WHERE id=%s", (r["id"],))
            conn.commit()
            return jsonify({"success": False, "message": "QR scaduto"}), 400
        saldo_prima = Decimal(str(u["saldo"]))
        importo = Decimal(str(r["importo"]))
        saldo_dopo = saldo_prima + importo
        cur.execute("UPDATE utenti SET saldo=%s WHERE id=%s", (saldo_dopo, g.user_id))
        cur.execute(
            "UPDATE ricariche SET stato='COMPLETATA', id_utente_riceve=%s, data_completata=NOW() WHERE id=%s",
            (g.user_id, r["id"]))
        cur.execute(
            """INSERT INTO transazioni (id_utente, uid_carta, nome_utente, titolo, tipo, categoria,
               importo, saldo_prima, saldo_dopo, esito)
               VALUES (%s,%s,%s,%s,'income','transfer',%s,%s,%s,'APPROVATA')""",
            (g.user_id, u["uid"], u["nome"], f"Ricarica QR - {r['nome_negozio']}",
             importo, saldo_prima, saldo_dopo))
        conn.commit()
        return jsonify({"success": True, "amount": float(importo),
                        "merchant_name": r["nome_negozio"], "new_balance": float(saldo_dopo)})
    except Exception:
        conn.rollback()
        return jsonify({"success": False, "message": "Errore interno"}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/api/card/status", methods=["POST"])
@token_richiesto
def api_card_status():
    data = request.get_json() or {}
    active = data.get("active")
    if active is None or not isinstance(active, bool):
        return jsonify({"success": False, "message": "active deve essere boolean"}), 400
    query("UPDATE utenti SET attiva=%s WHERE id=%s", (active, g.user_id))
    return jsonify({"success": True, "active": active})


@app.route("/api/transfer", methods=["POST"])
@token_richiesto
def api_transfer():
    """
    Bonifico: se l'IBAN destinatario appartiene a un utente del sistema,
    accredita il suo saldo + registra transazione income.
    Tutto in unica transazione DB con FOR UPDATE su mittente e destinatario,
    ordinati per id per evitare deadlock.
    """
    data = request.get_json() or {}
    beneficiary = str(data.get("beneficiary", "")).strip()
    iban_raw = str(data.get("iban", "")).strip()
    iban   = iban_normalize(iban_raw)
    reason = str(data.get("reason", "")).strip()
    amount = to_decimal(data.get("amount"), "0")
    if not beneficiary or not iban or not reason:
        return jsonify({"success": False, "message": "Campi obbligatori mancanti"}), 400
    if not iban_valido(iban):
        return jsonify({"success": False, "message": "IBAN non valido"}), 400
    if amount <= Decimal("0"):
        return jsonify({"success": False, "message": "Importo non valido"}), 400

    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # 1) Identifica eventuale destinatario interno (senza lock)
        cur.execute("SELECT id FROM utenti WHERE iban=%s", (iban,))
        dest = cur.fetchone()
        dest_id = int(dest["id"]) if dest else None

        # Impedisci bonifici a se stessi (didattico: no-op che confonde)
        if dest_id == g.user_id:
            conn.rollback()
            return jsonify({"success": False,
                            "message": "Non puoi inviare un bonifico a te stesso"}), 400

        # 2) Lock ordinato per evitare deadlock se entrambi interni
        ids_da_bloccare = sorted({g.user_id, dest_id}) if dest_id else [g.user_id]
        cur.execute(
            f"SELECT id, uid, nome, saldo, attiva FROM utenti "
            f"WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            (ids_da_bloccare,))
        rows = {row["id"]: row for row in cur.fetchall()}
        mittente = rows.get(g.user_id)
        if not mittente or not mittente["attiva"]:
            conn.rollback()
            return jsonify({"success": False, "message": "Utente non valido o carta bloccata"}), 403

        saldo_m_prima = Decimal(str(mittente["saldo"]))
        if saldo_m_prima < amount:
            conn.rollback()
            return jsonify({"success": False, "message": "Fondi insufficienti"}), 400

        saldo_m_dopo = saldo_m_prima - amount

        # 3) Scala il mittente
        cur.execute("UPDATE utenti SET saldo=%s WHERE id=%s", (saldo_m_dopo, g.user_id))

        # 4) Salva il bonifico
        cur.execute(
            """INSERT INTO bonifici
               (id_utente, beneficiario, iban_destinatario, causale, importo, stato, id_utente_riceve)
               VALUES (%s,%s,%s,%s,%s,'COMPLETATO',%s)
               RETURNING id, creato_il""",
            (g.user_id, beneficiary, iban, reason, amount, dest_id))
        b = cur.fetchone()

        # 5) Movimento del mittente (uscita)
        cur.execute(
            """INSERT INTO transazioni (id_utente, uid_carta, nome_utente, titolo, tipo, categoria,
               importo, saldo_prima, saldo_dopo, esito)
               VALUES (%s,%s,%s,%s,'expense','transfer',%s,%s,%s,'APPROVATA')""",
            (g.user_id, mittente["uid"], mittente["nome"], f"Bonifico a {beneficiary}",
             amount, saldo_m_prima, saldo_m_dopo))

        # 6) Se destinatario interno: accredita e registra entrata
        if dest_id:
            dest_row = rows.get(dest_id)
            saldo_d_prima = Decimal(str(dest_row["saldo"]))
            saldo_d_dopo  = saldo_d_prima + amount
            cur.execute("UPDATE utenti SET saldo=%s WHERE id=%s", (saldo_d_dopo, dest_id))
            cur.execute(
                """INSERT INTO transazioni (id_utente, uid_carta, nome_utente, titolo, tipo, categoria,
                   importo, saldo_prima, saldo_dopo, esito)
                   VALUES (%s,%s,%s,%s,'income','transfer',%s,%s,%s,'APPROVATA')""",
                (dest_id, dest_row["uid"], dest_row["nome"],
                 f"Bonifico da {mittente['nome']}",
                 amount, saldo_d_prima, saldo_d_dopo))

        conn.commit()
        return jsonify({
            "success":     True,
            "transfer_id": b["id"],
            "created_at":  b["creato_il"].isoformat(),
            "new_balance": float(saldo_m_dopo),
            "internal":    dest_id is not None,
        })
    except Exception:
        conn.rollback()
        return jsonify({"success": False, "message": "Errore interno"}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/api/nfc/pay", methods=["POST"])
@token_richiesto
def api_nfc_pay():
    data = request.get_json() or {}
    merchant_name = str(data.get("merchant_name", "POS NFC")).strip()
    amount = to_decimal(data.get("amount"), "0")
    nfc_token = str(data.get("nfc_token", "")).strip()
    if not nfc_token:
        return jsonify({"success": False, "message": "nfc_token mancante"}), 400
    if amount <= Decimal("0"):
        return jsonify({"success": False, "message": "Importo non valido"}), 400
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT id, uid, nome, saldo, attiva FROM utenti WHERE id=%s FOR UPDATE",
                    (g.user_id,))
        u = cur.fetchone()
        if not u or not u["attiva"]:
            conn.rollback()
            return jsonify({"success": False, "message": "Utente non valido o carta bloccata"}), 403
        saldo_prima = Decimal(str(u["saldo"]))
        if saldo_prima < amount:
            conn.rollback()
            return jsonify({"success": False, "message": "Fondi insufficienti"}), 400
        saldo_dopo = saldo_prima - amount
        cur.execute("UPDATE utenti SET saldo=%s WHERE id=%s", (saldo_dopo, g.user_id))
        cur.execute(
            """INSERT INTO transazioni (id_utente, uid_carta, nome_utente, titolo, tipo, categoria,
               importo, saldo_prima, saldo_dopo, esito)
               VALUES (%s,%s,%s,%s,'expense','other',%s,%s,%s,'APPROVATA')""",
            (g.user_id, u["uid"], u["nome"], f"Pagamento NFC - {merchant_name}",
             amount, saldo_prima, saldo_dopo))
        conn.commit()
        return jsonify({"success": True, "new_balance": float(saldo_dopo)})
    except Exception:
        conn.rollback()
        return jsonify({"success": False, "message": "Errore interno"}), 500
    finally:
        cur.close()
        conn.close()


# ============================================================
# Errori
# ============================================================

@app.errorhandler(403)
def forbidden(_e):
    return render_template("errore.html", codice=403, messaggio="Accesso negato"), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("errore.html", codice=404, messaggio="Pagina non trovata"), 404


# ============================================================
# Avvio
# ============================================================

if __name__ == "__main__":
    cleanup_expired_tokens()
    ip = get_local_ip()
    host  = os.getenv("HOST", "0.0.0.0")
    port  = env_int("PORT", 5000)
    debug = env_bool("FLASK_DEBUG", False)
    print("=" * 60)
    print("POS IoT - Flask API + Web (sicurezza completa)")
    print("=" * 60)
    print(f"Sito esercente:  http://{ip}:{port}")
    print(f"API base URL:    http://{ip}:{port}/api")
    print()
    print("Login esercente:")
    print("  admin / admin123  (amministratore)")
    print("  mario / mario123  (esercente)")
    print()
    print("Login app Flutter (clienti):")
    print("  mario.rossi  / password123")
    print("  luca.bianchi / password123")
    print("  anna.verdi   / password123")
    print("=" * 60)
    app.run(host=host, port=port, debug=debug)