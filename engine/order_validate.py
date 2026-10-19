#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订单级校验（修订版 2026-10-18）：任何关键数据缺失或不一致时，禁止生成可执行的订单指引。

原则：缺数据一律"失败关闭"（报错），不默认放行。
阈值优先读 config/strategy.yaml（lots / display_only / universe / boards / rules / risk / fees），
读不到时才回退到本文件里的默认值。

校验项：
  标的(有行情/在股票池/非 display_only) · 账户 · 方向 · 价格(涨跌停与偏离) · 信号日期(非未来/非过期)
  交易单位(读 yaml lots；A股卖出清仓允许零股) · 数量(买卖均必填且>0)
  卖出：持仓、A股 T+1 当日买入不可卖
  买入：可用资金(含费用+滑点)、总仓位上限(读 yaml risk)、单标的占比(可选 risk.max_single_symbol_pct)

对外接口与旧版一致：validate_order(account, symbol, side, qty, price, signal_date, db_path=DB)
另增：suggest_buy_qty(account, symbol, price, db_path=DB) —— 买入数量估算（按 buy_frac × 可用现金，向下取整手，
并按总仓位上限收缩）。若你的 pipeline 有自己的仓位算法，请让 app 调 pipeline 的结果，不要用这个。
"""
import os
import sqlite3
from datetime import datetime, date

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "db", "sim.db")
CFG_PATH = os.path.join(BASE, "config", "strategy.yaml")

ACCOUNTS = ("sim_a", "sim_hk", "sim_crypto")
DEFAULT_LOT = {"sim_a": 100, "sim_hk": 100, "sim_crypto": 0.0001}
MAX_SIGNAL_AGE_DAYS = 7          # 与 pipeline 信号标记规则一致（>7天标"已过期"，2026-10-18起）
MAX_DATA_AGE_DAYS = 5            # 最新行情超过 N 个自然日未更新则警告
CRYPTO_HK_PRICE_DEV_ERR = 0.20   # 港股/加密：偏离最新收盘价超过此比例拒绝
PRICE_DEV_WARN = 0.10
DEFAULT_FEES = {"a_buy": 0.00025, "a_min_fee": 5, "hk_buy": 0.00108, "hk_min_fee": 50,
                "crypto": 0.0005, "slippage": 0.002}


def _load_cfg():
    try:
        import yaml
        with open(CFG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _norm(sym, account=None):
    """yaml 里未加引号的纯数字代码会被解析成 int（丢前导零），统一成字符串并给 A 股补齐 6 位。"""
    s = str(sym).strip() if sym is not None else ""
    if s.isdigit() and (account in (None, "sim_a")) and len(s) < 6:
        s = s.zfill(6)
    return s


def _lot_for(cfg, account, symbol):
    lots = {_norm(k): v for k, v in (cfg.get("lots") or {}).items()}
    v = lots.get(_norm(symbol, account))
    return float(v) if v else DEFAULT_LOT.get(account, 1)


def _fee_est(cfg, account, notional):
    f = {**DEFAULT_FEES, **(cfg.get("fees") or {})}
    if account == "sim_a":
        fee = max(f["a_min_fee"], notional * f["a_buy"])
    elif account == "sim_hk":
        fee = max(f["hk_min_fee"], notional * f["hk_buy"])
    else:
        fee = notional * f["crypto"]
    return fee + notional * f["slippage"]


def _latest_total(con, account):
    return con.execute(
        "SELECT nav, cash, position_value FROM daily_report "
        "WHERE account=? AND symbol='TOTAL' ORDER BY date DESC LIMIT 1", (account,)).fetchone()


def _position_cap(cfg, account):
    risk = cfg.get("risk") or {}
    if account in ("sim_a", "sim_hk"):
        return float(risk.get("max_stock_position_pct", 70)) / 100.0
    return float(risk.get("max_crypto_position_pct", 100)) / 100.0


def _limit_pct(cfg, symbol):
    boards = {_norm(k): v for k, v in (cfg.get("boards") or {}).items()}
    lp = (cfg.get("rules") or {}).get("limit_pct") or {"main": 0.1, "star": 0.2}
    board = boards.get(_norm(symbol), "main")
    return float(lp.get(board, 0.1))


def suggest_buy_qty(account, symbol, price, db_path=DB):
    """买入数量估算：buy_frac × 可用现金，向下取整手；再按总仓位上限收缩。算不出返回 0。"""
    cfg = _load_cfg()
    if not price or price <= 0:
        return 0
    con = sqlite3.connect(db_path)
    try:
        row = _latest_total(con, account)
    finally:
        con.close()
    if not row or not row[0] or row[0] <= 0:
        return 0
    nav, cash, pos = row[0], row[1] or 0, row[2] or 0
    frac = float((cfg.get("sizing") or {}).get("buy_frac", 0.5))
    budget = cash * frac
    headroom = _position_cap(cfg, account) * nav - pos
    budget = min(budget, max(headroom, 0))
    lot = _lot_for(cfg, account, symbol)
    unit = lot * price
    if unit <= 0:
        return 0
    n_lots = int(budget // (unit * (1 + _fee_est(cfg, account, unit) / unit)))
    qty = n_lots * lot
    return round(qty, 8) if lot < 1 else int(qty)


def validate_order(account, symbol, side, qty, price, signal_date, db_path=DB):
    """返回 (ok: bool, errors: list[str], warnings: list[str])"""
    errors, warnings = [], []
    cfg = _load_cfg()
    sym = _norm(symbol, account)
    con = sqlite3.connect(db_path)
    try:
        _validate(con, cfg, errors, warnings, account, sym, side, qty, price, signal_date)
    except Exception as e:  # 校验自身出错也必须失败关闭
        errors.append(f"校验异常，已拦截：{type(e).__name__}: {e}")
    finally:
        con.close()
    return (len(errors) == 0, errors, warnings)


def _validate(con, cfg, errors, warnings, account, sym, side, qty, price, signal_date):
    # 1. 账户 / 方向
    if account not in ACCOUNTS:
        errors.append(f"未知账户 {account}")
    if side not in ("BUY", "SELL"):
        errors.append(f"非法方向 {side}")

    # 2. 标的：有行情、在股票池、非 display_only
    last_close, last_date = None, None
    if not sym:
        errors.append("标的代码缺失")
    else:
        row = con.execute("SELECT close, date FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 1",
                          (sym,)).fetchone()
        if not row:
            errors.append(f"标的 {sym} 无行情数据")
        else:
            last_close, last_date = row[0], row[1]
        uni = {a: {_norm(x, a) for x in v} for a, v in (cfg.get("universe") or {}).items()}
        if uni and account in uni and sym not in uni[account]:
            errors.append(f"标的 {sym} 不在 {account} 股票池内")
        disp = {_norm(x) for x in (cfg.get("display_only") or [])}
        if side == "BUY" and sym in disp:
            errors.append(f"标的 {sym} 为只展示(display_only)，禁止买入")

    # 3. 价格：有效 + 涨跌停/偏离
    if not price or price <= 0:
        errors.append(f"价格无效 {price}")
    elif last_close:
        dev = abs(price - last_close) / last_close
        if account == "sim_a":
            lim = _limit_pct(cfg, sym)
            if dev > lim + 1e-4:
                errors.append(f"价格偏离最新收盘价 {dev*100:.1f}%，超出涨跌停幅度 {lim*100:.0f}%")
            elif dev > PRICE_DEV_WARN:
                warnings.append(f"价格偏离最新收盘价 {dev*100:.1f}%")
        else:
            if dev > CRYPTO_HK_PRICE_DEV_ERR:
                errors.append(f"价格偏离最新收盘价 {dev*100:.1f}%（疑似脏数据）")
            elif dev > PRICE_DEV_WARN:
                warnings.append(f"价格偏离最新收盘价 {dev*100:.1f}%")

    # 4. 行情新鲜度（警告）
    if last_date:
        try:
            age = (date.today() - datetime.strptime(str(last_date)[:10], "%Y-%m-%d").date()).days
            if age > MAX_DATA_AGE_DAYS:
                warnings.append(f"最新行情停在 {last_date}，已 {age} 天未更新")
        except ValueError:
            warnings.append(f"行情日期格式异常 {last_date}")

    # 5. 信号日期：必填、非未来、非过期
    if not signal_date:
        errors.append("信号日期缺失")
    else:
        try:
            sd = datetime.strptime(str(signal_date)[:10], "%Y-%m-%d").date()
            age = (date.today() - sd).days
            if age < 0:
                errors.append(f"信号日期 {signal_date} 在未来")
            elif age > MAX_SIGNAL_AGE_DAYS:
                errors.append(f"信号已过期：{signal_date}（{age} 天前，上限 {MAX_SIGNAL_AGE_DAYS} 天）")
        except ValueError:
            errors.append(f"信号日期格式无效 {signal_date}")

    # 6. 数量：买卖均必填且 > 0
    if not qty or qty <= 0:
        errors.append(f"{'买入' if side == 'BUY' else '卖出'}数量缺失或为 0（必须带真实数量）")
        return  # 数量无效，后续依赖数量的校验无意义

    # 持仓（卖出与单标的占比都要用）
    pos = con.execute("SELECT qty FROM positions WHERE account=? AND symbol=?", (account, sym)).fetchone()
    avail = float(pos[0]) if pos and pos[0] else 0.0

    # 7. 交易单位（卖出且恰为清仓时，A 股允许零股）
    lot = _lot_for(cfg, account, sym)
    if lot >= 1 and abs(qty % lot) > 1e-9:
        clearing = side == "SELL" and account == "sim_a" and abs(qty - avail) < 1e-9
        if not clearing:
            errors.append(f"数量 {qty} 不是 {lot:g} 的整数倍")

    # 8. 卖出：持仓 + T+1
    if side == "SELL":
        if avail <= 0:
            errors.append("无持仓，不能卖出")
        elif qty > avail + 1e-9:
            errors.append(f"卖出 {qty} 超过持仓 {avail:g}")
        elif account == "sim_a" and (cfg.get("rules") or {}).get("a_t1", True):
            try:
                bought_today = con.execute(
                    "SELECT COALESCE(SUM(qty),0) FROM fills WHERE account=? AND symbol=? AND side='BUY' AND date=?",
                    (account, sym, date.today().isoformat())).fetchone()[0] or 0
            except sqlite3.Error:
                bought_today = 0
                warnings.append("无法读取 fills，T+1 未校验")
            if qty > avail - bought_today + 1e-9:
                errors.append(f"T+1：当日买入 {bought_today:g} 股不可卖，可卖 {max(avail - bought_today, 0):g}")

    # 9. 买入：资金 + 总仓位 + 单标的占比
    if side == "BUY" and price and price > 0:
        tot = _latest_total(con, account)
        if not tot or not tot[0] or tot[0] <= 0:
            errors.append("账户净值/现金缺失，无法校验资金与仓位（失败关闭）")
        else:
            nav, cash, cur_pos = tot[0], tot[1] or 0, tot[2] or 0
            notional = qty * price
            need = notional + _fee_est(cfg, account, notional)
            if need > cash + 1e-9:
                errors.append(f"需资金 {need:.2f}（含费用与滑点）超过可用 {cash:.2f}")
            cap = _position_cap(cfg, account)
            new_ratio = (cur_pos + notional) / nav
            if new_ratio > cap + 1e-9:
                errors.append(f"总仓位 {new_ratio*100:.1f}% 超过上限 {cap*100:.0f}%（当前持仓 {cur_pos:.2f}）")
            sym_cap = (cfg.get("risk") or {}).get("max_single_symbol_pct")
            sym_ratio = (avail * price + notional) / nav
            if sym_cap is not None and sym_ratio > float(sym_cap) / 100 + 1e-9:
                errors.append(f"单标的占比 {sym_ratio*100:.1f}% 超过上限 {float(sym_cap):g}%")
            elif notional / nav > 0.2:
                warnings.append(f"单笔占比 {notional/nav*100:.1f}% 超过20%")
