# Dashboard Widgets Implementation Guide

## Overview
This document explains how the dashboard widgets work and how to integrate real data from your database.

## Current Widgets Implemented

### 1. Total Portfolio Value Widget
- **Data Source**: `calculate_total_portfolio_value(user_id)` function
- **Real Data**: Sums all holdings across all portfolios with live prices
- **Display**: Total value, change percentage, holdings count
- **Location**: Top of dashboard

### 2. Best Performer Widget
- **Data Source**: `get_best_performer(user_id)` function
- **Real Data**: Finds stock with highest daily change from portfolios
- **Display**: Stock symbol, change percentage
- **Location**: Dashboard grid

### 3. Upcoming Results Widget
- **Data Source**: `get_upcoming_results_count(user_id)` function
- **Real Data**: Counts stocks with future result dates
- **Display**: Count, next stock and date
- **Location**: Dashboard grid

### 4. Active Stocks Widget
- **Data Source**: `stocks|length` (Jinja2 filter)
- **Real Data**: Count of stocks in daily tracker
- **Display**: Number of active stocks
- **Location**: Dashboard grid

## How to Add New Widgets

### Step 1: Create Backend Function
```python
def get_widget_data(user_id):
    """Calculate data for your widget"""
    conn = get_db()
    # Your calculation logic here
    result = {
        'value': calculated_value,
        'change': change_percentage,
        'details': additional_info
    }
    conn.close()
    return result
```

### Step 2: Update Dashboard Route
```python
@app.route('/dashboard')
@login_required
def dashboard():
    # ... existing code ...
    
    # Add your new widget data
    widget_data = get_widget_data(current_user.id)
    
    return render_template('dashboard.html', 
                         stocks=processed_stocks, 
                         active_page='dashboard', 
                         total_portfolio_data=total_portfolio_data,
                         best_performer=best_performer,
                         upcoming_results=upcoming_results,
                         widget_data=widget_data)  # Add this
```

### Step 3: Add Widget to Template
```html
<div class="card" style="display: flex; align-items: center; gap: 1em;">
    <div style="font-size: 2em; color: var(--info-color);">📊</div>
    <div>
        <div style="font-size: 1em; color: var(--secondary-color); font-weight: 600;">Widget Title</div>
        <div style="font-size: 1.5em; font-weight: 700; color: var(--info-color);">
            {{ widget_data.value }}
        </div>
        <div style="font-size: 0.9em; color: var(--secondary-color);">
            {{ widget_data.details }}
        </div>
    </div>
</div>
```

## Suggested Additional Widgets

### 1. Portfolio Allocation Chart
- **Data**: Percentage allocation by stock/sector
- **Visual**: Pie chart or horizontal bars
- **Function**: `get_portfolio_allocation(user_id)`

### 2. Daily P&L
- **Data**: Today's profit/loss across all portfolios
- **Function**: `calculate_daily_pnl(user_id)`

### 3. Risk Metrics
- **Data**: Stop loss breaches, risk percentage
- **Function**: `calculate_risk_metrics(user_id)`

### 4. Market Sentiment
- **Data**: GO vs WAIT stocks ratio
- **Function**: `get_market_sentiment(user_id)`

### 5. Top Gainers/Losers
- **Data**: Top 3 performing stocks
- **Function**: `get_top_performers(user_id, limit=3)`

## Integration with Existing Data

### Portfolio Data Structure
```sql
-- portfolios table
id, user_id, name

-- holdings table  
id, portfolio_id, symbol, exit_price, allocation
```

### Profit Tracker Data Structure
```sql
-- profit_tracker table
id, user_id, symbol, ath_profit, idx_type, result_date
```

### Live Price Integration
```python
def get_holding_details(symbol, user_id):
    # Gets live price from yfinance
    # Returns: cmp, change_pct, result_date
```

## Dark Mode Support
All widgets automatically support dark mode through CSS variables:
- `var(--primary-color)`
- `var(--secondary-color)`
- `var(--success-color)`
- `var(--danger-color)`
- `var(--warning-color)`
- `var(--info-color)`

## Performance Considerations
- Widget calculations use live price data (yfinance)
- Consider caching for production use
- Database queries are optimized with proper indexing
- Widgets load asynchronously if needed

## Future Enhancements
1. **Charts**: Add Chart.js or D3.js for visualizations
2. **Real-time Updates**: WebSocket for live price updates
3. **Customization**: User-configurable widget layout
4. **Export**: Widget data export functionality
5. **Mobile**: Responsive widget layouts

## Troubleshooting
- **No Data**: Check if user has portfolios/holdings
- **Price Errors**: Verify yfinance connectivity
- **Performance**: Monitor database query efficiency
- **Styling**: Ensure CSS variables are defined 