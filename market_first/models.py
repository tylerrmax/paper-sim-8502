"""Market-First 共享数据模型与常量。

契约：
- 时间戳一律 ISO 字符串：
  datetime.now().astimezone().isoformat(timespec='seconds')
- JSON 列存 json.dumps(obj, ensure_ascii=False)
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 生产库路径（只读使用；连接时须用 sqlite3 URI mode=ro）
PROD_SIM_DB = "/home/hatch/workspace/paper-trading-sim/db/sim.db"

# 母板 16 只 + BTC（候选池独立于母板，此常量仅作参照/标注用）
MOTHERBOARD_SYMBOLS = (
    "002446",
    "688305",
    "300827",
    "600150",
    "002639",
    "002472",
    "002812",
    "HK9660",
    "HK0354",
    "002192",
    "000938",
    "002837",
    "300499",
    "002185",
    "002747",
    "600522",
    "BTCUSDT",
)


@dataclass
class DiscoveryEvent:
    """一次发现事件：某来源在某时点发现某标的。

    market: classify_market(symbol) 的结果，如 SH/SZ/BJ/CB/HK/UNKNOWN
    discovered_at: ISO 字符串时间戳
    price_at_discovery / price_data_time: 发现时基准价及其数据时间，
        无可靠价格时为 None（不得以 0 或模拟价填充）
    evidence: 来源证据 dict，落库时以 JSON 存入 evidence_json
    """

    market: str
    symbol: str
    name: str
    source_id: str
    discovered_at: str
    trigger_condition: str
    price_at_discovery: float | None = None
    price_data_time: str | None = None
    evidence: dict = field(default_factory=dict)
