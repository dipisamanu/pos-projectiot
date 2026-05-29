# -*- coding: utf-8 -*-
"""
Inizializza database POS IoT
- Tabelle: utenti, esercenti, transazioni, ricariche, bonifici,
  refresh_tokens, revoked_access_tokens
- IBAN deterministico per utenti (IT60POS + id zero-paddato 14 cifre)
- Password e PIN Argon2
"""

import os
from datetime import datetime, timedelta
from decimal import Decimal

import psycopg2
from argon2 import PasswordHasher
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "iot_db"),
    "user": os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", ""),
}

if not DB_CONFIG["password"]:
    raise RuntimeError("DB_PASSWORD non impostata nel file .env")

CATEGORIE_VALIDE = {
    "shopping",
    "transport",
    "food",
    "entertainment",
    "health",
    "travel",
    "utilities",
    "salary",
    "transfer",
    "education",
    "subscriptions",
    "other",
}

ph = PasswordHasher()


def hash_pin(pin: str) -> str:
    # PIN della carta NFC: Argon2 (salted, resistente a rainbow table)
    return ph.hash(pin)


def hash_password(password: str) -> str:
    # Password app/esercente: Argon2 (sicuro contro brute force)
    return ph.hash(password)


def genera_iban(user_id: int) -> str:
    # Stesso formato usato dal webserver
    return f"IT60POS{user_id:014d}"


