# -*- coding: utf-8 -*-
# Inizializza il database PostgreSQL con tutte le tabelle
# Eseguire una sola volta, ricrea tutto da zero

import hashlib
import psycopg2

DB_CONFIG = {
    'host':     'localhost',
    'database': 'iot_db',
    'user':     'admin',
    'password': 'password'
}

def hash_pin(pin):
    # Hash sha256 usato per PIN clienti e password esercenti
    return hashlib.sha256(pin.encode()).hexdigest()

def inizializza():
    conn = psycopg2.connect(**DB_CONFIG)
    cur  = conn.cursor()

    # Pulisco tutto in ordine inverso per le foreign key
    cur.execute('DROP TABLE IF EXISTS ricariche')
    cur.execute('DROP TABLE IF EXISTS transazioni')
    cur.execute('DROP TABLE IF EXISTS esercenti')
    cur.execute('DROP TABLE IF EXISTS utenti')

    # Tabella utenti (clienti con carta NFC)
    cur.execute('''
        CREATE TABLE utenti (
            id       SERIAL PRIMARY KEY,
            uid      TEXT NOT NULL UNIQUE,
            nome     TEXT NOT NULL,
            pin_hash TEXT NOT NULL,
            saldo    NUMERIC(10,2) NOT NULL DEFAULT 0.00
        )
    ''')

    # Tabella esercenti (chi gestisce il POS dal sito web)
    cur.execute('''
        CREATE TABLE esercenti (
            id            SERIAL PRIMARY KEY,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            nome_negozio  TEXT NOT NULL
        )
    ''')

    # Tabella transazioni (pagamenti effettuati dal POS fisico)
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

    # Tabella ricariche (ricariche tramite QR code dell'esercente)
    cur.execute('''
        CREATE TABLE ricariche (
            id                SERIAL PRIMARY KEY,
            token             TEXT NOT NULL UNIQUE,
            id_esercente      INTEGER NOT NULL REFERENCES esercenti(id),
            importo           NUMERIC(10,2) NOT NULL,
            id_utente_riceve  INTEGER REFERENCES utenti(id),
            stato             TEXT NOT NULL DEFAULT 'PENDING',
            data_creazione    TIMESTAMP NOT NULL DEFAULT NOW(),
            data_completata   TIMESTAMP,
            scadenza          TIMESTAMP NOT NULL
        )
    ''')

    # Utenti iniziali (clienti)
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

    # Esercenti iniziali (per testare il login)
    esercenti = [
        ('admin',   'admin123', 'Negozio Principale'),
        ('mario',   'mario123', 'Bar Mario'),
    ]
    for username, password, nome_negozio in esercenti:
        cur.execute(
            'INSERT INTO esercenti (username, password_hash, nome_negozio) VALUES (%s, %s, %s)',
            (username, hash_pin(password), nome_negozio)
        )
        print('Esercente inserito: ' + username + ' (' + nome_negozio + ')')

    conn.commit()
    cur.close()
    conn.close()
    print()
    print('Database pronto.')

if __name__ == '__main__':
    inizializza()