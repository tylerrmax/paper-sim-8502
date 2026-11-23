# -*- coding: utf-8 -*-
"""RT-3 对抗测试（2026-11-23 自红队补正）：InstFlowModule 时间锚。

自红队口径（冻结包 07_SELF_REDTEAM RT-3）：InstFlowModule 此前
以来源声称的 discovered_at 选「最新」事件定因子符号——把任一
事件的声称日期改到未来即可使其恒为「最新」，因子符号完全由
声称日期控制（探针 C5：subtotal +8.0 <-> -8.0 随声称日期翻
转）。修复后：排序锚只用服务器入库时间 created_at（与 P0-5
单一锚一致），缺失时以事件 id 序兜底；discovered_at 只随
选中事件留痕，不作选择依据。

对抗形态：
- 伪造未来 discovered_at（created_at 更早）不得翻转符号；
- 同一事件集只改声称日期，subtotal 必须不变（声称不变性）；
- 正对照：created_at 最晚者胜出，哪怕其声称日期最早；
- created_at 全缺失时以 id 序兜底选最新。
"""
import json

from market_first.research import InstFlowModule


def _ev(eid, discovered, created, net):
    ev = {"id": eid, "market": "SZ", "symbol": "002623",
          "source_id": "inst_lhb_live",
          "discovered_at": discovered,
          "evidence_json": json.dumps({
              "inst_buy": net if net > 0 else 0.0,
              "inst_sell": -net if net < 0 else 0.0,
              "inst_net": net})}
    if created is not None:
        ev["created_at"] = created
    return ev


def _subtotal(events):
    out = InstFlowModule().compute({}, [], {"inst_events": events})
    return out


def test_forged_future_discovered_at_cannot_flip_sign():
    real = _ev(1, "2026-11-10T09:00:00+08:00",
               "2026-11-10T10:00:00+08:00", 500.0)
    forged = _ev(2, "2027-01-05T09:00:00+08:00",
                 "2026-11-09T10:00:00+08:00", -900.0)
    out = _subtotal([real, forged])
    # 最新按 created_at 为真实事件（11-10 入库），符号为正
    assert out["subtotal"] == 8.0
    assert out["evidence"]["latest_selected_by"] == "created_at"
    assert out["evidence"]["latest_created_at"] == \
        "2026-11-10T10:00:00+08:00"
    assert out["evidence"]["latest_inst_net"] == 500.0


def test_claimed_date_variation_does_not_change_subtotal():
    real = _ev(1, "2026-11-10T09:00:00+08:00",
               "2026-11-10T10:00:00+08:00", 500.0)
    forged_late_claim = _ev(2, "2027-01-05T09:00:00+08:00",
                            "2026-11-09T10:00:00+08:00", -900.0)
    forged_early_claim = _ev(2, "2026-11-01T09:00:00+08:00",
                             "2026-11-09T10:00:00+08:00", -900.0)
    assert _subtotal([real, forged_late_claim])["subtotal"] == 8.0
    assert _subtotal([real, forged_early_claim])["subtotal"] == 8.0


def test_latest_selected_by_created_at_positive_control():
    # 声称日期更晚但入库更早的事件不得胜出；入库最晚者定符号。
    early_ingested = _ev(1, "2026-11-20T09:00:00+08:00",
                         "2026-11-10T10:00:00+08:00", -900.0)
    late_ingested = _ev(2, "2026-11-01T09:00:00+08:00",
                        "2026-11-15T10:00:00+08:00", 500.0)
    out = _subtotal([early_ingested, late_ingested])
    assert out["subtotal"] == 8.0
    assert out["evidence"]["latest_inst_net"] == 500.0
    # 声称值只留痕：记录的是选中事件自己的 discovered_at
    assert out["evidence"]["latest_discovered_at"] == \
        "2026-11-01T09:00:00+08:00"


def test_missing_created_at_falls_back_to_id_order():
    older = _ev(1, "2026-11-20T09:00:00+08:00", None, -900.0)
    newer = _ev(2, "2026-11-01T09:00:00+08:00", None, 500.0)
    out = _subtotal([older, newer])
    assert out["subtotal"] == 8.0
    assert out["evidence"]["latest_inst_net"] == 500.0
