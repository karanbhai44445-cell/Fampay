/#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FamPay & Multi-UPI Automated Payment Gateway & Live Terminal
Developer Tag: @karanbhaiyaaaa
"""

import os
import re
import time
import uuid
import email
import sqlite3
import imaplib
import threading
from datetime import datetime, timedelta
from email.header import decode_header

import qrcode
from flask import Flask, request, jsonify, send_from_directory, render_template_string

try:
    from imapclient import IMAPClient
    HAS_IMAPCLIENT = True
except ImportError:
    HAS_IMAPCLIENT = False

# ============================================
# CONFIGURATION (ENV VARIABLES PREFERRED)
# ============================================
GMAIL_USER = os.environ.get("GMAIL_USER", "your_email@gmail.com")
APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "your_app_password")
API_KEY = os.environ.get("API_KEY", "your_secret_api_key")
DEFAULT_UPI_ID = os.environ.get("DEFAULT_UPI_ID", "Vikrambhaiyaaa@fam")

DEV_TAG = "@karanbhaiyaaaa"
ORDER_VALID_MINUTES = 5

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get("DB_FILE_PATH", os.path.join(BASE_DIR, "fampay_api.db"))
QR_DIR = os.path.join(BASE_DIR, "qr_codes")
os.makedirs(QR_DIR, exist_ok=True)

app = Flask(__name__)

# ============================================
# DATABASE FUNCTIONS (OPTIMIZED FOR CONCURRENCY)
# ============================================
def get_conn():
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

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
    conn = get_conn()
    c = conn.cursor()
    # Exact amount matching within non-expired orders
    c.execute('''
        SELECT order_id FROM orders
        WHERE status = 'pending' AND ABS(requested_amount - ?) <= 0.01 AND expires_at > ?
        ORDER BY created_at ASC LIMIT 1
    ''', (amount, datetime.now().isoformat()))
    row = c.fetchone()
    if row:
        conn.close()
        return row[0]
    
    # Fallback for dynamic/variable payment orders (where amount was generated as 0)
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
# HELPER & PARSING FUNCTIONS
# ============================================
def generate_order_id():
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    rand = uuid.uuid4().hex[:6].upper()
    return f"FAM{ts}{rand}"

def generate_qr(order_id, upi_id, amount):
    params = f"pa={upi_id}&pn=MerchantPayment&tr={order_id}&cu=INR"
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

def get_subject(msg):
    subject_raw = msg.get("Subject", "")
    try:
        decoded, encoding = decode_header(subject_raw)[0]
        if isinstance(decoded, bytes):
            return decoded.decode(encoding or "utf-8", errors="ignore")
        return str(decoded)
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
    patterns = [
        r'(?:Rs\.?|INR|₹)\s*([\d,]+\.?\d*)',
        r'([\d,]+\.?\d*)\s*(?:INR|Rs\.?|credited|received)',
        r'received\s*(?:a\s*payment\s*of)?\s*(?:Rs\.?|₹|INR)\s*([\d,]+\.?\d*)'
    ]
    for pat in patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            try:
                val = float(match.group(1).replace(',', ''))
                if val > 0:
                    return val
            except ValueError:
                continue
    return None

def extract_utr(text):
    patterns = [
        r'(?:UPI\s*Ref(?:erence)?\s*(?:No\.?|Number)?|UTR|Txn\s*ID|RRN|Ref\s*No)\s*[:.]?\s*([A-Z0-9]{8,20})',
        r'Reference\s*(?:No\.?|Number)?\s*[:.]?\s*([A-Z0-9]{8,20})',
        r'([0-9]{12})' # standard 12-digit UPI RRN fallback
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None

def extract_sender(text):
    patterns = [
        r'(?:From|Paid by|Received from|Sender|Credited by)\s*:?\s*([A-Za-z0-9\s\.\@]+)',
        r'by\s+([A-Za-z\s\.]+)\s+via\s+UPI'
    ]
    for pat in patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            sender = match.group(1).strip().split('\n')[0]
            return sender[:30]
    return "UPI Customer"

def process_email(msg):
    subject = get_subject(msg)
    keywords = ['credit', 'credited', 'received', 'upi', 'payment', 'fam', 'paytm', 'phonepe']
    if not any(k in subject.lower() for k in keywords):
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
        return

    transaction_id = "FMPIB" + uuid.uuid4().hex[:10].upper()
    complete_order(order_id, utr, transaction_id, sender)

def imap_scan(fetch_ids_fn, fetch_msg_fn):
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

                def fetch_ids():
                    since_date = (datetime.now() - timedelta(days=1)).date()
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
            time.sleep(5)

def monitor_poll():
    while True:
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=15)
            try:
                mail.login(GMAIL_USER, APP_PASSWORD)
                mail.select("INBOX")
                since_date = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
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
            pass
        time.sleep(4)

def start_monitor():
    target = monitor_idle if HAS_IMAPCLIENT else monitor_poll
    t = threading.Thread(target=target, daemon=True)
    t.start()

# ============================================
# WEB UI DASHBOARD
# ============================================
HTML_UI = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FamPay Payment Gateway Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        body { background-color: #0f172a; color: #f8fafc; font-family: 'Segoe UI', system-ui, sans-serif; }
        .navbar { background-color: #1e293b; border-bottom: 1px solid #334155; }
        .card { background-color: #1e293b; border: 1px solid #334155; border-radius: 12px; color: #f8fafc; }
        .nav-tabs .nav-link { color: #94a3b8; border: none; font-weight: 600; font-size: 1.05rem; }
        .nav-tabs .nav-link.active { color: #38bdf8; background: transparent; border-bottom: 3px solid #38bdf8; }
        .btn-primary { background: linear-gradient(135deg, #38bdf8, #2563eb); border: none; font-weight: 600; }
        .qr-container { background: #ffffff; padding: 15px; border-radius: 12px; display: inline-block; }
        pre { background-color: #090d16; border: 1px solid #334155; color: #38bdf8; padding: 12px; border-radius: 8px; font-size: 0.85rem; }
        .badge-dev { background: #334155; color: #38bdf8; }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark mb-4">
        <div class="container">
            <a class="navbar-brand font-bold" href="#"><i class="fa-solid fa-bolt text-warning me-2"></i>FamPay Gateway Hub</a>
            <span class="badge badge-dev px-3 py-2 rounded-pill">{{ dev_tag }}</span>
        </div>
    </nav>
    <div class="container mb-5">
        <ul class="nav nav-tabs mb-4 justify-content-center" id="gatewayTabs" role="tablist">
            <li class="nav-item">
                <button class="nav-link active" id="docs-tab" data-bs-toggle="tab" data-bs-target="#docs" type="button">API Documentation</button>
            </li>
            <li class="nav-item">
                <button class="nav-link" id="testing-tab" data-bs-toggle="tab" data-bs-target="#testing" type="button">Test Payment QR</button>
            </li>
        </ul>
        <div class="tab-content" id="gatewayTabsContent">
            <div class="tab-pane fade show active" id="docs">
                <div class="row justify-content-center">
                    <div class="col-lg-10">
                        <div class="card p-4 shadow-sm mb-4">
                            <h4 class="text-info mb-3">Gateway Endpoints</h4>
                            <div class="mb-3">
                                <h6>1. Dynamic QR Endpoint:</h6>
                                <pre>GET /qr.php?upi={{ default_upi }}&amount=10</pre>
                            </div>
                            <div>
                                <h6>2. Verify Order Endpoint:</h6>
                                <pre>GET /verify.php?order_id=YOUR_ORDER_ID&api_key={{ api_key }}</pre>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            <div class="tab-pane fade" id="testing">
                <div class="row justify-content-center">
                    <div class="col-md-6">
                        <div class="card p-4 text-center">
                            <h4>Scan & Pay Test</h4>
                            <div id="test-step-1">
                                <input type="number" id="test_amount" class="form-control my-3 bg-dark text-light border-secondary" value="1" placeholder="Amount">
                                <button onclick="startTestQR()" class="btn btn-primary w-100">Generate QR</button>
                            </div>
                            <div id="test-step-2" style="display:none;" class="mt-3">
                                <div class="qr-container my-2"><img id="out-qr-img" src="" style="width:200px;"></div>
                                <h5 class="text-warning">Pay ₹<span id="out-amount"></span></h5>
                                <p class="small text-muted">Waiting for Auto-Verification...</p>
                                <button onclick="resetTest()" class="btn btn-sm btn-outline-secondary">Reset</button>
                            </div>
                            <div id="test-step-3" style="display:none;" class="mt-3 text-success">
                                <i class="fa-solid fa-circle-check fa-3x"></i>
                                <h4 class="mt-2">Payment Received!</h4>
                                <button onclick="resetTest()" class="btn btn-primary mt-2">Test Again</button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/bootstrap.bundle.min.js"></script>
    <script>
        let pollInterval = null;
        function startTestQR() {
            const amount = document.getElementById('test_amount').value;
            fetch(`/qr.php?upi={{ default_upi }}&amount=${amount}`)
                .then(res => res.json())
                .then(data => {
                    if(data.status === 'success') {
                        document.getElementById('out-amount').innerText = data.data.amount;
                        document.getElementById('out-qr-img').src = data.data.qr_url;
                        document.getElementById('test-step-1').style.display = 'none';
                        document.getElementById('test-step-2').style.display = 'block';
                        startPolling(data.data.order_id);
                    }
                });
        }
        function startPolling(orderId) {
            pollInterval = setInterval(() => {
                fetch(`/verify.php?order_id=${orderId}&api_key={{ api_key }}`)
                    .then(res => res.json())
                    .then(data => {
                        if(data.status === 'success') {
                            clearInterval(pollInterval);
                            document.getElementById('test-step-2').style.display = 'none';
                            document.getElementById('test-step-3').style.display = 'block';
                        }
                    });
            }, 3000);
        }
        function resetTest() {
            clearInterval(pollInterval);
            document.getElementById('test-step-1').style.display = 'block';
            document.getElementById('test-step-2').style.display = 'none';
            document.getElementById('test-step-3').style.display = 'none';
        }
    </script>
</body>
</html>
"""

