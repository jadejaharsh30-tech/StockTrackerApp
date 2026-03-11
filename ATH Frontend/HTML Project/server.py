from flask import Flask, render_template, jsonify
import pandas as pd
import os
import sys
import io
from datetime import datetime

# Import your existing foundation script
import master_ath_manager as backend

app = Flask(__name__)

DATABASE_FILENAME = "ATH_Results.xlsx"
DAILY_REPORT_PREFIX = "ATH_Report_"

def get_db_stats():
    """Reads the Excel file to get current stats for the dashboard."""
    if os.path.exists(DATABASE_FILENAME):
        df = pd.read_excel(DATABASE_FILENAME)
        total = len(df)
        nse = len(df[df['Exchange Found'].str.contains('NSE', na=False)])
        bse = len(df[df['Exchange Found'].str.contains('BSE', na=False)])
        last_update = df['ATH Date'].max() if 'ATH Date' in df.columns else "-"
        return {"exists": True, "total": total, "nse": nse, "bse": bse, "last_update": str(last_update)}
    return {"exists": False}

@app.route('/')
def index():
    """Serves the HTML page."""
    stats = get_db_stats()
    return render_template('index.html', stats=stats)

@app.route('/run-daily')
def run_daily():
    """Triggers the Daily Update logic and captures logs."""
    if not os.path.exists(DATABASE_FILENAME):
        return jsonify({"status": "error", "log": "Database not found!"})

    # Capture standard output (print statements) to show in the web UI
    capture = io.StringIO()
    sys.stdout = capture
    
    try:
        df_db = pd.read_excel(DATABASE_FILENAME)
        backend.run_daily_update(df_db)
        
        # Check if a report was generated today
        today_str = datetime.now().strftime('%Y-%m-%d')
        report_file = f"{DAILY_REPORT_PREFIX}{today_str}.xlsx"
        new_ath_data = []
        
        if os.path.exists(report_file):
            report_df = pd.read_excel(report_file)
            new_ath_data = report_df.to_dict(orient='records')
            
        log_output = capture.getvalue()
        return jsonify({"status": "success", "log": log_output, "new_aths": new_ath_data})
        
    except Exception as e:
        return jsonify({"status": "error", "log": str(e)})
    finally:
        sys.stdout = sys.__stdout__ # Reset print to normal

@app.route('/run-weekly')
def run_weekly():
    """Triggers the Weekend Refresh."""
    capture = io.StringIO()
    sys.stdout = capture
    
    try:
        if not os.path.exists(DATABASE_FILENAME):
            return jsonify({"status": "error", "log": "Database not found!"})
            
        df_db = pd.read_excel(DATABASE_FILENAME)
        backend.run_weekly_refresh(df_db)
        
        log_output = capture.getvalue()
        return jsonify({"status": "success", "log": log_output})
        
    except Exception as e:
        return jsonify({"status": "error", "log": str(e)})
    finally:
        sys.stdout = sys.__stdout__

if __name__ == '__main__':
    app.run(debug=True, port=5000)