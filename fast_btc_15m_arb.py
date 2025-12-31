"""
Fast BTC 15-minute "box" arbitrage bot for Polymarket Up/Down markets.

Idea:
Buy both outcomes (UP + DOWN) when the combined ask price is sufficiently below 1.00,
locking an expected payout of ~1.00 per share at resolution (ignoring fees/slippage).

This script focuses on latency / request minimization:
- Fetch both books in ONE request via `get_order_books`.
- Place both orders in ONE request via `post_orders`.
- Use FOK (fill-or-kill) so orders don't rest on the book by default.

WARNING: Trading real money is risky. Use paper trading / small size first.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from market_discovery import MarketDiscovery, DiscoveredMarket

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FastArbParams:
    min_edge: float = 0.005  # require (1 - (p_up+p_down)) >= edge
    usd_per_attempt: float = 5.0  # target spend per attempt (approx)
    min_seconds_to_expiry: float = 60.0  # stop this many seconds before expiry
    poll_interval_s: float = 0.25  # main loop interval
    order_type: str = "FOK"  # FOK recommended to avoid resting orders


def _require_env(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise ValueError(f"Missing required env var: {name}")
    return v


def _best_ask(summary) -> Optional[Tuple[float, float]]:
    """
    Returns (price, size) from a py-clob-client OrderBookSummary.
    price/size are strings in OrderSummary.
    """
    asks = getattr(summary, "asks", None)
    if not asks:
        return None
    top = asks[0]
    try:
        return float(top.price), float(top.size)
    except Exception:
        return None


async def _get_next_market(discovery: MarketDiscovery, asset: str, min_time_remaining: float) -> DiscoveredMarket:
    m = await discovery.find_next_market(asset=asset, min_time_remaining=min_time_remaining)
    if not m:
        raise RuntimeError(f"No {asset} 15-minute market found with >= {min_time_remaining}s remaining")
    return m


async def run_fast_box_arb(
    asset: str = "BTC",
    continuous: bool = True,
    params: FastArbParams = FastArbParams(),
):
    # Auth (no fallback)
    host = os.getenv("CLOB_HOST", "https://clob.polymarket.com").strip()
    chain_id = int(os.getenv("CHAIN_ID", "137"))
    private_key = _require_env("POLYMARKET_PRIVATE_KEY")
    api_key = _require_env("POLYMARKET_API_KEY")
    api_secret = _require_env("POLYMARKET_API_SECRET")
    api_passphrase = _require_env("POLYMARKET_API_PASSPHRASE")
    signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "2") or "2")
    funder = os.getenv("POLYMARKET_FUNDER", "").strip() or None

    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, BookParams, OrderArgs, OrderType, PartialCreateOrderOptions, PostOrdersArgs
    from py_clob_client.order_builder.constants import BUY

    creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
    client = ClobClient(
        host=host,
        chain_id=chain_id,
        key=private_key,
        creds=creds,
        signature_type=signature_type,
        funder=funder,
    )

    discovery = MarketDiscovery()
    try:
        cycle = 0
        while True:
            cycle += 1
            market = await _get_next_market(discovery, asset=asset, min_time_remaining=params.min_seconds_to_expiry + 30)
            logger.info(f"🎯 Market: {market.slug} | up={market.up_token_id[:10]}.. down={market.down_token_id[:10]}..")

            up_id = market.up_token_id
            down_id = market.down_token_id

            end_ts = market.expiry_timestamp - params.min_seconds_to_expiry
            attempts = 0
            fills = 0

            while time.time() < end_ts:
                attempts += 1

                # 1 request: fetch both books
                books = await asyncio.to_thread(
                    client.get_order_books,
                    [BookParams(token_id=up_id), BookParams(token_id=down_id)],
                )
                if not books or len(books) != 2:
                    await asyncio.sleep(params.poll_interval_s)
                    continue

                up_book, down_book = books[0], books[1]
                up_ask = _best_ask(up_book)
                down_ask = _best_ask(down_book)
                if not up_ask or not down_ask:
                    await asyncio.sleep(params.poll_interval_s)
                    continue

                p_up, s_up = up_ask
                p_down, s_down = down_ask
                p_sum = p_up + p_down
                edge = 1.0 - p_sum

                if edge < params.min_edge:
                    await asyncio.sleep(params.poll_interval_s)
                    continue

                # Compute shares q to spend ~usd_per_attempt
                # cost ~= p_sum * q
                q = params.usd_per_attempt / max(1e-9, p_sum)
                q = min(q, s_up, s_down)
                if q <= 0:
                    await asyncio.sleep(params.poll_interval_s)
                    continue

                # Create both signed orders locally; avoid extra calls by passing tick_size/neg_risk from book.
                up_opts = PartialCreateOrderOptions(tick_size=getattr(up_book, "tick_size", None), neg_risk=getattr(up_book, "neg_risk", None))
                down_opts = PartialCreateOrderOptions(tick_size=getattr(down_book, "tick_size", None), neg_risk=getattr(down_book, "neg_risk", None))

                up_order = client.create_order(OrderArgs(token_id=up_id, price=p_up, size=q, side=BUY), up_opts)
                down_order = client.create_order(OrderArgs(token_id=down_id, price=p_down, size=q, side=BUY), down_opts)

                ot = getattr(OrderType, params.order_type, OrderType.FOK)
                resp = await asyncio.to_thread(
                    client.post_orders,
                    [PostOrdersArgs(order=up_order, orderType=ot), PostOrdersArgs(order=down_order, orderType=ot)],
                )

                fills += 1
                logger.info(
                    f"⚡ BOX_ARB edge={edge:.4f} sum={p_sum:.4f} q={q:.4f} | "
                    f"UP@{p_up:.4f} DOWN@{p_down:.4f} | resp={resp}"
                )

                # Small delay to avoid hammering
                await asyncio.sleep(params.poll_interval_s)

            logger.info(f"⏹️ Cycle #{cycle} done. Attempts={attempts} Trades={fills}")
            if not continuous:
                break

    finally:
        await discovery.close()


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fast BTC 15m box arbitrage bot (Polymarket CLOB)")
    p.add_argument("--asset", default="BTC", choices=["BTC", "ETH"])
    p.add_argument("--edge", type=float, default=0.005, help="Min edge: 1 - (upAsk+downAsk)")
    p.add_argument("--usd", type=float, default=5.0, help="Target USD spend per attempt (approx)")
    p.add_argument("--interval", type=float, default=0.25, help="Polling interval seconds")
    p.add_argument("--stop-seconds", type=float, default=60.0, help="Stop this many seconds before expiry")
    p.add_argument("--order-type", type=str, default="FOK", choices=["FOK", "FAK", "GTC", "GTD"])
    p.add_argument("--once", action="store_true", help="Run one market cycle then exit")
    return p


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    args = _build_argparser().parse_args()
    arb_params = FastArbParams(
        min_edge=args.edge,
        usd_per_attempt=args.usd,
        poll_interval_s=args.interval,
        min_seconds_to_expiry=args.stop_seconds,
        order_type=args.order_type,
    )

    asyncio.run(run_fast_box_arb(asset=args.asset, continuous=(not args.once), params=arb_params))

