# -*- coding: utf-8 -*-
"""
POS IoT - Sistema di pagamento offline su Raspberry Pi 4
Hardware: RC522 (RFID), OLED SH1106, Tastierino HX-643, LED RGB
"""

import os
import sys
import time
import hashlib
import psycopg2
import RPi.GPIO as GPIO
from decimal import Decimal, InvalidOperation
from dotenv import load_dotenv
from mfrc522 import SimpleMFRC522
from luma.core.interface.serial import i2c
from luma.oled.device import sh1106
from luma.core.render import canvas
from PIL import ImageFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

devnull = open(os.devnull, 'w')
sys.stderr = devnull

DB_CONFIG = {
    'host':     os.getenv("DB_HOST", "127.0.0.1"),
    'database': os.getenv("DB_NAME", "iot_db"),
    'user':     os.getenv("DB_USER", "admin"),
    'password': os.getenv("DB_PASSWORD", "password")
}

serial_i2c = i2c(port=1, address=0x3C)
device = sh1106(serial_i2c)

reader = SimpleMFRC522()

KEYPAD    = [['1','2','3'], ['4','5','6'], ['7','8','9'], ['*','0','#']]
ROW_PINS  = [38, 36, 32, 35]
COL_PINS  = [33, 31, 29]

PIN_ROSSO = 15
PIN_VERDE = 13
PIN_BLU   = 11
TIMEOUT   = 30

GPIO.setwarnings(False)

for pin in ROW_PINS:
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.HIGH)

for pin in COL_PINS:
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

for pin in [PIN_ROSSO, PIN_VERDE, PIN_BLU]:
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.LOW)

try:
    font_grande = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
    font_medio  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
except Exception:
    font_grande = ImageFont.load_default()
    font_medio  = ImageFont.load_default()

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

def mostra(riga1, riga2='', durata=0):
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

def apri_connessione():
    return psycopg2.connect(**DB_CONFIG)

def cerca_utente(uid):
    try:
        conn = apri_connessione()
        cur  = conn.cursor()
        cur.execute('SELECT id, uid, nome, pin_hash, saldo, attiva FROM utenti WHERE uid = %s', (str(uid),))
        riga = cur.fetchone()
        cur.close()
        conn.close()
        if riga is None:
            return None
        return {
            'id':       riga[0],
            'uid':      riga[1],
            'nome':     riga[2],
            'pin_hash': riga[3],
            'saldo':    Decimal(str(riga[4])),
            'attiva':   riga[5]
        }
    except Exception:
        return None

def verifica_pin(pin_inserito, pin_hash_db):
    hash_inserito = hashlib.sha256(pin_inserito.encode("utf-8")).hexdigest()
    return hash_inserito == pin_hash_db

def elabora_transazione(id_utente, uid_carta, nome_utente, importo, esito, saldo_prima, saldo_dopo):
    conn = apri_connessione()
    cur  = conn.cursor()
    try:
        if esito == 'APPROVATA':
            cur.execute('UPDATE utenti SET saldo = %s WHERE id = %s', (saldo_dopo, id_utente))
            
        cur.execute(
            '''INSERT INTO transazioni 
               (id_utente, uid_carta, nome_utente, importo, saldo_prima, saldo_dopo, esito, titolo, tipo, categoria)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
            (id_utente, str(uid_carta), nome_utente, importo, saldo_prima, saldo_dopo, esito, 'Pagamento POS', 'expense', 'other')
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cur.close()
        conn.close()

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
            if importo_str == '' or importo_str == '.':
                return None
            try:
                return Decimal(importo_str)
            except InvalidOperation:
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
            pin_str = pin_str[:-1]
        elif tasto == '#':
            return None
            
        mostra('PIN:', '*' * len(pin_str) + '_' * (4 - len(pin_str)))
        
    time.sleep(0.3)
    return pin_str

def transazione():
    led_blu()
    mostra('POS Pronto', 'Avvicina carta')
    uid, _ = reader.read()
    
    led_giallo()
    mostra('Lettura...', '')
    time.sleep(0.5)
    
    utente = cerca_utente(uid)
    if utente is None:
        led_lampeggia(1, 0, 0, volte=4)
        mostra('Carta', 'Non Valida', 3)
        return
        
    if not utente['attiva']:
        led_lampeggia(1, 0, 0, volte=6)
        mostra('Carta', 'Bloccata', 3)
        try:
            elabora_transazione(utente['id'], utente['uid'], utente['nome'], Decimal("0.00"), 'NEGATA_BLOCCATA', utente['saldo'], utente['saldo'])
        except Exception:
            pass
        return
        
    mostra('Ciao', utente['nome'], 2)
    
    importo = inserisci_importo()
    if importo is None or importo <= Decimal("0.00"):
        led_lampeggia(1, 1, 0, volte=2)
        mostra('Operazione', 'Annullata', 2)
        return
        
    pin = inserisci_pin()
    if pin is None:
        led_lampeggia(1, 1, 0, volte=2)
        mostra('Operazione', 'Annullata', 2)
        return
        
    if not verifica_pin(pin, utente['pin_hash']):
        led_lampeggia(1, 0, 0, volte=5)
        mostra('PIN Errato', 'Negata', 3)
        try:
            elabora_transazione(utente['id'], utente['uid'], utente['nome'], importo, 'NEGATA_PIN', utente['saldo'], utente['saldo'])
        except Exception:
            pass
        return
        
    if utente['saldo'] < importo:
        led_lampeggia(1, 0, 0, volte=5)
        mostra('Fondi', 'Insufficienti', 3)
        try:
            elabora_transazione(utente['id'], utente['uid'], utente['nome'], importo, 'NEGATA_FONDI', utente['saldo'], utente['saldo'])
        except Exception:
            pass
        return
        
    nuovo_saldo = utente['saldo'] - importo
    try:
        elabora_transazione(utente['id'], utente['uid'], utente['nome'], importo, 'APPROVATA', utente['saldo'], nuovo_saldo)
    except Exception:
        led_lampeggia(1, 0, 0, volte=3)
        mostra('Errore DB', 'Riprova', 3)
        return
        
    mostra('Approvata', f"{nuovo_saldo:.2f} EUR")
    led_lampeggia(0, 1, 0, volte=6)
    time.sleep(1)

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