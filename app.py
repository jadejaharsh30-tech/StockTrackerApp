import sqlite3
import yfinance as yf
import pandas as pd
import io
import os
from flask import Flask, render_template, request, redirect, url_for, flash, Response, jsonify, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from daily_tasks import perform_archive_for_user, get_correct_log_date
from datetime import date, timedelta
from daily_tasks import update_all_result_dates # Add this to your imports
import requests
from datetime import datetime
from werkzeug.exceptions import abort
from werkzeug.exceptions import abort
import time
from news_feed import get_market_news # Import news helper
from fundamental_analysis import get_stock_fundamentals # Import fundamental analysis helper
from analytics_engine import get_portfolio_allocation, calculate_portfolio_metrics, get_turtle_metrics, calculate_sector_scores, calculate_risk_metrics # Import analytics helper
from alerts_manager import get_user_alerts, create_alert, delete_alert, check_alerts # Import alerts helper
from scoring_engine import ScoringEngine # Import new scoring engine
from market_stats import MarketStats # Checklist 3 Backend
from data_manager import DataManager
import logging

# --- LOGGING CONFIGURATION ---
# Filter out noisy polling requests from terminal to fix tqdm flickering
class AccessLogFilter(logging.Filter):
    def filter(self, record):
        # Filter both the direct API call and any redirects (like login checks)
        msg = record.getMessage()
        return "api/ath/status" not in msg

logging.getLogger("werkzeug").addFilter(AccessLogFilter())

# --- APP CONFIGURATION ---
DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tracker.db')
AUDIT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'db_update_audit.log')
print('Webapp DB path:', DATABASE)

data_manager = DataManager(db_path=DATABASE) # Pass absolute path
app = Flask(__name__)
# ... custom filters ...

# --- JINJA TEMPLATE GLOBALS ---
def score_color(score):
    """Returns a CSS color based on score (0-100). Used in Turtle Dashboard & Sector Analytics templates."""
    try:
        score = float(score or 0)
    except (TypeError, ValueError):
        return '#6B7280'  # Gray fallback
    if score >= 70:
        return '#10B981'  # Green
    elif score >= 40:
        return '#F59E0B'  # Amber
    else:
        return '#EF4444'  # Red

def text_color(score):
    """Returns a CSS color based on score for text (darker colors for readability). Used in Analytics templates."""
    try:
        score = float(score or 0)
    except (TypeError, ValueError):
        return '#666666'
    if score >= 70:
        return '#2E7D32'  # Dark Green
    elif score >= 40:
        return '#EF6C00'  # Dark Amber
    else:
        return '#C62828'  # Dark Red

@app.context_processor
def inject_template_helpers():
    """Inject helper functions into all Jinja2 templates."""
    return dict(score_color=score_color, text_color=text_color)


app.secret_key = 'ATH_SCANNER_FIXED_SECRET_KEY_V1' # Fixed key to prevent logout on reload
# --- Load Master Symbol List on Startup ---
try:
    with open('nse_symbols.txt') as f:
        MASTER_SYMBOL_LIST = {line.strip() for line in f if line.strip()}
except FileNotFoundError:
    print("WARNING: nse_symbols.txt not found. Autocomplete will only use symbols from the database.")
    MASTER_SYMBOL_LIST = set()

# --- Initialize Extensions ---
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'

# Session configuration is already set with app.secret_key = os.urandom(24)


# --- User Model for Flask-Login ---
class User(UserMixin):
    def __init__(self, id, username):
        self.id = id
        self.username = username

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    user_data = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    if user_data:
        return User(id=user_data['id'], username=user_data['username'])
    return None


# --- DATABASE FUNCTIONS ---
def get_db():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')  # Allow concurrent reads+writes
    return conn

def create_tables():
    conn = get_db()
    cursor = conn.cursor()
    # --- Existing Tables ---
    cursor.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, password TEXT NOT NULL)')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, symbol TEXT NOT NULL,
            rounding REAL NOT NULL, stop_loss REAL, ath_outperformance TEXT NOT NULL,
            is_green_candle TEXT NOT NULL, is_close_above_ath TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS profit_tracker (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, symbol TEXT NOT NULL,
            ath_profit TEXT NOT NULL, idx_type TEXT NOT NULL,
            result_date TEXT DEFAULT 'Not Announced', -- NEW COLUMN
            UNIQUE(user_id, symbol)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS historical_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, log_date TEXT NOT NULL,
            symbol TEXT NOT NULL, eod_price REAL NOT NULL, rounding_price REAL NOT NULL,
            stop_loss REAL, category TEXT NOT NULL, final_trigger TEXT NOT NULL, remarks TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL, reason TEXT NOT NULL, UNIQUE(user_id, symbol)
        )
    ''')
    
    # --- NEW TABLES FOR PORTFOLIO MODULE ---
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS portfolios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            exit_price REAL NOT NULL,
            allocation REAL NOT NULL,
            rg_status TEXT DEFAULT 'R',
            FOREIGN KEY (portfolio_id) REFERENCES portfolios (id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS fundamental_checklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            analysis_status TEXT DEFAULT 'PENDING',
            fund_allocation REAL DEFAULT 0,
            breakout_status TEXT DEFAULT 'PENDING',
            UNIQUE(user_id, symbol)
        )
    ''')
    
    # --- NEW TABLE FOR PREVIEW DATA ---
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS upload_previews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            preview_type TEXT NOT NULL,
            preview_data TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # --- NEW TABLE FOR SCORING HISTORY ---
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scoring_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            calc_date TEXT NOT NULL,
            overall_score REAL,
            growth_score REAL,
            quality_score REAL,
            value_score REAL,
            tech_score REAL,
            weights_json TEXT,
            full_data_json TEXT
        )
    ''')

    # --- NEW TABLES FOR TURTLE DASHBOARD (CHECKLIST 3) ---
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS market_stats_history (
            date TEXT PRIMARY KEY,
            ath_price_pct REAL,
            ath_profit_pct REAL,
            above_200dma REAL,
            regime TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stock_analytics_snapshot (
            date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            rs_rank REAL,
            trend_state TEXT,
            overall_score REAL,
            tech_score REAL,
            ath_flag TEXT,
            sector TEXT,
            PRIMARY KEY (date, symbol)
        )
    ''')

    conn.commit()
    conn.close()

