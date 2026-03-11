import streamlit as st
import pandas as pd
import os
from datetime import datetime
import sys
from io import StringIO
import contextlib

# Import your existing backend logic
# Ensure master_ath_manager.py is in the same folder
import master_ath_manager as backend

# ================= CONFIGURATION =================
PAGE_TITLE = "EquiTrend | ATH Monitor"
DATABASE_FILENAME = "ATH_Results.xlsx"
DAILY_REPORT_PREFIX = "ATH_Report_"
# =================================================

# Page Setup
st.set_page_config(page_title=PAGE_TITLE, layout="wide", page_icon="📈")

# Custom CSS for a professional look
st.markdown("""
    <style>
    .stButton>button {
        width: 100%;
        border-radius: 5px;
        height: 3em;
    }
    .main-header {
        font-size: 2.5rem; 
        font-weight: 700; 
        color: #1E1E1E;
        margin-bottom: 0px;
    }
    .sub-header {
        font-size: 1.2rem;
        color: #555;
        margin-bottom: 20px;
    }
    </style>
""", unsafe_allow_html=True)

# Helper to capture standard output (Logs)
@contextlib.contextmanager
def capture_output():
    new_out = StringIO()
    old_out = sys.stdout
    sys.stdout = new_out
    try:
        yield new_out
    finally:
        sys.stdout = old_out

def load_database():
    if os.path.exists(DATABASE_FILENAME):
        return pd.read_excel(DATABASE_FILENAME)
    return None

def main():
    # --- Sidebar ---
    st.sidebar.title("🚀 Controls")
    st.sidebar.markdown("---")
    
    mode = st.sidebar.radio("Select Operation Mode:", ["Dashboard", "Run Scanners"])
    
    st.sidebar.markdown("### 📂 Database Status")
    if os.path.exists(DATABASE_FILENAME):
        df_db = pd.read_excel(DATABASE_FILENAME)
        st.sidebar.success("Database Connected")
        st.sidebar.metric("Total Stocks", len(df_db))
        
        # Download Button for Main DB
        with open(DATABASE_FILENAME, "rb") as file:
            st.sidebar.download_button(
                label="Download Master DB",
                data=file,
                file_name=DATABASE_FILENAME,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
    else:
        st.sidebar.error("Database Not Found")

    # --- Main Content ---
    st.markdown(f'<div class="main-header">{PAGE_TITLE}</div>', unsafe_allow_html=True)
    st.markdown('<div class="sub-header">Automated All-Time High Tracking System</div>', unsafe_allow_html=True)
    
    if mode == "Dashboard":
        display_dashboard()
    elif mode == "Run Scanners":
        display_scanner_controls()

def display_dashboard():
    df = load_database()
    if df is not None:
        col1, col2, col3 = st.columns(3)
        
        # Calculate Stats
        try:
            latest_date = pd.to_datetime(df['ATH Date']).max().strftime('%Y-%m-%d')
        except:
            latest_date = "-"
            
        with col1:
            st.info(f"**Last Data Update:** {latest_date}")
        with col2:
            st.info(f"**NSE Stocks:** {len(df[df['Exchange Found'].str.contains('NSE', na=False)])}")
        with col3:
            st.info(f"**BSE Stocks:** {len(df[df['Exchange Found'].str.contains('BSE', na=False)])}")

        st.markdown("### 📊 Current Database Preview")
        st.dataframe(df.sort_values(by='ATH Date', ascending=False).head(50), use_container_width=True)
    else:
        st.warning(f"Please place '{DATABASE_FILENAME}' in the folder to see statistics.")

def display_scanner_controls():
    st.markdown("### ⚡ Scanner Operations")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("#### 🟢 Daily Scan (Fast)")
        st.caption("Checks today's prices against stored ATH. Handles recent splits.")
        run_daily = st.button("Run Daily Scan", type="primary")

    with col2:
        st.markdown("#### 🔴 Full Refresh (Deep Clean)")
        st.caption("Re-downloads full history for ALL stocks. Fixes data drift.")
        run_weekly = st.button("Run Weekend Refresh")

    # --- Logic Execution ---
    log_output = st.empty()
    
    if run_daily or run_weekly:
        df_db = load_database()
        if df_db is None:
            st.error("Database not found. Cannot run scan.")
            return

        status_text = "Running Daily Scan..." if run_daily else "Running Full Refresh (This takes time)..."
        
        with st.status(status_text, expanded=True) as status:
            # We capture the printed logs from your master script to show them in UI
            with capture_output() as output:
                try:
                    if run_daily:
                        backend.run_daily_update(df_db)
                    else:
                        backend.run_weekly_refresh(df_db)
                    
                    status.update(label="Process Complete! ✅", state="complete", expanded=False)
                except Exception as e:
                    st.error(f"An error occurred: {e}")
                    status.update(label="Failed ❌", state="error")
            
            # Show logs
            st.code(output.getvalue())

        # --- Post Run: Check for Results ---
        if run_daily:
            today_str = datetime.now().strftime('%Y-%m-%d')
            report_file = f"{DAILY_REPORT_PREFIX}{today_str}.xlsx"
            
            if os.path.exists(report_file):
                st.success(f"🚀 Scan Finished! Found New ATHs today.")
                
                # Load and Display the Daily Report
                daily_df = pd.read_excel(report_file)
                st.markdown("### 🏆 Today's New ATH List")
                st.dataframe(daily_df, use_container_width=True)
                
            else:
                st.info("Scan Finished. No new All-Time Highs detected today.")

if __name__ == "__main__":
    main()