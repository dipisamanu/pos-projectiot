# -*- coding: utf-8 -*-
"""
Test del tastierino HX-643 3x4 con pin corretti rilevati dal test diagnostico.
"""

import RPi.GPIO as GPIO
import time

KEYPAD = [
    ['1', '2', '3'],
    ['4', '5', '6'],
    ['7', '8', '9'],
    ['*', '0', '#']
]

ROW_PINS = [20, 16, 12, 19]
COL_PINS = [13, 6, 5]

# Inizializzazione GPIO
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

# Le righe sono uscite, inizialmente alte
for pin in ROW_PINS:
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.HIGH)

# Le colonne sono ingressi con resistenza pull-up interna
for pin in COL_PINS:
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)


def leggi_tasto():
    """Scansiona la matrice e restituisce il tasto premuto o None."""
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


def main():
    """Ciclo di lettura continua del tastierino."""
    print('=== Test Tastierino HX-643 3x4 ===')
    print('Premi i tasti. CTRL+C per uscire.')
    print()
    try:
        while True:
            tasto = leggi_tasto()
            if tasto is not None:
                print('Tasto premuto: ' + tasto)
            time.sleep(0.05)
    except KeyboardInterrupt:
        print('\nTest terminato.')
    finally:
        GPIO.cleanup()


if __name__ == '__main__':
    main()