def log_scoring_run(symbol, data, weights=None):
    """Logs the scoring result to the database."""
    try:
        import json
        conn = get_db()
        today = date.today().isoformat()
        
        # Check if already logged today (Debounce)
        existing = conn.execute(
            "SELECT id FROM scoring_history WHERE symbol = ? AND calc_date = ?",
            (symbol, today)
        ).fetchone()
        
        scores = data.get('pillar_scores', {})
        
        if existing:
            # Update existing
            conn.execute('''
                UPDATE scoring_history 
                SET overall_score=?, growth_score=?, quality_score=?, value_score=?, tech_score=?, weights_json=?, full_data_json=?
                WHERE id=?
            ''', (
                data.get('health_score', 0),
                scores.get('Growth', 0),
                scores.get('Profitability', 0),
                scores.get('Valuation', 0),
                scores.get('Technicals', 0),
                json.dumps(weights) if weights else None,
                json.dumps(data),
                existing['id']
            ))
        else:
            # Insert new
            conn.execute('''
                INSERT INTO scoring_history (symbol, calc_date, overall_score, growth_score, quality_score, value_score, tech_score, weights_json, full_data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                symbol,
                today,
                data.get('health_score', 0),
                scores.get('Growth', 0),
                scores.get('Profitability', 0),
                scores.get('Valuation', 0),
                scores.get('Technicals', 0),
                json.dumps(weights) if weights else None,
                json.dumps(data)
            ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Failed to log score: {e}")

# --- CALCULATION & HELPER FUNCTIONS ---
def get_live_price(ticker):
    """Get live price for a single ticker. Uses yfinance Ticker API with NaN validation."""
    import math
    try:
        stock = yf.Ticker(f"{ticker.upper()}.NS")
        # Use fast_info for quick price fetch
        price = getattr(stock, 'fast_info', {}).get('last_price')
        if price is None:
            # Fallback to info if fast_info is unavailable
            price = stock.info.get('regularMarketPrice', stock.info.get('previousClose'))
        if price is not None:
            price = float(price)
            if math.isnan(price) or math.isinf(price):
                return None
            return round(price, 2)
        return None
    except Exception:
        return None

def get_live_prices_batch(symbols):
    """Refactored to use DataManager for batch efficiency."""
    return data_manager.get_prices(symbols)

def get_live_prices_cache(symbols, cache_expiry_minutes=5):
    """Refactored to use the centralized DataManager for speed."""
    data = data_manager.get_prices(symbols, max_age_seconds=cache_expiry_minutes*60)
    return data

# Logic is the same, but the source of the 'ath_outperformance' data will change
def get_investment_category(ath_outperformance, ath_profit, idx_type):
    if ath_outperformance == 'Y':
        if ath_profit == 'Y': return "FUND", ""
        elif idx_type == 'FNO': return "PROP", ""
        else: return "NO ENTRY", "Index is not 'FNO'"
    else: return "NO ENTRY", "ATH OP is 'N'"

def get_holding_details(symbol, user_id):
    """Fast version of holding details using DataManager."""
    conn = get_db()
    profit_data = conn.execute("SELECT result_date FROM profit_tracker WHERE symbol = ? AND user_id = ?", (symbol, user_id)).fetchone()
    conn.close()
    result_date_from_db = profit_data['result_date'] if profit_data else "N/A"
    
    # Use DataManager instead of yf.Ticker(symbol).info
    live_data = data_manager.get_prices([symbol]).get(symbol.upper(), {'price': 'N/A', 'change_pct': 0})
    
    return {
        'cmp': live_data['price'],
        'change_pct': live_data['change_pct'],
        'result_date': result_date_from_db
    }

def calculate_total_portfolio_value(user_id):
    """
    Calculate total portfolio value across all portfolios for a user.
    Optimized to use batch fetching.
    """
    conn = get_db()
    
    # 1. Get all holdings first
    holdings = conn.execute("""
        SELECT h.symbol, h.allocation FROM holdings h
        JOIN portfolios p ON h.portfolio_id = p.id
        WHERE p.user_id = ?
    """, (user_id,)).fetchall()
    conn.close()
    
    if not holdings:
        return {'total_value': 0, 'change_pct': 0, 'holdings_count': 0}
        
    # 2. Batch fetch prices
    symbols = [h['symbol'] for h in holdings]
    live_data_dict = data_manager.get_prices(symbols)
    
    total_value = 0
    total_change_pct = 0
    valid_count = 0
    
    # 3. Aggregate
    for holding in holdings:
        live_data = live_data_dict.get(holding['symbol'].upper(), {'price': 'N/A', 'change_pct': 0})
        cmp = live_data['price']
        
        if cmp != 'N/A' and cmp > 0:
            total_value += (holding['allocation'] / 100) * cmp
            total_change_pct += live_data['change_pct']
            valid_count += 1
            
    avg_change_pct = total_change_pct / valid_count if valid_count > 0 else 0
    
    return {
        'total_value': round(total_value, 2),
        'change_pct': round(avg_change_pct, 2),
        'holdings_count': len(holdings)
    }

def get_best_performer_from_batch(live_data_dict):
    best_stock = None
    best_change = -999
    for symbol, data in live_data_dict.items():
        if data['change_pct'] > best_change:
            best_change = data['change_pct']
            best_stock = {
                'symbol': symbol,
                'change_pct': data['change_pct'],
                'cmp': data['price']
            }
    return best_stock

def get_worst_performer_from_batch(live_data_dict):
    worst_stock = None
    worst_change = 999
    for symbol, data in live_data_dict.items():
        if data['change_pct'] < worst_change:
            worst_change = data['change_pct']
            worst_stock = {
                'symbol': symbol,
                'change_pct': data['change_pct'],
                'cmp': data['price']
            }
    return worst_stock

def get_upcoming_results_count(user_id):
    """Get count of stocks with upcoming results"""
    conn = get_db()
    upcoming_count = conn.execute("""
        SELECT COUNT(*) as count FROM profit_tracker 
        WHERE user_id = ? AND result_date != 'Not Announced' 
        AND result_date >= date('now')
    """, (user_id,)).fetchone()
    
    next_result = conn.execute("""
        SELECT symbol, result_date FROM profit_tracker 
        WHERE user_id = ? AND result_date != 'Not Announced' 
        AND result_date >= date('now')
        ORDER BY result_date ASC LIMIT 1
    """, (user_id,)).fetchone()
    
    conn.close()
    
    return {
        'count': upcoming_count['count'] if upcoming_count else 0,
        'next_stock': next_result['symbol'] if next_result else None,
        'next_date': next_result['result_date'] if next_result else None
    }

def get_portfolio_allocation_data(portfolio_id, user_id):
    """Get allocation data for pie chart visualization"""
    conn = get_db()
    
    # Get holdings for this portfolio
    holdings = conn.execute("""
        SELECT symbol, allocation 
        FROM holdings 
        WHERE portfolio_id = ? 
        ORDER BY allocation DESC
    """, (portfolio_id,)).fetchall()
    
    conn.close()
    
    if not holdings:
        return None
    
    # Chart.js colors for different stocks
    colors = [
        '#FF6384', '#36A2EB', '#FFCE56', '#4BC0C0', '#9966FF',
        '#FF9F40', '#FF6384', '#C9CBCF', '#4BC0C0', '#FF6384'
    ]
    
    # Prepare data for Chart.js
    labels = []
    data = []
    background_colors = []
    
    for i, holding in enumerate(holdings):
        labels.append(holding['symbol'])
        data.append(holding['allocation'])
        background_colors.append(colors[i % len(colors)])
    total = sum(data)
    if total < 100:
        labels.append('Cash')
        data.append(round(100 - total, 2))
        background_colors.append('#cccccc')  # Gray for cash
    return {
        'labels': labels,
        'data': data,
        'background_colors': background_colors,
        'total_allocation': total
    }

def get_aggregated_portfolio_allocation(user_id):
    """Get aggregated allocation data across ALL portfolios for pie chart"""
    conn = get_db()
    
    # Get all holdings across all portfolios for this user
    holdings = conn.execute("""
        SELECT h.symbol, h.allocation 
        FROM holdings h
        JOIN portfolios p ON h.portfolio_id = p.id
        WHERE p.user_id = ?
    """, (user_id,)).fetchall()
    
    conn.close()
    
    if not holdings:
        return None
        
    # Aggregate allocation by symbol
    allocation_map = {}
    for holding in holdings:
        symbol = holding['symbol']
        alloc = holding['allocation']
        allocation_map[symbol] = allocation_map.get(symbol, 0) + alloc
    
    # Sort by allocation descending
    sorted_holdings = sorted(allocation_map.items(), key=lambda x: x[1], reverse=True)
    
    # Chart.js colors (Modern Palette)
    colors = [
        '#4f46e5', '#06b6d4', '#10b981', '#f59e0b', '#ef4444', 
        '#8b5cf6', '#ec4899', '#6366f1', '#14b8a6', '#84cc16'
    ]
    
    labels = []
    data = []
    background_colors = []
    
    for i, (symbol, alloc) in enumerate(sorted_holdings):
        labels.append(symbol)
        data.append(alloc)
        background_colors.append(colors[i % len(colors)])
        
    total = sum(data)
    if total < 100:
        labels.append('Cash / Unallocated')
        data.append(round(100 - total, 2))
        background_colors.append('#cbd5e1') # Slate-300 for cash
        
    return {
        'labels': labels,
        'data': data,
        'background_colors': background_colors
    }

def get_top_performers(user_id, limit=5):
    """Get top gainers and losers from all user's stocks"""
    conn = get_db()
    
    # Get all stocks from daily tracker
    daily_stocks = conn.execute("SELECT symbol FROM stocks WHERE user_id = ?", (user_id,)).fetchall()
    
    # Get all portfolio holdings
    portfolio_holdings = conn.execute("""
        SELECT DISTINCT h.symbol 
        FROM holdings h 
        JOIN portfolios p ON h.portfolio_id = p.id 
        WHERE p.user_id = ?
    """, (user_id,)).fetchall()
    
    conn.close()
    
    # Combine all unique symbols
    all_symbols = set()
    for stock in daily_stocks:
        all_symbols.add(stock['symbol'])
    for holding in portfolio_holdings:
        all_symbols.add(holding['symbol'])
    
    # Get live data for all symbols
    performers = []
    for symbol in all_symbols:
        live_data = get_holding_details(symbol, user_id)
        if live_data['change_pct'] != 0:  # Only include stocks with price changes
            performers.append({
                'symbol': symbol,
                'change_pct': live_data['change_pct'],
                'cmp': live_data['cmp']
            })
    
    # Sort by change percentage
    performers.sort(key=lambda x: x['change_pct'], reverse=True)
    
    # Get top gainers and losers
    top_gainers = performers[:limit]
    top_losers = performers[-limit:][::-1]  # Reverse to show biggest losers first
    
    return {
        'top_gainers': top_gainers,
        'top_losers': top_losers
    }

def get_worst_performer(user_id):
    """Get the worst performing stock from the user's daily tracker (stocks table)"""
    conn = get_db()
    worst_stock = None
    worst_change = 999
    stocks = conn.execute("SELECT symbol FROM stocks WHERE user_id = ?", (user_id,)).fetchall()
    for stock in stocks:
        live_data = get_holding_details(stock['symbol'], user_id)
        if live_data['change_pct'] < worst_change:
            worst_change = live_data['change_pct']
            worst_stock = {
                'symbol': stock['symbol'],
                'change_pct': live_data['change_pct'],
                'cmp': live_data['cmp']
            }
    conn.close()
    return worst_stock

def get_todays_results(user_id):
    """Get count and list of stocks with results today (YYYY-MM-DD format)"""
    from datetime import date
    conn = get_db()
    today_str = date.today().strftime('%Y-%m-%d')
    results = conn.execute("""
        SELECT symbol FROM profit_tracker 
        WHERE user_id = ? AND result_date = ?
    """, (user_id, today_str)).fetchall()
    conn.close()
    stocks = [row['symbol'] for row in results]
    return {
        'count': len(stocks),
        'stocks': stocks
    }


# --- AUTHENTICATION ROUTES ---
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    # This function remains unchanged
    if current_user.is_authenticated: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        conn = get_db()
        if conn.execute('SELECT COUNT(id) FROM users').fetchone()[0] >= 50:
            flash('Sorry, new user registration is closed at this time.', 'danger')
            conn.close()
            return redirect(url_for('signup'))
        username, password, confirm_password = request.form.get('username'), request.form.get('password'), request.form.get('confirm_password')
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if user: flash('Username already exists. Please choose a different one.', 'danger')
        elif password != confirm_password: flash('Passwords do not match.', 'danger')
        else:
            conn.execute('INSERT INTO users (username, password) VALUES (?, ?)', (username, generate_password_hash(password)))
            conn.commit()
            flash('Your account has been created! You can now log in.', 'success')
            conn.close()
            return redirect(url_for('login'))
        conn.close()
    return render_template('signup.html', active_page='signup')

@app.route('/login', methods=['GET', 'POST'])
def login():
    # This function remains unchanged
    if current_user.is_authenticated: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username, password = request.form.get('username'), request.form.get('password')
        conn = get_db()
        user_data = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()
        if user_data and check_password_hash(user_data['password'], password):
            login_user(User(id=user_data['id'], username=user_data['username']))
            return redirect(url_for('dashboard'))
        else: flash('Login Unsuccessful. Please check username and password.', 'danger')
    return render_template('login.html', active_page='login')

@app.route('/logout')
@login_required
def logout(): # This function remains unchanged
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))


# --- MAIN APPLICATION ROUTES ---
from flask import jsonify, request

# ... (rest of your existing routes) ...

@app.route('/search-tickers')
@login_required
def search_tickers():
    query = request.args.get('q', '').upper()
    
    if not query:
        return jsonify([])

    conn = get_db()
    # Get symbols the user has already personally tracked
    user_symbols = {row['symbol'] for row in conn.execute('SELECT DISTINCT symbol FROM profit_tracker WHERE user_id = ?', (current_user.id,))}
    conn.close()

    # Combine the master list with the user's personal list
    combined_symbols = MASTER_SYMBOL_LIST.union(user_symbols)

    # Find matches that start with the user's query
    matches = [symbol for symbol in combined_symbols if symbol.startswith(query)]
    
    # Return the first 10 matches as a JSON list
    return jsonify(matches[:10])

@app.route('/get-profit-data/<string:symbol>')
@login_required
def get_profit_data(symbol):
    """
    Fetches permanent data for a given stock for the current user.
    """
    conn = get_db()
    profit_data = conn.execute(
        'SELECT ath_profit, idx_type FROM profit_tracker WHERE symbol = ? AND user_id = ?',
        (symbol.upper(), current_user.id)
    ).fetchone()
    conn.close()
    
    if profit_data:
        # If data is found, return it as a JSON object
        return jsonify(dict(profit_data))
    else:
        # If no data is found (it's a new stock), return nothing
        return jsonify(None)

@app.route('/')
def index(): return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    t0 = time.time()
    conn = get_db()
    stocks_from_db = conn.execute('''
        SELECT s.*, pt.ath_profit, pt.idx_type, pt.result_date as result_date
        FROM stocks s
        LEFT JOIN profit_tracker pt ON s.symbol = pt.symbol AND pt.user_id = ?
        WHERE s.user_id = ?
        ORDER BY s.symbol ASC
    ''', (current_user.id, current_user.id)).fetchall()
    conn.close()
    print('DB fetch:', time.time() - t0); t1 = time.time()
    symbols = [stock['symbol'] for stock in stocks_from_db]
    live_data_dict = get_live_prices_cache(symbols, cache_expiry_minutes=5)
    print('Live data batch fetch:', time.time() - t1); t2 = time.time()
    processed_stocks = []
    for stock in stocks_from_db:
        display_stock = dict(stock)
        live_data = live_data_dict.get(stock['symbol'], {'price': 'N/A'})
        display_stock['result_date'] = stock['result_date'] if 'result_date' in stock.keys() else ''
        if stock['ath_profit'] is None:
            display_stock.update({'category': 'N/A', 'risk': 'N/A', 'final_trigger': 'N/A', 'remarks': 'Missing Profit Tracker data.'})
            display_stock['cmp'] = live_data['price']
            processed_stocks.append(display_stock)
            continue
        category, base_remarks = get_investment_category(stock['ath_outperformance'], stock['ath_profit'], stock['idx_type'])
        final_remark = base_remarks
        final_trigger = 'WAIT'
        cmp = live_data['price']
        price_gt_rounding, risk = 'N/A', 'N/A'
        if cmp != 'N/A':
            price_gt_rounding = 'Y' if cmp > stock['rounding'] else 'N'
            if stock['stop_loss'] and stock['stop_loss'] > 0:
                risk = f"{round(((stock['stop_loss'] / cmp) - 1) * 100, 2)}%"
            is_tracked = category in ['FUND', 'PROP']
            if is_tracked:
                if (stock['is_green_candle'] == 'Y' and price_gt_rounding == 'Y' and stock['is_close_above_ath'] == 'Y'):
                    final_trigger = "GO"
                    final_remark = ""
                elif price_gt_rounding == 'N':
                    final_remark = "Rounding > CMP"
        display_stock.update({
            'cmp': cmp,
            'category': category,
            'price_gt_rounding': price_gt_rounding,
            'risk': risk,
            'final_trigger': final_trigger,
            'remarks': final_remark
        })
        processed_stocks.append(display_stock)
    print('Processed stocks:', time.time() - t2); t3 = time.time()
    # Remove best/worst performer and top_performers from auto-load
    upcoming_results = get_upcoming_results_count(current_user.id)
    todays_results = get_todays_results(current_user.id)
    try:
        total_portfolio_data = calculate_total_portfolio_value(current_user.id)
    except Exception as e:
        print(f"Error calculating portfolio value: {e}")
        total_portfolio_data = {
            'total_value': 0,
            'change_pct': 0,
            'holdings_count': 0
        }
    return render_template('dashboard.html', 
                         stocks=processed_stocks, 
                         active_page='dashboard', 
                         total_portfolio_data=total_portfolio_data,
                         upcoming_results=upcoming_results,
                         todays_results=todays_results,
                         portfolio_allocation=get_aggregated_portfolio_allocation(current_user.id))

@app.route('/add', methods=['POST'])
@login_required
def add_stock():
    symbol = request.form.get('symbol', '').upper()
    if not symbol:
        flash('Error: Ticker symbol cannot be empty.', 'danger')
        return redirect(url_for('dashboard', _anchor='add-stock-form'))
    
    conn = get_db()
    try:
        # Check for duplicates in the daily tracker first
        is_duplicate = conn.execute('SELECT id FROM stocks WHERE symbol = ? AND user_id = ?', (symbol, current_user.id)).fetchone()
        if is_duplicate:
            flash(f"Error: '{symbol}' is already in today's tracker list.", 'danger')
            return redirect(url_for('dashboard', _anchor='add-stock-form'))

        # Validate ticker with yfinance
        if get_live_price(symbol) is None:
            flash(f"Error: Ticker '{symbol}' not found. Stock was not added.", 'danger')
            return redirect(url_for('dashboard', _anchor='add-stock-form'))

        # --- UPSERT PROFIT TRACKER DATA ---
        ath_profit = request.form.get('ath_profit')
        idx_type = request.form.get('idx_type')
        result_date = request.form.get('result_date')

        existing_profit_stock = conn.execute('SELECT * FROM profit_tracker WHERE symbol = ? AND user_id = ?', (symbol, current_user.id)).fetchone()

        if existing_profit_stock:
            # Only update columns that were actually submitted to avoid overwriting with NULL
            if ath_profit is not None and idx_type is not None:
                conn.execute('''
                    UPDATE profit_tracker 
                    SET ath_profit = ?, idx_type = ?, result_date = ? 
                    WHERE symbol = ? AND user_id = ?
                ''', (ath_profit, idx_type, result_date, symbol, current_user.id))
            elif result_date:
                # Only update the result_date if that's all we have
                conn.execute('''
                    UPDATE profit_tracker 
                    SET result_date = ? 
                    WHERE symbol = ? AND user_id = ?
                ''', (result_date, symbol, current_user.id))
        else:
            # Insert new record — use safe defaults if scanner data wasn't provided
            conn.execute('''
                INSERT INTO profit_tracker (user_id, symbol, ath_profit, idx_type, result_date) 
                VALUES (?, ?, ?, ?, ?)
            ''', (current_user.id, symbol,
                  ath_profit if ath_profit is not None else 'N',
                  idx_type if idx_type is not None else 'NONE',
                  result_date or 'Not Announced'))

        conn.commit()

        # --- ADD TO DAILY STOCKS TRACKER ---
        stop_loss = request.form.get('stop_loss')
        stock_data = (current_user.id, symbol, float(request.form['rounding']), float(stop_loss) if stop_loss else None,
                      request.form['ath_outperformance'], request.form['is_green_candle'], request.form['is_close_above_ath'])

        conn.execute('''
            INSERT INTO stocks (user_id, symbol, rounding, stop_loss, ath_outperformance, is_green_candle, is_close_above_ath) 
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', stock_data)

        conn.commit()
        flash(f"Success: Stock '{symbol}' added to the tracker.", 'success')
    except Exception as e:
        flash(f"Error adding stock: {str(e)}", 'danger')
    finally:
        conn.close()

    return redirect(url_for('dashboard', _anchor='add-stock-form'))

@app.route('/add_stock_ajax', methods=['POST'])
@login_required
def add_stock_ajax():
    data = request.get_json()
    symbol = data.get('symbol', '').upper()
    if not symbol:
        return jsonify({'success': False, 'error': 'Ticker symbol cannot be empty.'}), 400
    conn = get_db()
    try:
        is_duplicate = conn.execute('SELECT id FROM stocks WHERE symbol = ? AND user_id = ?', (symbol, current_user.id)).fetchone()
        if is_duplicate:
            return jsonify({'success': False, 'error': f"'{symbol}' is already in today's tracker list."}), 400
        cmp = get_live_price(symbol)
        if cmp is None:
            return jsonify({'success': False, 'error': f"Ticker '{symbol}' not found. Stock was not added."}), 400

        existing_profit_stock = conn.execute('SELECT * FROM profit_tracker WHERE symbol = ? AND user_id = ?', (symbol, current_user.id)).fetchone()
        ath_outperformance = data['ath_outperformance']

        # Capture values including result_date from the payload
        ath_profit = data.get('ath_profit')
        idx_type = data.get('idx_type')
        result_date = data.get('result_date')

        if existing_profit_stock:
            # Update existing record (Scanner might provide newer info/date)
            conn.execute('''
                UPDATE profit_tracker 
                SET ath_profit = ?, idx_type = ?, result_date = ?
                WHERE symbol = ? AND user_id = ?
            ''', (ath_profit, idx_type, result_date, symbol, current_user.id))
        else:
            # Insert new record
            conn.execute('''
                INSERT INTO profit_tracker (user_id, symbol, ath_profit, idx_type, result_date) 
                VALUES (?, ?, ?, ?, ?)
            ''', (current_user.id, symbol, ath_profit, idx_type, result_date))
        conn.commit()
        stop_loss = data.get('stop_loss')
        stock_data = (current_user.id, symbol, float(data['rounding']), float(stop_loss) if stop_loss else None,
                      ath_outperformance, data['is_green_candle'], data['is_close_above_ath'])
        conn.execute('INSERT INTO stocks (user_id, symbol, rounding, stop_loss, ath_outperformance, is_green_candle, is_close_above_ath) VALUES (?, ?, ?, ?, ?, ?, ?)', stock_data)
        conn.commit()
        # Fetch the new stock row data
        category, base_remarks = get_investment_category(ath_outperformance, ath_profit, idx_type)
        final_remark = base_remarks
        final_trigger = 'WAIT'
        price_gt_rounding, risk = 'N/A', 'N/A'
        if cmp:
            price_gt_rounding = 'Y' if cmp > float(data['rounding']) else 'N'
            if stop_loss and float(stop_loss) > 0:
                risk = f"{round(((float(stop_loss) / cmp) - 1) * 100, 2)}%"
            is_tracked = category in ['FUND', 'PROP']
            if is_tracked:
                if (data['is_green_candle'] == 'Y' and price_gt_rounding == 'Y' and data['is_close_above_ath'] == 'Y'):
                    final_trigger = "GO"
                    final_remark = ""
                elif price_gt_rounding == 'N':
                    final_remark = "Rounding > CMP"
        new_row = {
            'symbol': symbol,
            'cmp': cmp or 'N/A',
            'category': category,
            'price_gt_rounding': price_gt_rounding,
            'risk': risk,
            'final_trigger': final_trigger,
            'remarks': final_remark,
            'rounding': data['rounding'],
            'stop_loss': stop_loss,
            'id': conn.execute('SELECT id FROM stocks WHERE symbol = ? AND user_id = ?', (symbol, current_user.id)).fetchone()['id']
        }
        return jsonify({'success': True, 'row': new_row})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/edit/<int:stock_id>', methods=['GET', 'POST'])
@login_required
def edit_stock(stock_id):
    conn = get_db()
    # NEW QUERY: Joins with profit_tracker to get all necessary data
    stock = conn.execute('''
        SELECT s.*, pt.ath_profit, pt.idx_type
        FROM stocks s
        JOIN profit_tracker pt ON s.symbol = pt.symbol AND pt.user_id = ?
        WHERE s.id = ? AND s.user_id = ?
    ''', (current_user.id, stock_id, current_user.id)).fetchone()

    if not stock:
        flash('Error: Stock not found or you do not have permission to edit it.', 'danger')
        conn.close()
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        # The update logic remains the same, as we are only editing daily data
        stop_loss = request.form.get('stop_loss')
        updated_data = (
            request.form['rounding'],
            float(stop_loss) if stop_loss else None,
            request.form['ath_outperformance'],
            request.form['is_green_candle'],
            request.form['is_close_above_ath'],
            stock_id
        )
        conn.execute('''
            UPDATE stocks 
            SET rounding = ?, stop_loss = ?, ath_outperformance = ?, is_green_candle = ?, is_close_above_ath = ?
            WHERE id = ?
        ''', updated_data)
        conn.commit()
        conn.close()
        flash(f"Success: Stock '{stock['symbol']}' updated.", 'success')
        return redirect(url_for('dashboard'))
    
    conn.close()
    return render_template('edit_stock.html', stock=stock, active_page='dashboard')

# ... [All other routes like delete, go-list etc. are updated similarly]
# (Full, correct code for remaining routes provided below)

@app.route('/delete/<int:stock_id>', methods=['POST'])
@login_required
def delete_stock(stock_id): # This function remains unchanged
    conn = get_db()
    stock = conn.execute('SELECT symbol FROM stocks WHERE id = ? AND user_id = ?', (stock_id, current_user.id)).fetchone()
    if stock:
        conn.execute('DELETE FROM stocks WHERE id = ?', (stock_id,))
        conn.commit()
        flash(f"Stock '{stock['symbol']}' removed from the daily tracker.", 'info')
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/delete-all', methods=['POST'])
@login_required
def delete_all(): # This function remains unchanged
    conn = get_db()
    conn.execute('DELETE FROM stocks WHERE user_id = ?', (current_user.id,))
    conn.commit()
    conn.close()
    flash('Success: All stocks have been cleared from the daily tracker.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/delete-selected', methods=['POST'])
@login_required
def delete_selected():
    stock_ids_to_delete = request.form.getlist('stock_ids')

    if not stock_ids_to_delete:
        flash('You did not select any stocks to delete.', 'info')
        return redirect(url_for('dashboard'))

    conn = get_db()
    # Create placeholders for the query, e.g., (?, ?, ?)
    placeholders = ', '.join(['?'] * len(stock_ids_to_delete))
    
    # Efficiently delete all selected stocks in a single query
    # Also ensures user can only delete their own stocks
    query = f"DELETE FROM stocks WHERE id IN ({placeholders}) AND user_id = ?"
    
    # Add the current user's ID to the end of the list of parameters
    params = stock_ids_to_delete + [current_user.id]
    
    conn.execute(query, params)
    conn.commit()
    conn.close()
    
    flash(f"Successfully deleted {len(stock_ids_to_delete)} selected stock(s).", 'success')
    return redirect(url_for('dashboard'))

@app.route('/manual-archive', methods=['POST'])
@login_required
def manual_archive():
    """Manually triggers the archive process for the current user for the correct log date."""
    conn = get_db()
    # Use the same smart date logic as the archive script
    log_date = get_correct_log_date() # You'll need to copy this small function into app.py

    count = perform_archive_for_user(current_user.id, log_date, conn)

    conn.close()
    flash(f"Successfully archived {count} stocks for {log_date.strftime('%Y-%m-%d')}.", 'success')
    return redirect(url_for('dashboard'))



@app.route('/create-portfolio', methods=['POST'])
@login_required
def create_portfolio():
    name = request.form.get('portfolio_name')
    if name:
        conn = get_db()
        conn.execute('INSERT INTO portfolios (user_id, name) VALUES (?, ?)', (current_user.id, name))
        conn.commit()
        conn.close()
        flash(f"Portfolio '{name}' created successfully.", 'success')
    return redirect(url_for('portfolio'))

@app.route('/portfolio')
@login_required
def portfolio():
    # Get the active_tab from the URL, default to the first portfolio if it exists
    active_tab_id = request.args.get('active_tab')
    # skip_live = request.args.get('skip_live', '0') == '1'  # REMOVE THIS

    conn = get_db()
    portfolios_raw = conn.execute("SELECT * FROM portfolios WHERE user_id = ? ORDER BY id ASC", (current_user.id,)).fetchall()

    # Collect all unique holding symbols for batch fetching
    all_holding_symbols = set()
    for port in portfolios_raw:
        holdings_raw = conn.execute("SELECT * FROM holdings WHERE portfolio_id = ? ORDER BY symbol ASC", (port['id'],)).fetchall()
        for holding in holdings_raw:
            all_holding_symbols.add(holding['symbol'])
    # Always fetch live prices
    live_data_dict = get_live_prices_batch(list(all_holding_symbols))

    portfolios_processed = []
    # Determine the default active tab if none is specified
    default_active_set = False
    if not active_tab_id and portfolios_raw:
        active_tab_id = f"portfolio-{portfolios_raw[0]['id']}"
        default_active_set = True

    for port in portfolios_raw:
        portfolio_dict = dict(port)
        holdings_raw = conn.execute("SELECT * FROM holdings WHERE portfolio_id = ? ORDER BY symbol ASC", (port['id'],)).fetchall()
        processed_holdings = []
        for holding in holdings_raw:
            holding_dict = dict(holding)
            # Use batch live data
            live_data = live_data_dict.get(holding['symbol'], {'price': 'N/A', 'change_pct': 0})
            holding_dict['cmp'] = live_data['price']
            # Fetch result_date from profit_tracker
            result_row = conn.execute("SELECT result_date FROM profit_tracker WHERE symbol = ? AND user_id = ?", (holding['symbol'], current_user.id)).fetchone()
            holding_dict['result_date'] = result_row['result_date'] if result_row else ''
            # Use change_pct from batch live data
            holding_dict['change_pct'] = live_data.get('change_pct', 0)
            if live_data['price'] != 'N/A' and live_data['price'] > 0:
                holding_dict['from_exit'] = round(((holding['exit_price'] / live_data['price']) - 1) * 100, 2)
            else:
                holding_dict['from_exit'] = 'N/A'
            holding_dict['contribution'] = round((holding['allocation'] * holding_dict['change_pct'])/100, 2)
            processed_holdings.append(holding_dict)
        portfolio_dict['holdings'] = processed_holdings
        # Add allocation data for charts (to be updated for cash logic)
        allocation_data = get_portfolio_allocation_data(port['id'], current_user.id)
        portfolio_dict['allocation_data'] = allocation_data
        portfolios_processed.append(portfolio_dict)

    checklist_items = conn.execute('''
        SELECT f.*, pt.idx_type FROM fundamental_checklist f
        LEFT JOIN profit_tracker pt ON f.symbol = pt.symbol AND f.user_id = pt.user_id
        WHERE f.user_id = ? ORDER BY f.symbol ASC
    ''', (current_user.id,)).fetchall()
    conn.close()

    # If no portfolios exist and a checklist item was just added, make that tab active
    if not default_active_set and not portfolios_raw and active_tab_id == 'checklist':
        pass
    elif not default_active_set and not portfolios_raw:
        active_tab_id = 'checklist'

    return render_template('portfolio.html', portfolios=portfolios_processed, checklist_items=checklist_items, active_page='portfolio', active_tab_id=active_tab_id)

@app.route('/performance')
@login_required
def performance():
    """Dedicated performance analysis page"""
    # Get portfolio performance data
    conn = get_db()
    portfolios = conn.execute("SELECT * FROM portfolios WHERE user_id = ?", (current_user.id,)).fetchall()
    # Collect all unique holding symbols for batch fetching
    all_holding_symbols = set()
    portfolio_holdings_map = {}
    for portfolio in portfolios:
        holdings = conn.execute("SELECT * FROM holdings WHERE portfolio_id = ?", (portfolio['id'],)).fetchall()
        portfolio_holdings_map[portfolio['id']] = holdings
        for holding in holdings:
            all_holding_symbols.add(holding['symbol'])
    # Batch fetch live prices for all holdings using cache
    live_data_dict = get_live_prices_cache(list(all_holding_symbols), cache_expiry_minutes=5)
    portfolio_performance = []
    for portfolio in portfolios:
        holdings = portfolio_holdings_map[portfolio['id']]
        total_value = 0
        total_change = 0
        holdings_data = []
        for holding in holdings:
            live_data = live_data_dict.get(holding['symbol'], {'price': 'N/A', 'change_pct': 0})
            cmp = live_data['price']
            change_pct = live_data.get('change_pct', 0)
            if cmp != 'N/A' and cmp > 0:
                current_value = (holding['allocation'] / 100) * cmp
                total_value += current_value
                total_change += change_pct
                holdings_data.append({
                    'symbol': holding['symbol'],
                    'allocation': holding['allocation'],
                    'change_pct': change_pct,
                    'current_value': current_value
                })
        portfolio_performance.append({
            'name': portfolio['name'],
            'total_value': total_value,
            'total_change': total_change / len(holdings) if holdings else 0,
            'holdings_count': len(holdings),
            'holdings': holdings_data
        })
    conn.close()
    return render_template('performance.html', 
                         portfolio_performance=portfolio_performance,
                         active_page='performance')

@app.route('/add-holding/<int:portfolio_id>', methods=['POST'])
@login_required
def add_holding(portfolio_id):
    symbol = request.form.get('symbol','').upper()
    exit_price = request.form.get('exit_price')
    allocation = request.form.get('allocation')
    rg_status = request.form.get('rg_status', 'R')

    if symbol and exit_price and allocation:
        conn = get_db()
        conn.execute(
            'INSERT INTO holdings (portfolio_id, symbol, exit_price, allocation, rg_status) VALUES (?, ?, ?, ?, ?)',
            (portfolio_id, symbol, float(exit_price), float(allocation), rg_status)
        )
        conn.commit()
        conn.close()
        flash(f"Holding '{symbol}' added to portfolio.", 'success')
    else:
        flash('All fields are required to add a holding.', 'danger')

    return redirect(url_for('portfolio'))

@app.route('/edit-holding/<int:holding_id>', methods=['GET', 'POST'])
@login_required
def edit_holding(holding_id):
    conn = get_db()
    holding = conn.execute('''
        SELECT h.* FROM holdings h JOIN portfolios p ON h.portfolio_id = p.id
        WHERE h.id = ? AND p.user_id = ?
    ''', (holding_id, current_user.id)).fetchone()

    if not holding:
        flash('Holding not found or you do not have permission to edit it.', 'danger')
        return redirect(url_for('portfolio'))

    if request.method == 'POST':
        exit_price = request.form.get('exit_price')
        allocation = request.form.get('allocation')
        rg_status = request.form.get('rg_status', 'R')
        if exit_price and allocation:
            conn.execute(
                'UPDATE holdings SET exit_price = ?, allocation = ?, rg_status = ? WHERE id = ?',
                (float(exit_price), float(allocation), rg_status, holding_id)
            )
            conn.commit()
            flash(f"Holding '{holding['symbol']}' updated successfully.", 'success')
            conn.close()
            return redirect(url_for('portfolio'))
    
    conn.close()
    return render_template('edit_holding.html', holding=holding, active_page='portfolio')

@app.route('/delete-holding/<int:holding_id>', methods=['POST'])
@login_required
def delete_holding(holding_id):
    conn = get_db()
    holding = conn.execute('''
        SELECT h.id FROM holdings h JOIN portfolios p ON h.portfolio_id = p.id
        WHERE h.id = ? AND p.user_id = ?
    ''', (holding_id, current_user.id)).fetchone()

    if holding:
        conn.execute('DELETE FROM holdings WHERE id = ?', (holding_id,))
        conn.commit()
        flash('Holding removed from portfolio.', 'info')

    conn.close()
    return redirect(url_for('portfolio'))

@app.route('/rename-portfolio/<int:portfolio_id>', methods=['GET', 'POST'])
@login_required
def rename_portfolio(portfolio_id):
    conn = get_db()
    portfolio = conn.execute('SELECT * FROM portfolios WHERE id = ? AND user_id = ?', (portfolio_id, current_user.id)).fetchone()
    if not portfolio:
        flash('Portfolio not found or you do not have permission to edit it.', 'danger')
        return redirect(url_for('portfolio'))

    if request.method == 'POST':
        new_name = request.form.get('portfolio_name')
        if new_name:
            conn.execute('UPDATE portfolios SET name = ? WHERE id = ?', (new_name, portfolio_id))
            conn.commit()
            flash(f"Portfolio renamed to '{new_name}'.", 'success')
        conn.close()
        # Redirect with skip_live=1 to avoid slow live price fetch
        return redirect(url_for('portfolio', skip_live=1))

    conn.close()
    return render_template('rename_portfolio.html', portfolio=portfolio, active_page='portfolio')

@app.route('/delete-portfolio/<int:portfolio_id>', methods=['POST'])
@login_required
def delete_portfolio(portfolio_id):
    conn = get_db()
    portfolio = conn.execute('SELECT * FROM portfolios WHERE id = ? AND user_id = ?', (portfolio_id, current_user.id)).fetchone()
    if portfolio:
        conn.execute('DELETE FROM holdings WHERE portfolio_id = ?', (portfolio_id,))
        conn.execute('DELETE FROM portfolios WHERE id = ?', (portfolio_id,))
        conn.commit()
        flash(f"Portfolio '{portfolio['name']}' and all its holdings have been deleted.", 'info')
    conn.close()
    return redirect(url_for('portfolio'))

@app.route('/add-to-checklist', methods=['POST'])
@login_required
def add_to_checklist():
    symbol = request.form.get('symbol', '').upper()
    # Get the new form fields
    analysis_status = request.form.get('analysis_status', 'N')
    fund_allocation = request.form.get('fund_allocation')
    breakout_status = request.form.get('breakout_status', 'N')

    if symbol:
        conn = get_db()
        # Insert the new stock with all its initial data
        conn.execute(
            "INSERT OR IGNORE INTO fundamental_checklist (user_id, symbol, analysis_status, fund_allocation, breakout_status) VALUES (?, ?, ?, ?, ?)",
            (current_user.id, symbol, analysis_status, fund_allocation, breakout_status)
        )
        conn.commit()
        conn.close()
        flash(f"'{symbol}' was added to your Fundamental Checklist.", 'success')

    # This tells the page to re-open the 'checklist' tab after redirecting
    return redirect(url_for('portfolio', active_tab='checklist'))

@app.route('/edit-checklist-item/<int:item_id>', methods=['GET', 'POST'])
@login_required
def edit_checklist_item(item_id):
    conn = get_db()
    item = conn.execute('SELECT * FROM fundamental_checklist WHERE id = ? AND user_id = ?', (item_id, current_user.id)).fetchone()
    if not item:
        flash('Checklist item not found.', 'danger')
        return redirect(url_for('portfolio'))

    if request.method == 'POST':
        analysis_status = request.form.get('analysis_status')
        fund_allocation = request.form.get('fund_allocation')
        breakout_status = request.form.get('breakout_status')

        conn.execute(
            'UPDATE fundamental_checklist SET analysis_status = ?, fund_allocation = ?, breakout_status = ? WHERE id = ?',
            (analysis_status, fund_allocation, breakout_status, item_id)
        )
        conn.commit()
        conn.close()
        flash(f"Checklist for '{item['symbol']}' updated.", 'success')
        return redirect(url_for('portfolio'))

    conn.close()
    return render_template('edit_checklist_item.html', item=item, active_page='portfolio')

@app.route('/delete-checklist-item/<int:item_id>', methods=['POST'])
@login_required
def delete_checklist_item(item_id):
    conn = get_db()
    item = conn.execute('SELECT symbol FROM fundamental_checklist WHERE id = ? AND user_id = ?', (item_id, current_user.id)).fetchone()
    if item:
        conn.execute('DELETE FROM fundamental_checklist WHERE id = ?', (item_id,))
        conn.commit()
        flash(f"'{item['symbol']}' removed from your checklist.", 'info')
    conn.close()
    return redirect(url_for('portfolio'))

@app.route('/results-calendar')
@login_required
def results_calendar():
    search_symbol = request.args.get('search_symbol', '').strip().upper()
    result_date_str = request.args.get('result_date')
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    fno_only = request.args.get('fno_only', '') == '1'

    conn = get_db()
    base_query = "SELECT symbol, result_date, idx_type FROM profit_tracker WHERE user_id = ? AND result_date NOT IN ('Not Announced', 'N/A', 'CONFLICT')"
    params = [current_user.id]
    if search_symbol:
        base_query += " AND symbol LIKE ?"
        params.append(f"%{search_symbol}%")
    if fno_only:
        base_query += " AND idx_type = 'FNO'"
    all_results_raw = conn.execute(base_query, tuple(params)).fetchall()
    conflicts = conn.execute("SELECT symbol, result_date FROM profit_tracker WHERE user_id = ? AND result_date = 'CONFLICT'", (current_user.id,)).fetchall()
    conn.close()

    final_results = []
    all_results_with_dates = []
    unique_dates = set()
    for item in all_results_raw:
        try:
            item_with_date = dict(item)
            # Parse YYYY-MM-DD and store as date object
            item_with_date['date_obj'] = datetime.strptime(item['result_date'], '%Y-%m-%d').date()
            # For display, add a pretty string
            item_with_date['display_date'] = item_with_date['date_obj'].strftime('%d %B %Y')
            all_results_with_dates.append(item_with_date)
            unique_dates.add(item_with_date['date_obj'])
        except (ValueError, TypeError):
            continue

    # If result_date is present, filter by exact string match
    if result_date_str:
        final_results = [item for item in all_results_with_dates if item['result_date'] == result_date_str]
    # Otherwise, use the old logic
    elif start_date_str and end_date_str:
        try:
            s_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            e_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            if s_date == e_date:
                final_results = [item for item in all_results_with_dates if item['date_obj'] == s_date]
            else:
                final_results = [item for item in all_results_with_dates if s_date <= item['date_obj'] <= e_date]
        except (ValueError, TypeError):
            flash("Invalid date format for filtering.", "danger")
    else:
        s_date = date.today()
        e_date = date.today() + timedelta(days=7)
        final_results = [item for item in all_results_with_dates if s_date <= item['date_obj'] <= e_date]

    try:
        final_results.sort(key=lambda x: x['date_obj'])
    except (ValueError, TypeError):
        pass

    # Only show the next 5 unique result dates from today (sorted ascending)
    today = datetime.today().date()
    quick_dates = sorted([d for d in unique_dates if d >= today])[:5]
    quick_dates_str = [d.strftime('%Y-%m-%d') for d in quick_dates]
    quick_date_pairs = list(zip(quick_dates, quick_dates_str))
    return render_template('results_calendar.html', 
                           results=final_results, 
        quick_date_pairs=quick_date_pairs, 
        result_date=result_date_str, 
                           search_symbol=search_symbol,
                           start_date=start_date_str, 
        end_date=end_date_str, 
        conflicts=conflicts, 
        fno_only=fno_only,
        active_page='results_calendar')

@app.route('/download-results-view')
@login_required
def download_results_view():
    # This download function is now simplified and corrected
    search_symbol = request.args.get('search_symbol', '').strip().upper()
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    
    conn = get_db()
    base_query = "SELECT symbol, result_date, idx_type FROM profit_tracker WHERE user_id = ? AND result_date NOT IN ('Not Announced', 'N/A', 'CONFLICT')"
    params = [current_user.id]
    if search_symbol:
        base_query += " AND symbol LIKE ?"
        params.append(f"%{search_symbol}%")
    all_results_raw = conn.execute(base_query, tuple(params)).fetchall()
    conn.close()

    final_results_list = []
    
    if start_date_str and end_date_str:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        for item in all_results_raw:
            try:
                item_date = datetime.strptime(item['result_date'], '%d %B %Y').date()
                if start_date <= item_date <= end_date:
                    final_results_list.append(dict(item))
            except (ValueError, TypeError):
                continue
    else:
        final_results_list = [dict(row) for row in all_results_raw]

    if not final_results_list:
        flash("No data to download for the selected criteria.", "info")
        return redirect(url_for('results_calendar'))

    df = pd.DataFrame(final_results_list)
    df.rename(columns={'symbol': 'SYMBOL', 'result_date': 'RESULT_DATE', 'idx_type': 'INDEX_TYPE'}, inplace=True)

    output = io.BytesIO()
    writer = pd.ExcelWriter(output, engine='openpyxl')
    df.to_excel(writer, index=False, sheet_name='ResultsView')
    writer.close()
    output.seek(0)
    
    return Response(output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={"Content-Disposition": f"attachment;filename=results_view.xlsx"})

@app.route('/refresh-result-dates', methods=['POST'])
@login_required
def refresh_result_dates():
    """
    Triggers the daily_tasks.py script to run in the background
    using the PythonAnywhere API.
    """
    username = 'HarshrajJadeja' # Your PythonAnywhere username
    api_token = os.environ.get('PA_API_TOKEN')

    if not api_token:
        flash('Error: API Token not configured on the server.', 'danger')
        return redirect(url_for('results_calendar'))

    # The command to run your script
    command_to_run = f"/home/{username}/.virtualenvs/my-app-env/bin/python /home/{username}/daily_tasks.py"

    # The PythonAnywhere API endpoint for starting a new console
    console_url = f'https://www.pythonanywhere.com/api/v0/user/{username}/consoles/'

    try:
        response = requests.post(
            console_url,
            headers={'Authorization': f'Token {api_token}'},
            json={'executable': command_to_run}
        )
        if response.status_code == 201:
            flash('Background update process has been started. It may take several minutes to complete.', 'info')
        else:
            flash(f'Error starting update process: {response.text}', 'danger')
    except Exception as e:
        flash(f'An error occurred when trying to start the task: {e}', 'danger')

    return redirect(url_for('results_calendar'))

@app.route('/watchlist')
@login_required
def watchlist():
    """Renders the new Watchlist page."""
    conn = get_db()
    watchlist_stocks = conn.execute(
        "SELECT * FROM watchlist WHERE user_id = ? ORDER BY symbol ASC",
        (current_user.id,)
    ).fetchall()
    conn.close()
    return render_template('watchlist.html', stocks=watchlist_stocks, active_page='watchlist')

@app.route('/add-to-watchlist', methods=['POST'])
@login_required
def add_to_watchlist():
    """Handles the manual form submission for adding a stock to the watchlist."""
    symbol = request.form.get('symbol', '').upper()
    reason = request.form.get('reason', 'Manually Added')
    if symbol:
        conn = get_db()
        # INSERT OR IGNORE will fail silently if the stock is already on the watchlist, preventing duplicates
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (user_id, symbol, reason) VALUES (?, ?, ?)",
            (current_user.id, symbol, reason)
        )
        conn.commit()
        conn.close()
        flash(f"'{symbol}' was added to your watchlist (if not already present).", 'success')
    return redirect(url_for('watchlist'))

