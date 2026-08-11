#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FamPay Payment API — standalone service.

Endpoints (match the documentation you were given):
  GET /qr.php?upi=YOUR_UPI_ID&amount=AMOUNT
  GET /verify.php?order_id=ORDER_ID&api_key=YOUR_API_KEY

Runs its own background Gmail watcher (same reliable logic as the bot):
searches recent mail by date (not just UNSEEN, so it never misses a payment
just because Gmail was opened on your phone), tracks processed emails by
Message-ID so nothing is double-counted, and matches incoming FamPay credit
emails to pending orders by amount.
"""

import os
import re
import io
import time
import uuid
import email
import sqlite3
import imaplib
import threading
from datetime import datetime, timedelta
from email.header import decode_header

import qrcode
from flask import Flask, request, jsonify, send_from_directory

try:
    from imapclient import IMAPClient
    HAS_IMAPCLIENT = True
except ImportError:
    HAS_IMAPCLIENT = False

# ============================================
# CONFIG — sab yahan se control hota hai
# ============================================
GMAIL_USER = os.environ.get("GMAIL_USER", "karanbhaiya699@gmail.com")
APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "zrik hlyk ttdl qpol")
API_KEY = os.environ.get("API_KEY", "CHANGE_THIS_TO_YOUR_OWN_SECRET_KEY")

DEV_TAG = "@ANUJxCODER"   # replace with your own handle if you like
ORDER_VALID_MINUTES = 5

DB_FILE = os.environ.get("DB_FILE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fampay_api.db"))
QR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr_codes")
os.makedirs(QR_DIR, exist_ok=True)

app = Flask(__name__)

# ============================================
# DATABASE
# ============================================

def get_conn():
    return sqlite3.connect(DB_FILE)

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            upi_id TEXT,
            requested_amount REAL,
            status TEXT DEFAULT 'pending',
            utr TEXT,
            transaction_id TEXT,
            sender_name TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME,
            paid_at DATETIME
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS processed_emails (
            message_id TEXT PRIMARY KEY,
            processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def create_order(order_id, upi_id, amount):
    conn = get_conn()
    c = conn.cursor()
    now = datetime.now()
    expires = now + timedelta(minutes=ORDER_VALID_MINUTES)
    c.execute('''
        INSERT INTO orders (order_id, upi_id, requested_amount, status, created_at, expires_at)
        VALUES (?, ?, ?, 'pending', ?, ?)
    ''', (order_id, upi_id, amount, now.isoformat(), expires.isoformat()))
    conn.commit()
    conn.close()
    return now, expires

def get_order(order_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT order_id, upi_id, requested_amount, status, utr, transaction_id, sender_name, created_at, expires_at, paid_at FROM orders WHERE order_id = ?', (order_id,))
    row = c.fetchone()
    conn.close()
    return row

def mark_expired_if_needed(order_id):
    row = get_order(order_id)
    if row and row[3] == 'pending':
        expires_at = datetime.fromisoformat(row[8])
        if datetime.now() > expires_at:
            conn = get_conn()
            c = conn.cursor()
            c.execute("UPDATE orders SET status = 'expired' WHERE order_id = ?", (order_id,))
            conn.commit()
            conn.close()

def complete_order(order_id, utr, transaction_id, sender_name):
    conn = get_conn()
    c = conn.cursor()
    c.execute('''
        UPDATE orders SET status = 'completed', utr = ?, transaction_id = ?, sender_name = ?, paid_at = ?
        WHERE order_id = ?
    ''', (utr, transaction_id, sender_name, datetime.now().isoformat(), order_id))
    conn.commit()
    conn.close()

def get_pending_order_by_amount(amount):
    """Exact-amount match first; if the order was created with amount=0 (dynamic),
    fall back to the oldest still-valid dynamic order."""
    conn = get_conn()
    c = conn.cursor()
    c.execute('''
        SELECT order_id FROM orders
        WHERE status = 'pending' AND ABS(requested_amount - ?) <= 0.01 AND expires_at > ?
        ORDER BY created_at ASC LIMIT 1
    ''', (amount, datetime.now().isoformat()))
    row = c.fetchone()
    if row:
        conn.close()
        return row[0]
    c.execute('''
        SELECT order_id FROM orders
        WHERE status = 'pending' AND requested_amount = 0 AND expires_at > ?
        ORDER BY created_at ASC LIMIT 1
    ''', (datetime.now().isoformat(),))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def is_email_processed(message_id):
    if not message_id:
        return False
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT 1 FROM processed_emails WHERE message_id = ?', (message_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

def mark_email_processed(message_id):
    if not message_id:
        return
    conn = get_conn()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO processed_emails (message_id) VALUES (?)', (message_id,))
    conn.commit()
    conn.close()

# ============================================
# ORDER ID / QR GENERATION
# ============================================

def generate_order_id():
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    rand = uuid.uuid4().hex[:10].upper()
    return f"FAMPAY{ts}{rand}"

def generate_qr(order_id, upi_id, amount):
    params = f"pa={upi_id}&pn=Payment&tr={order_id}&cu=INR"
    if amount and float(amount) > 0:
        params += f"&am={amount}"
    upi_link = f"upi://pay?{params}"

    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(upi_link)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    path = os.path.join(QR_DIR, f"{order_id}.png")
    img.save(path)
    return path

# ============================================
# EMAIL PAYMENT DETECTION (FamPay)
# ============================================

def get_subject(msg):
    subject_raw = msg.get("Subject", "")
    try:
        decoded, encoding = decode_header(subject_raw)[0]
        if isinstance(decoded, bytes):
            return decoded.decode(encoding or "utf-8", errors="ignore")
        return decoded
    except Exception:
        return str(subject_raw)

def get_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode('utf-8', errors='ignore')
                    break
        if not body:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        html = payload.decode('utf-8', errors='ignore')
                        body = re.sub(r'<[^>]+>', ' ', html)
                        break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode('utf-8', errors='ignore')
    return body

def extract_amount(text):
    match = re.search(r'(?:Rs\.?|₹|INR)\s*([\d,]+\.?\d*)', text, re.IGNORECASE)
    if match:
        return float(match.group(1).replace(',', ''))
    match = re.search(r'([\d,]+\.?\d*)\s*(?:INR|Rs\.?)', text, re.IGNORECASE)
    if match:
        return float(match.group(1).replace(',', ''))
    return None

def extract_utr(text):
    patterns = [
        r'UPI\s*Ref(?:erence)?\s*(?:No\.?|Number)?\s*[:.]?\s*([A-Z0-9]{6,})',
        r'(?:UTR|Transaction\s*ID|Txn\s*ID|RRN)\s*[:.]?\s*([A-Z0-9-]{6,})',
        r'Reference\s*(?:No\.?|Number)?\s*[:.]?\s*([A-Z0-9-]{6,})',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None

def extract_sender(text):
    match = re.search(r'(?:From|Sender|Credited to|Paid by|Name)\s*:?\s*([A-Za-z\s\.]+)', text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return "Unknown"

def process_email(msg):
    subject = get_subject(msg)
    if not any(k in subject.lower() for k in ['credit', 'credited', 'received', 'upi', 'payment', 'fam']):
        return
    body = get_body(msg)
    text = subject + " " + body

    amount = extract_amount(text)
    if not amount:
        return

    utr = extract_utr(text) or f"FMP{int(time.time())}"
    sender = extract_sender(text)

    order_id = get_pending_order_by_amount(amount)
    if not order_id:
        print(f" No pending order for Rs.{amount:.2f}")
        return

    transaction_id = "FMPIB" + uuid.uuid4().hex[:10].upper()
    complete_order(order_id, utr, transaction_id, sender)
    print(f" Order {order_id} COMPLETED — Rs.{amount:.2f} (UTR: {utr})")

def imap_scan(fetch_ids_fn, fetch_msg_fn):
    """Shared scan logic — fetch_ids_fn() returns message ids for the last 2
    days, fetch_msg_fn(id) returns a parsed email.message.Message."""
    for msg_id in fetch_ids_fn():
        msg = fetch_msg_fn(msg_id)
        if msg is None:
            continue
        message_id = str(msg.get("Message-ID", "")).strip()
        if message_id and is_email_processed(message_id):
            continue
        try:
            process_email(msg)
        except Exception as e:
            print(f"Process error: {e}")
        if message_id:
            mark_email_processed(message_id)

def monitor_idle():
    while True:
        try:
            with IMAPClient('imap.gmail.com', ssl=True, timeout=30) as client:
                client.login(GMAIL_USER, APP_PASSWORD)
                client.select_folder('INBOX')
                print(" IMAP IDLE connected — instant payment detection active.")

                def fetch_ids():
                    since_date = (datetime.now() - timedelta(days=2)).date()
                    return client.search(['SINCE', since_date])

                def fetch_msg(msg_id):
                    data = client.fetch([msg_id], ['RFC822'])
                    raw = data.get(msg_id, {}).get(b'RFC822')
                    return email.message_from_bytes(raw) if raw else None

                imap_scan(fetch_ids, fetch_msg)

                while True:
                    client.idle()
                    try:
                        responses = client.idle_check(timeout=25)
                    finally:
                        client.idle_done()
                    if responses:
                        imap_scan(fetch_ids, fetch_msg)
        except Exception as e:
            print(f" IDLE connection dropped ({e}) — reconnecting in 5s...")
            time.sleep(5)

def monitor_poll():
    while True:
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=15)
            try:
                mail.login(GMAIL_USER, APP_PASSWORD)
                mail.select("INBOX")
                since_date = (datetime.now() - timedelta(days=2)).strftime("%d-%b-%Y")
                status, messages = mail.search(None, f'(SINCE {since_date})')
                ids = messages[0].split() if status == 'OK' else []

                def fetch_ids():
                    return ids

                def fetch_msg(num):
                    status, data = mail.fetch(num, '(RFC822)')
                    if status != 'OK':
                        return None
                    return email.message_from_bytes(data[0][1])

                imap_scan(fetch_ids, fetch_msg)
                mail.close()
            finally:
                try:
                    mail.logout()
                except Exception:
                    pass
        except Exception as e:
            print(f"Email error: {e}")
        time.sleep(3)

def start_monitor():
    target = monitor_idle if HAS_IMAPCLIENT else monitor_poll
    if not HAS_IMAPCLIENT:
        print(" 'imapclient' not installed — falling back to polling mode.")
        print(" For instant detection: pip install imapclient")
    t = threading.Thread(target=target, daemon=True)
    t.start()

# ============================================
# API ROUTES
# ============================================

@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "service": "FamPay Payment API",
        "dev": DEV_TAG,
        "endpoints": ["/qr.php", "/verify.php"],
    })

@app.route("/qr.php")
def qr_endpoint():
    upi = request.args.get("upi", "").strip()
    amount_raw = request.args.get("amount", "0").strip()

    if not upi:
        return jsonify({"status": "error", "message": "Missing 'upi' parameter", "dev": DEV_TAG}), 400

    try:
        amount = float(amount_raw) if amount_raw else 0
    except ValueError:
        return jsonify({"status": "error", "message": "Invalid 'amount' parameter", "dev": DEV_TAG}), 400

    order_id = generate_order_id()
    generate_qr(order_id, upi, amount)
    created_at, expires_at = create_order(order_id, upi, amount)

    qr_url = request.host_url.rstrip("/") + f"/qr_codes/{order_id}.png"

    return jsonify({
        "status": "success",
        "data": {
            "order_id": order_id,
            "qr_url": qr_url,
            "upi_id": upi,
            "amount": str(amount) if amount else "0",
            "created_at_ist": created_at.strftime("%d-%m-%Y %H:%M:%S"),
            "expires_at_ist": expires_at.strftime("%d-%m-%Y %H:%M:%S"),
        },
        "dev": DEV_TAG,
    })

@app.route("/verify.php")
def verify_endpoint():
    order_id = request.args.get("order_id", "").strip()
    api_key = request.args.get("api_key", "").strip()

    if api_key != API_KEY:
        return jsonify({"status": "error", "message": "Invalid API key", "dev": DEV_TAG}), 401

    if not order_id:
        return jsonify({"status": "error", "message": "Missing 'order_id' parameter", "dev": DEV_TAG}), 400

    mark_expired_if_needed(order_id)
    row = get_order(order_id)

    if not row:
        return jsonify({"status": "error", "message": "Order not found", "order_id": order_id, "dev": DEV_TAG}), 404

    _, upi_id, requested_amount, status, utr, transaction_id, sender_name, created_at, expires_at, paid_at = row

    if status == 'completed':
        return jsonify({
            "status": "success",
            "data": {
                "order_id": order_id,
                "transaction_id": transaction_id,
                "amount": requested_amount,
                "utr": utr,
                "sender_name": sender_name,
                "payment_time_ist": datetime.fromisoformat(paid_at).strftime("%d-%m-%Y %H:%M:%S") if paid_at else None,
            },
            "dev": DEV_TAG,
        })

    return jsonify({
        "status": "error",
        "message": "Transaction failed - Payment not received",
        "order_id": order_id,
        "dev": DEV_TAG,
    })

@app.route("/qr_codes/<path:filename>")
def qr_codes(filename):
    return send_from_directory(QR_DIR, filename)

# ============================================
# ENTRYPOINT
# ============================================

init_db()
start_monitor()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
