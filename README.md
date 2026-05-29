# POS IoT — Backend & terminale fisico

Sistema bancario didattico composto da tre parti:

- **Backend Flask** (Raspberry Pi 4): API REST + sito esercenti, database PostgreSQL
- **Terminale POS fisico** (`pos.py`): RC522 NFC, OLED SH1106, tastierino HX-643, LED RGB
- **App Flutter** (repo separato): client per i clienti finali

## Architettura

```
+-------------------+        HTTPS (ngrok)        +-----------------------+
|   App Flutter     | <-------------------------> |   Flask API REST      |
|   (cliente)       |                             |   (Raspberry Pi 4)    |
+-------------------+                             +-----------+-----------+
                                                              |
                                                  +-----------+-----------+
                                                  |   PostgreSQL 17       |
                                                  +-----------+-----------+
                                                              ^
                                                              |
                                                  +-----------+-----------+
                                                  |   pos.py (terminale)  |
                                                  |   RC522 + OLED +      |
                                                  |   tastierino + LED    |
                                                  +-----------------------+
```

## Stack tecnologico

- **Python** 3.14.x, **Flask** 3.x, **PostgreSQL** 17
- **JWT**: access token (durata configurabile) + refresh token (14 giorni) con blacklist su DB
- **Argon2** per password account e PIN carta (4 cifre, compatibile col lettore fisico)

## Setup

### 1. Prerequisiti

- Raspberry Pi 4 con Pi OS Trixie (Debian 13) arm64
- PostgreSQL 17 in esecuzione

### 2. Database

```bash
sudo -u postgres psql
CREATE USER admin WITH PASSWORD 'PLACEHOLDER_DB_PASSWORD';
CREATE DATABASE iot_db OWNER admin;
\q
```

### 3. Configurazione `.env`

```bash
cp .env.example .env
# Genera i segreti:
python -c "import secrets; print(secrets.token_urlsafe(64))"
# Inserisci i valori in DB_PASSWORD, FLASK_SECRET_KEY, JWT_SECRET
```

### 4. Webserver

```bash
pip install -r requirements.txt
python inizializza_db.py
python webserver.py
```

### 5. Terminale POS fisico (solo sul Pi)

```bash
pip install -r requirements-pos-rpi.txt
python pos.py
```

### 6. Esporre l'API ad app Flutter remota (ngrok)

```bash
ngrok http 5000
# Copia l'URL HTTPS in Banking_App/.env -> API_BASE_URL
```

## Schema database

| Tabella | Scopo |
|---|---|
| `utenti` | Clienti finali: UID carta, PIN (SHA256), saldo, IBAN deterministico, credenziali app (Argon2) |
| `esercenti` | Login sito: ruolo `admin` o `esercente` |
| `transazioni` | Movimenti per utente con tipo (income/expense), categoria, esito |
| `ricariche` | QR generati dagli esercenti (stato: PENDING/COMPLETATA/SCADUTA/ANNULLATA) |
| `bonifici` | Bonifici verso IBAN; se il destinatario è interno viene accreditato |
| `refresh_tokens` | Refresh JWT con flag `revoked` e timestamp |
| `revoked_access_tokens` | Blacklist access token per logout server-side |

## IBAN deterministico

Formato didattico: `IT60POS` + id utente zero-paddato a 14 cifre.
- User id `1` → `IT60POS00000000000001`
- User id `42` → `IT60POS00000000000042`

I bonifici interni vengono riconosciuti tramite questo IBAN e accreditati al destinatario nella stessa transazione DB.

## Endpoint API principali

| Metodo | Path | Descrizione |
|---|---|---|
| `GET`  | `/api/health` | Healthcheck |
| `POST` | `/api/login` | Login con username/password → access + refresh |
| `POST` | `/api/refresh` | Rotazione refresh token |
| `POST` | `/api/logout` | Revoca server-side dei token |
| `GET`  | `/api/me` | Profilo + IBAN + stato carta |
| `GET`  | `/api/balance` | Saldo + entrate/uscite mese corrente |
| `GET`  | `/api/transactions` | Storico transazioni dell'utente |
| `GET`  | `/api/qr/<token>` | Dettagli di un QR ricarica |
| `POST` | `/api/qr/confirm` | Accredita il QR sul conto |
| `POST` | `/api/transfer` | Bonifico (riconosce IBAN interni) |
| `POST` | `/api/nfc/pay` | Pagamento NFC contactless |
| `POST` | `/api/card/status` | Blocco/sblocco carta |

## Pinout hardware (BOARD numbering)

| Componente | Pin |
|---|---|
| **RC522** SDA | 24 |
| **RC522** SCK | 23 |
| **RC522** MOSI | 19 |
| **RC522** MISO | 21 |
| **RC522** RST | 22 |
| **OLED SH1106** SDA | 3 |
| **OLED SH1106** SCL | 5 |
| **Tastierino** righe | 38, 36, 32, 35 |
| **Tastierino** colonne | 33, 31, 29 |
| **LED RGB** R / G / B | 15 / 13 / 11 |
| **LED RGB** GND | 14 |
| **Buzzer PWM** | 16 |

## Comportamento del POS fisico

| Stato | LED | Display |
|---|---|---|
| Attesa carta | Blu fisso | "POS Pronto / Avvicina carta" |
| Lettura/elaborazione | Giallo fisso | "Lettura..." / "Elaboro..." |
| Approvata | Verde lampeggiante | "Approvata / <saldo> EUR" |
| PIN errato | Rosso lampeggiante | "PIN errato / Negata" |
| Fondi insufficienti | Rosso lampeggiante | "Fondi / insufficienti" |
| Carta bloccata | Rosso lampeggiante | "Carta / Bloccata" |
| Annullamento (#) | Viola fisso | "Operazione / Annullata" |
| Carta non valida | Rosso lampeggiante | "Carta / Non valida" |

## Credenziali di test

**Sito esercente** (`/login`):
- `admin / admin123` (amministratore)
- `mario / mario123`, `luca / luca123` (esercenti)

**App Flutter** (`/api/login`):
- `mario.rossi / password123`
- `luca.bianchi / password123`
- `anna.verdi / password123`


## Funzionalità privacy / sicurezza

- **Esercente**: vede solo le sue ricariche, niente nomi di chi le ha completate
- **Admin**: vede solo conteggi e log anonimizzati (id, importo, esito) — nessun saldo, IBAN o nome associato
- **Cliente**: solo nella sua app autenticata può vedere saldo, IBAN, storico
- **Argon2** per password account e PIN carta (4 cifre, compatibile col lettore fisico)
- **Flask-Limiter** per anti-bruteforce sul login

Anche l'admin non ha route per vedere il dettaglio di un singolo cliente: il pannello mostra esclusivamente metriche aggregate.

