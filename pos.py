# -*- coding: utf-8 -*-
"""
POS IoT - Terminale fisico di pagamento
Raspberry Pi 4 + RC522 + OLED SH1106 + tastierino HX-643 + LED RGB
Usa la logica process_payment integrata con il DB sicuro (Decimal, FOR UPDATE,
id_utente, attiva per blocco carta, titolo/tipo/categoria per app Flutter).
"""

import hashlib
import os
import sys
import time
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

# Carica .env
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Sopprime AUTH ERROR di mfrc522
devnull = open(os.devnull, "w")
sys.stderr = devnull


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


# ============================================================
# Hardware
# ============================================================

serial_i2c = i2c(port=1, address=0x3C)
device = sh1106(serial_i2c)
reader = SimpleMFRC522()  # imposta automaticamente GPIO in BOARD mode

KEYPAD    = [['1','2','3'], ['4','5','6'], ['7','8','9'], ['*','0','#']]
ROW_PINS  = [38, 36, 32, 35]
COL_PINS  = [33, 31, 29]
PIN_ROSSO = 15
PIN_VERDE = 13
PIN_BLU   = 11
TIMEOUT   = 30  # secondi di inattivita sul tastierino

GPIO.setwarnings(False)
for pin in ROW_PINS:
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.HIGH)
for pin in COL_PINS:
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
for pin in [PIN_ROSSO, PIN_VERDE, PIN_BLU]:
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.LOW)


# ============================================================
# Font OLED
# ============================================================

try:
    font_grande = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
    font_medio  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
except Exception:
    font_grande = ImageFont.load_default()
    font_medio  = ImageFont.load_default()


# ============================================================
# LED RGB (catodo comune)
# ============================================================

def led(r=0, g=0, b=0):
    GPIO.output(PIN_ROSSO, r)
    GPIO.output(PIN_VERDE, g)
    GPIO.output(PIN_BLU,   b)

def led_spegni(): led(0, 0, 0)
def led_verde():  led(0, 1, 0)
def led_rosso():  led(1, 0, 0)
def led_giallo(): led(1, 1, 0)
def led_blu():    led(0, 0, 1)

def led_lampeggia(r, g, b, volte=4, intervallo=0.2):
    for _ in range(volte):
        led(r, g, b)
        time.sleep(intervallo)
        led_spegni()
        time.sleep(intervallo)


# ============================================================
# Display OLED
# ============================================================

def mostra(riga1, riga2='', durata=0):
    # Mostra due righe centrate
    with canvas(device) as draw:
        bbox1 = draw.textbbox((0, 0), riga1, font=font_grande)
        x1 = (128 - (bbox1[2] - bbox1[0])) // 2
        draw.text((x1, 8), riga1, font=font_grande, fill='white')
        if riga2:
            bbox2 = draw.textbbox((0, 0), riga2, font=font_medio)
            x2 = (128 - (bbox2[2] - bbox2[0])) // 2
            draw.text((x2, 38), riga2, font=font_medio, fill='white')
    if durata > 0:
        time.sleep(durata)


# ============================================================
# Tastierino
# ============================================================

def leggi_tasto():
    for i, row_pin in enumerate(ROW_PINS):
        GPIO.output(row_pin, GPIO.LOW)
        for j, col_pin in enumerate(COL_PINS):
            if GPIO.input(col_pin) == GPIO.LOW:
                tasto = KEYPAD[i][j]
                while GPIO.input(col_pin) == GPIO.LOW:
                    time.sleep(0.02)
                GPIO.output(row_pin, GPIO.HIGH)
                return tasto
        GPIO.output(row_pin, GPIO.HIGH)
    return None


