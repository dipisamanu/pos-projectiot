# -*- coding: utf-8 -*-
"""
Sistema POS IoT - Terminale di pagamento offline
Raspberry Pi 4 con RC522, OLED SH1106, tastierino HX-643, LED RGB
Database PostgreSQL
"""

import os
import sys
import time
import hashlib
import psycopg2
import RPi.GPIO as GPIO
from mfrc522 import SimpleMFRC522
from luma.core.interface.serial import i2c
from luma.oled.device import sh1106
from luma.core.render import canvas
from PIL import ImageFont

# Sopprime i messaggi AUTH ERROR della libreria mfrc522
devnull = open(os.devnull, 'w')
sys.stderr = devnull

# ============================================================
# Configurazione database
# ============================================================
DB_CONFIG = {
    'host':     'localhost',
    'database': 'iot_db',
    'user':     'admin',
    'password': 'password'
}

# ============================================================
# Configurazione hardware
# ============================================================

# Display OLED SH1106 1.3" via I2C
serial_i2c = i2c(port=1, address=0x3C)
device = sh1106(serial_i2c)

# Lettore RFID RC522 via SPI
# Importante: SimpleMFRC522 imposta automaticamente GPIO in modalita BOARD
reader = SimpleMFRC522()

# Tastierino HX-643 3x4 (pin fisici BOARD, verificati con test diagnostico)
KEYPAD    = [['1','2','3'], ['4','5','6'], ['7','8','9'], ['*','0','#']]
ROW_PINS  = [38, 36, 32, 35]
COL_PINS  = [33, 31, 29]

# LED RGB catodo comune (pin fisici BOARD)
PIN_ROSSO = 15
PIN_VERDE = 13
PIN_BLU   = 11

# Timeout inattivita tastierino in secondi
TIMEOUT = 30

# ============================================================
# Inizializzazione GPIO
# ============================================================
GPIO.setwarnings(False)

# Righe tastierino come uscite
for pin in ROW_PINS:
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.HIGH)

# Colonne tastierino come ingressi con pull-up
for pin in COL_PINS:
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

# Pin LED come uscite, inizialmente spenti
GPIO.setup(PIN_ROSSO, GPIO.OUT)
GPIO.setup(PIN_VERDE, GPIO.OUT)
GPIO.setup(PIN_BLU,   GPIO.OUT)
GPIO.output(PIN_ROSSO, GPIO.LOW)
GPIO.output(PIN_VERDE, GPIO.LOW)
GPIO.output(PIN_BLU,   GPIO.LOW)

# ============================================================
# Font per display OLED
# ============================================================
try:
    font_grande = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16
    )
    font_medio = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12
    )
except Exception:
    font_grande = ImageFont.load_default()
    font_medio  = ImageFont.load_default()


# ============================================================
# Funzioni LED RGB
# ============================================================

def led(r=0, g=0, b=0):
    GPIO.output(PIN_ROSSO, r)
    GPIO.output(PIN_VERDE, g)
    GPIO.output(PIN_BLU,   b)

def led_spegni():   led(0, 0, 0)
def led_verde():    led(0, 1, 0)
def led_rosso():    led(1, 0, 0)
def led_giallo():   led(1, 1, 0)  # usato durante elaborazione
def led_blu():      led(0, 0, 1)  # usato durante attesa carta

def led_lampeggia(r, g, b, volte=4, intervallo=0.2):
    for _ in range(volte):
        led(r, g, b)
        time.sleep(intervallo)
        led_spegni()
        time.sleep(intervallo)


# ============================================================
# Funzioni display OLED
# ============================================================

def mostra(riga1, riga2='', durata=0):
    # Mostra due righe centrate sul display OLED
    with canvas(device) as draw:
        # Riga 1 in alto con font grande
        bbox1 = draw.textbbox((0, 0), riga1, font=font_grande)
        x1 = (128 - (bbox1[2] - bbox1[0])) // 2
        draw.text((x1, 8), riga1, font=font_grande, fill='white')
        # Riga 2 in basso con font medio
        if riga2:
            bbox2 = draw.textbbox((0, 0), riga2, font=font_medio)
            x2 = (128 - (bbox2[2] - bbox2[0])) // 2
            draw.text((x2, 38), riga2, font=font_medio, fill='white')
    if durata > 0:
        time.sleep(durata)


# ============================================================
# Funzioni database PostgreSQL
# ============================================================

def apri_connessione():
    return psycopg2.connect(**DB_CONFIG)

def cerca_utente(uid):
    # Cerca utente per UID del tag NFC, restituisce dict o None
    try:
        conn = apri_connessione()
        cur  = conn.cursor()
        cur.execute(
            'SELECT uid, nome, pin_hash, saldo FROM utenti WHERE uid = %s',
            (str(uid),)
        )
        riga = cur.fetchone()
        cur.close()
        conn.close()
        if riga is None:
            return None
        return {'uid': riga[0], 'nome': riga[1], 'pin_hash': riga[2], 'saldo': float(riga[3])}
    except Exception:
        return None

def verifica_pin(pin_inserito, pin_hash_db):
    # Confronta hash sha256 del PIN inserito con quello nel database
    return hashlib.sha256(pin_inserito.encode()).hexdigest() == pin_hash_db

def aggiorna_saldo(uid, nuovo_saldo):
    conn = apri_connessione()
    cur  = conn.cursor()
    cur.execute('UPDATE utenti SET saldo = %s WHERE uid = %s', (nuovo_saldo, str(uid)))
    conn.commit()
    cur.close()
    conn.close()

