# -*- coding: utf-8 -*-
"""
第二层 P3 实时行情心跳监控测试（假时钟 + 假响应，不触真实网络，
不写生产 reports —— 心跳路径经环境变量注入 tmp 文件）。
"""
import json
import os
from datetime import datetime, timedelta

import pytest

from engine import heartbeat, realtime

CST = heartbeat.CST
FRI_0959 = datetime(2026, 11, 6, 9, 59, tzinfo=CST)   # 周五盘中
FRI_1000 = datetime(2026, 11, 6, 10, 0, tzinfo=CST)
FRI_0200 = datetime(2026, 11, 6, 2, 0, tzinfo=CST)    # 周五凌晨（窗口外）


@pytest.fixture(autouse=True)
def hb_path(tmp_path, monkeypatch):
    p = str(tmp_path / "heartbeat.jsonl")
    monkeypatch.setenv(heartbeat.ENV_PATH, p)
    realtime._HB_FAILURES = 0
    return p


def _tx_payload(ts14="20261106095900", price="6.93"):
    parts = ["1", "盛路通信", "002446", price, "6.78"] + ["0"] * 25 + [ts14]
    return 'v_sz002446="%s";' % "~".join(parts)


def _events(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ① 正常响应 → 四阶段按序落账、status 正常
def test_normal_fetch_records_all_stages_in_order(hb_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "_now", lambda: FRI_0959)
    monkeypatch.setattr(realtime, "_http_get", lambda url: _tx_payload())
    out = realtime.fetch_cn_hk(["002446"])
    assert out["002446"]["price"] == 6.93

    evts = _events(hb_path)
    assert [e["stage"] for e in evts] == ["request", "response",
                                          "valid", "persisted"]
    assert all(e["ok"] for e in evts)
    assert all(e["source"] == "cn_hk" for e in evts)

    st = heartbeat.status(now=FRI_1000, path=hb_path)
    assert st["sources"]["cn_hk"]["anomaly"] is False
    assert st["sources"]["cn_hk"]["last_valid"] is not None
    assert st["sources"]["cn_hk"]["valid_age_min"] == 1.0


# ② HTTP 200 但内容垃圾 → response ok、valid 失败、窗口内 status 异常
def test_http200_garbage_is_not_valid(hb_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "_now", lambda: FRI_0959)
    monkeypatch.setattr(realtime, "_http_get",
                        lambda url: "this is not a quote payload")
    out = realtime.fetch_cn_hk(["002446"])
    assert out == {}

    evts = _events(hb_path)
    by_stage = {e["stage"]: e["ok"] for e in evts}
    assert by_stage["request"] is True
    assert by_stage["response"] is True   # 传输成功 ≠ 行情有效
    assert by_stage["valid"] is False
    assert "persisted" not in by_stage

    st = heartbeat.status(now=FRI_1000, path=hb_path)
    assert st["sources"]["cn_hk"]["anomaly"] is True


def test_http200_zero_price_is_not_valid(hb_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "_now", lambda: FRI_0959)
    monkeypatch.setattr(realtime, "_http_get",
                        lambda url: _tx_payload(price="0"))
    assert realtime.fetch_cn_hk(["002446"]) == {}
    by_stage = {e["stage"]: e["ok"] for e in _events(hb_path)}
    assert by_stage["valid"] is False


# ③ 源时间戳为未来 → valid 失败；取数行为本身不被心跳改变
def test_future_source_timestamp_fails_valid(hb_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "_now", lambda: FRI_0959)
    monkeypatch.setattr(realtime, "_http_get",
                        lambda url: _tx_payload(ts14="20990101095900"))
    out = realtime.fetch_cn_hk(["002446"])
    assert out["002446"]["price"] == 6.93  # 取数/回退行为不变，只告警
    evts = _events(hb_path)
    by_stage = {e["stage"]: e["ok"] for e in evts}
    assert by_stage["valid"] is False
    assert "persisted" not in by_stage
    valid_evt = [e for e in evts if e["stage"] == "valid"][0]
    assert "未来" in valid_evt["detail"]


# ④ 窗口外无 valid 不报异常；加密按 30 分钟口径判定
def test_outside_window_no_anomaly_and_crypto_tolerance(hb_path):
    st = heartbeat.status(now=FRI_0200, path=hb_path)  # 空日志
    assert st["sources"]["cn_hk"]["anomaly"] is False

    t0 = datetime(2026, 11, 6, 1, 0, tzinfo=CST)
    heartbeat.record("crypto", "valid", True, ts=t0, path=hb_path)
    st = heartbeat.status(now=t0 + timedelta(minutes=20), path=hb_path)
    assert st["sources"]["crypto"]["anomaly"] is False
    st = heartbeat.status(now=t0 + timedelta(minutes=31), path=hb_path)
    assert st["sources"]["crypto"]["anomaly"] is True


# ⑤ JSONL 只追加，不覆盖旧记录
def test_jsonl_appends_without_overwriting(hb_path):
    heartbeat.record("cn_hk", "request", True, ts=FRI_0959, path=hb_path)
    with open(hb_path, encoding="utf-8") as f:
        first_line = f.readline()
    heartbeat.record("cn_hk", "valid", True, ts=FRI_0959, path=hb_path)
    with open(hb_path, encoding="utf-8") as f:
        lines = f.readlines()
    assert len(lines) == 2
    assert lines[0] == first_line
    evts = heartbeat.read_events(path=hb_path)
    assert [e["stage"] for e in evts] == ["request", "valid"]


# ⑥ 埋点写失败时 fetch 仍正常返回
def test_heartbeat_write_failure_never_breaks_fetch(hb_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "_now", lambda: FRI_0959)

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(heartbeat, "record", boom)
    monkeypatch.setattr(realtime, "_http_get", lambda url: _tx_payload())
    before = realtime._HB_FAILURES
    out = realtime.fetch_cn_hk(["002446"])
    assert out["002446"]["price"] == 6.93
    assert realtime._HB_FAILURES > before
    assert not os.path.exists(hb_path)  # 写失败未落账，但取数不受影响


# CLI：异常 exit 1 / 正常 exit 0（只读）
def test_cli_exit_codes(hb_path, monkeypatch):
    from tools.heartbeat_check import main
    # 空日志：周五盘中，两源均应判异常 → exit 1
    assert main(["--path", hb_path, "--now", "2026-11-06T10:00:00+08:00"]) == 1
    # 补齐两源新鲜 valid → exit 0
    heartbeat.record("cn_hk", "valid", True, ts=FRI_0959, path=hb_path)
    heartbeat.record("crypto", "valid", True, ts=FRI_0959, path=hb_path)
    assert main(["--path", hb_path, "--now", "2026-11-06T10:00:00+08:00"]) == 0
