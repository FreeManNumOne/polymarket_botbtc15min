"""
Market Data Feeds.
Provides real-time price data from:
- Binance WebSocket for BTC/ETH spot prices
- Polymarket order book snapshots
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional, Callable, Dict
from dataclasses import dataclass

import websockets

logger = logging.getLogger(__name__)


@dataclass
class PriceUpdate:
    """A price tick from a market data source."""
    symbol: str
    price: float
    timestamp: datetime
    source: str


# Callback type: (price_update) -> None
PriceCallback = Callable[[PriceUpdate], None]


class BinanceWebSocket:
    """
    WebSocket connection to Binance for real-time BTC/ETH prices.
    
    Uses the Binance Spot WebSocket API:
    wss://stream.binance.com:9443/ws/<symbol>@trade
    """
    
    BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"
    
    def __init__(self, symbols: list[str] = None):
        """
        Args:
            symbols: List of symbols to subscribe to, e.g., ["btcusdt", "ethusdt"]
        """
        self.symbols = [s.lower() for s in (symbols or ["btcusdt"])]
        self._prices: Dict[str, float] = {}
        self._last_update: Dict[str, datetime] = {}
        self._callbacks: list[PriceCallback] = []
        self._ws = None
        self._running = False
    
    def add_callback(self, callback: PriceCallback) -> None:
        """Add a callback to be invoked on each price update."""
        self._callbacks.append(callback)
    
    def get_price(self, symbol: str = "btcusdt") -> Optional[float]:
        """Get the latest price for a symbol."""
        return self._prices.get(symbol.lower())
    
    def get_last_update(self, symbol: str = "btcusdt") -> Optional[datetime]:
        """Get timestamp of last price update."""
        return self._last_update.get(symbol.lower())
    
    async def connect(self) -> None:
        """Connect to Binance WebSocket and start streaming."""
        # Build stream URL for multiple symbols
        streams = "/".join([f"{s}@trade" for s in self.symbols])
        url = f"{self.BINANCE_WS_URL}/{streams}"
        
        logger.info(f"Connecting to Binance WebSocket: {self.symbols}")
        
        self._running = True
        retry_count = 0
        max_retries = 5
        
        while self._running:
            try:
                async with websockets.connect(url) as ws:
                    self._ws = ws
                    retry_count = 0
                    logger.info("Binance WebSocket connected")
                    
                    async for message in ws:
                        await self._handle_message(message)
                        
            except websockets.ConnectionClosed as e:
                logger.warning(f"WebSocket closed: {e}")
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
            
            if self._running:
                retry_count += 1
                if retry_count > max_retries:
                    logger.error("Max retries exceeded, stopping")
                    break
                    
                wait_time = min(30, 2 ** retry_count)
                logger.info(f"Reconnecting in {wait_time}s...")
                await asyncio.sleep(wait_time)
    
    async def _handle_message(self, raw_message: str) -> None:
        """Parse and process incoming WebSocket message."""
        try:
            data = json.loads(raw_message)
            
            # Binance trade message format
            symbol = data.get("s", "").lower()
            price = float(data.get("p", 0))
            trade_time = data.get("T", 0)
            
            if symbol and price > 0:
                self._prices[symbol] = price
                self._last_update[symbol] = datetime.fromtimestamp(trade_time / 1000)
                
                update = PriceUpdate(
                    symbol=symbol,
                    price=price,
                    timestamp=self._last_update[symbol],
                    source="binance",
                )
                
                # Notify callbacks
                for callback in self._callbacks:
                    try:
                        callback(update)
                    except Exception as e:
                        logger.error(f"Callback error: {e}")
                        
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse message: {e}")
        except Exception as e:
            logger.error(f"Error handling message: {e}")
    
    async def disconnect(self) -> None:
        """Disconnect from WebSocket."""
        self._running = False
        if self._ws:
            await self._ws.close()
            logger.info("Binance WebSocket disconnected")


class CoinGeckoPriceFeed:
    """
    Fallback price feed using CoinGecko REST API.
    Used when Binance WebSocket is blocked (HTTP 451 in US).
    """
    
    COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
    
    def __init__(self, symbols: list[str] = None):
        self.symbols = symbols or ["btcusdt"]
        self._prices: Dict[str, float] = {}
        self._callbacks: list[PriceCallback] = []
        self._running = False
        self._session = None
    
    def add_callback(self, callback: PriceCallback) -> None:
        self._callbacks.append(callback)
    
    def get_price(self, symbol: str = "btcusdt") -> Optional[float]:
        return self._prices.get(symbol.lower())
    
    async def _fetch_prices(self) -> None:
        """Fetch prices from CoinGecko API."""
        try:
            if self._session is None:
                import aiohttp
                self._session = aiohttp.ClientSession()
            
            # Map symbols to CoinGecko IDs
            coin_ids = []
            if any("btc" in s.lower() for s in self.symbols):
                coin_ids.append("bitcoin")
            if any("eth" in s.lower() for s in self.symbols):
                coin_ids.append("ethereum")
            
            if not coin_ids:
                return
            
            params = {
                "ids": ",".join(coin_ids),
                "vs_currencies": "usd"
            }
            
            async with self._session.get(self.COINGECKO_URL, params=params, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    if "bitcoin" in data:
                        price = data["bitcoin"]["usd"]
                        self._prices["btcusdt"] = price
                        self._notify("btcusdt", price)
                    
                    if "ethereum" in data:
                        price = data["ethereum"]["usd"]
                        self._prices["ethusdt"] = price
                        self._notify("ethusdt", price)
                        
        except Exception as e:
            logger.debug(f"CoinGecko fetch error: {e}")
    
    def _notify(self, symbol: str, price: float) -> None:
        """Notify callbacks of price update."""
        update = PriceUpdate(
            symbol=symbol,
            price=price,
            timestamp=datetime.now(),
            source="coingecko",
        )
        for callback in self._callbacks:
            try:
                callback(update)
            except Exception as e:
                logger.error(f"Callback error: {e}")
    
    async def connect(self) -> None:
        """Poll CoinGecko API for prices."""
        self._running = True
        logger.info("Using CoinGecko REST API for price data")
        
        while self._running:
            await self._fetch_prices()
            await asyncio.sleep(5)  # Poll every 5 seconds (rate limit friendly)
    
    async def disconnect(self) -> None:
        """Stop polling."""
        self._running = False
        if self._session:
            await self._session.close()


class SimulatedPriceFeed:
    """
    Simulated price feed for testing.
    Generates random walk prices around a base value.
    """
    
    def __init__(self, base_price: float = 100000.0, volatility: float = 0.001):
        """
        Args:
            base_price: Starting price
            volatility: Per-tick volatility (standard deviation of returns)
        """
        self._price = base_price
        self._volatility = volatility
        self._callbacks: list[PriceCallback] = []
        self._running = False
    
    def add_callback(self, callback: PriceCallback) -> None:
        self._callbacks.append(callback)
    
    def get_price(self, symbol: str = "btcusdt") -> float:
        return self._price
    
    def set_price(self, price: float) -> None:
        """Manually set price for testing."""
        self._price = price
        self._notify()
    
    def _notify(self) -> None:
        """Notify all callbacks of price update."""
        update = PriceUpdate(
            symbol="btcusdt",
            price=self._price,
            timestamp=datetime.now(),
            source="simulated",
        )
        for callback in self._callbacks:
            try:
                callback(update)
            except Exception as e:
                logger.error(f"Callback error: {e}")
    
    async def run(self, tick_interval: float = 0.5) -> None:
        """Generate simulated price ticks."""
        import random
        
        self._running = True
        logger.info(f"Starting simulated price feed at ${self._price:.2f}")
        
        while self._running:
            # Random walk with mean reversion
            drift = 0.0
            shock = random.gauss(0, self._volatility)
            self._price *= (1 + drift + shock)
            
            self._notify()
            await asyncio.sleep(tick_interval)
    
    async def stop(self) -> None:
        """Stop the simulated feed."""
        self._running = False


class MarketDataManager:
    """
    Unified interface for market data.
    Uses CoinGecko API by default (works worldwide including US).
    """
    
    def __init__(
        self,
        use_live: bool = True,
        symbols: list[str] = None,
        force_simulated: bool = False,
    ):
        """
        Args:
            use_live: If True, use live price data
            symbols: Symbols to track
            force_simulated: Force simulated prices (for unit tests only)
        """
        self.symbols = symbols or ["btcusdt"]
        
        if force_simulated:
            self._feed = SimulatedPriceFeed()
            self.use_live = False
        else:
            # Use CoinGecko (works in US, Binance WebSocket is blocked)
            self._feed = CoinGeckoPriceFeed(self.symbols)
            self.use_live = True
    
    def add_callback(self, callback: PriceCallback) -> None:
        """Add callback for price updates."""
        self._feed.add_callback(callback)
    
    def get_btc_price(self) -> Optional[float]:
        """Get current BTC price."""
        return self._feed.get_price("btcusdt")
    
    def get_eth_price(self) -> Optional[float]:
        """Get current ETH price."""
        return self._feed.get_price("ethusdt")
    
    async def start(self) -> None:
        """Start receiving market data."""
        if self.use_live:
            await self._feed.connect()
        else:
            await self._feed.run()
    
    async def stop(self) -> None:
        """Stop market data feed."""
        if self.use_live:
            await self._feed.disconnect()
        else:
            await self._feed.stop()
