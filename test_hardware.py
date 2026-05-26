# -*- coding: utf-8 -*-
"""
Test Hardware POS IoT - Verifica tutti i componenti senza app
RC522, OLED SH1106, Tastierino HX-643, LED RGB
"""

import time
import sys
import os
from decimal import Decimal

import RPi.GPIO as GPIO
from mfrc522 import SimpleMFRC522
from luma.core.interface.serial import i2c
from luma.oled.device import sh1106
from luma.core.render import canvas
from PIL import ImageFont

# Soppressione AUTH ERROR di mfrc522
devnull = open(os.devnull, "w")
sys_stderr_backup = sys.stderr
sys.stderr = devnull

# ============================================================
# Config Hardware (BOARD MODE)
# ============================================================

KEYPAD = [
    ['1', '2', '3'],
    ['4', '5', '6'],
    ['7', '8', '9'],
    ['*', '0', '#']
]

ROW_PINS = [20, 16, 12, 19]
COL_PINS = [13, 6, 5]

PIN_ROSSO  = 17
PIN_VERDE  = 27
PIN_BLU    = 22

PIN_GND    = 14

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

# Setup LED RGB
for pin in [PIN_ROSSO, PIN_VERDE, PIN_BLU]:
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.LOW)

# Setup Tastierino
for pin in ROW_PINS:
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.HIGH)
for pin in COL_PINS:
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

# Setup OLED
try:
    serial_i2c = i2c(port=1, address=0x3C)
    device = sh1106(serial_i2c)
    font_grande = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
    font_medio  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
except Exception as e:
    font_grande = ImageFont.load_default()
    font_medio  = ImageFont.load_default()
    print(f"[WARN] Font non trovato: {e}")

# Setup RC522
reader = SimpleMFRC522()

# ============================================================
# Funzioni Helper
# ============================================================

def led(r=0, g=0, b=0):
    GPIO.output(PIN_ROSSO, r)
    GPIO.output(PIN_VERDE, g)
    GPIO.output(PIN_BLU,   b)

def led_spegni():     led(0, 0, 0)
def led_rosso():      led(1, 0, 0)
def led_verde():      led(0, 1, 0)
def led_blu():        led(0, 0, 1)
def led_giallo():     led(1, 1, 0)
def led_viola():      led(1, 0, 1)
def led_azzurro():    led(0, 1, 1)
def led_bianco():     led(1, 1, 1)

def led_lampeggia(r, g, b, volte=3, intervallo=0.3):
    for _ in range(volte):
        led(r, g, b)
        time.sleep(intervallo)
        led_spegni()
        time.sleep(intervallo)

def mostra(riga1, riga2='', durata=0):
    try:
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
    except Exception as e:
        print(f"[OLED Error] {e}")

def leggi_tasto(timeout=5):
    """Legge un singolo tasto con timeout in secondi"""
    inizio = time.time()
    while time.time() - inizio < timeout:
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
        time.sleep(0.05)
    return None

# ============================================================
# Test 1: LED RGB
# ============================================================

def test_led():
    print("\n[TEST 1] LED RGB")
    print("Testando LED con sequenza colori...")
    led_spegni()
    
    colori = [
        (1, 0, 0, "ROSSO"),
        (0, 1, 0, "VERDE"),
        (0, 0, 1, "BLU"),
        (1, 1, 0, "GIALLO"),
        (1, 0, 1, "VIOLA"),
        (0, 1, 1, "AZZURRO"),
        (1, 1, 1, "BIANCO"),
    ]
    
    for r, g, b, nome in colori:
        led(r, g, b)
        mostra("LED Test", nome, 1)
        print(f"  ✓ {nome}")
    
    led_spegni()
    print("✓ LED RGB: OK")


# ============================================================
# Test 2: Display OLED
# ============================================================

def test_oled():
    print("\n[TEST 2] OLED SH1106")
    print("Testando display OLED...")
    led_giallo()
    mostra("OLED Test", "Connesso OK", 2)
    led_spegni()
    print("✓ OLED SH1106: OK")


# ============================================================
# Test 3: Tastierino HX-643
# ============================================================

