#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradingView 级别图表：基于 Lightweight Charts（TradingView 开源库）
生成可嵌入 Streamlit 的 HTML，含 K线 + MA20 + 成交量 + 关键位线 + 信号标记
参考：用户截图的机构版布局 + TradingView 交互
"""
import json


def kline_html(symbol, bars, ma_period=20, price_lines=None, markers=None,
               height=420):
    """生成单标的 K线 HTML。

    bars: [(date, open, high, low, close, volume), ...] 正序
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
    # MA
    closes = [c for _, _, _, _, c, _ in bars]
    ma_data = []
    for i in range(len(closes)):
        if i >= ma_period - 1:
            ma_data.append({
                "time": bars[i][0],
                "value": round(sum(closes[i - ma_period + 1:i + 1]) / ma_period, 4),
            })

    price_lines = price_lines or []
    markers = markers or []

    # 转义供 JS 用
    j_candles = json.dumps(candles)
    j_vols = json.dumps(vols)
    j_ma = json.dumps(ma_data)
    j_pl = json.dumps(price_lines)
    j_mk = json.dumps(markers)

    return f"""<div id="tv_{symbol}" style="height:{height}px;"></div>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
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
    upColor: '#2ebd85', downColor: '#f6465d',
    wickUpColor: '#2ebd85', wickDownColor: '#f6465d',
    borderVisible: false,
  }});
  candles.setData({j_candles});
  // MA20
  var ma = chart.addLineSeries({{ color: '#f0b90b', lineWidth: 2,
                                  title: 'MA{ma_period}' }});
  ma.setData({j_ma});
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
