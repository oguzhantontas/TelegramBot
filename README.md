## Telegram Sales Bot Scaffold

This folder contains a Python implementation of the sales bot. It is ready for VS Code: open `TelegramSalesBot/`, create a virtual environment, and run `main.py`.

### Setup
1. Copy `.env.example` to `.env` and fill in:
   - `TELEGRAM_BOT_TOKEN` from BotFather.
   - `GOOGLE_SERVICE_ACCOUNT_JSON` pointing to your service-account key file.
   - `SHEETS_IDS` comma-separated list of the three Google Sheet IDs.
2. Create `credentials/` and place the service-account JSON there, then share each sheet with the service-account email.
3. Install deps:
   ```bash
   python -m venv .venv
   .\.venv\Scripts\activate
   pip install -r requirements.txt
   ```
4. Run:
   ```bash
   python main.py
   ```

Use `/start`, `/mysales`, and `/week` inside Telegram to test. The `fetch_sales_from_sheets` function currently uses a placeholder range (`A:F`) and expects columns A (name), C (date `YYYY-MM-DD`), and F (sale). Adjust the range/date parsing as needed once you finalize the sheet layouts.
