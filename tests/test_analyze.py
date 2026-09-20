# -*- coding: utf-8 -*-
"""analyze.py 的验收测试（标准库 unittest，零依赖）。

运行： uv run python -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import analyze  # noqa: E402  （路径修正后再导入）


def make_args(**overrides) -> Namespace:
    base = dict(sla_dict=dict(analyze.DEFAULT_SLA), split_ratio=0.5, min_cluster=3, quiet=True)
    base.update(overrides)
    return Namespace(**base)


def rows_to_result(rows, **overrides):
    tickets, warnings = analyze.build_tickets(rows)
    warnings = warnings + analyze.check_data_contract(tickets)
    result = analyze.build_result(tickets, warnings, make_args(**overrides), ROOT / "task5_tickets.json")
    return tickets, result


def load_fixture_rows():
    with open(ROOT / "task5_tickets.json", encoding="utf-8") as fh:
        return json.load(fh)


class TestLoading(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = load_fixture_rows()
        cls.tickets, cls.result = rows_to_result(cls.rows)

    def test_fixture_loaded_completely(self):
        self.assertEqual(len(self.tickets), 50)
        self.assertEqual(self.result["meta"]["total"], 50)
        self.assertEqual(self.result["meta"]["window"]["start"], "2024-06-01")
        self.assertEqual(self.result["meta"]["window"]["end"], "2024-06-11")

    def test_daily_counts_and_peak(self):
        daily = self.result["dimensions"]["time"]["daily"]
        self.assertEqual(daily["2024-06-01"], 3)
        self.assertEqual(daily["2024-06-10"], 6)
        self.assertEqual(self.result["dimensions"]["time"]["peak_day"], "2024-06-10")
        self.assertEqual(sum(daily.values()), 50)

    def test_missing_field_warns_but_survives(self):
        rows = [dict(row) for row in self.rows]
        rows[0].pop("satisfaction")
        tickets, result = rows_to_result(rows)
        self.assertEqual(len(tickets), 50)
        self.assertIsNone(tickets[0].satisfaction)
        self.assertTrue(any("满意度" in w for w in result["warnings"]))
        self.assertLessEqual(result["dimensions"]["satisfaction"]["n"], 49)

    def test_unknown_category_is_kept(self):
        rows = [dict(row) for row in self.rows]
        rows[1]["category"] = "新分类-测试"
        _, result = rows_to_result(rows)
        self.assertIn("新分类-测试", result["dimensions"]["category"]["counts"])

    def test_bad_date_is_skipped_with_warning(self):
        rows = [dict(row) for row in self.rows]
        rows[2]["created_at"] = "2024/06/01 09:15"
        tickets, result = rows_to_result(rows)
        self.assertEqual(len(tickets), 49)
        self.assertTrue(any("创建时间" in w for w in result["warnings"]))

    def test_duplicate_ticket_id_is_deduped(self):
        rows = [dict(row) for row in self.rows]
        rows.append(dict(rows[0]))
        tickets, result = rows_to_result(rows)
        self.assertEqual(len(tickets), 50)
        self.assertTrue(any("重复" in w for w in result["warnings"]))

    def test_empty_input_produces_skeleton(self):
        tickets, result = rows_to_result([])
        self.assertEqual(tickets, [])
        self.assertEqual(result["meta"]["total"], 0)
        markdown = analyze.render_markdown(result)
        self.assertIn("输入数据为空", markdown)
        self.assertTrue(analyze.render_html(result).startswith("<!DOCTYPE html>"))

    def test_channel_contract_warning(self):
        _, result = rows_to_result(self.rows)
        self.assertTrue(any("邮件" in w for w in result["warnings"]))

    def test_unresolved_with_hours_is_reported(self):
        _, result = rows_to_result(self.rows)
        self.assertTrue(any("已挂起时长" in w for w in result["warnings"]))


class TestMetrics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = load_fixture_rows()
        cls.tickets, cls.result = rows_to_result(cls.rows)
        cls.dims = cls.result["dimensions"]

    def test_share_shift_payment(self):
        self.assertAlmostEqual(self.dims["category"]["share_shift_pp"]["支付问题"], 26.1, places=1)
        self.assertAlmostEqual(self.dims["category"]["early_share"]["支付问题"], 0.1579, places=3)
        self.assertAlmostEqual(self.dims["category"]["late_share"]["支付问题"], 0.4194, places=3)

    def test_high_priority_shift(self):
        self.assertEqual(self.dims["priority"]["counts"]["高"], 31)
        self.assertAlmostEqual(self.dims["priority"]["high_share_early"], 0.4737, places=3)
        self.assertAlmostEqual(self.dims["priority"]["high_share_late"], 0.7097, places=3)
        self.assertLess(self.result["stats"]["high_share_binom_p"], 0.05)

    def test_resolution_percentiles_use_nearest_rank(self):
        refund = self.dims["resolution"]["by_category"]["退款退货"]
        self.assertEqual(refund["p50"], 12)
        self.assertEqual(refund["p90"], 96)
        self.assertAlmostEqual(refund["mean"], 30.0, places=1)

    def test_sla_breach_only_counts_resolved(self):
        breaches = [b["ticket_id"] for b in self.dims["resolution"]["breaches"]]
        self.assertEqual(sorted(breaches), ["T001", "T006", "T007", "T024"])
        open_breaches = [b["ticket_id"] for b in self.dims["resolution"]["open_breaches"]]
        self.assertEqual(sorted(open_breaches), ["T019", "T031", "T039", "T042", "T047"])
        unresolved_ids = [t.ticket_id for t in self.tickets if not t.is_resolved]
        self.assertTrue(set(breaches).isdisjoint(set(unresolved_ids)))

    def test_backlog_numbers(self):
        backlog = self.dims["backlog"]
        self.assertEqual(backlog["unresolved"], 8)
        self.assertEqual(backlog["high_unresolved"], 7)
        self.assertEqual(backlog["aging"][0]["ticket_id"], "T031")
        self.assertEqual(backlog["aging"][0]["hours"], 120)

    def test_satisfaction_headline(self):
        sat = self.dims["satisfaction"]
        self.assertEqual(sat["n"], 50)
        self.assertAlmostEqual(sat["mean"], 2.36, places=2)
        self.assertAlmostEqual(sat["low_rate"], 0.54, places=2)

    def test_custom_sla_changes_breach_count(self):
        _, strict = rows_to_result(self.rows, sla_dict={"高": 4, "中": 8, "低": 12})
        base = self.result["dimensions"]["resolution"]["breach_count"]
        self.assertGreater(strict["dimensions"]["resolution"]["breach_count"], base)


class TestClustersAndAnomalies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = load_fixture_rows()
        cls.tickets, cls.result = rows_to_result(cls.rows)
        cls.clusters = {c["key"]: c for c in cls.result["clusters"]}

    def test_payment_cluster_membership(self):
        pay = self.clusters["pay_state_mismatch"]
        self.assertEqual(pay["size"], 12)
        for tid in ("T008", "T030", "T035", "T046", "T050"):
            self.assertIn(tid, pay["tickets"])
        self.assertEqual(pay["recurrence_hits"], 2)

    def test_cluster_membership_is_exclusive_and_complete(self):
        clustered = [tid for c in self.result["clusters"] for tid in c["tickets"]]
        self.assertEqual(len(clustered), len(set(clustered)), "同一工单不得被计入两个簇")
        self.assertEqual(len(clustered) + len(self.result["unclustered"]), 50)

    def test_multi_match_is_disclosed(self):
        self.assertTrue(self.result["multi_match"])
        for row in self.result["multi_match"]:
            self.assertTrue(row["assigned"] and row["also_matched"])

    def test_cohesion_baseline_and_lift(self):
        baseline = self.result["stats"]["cohesion_baseline"]
        self.assertGreater(baseline, 0)
        self.assertGreater(self.clusters["pay_state_mismatch"]["cohesion_lift"], 2)
        incoherent = [a for a in self.result["anomalies"] if "候选簇" in a["title"]]
        self.assertTrue(incoherent, "应至少有一个因一致性存疑而降级的候选簇")
        self.assertTrue(all(a["level"] == "观察" for a in incoherent))

    def test_top_anomaly_is_payment_surge(self):
        top = self.result["anomalies"][0]
        self.assertEqual(top["level"], "高危")
        self.assertIn("支付问题突增", top["title"])
        self.assertIn("T046", top["tickets"])
        self.assertLess(top["p_value"], 0.001)

    def test_levels_and_required_fields(self):
        levels = self.result["level_counts"]
        self.assertEqual(levels["高危"], 4)
        self.assertEqual(levels["观察"], 3)
        for a in self.result["anomalies"]:
            for field in ("id", "type", "level", "score", "evidence", "why", "action", "verify"):
                self.assertIn(field, a)
                self.assertTrue(str(a[field]).strip())

    def test_reports_contain_key_conclusions(self):
        markdown = analyze.render_markdown(self.result)
        html = analyze.render_html(self.result)
        for text in ("支付问题", "退款退货", "未归类", "SLA"):
            self.assertIn(text, markdown)
            self.assertIn(text, html)


class TestStatistics(unittest.TestCase):
    def test_poisson_tail_bounds_and_monotonicity(self):
        self.assertEqual(analyze.poisson_tail(0, 3.0), 1.0)
        self.assertEqual(analyze.poisson_tail(3, 0.0), 0.0)
        values = [analyze.poisson_tail(k, 3.6) for k in range(1, 12)]
        self.assertTrue(all(0.0 <= v <= 1.0 for v in values))
        self.assertEqual(values, sorted(values, reverse=True))

    def test_binom_tail_known_values(self):
        self.assertAlmostEqual(analyze.binom_tail(0, 10, 0.5), 1.0, places=6)
        self.assertAlmostEqual(analyze.binom_tail(11, 10, 0.5), 0.0, places=6)
        self.assertAlmostEqual(analyze.binom_tail(9, 10, 0.5), 0.0107421875, places=6)

    def test_percentile_nearest_rank(self):
        self.assertEqual(analyze.percentile([5, 1, 3, 2, 4], 0.5), 3)
        self.assertEqual(analyze.percentile([5, 1, 3, 2, 4], 0.9), 5)
        self.assertIsNone(analyze.percentile([], 0.5))

    def test_bigrams_ignore_punctuation(self):
        self.assertEqual(analyze.bigrams("重复扣款！"), analyze.bigrams("重复扣款"))


class TestDeterminism(unittest.TestCase):
    def test_same_input_same_metrics(self):
        rows = load_fixture_rows()
        _, first = rows_to_result(copy.deepcopy(rows))
        _, second = rows_to_result(copy.deepcopy(rows))
        first.pop("meta"), second.pop("meta")
        first.pop("_clusters"), second.pop("_clusters")
        self.assertEqual(json.dumps(first, ensure_ascii=False, sort_keys=True),
                         json.dumps(second, ensure_ascii=False, sort_keys=True))

    def test_json_export_is_serializable(self):
        rows = load_fixture_rows()
        _, result = rows_to_result(rows)
        export = {k: v for k, v in result.items() if k not in ("charts", "_clusters")}
        text = json.dumps(export, ensure_ascii=False)
        self.assertIn("pay_state_mismatch", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
