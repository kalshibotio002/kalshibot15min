# BTC 15-minute Signal Desk — any-device edition

This is a browser-based Streamlit dashboard. Once deployed, open its private URL
from an iPhone, iPad, Mac, Windows PC, or Chromebook. You can also add the URL
to your phone's Home Screen for app-like access.

## What's included

- Live BTC-USD spot feed from Coinbase Exchange (a proxy, not Kalshi's settlement index)
- Public Kalshi KXBTC15M market metadata when available
- EMA 9/21/50, RSI 14, 5m and 15m momentum, and realized-volatility filters
- WAIT unless the mandatory conditions pass and the rule score meets the threshold
- One automatic paper entry per market expiry
- Paper-trade ledger, observed win rate, and historical directional backtest
- Optional email alerts using SMTP environment variables
- Password gate using `APP_PASSWORD`
- Persistent SQLite database on the Render disk mounted at `/var/data`

## Deploy online with Render

This deployment uses a persistent disk so the paper ledger survives app restarts.
Render may charge for the always-on web service and persistent disk; check its
current pricing before creating the service.

1. Create a GitHub account if you do not already have one.
2. Create a **private** GitHub repository, for example `btc-15m-signal-desk`.
3. Upload all files in this folder to that repository (including `render.yaml`
   and `Dockerfile`; do not upload `bot_data.sqlite3` or passwords).
4. Sign in to Render and choose **New → Blueprint**.
5. Connect the GitHub repository and deploy the `render.yaml` blueprint.
6. When Render asks for `APP_PASSWORD`, enter a strong, unique password.
7. Wait for deployment to finish, then open the Render URL shown on the service.
8. Sign in with the password you set. Save the URL as a bookmark.

### Optional email alerts

Add these environment variables to the Render service, then redeploy:
- `SMTP_HOST` — your email provider's SMTP server
- `SMTP_PORT` — usually `587`
- `SMTP_USER`
- `SMTP_PASSWORD` — use an app password where your provider supports it
- `ALERT_EMAIL_FROM`
- `ALERT_EMAIL_TO`

Email is sent only when a new qualifying paper entry is created. Do not put
credentials in this repository or share them in chat.

## Add to iPhone Home Screen

1. Open the deployed dashboard URL in Safari.
2. Tap **Share**.
3. Choose **Add to Home Screen**.
4. Tap **Add**. It will appear as an app-like shortcut.

On Android, open the URL in Chrome and use **Add to Home screen** or
**Install app** if offered.

## Run locally on a Mac

Install Python 3.10 or newer, then in Terminal run:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```
Open `http://localhost:8501`. Set `APP_PASSWORD` in your shell if you want
the local password gate too.

## Important limitations

- This app does not place real Kalshi orders.
- The signal score is a weighted rule score, not a probability or guaranteed win rate.
- Paper settlement and backtesting compare Coinbase spot prices directionally;
  they do not reproduce the official Kalshi contract settlement index, averaging
  window, fees, spread, liquidity, slippage, or actual fills.
- A one-entry-per-expiry lock prevents multiple paper entries for the same
  expiry in the local database.
- Email alerts require your own SMTP provider credentials. Browser push
  notifications are not included in this version.
- Keep the repository private and use a strong dashboard password. A password
  gate is an additional app-level safeguard, not a substitute for careful
  account security.

This is an experimental research tool, not financial advice.
