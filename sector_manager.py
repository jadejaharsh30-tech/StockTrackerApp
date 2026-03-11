
import json
import os
import pandas as pd
import numpy as np

# Default "Seed" Stats for Indian Market Sectors (Approximations to ensure non-zero start)
# In production, these should be updated via update_sector_stats() scanning the universe.
DEFAULT_SECTOR_STATS = {
    "Financial Services": {"pe": 20.0, "pb": 2.5, "ev_ebitda": 15.0},
    "Technology": {"pe": 25.0, "pb": 5.0, "ev_ebitda": 18.0},
    "Consumer Cyclical": {"pe": 35.0, "pb": 6.0, "ev_ebitda": 25.0},
    "Consumer Defensive": {"pe": 40.0, "pb": 8.0, "ev_ebitda": 30.0},
    "Healthcare": {"pe": 30.0, "pb": 4.0, "ev_ebitda": 20.0},
    "Energy": {"pe": 12.0, "pb": 1.5, "ev_ebitda": 8.0},
    "Industrials": {"pe": 25.0, "pb": 3.0, "ev_ebitda": 15.0},
    "Basic Materials": {"pe": 15.0, "pb": 1.8, "ev_ebitda": 10.0},
    "Real Estate": {"pe": 30.0, "pb": 2.0, "ev_ebitda": 20.0},
    "Utilities": {"pe": 15.0, "pb": 1.5, "ev_ebitda": 10.0},
    "Communication Services": {"pe": 20.0, "pb": 3.0, "ev_ebitda": 12.0},
    "Unknown": {"pe": 20.0, "pb": 3.0, "ev_ebitda": 15.0} # Fallback
}

CACHE_FILE = "sector_stats.json"

class SectorManager:
    def __init__(self):
        self.stats = self.load_stats()

    def load_stats(self):
        """Loads stats from cache or uses defaults."""
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, 'r') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error loading sector stats: {e}")
                return DEFAULT_SECTOR_STATS
        return DEFAULT_SECTOR_STATS

    def save_stats(self):
        """Saves current stats to cache."""
        try:
            with open(CACHE_FILE, 'w') as f:
                json.dump(self.stats, f, indent=4)
        except Exception as e:
            print(f"Error saving sector stats: {e}")

    def get_sector_defaults(self, sector):
        """Returns median stats for a given sector."""
        # Fuzzy match or direct key
        if sector in self.stats:
            return self.stats[sector]
        
        # Try to find a partial match
        for key in self.stats:
            if key in sector or sector in key:
                return self.stats[key]
        
        return self.stats["Unknown"]

    def calculate_percentile(self, value, metric, sector):
        """
        Calculates the percentile rank of a stock's metric against its sector.
        Lower is usually Cheaper (Better) for PE, PB, EV/EBITDA.
        Returns a score 0-100 where 100 = Cheapest (Low percentile).
        """
        if value is None or value == 0: return 50
        
        medians = self.get_sector_defaults(sector)
        median_val = medians.get(metric, 20.0)
        
        if median_val == 0: return 50

        # Simple relative ratio logic if full distribution isn't available
        # If Stock PE = 10, Sector PE = 20 -> Ratio 0.5 (Cheap) -> High Score
        # If Stock PE = 40, Sector PE = 20 -> Ratio 2.0 (Expensive) -> Low Score
        
        # We will map ratio to a percentile-like score
        # Ratio < 0.5 -> Top Decile (Cheap) -> Score ~90-100
        # Ratio 1.0 -> Median -> Score 50
        # Ratio > 2.0 -> Bottom Decile (Expensive) -> Score ~0-10
        
        ratio = value / median_val
        
        # Logarithmic decay for scoring
        # 0.5 -> 80
        # 1.0 -> 50
        # 1.5 -> 20
        # 2.0 -> 0
        
        if ratio <= 0.5: return 100
        if ratio >= 2.0: return 0
        
        # Linear interpolation between 0.5 and 2.0
        # Slope = (0 - 100) / (2.0 - 0.5) = -100 / 1.5 = -66.66
        # Score = 100 + (ratio - 0.5) * -66.66
        
        score = 100 + ((ratio - 0.5) * -66.66)
        return max(0, min(100, score))
