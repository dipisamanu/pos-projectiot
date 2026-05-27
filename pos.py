# -*- coding: utf-8 -*-
"""
POS IoT - Terminale fisico di pagamento
Due modalità:
  1. Portale: esercente crea richiesta sul sito → pos.py la riceve → cliente avvicina carta
  2. Manuale: cliente avvicina carta → inserisce importo → inserisce PIN
"""

import hashlib
import os
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

import psycopg2
import psycopg2.extras
import RPi.GPIO as GPIO
from dotenv import load_dotenv
from mfrc522 import SimpleMFRC522
from luma.core.interface.serial import i2c
from luma.oled.device import sh1106
from luma.core.render import canvas
from PIL import ImageFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Sopprime AUTH ERROR mfrc522
_stderr_orig = sys.stderr
sys.stderr = open(os.devnull, "w")

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
    sys.stderr = _stderr_orig
    raise RuntimeError("DB_PASSWORD non impostata nel file .env")

TIMEOUT_TASTO = 30  # secondi inattività tastierino

# ============================================================
# Hardware
# ============================================================

serial_i2c = i2c(port=1, address=0x3C)
device     = sh1106(serial_i2c)
reader     = SimpleMFRC522()  # forza GPIO BOARD mode

KEYPAD   = [['1','2','3'], ['4','5','6'], ['7','8','9'], ['*','0','#']]
ROW_PINS = [38, 36, 32, 35]
COL_PINS = [33, 31, 29]
PIN_R    = 15
PIN_G    = 13
PIN_B    = 11

GPIO.setwarnings(False)
for p in ROW_PINS:
    GPIO.setup(p, GPIO.OUT)
    GPIO.output(p, GPIO.HIGH)
for p in COL_PINS:
    GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_UP)
for p in [PIN_R, PIN_G, PIN_B]:
    GPIO.setup(p, GPIO.OUT)
    GPIO.output(p, GPIO.LOW)

try:
    font_grande = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
    font_medio  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
except Exception:
    font_grande = ImageFont.load_default()
    font_medio  = ImageFont.load_default()

# ============================================================
# LED
# ============================================================

def led(r=0, g=0, b=0):
    GPIO.output(PIN_R, r)
    GPIO.output(PIN_G, g)
    GPIO.output(PIN_B, b)

def led_off():     led(0, 0, 0)
def led_blu():     led(0, 0, 1)
def led_verde():   led(0, 1, 0)
def led_rosso():   led(1, 0, 0)
def led_giallo():  led(1, 1, 0)
def led_viola():   led(1, 0, 1)

def led_blink(r, g, b, n=4, t=0.2):
    for _ in range(n):
        led(r, g, b); time.sleep(t)
        led_off();     time.sleep(t)

# ============================================================
# Display OLED
# ============================================================

