#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""决策台仓位风险计算单元测试；不依赖 Streamlit 或私有数据库。"""
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("decision_desk", os.path.join(ROOT, "engine", "decision_desk.py"))
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DecisionSizingTests(unittest.TestCase):
    def test_position_size_respects_risk_cash_and_exposure(self):
        got = MODULE.calculate_position_size(100000, 50000, 50000, 25.0, 24.0, 1.0, 100, 70)
        self.assertTrue(got["valid"])
        self.assertEqual(got["risk_qty"], 1000)
        self.assertEqual(got["cash_qty"], 2000)
        self.assertEqual(got["exposure_qty"], 800)
        self.assertEqual(got["quantity"], 800)
        self.assertAlmostEqual(got["estimated_loss"], 800.0)

    def test_stops_must_be_below_reference_price(self):
        got = MODULE.calculate_position_size(100000, 50000, 0, 25.0, 25.0, 1.0, 100, 70)
        self.assertFalse(got["valid"])

    def test_no_cash_means_no_position(self):
        got = MODULE.calculate_position_size(100000, 0, 0, 25.0, 24.0, 1.0, 100, 70)
        self.assertTrue(got["valid"])
        self.assertEqual(got["quantity"], 0)

    def test_fractional_lot_for_crypto(self):
        got = MODULE.calculate_position_size(20000, 10000, 0, 100000.0, 99000.0, 1.0, 0.00001, 100)
        self.assertTrue(got["valid"])
        self.assertLessEqual(got["estimated_loss"], 200.0 + 1e-6)

    def test_invalid_inputs_fail_closed(self):
        got = MODULE.calculate_position_size(0, 1000, 0, 25.0, 24.0, 1.0, 100, 70)
        self.assertFalse(got["valid"])

    def test_non_finite_inputs_fail_closed(self):
        got = MODULE.calculate_position_size(100000, 50000, 0, float("nan"), 24.0, 1.0, 100, 70)
        self.assertFalse(got["valid"])


if __name__ == "__main__":
    unittest.main()
