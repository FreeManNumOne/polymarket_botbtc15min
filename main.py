"""
Main entry point for the Legged Arb Market Maker Bot.
"""

import asyncio
import logging
import sys
from datetime import datetime, timedelta
from typing import Optional

from config import load_config, MarketConfig
from bot import LeggedArbBot
from order_manager import PaperOrderManager, LiveOrderManager
from market_data import MarketDataManager
from market_discovery import MarketDiscovery, discover_markets_cli

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Reduce noise from external libraries
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def auto_discover_market(asset: str = "BTC") -> MarketConfig:
    """
    Automatically discover the next available market.
    
    Args:
        asset: "BTC" or "ETH"
        
    Returns:
        MarketConfig with discovered market details
    """
    discovery = MarketDiscovery()
    
    try:
        logger.info(f"🔍 Auto-discovering next {asset} 15-minute market...")
        
        market = await discovery.find_next_market(asset, min_time_remaining=120)
        
        if not market:
            raise ValueError(f"No active {asset} 15-minute markets found")
        
        logger.info(f"✅ Found market: {market.slug}")
        logger.info(f"   Window: {market.window_start_time}-{market.window_end_time}")
        logger.info(f"   Time left: {market.time_to_expiry/60:.1f} min")
        logger.info(f"   Condition: {market.condition_id[:30]}...")
        
        return MarketConfig(
            condition_id=market.condition_id,
            yes_token_id=market.yes_token_id,
            no_token_id=market.no_token_id,
            strike_price=0.0,  # Up/down markets track direction, not absolute price
        ), market.expiry_timestamp
        
    finally:
        await discovery.close()


