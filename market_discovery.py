"""
Market Discovery for Polymarket 15-Minute Cycles.
Automatically finds BTC/ETH 15-minute up/down binary options markets.
"""

import asyncio
import aiohttp
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List

logger = logging.getLogger(__name__)

# Polymarket API endpoints
GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"


@dataclass
class DiscoveredMarket:
    """A discovered 15-minute cycle market."""
    condition_id: str
    up_token_id: str      # "Up" outcome token (equivalent to YES for price going up)
    down_token_id: str    # "Down" outcome token (equivalent to NO for price going up)
    question: str
    asset: str            # "BTC" or "ETH"
    slug: str
    expiry_timestamp: float
    start_timestamp: float
    window_start_time: str   # Human readable, e.g., "3:30AM ET"
    window_end_time: str     # Human readable, e.g., "3:45AM ET"
    
    # Aliases for compatibility with bot code (UP = YES on price going up)
    @property
    def yes_token_id(self) -> str:
        return self.up_token_id
    
    @property
    def no_token_id(self) -> str:
        return self.down_token_id
    
    @property
    def strike_price(self) -> float:
        """For up/down markets, strike is the price at window start. Return 0 as placeholder."""
        return 0.0
    
    @property
    def time_to_expiry(self) -> float:
        """Seconds until expiry."""
        return self.expiry_timestamp - datetime.now(timezone.utc).timestamp()
    
    @property
    def time_to_start(self) -> float:
        """Seconds until market window starts."""
        return self.start_timestamp - datetime.now(timezone.utc).timestamp()
    
    @property
    def is_active(self) -> bool:
        """Market window has started but not expired."""
        now = datetime.now(timezone.utc).timestamp()
        return self.start_timestamp <= now < self.expiry_timestamp
    
    @property
    def is_upcoming(self) -> bool:
        """Market window hasn't started yet."""
        return self.time_to_start > 0
    
    def __str__(self) -> str:
        status = "🟢 ACTIVE" if self.is_active else ("🟡 UPCOMING" if self.is_upcoming else "⚫ EXPIRED")
        mins_left = self.time_to_expiry / 60
        return (
            f"{self.asset} 15m | {self.window_start_time}-{self.window_end_time} | "
            f"{mins_left:.1f}min left | {status}"
        )


