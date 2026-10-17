#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradingView 级别图表：基于 Lightweight Charts（TradingView 开源库）
生成可嵌入 Streamlit 的 HTML，含 K线 + MA20 + 成交量 + 关键位线 + 信号标记
参考：用户截图的机构版布局 + TradingView 交互
图表库 JS 已内嵌到本仓库（engine/lightweight-charts.standalone.js，
v4.2.0，Apache-2.0），不再依赖 unpkg CDN，国内打开也能秒加载。
"""
import json
import os

_LWC_PATH = os.path.join(os.path.dirname(__file__),
                         "lightweight-charts.standalone.js")
with open(_LWC_PATH, encoding="utf-8") as _f:
    _LWC_JS = _f.read()  # 内嵌：免 CDN，国内直连可用


def kline_html(symbol, bars, ma_periods=(5, 10, 20, 30, 60), price_lines=None, markers=None,
               height=420):
    """生成单标的 K线 HTML（专业版：多均线+成交量+关键位线+信号标记）。

    bars: [(date, open, high, low, close, volume), ...] 正序
    ma_periods: 均线周期元组，默认 (5,10,20,30,60)
    price_lines: [{"price": float, "title": str, "color": str}, ...]
                 如成本线、止损线、止盈线
    markers: [{"time": "YYYY-MM-DD", "position": "belowBar"/"aboveBar",
               "shape": "arrowUp"/"arrowDown", "text": str, "color": str}, ...]
    """
    # K线数据
    candles = [
        {"time": d, "open": o, "high": h, "low": l, "close": c}
        for d, o, h, l, c, v in bars
    ]
    # 成交量
    vols = [
        {"time": d, "value": v or 0,
         "color": "rgba(46,189,133,0.5)" if c >= o else "rgba(246,70,93,0.5)"}
        for d, o, h, l, c, v in bars
    ]
    # 多均线
    closes = [c for _, _, _, _, c, _ in bars]
    ma_colors = {5: "#f0b90b", 10: "#1890ff", 20: "#e91e63", 30: "#00bcd4", 60: "#ff9800"}
    ma_series = {}
    for period in ma_periods:
        data = []
        for i in range(len(closes)):
            if i >= period - 1:
                data.append({
                    "time": bars[i][0],
                    "value": round(sum(closes[i - period + 1:i + 1]) / period, 4),
                })
        ma_series[period] = data

    price_lines = price_lines or []
    markers = markers or []

    # 转义供 JS 用
    j_candles = json.dumps(candles)
    j_vols = json.dumps(vols)
    j_pl = json.dumps(price_lines)
    j_mk = json.dumps(markers)
    # 多均线 JS
    ma_js_parts = []
    for period in ma_periods:
        j_ma = json.dumps(ma_series[period])
        color = ma_colors.get(period, "#888888")
        ma_js_parts.append(
            f"var ma{period} = chart.addLineSeries({{ color: '{color}', lineWidth: 1, "
            f"title: 'MA{period}', priceLineVisible: false, lastValueVisible: false }});\n"
            f"  ma{period}.setData({j_ma});"
        )
    ma_js = "\n".join(ma_js_parts)

    return f"""<div id="tv_{symbol}" style="height:{height}px;"></div>
<script>{_LWC_JS}</script>
<script>
(function() {{
  var el = document.getElementById('tv_{symbol}');
  var chart = LightweightCharts.createChart(el, {{
    width: el.clientWidth, height: {height},
    layout: {{ background: {{ color: '#0d1117' }}, textColor: '#8a8f9c',
               fontFamily: 'ui-monospace, monospace' }},
    grid: {{ vertLines: {{ color: 'rgba(30,37,50,0.5)' }},
             horzLines: {{ color: 'rgba(30,37,50,0.5)' }} }},
    crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
    rightPriceScale: {{ borderColor: '#1e2532' }},
    timeScale: {{ borderColor: '#1e2532', timeVisible: false }},
  }});
  var candles = chart.addCandlestickSeries({{
    upColor: '#f6465d', downColor: '#2ebd85',
    wickUpColor: '#f6465d', wickDownColor: '#2ebd85',
    borderVisible: false,
  }});
  candles.setData({j_candles});
{ma_js}
  // 成交量（底部叠加）
  var vol = chart.addHistogramSeries({{
    priceScaleId: 'vol', priceFormat: {{ type: 'volume' }},
  }});
  chart.priceScale('vol').applyOptions({{ scaleMargins: {{ top: 0.85, bottom: 0 }} }});
  vol.setData({j_vols});
  // 关键位线
  var pls = {j_pl};
  pls.forEach(function(p) {{
    candles.createPriceLine({{ price: p.price, title: p.title,
      color: p.color || '#ff9800', lineWidth: 1, lineStyle: 2,
      axisLabelVisible: true }});
  }});
  // 信号标记
  if ({j_mk}.length) candles.setMarkers({j_mk});
  chart.timeScale().fitContent();
  window.addEventListener('resize', function() {{
    chart.applyOptions({{ width: el.clientWidth }});
  }});
}})();
</script>"""
