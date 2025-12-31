"""
Configuration management for the Legged Arb Market Maker.
Loads settings from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class MarketConfig:
    """Configuration for the target Polymarket market."""
    condition_id: str
    yes_token_id: str
    no_token_id: str
    strike_price: float


@dataclass
class TradingConfig:
    """Trading parameters."""
    target_margin: float  # Spread below fair value for bids
    min_profit: float     # Minimum locked profit per arb
    stop_loss_threshold: float  # Max loss before dumping position
    gamma_stop_minutes: float   # Cancel all orders within N minutes of expiry
    position_size: float  # USD per leg
    volatility: float     # Implied volatility (annualized)


@dataclass
class Config:
    """Main configuration container."""
    # Polymarket API
    private_key: str
    clob_host: str
    chain_id: int

    # Optional direct API creds (Level 2) + signing config
    api_key: str
    api_secret: str
    api_passphrase: str
    signature_type: int
    funder: str
    
    # Market
    market: MarketConfig
    
    # Trading
    trading: TradingConfig
    
    # Mode
    paper_mode: bool


def load_config() -> Config:
    """Load configuration from environment variables."""
    
    # Validate required fields
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    if not private_key and os.getenv("TRADING_MODE", "paper") == "live":
        raise ValueError("POLYMARKET_PRIVATE_KEY is required for live trading")

    # Optional API key auth (Level 2). If these are not set, code will derive creds at runtime.
    api_key = os.getenv("POLYMARKET_API_KEY", "").strip()
    api_secret = os.getenv("POLYMARKET_API_SECRET", "").strip()
    api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE", "").strip()

    signature_type_raw = os.getenv("POLYMARKET_SIGNATURE_TYPE", "").strip()
    try:
        signature_type = int(signature_type_raw) if signature_type_raw else 2
    except ValueError:
        signature_type = 2

    funder = os.getenv("POLYMARKET_FUNDER", "").strip()
    
    market = MarketConfig(
        condition_id=os.getenv("CONDITION_ID", ""),
        yes_token_id=os.getenv("YES_TOKEN_ID", ""),
        no_token_id=os.getenv("NO_TOKEN_ID", ""),
        strike_price=float(os.getenv("STRIKE_PRICE", "100000")),
    )
    
    trading = TradingConfig(
        target_margin=float(os.getenv("TARGET_MARGIN", "0.03")),  # 3% margin below fair value
        min_profit=float(os.getenv("MIN_PROFIT", "0.02")),  # 2% minimum locked profit
        stop_loss_threshold=float(os.getenv("STOP_LOSS_THRESHOLD", "0.15")),
        gamma_stop_minutes=float(os.getenv("GAMMA_STOP_MINUTES", "2")),
        position_size=float(os.getenv("POSITION_SIZE", "50.0")),
        volatility=float(os.getenv("VOLATILITY", "0.60")),
    )
    
    return Config(
        private_key=private_key,
        clob_host=os.getenv("CLOB_HOST", "https://clob.polymarket.com"),
        chain_id=int(os.getenv("CHAIN_ID", "137")),
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
        signature_type=signature_type,
        funder=funder,
        market=market,
        trading=trading,
        paper_mode=os.getenv("TRADING_MODE", "paper").lower() == "paper",
    )