@app.route('/delete-watchlist-entries', methods=['POST'])
@login_required
def delete_watchlist_entries():
    """Handles multi-deleting stocks from the watchlist."""
    ids_to_delete = request.form.getlist('stock_ids')
    if not ids_to_delete:
        flash('You did not select any stocks to delete.', 'info')
        return redirect(url_for('watchlist'))

    conn = get_db()
    placeholders = ', '.join(['?'] * len(ids_to_delete))
    query = f"DELETE FROM watchlist WHERE id IN ({placeholders}) AND user_id = ?"
    params = ids_to_delete + [current_user.id]

    conn.execute(query, params)
    conn.commit()
    conn.close()

    flash(f"Successfully deleted {len(ids_to_delete)} selected stock(s) from your watchlist.", 'success')
    return redirect(url_for('watchlist'))

@app.route('/download-watchlist')
@login_required
def download_watchlist():
    """Downloads the user's entire watchlist as an Excel file."""
    conn = get_db()
    query = "SELECT symbol, reason FROM watchlist WHERE user_id = ? ORDER BY symbol ASC"
    df = pd.read_sql_query(query, conn, params=(current_user.id,))
    conn.close()
    
    # Rename columns for user-friendly Excel file
    df.rename(columns={'symbol': 'SYMBOL', 'reason': 'REASON'}, inplace=True)
    
    output = io.BytesIO()
    writer = pd.ExcelWriter(output, engine='openpyxl')
    df.to_excel(writer, index=False, sheet_name='Watchlist')
    writer.close()
    output.seek(0)
    
    return Response(output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={"Content-Disposition": "attachment;filename=watchlist_backup.xlsx"})


