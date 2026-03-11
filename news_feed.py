import yfinance as yf
from datetime import datetime
import concurrent.futures

def get_market_news(symbols=None, limit=12):
    """
    Fetches news using yfinance.
    If symbols provided, searches for them. Otherwise gets general market news (Nifty 50).
    """
    
    def fetch_ticker_news(ticker_sym):
        try:
            # Append .NS if not present, assuming NSE for India context
            # But symbols might already have it or not. The app seems to store them without .NS usually.
            # Safety check.
            ticker_name = ticker_sym if ticker_sym.endswith('.NS') or ticker_sym.startswith('^') else f"{ticker_sym}.NS"
            ticker = yf.Ticker(ticker_name)
            return ticker.news
        except Exception:
            return []

    try:
        raw_news = []
        if symbols and len(symbols) > 0:
            # Parallel fetch for specific symbols
            # Limit symbols to check to avoid spamming usage
            targets = symbols[:8] # Check news for up to 8 tickers
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                results = list(executor.map(fetch_ticker_news, targets))
                for res in results:
                    if res:
                        raw_news.extend(res)
        else:
            # General Market News (Nifty 50)
            raw_news = fetch_ticker_news("^NSEI")

        # Parse and Normalize
        processed_news = []
        seen_links = set()
        
        for item in raw_news:
            try:
                # Handle YFinance News Structure (New vs Old)
                content = item.get('content', {})
                
                title = item.get('title') or content.get('title')
                
                # Try to get a link
                link = item.get('link') 
                if not link:
                    click_through = content.get('clickThroughUrl')
                    if click_through:
                        link = click_through.get('url')
                    else:
                        link = content.get('canonicalUrl', {}).get('url')
                
                # Try to get publish time
                pub_date = item.get('providerPublishTime') or content.get('pubDate')
                # If timestamp (int), convert to string
                if isinstance(pub_date, (int, float)):
                    pub_date = datetime.fromtimestamp(pub_date).strftime('%Y-%m-%d %H:%M')
                
                source = item.get('publisher') or content.get('provider', {}).get('displayName') or "Yahoo Finance"
                
                if title and link and link not in seen_links:
                    processed_news.append({
                        'title': title,
                        'link': link,
                        'source': source,
                        'published': pub_date or '',
                    })
                    seen_links.add(link)
            except Exception:
                continue

        # Valid items only
        processed_news = [n for n in processed_news if n['title'] and n['link']]

        # Limit results
        return processed_news[:limit]

    except Exception as e:
        print(f"Error fetching news: {e}")
        return []
