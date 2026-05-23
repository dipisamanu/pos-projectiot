# -*- coding: utf-8 -*-
"""
Server web del POS IoT - versione 2
Aggiunte: dettaglio utente, modifica saldo, storico ricariche,
annulla QR, polling stato QR, cerca utenti.
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
    'host':     'localhost',
    'database': 'iot_db',
    'user':     'admin',
    'password': 'password'
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
        esercente = query(
            'SELECT * FROM esercenti WHERE username = %s',
            (username,), fetch=True, one=True
        )
        if esercente and esercente['password_hash'] == hash_pwd(password):
            session['esercente_id']   = esercente['id']
            session['esercente_nome'] = esercente['nome_negozio']
            return redirect(url_for('dashboard'))
        errore = 'Credenziali non valide'
    return render_template('login.html', errore=errore)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ============================================================
# Dashboard
# ============================================================

@app.route('/dashboard')
@login_richiesto
def dashboard():
    num_utenti = query('SELECT COUNT(*) AS n FROM utenti', fetch=True, one=True)['n']
    num_trans  = query('SELECT COUNT(*) AS n FROM transazioni', fetch=True, one=True)['n']
    num_ricar  = query(
        'SELECT COUNT(*) AS n FROM ricariche WHERE id_esercente = %s AND stato = %s',
        (session['esercente_id'], 'COMPLETATA'), fetch=True, one=True
    )['n']
    totale_circolante = query(
        'SELECT COALESCE(SUM(saldo), 0) AS tot FROM utenti', fetch=True, one=True
    )['tot']
    ultime_trans = query(
        '''SELECT data_ora, nome_utente, importo, esito
           FROM transazioni ORDER BY data_ora DESC LIMIT 5''', fetch=True
    )
    return render_template(
        'dashboard.html',
        num_utenti=num_utenti,
        num_trans=num_trans,
        num_ricar=num_ricar,
        totale_circolante=float(totale_circolante),
        ultime_trans=ultime_trans
    )


# ============================================================
# Utenti
# ============================================================

@app.route('/utenti')
@login_richiesto
def utenti():
    cerca = request.args.get('q', '').strip()
    if cerca:
        lista = query(
            '''SELECT id, uid, nome, saldo FROM utenti
               WHERE LOWER(nome) LIKE %s OR uid LIKE %s
               ORDER BY nome''',
            (f'%{cerca.lower()}%', f'%{cerca}%'), fetch=True
        )
    else:
        lista = query('SELECT id, uid, nome, saldo FROM utenti ORDER BY nome', fetch=True)
    return render_template('utenti.html', utenti=lista, cerca=cerca)


@app.route('/utenti/<int:id_utente>')
@login_richiesto
def dettaglio_utente(id_utente):
    u = query('SELECT * FROM utenti WHERE id = %s', (id_utente,), fetch=True, one=True)
    if not u:
        abort(404)
    trans = query(
        '''SELECT data_ora, importo, saldo_dopo, esito
           FROM transazioni WHERE uid_carta = %s
           ORDER BY data_ora DESC LIMIT 30''', (u['uid'],), fetch=True
    )
    ricariche = query(
        '''SELECT r.data_completata, r.importo, e.nome_negozio
           FROM ricariche r JOIN esercenti e ON e.id = r.id_esercente
           WHERE r.id_utente_riceve = %s AND r.stato = %s
           ORDER BY r.data_completata DESC LIMIT 30''',
        (id_utente, 'COMPLETATA'), fetch=True
    )
    return render_template('dettaglio_utente.html', utente=u, trans=trans, ricariche=ricariche)


@app.route('/utenti/<int:id_utente>/saldo', methods=['POST'])
@login_richiesto
def modifica_saldo(id_utente):
    # Permette all'esercente di accreditare/scalare saldo manualmente
    try:
        delta = float(request.form.get('delta', 0))
    except ValueError:
        return redirect(url_for('dettaglio_utente', id_utente=id_utente))
    u = query('SELECT saldo FROM utenti WHERE id = %s', (id_utente,), fetch=True, one=True)
    if u:
        nuovo = float(u['saldo']) + delta
        if nuovo < 0:
            nuovo = 0
        query('UPDATE utenti SET saldo = %s WHERE id = %s', (nuovo, id_utente))
    return redirect(url_for('dettaglio_utente', id_utente=id_utente))


@app.route('/utenti/nuovo', methods=['GET', 'POST'])
@login_richiesto
def nuovo_utente():
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
            if query('SELECT id FROM utenti WHERE uid = %s', (uid,), fetch=True, one=True):
                errore = 'Esiste gia un utente con questo UID'
            else:
                query(
                    'INSERT INTO utenti (uid, nome, pin_hash, saldo) VALUES (%s, %s, %s, %s)',
                    (uid, nome, hash_pwd(pin), saldo_n)
                )
                return redirect(url_for('utenti'))
    return render_template('nuovo_utente.html', errore=errore)


# ============================================================
# Transazioni
# ============================================================

@app.route('/transazioni')
@login_richiesto
def transazioni():
    lista = query(
        '''SELECT data_ora, nome_utente, importo, saldo_dopo, esito
           FROM transazioni ORDER BY data_ora DESC LIMIT 100''', fetch=True
    )
    return render_template('transazioni.html', transazioni=lista)


# ============================================================
# Ricariche
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
    # Endpoint JSON usato dal JavaScript per fare polling
    r = query(
        '''SELECT r.stato, r.data_completata, u.nome AS nome_utente
           FROM ricariche r LEFT JOIN utenti u ON u.id = r.id_utente_riceve
           WHERE r.token = %s AND r.id_esercente = %s''',
        (token, session['esercente_id']), fetch=True, one=True
    )
    if not r:
        return jsonify({'errore': 'non trovato'}), 404
    return jsonify({
        'stato':         r['stato'],
        'nome_utente':   r['nome_utente'],
        'data_completata': r['data_completata'].strftime('%H:%M:%S') if r['data_completata'] else None
    })


@app.route('/qr/<token>/annulla', methods=['POST'])
@login_richiesto
def qr_annulla(token):
    query(
        '''UPDATE ricariche SET stato = %s WHERE token = %s AND id_esercente = %s AND stato = %s''',
        ('ANNULLATA', token, session['esercente_id'], 'PENDING')
    )
    return redirect(url_for('ricariche'))


@app.route('/ricariche')
@login_richiesto
def ricariche():
    lista = query(
        '''SELECT r.*, u.nome AS nome_destinatario
           FROM ricariche r LEFT JOIN utenti u ON u.id = r.id_utente_riceve
           WHERE r.id_esercente = %s
           ORDER BY r.data_creazione DESC LIMIT 100''',
        (session['esercente_id'],), fetch=True
    )
    return render_template('ricariche.html', ricariche=lista)


# ============================================================
# Pagina cliente
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
                               errore='Questa ricarica e gia stata gestita o annullata', ricarica=r)
    if r['scadenza'] < datetime.now():
        query('UPDATE ricariche SET stato = %s WHERE id = %s', ('SCADUTA', r['id']))
        return render_template('pay_cliente.html', errore='Ricarica scaduta', ricarica=r)

    if request.method == 'POST':
        uid = request.form.get('uid', '').strip()
        pin = request.form.get('pin', '').strip()
        utente = query('SELECT * FROM utenti WHERE uid = %s', (uid,), fetch=True, one=True)
        if not utente:
            return render_template('pay_cliente.html', errore='UID non trovato', ricarica=r)
        if utente['pin_hash'] != hash_pwd(pin):
            return render_template('pay_cliente.html', errore='PIN errato', ricarica=r)
        nuovo_saldo = float(utente['saldo']) + float(r['importo'])
        query('UPDATE utenti SET saldo = %s WHERE id = %s', (nuovo_saldo, utente['id']))
        query(
            '''UPDATE ricariche SET stato = %s, id_utente_riceve = %s, data_completata = NOW()
               WHERE id = %s''',
            ('COMPLETATA', utente['id'], r['id'])
        )
        return render_template('pay_cliente.html', successo=True, ricarica=r,
                               nome_utente=utente['nome'], nuovo_saldo=nuovo_saldo)
    return render_template('pay_cliente.html', ricarica=r)


# ============================================================
# API REST per Flutter
# ============================================================

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    uid  = str(data.get('uid', '')).strip()
    pin  = str(data.get('pin', '')).strip()
    utente = query('SELECT id, uid, nome, pin_hash, saldo FROM utenti WHERE uid = %s',
                   (uid,), fetch=True, one=True)
    if not utente or utente['pin_hash'] != hash_pwd(pin):
        return jsonify({'success': False, 'errore': 'Credenziali non valide'}), 401
    return jsonify({
        'success': True,
        'utente': {'id': utente['id'], 'uid': utente['uid'],
                   'nome': utente['nome'], 'saldo': float(utente['saldo'])}
    })


@app.route('/api/saldo', methods=['POST'])
def api_saldo():
    data = request.get_json() or {}
    utente = query('SELECT pin_hash, saldo FROM utenti WHERE uid = %s',
                   (str(data.get('uid', '')),), fetch=True, one=True)
    if not utente or utente['pin_hash'] != hash_pwd(str(data.get('pin', ''))):
        return jsonify({'success': False}), 401
    return jsonify({'success': True, 'saldo': float(utente['saldo'])})


@app.route('/api/transazioni', methods=['POST'])
def api_transazioni():
    data = request.get_json() or {}
    uid  = str(data.get('uid', ''))
    utente = query('SELECT pin_hash FROM utenti WHERE uid = %s', (uid,), fetch=True, one=True)
    if not utente or utente['pin_hash'] != hash_pwd(str(data.get('pin', ''))):
        return jsonify({'success': False}), 401
    trans = query(
        '''SELECT data_ora, importo, saldo_dopo, esito FROM transazioni
           WHERE uid_carta = %s ORDER BY data_ora DESC LIMIT 50''',
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
    utente = query('SELECT * FROM utenti WHERE uid = %s', (uid,), fetch=True, one=True)
    if not utente or utente['pin_hash'] != hash_pwd(pin):
        return jsonify({'success': False, 'errore': 'Credenziali non valide'}), 401
    r = query('SELECT * FROM ricariche WHERE token = %s', (token,), fetch=True, one=True)
    if not r:
        return jsonify({'success': False, 'errore': 'Token non valido'}), 404
    if r['stato'] != 'PENDING':
        return jsonify({'success': False, 'errore': 'Ricarica gia usata o annullata'}), 400
    if r['scadenza'] < datetime.now():
        query('UPDATE ricariche SET stato = %s WHERE id = %s', ('SCADUTA', r['id']))
        return jsonify({'success': False, 'errore': 'Ricarica scaduta'}), 400
    nuovo_saldo = float(utente['saldo']) + float(r['importo'])
    query('UPDATE utenti SET saldo = %s WHERE id = %s', (nuovo_saldo, utente['id']))
    query(
        '''UPDATE ricariche SET stato = %s, id_utente_riceve = %s, data_completata = NOW()
           WHERE id = %s''',
        ('COMPLETATA', utente['id'], r['id'])
    )
    return jsonify({'success': True, 'importo': float(r['importo']), 'nuovo_saldo': nuovo_saldo})


# ============================================================
# Avvio
# ============================================================

if __name__ == '__main__':
    ip = get_local_ip()
    print('=' * 50)
    print('POS IoT - Webserver Flask')
    print('=' * 50)
    print(f'Indirizzo: http://{ip}:5000')
    print('Login esercente: admin / admin123')
    print('=' * 50)
    app.run(host='0.0.0.0', port=5000, debug=True)