async def run_with_discovery(
    asset: str = "BTC",
    paper_mode: bool = True,
    continuous: bool = False,
    duration_hours: Optional[float] = None,
):
    """
    Run bot with automatic market discovery.
    
    Args:
        asset: "BTC" or "ETH"
        paper_mode: If True, use paper trading
        continuous: If True, automatically find next market when current expires
        duration_hours: If set, run for this many hours then stop
    """
    from trade_logger import TradeLogger
    
    config = load_config()
    config.paper_mode = paper_mode
    
    # Create trade logger for the session
    trade_logger = TradeLogger()
    
    # Calculate end time if duration specified
    end_time = None
    if duration_hours:
        end_time = datetime.now().timestamp() + (duration_hours * 3600)
        logger.info(f"📅 Will run for {duration_hours} hours")
    
    cycle_count = 0
    
    try:
        while True:
            # Check if we've exceeded duration
            if end_time and datetime.now().timestamp() >= end_time:
                logger.info("⏰ Duration limit reached")
                break
            
            try:
                # Discover market
                market_config, expiry_ts = await auto_discover_market(asset)
                config.market = market_config
                
                # Get the slug for logging
                discovery = MarketDiscovery()
                market = await discovery.find_next_market(asset, min_time_remaining=60)
                market_slug = market.slug if market else f"{asset}-unknown"
                await discovery.close()
                
                cycle_count += 1
                logger.info(f"🔄 Starting cycle #{cycle_count}: {market_slug}")
                
                # Start a new cycle in the logger
                trade_logger.start_cycle(market_slug, asset)
                
                # Initialize order manager
                if config.paper_mode:
                    order_manager = PaperOrderManager(
                        yes_token_id=config.market.yes_token_id,
                        no_token_id=config.market.no_token_id,
                    )
                else:
                    from py_clob_client.client import ClobClient
                    from py_clob_client.clob_types import ApiCreds
                    from py_clob_client.clob_types import BalanceAllowanceParams
                    from py_clob_client.clob_types import AssetType
                    
                    creds = None
                    if config.api_key and config.api_secret and config.api_passphrase:
                        creds = ApiCreds(
                            api_key=config.api_key,
                            api_secret=config.api_secret,
                            api_passphrase=config.api_passphrase,
                        )
                    if creds is None:
                        raise ValueError(
                            "Live trading requires POLYMARKET_API_KEY / POLYMARKET_API_SECRET / "
                            "POLYMARKET_API_PASSPHRASE (no derive-api-key fallback)."
                        )

                    clob_client = ClobClient(
                        host=config.clob_host,
                        chain_id=config.chain_id,
                        key=config.private_key,
                        creds=creds,
                        signature_type=config.signature_type,
                        funder=(config.funder or None),
                    )

                    # Preflight: show collateral balance & allowance (helps diagnose 400 errors)
                    try:
                        bal = await asyncio.to_thread(
                            clob_client.get_balance_allowance,
                            BalanceAllowanceParams(
                                asset_type=AssetType.COLLATERAL,
                                signature_type=config.signature_type,
                            ),
                        )
                        logger.info(f"💰 Balance/allowance: {bal}")
                    except Exception as e:
                        logger.warning(f"Could not fetch balance/allowance: {e}")
                    
                    order_manager = LiveOrderManager(
                        clob_client=clob_client,
                        yes_token_id=config.market.yes_token_id,
                        no_token_id=config.market.no_token_id,
                    )
                
                # Create bot with trade logger
                # Note: No external price feed needed - we use Polymarket order books directly
                bot = LeggedArbBot(
                    config=config,
                    order_manager=order_manager,
                    market_data=None,  # Not needed for up/down markets
                    trade_logger=trade_logger,
                    market_slug=market_slug,
                    asset=asset,
                )
                bot.set_expiry(expiry_ts)
                
                # Calculate how long to run
                time_to_expiry = expiry_ts - datetime.now().timestamp()
                run_duration = max(10, time_to_expiry - 60)  # Stop 1 min before expiry
                
                logger.info(f"⏱️  Running for {run_duration/60:.1f} minutes until near expiry")
                
                # Run bot with timeout
                try:
                    await asyncio.wait_for(
                        bot.run(tick_interval=1.0),
                        timeout=run_duration,
                    )
                except asyncio.TimeoutError:
                    logger.info("⏰ Market cycle complete, cleaning up...")
                    await order_manager.cancel_all_orders()
                    
                    # Mark cycle as expired if not locked
                    if trade_logger.current_cycle:
                        trade_logger.complete_cycle("EXPIRED")
                
                if not continuous:
                    break
                
                # Wait for next cycle
                logger.info("⏳ Waiting 30 seconds before discovering next market...")
                await asyncio.sleep(30)
                
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(f"Error in cycle: {e}")
                if trade_logger.current_cycle:
                    trade_logger.complete_cycle("STOPPED")
                if not continuous:
                    raise
                logger.info("Retrying in 30 seconds...")
                await asyncio.sleep(30)
                
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        # Print summary
        trade_logger.print_summary()
        logger.info(f"📊 Trade log saved to: {trade_logger.log_file}")