class MarketDiscovery:
    """
    Discovers Polymarket 15-minute up/down cycle markets for BTC and ETH.
    
    These markets resolve based on whether the asset price at the end of a
    15-minute window is higher ("Up") or lower ("Down") than the start.
    """
    
    INTERVAL = 15 * 60  # 15 minutes in seconds
    
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict[str, DiscoveredMarket] = {}  # slug -> market
        self._cache_time: Optional[float] = None
        self._cache_ttl = 30  # Refresh every 30 seconds
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def close(self) -> None:
        """Close HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
    
    def _generate_window_timestamps(self, count: int = 10) -> List[int]:
        """
        Generate timestamps for current and upcoming 15-minute windows.
        
        Args:
            count: Number of windows to generate
            
        Returns:
            List of unix timestamps aligned to 15-minute boundaries
        """
        now = int(time.time())
        current_window = (now // self.INTERVAL) * self.INTERVAL
        
        return [current_window + (i * self.INTERVAL) for i in range(count)]
    
    async def _fetch_event(self, slug: str) -> Optional[dict]:
        """Fetch a specific event by slug."""
        session = await self._get_session()
        
        try:
            url = f"{GAMMA_API_URL}/events"
            params = {"slug": slug}
            
            async with session.get(url, params=params, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        return data[0]
        except asyncio.TimeoutError:
            logger.debug(f"Timeout fetching {slug}")
        except Exception as e:
            logger.debug(f"Error fetching {slug}: {e}")
        
        return None
    
    def _parse_event(self, event: dict, asset: str) -> Optional[DiscoveredMarket]:
        """Parse an event into a DiscoveredMarket."""
        try:
            markets = event.get("markets", [])
            if not markets:
                return None
            
            market = markets[0]
            
            # Get token IDs - format is [Up token, Down token]
            # Note: clobTokenIds comes as a JSON string, not a list!
            token_ids_raw = market.get("clobTokenIds", "[]")
            if isinstance(token_ids_raw, str):
                import json
                token_ids = json.loads(token_ids_raw)
            else:
                token_ids = token_ids_raw
            
            if len(token_ids) < 2:
                return None
            
            # Parse end date
            end_date = event.get("endDate") or market.get("endDate")
            if not end_date:
                return None
            
            if isinstance(end_date, str):
                expiry_ts = datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp()
            else:
                expiry_ts = float(end_date)
            
            # Start is 15 minutes before end
            start_ts = expiry_ts - self.INTERVAL
            
            # Parse time from title (e.g., "Bitcoin Up or Down - December 15, 3:30AM-3:45AM ET")
            title = event.get("title", "")
            window_start = ""
            window_end = ""
            
            if "-" in title:
                parts = title.split("-")
                if len(parts) >= 2:
                    time_part = parts[-1].strip()
                    if "AM" in time_part or "PM" in time_part:
                        time_range = time_part.replace(" ET", "")
                        if "-" in parts[-2]:
                            time_range = parts[-2].split("-")[-1].strip() + "-" + time_range
                        if "-" in time_range:
                            times = time_range.split("-")
                            window_start = times[0].strip()
                            window_end = times[1].strip() if len(times) > 1 else ""
            
            return DiscoveredMarket(
                condition_id=market.get("conditionId", ""),
                up_token_id=token_ids[0],
                down_token_id=token_ids[1],
                question=market.get("question", ""),
                asset=asset.upper(),
                slug=event.get("slug", ""),
                expiry_timestamp=expiry_ts,
                start_timestamp=start_ts,
                window_start_time=window_start,
                window_end_time=window_end,
            )
            
        except Exception as e:
            logger.debug(f"Error parsing event: {e}")
            return None
    
    async def discover(
        self,
        assets: List[str] = None,
        windows: int = 10,
        include_current: bool = True,
    ) -> List[DiscoveredMarket]:
        """
        Discover available 15-minute cycle markets.
        
        Args:
            assets: List of assets to find ("BTC", "ETH"). Default: both.
            windows: Number of 15-minute windows to search (default: 10 = 2.5 hours)
            include_current: Include the currently active window
            
        Returns:
            List of discovered markets, sorted by expiry time.
        """
        if assets is None:
            assets = ["BTC", "ETH"]
        
        assets = [a.lower() for a in assets]
        
        # Check cache freshness
        now = time.time()
        if self._cache_time and (now - self._cache_time) < self._cache_ttl:
            # Return cached results filtered by asset
            cached = [m for m in self._cache.values() 
                     if m.asset.lower() in assets and m.time_to_expiry > 0]
            if cached:
                return sorted(cached, key=lambda m: m.expiry_timestamp)
        
        # Generate window timestamps
        timestamps = self._generate_window_timestamps(windows)
        
        # Fetch events concurrently
        tasks = []
        for ts in timestamps:
            for asset in assets:
                slug = f"{asset}-updown-15m-{ts}"
                if slug not in self._cache or self._cache[slug].time_to_expiry < 0:
                    tasks.append(self._fetch_and_parse(slug, asset))
        
        if tasks:
            logger.info(f"Fetching {len(tasks)} 15-minute markets...")
            results = await asyncio.gather(*tasks)
            
            for market in results:
                if market:
                    self._cache[market.slug] = market
        
        self._cache_time = now
        
        # Filter and sort
        markets = [m for m in self._cache.values() 
                  if m.asset.lower() in assets and m.time_to_expiry > 0]
        
        if not include_current:
            markets = [m for m in markets if m.is_upcoming]
        
        markets.sort(key=lambda m: m.expiry_timestamp)
        
        logger.info(f"Discovered {len(markets)} active 15-minute markets")
        
        return markets
    
    async def _fetch_and_parse(self, slug: str, asset: str) -> Optional[DiscoveredMarket]:
        """Fetch and parse a single event."""
        event = await self._fetch_event(slug)
        if event:
            return self._parse_event(event, asset)
        return None
    
    async def find_next_market(
        self,
        asset: str = "BTC",
        min_time_remaining: float = 120,  # At least 2 minutes left
    ) -> Optional[DiscoveredMarket]:
        """
        Find the next available market for trading.
        
        Args:
            asset: "BTC" or "ETH"
            min_time_remaining: Minimum seconds until expiry
            
        Returns:
            The next market to trade, or None if none available.
        """
        markets = await self.discover(assets=[asset])
        
        for market in markets:
            if market.time_to_expiry >= min_time_remaining:
                return market
        
        return None
    
    async def get_current_btc_market(self) -> Optional[DiscoveredMarket]:
        """Get the currently active BTC 15-minute market."""
        markets = await self.discover(assets=["BTC"])
        for m in markets:
            if m.is_active:
                return m
        return markets[0] if markets else None
    
    async def get_current_eth_market(self) -> Optional[DiscoveredMarket]:
        """Get the currently active ETH 15-minute market."""
        markets = await self.discover(assets=["ETH"])
        for m in markets:
            if m.is_active:
                return m
        return markets[0] if markets else None


async def discover_markets_cli():
    """CLI helper to discover and display available markets."""
    discovery = MarketDiscovery()
    
    try:
        print("\n🔍 Discovering 15-minute BTC/ETH cycle markets...\n")
        
        markets = await discovery.discover(windows=8)  # Next 2 hours
        
        if not markets:
            print("❌ No 15-minute markets found.")
            print("   Check your internet connection or try again.")
            return
        
        print(f"Found {len(markets)} markets:\n")
        
        btc_markets = [m for m in markets if m.asset == "BTC"]
        eth_markets = [m for m in markets if m.asset == "ETH"]
        
        print("📈 BTC Markets:")
        for market in btc_markets[:5]:
            print(f"   {market}")
        
        print("\n📈 ETH Markets:")
        for market in eth_markets[:5]:
            print(f"   {market}")
        
        # Show recommended
        print("\n" + "=" * 50)
        btc = await discovery.find_next_market("BTC", min_time_remaining=180)
        eth = await discovery.find_next_market("ETH", min_time_remaining=180)
        
        print("🎯 Recommended (>3 min remaining):")
        if btc:
            print(f"   BTC: {btc.slug}")
            print(f"        Condition: {btc.condition_id[:40]}...")
            print(f"        Up Token:  {btc.up_token_id[:40]}...")
            print(f"        Down Token: {btc.down_token_id[:40]}...")
        else:
            print("   BTC: None with >3 min remaining")
            
        if eth:
            print(f"   ETH: {eth.slug}")
            print(f"        Condition: {eth.condition_id[:40]}...")
        else:
            print("   ETH: None with >3 min remaining")
        
    finally:
        await discovery.close()


if __name__ == "__main__":
    asyncio.run(discover_markets_cli())