def mostra(r1, r2='', wait=0):
    with canvas(device) as draw:
        bb = draw.textbbox((0, 0), r1, font=font_grande)
        x1 = max(0, (128 - (bb[2] - bb[0])) // 2)
        draw.text((x1, 8), r1, font=font_grande, fill='white')
        if r2:
            bb2 = draw.textbbox((0, 0), r2, font=font_medio)
            x2 = max(0, (128 - (bb2[2] - bb2[0])) // 2)
            draw.text((x2, 38), r2, font=font_medio, fill='white')
    if wait > 0:
        time.sleep(wait)

# ============================================================
# Tastierino
# ============================================================

def leggi_tasto():
    for i, rp in enumerate(ROW_PINS):
        GPIO.output(rp, GPIO.LOW)
        for j, cp in enumerate(COL_PINS):
            if GPIO.input(cp) == GPIO.LOW:
                t = KEYPAD[i][j]
                while GPIO.input(cp) == GPIO.LOW:
                    time.sleep(0.02)
                GPIO.output(rp, GPIO.HIGH)
                return t
        GPIO.output(rp, GPIO.HIGH)
    return None

def inserisci_importo():
    s = ''
    dec = False
    led_giallo()
    mostra('Importo:', '0 EUR')
    last = time.time()
    while True:
        t = leggi_tasto()
        if t is None:
            if time.time() - last > TIMEOUT_TASTO:
                return None
            time.sleep(0.05)
            continue
        last = time.time()
        if t == '#':
            if not s or s == '.':
                return None
            try:
                return Decimal(s)
            except (InvalidOperation, ValueError):
                return None
        elif t == '*':
            if not dec and s:
                s += '.'; dec = True
        elif t.isdigit() and len(s) < 8:
            s += t
        mostra('Importo:', (s or '0') + ' EUR')

def inserisci_pin():
    p = ''
    led_giallo()
    mostra('PIN:', '____')
    last = time.time()
    while len(p) < 4:
        t = leggi_tasto()
        if t is None:
            if time.time() - last > TIMEOUT_TASTO:
                return None
            time.sleep(0.05)
            continue
        last = time.time()
        if t.isdigit():    p += t
        elif t == '*':     p = p[:-1]
        elif t == '#':     return None
        mostra('PIN:', '*' * len(p) + '_' * (4 - len(p)))
    time.sleep(0.3)
    return p

# ============================================================
# NFC - lettura non bloccante
# ============================================================

def leggi_carta_timeout(secondi=60):
    """
    Aspetta una carta NFC per al massimo `secondi`.
    Premi # sul tastierino per annullare.
    Ritorna uid o None.
    """
    fine = time.time() + secondi
    while time.time() < fine:
        uid, _ = reader.read_no_block()
        if uid:
            return uid
        t = leggi_tasto()
        if t == '#':
            return None
        rimasto = int(fine - time.time())
        mostra('Avvicina carta', f'# annulla ({rimasto}s)')
        time.sleep(0.3)
    return None

# ============================================================
# DB helpers
# ============================================================

def hash_pin(pin):
    return hashlib.sha256(pin.encode()).hexdigest()

def cerca_utente(uid):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, uid, nome, saldo, attiva FROM utenti WHERE uid=%s", (str(uid),))
        u = cur.fetchone()
        cur.close(); conn.close()
        return dict(u) if u else None
    except Exception:
        return None

def cerca_richiesta_pendente():
    """Ritorna la prima richiesta PENDING non scaduta (FIFO)."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT r.id, r.importo, r.descrizione, e.nome_negozio
            FROM richieste_pagamento r
            JOIN esercenti e ON e.id = r.id_esercente
            WHERE r.stato = 'PENDING' AND r.scadenza > NOW()
            ORDER BY r.data_creazione ASC
            LIMIT 1
        """)
        r = cur.fetchone()
        cur.close(); conn.close()
        return dict(r) if r else None
    except Exception:
        return None

def aggiorna_stato_richiesta(id_richiesta, stato):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur  = conn.cursor()
        cur.execute(
            "UPDATE richieste_pagamento SET stato=%s WHERE id=%s",
            (stato, id_richiesta)
        )
        conn.commit(); cur.close(); conn.close()
    except Exception:
        pass

def esegui_pagamento(uid, pin, importo, titolo='Pagamento POS', id_richiesta=None):
    """
    Pagamento atomico con FOR UPDATE.
    Se id_richiesta è fornito, aggiorna la richiesta nella stessa transazione.
    Ritorna (esito, nuovo_saldo|None)
    """
    conn = psycopg2.connect(**DB_CONFIG)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT id, uid, nome, saldo, attiva, pin_hash FROM utenti WHERE uid=%s FOR UPDATE",
            (str(uid),)
        )
        u = cur.fetchone()
        if not u:
            conn.rollback()
            return 'NEGATA_CARTA', None
        if not u['attiva']:
            conn.rollback()
            return 'NEGATA_BLOCCATA', None
        if u['pin_hash'] != hash_pin(pin):
            cur.execute(
                """INSERT INTO transazioni
                   (id_utente, uid_carta, nome_utente, titolo, tipo, categoria,
                    importo, saldo_prima, saldo_dopo, esito)
                   VALUES (%s,%s,%s,'Tentativo PIN errato','expense','other',%s,%s,%s,'NEGATA_PIN')""",
                (u['id'], u['uid'], u['nome'], importo, u['saldo'], u['saldo'])
            )
            conn.commit()
            return 'NEGATA_PIN', None

        saldo_prima = Decimal(str(u['saldo']))
        if saldo_prima < importo:
            cur.execute(
                """INSERT INTO transazioni
                   (id_utente, uid_carta, nome_utente, titolo, tipo, categoria,
                    importo, saldo_prima, saldo_dopo, esito)
                   VALUES (%s,%s,%s,%s,'expense','other',%s,%s,%s,'NEGATA_FONDI')""",
                (u['id'], u['uid'], u['nome'], titolo, importo, saldo_prima, saldo_prima)
            )
            if id_richiesta:
                cur.execute(
                    "UPDATE richieste_pagamento SET stato='NEGATA' WHERE id=%s",
                    (id_richiesta,)
                )
            conn.commit()
            return 'NEGATA_FONDI', None

        saldo_dopo = saldo_prima - importo
        cur.execute("UPDATE utenti SET saldo=%s WHERE id=%s", (saldo_dopo, u['id']))
        cur.execute(
            """INSERT INTO transazioni
               (id_utente, uid_carta, nome_utente, titolo, tipo, categoria,
                importo, saldo_prima, saldo_dopo, esito)
               VALUES (%s,%s,%s,%s,'expense','other',%s,%s,%s,'APPROVATA')""",
            (u['id'], u['uid'], u['nome'], titolo, importo, saldo_prima, saldo_dopo)
        )
        if id_richiesta:
            cur.execute(
                """UPDATE richieste_pagamento
                   SET stato='COMPLETATA', id_utente_paga=%s, data_completata=NOW()
                   WHERE id=%s""",
                (u['id'], id_richiesta)
            )
        conn.commit()
        return 'APPROVATA', saldo_dopo
    except Exception as e:
        conn.rollback()
        return 'ERRORE', None
    finally:
        cur.close(); conn.close()

