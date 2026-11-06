# -*- coding: utf-8 -*-
"""
P1 自动重试（sync_8502_github.py 重试层）故障注入测试。

覆盖：
(i)   两次 ConnectionReset 后成功 → 共 3 次尝试、成功、留痕完整；
(ii)  HTTP 403 与 GateRefusal → 恰好 1 次尝试、立即失败、不重试；
(iii) PUT 发出后断线但远端已生效 → 重试前 GET 核对、不再重复 PUT；
(iv)  持续连接失败 → 4 次尝试后抛原异常且有 retry_exhausted 记录；
(v)   退避间隔单调递增且封顶（假 sleeper 断言，不真睡）；
(vi)  429/403 限流语义分类：带 rate-limit 终止语义 → 不可重试；
      普通 429 → 可重试；HTTP 503 → 可重试。

全部以假传输注入，不碰真实网络与生产远端；同步模块经桩 gh_api 载入，
SYNC8502_SRC 指向临时目录，重试日志写入临时文件。
"""
import importlib.util
import io
import json
import os
import sys
import types
import urllib.error
from email.message import Message

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import release_gate as gate  # noqa: E402

SYNC_PATH = os.path.join(ROOT, "sync_8502_github.py")


def _load_sync(monkeypatch, tmp_path):
    """以桩 gh_api 载入 sync 脚本（传输随后由各测试 monkeypatch 注入）。"""
    monkeypatch.setitem(sys.modules, "gh_api",
                        types.SimpleNamespace(api=lambda *a, **k: {}))
    monkeypatch.setenv("SYNC8502_SRC", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "sync_8502_github_retry_under_test", SYNC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sync_mod(monkeypatch, tmp_path):
    mod = _load_sync(monkeypatch, tmp_path)
    sleeps = []
    monkeypatch.setattr(mod, "_sleep", sleeps.append)
    log_path = tmp_path / "sync_retry_log.jsonl"
    monkeypatch.setattr(mod, "_RETRY_LOG_PATH", str(log_path))
    mod._test_sleeps = sleeps
    mod._test_log_path = log_path
    return mod


def _read_log(mod):
    if not mod._test_log_path.exists():
        return []
    return [json.loads(line)
            for line in mod._test_log_path.read_text(
                encoding="utf-8").splitlines() if line.strip()]


class FakeTransport:
    """按脚本逐项响应的假传输：异常实例 → 抛出；可调用 → 执行；其余原样返回。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, method, path, data=None):
        self.calls.append((method, path))
        assert self.script, f"假传输脚本已耗尽：{method} {path}"
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        if callable(action):
            return action(method, path, data)
        return action


def _http_error(code, body=b"", headers=None):
    hdrs = Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "msg", hdrs, io.BytesIO(body))


# (i) 两次连接重置后成功
def test_get_retries_twice_then_succeeds(sync_mod, monkeypatch):
    fake = FakeTransport([ConnectionResetError("reset by peer"),
                          ConnectionResetError("reset by peer"),
                          {"sha": "abc123"}])
    monkeypatch.setattr(sync_mod, "_api_transport", fake)
    out = sync_mod.api("GET", "/repos/r/contents/a.txt")
    assert out == {"sha": "abc123"}
    assert len(fake.calls) == 3
    assert sync_mod._test_sleeps and len(sync_mod._test_sleeps) == 2
    events = _read_log(sync_mod)
    failed = [e for e in events if e["event"] == "attempt_failed"]
    assert len(failed) == 2
    assert all(e["category"] == "connection" and e["will_retry"] is True
               and e["attempt"] in (1, 2) and e["api_path"]
               for e in failed)
    assert failed[0]["ts"] and failed[0]["method"] == "GET"
    success = [e for e in events if e["event"] == "retry_success"]
    assert len(success) == 1 and success[0]["attempt"] == 3


# (ii) 确定性错误恰好一次尝试
def test_http_403_not_retried(sync_mod, monkeypatch):
    fake = FakeTransport([_http_error(403, b'{"message":"Forbidden"}')])
    monkeypatch.setattr(sync_mod, "_api_transport", fake)
    with pytest.raises(urllib.error.HTTPError):
        sync_mod.api("GET", "/repos/r/contents/a.txt")
    assert len(fake.calls) == 1
    assert sync_mod._test_sleeps == []
    events = _read_log(sync_mod)
    assert len(events) == 1
    assert events[0]["event"] == "attempt_failed"
    assert events[0]["category"] == "http_403"
    assert events[0]["will_retry"] is False


def test_gate_refusal_not_retried(sync_mod, monkeypatch):
    fake = FakeTransport([gate.GateRefusal("发布门禁拒绝：指纹异常")])
    monkeypatch.setattr(sync_mod, "_api_transport", fake)
    with pytest.raises(gate.GateRefusal):
        sync_mod.api("PUT", "/repos/r/contents/db/sim.db", {"x": 1})
    assert len(fake.calls) == 1
    assert sync_mod._test_sleeps == []
    events = _read_log(sync_mod)
    assert len(events) == 1 and events[0]["will_retry"] is False
    assert events[0]["category"] == "non_retryable"


# (iii) PUT 结果未知 → 核对远端已生效，不重复 PUT
def test_put_outcome_unknown_reconciled_no_second_put(sync_mod, monkeypatch):
    state = {"sha": "oldblob"}

    def put_then_drop(method, path, data):
        state["sha"] = "targetblob"  # PUT 已在远端生效，随后连接断开
        raise ConnectionResetError("response lost")

    def get_contents(method, path, data):
        return {"sha": state["sha"]}

    fake = FakeTransport([put_then_drop, get_contents])
    monkeypatch.setattr(sync_mod, "_api_transport", fake)
    resp, reconciled = sync_mod._put_file_contents(
        "app.py", "owner/repo", {"message": "m", "content": "eA=="},
        "targetblob")
    assert reconciled is True
    put_calls = [c for c in fake.calls if c[0] == "PUT"]
    assert len(put_calls) == 1
    events = _read_log(sync_mod)
    rec = [e for e in events if e["event"] == "idempotent_reconciled"]
    assert len(rec) == 1
    assert rec[0]["file"] == "app.py" and rec[0]["repo"] == "owner/repo"
    assert rec[0]["attempt"] == 1


def test_put_still_missing_retries_and_succeeds(sync_mod, monkeypatch):
    state = {"sha": "oldblob"}

    def put_drop_not_landed(method, path, data):
        raise ConnectionResetError("request lost")  # 未生效

    def get_contents(method, path, data):
        return {"sha": state["sha"]}

    def put_ok(method, path, data):
        state["sha"] = "targetblob"
        return {"commit": {"sha": "deadbeefcafe"}}

    fake = FakeTransport([put_drop_not_landed, get_contents, put_ok])
    monkeypatch.setattr(sync_mod, "_api_transport", fake)
    resp, reconciled = sync_mod._put_file_contents(
        "app.py", "owner/repo", {"message": "m", "content": "eA=="},
        "targetblob")
    assert reconciled is False
    assert resp["commit"]["sha"] == "deadbeefcafe"
    assert len([c for c in fake.calls if c[0] == "PUT"]) == 2


# (iv)+(v) 持续失败耗尽 + 退避单调递增封顶
def test_retry_exhausted_after_four_attempts(sync_mod, monkeypatch):
    fake = FakeTransport([ConnectionResetError("reset")] * 4)
    monkeypatch.setattr(sync_mod, "_api_transport", fake)
    with pytest.raises(ConnectionResetError):
        sync_mod.api("GET", "/repos/r/contents/a.txt")
    assert len(fake.calls) == 4
    events = _read_log(sync_mod)
    exhausted = [e for e in events if e["event"] == "retry_exhausted"]
    assert len(exhausted) == 1 and exhausted[0]["attempt"] == 4
    delays = sync_mod._test_sleeps
    assert len(delays) == 3
    assert delays[0] < delays[1] < delays[2]  # 单调递增
    assert 20.0 <= delays[0] and delays[2] <= 180.0 * 1.1  # 基准与封顶


# (vi) 错误分类口径
def test_error_classification(sync_mod):
    cls = sync_mod._classify_error
    assert cls(ConnectionResetError()) == ("connection", True)
    assert cls(TimeoutError()) == ("connection", True)
    assert cls(_http_error(503)) == ("http_503", True)
    assert cls(_http_error(404)) == ("http_404", False)
    assert cls(_http_error(422)) == ("http_422", False)
    # 429 带明确 rate-limit 终止语义 → 不可重试
    assert cls(_http_error(
        429, b'{"message":"API rate limit exceeded for user"}')
    ) == ("rate_limited", False)
    # GitHub 以 403 返回限流 → 同样不可重试
    assert cls(_http_error(
        403, b'{"message":"You have triggered an abuse detection '
             b'mechanism; rate limit"}')) == ("rate_limited", False)
    # 配额已耗尽（响应头）→ 不可重试
    assert cls(_http_error(
        403, b'{}', headers={"X-RateLimit-Remaining": "0"})
    ) == ("rate_limited", False)
    # 普通 429（无终止语义）→ 可重试
    assert cls(_http_error(429, b'{"message":"slow down"}')
               ) == ("http_429", True)
    # 未分类异常 fail-closed
    assert cls(ValueError("bad json")) == ("non_retryable", False)
    assert cls(gate.GateRefusal("x")) == ("non_retryable", False)
