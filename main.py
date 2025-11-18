import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
except ImportError:
    service_account = None
    build = None

logging.basicConfig(
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
SHEETS_IDS = [s.strip() for s in os.getenv('SHEETS_IDS', '').split(',') if s.strip()]
SERVICE_ACCOUNT_JSON = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
DEFAULT_USER = os.getenv('DEFAULT_USER_NAME', '').strip()

logger.info(
    "Env check: SERVICE_ACCOUNT_JSON_BASE64 present=%s, GOOGLE_SERVICE_ACCOUNT_JSON=%s",
    bool(os.getenv('SERVICE_ACCOUNT_JSON_BASE64') or os.getenv('SERVICE_ACCOUNT_JSON')),
    SERVICE_ACCOUNT_JSON or 'not set'
)

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
USERS_FILE = os.path.join(DATA_DIR, 'users.json')

# FIXED: Updated configurations with correct date formats and column indices
SHEET_CONFIGS = {
    '1MlIztcbS1hR-gMnT9aOtH5LMALcIOLCUL08o1SMsePg': {
        'name_idx': 0, 
        'date_idx': 3, 
        'sale_idx': 5, 
        'formats': ['%b %d, %Y, %I:%M:%S %p', '%B %d, %Y, %I:%M:%S %p', '%Y-%m-%d']  # Format: "Nov 15, 2025, 5:08:33 PM"
    },
    '1Q0VkLwxwKTc_-t17Ij-t_rI-wtwdLE37FyUjkznCYrI': {
        'name_idx': 0, 
        'date_idx': 2,  # FIXED: Changed from 3 to 2
        'sale_idx': 4,  # Sales amount is in column 4
        'formats': ['%B %dth, %Y at %I:%M %p', '%B %dst, %Y at %I:%M %p', '%B %dnd, %Y at %I:%M %p', '%B %drd, %Y at %I:%M %p']  # Format: "August 6th, 2025 at 5:52 PM GMT+8"
    },
    '1Eqtc8utEzUAdknJI_-u1AGg1SxBH3T78JpVwkpIQZ2Q': {
        'name_idx': 0, 
        'date_idx': 3, 
        'sale_idx': 5, 
        'formats': ['%Y-%m-%d', '%b %d, %Y', '%B %d, %Y']
    },
}
DEFAULT_CONFIG = {'name_idx': 0, 'date_idx': 3, 'sale_idx': 5, 'formats': ['%b %d, %Y, %I:%M:%S %p', '%B %d, %Y', '%Y-%m-%d']}


def calculate_window_days():
    """Calculate days in current sales window (8-17, 18-27, or 28-7)"""
    today = datetime.utcnow()
    day = today.day
    
    if 8 <= day <= 17:
        start_day = 8
    elif 18 <= day <= 27:
        start_day = 18
    elif day >= 28:
        start_day = 28
    else:  # days 1-7: window started on 28th of previous month
        prev_month = today.replace(day=1) - timedelta(days=1)
        start = prev_month.replace(day=28, hour=0, minute=0, second=0, microsecond=0)
        return (today - start).days + 1
    
    start = today.replace(day=start_day, hour=0, minute=0, second=0, microsecond=0)
    return (today - start).days + 1


def _month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def get_window_range(window: str) -> tuple[datetime, datetime]:
    """Return start/end datetimes for named windows."""
    today = datetime.utcnow()
    this_month_start = _month_start(today)

    if window == 'first':
        prev_month_last = this_month_start - timedelta(days=1)
        start = prev_month_last.replace(day=28, hour=0, minute=0, second=0, microsecond=0)
        end = this_month_start.replace(day=7, hour=23, minute=59, second=59, microsecond=999999)
    elif window == 'second':
        start = this_month_start.replace(day=8, hour=0, minute=0, second=0, microsecond=0)
        end = this_month_start.replace(day=17, hour=23, minute=59, second=59, microsecond=999999)
    elif window == 'third':
        start = this_month_start.replace(day=18, hour=0, minute=0, second=0, microsecond=0)
        end = this_month_start.replace(day=27, hour=23, minute=59, second=59, microsecond=999999)
    else:
        raise ValueError(f"Unknown window name: {window}")

    return start, end


def ensure_data_dir():
    """Create data directory and users file if they don't exist"""
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'w', encoding='utf-8') as fh:
            json.dump({}, fh)


def load_user_map() -> Dict[str, str]:
    """Load user ID to name mapping"""
    ensure_data_dir()
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except (json.JSONDecodeError, FileNotFoundError):
        return {}


