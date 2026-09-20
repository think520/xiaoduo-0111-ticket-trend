# -*- coding: utf-8 -*-
"""analyze.py 的验收测试（标准库 unittest，零依赖）。

运行： uv run python -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import csv
import json
import sys
import tempfile
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

    def test_alt_scope_matches_common_tooling(self):
        """全量口径（含未解决 + 线性插值）应与同行/Excel 常见结果一致。"""
        alt = self.dims["resolution"]["alt"]
        self.assertEqual(alt["n"], 50)
        self.assertAlmostEqual(alt["mean"], 19.69, places=2)
        self.assertAlmostEqual(alt["p90"], 50.4, places=1)
        refund = self.dims["resolution"]["by_category"]["退款退货"]
        self.assertAlmostEqual(refund["alt_mean"], 45.23, places=2)
        self.assertAlmostEqual(refund["alt_p90"], 96.0, places=1)

    def test_two_scope_note_is_in_report(self):
        markdown = analyze.render_markdown(self.result)
        self.assertIn("口径对照", markdown)
        self.assertIn("全量口径", markdown)

    def test_linear_vs_nearest_rank_percentile(self):
        values = [1, 2, 3, 4]
        self.assertEqual(analyze.percentile(values, 0.9), 4)        # nearest-rank
        self.assertAlmostEqual(analyze.percentile_linear(values, 0.9), 3.7, places=2)  # 线性插值
        self.assertAlmostEqual(analyze.percentile_linear([1, 2, 3, 4, 5], 0.5), 3.0, places=2)


class TestTimeAndHourly(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = load_fixture_rows()
        cls.tickets, cls.result = rows_to_result(cls.rows)
        cls.t = cls.result["dimensions"]["time"]

    def test_hourly_distribution(self):
        hourly = self.t["hourly"]
        self.assertEqual(len(hourly), 24)
        self.assertEqual(sum(hourly.values()), 50)
        self.assertEqual(self.t["busiest_hour"], "09")
        self.assertEqual(self.t["peak_hours"][0], "09")
        self.assertEqual(hourly["09"], 9)

    def test_rolling_three_day_comparison(self):
        roll = self.t["rolling_3d"]
        self.assertTrue(roll["available"])
        self.assertEqual(roll["recent_days"], ["2024-06-09", "2024-06-10", "2024-06-11"])
        self.assertEqual(roll["prior_days"], ["2024-06-06", "2024-06-07", "2024-06-08"])
        self.assertAlmostEqual(roll["change_pct"], 0.066, places=3)
        self.assertEqual(roll["trend"], "稳定")

    def test_rolling_threshold_behaviour(self):
        # 造一组"最近三天明显变少"的数据：前三天每天 4 条，最近三天每天 1 条
        base = load_fixture_rows()[0]
        rows = []
        for day, count in (("2024-06-03", 4), ("2024-06-04", 4), ("2024-06-05", 4),
                           ("2024-06-06", 1), ("2024-06-07", 1), ("2024-06-08", 1)):
            for _ in range(count):
                row = dict(base)
                row["ticket_id"] = f"S{len(rows) + 1:03d}"
                row["created_at"] = f"{day} 09:00"
                rows.append(row)
        _, result = rows_to_result(rows)
        roll = result["dimensions"]["time"]["rolling_3d"]
        self.assertAlmostEqual(roll["change_pct"], -0.75, places=2)
        self.assertEqual(roll["trend"], "下降")


class TestTicketRanking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = load_fixture_rows()
        cls.tickets, cls.result = rows_to_result(cls.rows)
        cls.ranks = cls.result["ticket_ranking"]

    def test_level_counts(self):
        counts = self.result["summary"]["ticket_levels"]
        self.assertEqual(counts["P1"], 5)
        self.assertEqual(counts["P2"], 5)
        self.assertEqual(counts["P3"], 13)
        self.assertEqual(len(self.ranks), 23)

    def test_top_ticket_is_worst_unresolved_refund(self):
        top = self.ranks[0]
        self.assertEqual(top["ticket_id"], "T031")
        self.assertEqual(top["level"], "P1")
        self.assertEqual(top["score"], 11)
        self.assertFalse(top["is_resolved"])
        self.assertIn("未解决", top["reasons"])
        self.assertTrue(any("SLA" in r for r in top["reasons"]))

    def test_levels_match_score_bands(self):
        for r in self.ranks:
            if r["level"] == "P1":
                self.assertGreaterEqual(r["score"], 9)
            elif r["level"] == "P2":
                self.assertTrue(7 <= r["score"] <= 8)
            else:
                self.assertTrue(5 <= r["score"] <= 6)
            self.assertTrue(r["reasons"], f"{r['ticket_id']} 缺少命中原因")

    def test_ranking_sorted_and_disclaimer_present(self):
        scores = [r["score"] for r in self.ranks]
        self.assertEqual(scores, sorted(scores, reverse=True))
        markdown = analyze.render_markdown(self.result)
        self.assertIn("工单级优先跟进清单", markdown)
        self.assertIn("不是企业正式事故等级", markdown)


class TestStrictMode(unittest.TestCase):
    def test_strict_passes_on_valid_fixture(self):
        rows = load_fixture_rows()
        tickets, _ = analyze.build_tickets(rows, strict=True)
        self.assertEqual(len(tickets), 50)

    def test_strict_rejects_bad_enum_and_type(self):
        rows = [dict(r) for r in load_fixture_rows()]
        rows[0]["priority"] = "紧急"
        rows[1]["is_resolved"] = "true"
        with self.assertRaises(analyze.DataError):
            analyze.build_tickets(rows, strict=True)
        # 宽松模式：只告警，继续跑完
        tickets, warnings = analyze.build_tickets(rows, strict=False)
        self.assertEqual(len(tickets), 50)
        self.assertTrue(any("紧急" in w for w in warnings))

    def test_strict_flag_wired_in_cli(self):
        parser = analyze.build_parser()
        args = parser.parse_args(["--strict", "--quiet"])
        self.assertTrue(args.strict)


class TestCharts(unittest.TestCase):
    def test_six_charts_generated(self):
        rows = load_fixture_rows()
        _, result = rows_to_result(rows)
        charts = result["charts"]
        self.assertEqual(len(charts), 6)
        self.assertIn("06_hourly.svg", charts)
        for name, svg in charts.items():
            self.assertTrue(svg.startswith("<svg"), name)
            self.assertTrue(svg.rstrip().endswith("</svg>"), name)

    def test_md_only_still_writes_charts(self):
        """--formats md 时也必须落盘 SVG，否则报告里的图片链接会图裂。"""
        outdir = Path(tempfile.mkdtemp(prefix="fmt_md_"))
        rows = load_fixture_rows()
        _, result = rows_to_result(rows)
        written = {p.name for p in analyze.write_outputs(result, outdir, ["md"])}
        self.assertIn("趋势分析报告.md", written)
        self.assertIn("01_daily_volume.svg", written)
        self.assertEqual(len(list((outdir / "charts").glob("*.svg"))), 6)

    def test_charts_not_written_for_json_only(self):
        outdir = Path(tempfile.mkdtemp(prefix="fmt_json_"))
        rows = load_fixture_rows()
        _, result = rows_to_result(rows)
        written = {p.name for p in analyze.write_outputs(result, outdir, ["json"])}
        self.assertEqual(written, {"metrics.json"})


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


class TestSwapDataset(unittest.TestCase):
    """换数据集能力：仓库自带示例（与 task5 完全不同）必须能跑，且 JSON 与 CSV 结果一致。

    回归背景：CSV 里所有值都是字符串，早期 `--strict` 会因为 `is_resolved: "True"`
    不是 Python bool 而报错（甚至把全部工单当成未解决），本类锁住这个行为。
    """

    def _result_for(self, path: Path):
        rows, load_warnings = analyze.load_rows(path)
        tickets, build_warnings = analyze.build_tickets(rows, strict=True)
        warnings = load_warnings + build_warnings + analyze.check_data_contract(tickets)
        return tickets, analyze.build_result(tickets, warnings, make_args(), path)

    def test_example_files_exist_and_are_small(self):
        ex = ROOT / "examples"
        self.assertTrue((ex / "tickets_example.json").exists())
        self.assertTrue((ex / "tickets_example.csv").exists())

    def test_example_json_and_csv_agree(self):
        tickets_json, r_json = self._result_for(ROOT / "examples" / "tickets_example.json")
        tickets_csv, r_csv = self._result_for(ROOT / "examples" / "tickets_example.csv")
        self.assertEqual(len(tickets_json), 24)
        self.assertEqual(len(tickets_csv), 24)
        self.assertEqual(r_json["meta"]["total"], 24)
        self.assertEqual(len(r_json["dimensions"]["category"]["counts"]), 5)
        self.assertEqual(r_json["dimensions"]["backlog"]["unresolved"], 4)
        # 同内容的 JSON 与 CSV 必须得到完全一致的指标
        self.assertEqual(r_json["dimensions"], r_csv["dimensions"])
        self.assertEqual(r_json["meta"]["days"], r_csv["meta"]["days"])

    def test_strict_accepts_string_booleans_from_csv(self):
        rows = [dict(r) for r in load_fixture_rows()]
        tmp = Path(tempfile.mkdtemp(prefix="swap_csv_")) / "t.csv"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        loaded, _ = analyze.load_rows(tmp)
        tickets, _ = analyze.build_tickets(loaded, strict=True)   # 不应抛 DataError
        self.assertEqual(len(tickets), 50)
        self.assertEqual(sum(1 for t in tickets if not t.is_resolved), 8)  # 不能被当成全部未解决


if __name__ == "__main__":
    unittest.main(verbosity=2)
