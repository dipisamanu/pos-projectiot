# -*- coding: utf-8 -*-
"""
Server web POS IoT - versione privacy-oriented
Differenza per ruolo:
  - admin:     vede statistiche generali, lista nomi utenti, transazioni anonimizzate
  - esercente: vede solo le PROPRIE ricariche e puo registrare nuovi utenti
Nessun ruolo vede saldi altrui o storico transazioni dettagliato dei singoli utenti.
"""

import hashlib
import secrets
import socket
import io
import base64
from datetime import datetime, timedelta
from functools import wraps

import psycopg2
import psycopg2.extras
import qrcode
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, abort
from flask_cors import CORS


DB_CONFIG = {
    'host': 'localhost', 'database': 'iot_db',
    'user': 'admin',     'password': 'password'
}
QR_VALIDITA_SECONDI = 300

app = Flask(__name__)
app.secret_key = 'cambiami-in-produzione-progetto-pos-iot-2026'
CORS(app)


def hash_pwd(testo):
    return hashlib.sha256(testo.encode()).hexdigest()


def db():
    return psycopg2.connect(**DB_CONFIG)


def query(sql, params=None, fetch=False, one=False):
    conn = db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params or ())
    result = None
    if fetch:
        result = cur.fetchone() if one else cur.fetchall()
    conn.commit()
    cur.close()
    conn.close()
    return result


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


def login_richiesto(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'esercente_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def admin_richiesto(f):
    # Blocca accesso alle route admin-only
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'esercente_id' not in session:
            return redirect(url_for('login'))
        if session.get('ruolo') != 'admin':
            abort(403)
        return f(*args, **kwargs)
    return wrapper


# Rende la variabile ruolo disponibile in tutti i template
@app.context_processor
def inject_user():
    return dict(
        ruolo=session.get('ruolo'),
        esercente_nome=session.get('esercente_nome')
    )


# ============================================================
# Auth
# ============================================================

@app.route('/')
def home():
    if 'esercente_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    errore = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        e = query('SELECT * FROM esercenti WHERE username = %s',
                  (username,), fetch=True, one=True)
        if e and e['password_hash'] == hash_pwd(password):
            session['esercente_id']   = e['id']
            session['esercente_nome'] = e['nome_negozio']
            session['ruolo']          = e['ruolo']
            return redirect(url_for('dashboard'))
        errore = 'Credenziali non valide'
    return render_template('login.html', errore=errore)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ============================================================
# Dashboard (vista diversa per admin / esercente)
# ============================================================

@app.route('/dashboard')
@login_richiesto
def dashboard():
    if session.get('ruolo') == 'admin':
        # Admin vede statistiche aggregate (numeri, no dati personali)
        return render_template(
            'dashboard.html',
            modalita='admin',
            num_utenti=query('SELECT COUNT(*) AS n FROM utenti', fetch=True, one=True)['n'],
            num_trans=query('SELECT COUNT(*) AS n FROM transazioni', fetch=True, one=True)['n'],
            num_ricar=query("SELECT COUNT(*) AS n FROM ricariche WHERE stato='COMPLETATA'",
                            fetch=True, one=True)['n'],
            num_esercenti=query('SELECT COUNT(*) AS n FROM esercenti', fetch=True, one=True)['n']
        )

    # Esercente normale: solo statistiche proprie
    num_proprie = query(
        "SELECT COUNT(*) AS n FROM ricariche WHERE id_esercente=%s AND stato='COMPLETATA'",
        (session['esercente_id'],), fetch=True, one=True
    )['n']
    tot_proprie = query(
        "SELECT COALESCE(SUM(importo), 0) AS tot FROM ricariche WHERE id_esercente=%s AND stato='COMPLETATA'",
        (session['esercente_id'],), fetch=True, one=True
    )['tot']
    pending_proprie = query(
        "SELECT COUNT(*) AS n FROM ricariche WHERE id_esercente=%s AND stato='PENDING' AND scadenza > NOW()",
        (session['esercente_id'],), fetch=True, one=True
    )['n']
    return render_template(
        'dashboard.html',
        modalita='esercente',
        num_proprie=num_proprie,
        tot_proprie=float(tot_proprie),
        pending_proprie=pending_proprie
    )


