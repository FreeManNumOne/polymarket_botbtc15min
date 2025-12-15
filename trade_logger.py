"""
Trade Logger and Performance Tracker.
Records all trades to a JSON file and calculates performance metrics.
"""

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, List
from pathlib import Path

logger = logging.getLogger(__name__)

# Default log directory
LOG_DIR = Path(__file__).parent / "trade_logs"


@dataclass
class TradeRecord:
    """A single trade record."""
    timestamp: str
    market_slug: str
    asset: str
    side: str  # "UP" or "DOWN"
    action: str  # "BUY", "SELL"
    price: float
    quantity: float
    value: float  # price * quantity
    state: str  # Bot state at time of trade
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CycleRecord:
    """A complete trading cycle (entry + exit or settlement)."""
    cycle_id: str
    market_slug: str
    asset: str
    start_time: str
    end_time: Optional[str] = None
    
    # Entry trades
    up_entry_price: Optional[float] = None
    up_entry_qty: Optional[float] = None
    down_entry_price: Optional[float] = None
    down_entry_qty: Optional[float] = None
    
    # Total cost and profit
    total_cost: float = 0.0
    locked_profit: float = 0.0
    
    # Status
    status: str = "OPEN"  # "OPEN", "LOCKED", "STOPPED", "EXPIRED"
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PerformanceStats:
    """Aggregated performance statistics."""
    total_trades: int = 0
    total_cycles: int = 0
    completed_cycles: int = 0
    locked_cycles: int = 0  # Successfully locked profit
    stopped_cycles: int = 0  # Hit stop loss
    expired_cycles: int = 0  # Expired without completion
    
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_pnl: float = 0.0
    
    win_rate: float = 0.0
    avg_profit_per_cycle: float = 0.0
    
    start_time: str = ""
    end_time: str = ""
    duration_hours: float = 0.0
    
    def to_dict(self) -> dict:
        return asdict(self)


