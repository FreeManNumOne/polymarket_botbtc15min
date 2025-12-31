# Polymarket 15m Up/Down 套利 Bot（Legged Arb）

这个项目实现了一个 **YES/NO 互补套利（legged arbitrage）** 的交易机器人，面向 Polymarket 的 **BTC/ETH 15 分钟 Up/Down** 周期市场：

- 在 **NEUTRAL** 状态同时挂 **YES** 与 **NO** 的折价买单（“捡便宜”）
- 一旦一边成交（变成 **LEGGED_YES/LEGGED_NO**），立刻去补另一边（对冲腿）
- 当 **YES + NO 总成本 < 1.00 - min_profit** 时进入 **LOCKED**，理论上锁定结算收益

> 重要：这是一个示例/研究用途的交易 bot。实盘有滑点、成交概率、链上/交易所风险与极端行情风险，请自行评估。

## 代码结构（从哪看起）

- **策略主循环**：`bot.py`（`LeggedArbBot`）
- **状态机/仓位与收益计算**：`state_machine.py`
- **下单与盘口**：
  - `order_manager.py`：`PaperOrderManager`（纸面）/ `LiveOrderManager`（实盘）
  - `PaperOrderManager` 会 **拉取真实 Polymarket order book**，但在本地模拟成交
- **市场自动发现（15m up/down）**：`market_discovery.py`
- **风控与紧急停止**：`safety.py`
- **交易日志与报表**：`trade_logger.py`
- **配置**：`config.py` + `.env`

## 安装

```bash
python3 -m pip install -r requirements.txt
```

## 配置（`.env`）

复制示例：

```bash
cp .env.example .env
```

常用字段：

- **纸面模式（推荐先跑这个）**
  - `TRADING_MODE=paper`
  - `PRIVATE_KEY` 可以留空
  - 如果你使用 `--discover` 自动找市场，`CONDITION_ID/YES_TOKEN_ID/NO_TOKEN_ID` 也可以留空
- **实盘模式**
  - `TRADING_MODE=live`
  - 必须配置 `PRIVATE_KEY`

## 运行

### 1) 列出可用 15 分钟市场

```bash
python3 main.py --list
```

### 2) 纸面交易 + 自动发现（推荐）

```bash
python3 main.py --paper --discover --asset BTC
```

### 3) 连续跑（自动滚动到下一轮市场）

```bash
python3 main.py --paper --continuous --asset BTC
```

或者跑固定时长（小时）：

```bash
python3 main.py --paper --hours 2 --asset BTC
```

### 4) 实盘（高风险）

确保 `.env` 里 `TRADING_MODE=live` 且 `PRIVATE_KEY` 已设置，然后：

```bash
python3 main.py --live --discover --asset BTC
```

## 查看交易报表

每次运行会在 `trade_logs/` 下生成 `session_*.json`。

查看某次 session：

```bash
python3 main.py --report trade_logs/session_YYYYMMDD_HHMMSS.json
```

或直接：

```bash
python3 trade_logger.py trade_logs/session_YYYYMMDD_HHMMSS.json
```

## 常用参数（风控/策略）

在 `.env` 中：

- `TARGET_MARGIN`：NEUTRAL 时挂单相对“公平价”的折价幅度（越大越保守，越不容易成交）
- `MIN_PROFIT`：补腿时要求的最小锁定利润（`cost_basis + hedge < 1 - MIN_PROFIT`）
- `STOP_LOSS_THRESHOLD`：腿仓亏损超过阈值触发紧急退出（见 `safety.py`）
- `GAMMA_STOP_MINUTES`：到期前 N 分钟停止挂单/触发紧急停止逻辑
- `POSITION_SIZE`：每条腿目标美元规模（会转换成份额：`size = POSITION_SIZE / price`）

## 单元测试

```bash
python3 -m pytest -q
```