# ============================================================
# Utenti (solo admin vede la lista, e solo i nomi)
# ============================================================

@app.route('/utenti')
@admin_richiesto
def utenti():
    cerca = request.args.get('q', '').strip()
    if cerca:
        # Solo nome, niente UID né saldo
        lista = query(
            "SELECT id, nome FROM utenti WHERE LOWER(nome) LIKE %s ORDER BY nome",
            (f'%{cerca.lower()}%',), fetch=True
        )
    else:
        lista = query('SELECT id, nome FROM utenti ORDER BY nome', fetch=True)
    return render_template('utenti.html', utenti=lista, cerca=cerca)


@app.route('/utenti/nuovo', methods=['GET', 'POST'])
@login_richiesto
def nuovo_utente():
    # Sia admin sia esercente possono registrare un nuovo cliente
    # perche e l'esercente che gli assegna fisicamente la carta NFC
    errore = None
    if request.method == 'POST':
        uid   = request.form.get('uid', '').strip()
        nome  = request.form.get('nome', '').strip()
        pin   = request.form.get('pin', '').strip()
        saldo = request.form.get('saldo', '0').strip()
        if not uid or not nome:
            errore = 'UID e nome sono obbligatori'
        elif len(pin) != 4 or not pin.isdigit():
            errore = 'Il PIN deve essere 4 cifre numeriche'
        else:
            try:
                saldo_n = float(saldo)
                if saldo_n < 0: raise ValueError()
            except ValueError:
                errore = 'Saldo non valido'
        if errore is None:
            if query('SELECT id FROM utenti WHERE uid=%s', (uid,), fetch=True, one=True):
                errore = 'Esiste gia un utente con questo UID'
            else:
                query(
                    'INSERT INTO utenti (uid, nome, pin_hash, saldo) VALUES (%s, %s, %s, %s)',
                    (uid, nome, hash_pwd(pin), saldo_n)
                )
                return redirect(url_for('dashboard'))
    return render_template('nuovo_utente.html', errore=errore)


# ============================================================
# Transazioni POS (solo admin, dati anonimizzati)
# ============================================================

@app.route('/transazioni')
@admin_richiesto
def transazioni():
    # Solo ID, data, importo ed esito. NO nome utente, NO saldi prima/dopo
    lista = query(
        '''SELECT id, data_ora, importo, esito
           FROM transazioni ORDER BY data_ora DESC LIMIT 100''',
        fetch=True
    )
    return render_template('transazioni.html', transazioni=lista)


# ============================================================
# Ricariche (ognuno vede le proprie)
# ============================================================

@app.route('/ricarica', methods=['GET', 'POST'])
@login_richiesto
def ricarica():
    errore = None
    if request.method == 'POST':
        try:
            importo = float(request.form.get('importo', '0'))
            if importo <= 0: raise ValueError()
        except ValueError:
            errore = 'Importo non valido'
            return render_template('ricarica.html', errore=errore)
        token    = secrets.token_urlsafe(16)
        scadenza = datetime.now() + timedelta(seconds=QR_VALIDITA_SECONDI)
        query(
            'INSERT INTO ricariche (token, id_esercente, importo, scadenza) VALUES (%s, %s, %s, %s)',
            (token, session['esercente_id'], importo, scadenza)
        )
        return redirect(url_for('qr_attivo', token=token))
    return render_template('ricarica.html', errore=errore)


@app.route('/qr/<token>')
@login_richiesto
def qr_attivo(token):
    r = query(
        'SELECT * FROM ricariche WHERE token = %s AND id_esercente = %s',
        (token, session['esercente_id']), fetch=True, one=True
    )
    if not r:
        abort(404)
    url_pagamento = f'http://{get_local_ip()}:5000/pay/{token}'
    img = qrcode.make(url_pagamento)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    return render_template('qr_attivo.html', ricarica=r, qr_b64=qr_b64,
                           url_pagamento=url_pagamento, token=token)