def registra_transazione(uid, nome, importo, saldo_prima, saldo_dopo, esito):
    # Salva la transazione nello storico
    # esito: APPROVATA, NEGATA_PIN, NEGATA_FONDI, NEGATA_CARTA
    conn = apri_connessione()
    cur  = conn.cursor()
    cur.execute(
        '''INSERT INTO transazioni
           (uid_carta, nome_utente, importo, saldo_prima, saldo_dopo, esito)
           VALUES (%s, %s, %s, %s, %s, %s)''',
        (str(uid), nome, importo, saldo_prima, saldo_dopo, esito)
    )
    conn.commit()
    cur.close()
    conn.close()


# ============================================================
# Funzioni tastierino
# ============================================================

def leggi_tasto():
    # Scansiona la matrice 4x3 e restituisce il tasto premuto o None
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
    # Legge importo dal tastierino
    # * = punto decimale, # = conferma, # vuoto = annulla
    # Timeout di 30 secondi senza input
    importo_str = ''
    decimale    = False
    led_giallo()
    mostra('Importo:', '0 EUR')
    ultimo_input = time.time()

    while True:
        tasto = leggi_tasto()
        if tasto is None:
            if time.time() - ultimo_input > TIMEOUT:
                return None
            time.sleep(0.05)
            continue
        ultimo_input = time.time()

        if tasto == '#':
            # Campo vuoto con # = annullamento
            if importo_str == '' or importo_str == '.':
                return None
            try:
                return float(importo_str)
            except ValueError:
                return None
        elif tasto == '*':
            # Aggiunge punto decimale una sola volta
            if not decimale and importo_str != '':
                importo_str += '.'
                decimale = True
        elif tasto.isdigit():
            if len(importo_str) < 8:
                importo_str += tasto
        visualizza = importo_str if importo_str else '0'
        mostra('Importo:', visualizza + ' EUR')

def inserisci_pin():
    # Legge PIN a 4 cifre, mostra asterischi
    # * = cancella ultima cifra, # = annulla
    # Timeout di 30 secondi senza input
    pin_str = ''
    led_giallo()
    mostra('PIN:', '____')
    ultimo_input = time.time()

    while len(pin_str) < 4:
        tasto = leggi_tasto()
        if tasto is None:
            if time.time() - ultimo_input > TIMEOUT:
                return None
            time.sleep(0.05)
            continue
        ultimo_input = time.time()

        if tasto.isdigit():
            pin_str += tasto
        elif tasto == '*':
            # Cancella ultima cifra inserita
            pin_str = pin_str[:-1]
        elif tasto == '#':
            return None

        visualizza = '*' * len(pin_str) + '_' * (4 - len(pin_str))
        mostra('PIN:', visualizza)

    time.sleep(0.3)
    return pin_str


# ============================================================
# Logica principale della transazione
# ============================================================

def transazione():
    # Caso 0: attesa carta (LED blu fisso)
    led_blu()
    mostra('POS Pronto', 'Avvicina carta')
    uid, _ = reader.read()

    # Lettura in corso
    led_giallo()
    mostra('Lettura...', '')
    time.sleep(0.5)

    # Caso 2: carta non valida
    utente = cerca_utente(uid)
    if utente is None:
        led_lampeggia(1, 0, 0, volte=4)
        mostra('Carta', 'Non Valida', 3)
        return

    # Saluto personalizzato con saldo
    saldo_str = str(round(utente['saldo'], 2)) + ' EUR'
    mostra('Ciao', utente['nome'], 2)

    # Caso 1: inserimento importo
    importo = inserisci_importo()

    # Caso 5: annullamento durante importo
    if importo is None or importo <= 0:
        led_lampeggia(1, 1, 0, volte=2)
        mostra('Operazione', 'Annullata', 2)
        return

    # Inserimento PIN
    pin = inserisci_pin()

    # Caso 5: annullamento durante PIN
    if pin is None:
        led_lampeggia(1, 1, 0, volte=2)
        mostra('Operazione', 'Annullata', 2)
        return

    # Caso 3: PIN errato
    if not verifica_pin(pin, utente['pin_hash']):
        led_lampeggia(1, 0, 0, volte=5)
        mostra('PIN Errato', 'Negata', 3)
        try:
            registra_transazione(
                utente['uid'], utente['nome'],
                importo, utente['saldo'], utente['saldo'], 'NEGATA_PIN'
            )
        except Exception:
            pass
        return

    # Caso 4: fondi insufficienti
    if utente['saldo'] < importo:
        led_lampeggia(1, 0, 0, volte=5)
        mostra('Fondi', 'Insufficienti', 3)
        try:
            registra_transazione(
                utente['uid'], utente['nome'],
                importo, utente['saldo'], utente['saldo'], 'NEGATA_FONDI'
            )
        except Exception:
            pass
        return

    # Caso 1: transazione approvata
    nuovo_saldo = round(utente['saldo'] - importo, 2)
    try:
        aggiorna_saldo(utente['uid'], nuovo_saldo)
        registra_transazione(
            utente['uid'], utente['nome'],
            importo, utente['saldo'], nuovo_saldo, 'APPROVATA'
        )
    except Exception:
        led_lampeggia(1, 0, 0, volte=3)
        mostra('Errore DB', 'Riprova', 3)
        return

    saldo_nuovo_str = str(nuovo_saldo) + ' EUR'
    mostra('Approvata', saldo_nuovo_str)
    led_lampeggia(0, 1, 0, volte=6)
    time.sleep(1)


# ============================================================
# Avvio del programma
# ============================================================

def main():
    try:
        led_giallo()
        mostra('POS IoT', 'Avvio...', 2)
        while True:
            transazione()
            time.sleep(1)
    except KeyboardInterrupt:
        mostra('Spegnimento', 'in corso...', 1)
    finally:
        led_spegni()
        device.clear()
        sys.stderr = sys.__stderr__
        GPIO.cleanup()

if __name__ == '__main__':
    main()