@app.route('/upload-watchlist', methods=['POST'])
@login_required
def upload_watchlist():
    """Restores the watchlist from an uploaded Excel file."""
    if 'excel_file' not in request.files or request.files['excel_file'].filename == '':
        flash('No file selected', 'danger')
        return redirect(url_for('watchlist'))
        
    file = request.files['excel_file']
    if file and file.filename.endswith(('.xlsx', '.xls')):
        try:
            df = pd.read_excel(file, dtype=str)
            df.columns = [col.upper().strip() for col in df.columns]
            required_cols = {'SYMBOL', 'REASON'}
            if not required_cols.issubset(df.columns):
                flash(f'Error: Excel file must contain columns: {", ".join(required_cols)}', 'danger')
                return redirect(url_for('watchlist'))

            conn = get_db()
            # For a clean restore, first delete all of the user's existing watchlist items
            conn.execute('DELETE FROM watchlist WHERE user_id = ?', (current_user.id,))
            
            # Now, insert all records from the Excel file
            for _, row in df.iterrows():
                # Use INSERT OR IGNORE just in case the excel file has duplicate symbols
                conn.execute(
                    "INSERT OR IGNORE INTO watchlist (user_id, symbol, reason) VALUES (?, ?, ?)",
                    (current_user.id, row['SYMBOL'], row['REASON'])
                )
            conn.commit()
            conn.close()
            flash('Successfully restored watchlist from backup file.', 'success')
        except Exception as e:
            flash(f'Error processing Excel file: {e}', 'danger')
            
    return redirect(url_for('watchlist'))

@app.route('/go-list')
@login_required
def go_list():
    conn = get_db()
    # CHANGED: Query updated to get ath_outperformance from stocks table
    stocks_from_db = conn.execute('''
        SELECT s.*, pt.ath_profit, pt.idx_type FROM stocks s
        LEFT JOIN profit_tracker pt ON s.symbol = pt.symbol AND pt.user_id = ?
        WHERE s.user_id = ? ORDER BY s.symbol ASC
    ''', (current_user.id, current_user.id)).fetchall()
    conn.close()
    go_stocks = []
    for stock in stocks_from_db:
        if not stock['ath_profit']: continue
        # CHANGED: Pass the dynamic ath_outperformance value
        category, _ = get_investment_category(stock['ath_outperformance'], stock['ath_profit'], stock['idx_type'])
        cmp = get_live_price(stock['symbol'])
        if cmp:
            price_gt_rounding = 'Y' if cmp > stock['rounding'] else 'N'
            if category in ['FUND', 'PROP'] and stock['is_green_candle'] == 'Y' and price_gt_rounding == 'Y' and stock['is_close_above_ath'] == 'Y':
                go_stock_data = dict(stock)
                go_stock_data['category'] = category
                go_stocks.append(go_stock_data)
    return render_template('go_list.html', stocks=go_stocks, active_page='go_list')

@app.route('/history/<string:log_date>')
@login_required
def history_detail(log_date):
    """Renders the detailed view of all stocks for a specific log date."""
    conn = get_db()
    records = conn.execute(
        "SELECT * FROM historical_log WHERE user_id = ? AND log_date = ? ORDER BY symbol ASC",
        (current_user.id, log_date)
    ).fetchall()
    conn.close()
    return render_template('history_detail.html', records=records, date=log_date, active_page='history')