# ============================================================
# Flusso: richiesta dal portale web
# ============================================================

def gestisci_richiesta(richiesta):
    importo   = Decimal(str(richiesta['importo']))
    descr     = richiesta['descrizione'][:16]
    negozio   = richiesta['nome_negozio'][:16]
    id_rich   = richiesta['id']

    # Mostra richiesta
    led_giallo()
    mostra(f'{importo:.2f} EUR', negozio, 2)

    # Segna IN_CORSO (impedisce ad altri POS di prenderla)
    aggiorna_stato_richiesta(id_rich, 'IN_CORSO')

    # Attendi carta (60 secondi, # per annullare)
    led_blu()
    mostra('Avvicina carta', descr)
    uid = leggi_carta_timeout(60)

    if uid is None:
        # Timeout o annullamento: rimetti PENDING
        aggiorna_stato_richiesta(id_rich, 'PENDING')
        led_viola()
        mostra('Annullato', '', 2)
        led_off()
        return

    # Identifica utente
    utente = cerca_utente(uid)
    if utente is None:
        aggiorna_stato_richiesta(id_rich, 'PENDING')
        led_blink(1, 0, 0, n=4)
        mostra('Carta', 'Non valida', 3)
        return

    if not utente['attiva']:
        aggiorna_stato_richiesta(id_rich, 'PENDING')
        led_blink(1, 0, 0, n=6)
        mostra('Carta', 'Bloccata', 3)
        return

    mostra('Ciao', utente['nome'], 2)

    # PIN
    pin = inserisci_pin()
    if pin is None:
        aggiorna_stato_richiesta(id_rich, 'PENDING')
        led_viola()
        mostra('Annullato', '', 2)
        led_off()
        return

    # Pagamento atomico
    led_giallo()
    mostra('Elaboro...', '')
    titolo = f'{descr} - {negozio}'
    esito, nuovo_saldo = esegui_pagamento(
        utente['uid'], pin, importo, titolo=titolo, id_richiesta=id_rich
    )

    if esito == 'APPROVATA':
        mostra('Approvata', f'{nuovo_saldo:.2f} EUR')
        led_blink(0, 1, 0, n=6)
        time.sleep(1)
    elif esito == 'NEGATA_PIN':
        # Rimetti PENDING: il cliente può riprovare
        aggiorna_stato_richiesta(id_rich, 'PENDING')
        led_blink(1, 0, 0, n=5)
        mostra('PIN Errato', 'Riprova', 3)
    elif esito == 'NEGATA_FONDI':
        # La richiesta è già segnata NEGATA da esegui_pagamento
        led_blink(1, 0, 0, n=5)
        mostra('Fondi', 'Insufficienti', 3)
    elif esito == 'NEGATA_BLOCCATA':
        aggiorna_stato_richiesta(id_rich, 'PENDING')
        led_blink(1, 0, 0, n=6)
        mostra('Carta', 'Bloccata', 3)
    else:
        aggiorna_stato_richiesta(id_rich, 'PENDING')
        led_blink(1, 0, 0, n=3)
        mostra('Errore DB', 'Riprova', 3)