@app.route('/qr/<token>/stato')
@login_richiesto
def qr_stato(token):
    # NON espone il nome dell'utente che ha completato (privacy)
    r = query(
        '''SELECT stato, data_completata FROM ricariche
           WHERE token=%s AND id_esercente=%s''',
        (token, session['esercente_id']), fetch=True, one=True
    )
    if not r:
        return jsonify({'errore': 'non trovato'}), 404
    return jsonify({
        'stato':           r['stato'],
        'data_completata': r['data_completata'].strftime('%H:%M:%S') if r['data_completata'] else None
    })


@app.route('/qr/<token>/annulla', methods=['POST'])
@login_richiesto
def qr_annulla(token):
    query(
        """UPDATE ricariche SET stato='ANNULLATA'
           WHERE token=%s AND id_esercente=%s AND stato='PENDING'""",
        (token, session['esercente_id'])
    )
    return redirect(url_for('ricariche'))


@app.route('/ricariche')
@login_richiesto
def ricariche():
    # NON include nome del destinatario per privacy
    lista = query(
        '''SELECT id, token, importo, stato, data_creazione, data_completata, scadenza
           FROM ricariche WHERE id_esercente=%s
           ORDER BY data_creazione DESC LIMIT 100''',
        (session['esercente_id'],), fetch=True
    )
    return render_template('ricariche.html', ricariche=lista)


# ============================================================
# Pagina cliente (mobile, accessibile via QR scan)
# ============================================================

@app.route('/pay/<token>', methods=['GET', 'POST'])
def pay_cliente(token):
    r = query(
        '''SELECT r.*, e.nome_negozio FROM ricariche r
           JOIN esercenti e ON e.id = r.id_esercente WHERE r.token = %s''',
        (token,), fetch=True, one=True
    )
    if not r:
        return render_template('pay_cliente.html', errore='Token non valido', ricarica=None)
    if r['stato'] != 'PENDING':
        return render_template('pay_cliente.html',
                               errore='Questa ricarica e gia stata gestita o annullata',
                               ricarica=r)
    if r['scadenza'] < datetime.now():
        query("UPDATE ricariche SET stato='SCADUTA' WHERE id=%s", (r['id'],))
        return render_template('pay_cliente.html', errore='Ricarica scaduta', ricarica=r)

    if request.method == 'POST':
        uid = request.form.get('uid', '').strip()
        pin = request.form.get('pin', '').strip()
        utente = query('SELECT * FROM utenti WHERE uid=%s', (uid,), fetch=True, one=True)
        if not utente:
            return render_template('pay_cliente.html', errore='UID non trovato', ricarica=r)
        if utente['pin_hash'] != hash_pwd(pin):
            return render_template('pay_cliente.html', errore='PIN errato', ricarica=r)
        nuovo_saldo = float(utente['saldo']) + float(r['importo'])
        query('UPDATE utenti SET saldo=%s WHERE id=%s', (nuovo_saldo, utente['id']))
        query(
            '''UPDATE ricariche SET stato='COMPLETATA', id_utente_riceve=%s, data_completata=NOW()
               WHERE id=%s''',
            (utente['id'], r['id'])
        )
        return render_template('pay_cliente.html', successo=True, ricarica=r,
                               nome_utente=utente['nome'], nuovo_saldo=nuovo_saldo)
    return render_template('pay_cliente.html', ricarica=r)


# ============================================================
# API REST per app Flutter (il cliente vede SOLO i suoi dati)
# ============================================================

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    uid  = str(data.get('uid', '')).strip()
    pin  = str(data.get('pin', '')).strip()
    u = query('SELECT id, uid, nome, pin_hash, saldo FROM utenti WHERE uid=%s',
              (uid,), fetch=True, one=True)
    if not u or u['pin_hash'] != hash_pwd(pin):
        return jsonify({'success': False, 'errore': 'Credenziali non valide'}), 401
    return jsonify({
        'success': True,
        'utente': {'id': u['id'], 'uid': u['uid'], 'nome': u['nome'], 'saldo': float(u['saldo'])}
    })


