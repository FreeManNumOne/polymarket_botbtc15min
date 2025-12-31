"""
Order Management for Polymarket CLOB.
Handles order placement, cancellation, and fill tracking.
Supports both live trading and paper trading modes.
"""

import asyncio
import logging
import uuid
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, List, Callable, Awaitable
from enum import Enum

import aiohttp

logger = logging.getLogger(__name__)

# Polymarket CLOB API
CLOB_API_URL = "https://clob.polymarket.com"


class OrderSide(Enum):
    YES = "YES"
    NO = "NO"


class OrderStatus(Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Order:
    """Represents a limit order."""
    id: str
    side: OrderSide
    price: float
    size: float
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    filled_avg_price: float = 0.0
    created_at: datetime = field(default_factory=datetime.now)
    token_id: str = ""
    
    @property
    def remaining(self) -> float:
        return self.size - self.filled_qty
    
    @property
    def is_active(self) -> bool:
        return self.status in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}


@dataclass
class OrderBook:
    """Simplified order book snapshot."""
    bids: List[tuple]  # [(price, size), ...]
    asks: List[tuple]  # [(price, size), ...]
    
    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None
    
    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None
    
    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None
    
    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None


# Type for fill callback: (side, price, qty) -> None
FillCallback = Callable[[str, float, float], Awaitable[None]]