@app.route('/delete-history-entries', methods=['POST'])
@login_required
def delete_history_entries():
    log_ids_to_delete = request.form.getlist('log_ids')
    log_date = request.form.get('log_date') # Get date for redirect

    if not log_ids_to_delete:
        flash('You did not select any records to delete.', 'info')
        return redirect(url_for('history_detail', log_date=log_date))

    conn = get_db()
    placeholders = ', '.join(['?'] * len(log_ids_to_delete))
    query = f"DELETE FROM historical_log WHERE id IN ({placeholders}) AND user_id = ?"
    params = log_ids_to_delete + [current_user.id]

    conn.execute(query, params)
    conn.commit()
    conn.close()

    flash(f"Successfully deleted {len(log_ids_to_delete)} selected historical records.", 'success')
    # Redirect back to the same detail page
    return redirect(url_for('history_detail', log_date=log_date))

@app.route('/download-history')
@login_required
def download_history():
    """Downloads the user's entire historical log as an Excel file."""
    conn = get_db()
    query = "SELECT log_date, symbol, eod_price, rounding_price, stop_loss, category, final_trigger, remarks FROM historical_log WHERE user_id = ? ORDER BY log_date DESC, symbol ASC"
    df = pd.read_sql_query(query, conn, params=(current_user.id,))
    conn.close()
    
    output = io.BytesIO()
    writer = pd.ExcelWriter(output, engine='openpyxl')
    df.to_excel(writer, index=False, sheet_name='HistoricalLog')
    writer.close()
    output.seek(0)
    
    return Response(output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={"Content-Disposition": "attachment;filename=historical_log_backup.xlsx"})