def save_user_map(data: Dict[str, str]):
    """Save user ID to name mapping"""
    ensure_data_dir()
    with open(USERS_FILE, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=2)


def resolve_user_name(telegram_user) -> str:
    """Get user's sheet name from mapping or default"""
    user_map = load_user_map()
    mapped = user_map.get(str(telegram_user.id))
    if mapped:
        return mapped
    return DEFAULT_USER or telegram_user.full_name


def parse_sheet_date(raw: str):
    """Try multiple date formats to parse sheet date"""
    cleaned = (raw or '').strip()
    
    # Handle "August 6th, 2025 at 5:52 PM GMT+8" format
    # Remove timezone info and ordinal suffixes
    if ' GMT' in cleaned:
        cleaned = cleaned.split(' GMT')[0].strip()
    
    # Replace ordinal suffixes (1st, 2nd, 3rd, 4th, etc.)
    import re
    cleaned = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', cleaned)
    
    candidates = [
        '%B %d, %Y at %I:%M %p',   # August 6, 2025 at 5:52 PM (after removing "th" and "GMT+8")
        '%b %d, %Y at %I:%M %p',   # Aug 6, 2025 at 5:52 PM
        '%b %d, %Y, %I:%M:%S %p',  # Nov 15, 2025, 5:08:33 PM
        '%B %d, %Y, %I:%M:%S %p',  # November 15, 2025, 5:08:33 PM
        '%Y-%m-%d',                # 2025-11-15
        '%Y/%m/%d',                # 2025/11/15
        '%m/%d/%Y',                # 11/15/2025
        '%d/%m/%Y',                # 15/11/2025
        '%Y-%m-%d %H:%M',
        '%Y/%m/%d %H:%M',
    ]
    
    for fmt in candidates:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    
    # Try to extract just the date part if longer string
    if len(cleaned) >= 10:
        # Try YYYY-MM-DD format
        if cleaned[:10].count('-') == 2:
            try:
                return datetime.strptime(cleaned[:10], '%Y-%m-%d')
            except ValueError:
                pass
        # Try MM/DD/YYYY or DD/MM/YYYY format
        if cleaned[:10].count('/') == 2:
            try:
                return datetime.strptime(cleaned[:10], '%m/%d/%Y')
            except ValueError:
                try:
                    return datetime.strptime(cleaned[:10], '%d/%m/%Y')
                except ValueError:
                    pass
    
    return None