class TradeLogger:
    """
    Logs trades and calculates performance metrics.
    
    Saves to JSON file for persistence across sessions.
    """
    
    def __init__(self, log_dir: Optional[Path] = None, session_name: Optional[str] = None):
        self.log_dir = log_dir or LOG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # Session name for this run
        if session_name:
            self.session_name = session_name
        else:
            self.session_name = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        self.log_file = self.log_dir / f"session_{self.session_name}.json"
        
        # Current session data
        self.trades: List[TradeRecord] = []
        self.cycles: List[CycleRecord] = []
        self.current_cycle: Optional[CycleRecord] = None
        self.session_start = datetime.now()
        
        # Load existing data if resuming
        self._load()
        
        logger.info(f"Trade logger initialized: {self.log_file}")
    
    def _load(self) -> None:
        """Load existing session data if available."""
        if self.log_file.exists():
            try:
                with open(self.log_file, "r") as f:
                    data = json.load(f)
                    
                self.trades = [TradeRecord(**t) for t in data.get("trades", [])]
                self.cycles = [CycleRecord(**c) for c in data.get("cycles", [])]
                
                if data.get("session_start"):
                    self.session_start = datetime.fromisoformat(data["session_start"])
                    
                logger.info(f"Loaded {len(self.trades)} trades, {len(self.cycles)} cycles")
                
            except Exception as e:
                logger.warning(f"Could not load existing log: {e}")
    
    def _save(self) -> None:
        """Save session data to file."""
        data = {
            "session_name": self.session_name,
            "session_start": self.session_start.isoformat(),
            "session_end": datetime.now().isoformat(),
            "trades": [t.to_dict() for t in self.trades],
            "cycles": [c.to_dict() for c in self.cycles],
            "stats": self.get_stats().to_dict(),
        }
        
        with open(self.log_file, "w") as f:
            json.dump(data, f, indent=2)
    
    def start_cycle(self, market_slug: str, asset: str) -> None:
        """Start a new trading cycle."""
        cycle_id = f"{asset}_{datetime.now().strftime('%H%M%S')}"
        
        self.current_cycle = CycleRecord(
            cycle_id=cycle_id,
            market_slug=market_slug,
            asset=asset,
            start_time=datetime.now().isoformat(),
        )
        
        logger.info(f"📊 Started cycle: {cycle_id}")
    
    def record_trade(
        self,
        side: str,
        price: float,
        quantity: float,
        state: str,
        market_slug: str = "",
        asset: str = "",
    ) -> None:
        """Record a trade (fill)."""
        trade = TradeRecord(
            timestamp=datetime.now().isoformat(),
            market_slug=market_slug or (self.current_cycle.market_slug if self.current_cycle else ""),
            asset=asset or (self.current_cycle.asset if self.current_cycle else ""),
            side=side.upper(),
            action="BUY",
            price=price,
            quantity=quantity,
            value=price * quantity,
            state=state,
        )
        
        self.trades.append(trade)
        
        # Update current cycle
        if self.current_cycle:
            if side.upper() in ["YES", "UP"]:
                self.current_cycle.up_entry_price = price
                self.current_cycle.up_entry_qty = quantity
            else:
                self.current_cycle.down_entry_price = price
                self.current_cycle.down_entry_qty = quantity
            
            # Calculate total cost
            up_cost = (self.current_cycle.up_entry_price or 0) * (self.current_cycle.up_entry_qty or 0)
            down_cost = (self.current_cycle.down_entry_price or 0) * (self.current_cycle.down_entry_qty or 0)
            self.current_cycle.total_cost = up_cost + down_cost
        
        self._save()
        logger.debug(f"Recorded trade: {side} {quantity:.2f}@{price:.4f}")
    
    def complete_cycle(self, status: str, locked_profit: float = 0.0) -> None:
        """Complete the current cycle."""
        if not self.current_cycle:
            return
        
        self.current_cycle.end_time = datetime.now().isoformat()
        self.current_cycle.status = status
        self.current_cycle.locked_profit = locked_profit
        
        self.cycles.append(self.current_cycle)
        
        logger.info(
            f"📊 Completed cycle: {self.current_cycle.cycle_id} | "
            f"Status: {status} | Profit: ${locked_profit:.4f}"
        )
        
        self.current_cycle = None
        self._save()
    
    def get_stats(self) -> PerformanceStats:
        """Calculate performance statistics."""
        stats = PerformanceStats()
        
        stats.total_trades = len(self.trades)
        stats.total_cycles = len(self.cycles)
        
        for cycle in self.cycles:
            if cycle.status == "LOCKED":
                stats.locked_cycles += 1
                stats.completed_cycles += 1
                if cycle.locked_profit > 0:
                    stats.gross_profit += cycle.locked_profit
                else:
                    stats.gross_loss += abs(cycle.locked_profit)
                    
            elif cycle.status == "STOPPED":
                stats.stopped_cycles += 1
                stats.completed_cycles += 1
                # Stop loss typically means a loss
                if cycle.total_cost > 0:
                    # Estimate loss as half the position (worst case)
                    stats.gross_loss += cycle.total_cost * 0.5
                    
            elif cycle.status == "EXPIRED":
                stats.expired_cycles += 1
        
        stats.net_pnl = stats.gross_profit - stats.gross_loss
        
        if stats.completed_cycles > 0:
            stats.win_rate = stats.locked_cycles / stats.completed_cycles
            stats.avg_profit_per_cycle = stats.net_pnl / stats.completed_cycles
        
        stats.start_time = self.session_start.isoformat()
        stats.end_time = datetime.now().isoformat()
        stats.duration_hours = (datetime.now() - self.session_start).total_seconds() / 3600
        
        return stats
    
    def print_summary(self) -> None:
        """Print a summary of the session."""
        stats = self.get_stats()
        
        print("\n" + "=" * 60)
        print("📊 TRADING SESSION SUMMARY")
        print("=" * 60)
        print(f"Session: {self.session_name}")
        print(f"Duration: {stats.duration_hours:.2f} hours")
        print(f"Log File: {self.log_file}")
        print("-" * 60)
        print(f"Total Trades: {stats.total_trades}")
        print(f"Total Cycles: {stats.total_cycles}")
        print(f"  - Locked (profit): {stats.locked_cycles}")
        print(f"  - Stopped (loss):  {stats.stopped_cycles}")
        print(f"  - Expired:         {stats.expired_cycles}")
        print("-" * 60)
        print(f"Gross Profit: ${stats.gross_profit:.4f}")
        print(f"Gross Loss:   ${stats.gross_loss:.4f}")
        print(f"Net P&L:      ${stats.net_pnl:.4f}")
        print("-" * 60)
        print(f"Win Rate:     {stats.win_rate:.1%}")
        print(f"Avg/Cycle:    ${stats.avg_profit_per_cycle:.4f}")
        print("=" * 60 + "\n")
    
    def get_recent_trades(self, n: int = 10) -> List[TradeRecord]:
        """Get the N most recent trades."""
        return self.trades[-n:]
    
    def get_recent_cycles(self, n: int = 5) -> List[CycleRecord]:
        """Get the N most recent cycles."""
        return self.cycles[-n:]