def inserisci_importo():
    # Inserimento importo con tastierino, # conferma, * punto, # vuoto annulla
    importo_str = ''
    decimale    = False
    led_giallo()
    mostra('Importo:', '0 EUR')
    ultimo = time.time()
    while True:
        tasto = leggi_tasto()
        if tasto is None:
            if time.time() - ultimo > TIMEOUT:
                return None
            time.sleep(0.05)
            continue
        ultimo = time.time()
        if tasto == '#':
            if importo_str == '' or importo_str == '.':
                return None
            try:
                return Decimal(importo_str)
            except (InvalidOperation, ValueError):
                return None
        elif tasto == '*':
            if not decimale and importo_str != '':
                importo_str += '.'
                decimale = True
        elif tasto.isdigit():
            if len(importo_str) < 8:
                importo_str += tasto
        mostra('Importo:', (importo_str if importo_str else '0') + ' EUR')


def inserisci_pin():
    # PIN 4 cifre, * cancella, # annulla
    pin_str = ''
    led_giallo()
    mostra('PIN:', '____')
    ultimo = time.time()
    while len(pin_str) < 4:
        tasto = leggi_tasto()
        if tasto is None:
            if time.time() - ultimo > TIMEOUT:
                return None
            time.sleep(0.05)
            continue
        ultimo = time.time()
        if tasto.isdigit():
            pin_str += tasto
        elif tasto == '*':
            pin_str = pin_str[:-1]
        elif tasto == '#':
            return None
        mostra('PIN:', '*' * len(pin_str) + '_' * (4 - len(pin_str)))
    time.sleep(0.3)
    return pin_str


# ============================================================
# Database (con FOR UPDATE per evitare race condition)
# ============================================================

def hash_pin(pin):
    return hashlib.sha256(pin.encode("utf-8")).hexdigest()


def cerca_utente_breve(uid):
    # Lettura veloce solo per validare carta e mostrare nome (no lock)
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id, uid, nome, saldo, attiva FROM utenti WHERE uid=%s",
            (str(uid),)
        )
        u = cur.fetchone()
        cur.close()
        conn.close()
        return dict(u) if u else None
    except Exception:
        return None