def fetch_sales_from_sheets(
    user_name: str,
    days: int = 1,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> Dict[str, float]:
    """Fetch sales data from Google Sheets for specified user and time period"""
    if not SERVICE_ACCOUNT_JSON or not os.path.exists(SERVICE_ACCOUNT_JSON):
        logger.warning('Service account JSON missing; returning dummy data')
        return {'total': 0.0, 'per_sheet': {}, 'error': 'Service account not configured'}
    
    if not service_account or not build:
        logger.error('google-api-python-client not installed; returning dummy data')
        return {'total': 0.0, 'per_sheet': {}, 'error': 'Google API client not installed'}

    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_JSON,
            scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'],
        )
        service = build('sheets', 'v4', credentials=creds)
    except Exception as e:
        logger.error(f'Failed to create Google Sheets service: {e}')
        return {'total': 0.0, 'per_sheet': {}, 'error': f'Authentication error: {str(e)}'}

    if start_date:
        since = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        since = datetime.utcnow() - timedelta(days=days - 1)
        since = since.replace(hour=0, minute=0, second=0, microsecond=0)

    if end_date:
        until = end_date.replace(hour=23, minute=59, second=59, microsecond=999999)
    else:
        until = datetime.utcnow()
        until = until.replace(hour=23, minute=59, second=59, microsecond=999999)
    totals = 0.0
    breakdown: Dict[str, float] = {}
    
    # Debug tracking
    total_rows = 0
    name_matches = 0
    matching_sales = 0
    sample_dates = []

    logger.info(f"Searching for user: '{user_name}' since {since.strftime('%Y-%m-%d')} ({days} days)")

    for sheet_id in SHEETS_IDS:
        conf = SHEET_CONFIGS.get(sheet_id, DEFAULT_CONFIG)
        
        try:
            result = service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range='A:Z',
            ).execute()
            rows: List[List[str]] = result.get('values', [])
            
            if not rows:
                logger.warning(f'No data found in sheet {sheet_id}')
                continue
            
            logger.info(f'Sheet {sheet_id[-8:]}: Processing {len(rows)-1} data rows')
            sheet_total = 0.0
            sheet_sales_count = 0
            
            # Skip header row
            for idx, row in enumerate(rows[1:], start=2):
                total_rows += 1
                
                if len(row) <= max(conf['name_idx'], conf['date_idx'], conf['sale_idx']):
                    continue
                
                try:
                    name = row[conf['name_idx']].strip()
                    date_str = row[conf['date_idx']].strip()
                    sale_str = row[conf['sale_idx']].strip()
                except (IndexError, AttributeError):
                    continue

                # Check name match first
                if name.lower() != user_name.lower():
                    continue
                    
                name_matches += 1
                logger.info(f'Sheet {sheet_id[-8:]}, Row {idx}: Found name match "{name}", date string: "{date_str}"')
                
                # Parse date with config formats first, then fallback
                sale_date = None
                date_format_used = None
                for fmt in conf.get('formats', []):
                    try:
                        sale_date = datetime.strptime(date_str, fmt)
                        date_format_used = fmt
                        logger.info(f'Sheet {sheet_id[-8:]}, Row {idx}: Successfully parsed date "{date_str}" with format "{fmt}"')
                        break
                    except ValueError as e:
                        logger.debug(f'Sheet {sheet_id[-8:]}, Row {idx}: Format "{fmt}" failed for "{date_str}"')
                        continue
                
                if not sale_date:
                    sale_date = parse_sheet_date(date_str)
                    if sale_date:
                        date_format_used = "fallback parser"
                        logger.info(f'Sheet {sheet_id[-8:]}, Row {idx}: Parsed date "{date_str}" with fallback parser')
                
                if not sale_date:
                    logger.warning(f'Sheet {sheet_id[-8:]}, Row {idx}: ❌ FAILED to parse date "{date_str}" with any format. Tried: {conf.get("formats", [])}')
                    continue
                
                # Store sample dates
                if len(sample_dates) < 5:
                    sample_dates.append(sale_date.strftime('%Y-%m-%d'))
                
                # Check if this sale matches our criteria
                if since <= sale_date <= until:
                    try:
                        # Clean up sale amount
                        clean_sale = sale_str.replace('$', '').replace(',', '').strip()
                        amount = float(clean_sale) if clean_sale else 0.0
                        sheet_total += amount
                        matching_sales += 1
                        sheet_sales_count += 1
                        logger.info(f'Sheet {sheet_id[-8:]}, Row {idx}: ✓ {name} | {sale_date.strftime("%Y-%m-%d")} | ${amount:.2f}')
                    except ValueError as e:
                        logger.warning(f'Sheet {sheet_id[-8:]}, Row {idx}: Could not parse sale amount "{sale_str}": {e}')
                        continue
                else:
                    logger.debug(f'Sheet {sheet_id[-8:]}, Row {idx}: Date {sale_date.strftime("%Y-%m-%d")} is before {since.strftime("%Y-%m-%d")}')
            
            if sheet_total > 0:
                breakdown[sheet_id] = sheet_total
                totals += sheet_total
                logger.info(f'Sheet {sheet_id[-8:]}: ✓ Found {sheet_sales_count} sales totaling ${sheet_total:.2f}')
            else:
                logger.info(f'Sheet {sheet_id[-8:]}: No sales found in date range')
        
        except Exception as e:
            logger.error(f'Error fetching data from sheet {sheet_id[-8:]}: {e}')
            continue

    debug_info = {
        'total_rows': total_rows,
        'name_matches': name_matches,
        'matching_sales': matching_sales,
        'date_range': f"{since.strftime('%Y-%m-%d')} to {until.strftime('%Y-%m-%d')}",
        'sample_dates': ', '.join(sample_dates[:5]) if sample_dates else 'None found'
    }

    logger.info(f"FINAL TOTALS: ${totals:.2f} from {matching_sales} sales across {len(breakdown)} sheets")
    return {'total': totals, 'per_sheet': breakdown, 'debug_info': debug_info}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    welcome_msg = (
        "🎉 Welcome to SaleCounter Bot!\n\n"
        "Commands:\n"
        "/setname <Your Sheet Name> - Set your name as it appears in sheets\n"
        "/mysales - Check your sales for current window\n"
        "/week - Same as /mysales\n"
        "/debug - Show configuration and sample data\n\n"
        f"Current user: {resolve_user_name(update.effective_user)}"
    )
    await update.message.reply_text(welcome_msg)


