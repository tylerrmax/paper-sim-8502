# -*- coding: utf-8 -*-
"""P0-1 登记期拒绝测试（2026-11-23 红队集中修复第 4 项，用户定案）。

问题（自红队复核 + 冻结包遗留待裁定项）：修复前
register_rule_implementation 允许显式 version 与实现自报版本
不一致的登记（条目 impl_version 恒取自报值，错配只在 judge
判定时由 IMPL_MISMATCH 双边核对兜底）。关键值（版本）可被
调用方在登记期错位，与自红队「关键值不得被调用方覆盖」
口径不符。

定案修复：显式 version != 实现自报版本 -> 登记期 ValueError
直接拒绝，登记表不落条目。judge 侧双边核对保持不变（对
直写污染登记表等表外错配继续兜底，见既有 rule_binding /
p0_1_version_binding 两文件的构造迁移）。

构造变更登记（断言零改动）：
- test_market_first_rule_binding.py::
  test_impl_mismatch_impl_impersonates_version
- test_market_first_p0_1_version_binding.py::
  test_v1_impl_cannot_impersonate_v2_via_explicit_version
两处原「API 冒充登记」构造改为直写 RULE_IMPLEMENTATIONS
污染，judge 拒绝断言与覆盖意图逐字保留。
"""
import pytest

from market_first import validation

RID = "mf-judge-test"


class _Rule:
    def __init__(self, version):
        self.version = version

    def evaluate(self, scope, metrics):
        return "passed", "fake rule"


@pytest.fixture(autouse=True)
def _clean_impls():
    yield
    for key in [k for k in validation.RULE_IMPLEMENTATIONS
                if k[0] == RID]:
        validation.RULE_IMPLEMENTATIONS.pop(key, None)


def test_register_v2_impl_as_v1_rejected():
    """v2 实现以 version='v1' 冒充登记：登记期拒绝、不落条目。"""
    with pytest.raises(ValueError):
        validation.register_rule_implementation(
            RID, _Rule("v2"), version="v1")
    assert (RID, "v1") not in validation.RULE_IMPLEMENTATIONS
    assert (RID, "v2") not in validation.RULE_IMPLEMENTATIONS


def test_register_v1_impl_as_v2_rejected():
    """镜像：v1 实现以 version='v2' 冒充登记同样拒绝。"""
    with pytest.raises(ValueError):
        validation.register_rule_implementation(
            RID, _Rule("v1"), version="v2")
    assert (RID, "v2") not in validation.RULE_IMPLEMENTATIONS
    assert (RID, "v1") not in validation.RULE_IMPLEMENTATIONS


def test_register_explicit_version_without_self_report_rejected():
    """实现无自报版本、调用方单方面指定 version：同样拒绝
    （版本不得由调用方单方决定）。"""
    class _NoVersion:
        def evaluate(self, scope, metrics):
            return "passed", "fake rule"
    with pytest.raises(ValueError):
        validation.register_rule_implementation(
            RID, _NoVersion(), version="v9")
    assert (RID, "v9") not in validation.RULE_IMPLEMENTATIONS


def test_register_matching_explicit_version_allowed():
    """正常路径：显式 version 与自报一致 -> 登记成功。"""
    key = validation.register_rule_implementation(
        RID, _Rule("v1"), version="v1")
    assert key == (RID, "v1")
    entry = validation.RULE_IMPLEMENTATIONS[(RID, "v1")]
    assert entry["impl_version"] == "v1"
    assert entry["impl_key"] == RID


def test_register_default_version_unchanged():
    """回归对照：不传 version 仍按实现自报版本登记。"""
    key = validation.register_rule_implementation(RID, _Rule("v3"))
    assert key == (RID, "v3")
    assert validation.RULE_IMPLEMENTATIONS[(RID, "v3")][
        "impl_version"] == "v3"