# ============================================================
# Flusso: pagamento manuale (fallback se nessuna richiesta)
# ============================================================

def gestisci_manuale(uid):
    utente = cerca_utente(uid)
    if utente is None:
        led_blink(1, 0, 0, n=4)
        mostra('Carta', 'Non Valida', 3)
        return
    if not utente['attiva']:
        led_blink(1, 0, 0, n=6)
        mostra('Carta', 'Bloccata', 3)
        return

    mostra('Ciao', utente['nome'], 2)

    importo = inserisci_importo()
    if importo is None or importo <= 0:
        led_viola()
        mostra('Annullato', '', 2)
        led_off()
        return

    pin = inserisci_pin()
    if pin is None:
        led_viola()
        mostra('Annullato', '', 2)
        led_off()
        return

    led_giallo()
    mostra('Elaboro...', '')
    esito, nuovo_saldo = esegui_pagamento(utente['uid'], pin, importo)

    if esito == 'APPROVATA':
        mostra('Approvata', f'{nuovo_saldo:.2f} EUR')
        led_blink(0, 1, 0, n=6)
        time.sleep(1)
    elif esito == 'NEGATA_PIN':
        led_blink(1, 0, 0, n=5)
        mostra('PIN Errato', 'Negata', 3)
    elif esito == 'NEGATA_FONDI':
        led_blink(1, 0, 0, n=5)
        mostra('Fondi', 'Insufficienti', 3)
    elif esito == 'NEGATA_BLOCCATA':
        led_blink(1, 0, 0, n=6)
        mostra('Carta', 'Bloccata', 3)
    else:
        led_blink(1, 0, 0, n=3)
        mostra('Errore DB', 'Riprova', 3)

# ============================================================
# Main loop
# ============================================================

def main():
    try:
        led_giallo()
        mostra('POS IoT', 'Avvio...', 2)

        while True:
            # Controlla richieste pendenti dal portale web
            richiesta = cerca_richiesta_pendente()

            if richiesta:
                # Modalità portale: importo già definito dall'esercente
                gestisci_richiesta(richiesta)
            else:
                # Modalità manuale: mostra attesa, controlla carta per 3s
                led_blu()
                mostra('POS Pronto', 'Avvicina carta')
                fine = time.time() + 3
                uid_trovato = None
                while time.time() < fine:
                    uid, _ = reader.read_no_block()
                    if uid:
                        uid_trovato = uid
                        break
                    time.sleep(0.3)
                if uid_trovato:
                    led_giallo()
                    mostra('Lettura...', '')
                    time.sleep(0.4)
                    gestisci_manuale(uid_trovato)

            time.sleep(0.5)

    except KeyboardInterrupt:
        mostra('Spegnimento', '', 1)
    finally:
        led_off()
        try:
            device.clear()
        except Exception:
            pass
        sys.stderr = _stderr_orig
        GPIO.cleanup()


if __name__ == "__main__":
    main()