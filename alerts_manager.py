import sqlite3
import os

_DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tracker.db')

def _get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn

def get_user_alerts(user_id, db_path=None):
    """Fetch all active alerts for a user."""
    db_path = db_path or _DEFAULT_DB
    conn = _get_conn(db_path)
    alerts = conn.execute('SELECT * FROM alerts WHERE user_id = ? AND is_active = 1 ORDER BY created_at DESC', (user_id,)).fetchall()
    conn.close()
    return alerts

def create_alert(user_id, symbol, target_price, condition, db_path=None):
    """Create a new price alert."""
    db_path = db_path or _DEFAULT_DB
    conn = _get_conn(db_path)
    conn.execute('''
        INSERT INTO alerts (user_id, symbol, target_price, condition)
        VALUES (?, ?, ?, ?)
    ''', (user_id, symbol.upper(), target_price, condition))
    conn.commit()
    conn.close()

def delete_alert(alert_id, user_id, db_path=None):
    """Delete (deactivate) an alert."""
    db_path = db_path or _DEFAULT_DB
    conn = _get_conn(db_path)
    # verifying ownership
    conn.execute('DELETE FROM alerts WHERE id = ? AND user_id = ?', (alert_id, user_id))
    conn.commit()
    conn.close()

def check_alerts(user_id, db_path=None):
    """
    Checks if any active alerts have been triggered based on current prices.
    Returns a list of triggered alert messages.
    """
    import yfinance as yf
    
    alerts = get_user_alerts(user_id, db_path)
    if not alerts:
        return []
        
    triggered = []
    
    # Optimization: Batch fetch prices? For now simple loop.
    for alert in alerts:
        symbol = alert['symbol']
        target = alert['target_price']
        condition = alert['condition']
        
        try:
            # We assume .NS for simplicity as per previous context
            ticker_name = symbol if symbol.endswith('.NS') else f"{symbol}.NS"
            ticker = yf.Ticker(ticker_name)
            current_price = ticker.fast_info.last_price
            
            if not current_price: continue
            
            is_triggered = False
            if condition == 'ABOVE' and current_price >= target:
                is_triggered = True
            elif condition == 'BELOW' and current_price <= target:
                is_triggered = True
                
            if is_triggered:
                triggered.append({
                    'id': alert['id'],
                    'symbol': symbol,
                    'price': current_price,
                    'target': target,
                    'condition': condition,
                    'message': f"🔔 Alert: {symbol} is {condition} {target} (Current: {round(current_price, 2)})"
                })
        except:
            continue
            
    return triggered