async def main(paper_mode: bool = None, asset: str = None, auto_discover: bool = False):
    """
    Main application entry point.
    
    Args:
        paper_mode: Override config paper mode
        asset: Asset to trade ("BTC" or "ETH")
        auto_discover: If True, automatically discover markets
    """
    if auto_discover and asset:
        await run_with_discovery(asset=asset, paper_mode=paper_mode or True)
        return
    
    # Original manual config mode
    config = load_config()
    
    if paper_mode is not None:
        config.paper_mode = paper_mode
    
    # Validate configuration
    if not config.paper_mode:
        if not config.private_key:
            logger.error("POLYMARKET_PRIVATE_KEY required for live trading")
            sys.exit(1)
        if not (config.api_key and config.api_secret and config.api_passphrase):
            logger.error(
                "POLYMARKET_API_KEY / POLYMARKET_API_SECRET / POLYMARKET_API_PASSPHRASE required for live trading"
            )
            sys.exit(1)
        if not config.market.condition_id:
            logger.error("CONDITION_ID required - use --discover to auto-find markets")
            sys.exit(1)
    
    # Initialize order manager
    if config.paper_mode:
        order_manager = PaperOrderManager(
            yes_token_id=config.market.yes_token_id or "YES_TOKEN",
            no_token_id=config.market.no_token_id or "NO_TOKEN",
        )
    else:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        from py_clob_client.clob_types import BalanceAllowanceParams
        from py_clob_client.clob_types import AssetType

        creds = None
        if config.api_key and config.api_secret and config.api_passphrase:
            creds = ApiCreds(
                api_key=config.api_key,
                api_secret=config.api_secret,
                api_passphrase=config.api_passphrase,
            )
        if creds is None:
            raise ValueError(
                "Live trading requires POLYMARKET_API_KEY / POLYMARKET_API_SECRET / "
                "POLYMARKET_API_PASSPHRASE (no derive-api-key fallback)."
            )
        
        clob_client = ClobClient(
            host=config.clob_host,
            chain_id=config.chain_id,
            key=config.private_key,
            creds=creds,
            signature_type=config.signature_type,
            funder=(config.funder or None),
        )

        try:
            bal = await asyncio.to_thread(
                clob_client.get_balance_allowance,
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=config.signature_type,
                ),
            )
            logger.info(f"💰 Balance/allowance: {bal}")
        except Exception as e:
            logger.warning(f"Could not fetch balance/allowance: {e}")
        
        order_manager = LiveOrderManager(
            clob_client=clob_client,
            yes_token_id=config.market.yes_token_id,
            no_token_id=config.market.no_token_id,
        )
    
    # Initialize market data
    market_data = MarketDataManager(
        use_live=True,  # Always use real Binance prices
        symbols=["btcusdt"],
    )
    
    # Create bot
    bot = LeggedArbBot(
        config=config,
        order_manager=order_manager,
        market_data=market_data,
    )
    
    # Set expiry (for paper trading, use 15 minutes from now)
    if config.paper_mode:
        expiry = datetime.now() + timedelta(minutes=15)
        bot.set_expiry(expiry.timestamp())
    
    # Run
    try:
        await asyncio.gather(
            market_data.start(),
            bot.run(tick_interval=1.0),
        )
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await market_data.stop()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Legged Arb Market Maker Bot for Polymarket 15-min cycles"
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Run in paper trading mode (no real orders)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run in live trading mode (real orders)",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Auto-discover available 15-minute markets",
    )
    parser.add_argument(
        "--asset",
        choices=["BTC", "ETH"],
        default="BTC",
        help="Asset to trade (default: BTC)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_markets",
        help="List all available 15-minute markets and exit",
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="Run continuously, automatically moving to next market cycle",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=None,
        help="Run for this many hours then stop (e.g., --hours 10)",
    )
    parser.add_argument(
        "--report",
        type=str,
        metavar="LOG_FILE",
        help="View a saved session report",
    )
    
    args = parser.parse_args()
    
    if args.live and args.paper:
        print("Error: Cannot specify both --live and --paper")
        sys.exit(1)
    
    # View report mode
    if args.report:
        from trade_logger import print_session_report
        from pathlib import Path
        print_session_report(Path(args.report))
        sys.exit(0)
    
    # List markets mode
    if args.list_markets:
        asyncio.run(discover_markets_cli())
        sys.exit(0)
    
    # Determine paper mode
    paper_mode = not args.live  # Default to paper unless --live
    
    # If hours specified, enable continuous mode
    continuous = args.continuous or (args.hours is not None)
    
    # Run with auto-discovery or manual config
    if args.discover or continuous:
        asyncio.run(run_with_discovery(
            asset=args.asset,
            paper_mode=paper_mode,
            continuous=continuous,
            duration_hours=args.hours,
        ))
    else:
        asyncio.run(main(
            paper_mode=paper_mode,
            asset=args.asset,
            auto_discover=args.discover,
        ))