def print_session_report(log_file: Path) -> None:
    """Print a report from a saved session log file."""
    if not log_file.exists():
        print(f"Log file not found: {log_file}")
        return
    
    with open(log_file, "r") as f:
        data = json.load(f)
    
    stats = data.get("stats", {})
    
    print("\n" + "=" * 60)
    print("📊 SESSION REPORT")
    print("=" * 60)
    print(f"Session: {data.get('session_name', 'Unknown')}")
    print(f"Start:   {data.get('session_start', 'Unknown')}")
    print(f"End:     {data.get('session_end', 'Unknown')}")
    print(f"Duration: {stats.get('duration_hours', 0):.2f} hours")
    print("-" * 60)
    print(f"Total Trades: {stats.get('total_trades', 0)}")
    print(f"Cycles: {stats.get('total_cycles', 0)}")
    print(f"  Locked: {stats.get('locked_cycles', 0)}")
    print(f"  Stopped: {stats.get('stopped_cycles', 0)}")
    print(f"  Expired: {stats.get('expired_cycles', 0)}")
    print("-" * 60)
    print(f"Net P&L: ${stats.get('net_pnl', 0):.4f}")
    print(f"Win Rate: {stats.get('win_rate', 0):.1%}")
    print("=" * 60)
    
    # Show recent trades
    trades = data.get("trades", [])
    if trades:
        print("\n📝 Recent Trades:")
        for trade in trades[-10:]:
            print(f"  {trade['timestamp'][:19]} | {trade['side']:4} | "
                  f"{trade['quantity']:.2f}@{trade['price']:.4f} | {trade['state']}")
    
    # Show cycles
    cycles = data.get("cycles", [])
    if cycles:
        print("\n🔄 Cycles:")
        for cycle in cycles[-10:]:
            print(f"  {cycle['cycle_id']} | {cycle['status']:7} | "
                  f"Cost: ${cycle['total_cost']:.2f} | Profit: ${cycle['locked_profit']:.4f}")


def list_sessions(log_dir: Optional[Path] = None) -> List[Path]:
    """List all session log files."""
    log_dir = log_dir or LOG_DIR
    if not log_dir.exists():
        return []
    
    return sorted(log_dir.glob("session_*.json"), reverse=True)


if __name__ == "__main__":
    # CLI to view session reports
    import sys
    
    if len(sys.argv) > 1:
        # View specific session
        log_file = Path(sys.argv[1])
        print_session_report(log_file)
    else:
        # List all sessions
        sessions = list_sessions()
        if sessions:
            print("\n📁 Available Sessions:")
            for s in sessions[:10]:
                print(f"  {s.name}")
            print(f"\nTo view a session: python trade_logger.py {sessions[0]}")
        else:
            print("No sessions found. Run the bot first to generate logs.")