def inizializza():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    # Pulizia in ordine inverso per le foreign key
    cur.execute("DROP TABLE IF EXISTS revoked_access_tokens")
    cur.execute("DROP TABLE IF EXISTS refresh_tokens")
    cur.execute("DROP TABLE IF EXISTS bonifici")
    cur.execute("DROP TABLE IF EXISTS ricariche")
    cur.execute("DROP TABLE IF EXISTS transazioni")
    cur.execute("DROP TABLE IF EXISTS esercenti")
    cur.execute("DROP TABLE IF EXISTS utenti")
    cur.execute("DROP TABLE IF EXISTS richieste_pagamento")

    cur.execute("""
        CREATE TABLE utenti (
            id            SERIAL PRIMARY KEY,
            uid           TEXT NOT NULL UNIQUE,
            nome          TEXT NOT NULL,
            pin_hash      TEXT NOT NULL,
            saldo         NUMERIC(12,2) NOT NULL DEFAULT 0.00,
            attiva        BOOLEAN NOT NULL DEFAULT TRUE,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            iban          TEXT UNIQUE
        )
    """)

    cur.execute("""
        CREATE TABLE esercenti (
            id            SERIAL PRIMARY KEY,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            nome_negozio  TEXT NOT NULL,
            ruolo         TEXT NOT NULL DEFAULT 'esercente'
        )
    """)

    cur.execute("""
        CREATE TABLE refresh_tokens (
            id           SERIAL PRIMARY KEY,
            user_id      INTEGER NOT NULL REFERENCES utenti(id) ON DELETE CASCADE,
            token_hash   TEXT NOT NULL UNIQUE,
            expires_at   TIMESTAMP NOT NULL,
            revoked      BOOLEAN NOT NULL DEFAULT FALSE,
            created_at   TIMESTAMP NOT NULL DEFAULT NOW(),
            revoked_at   TIMESTAMP,
            last_used_at TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE revoked_access_tokens (
            id         SERIAL PRIMARY KEY,
            jti        TEXT NOT NULL UNIQUE,
            expires_at TIMESTAMP NOT NULL,
            revoked_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE transazioni (
            id           SERIAL PRIMARY KEY,
            id_utente    INTEGER REFERENCES utenti(id),
            uid_carta    TEXT NOT NULL,
            nome_utente  TEXT NOT NULL,
            titolo       TEXT NOT NULL DEFAULT 'Transazione',
            tipo         TEXT NOT NULL DEFAULT 'expense',
            categoria    TEXT NOT NULL DEFAULT 'other',
            importo      NUMERIC(12,2) NOT NULL,
            saldo_prima  NUMERIC(12,2) NOT NULL,
            saldo_dopo   NUMERIC(12,2) NOT NULL,
            esito        TEXT NOT NULL DEFAULT 'APPROVATA',
            data_ora     TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE ricariche (
            id                SERIAL PRIMARY KEY,
            token             TEXT NOT NULL UNIQUE,
            id_esercente      INTEGER NOT NULL REFERENCES esercenti(id),
            importo           NUMERIC(12,2) NOT NULL,
            id_utente_riceve  INTEGER REFERENCES utenti(id),
            stato             TEXT NOT NULL DEFAULT 'PENDING',
            data_creazione    TIMESTAMP NOT NULL DEFAULT NOW(),
            data_completata   TIMESTAMP,
            scadenza          TIMESTAMP NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE bonifici (
            id                SERIAL PRIMARY KEY,
            id_utente         INTEGER NOT NULL REFERENCES utenti(id),
            beneficiario      TEXT NOT NULL,
            iban_destinatario TEXT NOT NULL,
            causale           TEXT NOT NULL,
            importo           NUMERIC(12,2) NOT NULL,
            stato             TEXT NOT NULL DEFAULT 'COMPLETATO',
            id_utente_riceve  INTEGER REFERENCES utenti(id),
            creato_il         TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("""
    CREATE TABLE richieste_pagamento (
        id              SERIAL PRIMARY KEY,
        id_esercente    INTEGER NOT NULL REFERENCES esercenti(id),
        importo         NUMERIC(12,2) NOT NULL,
        descrizione     TEXT NOT NULL DEFAULT 'Pagamento POS',
        stato           TEXT NOT NULL DEFAULT 'PENDING',
        id_utente_paga  INTEGER REFERENCES utenti(id),
        data_creazione  TIMESTAMP NOT NULL DEFAULT NOW(),
        data_completata TIMESTAMP,
        scadenza        TIMESTAMP NOT NULL
    )
""")

    # Seed utenti (clienti app) + generazione IBAN deterministico
    utenti_seed = [
        (
            "584195345601",
            "Mario Rossi",
            "1234",
            Decimal("2450.00"),
            "mario.rossi",
            "password123",
        ),
        (
            "111222333444",
            "Luca Bianchi",
            "5678",
            Decimal("980.20"),
            "luca.bianchi",
            "password123",
        ),
        (
            "555666777888",
            "Anna Verdi",
            "0000",
            Decimal("5270.55"),
            "anna.verdi",
            "password123",
        ),
    ]
    for uid, nome, pin, saldo, username, pwd in utenti_seed:
        cur.execute(
            """INSERT INTO utenti (uid, nome, pin_hash, saldo, attiva, username, password_hash)
               VALUES (%s, %s, %s, %s, TRUE, %s, %s) RETURNING id""",
            (uid, nome, hash_pin(pin), saldo, username, hash_password(pwd)),
        )
        new_id = cur.fetchone()[0]
        iban = genera_iban(new_id)
        cur.execute("UPDATE utenti SET iban=%s WHERE id=%s", (iban, new_id))
        print(
            f"Utente inserito: {nome} (login: {username} / password123) - IBAN: {iban}"
        )

    # Seed esercenti (login sito)
    esercenti = [
        ("admin", "admin123", "Amministratore", "admin"),
        ("mario", "mario123", "Bar Mario", "esercente"),
        ("luca", "luca123", "Pizzeria Luca", "esercente"),
    ]
    for username, password, nome_negozio, ruolo in esercenti:
        cur.execute(
            """INSERT INTO esercenti (username, password_hash, nome_negozio, ruolo)
               VALUES (%s, %s, %s, %s)""",
            (username, hash_password(password), nome_negozio, ruolo),
        )
        print(f"Esercente inserito: {username} ({ruolo})")

    # Transazioni demo per Mario Rossi (popolano la dashboard dell'app)
    cur.execute("SELECT id, uid, nome, saldo FROM utenti WHERE username='mario.rossi'")
    mario = cur.fetchone()
    if mario:
        id_u, uid_u, nome_u, saldo = mario
        saldo = Decimal(saldo)
        demo = [
            ("Accredito Stipendio Maggio", "income", "salary", Decimal("2620.00")),
            ("LIDL", "expense", "food", Decimal("64.30")),
            ("Benzina Eni", "expense", "transport", Decimal("50.00")),
            ("Netflix Monthly", "expense", "subscriptions", Decimal("17.99")),
            ("Amazon Shopping", "expense", "shopping", Decimal("124.50")),
            ("Cena Sushi", "expense", "food", Decimal("45.00")),
            ("Rimborso Spese", "income", "transfer", Decimal("120.00")),
            ("Palestra", "expense", "health", Decimal("55.00")),
            ("Cinema UCI", "expense", "entertainment", Decimal("12.50")),
            ("Bolletta Enel", "expense", "utilities", Decimal("89.20")),
            ("Corso Udemy", "expense", "education", Decimal("19.99")),
            ("Volo Ryanair", "expense", "travel", Decimal("85.00")),
        ]
        for i, (titolo, tipo, categoria, importo) in enumerate(demo):
            saldo_prima = saldo
            saldo = saldo + importo if tipo == "income" else saldo - importo
            cur.execute(
                """INSERT INTO transazioni
                   (id_utente, uid_carta, nome_utente, titolo, tipo, categoria,
                    importo, saldo_prima, saldo_dopo, esito, data_ora)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'APPROVATA',%s)""",
                (
                    id_u,
                    uid_u,
                    nome_u,
                    titolo,
                    tipo,
                    categoria,
                    importo,
                    saldo_prima,
                    saldo,
                    datetime.now() - timedelta(days=i * 2),
                ),
            )
        cur.execute("UPDATE utenti SET saldo=%s WHERE id=%s", (saldo, id_u))
        print(f"Inserite {len(demo)} transazioni demo per Mario Rossi")

    conn.commit()
    cur.close()
    conn.close()
    print("\nDatabase inizializzato correttamente.")


if __name__ == "__main__":
    inizializza()