@app.route('/upload-history', methods=['POST'])
@login_required
def upload_history():
    """Restores the historical log from an uploaded Excel file."""
    if 'excel_file' not in request.files or request.files['excel_file'].filename == '':
        flash('No file selected', 'danger')
        return redirect(url_for('history'))
        
    file = request.files['excel_file']
    if file and file.filename.endswith(('.xlsx', '.xls')):
        try:
            df = pd.read_excel(file, dtype={'log_date': str}) # Read date as string
            conn = get_db()
            
            # For a clean restore, first delete all of the user's existing history
            conn.execute('DELETE FROM historical_log WHERE user_id = ?', (current_user.id,))
            
            # Now, insert all records from the Excel file
            for _, row in df.iterrows():
                log_data = (
                    current_user.id, row['log_date'], row['symbol'], row['eod_price'],
                    row['rounding_price'], row.get('stop_loss'), row['category'],
                    row['final_trigger'], row.get('remarks')
                )
                conn.execute('''
                    INSERT INTO historical_log (user_id, log_date, symbol, eod_price, rounding_price, stop_loss, category, final_trigger, remarks)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', log_data)
            
            conn.commit()
            conn.close()
            flash('Successfully restored historical log from backup file.', 'success')
        except Exception as e:
            flash(f'Error processing Excel file: {e}', 'danger')
            
    return redirect(url_for('history'))

@app.route('/profit-manager', methods=['GET'])
@login_required
def profit_manager():
    stocks = get_profit_manager_stocks()
    search_term = request.args.get('search', '').strip()
    
    # Get preview data from session if available
    ath_profit_preview = session.get('ath_profit_preview')
    result_date_preview = session.get('result_date_preview')
    
    return render_template('profit_manager.html', 
                         stocks=stocks, 
                         active_page='profit_manager', 
                         search_term=search_term,
                         ath_profit_preview=ath_profit_preview,
                         result_date_preview=result_date_preview)

@app.route('/upload-profits', methods=['POST'])
@login_required
def upload_profits():
    # This function needs to be updated to expect the new Excel format
    if 'excel_file' not in request.files or request.files['excel_file'].filename == '':
        flash('No file selected', 'danger')
        return redirect(url_for('profit_manager'))
    file = request.files['excel_file']
    if file and file.filename.endswith(('.xlsx', '.xls')):
        try:
            df = pd.read_excel(file, dtype=str)
            df.columns = [col.upper().strip() for col in df.columns]
            # CHANGED: Required columns updated
            required_cols = {'SYMBOL', 'ATH_PROFIT', 'INDEX_TYPE', 'RESULT_DATE'}
            if not required_cols.issubset(df.columns):
                flash(f'Error: Excel file must contain columns: {", ".join(required_cols)}', 'danger')
                return redirect(url_for('profit_manager'))
            conn = get_db()
            for _, row in df.iterrows():
                result_date = row['RESULT_DATE'] if pd.notna(row['RESULT_DATE']) else 'Not Announced'
                # Convert to YYYY-MM-DD if possible
                if result_date not in ['Not Announced', 'N/A', 'CONFLICT']:
                    try:
                        # Try parsing old format first
                        try:
                            date_obj = datetime.strptime(result_date, '%d %B %Y')
                        except Exception:
                            date_obj = datetime.strptime(result_date, '%Y-%m-%d')
                        result_date = date_obj.strftime('%Y-%m-%d')
                    except Exception:
                        pass
                conn.execute('''
                    INSERT INTO profit_tracker (user_id, symbol, ath_profit, idx_type, result_date) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, symbol) DO UPDATE SET
                    ath_profit=excluded.ath_profit, idx_type=excluded.idx_type, result_date=excluded.result_date
                    ''', (current_user.id, row['SYMBOL'], row['ATH_PROFIT'], row['INDEX_TYPE'], result_date))
            conn.commit()
            conn.close()
            flash('Successfully uploaded and updated profit tracker.', 'success')
        except Exception as e: flash(f'Error processing Excel file: {e}', 'danger')
    return redirect(url_for('profit_manager'))

@app.route('/delete-profit-record/<int:stock_id>', methods=['POST'])
@login_required
def delete_profit_record(stock_id): # This function remains unchanged
    conn = get_db()
    stock = conn.execute('SELECT symbol FROM profit_tracker WHERE id = ? AND user_id = ?', (stock_id, current_user.id)).fetchone()
    if stock:
        conn.execute('DELETE FROM stocks WHERE symbol = ? AND user_id = ?', (stock['symbol'], current_user.id))
        conn.execute('DELETE FROM profit_tracker WHERE id = ?', (stock_id,))
        conn.commit()
        flash(f"'{stock['symbol']}' has been permanently deleted from all records.", 'success')
    conn.close()
    return redirect(url_for('profit_manager'))

@app.route('/delete-profit-entries', methods=['POST'])
@login_required
def delete_profit_entries():
    """Handles multi-deleting stocks from the permanent profit_tracker and all other tables."""
    ids_to_delete = request.form.getlist('profit_ids')
    if not ids_to_delete:
        flash('You did not select any stocks to delete.', 'info')
        return redirect(url_for('profit_manager'))

    conn = get_db()
    
    # First, get the symbols for the given IDs to delete them from other tables
    placeholders = ', '.join(['?'] * len(ids_to_delete))
    symbols_to_delete_rows = conn.execute(f"SELECT symbol FROM profit_tracker WHERE id IN ({placeholders}) AND user_id = ?", ids_to_delete + [current_user.id]).fetchall()
    symbols_to_delete = [row['symbol'] for row in symbols_to_delete_rows]

    if symbols_to_delete:
        symbol_placeholders = ', '.join(['?'] * len(symbols_to_delete))
        # Delete from all other tables
        conn.execute(f"DELETE FROM stocks WHERE symbol IN ({symbol_placeholders}) AND user_id = ?", symbols_to_delete + [current_user.id])
        conn.execute(f"DELETE FROM watchlist WHERE symbol IN ({symbol_placeholders}) AND user_id = ?", symbols_to_delete + [current_user.id])
        conn.execute(f"DELETE FROM historical_log WHERE symbol IN ({symbol_placeholders}) AND user_id = ?", symbols_to_delete + [current_user.id])

    # Finally, delete from the profit_tracker itself
    conn.execute(f"DELETE FROM profit_tracker WHERE id IN ({placeholders}) AND user_id = ?", ids_to_delete + [current_user.id])
    
    conn.commit()
    conn.close()
    
    flash(f"Successfully deleted {len(ids_to_delete)} stock(s) and all their associated data.", 'success')
    return redirect(url_for('profit_manager'))

@app.route('/toggle-profit-status/<int:stock_id>', methods=['POST'])
@login_required
def toggle_profit_status(stock_id): # This function remains unchanged
    conn = get_db()
    stock = conn.execute('SELECT * FROM profit_tracker WHERE id = ? AND user_id = ?', (stock_id, current_user.id)).fetchone()
    if stock:
        new_status = 'N' if stock['ath_profit'] == 'Y' else 'Y'
        conn.execute('UPDATE profit_tracker SET ath_profit = ? WHERE id = ?', (new_status, stock_id))
        conn.commit()
        flash(f"'{stock['symbol']}' ATH Profit status updated to '{new_status}'.", 'success')
    conn.close()
    return redirect(url_for('profit_manager'))

@app.route('/toggle-index-type/<int:stock_id>', methods=['POST'])
@login_required
def toggle_index_type(stock_id): # This function remains unchanged
    conn = get_db()
    stock = conn.execute('SELECT * FROM profit_tracker WHERE id = ? AND user_id = ?', (stock_id, current_user.id)).fetchone()
    if stock:
        new_status = 'TM' if stock['idx_type'] == 'FNO' else 'FNO'
        conn.execute('UPDATE profit_tracker SET idx_type = ? WHERE id = ?', (new_status, stock_id))
        conn.commit()
        flash(f"'{stock['symbol']}' Index Type updated to '{new_status}'.", 'success')
    conn.close()
    return redirect(url_for('profit_manager'))

@app.route('/edit-profit-record/<int:record_id>', methods=['GET', 'POST'])
@login_required
def edit_profit_record(record_id):
    conn = get_db()
    item = conn.execute('SELECT * FROM profit_tracker WHERE id = ? AND user_id = ?', (record_id, current_user.id)).fetchone()
    if not item:
        flash('Record not found.', 'danger')
        return redirect(url_for('profit_manager'))
    
    if request.method == 'POST':
        new_date = request.form.get('result_date').strip()
        if new_date:
            # Convert to YYYY-MM-DD if possible
            if new_date not in ['Not Announced', 'N/A', 'CONFLICT']:
                try:
                    try:
                        date_obj = datetime.strptime(new_date, '%d %B %Y')
                    except Exception:
                        date_obj = datetime.strptime(new_date, '%Y-%m-%d')
                    new_date = date_obj.strftime('%Y-%m-%d')
                except Exception:
                    pass
            conn.execute('UPDATE profit_tracker SET result_date = ? WHERE id = ?', (new_date, record_id))
            conn.commit()
            flash(f"Record for '{item['symbol']}' updated.", 'success')
        conn.close()
        return redirect(url_for('profit_manager'))

    conn.close()
    return render_template('edit_profit_record.html', item=item, active_page='profit_manager', quick_dates=[])

@app.route('/download-profits')
@login_required
def download_profits():
    conn = get_db()
    # CHANGED: Query updated to reflect new permanent data
    df = pd.read_sql_query("SELECT symbol, ath_profit, idx_type, result_date FROM profit_tracker WHERE user_id = ? ORDER BY symbol ASC", conn, params=(current_user.id,))
    conn.close()

    df.rename(columns={'symbol': 'SYMBOL', 'ath_profit': 'ATH_PROFIT', 'idx_type': 'INDEX_TYPE', 'result_date': 'RESULT_DATE'}, inplace=True)
    output = io.BytesIO()
    df.to_excel(output, engine='openpyxl', index=False, sheet_name='ProfitTracker')
    output.seek(0)
    return Response(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={"Content-Disposition": "attachment;filename=profit_tracker.xlsx"})

@app.route('/download-portfolio/<int:portfolio_id>')
@login_required
def download_portfolio(portfolio_id):
    """Downloads a specific portfolio's holdings as an Excel file."""
    conn = get_db()
    # Verify the portfolio belongs to the current user
    portfolio = conn.execute('SELECT name FROM portfolios WHERE id = ? AND user_id = ?', (portfolio_id, current_user.id)).fetchone()
    if not portfolio:
        conn.close()
        flash('Portfolio not found or you do not have permission to access it.', 'danger')
        return redirect(url_for('portfolio'))
    
    query = "SELECT symbol, exit_price, allocation, rg_status FROM holdings WHERE portfolio_id = ? ORDER BY symbol ASC"
    df = pd.read_sql_query(query, conn, params=(portfolio_id,))
    conn.close()
    
    # Rename columns for user-friendly Excel file
    df.rename(columns={'symbol': 'Company Ticker', 'exit_price': 'Exit Price', 'allocation': 'Allocation %', 'rg_status': 'R/G'}, inplace=True)
    
    output = io.BytesIO()
    writer = pd.ExcelWriter(output, engine='openpyxl')
    df.to_excel(writer, index=False, sheet_name='Portfolio')
    writer.close()
    output.seek(0)
    
    return Response(output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={"Content-Disposition": f"attachment;filename={portfolio['name']}_portfolio.xlsx"})

@app.route('/upload-portfolio/<int:portfolio_id>', methods=['POST'])
@login_required
def upload_portfolio(portfolio_id):
    """Restores portfolio holdings from an uploaded Excel file."""
    if 'excel_file' not in request.files or request.files['excel_file'].filename == '':
        flash('No file selected', 'danger')
        return redirect(url_for('portfolio'))
        
    file = request.files['excel_file']
    if file and file.filename.endswith(('.xlsx', '.xls')):
        try:
            df = pd.read_excel(file, dtype=str)
            df.columns = [col.strip() for col in df.columns]
            required_cols = {'Company Ticker', 'Exit Price', 'Allocation %', 'R/G'}
            if not required_cols.issubset(df.columns):
                flash(f'Error: Excel file must contain columns: {", ".join(required_cols)}', 'danger')
                return redirect(url_for('portfolio'))

            conn = get_db()
            # Verify the portfolio belongs to the current user
            portfolio = conn.execute('SELECT name FROM portfolios WHERE id = ? AND user_id = ?', (portfolio_id, current_user.id)).fetchone()
            if not portfolio:
                conn.close()
                flash('Portfolio not found or you do not have permission to access it.', 'danger')
                return redirect(url_for('portfolio'))
            
            # For a clean restore, first delete all existing holdings in this portfolio
            conn.execute('DELETE FROM holdings WHERE portfolio_id = ?', (portfolio_id,))
            
            # Now, insert all records from the Excel file
            for _, row in df.iterrows():
                if pd.notna(row['Company Ticker']) and pd.notna(row['Exit Price']) and pd.notna(row['Allocation %']) and pd.notna(row['R/G']):
                    conn.execute(
                        "INSERT INTO holdings (portfolio_id, symbol, exit_price, allocation, rg_status) VALUES (?, ?, ?, ?, ?)",
                        (portfolio_id, row['Company Ticker'].upper(), float(row['Exit Price']), float(row['Allocation %']), row['R/G'])
                    )
            conn.commit()
            conn.close()
            flash(f'Successfully restored portfolio holdings from backup file.', 'success')
        except Exception as e:
            flash(f'Error processing Excel file: {e}', 'danger')
            
    return redirect(url_for('portfolio'))

@app.route('/dashboard_widgets_data')
@login_required
def dashboard_widgets_data():
    # Best/Worst Performer from daily tracker (use cache)
    conn = get_db()
    stocks = conn.execute('SELECT symbol FROM stocks WHERE user_id = ?', (current_user.id,)).fetchall()
    symbols = [row['symbol'] for row in stocks]
    live_data_dict = get_live_prices_cache(symbols)
    best = None
    worst = None
    best_change = -999
    worst_change = 999
    for symbol in symbols:
        data = live_data_dict.get(symbol, {'price': 'N/A', 'change_pct': 0})
        if data['change_pct'] > best_change:
            best_change = data['change_pct']
            best = {'symbol': symbol, 'change_pct': data['change_pct']}
        if data['change_pct'] < worst_change:
            worst_change = data['change_pct']
            worst = {'symbol': symbol, 'change_pct': data['change_pct']}
    # Upcoming/Today's Results
    today_str = datetime.today().strftime('%Y-%m-%d')
    upcoming = conn.execute("SELECT COUNT(*) FROM profit_tracker WHERE user_id = ? AND result_date != 'Not Announced' AND result_date >= ?", (current_user.id, today_str)).fetchone()[0]
    todays = conn.execute("SELECT COUNT(*) FROM profit_tracker WHERE user_id = ? AND result_date = ?", (current_user.id, today_str)).fetchone()[0]
    conn.close()
    return jsonify({
        'best_performer': best,
        'worst_performer': worst,
        'upcoming_results': upcoming,
        'todays_results': todays
    })

# Helper for widget cache
WIDGET_CACHE_DB = 'widget_cache.db'
def get_widget_cache():
    import sqlite3
    conn = sqlite3.connect(WIDGET_CACHE_DB)
    conn.execute('''CREATE TABLE IF NOT EXISTS performer_cache (
        user_id INTEGER PRIMARY KEY,
        best_symbol TEXT,
        best_change REAL,
        best_price REAL,
        worst_symbol TEXT,
        worst_change REAL,
        worst_price REAL,
        last_updated INTEGER
    )''')
    return conn

@app.route('/dashboard_performer_widget')
@login_required
def dashboard_performer_widget():
    # Fetch only tracker stocks for the current user
    conn = get_db()
    stocks_from_db = conn.execute('SELECT symbol FROM stocks WHERE user_id = ?', (current_user.id,)).fetchall()
    conn.close()
    symbols = [row['symbol'] for row in stocks_from_db]
    # Fetch daily change % for these symbols (using yfinance .history or .download)
    import yfinance as yf
    best = None
    worst = None
    best_change = -999
    worst_change = 999
    for symbol in symbols:
        try:
            stock = yf.Ticker(f"{symbol.upper()}.NS")
            hist = stock.history(period="2d")
            if not hist.empty and len(hist['Close']) >= 2:
                price = hist['Close'].iloc[-1]
                prev = hist['Close'].iloc[-2]
                change_pct = ((price - prev) / prev) * 100 if prev else 0
            elif not hist.empty:
                price = hist['Close'].iloc[-1]
                change_pct = 0
            else:
                price = 'N/A'
                change_pct = 0
            if change_pct > best_change:
                best_change = change_pct
                best = {'symbol': symbol, 'change_pct': round(change_pct, 2), 'price': round(price, 2) if price != 'N/A' else 'N/A'}
            if change_pct < worst_change:
                worst_change = change_pct
                worst = {'symbol': symbol, 'change_pct': round(change_pct, 2), 'price': round(price, 2) if price != 'N/A' else 'N/A'}
        except Exception:
            continue
    # Cache the result
    cache_conn = get_widget_cache()
    cache_conn.execute('REPLACE INTO performer_cache (user_id, best_symbol, best_change, best_price, worst_symbol, worst_change, worst_price, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (current_user.id, best['symbol'] if best else None, best['change_pct'] if best else None, best['price'] if best else None,
         worst['symbol'] if worst else None, worst['change_pct'] if worst else None, worst['price'] if worst else None, int(time.time())))
    cache_conn.commit()
    cache_conn.close()
    return jsonify({'best_performer': best, 'worst_performer': worst})

@app.route('/dashboard_performer_widget_cached')
@login_required
def dashboard_performer_widget_cached():
    # Serve the last cached result if available
    cache_conn = get_widget_cache()
    row = cache_conn.execute('SELECT best_symbol, best_change, best_price, worst_symbol, worst_change, worst_price, last_updated FROM performer_cache WHERE user_id = ?', (current_user.id,)).fetchone()
    cache_conn.close()
    if row:
        best = {'symbol': row[0], 'change_pct': row[1], 'price': row[2]} if row[0] else None
        worst = {'symbol': row[3], 'change_pct': row[4], 'price': row[5]} if row[3] else None
        return jsonify({'best_performer': best, 'worst_performer': worst, 'last_updated': row[6]})
    else:
        return jsonify({'best_performer': None, 'worst_performer': None, 'last_updated': None})

@app.route('/refresh_portfolio_data/<int:portfolio_id>')
@login_required
def refresh_portfolio_data(portfolio_id):
    conn = get_db()
    holdings = conn.execute("SELECT * FROM holdings WHERE portfolio_id = ? ORDER BY symbol ASC", (portfolio_id,)).fetchall()
    symbols = [h['symbol'] for h in holdings]
    import yfinance as yf
    updated = []
    for holding in holdings:
        symbol = holding['symbol']
        try:
            stock = yf.Ticker(f"{symbol.upper()}.NS")
            hist = stock.history(period="2d")
            if not hist.empty and len(hist['Close']) >= 2:
                price = hist['Close'].iloc[-1]
                prev = hist['Close'].iloc[-2]
                change_pct = ((price - prev) / prev) * 100 if prev else 0
            else:
                # Fallback to .info if history is missing or too short
                info = stock.info
                price = info.get('regularMarketPrice', info.get('previousClose'))
                prev = info.get('previousClose')
                if price is not None and prev is not None and prev != 0:
                    change_pct = ((price - prev) / prev) * 100
                else:
                    change_pct = 0
            from_exit = round(((holding['exit_price'] / price) - 1) * 100, 2) if price != 'N/A' and price and price > 0 else 'N/A'
            contribution = round((holding['allocation'] * change_pct)/100, 2)
            updated.append({
                'id': holding['id'],
                'symbol': symbol,
                'cmp': round(price, 2) if price != 'N/A' and price is not None else 'N/A',
                'exit_price': holding['exit_price'],
                'from_exit': from_exit,
                'allocation': holding['allocation'],
                'change_pct': round(change_pct, 2),
                'contribution': contribution,
                'result_date': holding.get('result_date', '')
            })
        except Exception:
            updated.append({
                'id': holding['id'],
                'symbol': symbol,
                'cmp': 'N/A',
                'exit_price': holding['exit_price'],
                'from_exit': 'N/A',
                'allocation': holding['allocation'],
                'change_pct': 0,
                'contribution': 0,
                'result_date': holding.get('result_date', '')
            })
    conn.close()
    return jsonify({'holdings': updated})

@app.route('/upload-ath-profit-preview', methods=['POST'])
@login_required
def upload_ath_profit_preview():
    """Preview ATH Profit upload with hybrid validation"""
    if 'excel_file' not in request.files or request.files['excel_file'].filename == '':
        flash('No file selected', 'danger')
        return redirect(url_for('profit_manager'))
    
    file = request.files['excel_file']
    if not file.filename.endswith(('.xlsx', '.xls')):
        flash('Please upload an Excel file (.xlsx or .xls)', 'danger')
        return redirect(url_for('profit_manager'))
    
    try:
        df = pd.read_excel(file, dtype=str)
        df.columns = [col.upper().strip() for col in df.columns]
        
        # Check required columns
        required_cols = {'SYMBOL', 'ATH_PROFIT'}
        if not required_cols.issubset(df.columns):
            flash(f'Error: Excel file must contain columns: {", ".join(required_cols)}', 'danger')
            return redirect(url_for('profit_manager'))
        
        conn = get_db()
        
        # Get existing tickers with current values for comparison
        existing_data = {}
        for row in conn.execute('SELECT symbol, ath_profit FROM profit_tracker WHERE user_id = ?', (current_user.id,)).fetchall():
            existing_data[row['symbol']] = row['ath_profit']
        
        # Process upload data
        to_update = []
        to_create = []
        errors = []
        
        for idx, row in df.iterrows():
            # Handle symbol column
            symbol_raw = row['SYMBOL']
            if pd.isna(symbol_raw) or symbol_raw == '':
                errors.append(f"Row {idx+1}: Empty symbol")
                continue
            symbol = str(symbol_raw).strip().upper()
            
            # Handle ATH Profit column
            ath_profit_raw = row['ATH_PROFIT']
            if pd.isna(ath_profit_raw) or ath_profit_raw == '':
                errors.append(f"Row {idx+1}: Empty ATH Profit value")
                continue
            ath_profit = str(ath_profit_raw).strip().upper()
            
            # Validate data
            if not symbol:
                errors.append(f"Row {idx+1}: Empty symbol")
                continue
            if ath_profit not in ['Y', 'N']:
                errors.append(f"Row {idx+1}: Invalid ATH Profit value '{ath_profit}' (must be Y or N)")
                continue
            
            if symbol in existing_data:
                # Only add to update if value is different
                current_value = existing_data[symbol]
                if ath_profit != current_value:
                    to_update.append({'symbol': symbol, 'ath_profit': ath_profit, 'old_value': current_value})
            else:
                to_create.append({'symbol': symbol, 'ath_profit': ath_profit})
        
        # Store preview data in database
        import json
        preview_data = {
            'to_update': to_update,
            'to_create': to_create,
            'errors': errors
        }
        
        # Clear any existing preview for this user
        conn.execute('DELETE FROM upload_previews WHERE user_id = ? AND preview_type = ?', 
                    (current_user.id, 'ath_profit'))
        
        # Store new preview data
        conn.execute('INSERT INTO upload_previews (user_id, preview_type, preview_data) VALUES (?, ?, ?)',
                    (current_user.id, 'ath_profit', json.dumps(preview_data)))
        conn.commit()
        conn.close()
        
        return render_template('profit_manager.html', 
                             stocks=get_profit_manager_stocks(),
                             search_term=request.args.get('search', ''),
                             ath_profit_preview=preview_data)
        
    except Exception as e:
        flash(f'Error processing Excel file: {e}', 'danger')
        return redirect(url_for('profit_manager'))

@app.route('/upload-result-date-preview', methods=['POST'])
@login_required
def upload_result_date_preview():
    """Preview Result Date upload with hybrid validation"""
    if 'excel_file' not in request.files or request.files['excel_file'].filename == '':
        flash('No file selected', 'danger')
        return redirect(url_for('profit_manager'))
    
    file = request.files['excel_file']
    if not file.filename.endswith(('.xlsx', '.xls')):
        flash('Please upload an Excel file (.xlsx or .xls)', 'danger')
        return redirect(url_for('profit_manager'))
    
    try:
        df = pd.read_excel(file, dtype=str)
        df.columns = [col.upper().strip() for col in df.columns]
        
        # Check required columns
        required_cols = {'SYMBOL', 'RESULT_DATE'}
        if not required_cols.issubset(df.columns):
            flash(f'Error: Excel file must contain columns: {", ".join(required_cols)}', 'danger')
            return redirect(url_for('profit_manager'))
        
        conn = get_db()
        
        # Get existing tickers with current values for comparison
        existing_data = {}
        for row in conn.execute('SELECT symbol, result_date FROM profit_tracker WHERE user_id = ?', (current_user.id,)).fetchall():
            existing_data[row['symbol']] = row['result_date']
        
        # Process upload data
        to_update = []
        to_create = []
        errors = []
        
        for idx, row in df.iterrows():
            # Handle symbol column
            symbol_raw = row['SYMBOL']
            if pd.isna(symbol_raw) or symbol_raw == '':
                errors.append(f"Row {idx+1}: Empty symbol")
                continue
            symbol = str(symbol_raw).strip().upper()
            
            # Handle Result Date column
            result_date_raw = row['RESULT_DATE']
            if pd.isna(result_date_raw) or result_date_raw == '':
                result_date = 'Not Announced'
            else:
                result_date = str(result_date_raw)
            
            # Validate data
            if not symbol:
                errors.append(f"Row {idx+1}: Empty symbol")
                continue
            
            # Convert date format if needed
            if result_date not in ['Not Announced', 'N/A', 'CONFLICT']:
                try:
                    try:
                        date_obj = datetime.strptime(result_date, '%d %B %Y')
                    except Exception:
                        date_obj = datetime.strptime(result_date, '%Y-%m-%d')
                    result_date = date_obj.strftime('%Y-%m-%d')
                except Exception:
                    errors.append(f"Row {idx+1}: Invalid date format '{result_date}'")
                    continue
            
            if symbol in existing_data:
                # Only add to update if value is different
                current_value = existing_data[symbol]
                if result_date != current_value:
                    to_update.append({'symbol': symbol, 'result_date': result_date, 'old_value': current_value})
            else:
                to_create.append({'symbol': symbol, 'result_date': result_date})
        
        # Store preview data in database
        import json
        preview_data = {
            'to_update': to_update,
            'to_create': to_create,
            'errors': errors
        }
        
        # Clear any existing preview for this user
        conn.execute('DELETE FROM upload_previews WHERE user_id = ? AND preview_type = ?', 
                    (current_user.id, 'result_date'))
        
        # Store new preview data
        conn.execute('INSERT INTO upload_previews (user_id, preview_type, preview_data) VALUES (?, ?, ?)',
                    (current_user.id, 'result_date', json.dumps(preview_data)))
        conn.commit()
        conn.close()
        
        return render_template('profit_manager.html', 
                             stocks=get_profit_manager_stocks(),
                             search_term=request.args.get('search', ''),
                             result_date_preview=preview_data)
        
    except Exception as e:
        flash(f'Error processing Excel file: {e}', 'danger')
        return redirect(url_for('profit_manager'))

@app.route('/confirm-ath-profit-upload', methods=['POST'])
@login_required
def confirm_ath_profit_upload():
    """Process confirmed ATH Profit upload"""
    conn = get_db()
    
    # Get preview data from database
    preview_row = conn.execute(
        'SELECT preview_data FROM upload_previews WHERE user_id = ? AND preview_type = ? ORDER BY created_at DESC LIMIT 1',
        (current_user.id, 'ath_profit')
    ).fetchone()
    
    if not preview_row:
        flash('No preview data found. Please upload again.', 'danger')
        conn.close()
        return redirect(url_for('profit_manager'))
    
    import json
    preview_data = json.loads(preview_row['preview_data'])
    
    updated_count = 0
    created_count = 0
    
    try:
        # Update existing entries
        for item in preview_data['to_update']:
            conn.execute(
                'UPDATE profit_tracker SET ath_profit = ? WHERE symbol = ? AND user_id = ?',
                (item['ath_profit'], item['symbol'], current_user.id)
            )
            updated_count += 1
        
        # Create new entries with defaults
        for item in preview_data['to_create']:
            conn.execute(
                'INSERT INTO profit_tracker (user_id, symbol, ath_profit, idx_type, result_date) VALUES (?, ?, ?, ?, ?)',
                (current_user.id, item['symbol'], item['ath_profit'], 'TM', 'Not Announced')
            )
            created_count += 1
        
        # Clear preview data
        conn.execute('DELETE FROM upload_previews WHERE user_id = ? AND preview_type = ?', 
                    (current_user.id, 'ath_profit'))
        
        conn.commit()
        flash(f'Successfully updated {updated_count} existing entries and created {created_count} new entries.', 'success')
        
    except Exception as e:
        flash(f'Error processing upload: {e}', 'danger')
    finally:
        conn.close()
    
    return redirect(url_for('profit_manager'))

@app.route('/dashboard/turtle')
@login_required
def turtle_dashboard():
    """Renders the Checklist 3 Turtle Research Terminal."""
    kpi, sparklines, qualifiers = MarketStats.get_dashboard_data()
    return render_template('turtle_dashboard.html', active_page='dashboard', kpi=kpi or {}, sparklines=sparklines, qualifiers=qualifiers)

@app.route('/run_market_scan', methods=['POST'])
@login_required
def run_market_scan():
    """Manually triggers the daily market stats calculation."""
    try:
        MarketStats.calculate_daily_stats()
        flash('Daily Market Scan completed successfully.', 'success')
    except Exception as e:
        flash(f'Scan failed: {str(e)}', 'danger')
    return redirect(url_for('turtle_dashboard'))

@app.route('/news')
@login_required
def news():
    """News page showing market updates in 3 sections"""
    conn = get_db()
    
    # 1. Dashboard Stocks (Daily Tracker)
    dashboard_rows = conn.execute('SELECT DISTINCT symbol FROM stocks WHERE user_id = ?', (current_user.id,)).fetchall()
    dashboard_symbols = [row['symbol'] for row in dashboard_rows]
    
    # 2. Portfolio Stocks (Holdings)
    portfolio_rows = conn.execute('''
        SELECT DISTINCT h.symbol 
        FROM holdings h 
        JOIN portfolios p ON h.portfolio_id = p.id 
        WHERE p.user_id = ?
    ''', (current_user.id,)).fetchall()
    portfolio_symbols = [row['symbol'] for row in portfolio_rows]
    
    conn.close()
    
    # Fetch News
    # General: No symbols
    news_general = get_market_news([], limit=12)
    
    # Dashboard: Specific symbols
    if dashboard_symbols:
        news_dashboard = get_market_news(dashboard_symbols, limit=12)
    else:
        news_dashboard = []
        
    # Portfolio: Specific symbols
    if portfolio_symbols:
        news_portfolio = get_market_news(portfolio_symbols, limit=12)
    else:
        news_portfolio = []
    
    return render_template('news.html', 
                         news_general=news_general, 
                         news_dashboard=news_dashboard, 
                         news_portfolio=news_portfolio,
                         active_page='news')

@app.route('/fundamental-analysis', methods=['GET', 'POST'])
@login_required
def fundamental_analysis():
    """Fundamental Analysis Wizard page."""
    data = None
    error = None
    symbol = None
    weights = None
    
    if request.method == 'POST':
        symbol = request.form.get('symbol', '').strip().upper()
        
        # Extract Custom Weights from Form
        try:
            weights = {
                # Pillar Weights
                'Growth': float(request.form.get('w_growth', 30)),
                'Profitability': float(request.form.get('w_quality', 25)),
                'Valuation': float(request.form.get('w_value', 20)),
                'Technicals': float(request.form.get('w_tech', 25)),
                
                # Growth Factors
                'G_Rev': float(request.form.get('w_g_rev', 35)),
                'G_EPS': float(request.form.get('w_g_eps', 35)),
                'G_OCF': float(request.form.get('w_g_ocf', 20)),
                'G_Cons': float(request.form.get('w_g_cons', 10)),
                
                # Profitability Factors
                'Q_ROE': float(request.form.get('w_q_roe', 30)),
                'Q_Mar': float(request.form.get('w_q_mar', 20)),
                'Q_Deb': float(request.form.get('w_q_deb', 20)),
                'Q_Sta': float(request.form.get('w_q_sta', 15)),
                'Q_Cas': float(request.form.get('w_q_cas', 15)),
                
                # Valuation Factors
                'V_PEG': float(request.form.get('w_v_peg', 15)),
                'V_PER': float(request.form.get('w_v_per', 30)),
                'V_EV': float(request.form.get('w_v_ev', 20)),
                'V_PB': float(request.form.get('w_v_pb', 10)),
                
                # Technical Factors
                'T_Tre': float(request.form.get('w_t_tre', 35)),
                'T_Mom': float(request.form.get('w_t_mom', 30)),
                'T_RSI': float(request.form.get('w_t_rsi', 15)),
                'T_Dra': float(request.form.get('w_t_dra', 10)),
                'T_Exi': float(request.form.get('w_t_exi', 10))
            }
        except (ValueError, TypeError):
            weights = None # Logic will use defaults if None

    # Check for Symbol in GET args (e.g. from Dashboard Drawer or Link)
    if not symbol:
        symbol = request.args.get('symbol', '').strip().upper()

    if symbol:
        try:
            data = get_stock_fundamentals(symbol, custom_weights=weights)
            if not data:
                error = f"Could not fetch data for '{symbol}'. Please check the symbol and try again."
            else:
                # Log the successful run (Checklist 1.7)
                log_scoring_run(symbol, data, weights)
        except Exception as e:
            error = f"Error processing '{symbol}': {str(e)}"
                
    # Fetch History for Chart
    history_data = []
    if symbol:
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT calc_date, overall_score, growth_score, quality_score, value_score, tech_score FROM scoring_history WHERE symbol = ? ORDER BY calc_date ASC LIMIT 30",
                (symbol,)
            ).fetchall()
            conn.close()
            # Convert rows to dicts for JSON serialization
            history_data = [dict(row) for row in rows]
        except Exception as e:
            print(f"Error fetching history: {e}")

    # Check for Embed Mode (from Dashboard Drawer)
    embed_mode = request.args.get('embed') == 'true'

    return render_template('fundamental_analysis.html', data=data, error=error, symbol=symbol, weights=weights, history=history_data, active_page='analysis', embed_mode=embed_mode)

@app.route('/analytics')
@login_required
def analytics():
    """
    Sector Deep Analytics Route (Master Plan Phase 4).
    """
    mode = request.args.get('mode', 'market_cap')
    conn = get_db()
    
    # 1. Fetch User Universe (Same logic as Turtle Dashboard)
    wl = conn.execute("SELECT symbol FROM watchlist WHERE user_id = ?", (current_user.id,)).fetchall()
    pt = conn.execute("SELECT symbol FROM profit_tracker WHERE user_id = ?", (current_user.id,)).fetchall()
    hl = conn.execute("SELECT DISTINCT symbol FROM holdings h JOIN portfolios p ON h.portfolio_id=p.id WHERE p.user_id = ?", (current_user.id,)).fetchall()
    conn.close()
    
    universe = set([r['symbol'] for r in wl] + [r['symbol'] for r in pt] + [r['symbol'] for r in hl])
    universe = list(universe)[:20] # Limit for MVP speed
    
    engine = ScoringEngine()
    stocks_data = []
    
    # 2. Score Stocks
    for symbol in universe:
        try:
            fund_data = get_stock_fundamentals(symbol)
            if not fund_data: continue
            
            # Extract Data for Analytics
            stocks_data.append({
                'symbol': symbol,
                'sector': fund_data.get('sector', 'Unknown'),
                'market_cap': fund_data.get('market_cap', 1000000000), 
                'overall_score': fund_data.get('health_score', 0),
                'pillars': fund_data.get('pillar_scores', {}),
                'risk': {
                    'debt_to_equity': fund_data.get('debt_to_equity', 0),
                    'drawdown': abs(fund_data.get('drawdown', 0)) # Ensure positive for aggregation if calc expects magnitude
                }
            })
        except Exception: 
            continue
            
    # 3. Aggregate Sectors
    sectors = calculate_sector_scores(stocks_data, mode=mode)
    
    # 4. Market Breadth (Simulated for now, can be computed from universe)
    breadth = {'advances': 1250, 'declines': 850} 
    
    return render_template('analytics.html', 
                         sectors=sectors, 
                         breadth=breadth, 
                         mode=mode,
                         active_page='analytics')

@app.route('/alerts', methods=['GET', 'POST'])
@login_required
def alerts():
    """Price Alerts page."""
    if request.method == 'POST':
        symbol = request.form.get('symbol', '').strip()
        condition = request.form.get('condition')
        target_price = request.form.get('target_price')
        
        if symbol and condition and target_price:
            create_alert(current_user.id, symbol, float(target_price), condition)
            flash(f'Alert set for {symbol} {condition} {target_price}', 'success')
        else:
            flash('Please fill in all fields.', 'danger')
        return redirect(url_for('alerts'))

    # Fetch alerts and check for triggers
    user_alerts = get_user_alerts(current_user.id)
    triggered = check_alerts(current_user.id) # Check live prices
    
    return render_template('alerts.html', alerts=user_alerts, triggered_alerts=triggered, active_page='alerts')

@app.route('/delete-alert/<int:alert_id>', methods=['POST'])
@login_required
def delete_alert_route(alert_id):
    delete_alert(alert_id, current_user.id)
    flash('Alert deleted.', 'success')
    return redirect(url_for('alerts'))

@app.route('/history')
@login_required
def history():
    conn = get_db()
    # Fetch Daily Summaries (Date + Count)
    summary = conn.execute(
        "SELECT log_date, COUNT(id) as stock_count FROM historical_log WHERE user_id = ? GROUP BY log_date ORDER BY log_date DESC",
        (current_user.id,)
    ).fetchall()
    conn.close()
    
    # Group these summaries by Month-Year (e.g., "December 2025")
    grouped_history = {}
    for day in summary:
        try:
            date_obj = datetime.strptime(day['log_date'], '%Y-%m-%d')
            key = date_obj.strftime('%B %Y') # "December 2025"
        except ValueError:
            key = "Unknown Date"
            
        if key not in grouped_history:
            grouped_history[key] = []
        grouped_history[key].append(day)
        
    return render_template('history.html', grouped_history=grouped_history, active_page='history')

@app.route('/history/search')
@login_required
def history_search():
    """Handles searching and filtering the historical log."""
    search_symbol = request.args.get('search_symbol', '').strip().upper()
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()

    conn = get_db()
    query = "SELECT * FROM historical_log WHERE user_id = ?"
    params = [current_user.id]
    
    title = "History Search Results" # Default title

    if search_symbol:
        query += " AND symbol LIKE ?"
        params.append('%' + search_symbol + '%')
        title = f"Search Results for '{search_symbol}'"
    
    if start_date and end_date:
        query += " AND log_date BETWEEN ? AND ?"
        params.extend([start_date, end_date])
        title = f"Showing History from {start_date} to {end_date}"
    elif start_date:
        query += " AND log_date >= ?"
        params.append(start_date)
        title = f"Showing History from {start_date} onwards"
    elif end_date:
        query += " AND log_date <= ?"
        params.append(end_date)
        title = f"Showing History up to {end_date}"

    query += " ORDER BY log_date DESC, symbol ASC"
    
    records = conn.execute(query, tuple(params)).fetchall()
    conn.close()
    
    return render_template('history_search.html', records=records, title=title, active_page='history')

@app.route('/confirm-result-date-upload', methods=['POST'])
@login_required
def confirm_result_date_upload():
    """Process confirmed Result Date upload"""
    conn = get_db()
    
    # Get preview data from database
    preview_row = conn.execute(
        'SELECT preview_data FROM upload_previews WHERE user_id = ? AND preview_type = ? ORDER BY created_at DESC LIMIT 1',
        (current_user.id, 'result_date')
    ).fetchone()
    
    if not preview_row:
        flash('No preview data found. Please upload again.', 'danger')
        conn.close()
        return redirect(url_for('profit_manager'))
    
    import json
    preview_data = json.loads(preview_row['preview_data'])
    
    updated_count = 0
    created_count = 0
    
    try:
        # Update existing entries
        for item in preview_data['to_update']:
            conn.execute(
                'UPDATE profit_tracker SET result_date = ? WHERE symbol = ? AND user_id = ?',
                (item['result_date'], item['symbol'], current_user.id)
            )
            updated_count += 1
        
        # Create new entries with defaults
        for item in preview_data['to_create']:
            conn.execute(
                'INSERT INTO profit_tracker (user_id, symbol, ath_profit, idx_type, result_date) VALUES (?, ?, ?, ?, ?)',
                (current_user.id, item['symbol'], 'N', 'TM', item['result_date'])
            )
            created_count += 1
        
        # Clear preview data
        conn.execute('DELETE FROM upload_previews WHERE user_id = ? AND preview_type = ?', 
                    (current_user.id, 'result_date'))
        
        conn.commit()
        flash(f'Successfully updated {updated_count} existing entries and created {created_count} new entries.', 'success')
        
    except Exception as e:
        flash(f'Error processing upload: {e}', 'danger')
    finally:
        conn.close()
    
    return redirect(url_for('profit_manager'))

def get_profit_manager_stocks():
    """Helper function to get stocks for profit manager page"""
    conn = get_db()
    search_term = request.args.get('search', '').strip().upper()
    
    if search_term:
        stocks = conn.execute(
            'SELECT * FROM profit_tracker WHERE user_id = ? AND symbol LIKE ? ORDER BY symbol ASC',
            (current_user.id, f'%{search_term}%')
        ).fetchall()
    else:
        stocks = conn.execute(
            'SELECT * FROM profit_tracker WHERE user_id = ? ORDER BY symbol ASC',
            (current_user.id,)
        ).fetchall()
    
    conn.close()
    return stocks

# --- SECTOR ANALYTICS (CHECKLIST 4) ---
from sector_analytics import SectorAnalytics

@app.route('/sectors')
@login_required
def sectors():
    """Sector ranking table and analytics dashboard."""
    weight_type = request.args.get('weight', 'market_cap')
    
    analytics = SectorAnalytics()
    
    # Get sector rankings
    rankings = analytics.get_sector_ranking(weight_type=weight_type)
    
    # Compute score momentum (week-over-week change)
    conn = get_db()
    momentum_data = {}
    for sector in rankings['sector'].unique():
        query = '''
        SELECT overall_score, date 
        FROM sector_scores 
        WHERE sector = ? AND weight_type = ?
        ORDER BY date DESC LIMIT 2
        '''
        scores = pd.read_sql_query(query, conn, params=(sector, weight_type))
        if len(scores) == 2:
            momentum_data[sector] = scores.iloc[0]['overall_score'] - scores.iloc[1]['overall_score']
        else:
            momentum_data[sector] = 0
    conn.close()
    
    rankings['momentum'] = rankings['sector'].map(momentum_data)
    
    return render_template('sectors.html', 
                         rankings=rankings.to_dict('records'),
                         weight_type=weight_type,
                         active_page='sectors')

@app.route('/api/sector-scatter-data')
@login_required
def sector_scatter_data():
    """API endpoint for scatter plot data (Score vs 12M Return)."""
    weight_type = request.args.get('weight', 'market_cap')
    
    analytics = SectorAnalytics()
    rankings = analytics.get_sector_ranking(weight_type=weight_type)
    
    # Fetch 12-month returns for each sector
    from datetime import datetime, timedelta
    import yfinance as yf
    
    scatter_data = []
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365)
    
    # Fetch Nifty 500 for relative return calculation
    nifty_data = yf.download(SectorAnalytics.NIFTY_500_TICKER, start=start_date, end=end_date, progress=False)
    nifty_return = ((nifty_data['Close'].iloc[-1] / nifty_data['Close'].iloc[0]) - 1) * 100 if not nifty_data.empty else 0
    
    for _, row in rankings.iterrows():
        sector = row['sector']
        if sector in SectorAnalytics.SECTOR_TICKERS:
            ticker = SectorAnalytics.SECTOR_TICKERS[sector]
            data = yf.download(ticker, start=start_date, end=end_date, progress=False)
            
            if not data.empty and len(data) > 1:
                sector_return = ((data['Close'].iloc[-1] / data['Close'].iloc[0]) - 1) * 100
                relative_return = sector_return - nifty_return
                
                scatter_data.append({
                    'sector': sector,
                    'score': round(row['overall_score'], 2),
                    'return': round(relative_return, 2),
                    'beta': round(row['beta'], 2) if pd.notna(row.get('beta')) else None
                })
    
    return jsonify(scatter_data)

@app.route('/sectors/<sector_name>')
@login_required
def sector_detail(sector_name):
    """Detailed sector analysis view."""
    weight_type = request.args.get('weight', 'market_cap')
    
    conn = get_db()
    
    # Get top companies in sector
    top_companies_query = '''
    SELECT 
        c.symbol,
        c.overall_score,
        c.market_cap,
        c.weight,
        c.contribution
    FROM sector_company_contributions c
    WHERE c.sector = ?
    AND c.date = (SELECT MAX(date) FROM sector_company_contributions WHERE sector = ?)
    ORDER BY c.contribution DESC
    LIMIT 10
    '''
    top_companies = pd.read_sql_query(top_companies_query, conn, params=(sector_name, sector_name))
    
    # Get score distribution
    score_dist_query = '''
    SELECT overall_score
    FROM sector_company_contributions
    WHERE sector = ?
    AND date = (SELECT MAX(date) FROM sector_company_contributions WHERE sector = ?)
    '''
    score_distribution = pd.read_sql_query(score_dist_query, conn, params=(sector_name, sector_name))
    
    # Get risk metrics
    risk_query = '''
    SELECT *
    FROM sector_risk_metrics
    WHERE sector = ?
    ORDER BY calc_date DESC
    LIMIT 1
    '''
    risk_metrics = conn.execute(risk_query, (sector_name,)).fetchone()
    
    # Get sector score
    sector_score_query = '''
    SELECT *
    FROM sector_scores
    WHERE sector = ? AND weight_type = ?
    ORDER BY date DESC
    LIMIT 1
    '''
    sector_score = conn.execute(sector_score_query, (sector_name, weight_type)).fetchone()
    
    conn.close()
    
    return render_template('sector_detail.html',
                         sector_name=sector_name,
                         top_companies=top_companies.to_dict('records'),
                         score_distribution=score_distribution['overall_score'].tolist(),
                         risk_metrics=risk_metrics,
                         sector_score=sector_score,
                         weight_type=weight_type,
                         active_page='sectors')

@app.route('/sectors/export/csv')
@login_required
def export_sectors_csv():
    """Export sector rankings as CSV."""
    weight_type = request.args.get('weight', 'market_cap')
    
    analytics = SectorAnalytics()
    rankings = analytics.get_sector_ranking(weight_type=weight_type)
    
    # Create CSV
    output = io.StringIO()
    rankings.to_csv(output, index=False)
    output.seek(0)
    
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=sector_rankings_{weight_type}.csv'}
    )

from ath_scanner import ATHScanner, SCAN_STATUS # Import Scanner Status
from scanner_engine import run_full_scan, get_profit_tracker_tickers, get_scan_results, init_scanning_results_table, promote_ath_eod
import threading 

# --- ATH SCANNER ROUTES ---
@app.route('/ath-scanner')
@login_required
def ath_scanner():
    return render_template('ath_scanner.html', active_page='ath_scanner')

@app.route('/api/ath/run-daily', methods=['POST'])
@login_required
def run_daily_scan():
    """Trigger the fast daily scan in background."""
    if SCAN_STATUS['running']:
        return jsonify({'status': 'warning', 'message': 'Scan already in progress.'})
        
    def task():
        scanner = ATHScanner()
        try:
            scanner.run_daily_scan()
        except:
            SCAN_STATUS['running'] = False
            SCAN_STATUS['message'] = "Error in background scan."
            
    thread = threading.Thread(target=task)
    thread.daemon = True
    thread.start()
    
    return jsonify({'status': 'success', 'message': 'Scan started in background.'})

@app.route('/api/ath/status', methods=['GET'])
@login_required
def get_scan_status():
    """Poll for progress updates."""
    return jsonify(SCAN_STATUS)

@app.route('/api/ath/run-refresh', methods=['POST'])
@login_required
def run_weekend_refresh():
    """Trigger the deep clean."""
    if SCAN_STATUS['running']:
        return jsonify({'status': 'warning', 'message': 'Scan/Refresh already in progress.'})
        
    scanner = ATHScanner()
    
    def task():
        try:
            scanner.run_weekend_refresh()
        except: pass
            
    thread = threading.Thread(target=task)
    thread.start()
    return jsonify({'status': 'success', 'message': 'Deep refresh started. Check terminal for progress.'})

@app.route('/api/ath/todays-results', methods=['GET'])
@login_required
def get_ath_todays_results():
    """Fetch stocks that hit ATH today from the DB (ATH Scanner API)."""
    conn = get_db()
    # Get today's date in 'YYYY-MM-DD' format
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    # Query MASTER table for any stock where ath_date = today
    # This prevents manual edits with old dates from showing up.
    rows = conn.execute('''
        SELECT symbol, ath_price, ath_date 
        FROM ath_tracking_table 
        WHERE ath_date = ? AND ignored = 0
    ''', (today_str,)).fetchall()
    
    results = []
    for r in rows:
        results.append({
            'symbol': r['symbol'],
            'new_ath': r['ath_price'],
            'prev_ath': 0.0, # Not strictly tracked in history in V2, simplified
            'date': r['ath_date'],
            'outperformance': 0.0 # simplified
        })
    conn.close()
    return jsonify(results)

@app.route('/api/ath/get-stock-details/<symbol>')
@login_required
def get_stock_details_for_modal(symbol):
    """
    Helper to fetch DB details (ATH Profit, Index Type) 
    to pre-fill the 'Add to Tracker' modal.
    """
    conn = get_db()
    row = conn.execute("SELECT ath_profit, idx_type, result_date FROM profit_tracker WHERE symbol = ?", (symbol.upper(),)).fetchone()
    conn.close()
    
    if row:
        return jsonify({
            'found': True,
            'ath_profit': row['ath_profit'],
            'idx_type': row['idx_type'],
            'result_date': row['result_date']
        })
    return jsonify({'found': False, 'ath_profit': '', 'idx_type': ''})

# --- ATH DATA MANAGEMENT APIs ---
@app.route('/api/ath/all-data', methods=['GET'])
@login_required
def get_all_ath_data():
    """Fetch all ATH records for the Management Tab."""
    conn = get_db()
    cursor = conn.execute("SELECT symbol, exchange, previous_ath, ath_date, last_updated FROM ath_tracking_table ORDER BY symbol")
    rows = cursor.fetchall()
    conn.close()

    data = []
    for row in rows:
        data.append({
            'symbol': row['symbol'],
            'exchange': row['exchange'],
            'ath_price': row['previous_ath'],
            'ath_date': row['ath_date'],
            'last_updated': row['last_updated']
        })
    return jsonify({'data': data})

@app.route('/api/ath/update-record', methods=['POST'])
@login_required
def update_ath_record():
    """Update a specific ATH record."""
    data = request.json
    symbol = data.get('symbol')
    price = data.get('ath_price')
    date = data.get('ath_date')

    if not symbol or price is None or not date:
        return jsonify({'success': False, 'error': 'Missing required fields'})

    try:
        conn = get_db()
        conn.execute('''
            UPDATE ath_tracking_table 
            SET previous_ath = ?, ath_date = ?, last_updated = ? 
            WHERE symbol = ?
        ''', (price, date, time.time(), symbol))
        conn.commit()
        conn.close()
        
        # --- AUDIT LOG ---
        with open(AUDIT_LOG, "a") as f:
            f.write(f"[{time.ctime()}] SUCCESS: Symbol {symbol} updated to {price} ({date})\n")
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/ath/bulk-update', methods=['POST'])
@login_required
def bulk_update_ath():
    """Commit multiple changes at once."""
    try:
        data = request.json
        records = data.get('records', [])
        if not records:
            return jsonify({'success': False, 'error': 'No data provided'})

        conn = get_db()
        for rec in records:
            symbol = rec.get('symbol')
            price = rec.get('ath_price')
            date = rec.get('ath_date')
            
            if symbol and price is not None and date:
                conn.execute('''
                    UPDATE ath_tracking_table 
                    SET previous_ath = ?, ath_date = ?, last_updated = ? 
                    WHERE symbol = ?
                ''', (price, date, time.time(), symbol))
        
        conn.commit()
        conn.close()
        
        # Audit Log
        with open(AUDIT_LOG, "a") as f:
            f.write(f"[{time.ctime()}] SUCCESS: Bulk update processed ({len(records)} records)\n")
            
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# --- NEW ATH SCANNER (4-Strategy) ROUTES ---

# Scanner progress state (separate from old SCAN_STATUS)
SCANNER_STATUS = {
    'running': False,
    'progress': 0,
    'total': 0,
    'message': 'Idle',
    'results': []
}

@app.route('/api/run-scanner', methods=['POST'])
@login_required
def run_new_scanner():
    """Trigger the new 4-strategy ATH scan in background (database-light)."""
    if SCANNER_STATUS['running']:
        return jsonify({'status': 'warning', 'message': 'Scanner already in progress.'})

    # Get tickers from profit_tracker for current user
    tickers = get_profit_tracker_tickers(current_user.id)
    if not tickers:
        return jsonify({'status': 'error', 'message': 'No tickers found in profit tracker.'})

    # Ensure staging table exists with new schema
    init_scanning_results_table()

    def progress_callback(progress, total, message):
        SCANNER_STATUS['progress'] = progress
        SCANNER_STATUS['total'] = total
        SCANNER_STATUS['message'] = message

    def task():
        SCANNER_STATUS['running'] = True
        SCANNER_STATUS['progress'] = 0
        SCANNER_STATUS['total'] = len(tickers)
        SCANNER_STATUS['message'] = 'Starting...'
        SCANNER_STATUS['results'] = []
        try:
            results = run_full_scan(
                tickers, 
                progress_callback=progress_callback
            )
            SCANNER_STATUS['results'] = results
            SCANNER_STATUS['message'] = f'Scan complete. {len(results)} ATH hits found.'
        except Exception as e:
            SCANNER_STATUS['message'] = f'Scan error: {str(e)}'
        finally:
            SCANNER_STATUS['running'] = False

    thread = threading.Thread(target=task)
    thread.daemon = True
    thread.start()

    return jsonify({'status': 'success', 'message': f'Scanner started for {len(tickers)} tickers.'})

@app.route('/api/eod-promote', methods=['POST'])
@login_required
def eod_promote():
    """EOD Sync: Promote today_ath -> previous_ath for specific selected ATH hits."""
    try:
        data = request.get_json() or {}
        symbols = data.get('symbols', [])
        
        if not symbols:
            return jsonify({'status': 'warning', 'message': 'No symbols provided for promotion.'})

        promoted = promote_ath_eod(symbols)
        return jsonify({'status': 'success', 'message': f'EOD Sync complete. {promoted} stocks promoted.'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'EOD Sync failed: {str(e)}'})

@app.route('/api/scanner/status', methods=['GET'])
@login_required
def get_scanner_status():
    """Poll for scanner progress updates."""
    return jsonify(SCANNER_STATUS)

@app.route('/api/scanner/results', methods=['GET'])
@login_required
def get_scanner_results():
    """Fetch current scan results from staging table."""
    results = get_scan_results()
    return jsonify(results)

@app.route('/api/dashboard/stock-count', methods=['GET'])
@login_required
def get_dashboard_stock_count():
    """Returns the number of stocks currently in the dashboard 'stocks' table."""
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
    return jsonify({'count': count})

@app.route('/api/bulk-add-portfolio', methods=['POST'])
@login_required
def bulk_add_portfolio():
    """Bulk-add selected scanner results to the dashboard (stocks table)."""
    data = request.get_json()
    stocks = data.get('stocks', [])
    
    if not stocks:
        return jsonify({'success': False, 'error': 'No stocks provided.'})

    conn = get_db()
    added = []
    duplicates = []
    errors = []
    
    try:
        for entry in stocks:
            symbol = entry.get('symbol', '').upper()
            if not symbol:
                continue
                
            # Check for duplicate
            existing = conn.execute(
                'SELECT id FROM stocks WHERE symbol = ? AND user_id = ?',
                (symbol, current_user.id)
            ).fetchone()
            
            if existing:
                duplicates.append(symbol)
                continue
            
            # Prepare values
            trigger_price = entry.get('trigger_price', 0)
            stop_loss = entry.get('stop_loss')
            ath_outperformance = entry.get('ath_outperformance', '')
            green_candle = entry.get('green_candle', '')
            close_gt_ath = entry.get('close_gt_ath', '')
            
            # Insert into stocks table (rounding = trigger_price)
            conn.execute(
                '''INSERT INTO stocks 
                   (user_id, symbol, rounding, stop_loss, ath_outperformance, is_green_candle, is_close_above_ath) 
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (
                    current_user.id, symbol,
                    float(trigger_price),
                    float(stop_loss) if stop_loss else None,
                    ath_outperformance,
                    green_candle,
                    close_gt_ath
                )
            )
            added.append(symbol)
        
        conn.commit()
    except Exception as e:
        errors.append(str(e))
    finally:
        conn.close()
    
    return jsonify({
        'success': True,
        'added': added,
        'added_count': len(added),
        'duplicates': duplicates,
        'errors': errors
    })

# --- TURTLE RESEARCH DASHBOARD (INSTITUTIONAL) ---
@app.route('/turtle-research')
@login_required
def turtle_research():
    """Redirects legacy route to new Turtle Terminal."""
    return redirect(url_for('turtle_dashboard'))


# --- INITIATE APP ---
if __name__ == '__main__':
    create_tables()
    app.run(debug=True, use_reloader=False)