# ============================================
# API ROUTES (TELEGRAM BOT READY)
# ============================================
@app.route("/")
def home_ui():
    return render_template_string(
        HTML_UI,
        default_upi=DEFAULT_UPI_ID,
        api_key=API_KEY,
        gmail_user=GMAIL_USER,
        dev_tag=DEV_TAG
    )

@app.route("/qr.php")
def qr_endpoint():
    upi = request.args.get("upi", DEFAULT_UPI_ID).strip()
    amount_raw = request.args.get("amount", "0").strip()

    try:
        amount = float(amount_raw) if amount_raw else 0
    except ValueError:
        return jsonify({"status": "error", "message": "Invalid amount", "dev": DEV_TAG}), 400

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
            "amount": str(amount),
            "created_at": created_at.strftime("%d-%m-%Y %H:%M:%S"),
            "expires_at": expires_at.strftime("%d-%m-%Y %H:%M:%S")
        },
        "dev": DEV_TAG
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
            "message": "Payment Verified",
            "data": {
                "order_id": order_id,
                "transaction_id": transaction_id,
                "amount": requested_amount,
                "utr": utr,
                "sender_name": sender_name,
                "status": "completed",
                "payment_time": datetime.fromisoformat(paid_at).strftime("%d-%m-%Y %H:%M:%S") if paid_at else None
            },
            "dev": DEV_TAG
        })

    return jsonify({
        "status": "pending",
        "message": "Payment not received yet",
        "order_id": order_id,
        "dev": DEV_TAG
    })

@app.route("/qr_codes/<path:filename>")
def qr_codes(filename):
    return send_from_directory(QR_DIR, filename)

# ============================================
# START SERVER
# ============================================
init_db()
start_monitor()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
