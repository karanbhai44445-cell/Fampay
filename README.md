# FamPay Payment API

Apna khud ka payment API — QR generate karo, payment verify karo. Bilkul us
documentation jaisa jo aapko diya gaya tha (`/qr.php` aur `/verify.php`).

## Render pe deploy kaise karo

1. In files (`app.py`, `requirements.txt`) ko ek GitHub repo mein daalo.
2. Render dashboard → **New +** → **Web Service** → apna repo connect karo.
3. Settings:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT`
4. **Environment Variables** (Render dashboard → Environment):
   - `GMAIL_USER` = apna Gmail address
   - `GMAIL_APP_PASSWORD` = Gmail App Password (16-digit)
   - `API_KEY` = koi bhi strong random string (ye hi `/verify.php` ke liye use hogi)
5. Deploy karo. Kuch minute mein aapka `https://your-app.onrender.com` live ho jaayega.

## ⚠️ Zaroori: Persistent storage

Render ka **Free plan** har redeploy/restart pe local files (database + QR
images) delete kar deta hai. Isi wajah se pehle aapke Telegram bot mein users
gayab ho rahe the — yahi cheez is API mein bhi hogi (purane orders bhool
jaayega).

Do options:
- **Paid plan + Persistent Disk** — sabse reliable. Disk attach karo, phir
  environment variable `DB_FILE_PATH` ko disk ke path pe set karo
  (jaise `/var/data/fampay_api.db`).
- **Free plan** — chalega, bas dhyan rahe ki jab bhi redeploy karo, purane
  pending/completed orders history delete ho jaayegi. Naye orders turant
  kaam karenge, purana data hi nahi bachega.

## Endpoints

### 1. QR generate karo
```
GET /qr.php?upi=YOUR_UPI_ID&amount=AMOUNT
```
`amount` optional hai — 0 ya missing rakhoge to user koi bhi amount pay kar sakta hai.

### 2. Payment verify karo
```
GET /verify.php?order_id=ORDER_ID&api_key=YOUR_API_KEY
```

Dono responses bilkul us documentation jaisi hain jo aapke paas thi.

## Speed (instant detection)

`requirements.txt` mein `imapclient` already hai — isse IMAP **IDLE** mode
use hota hai, matlab Gmail khud bot ko turant "push" kar deta hai jaise hi
naya mail aaye (delay ~1-2 second). Agar kisi wajah se `imapclient` install
na ho paaye, API khud-ba-khud fast-polling mode (har 3 second) mein chali
jaayegi — kaam dono tarah karega, bas IDLE thoda fast hai.

## Local test (apne PC/Termux pe)

```bash
pip install -r requirements.txt
export GMAIL_USER="your@gmail.com"
export GMAIL_APP_PASSWORD="your 16 digit app password"
export API_KEY="your-secret-key"
python app.py
```
Fir browser mein: `http://localhost:5000/qr.php?upi=yourupi@fam&amount=10`
