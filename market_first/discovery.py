"""Market Discovery：可插拔发现适配器。

首个真实源 = 机构龙虎榜（复用 engine.inst_lhb 通路）。
适配器必须声明覆盖范围，不得以局部冒充全市场。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from market_first.models import PROD_SIM_DB, DiscoveryEvent


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def classify_market(symbol) -> str:
    """按代码前缀归类市场。

    '6' 开头 -> 'SH'；'0'/'3' 开头 -> 'SZ'；
    '92'/'83'/'87'/'43' 开头 -> 'BJ'；
    '11'/'12'/'13' 开头 -> 'CB'（可转债）；
    'HK' 开头 -> 'HK'；其余 -> 'UNKNOWN'。
    """
    s = str(symbol or "").strip()
    if not s:
        return "UNKNOWN"
    if s.upper().startswith("HK"):
        return "HK"
    if s.startswith("6"):
        return "SH"
    if s.startswith(("0", "3")):
        return "SZ"
    if s.startswith(("92", "83", "87", "43")):
        return "BJ"
    if s.startswith(("11", "12", "13")):
        return "CB"
    return "UNKNOWN"


class InstLhbAdapter:
    """机构龙虎榜发现适配器。

    优先（prefer='live'）实时抓取 engine.inst_lhb.fetch_inst_daily；
    实时不可用（state != 'normal' 或无行）时回退只读查询生产库
    inst_lhb_daily 表的已落库行。只保留 inst_net > 0 的标的。
    """

    COVERAGE = (
        "A股龙虎榜当日上榜标的（机构专用席位聚合口径）；"
        "覆盖范围为当日触发龙虎榜披露的股票，"
        "不是沪深京全部上市公司，不得称全市场扫描"
    )

    TRIGGER = "龙虎榜机构专用席位净买入>0（inst_net>0）"

    def __init__(self, sim_db_path=PROD_SIM_DB, prefer="live"):
        self.sim_db_path = sim_db_path
        self.prefer = prefer
        self.last_obtained_via: str | None = None
        self.last_run_note: str | None = None

    # -- 内部：两种取得方式统一成行 dict 列表 -------------------------
    def _fetch_live(self, date_str):
        """返回 (rows, state, reason)；导入/调用失败按非 normal 处理。"""
        try:
            from engine.inst_lhb import fetch_inst_daily
        except Exception as e:  # noqa: BLE001 - 导入失败回退 stored
            return [], "unavailable", "实时抓取导入失败：%s" % e
        try:
            agg_rows, _raw_rows, state, reason = fetch_inst_daily(date_str)
        except Exception as e:  # noqa: BLE001 - 抓取异常回退 stored
            return [], "failed", "实时抓取失败：%s" % e
        return list(agg_rows or []), state, reason

    def _fetch_stored(self, date_str):
        """只读查询 sim_db 的 inst_lhb_daily 该日行。"""
        uri = "file:%s?mode=ro" % self.sim_db_path
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT date, symbol, name, inst_buy, inst_sell, inst_net,"
                " seat_buy_records, seat_sell_records, window, trade_id,"
                " fetched_at FROM inst_lhb_daily WHERE date=?",
                (date_str,),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    # -- 主入口 ---------------------------------------------------------
    def discover(self, date_str, price_fn=None) -> list[DiscoveryEvent]:
        rows: list[dict] = []
        via: str | None = None
        live_note = ""

        if self.prefer == "live":
            live_rows, state, reason = self._fetch_live(date_str)
            live_note = "live: state=%s %s" % (state, reason or "")
            if state == "normal" and live_rows:
                rows, via = live_rows, "live"
            else:
                rows = self._fetch_stored(date_str)
                if rows:
                    via = "stored"
        else:
            rows = self._fetch_stored(date_str)
            if rows:
                via = "stored"

        if via is None or not rows:
            self.last_obtained_via = None
            self.last_run_note = (
                "数据日期 %s：live 与 stored 均无数据%s"
                % (date_str, ("；" + live_note) if live_note else "")
            )
            return []

        self.last_obtained_via = via
        self.last_run_note = (
            "数据日期 %s：via=%s 取得 %d 行（过滤 inst_net>0 前）%s；覆盖：%s"
            % (date_str, via, len(rows),
               ("；" + live_note) if live_note else "", self.COVERAGE)
        )

        now_iso = _now_iso()
        events: list[DiscoveryEvent] = []
        for row in rows:
            try:
                inst_net = float(row.get("inst_net") or 0)
            except (TypeError, ValueError):
                continue
            if inst_net <= 0:
                continue
            symbol = str(row.get("symbol") or "")
            if not symbol:
                continue
            if via == "stored":
                fetched_at = row.get("fetched_at")
            else:
                fetched_at = now_iso
            evidence = {
                "inst_buy": row.get("inst_buy"),
                "inst_sell": row.get("inst_sell"),
                "inst_net": row.get("inst_net"),
                "seat_buy_records": row.get("seat_buy_records"),
                "seat_sell_records": row.get("seat_sell_records"),
                "trade_id": row.get("trade_id"),
                "window": row.get("window"),
                "data_date": date_str,
                "obtained_via": via,
                "coverage": self.COVERAGE,
                "fetched_at": fetched_at,
            }
            price = None
            price_time = None
            if price_fn is not None:
                try:
                    price, price_time = price_fn(symbol, date_str)
                except Exception as e:  # noqa: BLE001 - 价格失败留 None 并记录
                    evidence["price_error"] = str(e)
                    price, price_time = None, None
            events.append(DiscoveryEvent(
                market=classify_market(symbol),
                symbol=symbol,
                name=row.get("name") or "",
                source_id="inst_lhb_%s" % via,
                discovered_at=now_iso,
                trigger_condition=self.TRIGGER,
                price_at_discovery=price,
                price_data_time=price_time,
                evidence=evidence,
            ))
        return events