def test_tastierino():
    print("\n[TEST 3] Tastierino HX-643")
    print("Premi 5 tasti (timeout 15 secondi)...")
    led_blu()
    
    tasti_letti = []
    inizio = time.time()
    timeout_totale = 15
    
    while len(tasti_letti) < 5 and time.time() - inizio < timeout_totale:
        tempo_rimanente = int(timeout_totale - (time.time() - inizio))
        mostra("Tastierino", f"Premi... ({tempo_rimanente}s)")
        
        for i, row_pin in enumerate(ROW_PINS):
            GPIO.output(row_pin, GPIO.LOW)
            for j, col_pin in enumerate(COL_PINS):
                if GPIO.input(col_pin) == GPIO.LOW:
                    tasto = KEYPAD[i][j]
                    tasti_letti.append(tasto)
                    mostra("Tasto", tasto, 0.5)
                    print(f"  ✓ Tasto letto: {tasto}")
                    while GPIO.input(col_pin) == GPIO.LOW:
                        time.sleep(0.02)
                    if len(tasti_letti) >= 5:
                        break
            GPIO.output(row_pin, GPIO.HIGH)
            if len(tasti_letti) >= 5:
                break
        time.sleep(0.05)
    
    led_spegni()
    if len(tasti_letti) >= 5:
        print(f"✓ Tastierino HX-643: OK (tasti letti: {', '.join(tasti_letti)})")
    else:
        print(f"✗ Tastierino HX-643: FALLITO (solo {len(tasti_letti)} tasti letti in 15s)")


# ============================================================
# Test 4: RC522 RFID
# ============================================================

def test_rc522():
    print("\n[TEST 4] RC522 RFID/NFC")
    print("Avvicina una carta NFC (timeout 20 secondi)...")
    led_blu()
    mostra("RC522", "Avvicina carta", 0)
    
    inizio = time.time()
    while time.time() - inizio < 20:
        try:
            uid, text = reader.read_no_block()
            if uid:
                led_verde()
                mostra("RC522 OK", f"UID: {uid}", 0)
                print(f"✓ RC522 RFID: OK")
                print(f"  UID riconosciuto: {uid}")
                time.sleep(2)
                return True
        except Exception as e:
            print(f"[RC522 Error] {e}")
        
        tempo_rimanente = int(20 - (time.time() - inizio))
        mostra("RC522", f"Attesa... ({tempo_rimanente}s)", 0)
        time.sleep(0.2)
    
    led_rosso()
    mostra("RC522", "Timeout 20s", 2)
    print(f"✗ RC522 RFID: FALLITO (nessuna carta rilevata in 20s)")
    return False


# ============================================================
# Test 5: Database PostgreSQL
# ============================================================

def test_database():
    print("\n[TEST 5] Database PostgreSQL")
    
    try:
        import psycopg2
        from dotenv import load_dotenv
        
        load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
        
        DB_CONFIG = {
            "host":     os.getenv("DB_HOST", "localhost"),
            "database": os.getenv("DB_NAME", "iot_db"),
            "user":     os.getenv("DB_USER", "admin"),
            "password": os.getenv("DB_PASSWORD", ""),
        }
        
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        
        cur.execute("SELECT COUNT(*) FROM utenti")
        n_utenti = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM transazioni")
        n_trans = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM esercenti")
        n_esercenti = cur.fetchone()[0]
        
        cur.close()
        conn.close()
        
        led_verde()
        mostra("DB OK", f"U:{n_utenti} T:{n_trans}", 2)
        print(f"✓ Database PostgreSQL: OK")
        print(f"  Utenti: {n_utenti}")
        print(f"  Transazioni: {n_trans}")
        print(f"  Esercenti: {n_esercenti}")
        
    except Exception as e:
        led_rosso()
        mostra("DB FALLITO", str(e)[:20], 2)
        print(f"✗ Database PostgreSQL: FALLITO")
        print(f"  Errore: {e}")


# ============================================================
# Main
# ============================================================

def cleanup():
    led_spegni()
    device.clear()
    sys.stderr = sys_stderr_backup
    GPIO.cleanup()
    print("\n[CLEANUP] GPIO pulito")

def main():
    try:
        mostra("Test HW", "Avvio...", 1)
        
        test_led()
        time.sleep(1)
        
        test_oled()
        time.sleep(1)
        
        test_tastierino()
        time.sleep(1)
        
        test_rc522()
        time.sleep(1)
        
        test_database()
        time.sleep(1)
        
        # Riepilogo finale
        mostra("Test OK", "Completato", 3)
        print("\n" + "="*60)
        print("TUTTI I TEST COMPLETATI")
        print("="*60)
        
    except KeyboardInterrupt:
        print("\n\n[INTERRUPT] Interruzione utente")
    except Exception as e:
        print(f"\n[ERRORE FATALE] {e}")
        import traceback
        traceback.print_exc()
    finally:
        cleanup()

if __name__ == "__main__":
    main()