class BaseOrderManager(ABC):
    """Abstract base class for order management."""
    
    def __init__(self, yes_token_id: str, no_token_id: str):
        self.yes_token_id = yes_token_id
        self.no_token_id = no_token_id
        self.orders: Dict[str, Order] = {}
        self._fill_callback: Optional[FillCallback] = None
    
    def set_fill_callback(self, callback: FillCallback) -> None:
        """Set callback to be invoked when orders are filled."""
        self._fill_callback = callback
    
    def get_token_id(self, side: OrderSide) -> str:
        """Get token ID for a given side."""
        return self.yes_token_id if side == OrderSide.YES else self.no_token_id
    
    @abstractmethod
    async def place_limit_buy(
        self, side: OrderSide, price: float, size: float
    ) -> Order:
        """Place a limit buy order."""
        pass
    
    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order by ID."""
        pass
    
    @abstractmethod
    async def cancel_all_orders(self) -> int:
        """Cancel all open orders. Returns count cancelled."""
        pass
    
    @abstractmethod
    async def market_buy(self, side: OrderSide, size: float) -> Order:
        """Execute a market buy order."""
        pass
    
    @abstractmethod
    async def get_order_book(self, side: OrderSide) -> OrderBook:
        """Get current order book for a side."""
        pass
    
    @abstractmethod
    async def refresh_order_status(self, order_id: str) -> Order:
        """Refresh and return the current status of an order."""
        pass
    
    def get_open_orders(self, side: Optional[OrderSide] = None) -> List[Order]:
        """Get all open orders, optionally filtered by side."""
        orders = [o for o in self.orders.values() if o.is_active]
        if side:
            orders = [o for o in orders if o.side == side]
        return orders
    
    async def _notify_fill(self, side: str, price: float, qty: float) -> None:
        """Notify callback of a fill."""
        if self._fill_callback:
            await self._fill_callback(side, price, qty)


class PaperOrderManager(BaseOrderManager):
    """
    Paper trading order manager with REAL Polymarket order book data.
    Fetches live prices from Polymarket but simulates fills locally.
    
    realistic_mode=True: Only fills when market price actually crosses your order
    realistic_mode=False: Random 5% fill chance per tick (for faster testing)
    """
    
    def __init__(
        self,
        yes_token_id: str,
        no_token_id: str,
        fill_probability: float = 0.05,  # 5% chance per tick (only if realistic_mode=False)
        realistic_mode: bool = True,  # True = only fill on real price crosses
    ):
        super().__init__(yes_token_id, no_token_id)
        self.fill_probability = fill_probability
        self.realistic_mode = realistic_mode
        self._session: Optional[aiohttp.ClientSession] = None
        self._cached_books: Dict[OrderSide, OrderBook] = {}
        self._cache_time: float = 0
        self._cache_ttl: float = 0.5  # Refresh every 0.5 seconds
        
        mode_str = "REALISTIC (fills only on price cross)" if realistic_mode else f"RANDOM (fill prob: {fill_probability:.0%})"
        logger.info(f"Paper trading with LIVE order books - {mode_str}")
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def _fetch_live_order_book(self, token_id: str) -> OrderBook:
        """Fetch real order book from Polymarket CLOB API."""
        try:
            session = await self._get_session()
            url = f"{CLOB_API_URL}/book"
            params = {"token_id": token_id}
            
            async with session.get(url, params=params, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    # Parse bids and asks
                    bids = []
                    asks = []
                    
                    for bid in data.get("bids", []):
                        price = float(bid.get("price", 0))
                        size = float(bid.get("size", 0))
                        if price > 0 and size > 0:
                            bids.append((price, size))
                    
                    for ask in data.get("asks", []):
                        price = float(ask.get("price", 0))
                        size = float(ask.get("size", 0))
                        if price > 0 and size > 0:
                            asks.append((price, size))
                    
                    # Sort: bids descending, asks ascending
                    bids.sort(key=lambda x: x[0], reverse=True)
                    asks.sort(key=lambda x: x[0])
                    
                    if bids or asks:
                        return OrderBook(bids=bids, asks=asks)
                        
        except asyncio.TimeoutError:
            logger.debug("Timeout fetching order book")
        except Exception as e:
            logger.debug(f"Error fetching order book: {e}")
        
        # Fallback to default if API fails
        return OrderBook(bids=[(0.50, 100)], asks=[(0.52, 100)])
    
    async def get_order_book(self, side: OrderSide) -> OrderBook:
        """Get live order book from Polymarket."""
        import time
        
        now = time.time()
        if now - self._cache_time > self._cache_ttl:
            # Refresh both books
            yes_book = await self._fetch_live_order_book(self.yes_token_id)
            no_book = await self._fetch_live_order_book(self.no_token_id)
            
            self._cached_books[OrderSide.YES] = yes_book
            self._cached_books[OrderSide.NO] = no_book
            self._cache_time = now
            
            # Log real prices as percentages
            yes_mid = (yes_book.best_bid + yes_book.best_ask) / 2 if yes_book.best_bid and yes_book.best_ask else yes_book.best_bid or yes_book.best_ask or 0.5
            no_mid = (no_book.best_bid + no_book.best_ask) / 2 if no_book.best_bid and no_book.best_ask else no_book.best_bid or no_book.best_ask or 0.5
            logger.info(f"📊 MARKET: YES {yes_mid*100:.1f}% | NO {no_mid*100:.1f}%")
        
        return self._cached_books.get(side, OrderBook(bids=[(0.50, 100)], asks=[(0.52, 100)]))
    
    async def place_limit_buy(
        self, side: OrderSide, price: float, size: float
    ) -> Order:
        """Place a simulated limit order with real price checking."""
        order = Order(
            id=f"paper_{uuid.uuid4().hex[:8]}",
            side=side,
            price=price,
            size=size,
            status=OrderStatus.OPEN,
            token_id=self.get_token_id(side),
        )
        self.orders[order.id] = order
        
        # Check if we can fill immediately against real order book
        book = await self.get_order_book(side)
        
        if book.best_ask and price >= book.best_ask:
            # Our bid is at or above the ask - immediate fill!
            fill_price = book.best_ask
            logger.info(f"[PAPER] 🎯 CROSSED SPREAD: {side.value} {size:.2f}@{fill_price:.4f}")
            order.status = OrderStatus.FILLED
            order.filled_qty = size
            order.filled_avg_price = fill_price
            await self._notify_fill(side.value, fill_price, size)
        else:
            logger.info(f"[PAPER] Placed {side.value} bid: {size:.2f}@{price:.4f}")
            
            # REALISTIC MODE: Only fill if someone actually sells to us
            # Check if any trade happens at or below our price
            # This tracks until order is cancelled or market crosses
            if not self.realistic_mode:
                # Legacy random mode for testing
                if random.random() < self.fill_probability:
                    asyncio.create_task(self._delayed_fill(order.id, delay=random.uniform(0.5, 2.0)))
            # In realistic mode, fills only happen via check_pending_fills()
        
        return order
    
    async def _delayed_fill(self, order_id: str, delay: float = 0.5) -> None:
        """Fill an order after a delay."""
        await asyncio.sleep(delay)
        await self.simulate_fill(order_id)
    
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a simulated order."""
        if order_id in self.orders:
            order = self.orders[order_id]
            if order.is_active:
                order.status = OrderStatus.CANCELLED
                logger.debug(f"[PAPER] Cancelled {order_id}")
                return True
        return False
    
    async def cancel_all_orders(self) -> int:
        """Cancel all simulated orders."""
        count = 0
        for order in self.orders.values():
            if order.is_active:
                order.status = OrderStatus.CANCELLED
                count += 1
        if count > 0:
            logger.info(f"[PAPER] Cancelled {count} orders")
        return count
    
    async def check_pending_fills(self) -> int:
        """
        Check if any open orders should be filled based on current market prices.
        Called each tick in realistic mode.
        Returns number of fills that occurred.
        """
        fills = 0
        for order in list(self.orders.values()):
            if order.status != OrderStatus.OPEN:
                continue
            
            # Get current order book for this side
            side = order.side
            book = await self.get_order_book(side)
            
            # For a limit BUY: we get filled if someone sells at/below our price
            # In practice, this means best_ask <= our bid price
            if book.best_ask and book.best_ask <= order.price:
                fill_price = book.best_ask
                logger.info(f"[PAPER] 🎯 FILL: {side.value} {order.size:.2f}@{fill_price:.4f}")
                order.status = OrderStatus.FILLED
                order.filled_qty = order.size
                order.filled_avg_price = fill_price
                await self._notify_fill(side.value, fill_price, order.size)
                fills += 1
        
        return fills
    
    async def market_buy(self, side: OrderSide, size: float) -> Order:
        """Market buy at real best ask price."""
        book = await self.get_order_book(side)
        fill_price = book.best_ask or 0.55
        
        order = Order(
            id=f"paper_mkt_{uuid.uuid4().hex[:8]}",
            side=side,
            price=fill_price,
            size=size,
            status=OrderStatus.FILLED,
            filled_qty=size,
            filled_avg_price=fill_price,
            token_id=self.get_token_id(side),
        )
        self.orders[order.id] = order
        
        logger.info(f"[PAPER] 🎯 MARKET BUY: {side.value} {size:.2f}@{fill_price:.4f}")
        await self._notify_fill(side.value, fill_price, size)
        
        return order
    
    async def refresh_order_status(self, order_id: str) -> Order:
        """Return current order status."""
        return self.orders.get(order_id)
    
    async def simulate_fill(self, order_id: str, fill_price: Optional[float] = None) -> bool:
        """Trigger a fill for an order."""
        order = self.orders.get(order_id)
        if not order or not order.is_active:
            return False
        
        # Use real bid price from order book if not specified
        if fill_price is None:
            book = await self.get_order_book(order.side)
            fill_price = order.price  # Fill at our bid price
        
        order.status = OrderStatus.FILLED
        order.filled_qty = order.size
        order.filled_avg_price = fill_price
        
        logger.info(f"[PAPER] 🎯 FILL: {order.side.value} {order.size:.2f}@{fill_price:.4f}")
        await self._notify_fill(order.side.value, fill_price, order.size)
        
        return True
    
    async def close(self) -> None:
        """Close HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()


class LiveOrderManager(BaseOrderManager):
    """
    Live order manager using Polymarket CLOB API.
    """
    
    def __init__(
        self,
        clob_client,  # ClobClient instance from py-clob-client
        yes_token_id: str,
        no_token_id: str,
    ):
        super().__init__(yes_token_id, no_token_id)
        self.client = clob_client
        logger.info("Live trading mode initialized")

    @staticmethod
    def _extract_order_id(response) -> str:
        """
        py-clob-client responses have varied over time.
        Normalize common variants into a single order id string.
        """
        if response is None:
            return ""

        # Most common: dict payloads
        if isinstance(response, dict):
            return (
                response.get("orderID")
                or response.get("orderId")
                or response.get("order_id")
                or response.get("id")
                or ""
            )

        # Fallback: object payloads
        for attr in ("orderID", "orderId", "order_id", "id"):
            val = getattr(response, attr, None)
            if isinstance(val, str) and val:
                return val
        return ""

    @staticmethod
    def _normalize_order_book_summary(raw) -> "OrderBook":
        """
        Newer py-clob-client returns OrderBookSummary/OrderSummary objects.
        Older code may expect dict-like structures. Convert to our internal OrderBook.
        """
        if raw is None:
            return OrderBook(bids=[], asks=[])

        def to_levels(levels):
            out = []
            if not levels:
                return out
            for lvl in levels:
                # object style: OrderSummary(price='0.5', size='10')
                if hasattr(lvl, "price") and hasattr(lvl, "size"):
                    try:
                        out.append((float(lvl.price), float(lvl.size)))
                        continue
                    except Exception:
                        pass

                # dict style: {"price": "...", "size": "..."}
                if isinstance(lvl, dict):
                    try:
                        out.append((float(lvl.get("price", 0)), float(lvl.get("size", 0))))
                        continue
                    except Exception:
                        pass
            return out

        # object style: OrderBookSummary(bids=[...], asks=[...])
        bids_raw = getattr(raw, "bids", None) if not isinstance(raw, dict) else raw.get("bids")
        asks_raw = getattr(raw, "asks", None) if not isinstance(raw, dict) else raw.get("asks")
        bids = to_levels(bids_raw)
        asks = to_levels(asks_raw)

        # Ensure expected sorting (best first)
        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])
        return OrderBook(bids=bids, asks=asks)
    
    async def place_limit_buy(
        self, side: OrderSide, price: float, size: float
    ) -> Order:
        """Place a limit buy order via Polymarket API."""
        token_id = self.get_token_id(side)
        
        try:
            # py-clob-client (>=0.34.x) expects strongly-typed OrderArgs
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY

            order_args = OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(size),
                side=BUY,
            )

            response = await asyncio.to_thread(self.client.create_and_post_order, order_args)
            
            order_id = self._extract_order_id(response) or str(uuid.uuid4())
            order = Order(
                id=order_id,
                side=side,
                price=price,
                size=size,
                status=OrderStatus.OPEN,
                token_id=token_id,
            )
            self.orders[order.id] = order
            
            logger.info(f"Placed {side.value} bid: {size}@{price:.4f} (ID: {order.id[:8]})")
            return order
            
        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            raise
    
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order via Polymarket API."""
        try:
            await asyncio.to_thread(self.client.cancel, order_id)
            
            if order_id in self.orders:
                self.orders[order_id].status = OrderStatus.CANCELLED
            
            logger.info(f"Cancelled order {order_id[:8]}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False
    
    async def cancel_all_orders(self) -> int:
        """Cancel all open orders."""
        count = 0
        for order in list(self.orders.values()):
            if order.is_active:
                if await self.cancel_order(order.id):
                    count += 1
        return count
    
    async def market_buy(self, side: OrderSide, size: float) -> Order:
        """Execute market buy by taking best ask."""
        book = await self.get_order_book(side)
        
        if not book.best_ask:
            raise ValueError(f"No asks available for {side.value}")
        
        return await self.place_limit_buy(side, book.best_ask, size)
    
    async def get_order_book(self, side: OrderSide) -> OrderBook:
        """Fetch order book from Polymarket API."""
        token_id = self.get_token_id(side)
        
        try:
            raw = await asyncio.to_thread(self.client.get_order_book, token_id)
            return self._normalize_order_book_summary(raw)
            
        except Exception as e:
            logger.error(f"Failed to fetch order book: {e}")
            return OrderBook(bids=[], asks=[])
    
    async def refresh_order_status(self, order_id: str) -> Order:
        """Refresh order status from API."""
        try:
            response = await asyncio.to_thread(
                self.client.get_order, order_id
            )
            
            if order_id in self.orders:
                order = self.orders[order_id]
                
                status_map = {
                    "open": OrderStatus.OPEN,
                    "filled": OrderStatus.FILLED,
                    "cancelled": OrderStatus.CANCELLED,
                }
                order.status = status_map.get(
                    (response.get("status", "") if isinstance(response, dict) else ""),
                    order.status
                )

                if isinstance(response, dict):
                    filled_size = (
                        response.get("filledSize")
                        or response.get("filled_size")
                        or response.get("matchedSize")
                        or response.get("matched_size")
                        or 0
                    )
                    avg_fill_price = (
                        response.get("avgFillPrice")
                        or response.get("avg_fill_price")
                        or response.get("averageFillPrice")
                        or response.get("average_fill_price")
                        or 0
                    )
                    try:
                        order.filled_qty = float(filled_size or 0)
                    except Exception:
                        order.filled_qty = 0.0
                    try:
                        order.filled_avg_price = float(avg_fill_price or 0)
                    except Exception:
                        order.filled_avg_price = 0.0
                
                if order.status == OrderStatus.FILLED and order.filled_qty > 0:
                    await self._notify_fill(
                        order.side.value,
                        order.filled_avg_price,
                        order.filled_qty
                    )
                
                return order
                
        except Exception as e:
            logger.error(f"Failed to refresh order {order_id}: {e}")
        
        return self.orders.get(order_id)
    
    async def poll_for_fills(self, interval: float = 1.0) -> None:
        """Continuously poll for order fills."""
        while True:
            for order_id in list(self.orders.keys()):
                order = self.orders.get(order_id)
                if order and order.is_active:
                    await self.refresh_order_status(order_id)
            
            await asyncio.sleep(interval)
