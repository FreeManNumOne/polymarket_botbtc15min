"""
Legged Arb Market Maker Bot.
Main trading logic implementing the three-phase arbitrage strategy.

Phases:
1. FISHER (Neutral): Place bids on both YES and NO at discounted prices
2. TRAPPER (Legged): One side filled, aggressively seek the other side
3. VAULT (Locked): Both sides acquired, wait for settlement
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from config import Config
from state_machine import StateMachine, BotState
from order_manager import BaseOrderManager, OrderSide, Order, OrderBook
from market_data import MarketDataManager, PriceUpdate
from pricing import get_skewed_bid
from safety import SafetyMonitor, SafetyConfig, emergency_exit, RiskLimits
from trade_logger import TradeLogger

logger = logging.getLogger(__name__)


class LeggedArbBot:
    """
    Main trading bot implementing the legged arbitrage strategy.
    
    The bot exploits the spread between YES/NO binary options by:
    1. Placing limit bids below fair value on both sides
    2. When one side fills, immediately seeking the other side
    3. Locking in profit when both sides are acquired for < $1.00 total
    """
    
    def __init__(
        self,
        config: Config,
        order_manager: BaseOrderManager,
        market_data: Optional[MarketDataManager] = None,
        trade_logger: Optional[TradeLogger] = None,
        market_slug: str = "",
        asset: str = "BTC",
    ):
        self.config = config
        self.order_manager = order_manager
        self.market_data = market_data
        self.trade_logger = trade_logger
        self.market_slug = market_slug
        self.asset = asset
        
        self.state_machine = StateMachine()
        self.safety = SafetyMonitor(SafetyConfig(
            gamma_stop_minutes=config.trading.gamma_stop_minutes,
            stop_loss_threshold=config.trading.stop_loss_threshold,
        ))
        self.risk_limits = RiskLimits()
        
        # Current market state
        self._btc_price: Optional[float] = None
        self._expiry_timestamp: Optional[float] = None
        self._active_orders: dict[OrderSide, Optional[str]] = {
            OrderSide.YES: None,
            OrderSide.NO: None,
        }
        
        # Tick tracking
        self._tick_count = 0
        self._last_tick_time: Optional[datetime] = None
        
        # Set up callbacks
        self.order_manager.set_fill_callback(self._on_fill)
        if self.market_data:
            self.market_data.add_callback(self._on_price_update)
    
    def set_expiry(self, expiry_timestamp: float) -> None:
        """Set the current market's expiry timestamp."""
        self._expiry_timestamp = expiry_timestamp
        expiry_dt = datetime.fromtimestamp(expiry_timestamp)
        logger.info(f"Market expiry set: {expiry_dt}")
    
    def _on_price_update(self, update: PriceUpdate) -> None:
        """Handle incoming price updates from market data."""
        if update.symbol == "btcusdt":
            self._btc_price = update.price
    
    async def _on_fill(self, side: str, price: float, qty: float) -> None:
        """Handle order fill events."""
        logger.info(f"FILL: {side} {qty:.2f}@{price:.4f}")
        
        # Update state machine
        new_state = self.state_machine.on_fill(side, price, qty)
        
        # Log trade
        if self.trade_logger:
            self.trade_logger.record_trade(
                side=side,
                price=price,
                quantity=qty,
                state=new_state.name,
                market_slug=self.market_slug,
                asset=self.asset,
            )
        
        # Clear the filled order from tracking
        order_side = OrderSide.YES if side == "YES" else OrderSide.NO
        self._active_orders[order_side] = None
        
        # If we're now locked, record the profit
        if new_state == BotState.LOCKED:
            profit = self.state_machine.inventory.locked_profit * qty
            self.risk_limits.record_pnl(profit)
            logger.info(f"🔒 LOCKED! Profit: ${profit:.4f}")
            
            # Complete the trading cycle
            if self.trade_logger:
                self.trade_logger.complete_cycle("LOCKED", profit)
    
    async def _get_real_fair_values(self) -> tuple[float, float]:
        """
        Get fair values from REAL Polymarket order book mid-prices.
        
        Returns:
            (fv_yes, fv_no): Fair values based on real market data
        """
        try:
            # Fetch real order books from Polymarket
            yes_book = await self.order_manager.get_order_book(OrderSide.YES)
            no_book = await self.order_manager.get_order_book(OrderSide.NO)
            
            # Calculate mid-price for YES
            if yes_book.best_bid and yes_book.best_ask:
                fv_yes = (yes_book.best_bid + yes_book.best_ask) / 2
            elif yes_book.best_bid:
                fv_yes = yes_book.best_bid
            elif yes_book.best_ask:
                fv_yes = yes_book.best_ask
            else:
                fv_yes = 0.50  # Fallback
            
            # Calculate mid-price for NO
            if no_book.best_bid and no_book.best_ask:
                fv_no = (no_book.best_bid + no_book.best_ask) / 2
            elif no_book.best_bid:
                fv_no = no_book.best_bid
            elif no_book.best_ask:
                fv_no = no_book.best_ask
            else:
                fv_no = 0.50  # Fallback
            
            return fv_yes, fv_no
            
        except Exception as e:
            logger.warning(f"Failed to get real prices: {e}, using 50/50")
            return 0.50, 0.50
    
    def _calculate_fair_values(self) -> tuple[float, float]:
        """
        Synchronous wrapper - returns cached fair values.
        Call _get_real_fair_values() in async context for fresh data.
        """
        # Return cached values or default
        return getattr(self, '_cached_fv_yes', 0.50), getattr(self, '_cached_fv_no', 0.50)
    
    def _calculate_hedge_price(self, cost_basis: float) -> float:
        """
        Calculate the maximum price we can pay for the hedge leg.
        
        Args:
            cost_basis: Average cost of the first leg
            
        Returns:
            Maximum acceptable price for the hedge leg
        """
        # To profit: cost_basis + hedge_price < 1.00
        # Target: cost_basis + hedge_price = 1.00 - min_profit
        max_hedge = 1.00 - cost_basis - self.config.trading.min_profit
        
        # Clamp to valid range
        return max(0.01, min(0.99, max_hedge))
    
    async def _cancel_side_order(self, side: OrderSide) -> None:
        """Cancel any active order on the given side."""
        order_id = self._active_orders.get(side)
        if order_id:
            await self.order_manager.cancel_order(order_id)
            self._active_orders[side] = None
    
    async def _place_bid(
        self,
        side: OrderSide,
        price: float,
        size: Optional[float] = None
    ) -> Optional[Order]:
        """Place a bid on the given side, replacing any existing order."""
        # Validate price
        if not self.safety.validate_price(price):
            return None
        
        # Cancel existing order on this side
        await self._cancel_side_order(side)
        
        # Calculate size
        if size is None:
            size = self.config.trading.position_size / price
        size = self.safety.validate_order_size(size * price) / price
        
        # Place new order
        order = await self.order_manager.place_limit_buy(side, price, size)
        self._active_orders[side] = order.id
        
        return order
    
    async def on_tick(self) -> None:
        """
        Main tick handler. Called periodically to evaluate and update positions.
        """
        self._tick_count += 1
        self._last_tick_time = datetime.now()
        
        # Check for fills from real market movement (realistic mode)
        if hasattr(self.order_manager, 'check_pending_fills'):
            await self.order_manager.check_pending_fills()
        
        # Safety checks
        if not self._run_safety_checks():
            return
        
        # Check if we can trade
        can_trade, reason = self.risk_limits.can_trade()
        if not can_trade:
            logger.warning(f"Trading disabled: {reason}")
            return
        
        state = self.state_machine.state
        
        # Get REAL fair values from Polymarket order books
        fv_yes, fv_no = await self._get_real_fair_values()
        self._cached_fv_yes = fv_yes
        self._cached_fv_no = fv_no
        
        logger.debug(
            f"Tick #{self._tick_count} | State: {state.name} | "
            f"FV: YES={fv_yes:.4f} NO={fv_no:.4f}"
        )
        
        # Execute phase-specific logic
        if state == BotState.NEUTRAL:
            await self._phase_fisher(fv_yes, fv_no)
        elif state == BotState.LEGGED_YES:
            await self._phase_trapper_yes(fv_no)
        elif state == BotState.LEGGED_NO:
            await self._phase_trapper_no(fv_yes)
        elif state == BotState.LOCKED:
            await self._phase_vault()
    
    def _run_safety_checks(self) -> bool:
        """
        Run safety checks. Returns False if trading should stop.
        """
        state = self.state_machine.state
        
        # Gamma stop check
        if self._expiry_timestamp:
            seconds_to_expiry = self._expiry_timestamp - datetime.now().timestamp()
            if self.safety.check_gamma_stop(seconds_to_expiry):
                asyncio.create_task(self._emergency_stop("Gamma stop triggered"))
                return False
        
        # Position timeout
        if self.safety.check_position_timeout(state):
            asyncio.create_task(self._emergency_stop("Position timeout"))
            return False
        
        # Stop loss check (for legged positions)
        if state == BotState.LEGGED_YES:
            # Check if our YES position is underwater
            fv_yes, _ = self._calculate_fair_values()
            cost = self.state_machine.inventory.yes.avg_cost
            if self.safety.check_stop_loss(fv_yes, cost):
                asyncio.create_task(self._emergency_stop("Stop loss triggered"))
                return False
                
        elif state == BotState.LEGGED_NO:
            _, fv_no = self._calculate_fair_values()
            cost = self.state_machine.inventory.no.avg_cost
            if self.safety.check_stop_loss(fv_no, cost):
                asyncio.create_task(self._emergency_stop("Stop loss triggered"))
                return False
        
        return True
    
    async def _emergency_stop(self, reason: str) -> None:
        """Trigger emergency stop."""
        await emergency_exit(self.order_manager, self.state_machine, reason)
    
    async def _phase_fisher(self, fv_yes: float, fv_no: float) -> None:
        """
        Phase 1: FISHER (Neutral State)
        Place bids on both YES and NO at fair value minus target margin.
        Goal: Catch panic sellers on either side.
        """
        margin = self.config.trading.target_margin
        inventory = self.state_machine.inventory
        
        # Calculate skewed bids (adjust for any existing inventory)
        bid_yes = get_skewed_bid(
            fair_value=fv_yes,
            inventory=inventory.yes.quantity,
            spread=margin,
        )
        bid_no = get_skewed_bid(
            fair_value=fv_no,
            inventory=inventory.no.quantity,
            spread=margin,
        )
        
        # Place/update bids
        await self._place_bid(OrderSide.YES, bid_yes)
        await self._place_bid(OrderSide.NO, bid_no)
        
        logger.info(f"🎣 FISHER: Bids @ YES:{bid_yes:.4f} NO:{bid_no:.4f}")
    
    async def _phase_trapper_yes(self, fv_no: float) -> None:
        """
        Phase 2: TRAPPER (Legged YES)
        We have YES shares. We MUST acquire NO to complete the arb.
        """
        # Cancel YES bid - we don't want more YES
        await self._cancel_side_order(OrderSide.YES)
        
        # Calculate max price for NO
        cost_basis_yes = self.state_machine.inventory.yes.avg_cost
        max_bid_no = self._calculate_hedge_price(cost_basis_yes)
        
        # Check if we can take the ask immediately
        book = await self.order_manager.get_order_book(OrderSide.NO)
        
        if book.best_ask and book.best_ask <= max_bid_no:
            # Market conditions favor immediate execution
            logger.info(
                f"🎯 TRAPPER: Crossing spread for NO @ {book.best_ask:.4f} "
                f"(max: {max_bid_no:.4f})"
            )
            await self.order_manager.market_buy(
                OrderSide.NO,
                self.state_machine.inventory.yes.quantity
            )
        else:
            # Place aggressive bid at our max price
            await self._place_bid(OrderSide.NO, max_bid_no)
            logger.info(
                f"🪤 TRAPPER: Bid NO @ {max_bid_no:.4f} "
                f"(YES cost: {cost_basis_yes:.4f})"
            )
    
    async def _phase_trapper_no(self, fv_yes: float) -> None:
        """
        Phase 2: TRAPPER (Legged NO)
        We have NO shares. We MUST acquire YES to complete the arb.
        """
        # Cancel NO bid - we don't want more NO
        await self._cancel_side_order(OrderSide.NO)
        
        # Calculate max price for YES
        cost_basis_no = self.state_machine.inventory.no.avg_cost
        max_bid_yes = self._calculate_hedge_price(cost_basis_no)
        
        # Check if we can take the ask immediately
        book = await self.order_manager.get_order_book(OrderSide.YES)
        
        if book.best_ask and book.best_ask <= max_bid_yes:
            logger.info(
                f"🎯 TRAPPER: Crossing spread for YES @ {book.best_ask:.4f} "
                f"(max: {max_bid_yes:.4f})"
            )
            await self.order_manager.market_buy(
                OrderSide.YES,
                self.state_machine.inventory.no.quantity
            )
        else:
            await self._place_bid(OrderSide.YES, max_bid_yes)
            logger.info(
                f"🪤 TRAPPER: Bid YES @ {max_bid_yes:.4f} "
                f"(NO cost: {cost_basis_no:.4f})"
            )
    
    async def _phase_vault(self) -> None:
        """
        Phase 3: VAULT (Locked State)
        Both sides acquired. Cancel all orders and wait for settlement.
        """
        # Ensure no active orders
        await self.order_manager.cancel_all_orders()
        self._active_orders = {OrderSide.YES: None, OrderSide.NO: None}
        
        summary = self.state_machine.get_summary()
        logger.info(
            f"🏦 VAULT: Profit locked @ ${summary['locked_profit']:.4f} | "
            f"YES: {summary['yes_qty']:.2f}@{summary['yes_avg_cost']:.4f} | "
            f"NO: {summary['no_qty']:.2f}@{summary['no_avg_cost']:.4f}"
        )
    
    async def run(self, tick_interval: float = 1.0) -> None:
        """
        Main run loop.
        
        Args:
            tick_interval: Seconds between ticks
        """
        logger.info("=" * 50)
        logger.info("🚀 Starting Legged Arb Bot")
        logger.info(f"   Mode: {'PAPER' if self.config.paper_mode else 'LIVE'}")
        logger.info(f"   Market: Up/Down 15-minute cycle")
        logger.info(f"   Position Size: ${self.config.trading.position_size:.2f}")
        logger.info(f"   Target Margin: {self.config.trading.target_margin:.2%}")
        logger.info(f"   Min Profit: {self.config.trading.min_profit:.2%}")
        logger.info("=" * 50)
        
        try:
            while True:
                await self.on_tick()
                await asyncio.sleep(tick_interval)
                
        except asyncio.CancelledError:
            logger.info("Bot stopped")
        except Exception as e:
            logger.exception(f"Bot error: {e}")
            await self._emergency_stop(f"Unhandled error: {e}")
        finally:
            # Cleanup
            await self.order_manager.cancel_all_orders()
    
    def get_status(self) -> dict:
        """Get current bot status."""
        return {
            "state": self.state_machine.state.name,
            "btc_price": self._btc_price,
            "tick_count": self._tick_count,
            "last_tick": self._last_tick_time.isoformat() if self._last_tick_time else None,
            "position": self.state_machine.get_summary(),
            "risk": self.risk_limits.get_stats(),
            "active_orders": {
                k.value: v for k, v in self._active_orders.items()
            },
        }