def build_sales_message(user_name: str, data: Dict[str, float], descriptor: str) -> str:
    """Compose sales summary text."""
    if 'error' in data:
        return (
            f"❌ Error: {data['error']}\n\n"
            "Please contact the bot administrator."
        )

    total = data['total']
    debug_info = data.get('debug_info', {})

    if total > 0:
        msg = f"💰 {user_name}'s sales for {descriptor}:\n\n"
        msg += f"**Total: ${total:.2f}**\n\n"
        if data['per_sheet']:
            msg += "Breakdown by sheet:\n"
            for sheet_id, amount in data['per_sheet'].items():
                msg += f"• Sheet ...{sheet_id[-8:]}: ${amount:.2f}\n"
        if debug_info:
            msg += f"\n📊 Debug: {debug_info.get('matching_sales', 0)} sales found"
    else:
        msg = f"📊 {user_name}: No sales found for {descriptor}\n\n"
        if debug_info:
            msg += "Debug info:\n"
            msg += f"• Total rows checked: {debug_info.get('total_rows', 0)}\n"
            msg += f"• Rows with your name: {debug_info.get('name_matches', 0)}\n"
            msg += f"• Date range: {debug_info.get('date_range', 'Unknown')}\n"
            msg += f"• Sample dates found: {debug_info.get('sample_dates', 'None')}"
    return msg


async def mysales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /mysales command"""
    user_name = resolve_user_name(update.effective_user)
    days = calculate_window_days()
    
    await update.message.reply_text(f"🔍 Checking sales for {user_name}...")
    
    data = fetch_sales_from_sheets(user_name=user_name, days=days)
    descriptor = f"the current window ({days} days)"
    await update.message.reply_text(build_sales_message(user_name, data, descriptor))


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /week command (alias for /mysales)"""
    await mysales(update, context)


async def _send_window_sales(update: Update, label: str, start: datetime, end: datetime):
    """Common logic for explicit window commands."""
    user_name = resolve_user_name(update.effective_user)
    descriptor = f"{label} window ({start.strftime('%Y-%m-%d')} - {end.strftime('%Y-%m-%d')})"

    await update.message.reply_text(f"🔍 Checking {descriptor} for {user_name}...")
    data = fetch_sales_from_sheets(user_name=user_name, start_date=start, end_date=end)
    await update.message.reply_text(build_sales_message(user_name, data, descriptor))