@app.route('/api/saldo', methods=['POST'])
def api_saldo():
    data = request.get_json() or {}
    u = query('SELECT pin_hash, saldo FROM utenti WHERE uid=%s',
              (str(data.get('uid', '')),), fetch=True, one=True)
    if not u or u['pin_hash'] != hash_pwd(str(data.get('pin', ''))):
        return jsonify({'success': False}), 401
    return jsonify({'success': True, 'saldo': float(u['saldo'])})


@app.route('/api/transazioni', methods=['POST'])
def api_transazioni():
    data = request.get_json() or {}
    uid  = str(data.get('uid', ''))
    u = query('SELECT pin_hash FROM utenti WHERE uid=%s', (uid,), fetch=True, one=True)
    if not u or u['pin_hash'] != hash_pwd(str(data.get('pin', ''))):
        return jsonify({'success': False}), 401
    trans = query(
        '''SELECT data_ora, importo, saldo_dopo, esito FROM transazioni
           WHERE uid_carta=%s ORDER BY data_ora DESC LIMIT 50''',
        (uid,), fetch=True
    )
    for t in trans:
        t['data_ora']   = t['data_ora'].strftime('%Y-%m-%d %H:%M:%S')
        t['importo']    = float(t['importo'])
        t['saldo_dopo'] = float(t['saldo_dopo'])
    return jsonify({'success': True, 'transazioni': trans})


@app.route('/api/ricarica', methods=['POST'])
def api_ricarica():
    data  = request.get_json() or {}
    token = data.get('token', '')
    uid   = str(data.get('uid', ''))
    pin   = str(data.get('pin', ''))
    u = query('SELECT * FROM utenti WHERE uid=%s', (uid,), fetch=True, one=True)
    if not u or u['pin_hash'] != hash_pwd(pin):
        return jsonify({'success': False, 'errore': 'Credenziali non valide'}), 401
    r = query('SELECT * FROM ricariche WHERE token=%s', (token,), fetch=True, one=True)
    if not r:
        return jsonify({'success': False, 'errore': 'Token non valido'}), 404
    if r['stato'] != 'PENDING':
        return jsonify({'success': False, 'errore': 'Ricarica gia usata o annullata'}), 400
    if r['scadenza'] < datetime.now():
        query("UPDATE ricariche SET stato='SCADUTA' WHERE id=%s", (r['id'],))
        return jsonify({'success': False, 'errore': 'Ricarica scaduta'}), 400
    nuovo_saldo = float(u['saldo']) + float(r['importo'])
    query('UPDATE utenti SET saldo=%s WHERE id=%s', (nuovo_saldo, u['id']))
    query(
        """UPDATE ricariche SET stato='COMPLETATA', id_utente_riceve=%s, data_completata=NOW()
           WHERE id=%s""",
        (u['id'], r['id'])
    )
    return jsonify({'success': True, 'importo': float(r['importo']), 'nuovo_saldo': nuovo_saldo})


# ============================================================
# Gestione errori
# ============================================================

@app.errorhandler(403)
def forbidden(e):
    return render_template('errore.html', codice=403,
                           messaggio='Accesso negato: questa pagina e riservata agli amministratori'), 403

@app.errorhandler(404)
def not_found(e):
    return render_template('errore.html', codice=404,
                           messaggio='Pagina non trovata'), 404


if __name__ == '__main__':
    ip = get_local_ip()
    print('=' * 50)
    print('POS IoT - Webserver Flask (privacy mode)')
    print('=' * 50)
    print(f'Indirizzo: http://{ip}:5000')
    print('Admin:     admin / admin123')
    print('Esercente: mario / mario123')
    print('=' * 50)
    app.run(host='0.0.0.0', port=5000, debug=True)