def registra_transazione_negata(uid, nome, importo, saldo, esito, titolo):
    # Salva tentativi negati per audit (non blocca se fallisce)
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur  = conn.cursor()
        # Recupera id_utente
        cur.execute("SELECT id FROM utenti WHERE uid=%s", (str(uid),))
        row = cur.fetchone()
        id_utente = row[0] if row else None
        cur.execute(
            """INSERT INTO transazioni
               (id_utente, uid_carta, nome_utente, titolo, tipo, categoria,
                importo, saldo_prima, saldo_dopo, esito)
               VALUES (%s,%s,%s,%s,'expense','other',%s,%s,%s,%s)""",
            (id_utente, str(uid), nome, titolo, importo, saldo, saldo, esito)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass


def esegui_pagamento(uid, pin, importo):
    """
    Transazione atomica con FOR UPDATE: previene race condition se il cliente
    paga dal POS e contemporaneamente l'app fa un bonifico.
    Ritorna (esito, messaggio, nuovo_saldo)
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
            return 'NEGATA_CARTA', 'Carta non trovata', None
        if not u['attiva']:
            conn.rollback()
            return 'NEGATA_BLOCCATA', 'Carta bloccata', None
        if u['pin_hash'] != hash_pin(pin):
            conn.rollback()
            cur.execute(
                """INSERT INTO transazioni
                   (id_utente, uid_carta, nome_utente, titolo, tipo, categoria,
                    importo, saldo_prima, saldo_dopo, esito)
                   VALUES (%s,%s,%s,'Tentativo PIN errato','expense','other',%s,%s,%s,'NEGATA_PIN')""",
                (u['id'], u['uid'], u['nome'], importo, u['saldo'], u['saldo'])
            )
            conn.commit()
            return 'NEGATA_PIN', 'PIN errato', None

        saldo_prima = Decimal(str(u['saldo']))
        if saldo_prima < importo:
            cur.execute(
                """INSERT INTO transazioni
                   (id_utente, uid_carta, nome_utente, titolo, tipo, categoria,
                    importo, saldo_prima, saldo_dopo, esito)
                   VALUES (%s,%s,%s,'Pagamento POS','expense','other',%s,%s,%s,'NEGATA_FONDI')""",
                (u['id'], u['uid'], u['nome'], importo, saldo_prima, saldo_prima)
            )
            conn.commit()
            return 'NEGATA_FONDI', 'Fondi insufficienti', None

        saldo_dopo = saldo_prima - importo
        cur.execute("UPDATE utenti SET saldo=%s WHERE id=%s", (saldo_dopo, u['id']))
        cur.execute(
            """INSERT INTO transazioni
               (id_utente, uid_carta, nome_utente, titolo, tipo, categoria,
                importo, saldo_prima, saldo_dopo, esito)
               VALUES (%s,%s,%s,'Pagamento POS','expense','other',%s,%s,%s,'APPROVATA')""",
            (u['id'], u['uid'], u['nome'], importo, saldo_prima, saldo_dopo)
        )
        conn.commit()
        return 'APPROVATA', 'Approvata', saldo_dopo
    except Exception as e:
        conn.rollback()
        return 'ERRORE', f'Errore DB: {e}', None
    finally:
        cur.close()
        conn.close()


# ============================================================
# Logica transazione completa
# ============================================================

def transazione():
    # Caso 0: attesa carta
    led_blu()
    mostra('POS Pronto', 'Avvicina carta')
    uid, _ = reader.read()

    led_giallo()
    mostra('Lettura...', '')
    time.sleep(0.5)

    # Validazione iniziale (no lock, solo per UI)
    utente = cerca_utente_breve(uid)
    if utente is None:
        led_lampeggia(1, 0, 0, volte=4)
        mostra('Carta', 'Non Valida', 3)
        return

    if not utente['attiva']:
        led_lampeggia(1, 0, 0, volte=6)
        mostra('Carta', 'Bloccata', 3)
        registra_transazione_negata(
            utente['uid'], utente['nome'], Decimal('0'),
            utente['saldo'], 'NEGATA_BLOCCATA', 'Tentativo carta bloccata'
        )
        return

    mostra('Ciao', utente['nome'], 2)

    importo = inserisci_importo()
    if importo is None or importo <= 0:
        led_lampeggia(1, 1, 0, volte=2)
        mostra('Operazione', 'Annullata', 2)
        return

    pin = inserisci_pin()
    if pin is None:
        led_lampeggia(1, 1, 0, volte=2)
        mostra('Operazione', 'Annullata', 2)
        return

    # Pagamento atomico con lock FOR UPDATE
    led_giallo()
    mostra('Elaboro...', '')
    esito, messaggio, nuovo_saldo = esegui_pagamento(utente['uid'], pin, importo)

    if esito == 'APPROVATA':
        mostra('Approvata', f'{nuovo_saldo:.2f} EUR')
        led_lampeggia(0, 1, 0, volte=6)
        time.sleep(1)
    elif esito == 'NEGATA_PIN':
        led_lampeggia(1, 0, 0, volte=5)
        mostra('PIN Errato', 'Negata', 3)
    elif esito == 'NEGATA_FONDI':
        led_lampeggia(1, 0, 0, volte=5)
        mostra('Fondi', 'Insufficienti', 3)
    elif esito == 'NEGATA_BLOCCATA':
        led_lampeggia(1, 0, 0, volte=6)
        mostra('Carta', 'Bloccata', 3)
    else:
        led_lampeggia(1, 0, 0, volte=3)
        mostra('Errore DB', 'Riprova', 3)


def main():
    try:
        led_giallo()
        mostra('POS IoT', 'Avvio...', 2)
        while True:
            transazione()
            time.sleep(1)
    except KeyboardInterrupt:
        mostra('Spegnimento', '', 1)
    finally:
        led_spegni()
        device.clear()
        sys.stderr = sys.__stderr__
        GPIO.cleanup()


if __name__ == "__main__":
    main()