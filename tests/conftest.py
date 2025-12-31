"""
Pytest configuration and fixtures.
"""

import pytest
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@pytest.fixture
def sample_config():
    """Sample configuration for testing."""
    from config import Config, MarketConfig, TradingConfig
    
    return Config(
        private_key="test_key",
        clob_host="https://test.polymarket.com",
        chain_id=137,
        api_key="test_api_key",
        api_secret="test_api_secret",
        api_passphrase="test_api_passphrase",
        signature_type=2,
        funder="",
        market=MarketConfig(
            condition_id="test_condition",
            yes_token_id="test_yes_token",
            no_token_id="test_no_token",
            strike_price=100000.0,
        ),
        trading=TradingConfig(
            target_margin=0.02,
            min_profit=0.01,
            stop_loss_threshold=0.15,
            gamma_stop_minutes=2.0,
            position_size=50.0,
            volatility=0.60,
        ),
        paper_mode=True,
    )
