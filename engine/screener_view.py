#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""选股页「三重校验·优质标的」展示（只读，不做计算；计算见 engine/screener.py）。

app.py 只需在选股板块加三行：
    from engine.screener_view import render_triple_screen
    render_triple_screen(st, DB, CFG, BASE, SYMBOL_CN)
引擎异常时失败关闭：不展示任何标的，只提示异常，不影响页面其余部分。
"""
from __future__ import annotations

from engine import screener as S

_STATUS_COLOR = {S.STATUS_PASS: "#1890ff", S.STATUS_VETO: "#c77ddb",
                 S.STATUS_WAIT: "#f0b90b", S.STATUS_FAIL: "#8a8f9c"}   # 避开红绿（红绿留给涨跌）
_INPUT_CN = {"ok": "已就绪", "missing": "未提供", "stale": "已过期", "invalid": "格式错误"}


def _badge(status):
    c = _STATUS_COLOR.get(status, "#8a8f9c")
    return (f'<span style="display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;'
            f'font-weight:700;color:{c};border:1px solid {c}55;background:{c}22;">{S.STATUS_CN.get(status, status)}</span>')


def _layer_line(label, res):
    return f'{label} {_badge(res["status"])} <span style="color:#8a8f9c;font-size:13px;">{res["reasons"][0] if res["reasons"] else ""}</span>'


def render_triple_screen(st, db_path, cfg, base_dir, names=None):
    names = names or {}
    st.markdown("### 🎯 三重校验·优质标的")
    st.caption("量化技术面 × X 博主新闻市场面 × 母板板块共振。三层全部通过才会出现在这里，宁缺毋滥；"
               "风控优先于消息，利好不能单独入选，可验证利空一票否决。仅为筛选流程，不构成投资建议。")
    try:
        res = S.run(db_path, cfg, base_dir)
    except Exception as e:  # 失败关闭
        st.error(f"三重校验引擎异常，本次不输出任何标的：{type(e).__name__}: {e}")
        return None

    rows, inp, sm = res["rows"], res["inputs"], res["summary"]
    c1, c2, c3 = st.columns(3)
    quant_ok = sum(1 for r in rows if r["quant"]["status"] == S.STATUS_PASS)
    c1.markdown(f'<div class="kpi-card"><div class="kpi-label">量化层（数据 {res["asof"]}）</div>'
                f'<div class="kpi-value">{quant_ok} / {len(rows)} 通过</div></div>', unsafe_allow_html=True)
    b = inp["briefs"]
    c2.markdown(f'<div class="kpi-card"><div class="kpi-label">新闻面（X 博主简报）</div>'
                f'<div class="kpi-value" style="font-size:18px">{_INPUT_CN[b["status"]]}</div>'
                f'<div class="kpi-label">{("截至 " + b["asof"] + "，" + str(len(b["items"])) + " 条") if b["asof"] else "缺少 data/screen_inputs/briefs.json"}</div></div>',
                unsafe_allow_html=True)
    s = inp["sectors"]
    c3.markdown(f'<div class="kpi-card"><div class="kpi-label">板块面（母板共振）</div>'
                f'<div class="kpi-value" style="font-size:18px">{_INPUT_CN[s["status"]]}</div>'
                f'<div class="kpi-label">{("截至 " + s["asof"] + "，" + str(len(s["sectors"])) + " 个板块") if s["asof"] else "缺少 data/screen_inputs/sector_board.json"}</div></div>',
                unsafe_allow_html=True)
    for src in (b, s):
        for e in src.get("errors", [])[:3]:
            st.caption(f"⚠ {e}")

    passed = [r for r in rows if r["status"] == S.STATUS_PASS]
    if not passed:
        why = []
        if b["status"] != "ok":
            why.append("新闻面输入" + _INPUT_CN[b["status"]])
        if s["status"] != "ok":
            why.append("板块面输入" + _INPUT_CN[s["status"]])
        st.markdown(
            '<div style="background:#1a1a2e;border:1px solid #1e2532;border-radius:8px;padding:14px 16px;margin:8px 0;">'
            '<b>今日无标的通过三重校验</b>（宁缺毋滥）'
            + (f'<br><span style="color:#8a8f9c;font-size:13px;">原因之一：{"；".join(why)}。输入补齐前，任何标的都不会被判为优质。</span>' if why else "")
            + f'<br><span style="color:#8a8f9c;font-size:13px;">明细：待校验 {sm["WAIT"]} · 未过关 {sm["FAIL"]} · 否决 {sm["VETO"]}</span></div>',
            unsafe_allow_html=True)
    for r in passed:
        t, rk = r["tech"], r["risk"]
        nm = f'{r["sym"]} {names.get(r["sym"], "")}'.strip()
        body = (f'<div style="background:#1a1a2e;border:1px solid {_STATUS_COLOR[S.STATUS_PASS]};border-radius:8px;'
                f'padding:14px 16px;margin:8px 0;"><b style="font-size:16px;">{nm}</b> {_badge(r["status"])}'
                f'<div style="margin-top:8px;font-size:14px;line-height:1.9;">'
                f'{_layer_line("① 量化", r["quant"])}<br>{_layer_line("② 新闻", r["news"])}<br>{_layer_line("③ 板块", r["sector"])}</div>')
        if rk:
            body += (f'<div style="margin-top:10px;padding-top:8px;border-top:1px solid #1e2532;font-size:14px;">'
                     f'<b>风险卡</b>　现价 <span class="num">{rk["px"]:.2f}</span>　止损 <span class="num">{rk["stop"]:.2f}</span>'
                     f'（{rk["stop_pct"]:.1f}%，{rk["atr_mult"]:g}×ATR）　建议数量 <span class="num">{rk["qty"]:g}</span>'
                     f'　金额 <span class="num">{rk["notional"]:,.0f}</span>　最大亏损 <span class="num">{rk["risk_amt"]:,.0f}</span>'
                     f'（占净值 {rk["risk_pct_of_nav"]:.2f}%）</div>')
        else:
            body += '<div style="margin-top:8px;color:#f0b90b;">⚠ 风险卡无法计算（缺 ATR 或净值），请勿据此建单。</div>'
        st.markdown(body + "</div>", unsafe_allow_html=True)
        with st.expander(f"证据链 · {nm}", expanded=False):
            for ev in r["news"].get("evidence", []):
                st.markdown(f'- `{ev["date"]}` **{ev["direction"]}** {ev["fact"]}　— {ev["source"]}'
                            + (f'　佐证：{ev["evidence"]}' if ev["evidence"] else "　（无可核验佐证）"))
            st.caption(f'板块：{"、".join(x["name"] for x in r["sector"].get("sectors", [])) or "—"}')

    with st.expander(f"全部标的明细（待校验 {sm['WAIT']} · 未过关 {sm['FAIL']} · 否决 {sm['VETO']}）", expanded=False):
        import pandas as pd
        st.dataframe(pd.DataFrame([{
            "标的": f'{r["sym"]} {names.get(r["sym"], "")}'.strip(),
            "结论": S.STATUS_CN[r["status"]],
            "量化": S.STATUS_CN[r["quant"]["status"]], "新闻": S.STATUS_CN[r["news"]["status"]],
            "板块": S.STATUS_CN[r["sector"]["status"]], "原因": r["why"]} for r in rows]), hide_index=True)
    return res