async def first_window(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start, end = get_window_range('first')
    await _send_window_sales(update, "the 28th-7th", start, end)


async def second_window(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start, end = get_window_range('second')
    await _send_window_sales(update, "the 8th-17th", start, end)


async def third_window(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start, end = get_window_range('third')
    await _send_window_sales(update, "the 18th-27th", start, end)


async def setname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setname command"""
    if not context.args:
        current_name = resolve_user_name(update.effective_user)
        await update.message.reply_text(
            f"Current name: {current_name}\n\n"
            "Usage: /setname <name as it appears in the sheet>\n"
            "Example: /setname John Smith"
        )
        return
    
    name = ' '.join(context.args).strip()
    user_map = load_user_map()
    user_map[str(update.effective_user.id)] = name
    save_user_map(user_map)
    
    await update.message.reply_text(f'✅ Saved! Your sheet name is now "{name}".')


async def showrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all columns from a specific sheet and row"""
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /showrow <sheet_num> <row_num>\nExample: /showrow 2 47")
        return
    
    try:
        sheet_num = int(context.args[0]) - 1
        row_num = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Invalid numbers. Use: /showrow 2 47")
        return
    
    if sheet_num < 0 or sheet_num >= len(SHEETS_IDS):
        await update.message.reply_text(f"Sheet number must be 1-{len(SHEETS_IDS)}")
        return
    
    sheet_id = SHEETS_IDS[sheet_num]
    
    if not SERVICE_ACCOUNT_JSON or not os.path.exists(SERVICE_ACCOUNT_JSON):
        await update.message.reply_text("Service account not configured")
        return
    
    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_JSON,
            scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'],
        )
        sheets_service = build('sheets', 'v4', credentials=creds)
        
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range='A:Z',
        ).execute()
        rows = result.get('values', [])
        
        if row_num < 1 or row_num > len(rows):
            await update.message.reply_text(f"Row {row_num} not found. Sheet has {len(rows)} rows.")
            return
        
        row = rows[row_num - 1]
        msg = f"📋 Sheet {sheet_num + 1}, Row {row_num}:\n\n"
        
        for i, cell in enumerate(row):
            msg += f"Column {i}: {cell[:100]}\n"
        
        await update.message.reply_text(msg)
        
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")


async def testdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test date parsing with a sample date"""
    if not context.args:
        await update.message.reply_text("Usage: /testdate Nov 15, 2025")
        return
    
    date_str = ' '.join(context.args)
    
    msg = f"Testing date string: '{date_str}'\n\n"
    
    # Try all formats
    formats_to_try = ['%b %d, %Y', '%B %d, %Y', '%Y-%m-%d', '%m/%d/%Y', '%d/%m/%Y']
    
    for fmt in formats_to_try:
        try:
            parsed = datetime.strptime(date_str, fmt)
            msg += f"✓ Format '{fmt}': {parsed.strftime('%Y-%m-%d')}\n"
        except ValueError:
            msg += f"✗ Format '{fmt}': Failed\n"
    
    # Try fallback parser
    parsed = parse_sheet_date(date_str)
    if parsed:
        msg += f"\n✓ Fallback parser: {parsed.strftime('%Y-%m-%d')}"
    else:
        msg += f"\n✗ Fallback parser: Failed"
    
    await update.message.reply_text(msg)


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /debug command - shows configuration and sample data"""
    user_name = resolve_user_name(update.effective_user)
    days = calculate_window_days()
    today = datetime.utcnow()
    since = today - timedelta(days=days - 1)
    
    msg = "🔧 Debug Information:\n\n"
    msg += f"User: {user_name}\n"
    msg += f"Telegram ID: {update.effective_user.id}\n"
    msg += f"Current date: {today.strftime('%Y-%m-%d')}\n"
    msg += f"Window: {days} days (since {since.strftime('%Y-%m-%d')})\n"
    msg += f"Sheets configured: {len(SHEETS_IDS)}\n\n"
    
    # Show sheet configs
    msg += "Sheet Configurations:\n"
    for i, sheet_id in enumerate(SHEETS_IDS, 1):
        conf = SHEET_CONFIGS.get(sheet_id, DEFAULT_CONFIG)
        msg += f"{i}. ...{sheet_id[-8:]}: "
        msg += f"name=col{conf['name_idx']}, date=col{conf['date_idx']}, sale=col{conf['sale_idx']}\n"
    
    await update.message.reply_text(msg)
    
    # Fetch sample data from sheet 2 specifically (the problematic one)
    if SERVICE_ACCOUNT_JSON and os.path.exists(SERVICE_ACCOUNT_JSON):
        try:
            creds = service_account.Credentials.from_service_account_file(
                SERVICE_ACCOUNT_JSON,
                scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'],
            )
            sheets_service = build('sheets', 'v4', credentials=creds)
            
            # Focus on sheet 2 (index 1) - the problematic one
            sheet_id = SHEETS_IDS[1]
            conf = SHEET_CONFIGS.get(sheet_id, DEFAULT_CONFIG)
            
            try:
                result = sheets_service.spreadsheets().values().get(
                    spreadsheetId=sheet_id,
                    range='A:Z',
                ).execute()
                rows = result.get('values', [])
                
                sample_msg = f"\n📋 SHEET 2 (...{sheet_id[-8:]}) - Row 47:\n\n"
                
                if len(rows) > 47:
                    row = rows[46]  # Row 47 is index 46
                    for i, cell in enumerate(row[:15]):  # Show first 15 columns
                        sample_msg += f"col{i}: {cell}\n"
                else:
                    sample_msg += "Row 47 not found"
                
                await update.message.reply_text(sample_msg)
                
            except Exception as e:
                await update.message.reply_text(f"\n❌ Error: {str(e)}")
            
        except Exception as e:
            await update.message.reply_text(f"\n❌ Error: {str(e)}")
    else:
        await update.message.reply_text("\n⚠️ Service account not configured")


def main():
    """Main function to run the bot"""
    if not BOT_TOKEN:
        logger.error('TELEGRAM_BOT_TOKEN missing from environment')
        return
    
    ensure_data_dir()
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('mysales', mysales))
    app.add_handler(CommandHandler('week', week))
    app.add_handler(CommandHandler('first', first_window))
    app.add_handler(CommandHandler('second', second_window))
    app.add_handler(CommandHandler('third', third_window))
    app.add_handler(CommandHandler('setname', setname))
    app.add_handler(CommandHandler('debug', debug))
    app.add_handler(CommandHandler('testdate', testdate))
    app.add_handler(CommandHandler('showrow', showrow))
    
    logger.info('Starting Telegram bot...')
    logger.info(f'Configured sheets: {len(SHEETS_IDS)}')
    logger.info(f'Default user: {DEFAULT_USER or "Not set"}')
    
    app.run_polling()


if __name__ == '__main__':
    main()
