import sqlite3
import requests
from bs4 import BeautifulSoup
from datetime import date, timedelta, datetime
import time
import yfinance as yf
import os

DATABASE = os.environ.get('DATABASE_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tracker.db'))

def get_db():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn

def get_screener_result_date(symbol):
    """Scrapes Screener and returns a standard date object, or None if not found."""
    url = f"https://www.screener.in/company/{symbol}/consolidated/"
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.content, 'html.parser')
        result_element = soup.find(lambda tag: tag.name in ['p', 'div', 'span'] and 'Upcoming result date' in tag.text)
        if result_element:
            result_text = result_element.text.strip().lower()
            if "tomorrow" in result_text:
                return date.today() + timedelta(days=1)
            elif "today" in result_text:
                return date.today()
            else:
                date_str = result_element.text.strip().split(':')[-1].strip()
                # Use %B for full month name like "July"
                return datetime.strptime(date_str, '%d %B %Y').date()
        return None
    except Exception:
        return None

def update_all_result_dates():
    """
    Fetches dates ONLY from Screener and updates the database, counting only actual changes.
    """
    print("--- Starting Result Date Update Process (Screener Only) ---")
    conn = get_db()
    try:
        # Get all unique symbols that need a date check
        unique_symbols = [row['symbol'] for row in conn.execute(
            "SELECT DISTINCT symbol FROM profit_tracker WHERE result_date = 'Not Announced' OR result_date = 'N/A' OR result_date = 'CONFLICT'"
        ).fetchall()]
        if not unique_symbols:
            print("No stocks need a date update today.")
            return
        print(f"Found {len(unique_symbols)} stocks that need a date check.")
        updated_symbols = set()
        for symbol in unique_symbols:
            print(f"  - Checking {symbol}...")
            date_obj = get_screener_result_date(symbol)
            final_date_str = "Not Announced"
            if isinstance(date_obj, date):
                final_date_str = date_obj.strftime('%Y-%m-%d')
            rows = conn.execute(
                "SELECT id, result_date FROM profit_tracker WHERE symbol = ? AND (result_date = 'Not Announced' OR result_date = 'N/A' OR result_date = 'CONFLICT')",
                (symbol,)
            ).fetchall()
            any_updated = False
            for row in rows:
                old_status = row['result_date']
                if final_date_str != old_status:
                    try:
                        conn.execute('UPDATE profit_tracker SET result_date = ? WHERE id = ?', (final_date_str, row['id']))
                        any_updated = True
                    except Exception as e:
                        print(f"    > ERROR updating {symbol} (id {row['id']}): {e}")
            if any_updated:
                updated_symbols.add(symbol)
                print(f"    > Status for {symbol} UPDATED to: {final_date_str}")
            else:
                old_status = rows[0]['result_date'] if rows else final_date_str
                print(f"    > No change for {symbol}. Status remains: {old_status}")
            time.sleep(1)
        try:
            conn.commit()
            print(f"Committed all updates to the database.")
        except Exception as e:
            print(f"ERROR during commit: {e}")
    except Exception as e:
        print(f"ERROR in update_all_result_dates: {e}")
    finally:
        conn.close()
        print("Connection closed.")
    print(f"\nFinished scraping. A total of {len(updated_symbols)} stock(s) had their status changed.")
    print("--- Result Date Update Process Finished ---")


# --- The rest of the file (archiving logic) remains unchanged ---
def get_correct_eod_price(ticker, log_date):
    try:
        stock = yf.Ticker(f"{ticker.upper()}.NS")
        end_date = log_date + timedelta(days=1)
        hist = stock.history(end=end_date.strftime("%Y-%m-%d"), period="5d")
        if not hist.empty: return round(hist['Close'].iloc[-1], 2)
        return None
    except Exception: return None

def get_investment_category(ath_outperformance, ath_profit, idx_type):
    if ath_outperformance == 'Y':
        if ath_profit == 'Y': return "FUND", ""
        elif idx_type == 'FNO': return "PROP", ""
        else: return "NO ENTRY", "Index is not 'FNO'"
    else: return "NO ENTRY", "ATH OP is 'N'"

def perform_archive_for_user(user_id, log_date, conn):
    log_date_str = log_date.strftime("%Y-%m-%d")
    print(f"\n--- Starting archive for user_id: {user_id} for log_date: {log_date_str} ---")
    conn.execute('DELETE FROM historical_log WHERE user_id = ? AND log_date = ?', (user_id, log_date_str))
    print(f"  - Cleared any existing logs for {log_date_str}.")

    stocks_to_archive = conn.execute('SELECT s.*, pt.ath_profit, pt.idx_type FROM stocks s LEFT JOIN profit_tracker pt ON s.symbol = pt.symbol AND s.user_id = pt.user_id WHERE s.user_id = ?', (user_id,)).fetchall()
    if not stocks_to_archive:
        print("  - No stocks in daily tracker. Nothing to archive.")
        return
    for stock in stocks_to_archive:
        symbol = stock['symbol']
        eod_price = get_correct_eod_price(symbol, log_date)
        if eod_price:
            category, final_remark = ("NO ENTRY", "New, add to Profit Mgr.") if not stock['ath_profit'] else get_investment_category(stock['ath_outperformance'], stock['ath_profit'], stock['idx_type'])
            final_trigger = 'WAIT'
            is_tracked = category in ['FUND', 'PROP']
            if is_tracked:
                price_gt_rounding = 'Y' if eod_price > stock['rounding'] else 'N'
                if stock['is_green_candle'] == 'Y' and price_gt_rounding == 'Y' and stock['is_close_above_ath'] == 'Y':
                    final_trigger = "GO"; final_remark = ""
                elif price_gt_rounding == 'N': final_remark = "Rounding > CMP"
            if final_trigger == 'GO': conn.execute("DELETE FROM watchlist WHERE user_id = ? AND symbol = ?", (user_id, symbol))
            elif final_remark in ["Rounding > CMP", "ATH Profit is 'N'", "ATH OP is 'N'"]:
                conn.execute('INSERT INTO watchlist (user_id, symbol, reason) VALUES (?, ?, ?) ON CONFLICT(user_id, symbol) DO UPDATE SET reason=excluded.reason', (user_id, symbol, final_remark))
            log_data = (user_id, log_date_str, symbol, eod_price, stock['rounding'], stock['stop_loss'], category, final_trigger, final_remark)
            conn.execute('INSERT INTO historical_log (user_id, log_date, symbol, eod_price, rounding_price, stop_loss, category, final_trigger, remarks) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', log_data)
    conn.commit()

def get_correct_log_date():
    today = date.today()
    weekday = today.weekday()
    if weekday == 5: return today - timedelta(days=1)
    elif weekday == 6: return today - timedelta(days=2)
    return today

def archive_all_users():
    print("\n--- Starting Daily Data Archive Process ---")
    conn = get_db()
    log_date = get_correct_log_date()
    users = conn.execute('SELECT id FROM users').fetchall()
    for user in users:
        perform_archive_for_user(user['id'], log_date, conn)
    conn.close()
    print("--- Daily Data Archive Finished ---")

if __name__ == '__main__':
    print(f"Executing Daily Tasks at {date.today().strftime('%Y-%m-%d %H:%M:%S')}")
    update_all_result_dates()
    archive_all_users()
    print("All Daily Tasks Complete.")