# -*- coding: utf-8 -*-
# Script da eseguire una volta sola per creare le tabelle
# e inserire gli utenti iniziali nel database PostgreSQL

import hashlib
import psycopg2

DB_CONFIG = {
    'host': 'localhost',
    'database': 'iot_db',
    'user': 'admin',
    'password': 'password'
}

def hash_pin(pin):
    # Converte il PIN in hash sha256, mai salvato in chiaro
    return hashlib.sha256(pin.encode()).hexdigest()

def inizializza():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    cur.execute('DROP TABLE IF EXISTS transazioni')
    cur.execute('DROP TABLE IF EXISTS utenti')

    cur.execute('''
        CREATE TABLE utenti (
            id       SERIAL PRIMARY KEY,
            uid      TEXT NOT NULL UNIQUE,
            nome     TEXT NOT NULL,
            pin_hash TEXT NOT NULL,
            saldo    NUMERIC(10,2) NOT NULL DEFAULT 0.00
        )
    ''')

    cur.execute('''
        CREATE TABLE transazioni (
            id           SERIAL PRIMARY KEY,
            uid_carta    TEXT NOT NULL,
            nome_utente  TEXT NOT NULL,
            importo      NUMERIC(10,2) NOT NULL,
            saldo_prima  NUMERIC(10,2) NOT NULL,
            saldo_dopo   NUMERIC(10,2) NOT NULL,
            esito        TEXT NOT NULL,
            data_ora     TIMESTAMP NOT NULL DEFAULT NOW()
        )
    ''')

    utenti = [
        ('584195345601', 'Mario Rossi',  '1234', 150.75),
        ('111222333444', 'Luca Bianchi', '5678',  50.00),
        ('555666777888', 'Anna Verdi',   '0000', 1000.00),
    ]
    for uid, nome, pin, saldo in utenti:
        cur.execute(
            'INSERT INTO utenti (uid, nome, pin_hash, saldo) VALUES (%s, %s, %s, %s)',
            (uid, nome, hash_pin(pin), saldo)
        )
        print('Utente inserito: ' + nome)

    conn.commit()
    cur.close()
    conn.close()
    print()
    print('Database pronto.')

if __name__ == '__main__':
    inizializza()