import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app import ablation
from eval.run_ablation import VARIANTS, report


TOTALS = ("visit_path_geo_m", "cross_day_outlier_count", "visits", "visit_minutes",
          "spatial_located_visits", "spatial_complete_days", "visit_minutes_known_count")
WEIGHTED = ("within_day_dispersion_m", "spatial_coordinate_coverage")


def result(case="synthetic", **fields):
    return {"case": case, "variant": "full", "repeat": 1, "outcome": "selection",
            "error": "", "has_plan": True, **fields}


class SpatialReport(unittest.TestCase):
    def render(self, rows):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            original = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
            (out / "results.jsonl").write_text(original, encoding="utf-8")
            captured = io.StringIO()
            with patch("eval.run_ablation.run_one", side_effect=AssertionError("run_one called")), \
                    patch("eval.run_ablation.Planner", side_effect=AssertionError("Planner created")), \
                    patch("eval.run_ablation.replay.Cassette", side_effect=AssertionError("cassette created")), \
                    redirect_stdout(captured):
                report(out)
            summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            markdown = (out / "summary.md").read_text(encoding="utf-8")
            with (out / "results.csv").open(encoding="utf-8-sig", newline="") as output:
                exported = list(csv.DictReader(output))
            self.assertEqual((out / "results.jsonl").read_text(encoding="utf-8"), original)
            self.assertEqual(captured.getvalue(), markdown)
        return summary, markdown, exported

    def test_no_clustering_is_the_fifth_variant_and_has_its_own_summary(self):
        self.assertEqual(list(VARIANTS), ["full", "no_reviewer", "no_dedupe", "no_code_checks", "no_clustering"])
        self.assertEqual(ablation.parse("no_clustering"), {"clustering"})
        summary, markdown, _ = self.render([result(visits=2), result(variant="no_clustering", visits=3)])
        self.assertEqual(summary["full"]["sum"]["visits"], 2)
        self.assertEqual(summary["no_clustering"]["sum"]["visits"], 3)
        self.assertEqual(summary["no_clustering"]["plans"], 1)
        self.assertIn("关掉空间分日聚类", markdown)

    def test_legacy_rows_keep_missing_spatial_fields_null(self):
        summary, markdown, exported = self.render([result(visits=2), result("other", visits=3)])
        item = summary["full"]
        for name in (*TOTALS, *WEIGHTED):
            if name == "visits":
                continue
            self.assertIsNone(item["mean"][name])
            self.assertEqual(item["valid_runs"][name], 0)
            if name in TOTALS:
                self.assertIsNone(item["sum"][name])
            self.assertNotIn(name, exported[0])
        self.assertEqual(item["sum"]["visits"], 5)
        self.assertIn("| visit 路径直线距离（米） | 合计 | - | 0 |", markdown)

    def test_missing_visits_are_not_fabricated_as_zero(self):
        summary, _, _ = self.render([result()])
        item = summary["full"]
        self.assertIsNone(item["sum"]["visits"])
        self.assertIsNone(item["mean"]["visits"])
        self.assertIsNone(item["dup_visit_rate"])
        self.assertEqual(item["valid_runs"]["visits"], 0)

    def test_all_explicit_nulls_stay_null_with_zero_valid_runs(self):
        fields = dict.fromkeys((*TOTALS, *WEIGHTED))
        summary, _, exported = self.render([result(**fields), result("other", **fields)])
        for name in fields:
            self.assertIsNone(summary["full"]["mean"][name])
            self.assertEqual(summary["full"]["valid_runs"][name], 0)
            self.assertEqual(exported[0][name], "")
        for name in TOTALS:
            self.assertIsNone(summary["full"]["sum"][name])
        for name in WEIGHTED:
            self.assertNotIn(name, summary["full"]["sum"])

    def test_known_zero_distances_and_minutes_are_valid(self):
        row = result(visits=1, visit_path_geo_m=0, within_day_dispersion_m=0, cross_day_outlier_count=0,
                     spatial_coordinate_coverage=1, visit_minutes=0, spatial_located_visits=1,
                     spatial_complete_days=1, visit_minutes_known_count=1)
        summary, markdown, exported = self.render([row])
        item = summary["full"]
        for name in ("visit_path_geo_m", "cross_day_outlier_count", "visit_minutes"):
            self.assertEqual(item["sum"][name], 0)
            self.assertEqual(item["valid_runs"][name], 1)
        self.assertEqual(item["mean"]["within_day_dispersion_m"], 0)
        self.assertEqual(item["valid_runs"]["within_day_dispersion_m"], 1)
        self.assertEqual(exported[0]["visit_minutes"], "0")
        self.assertIn("| 已知游玩分钟（visit_minutes） | 合计 | 0 | 1 |", markdown)

    def test_zero_coverage_is_valid_but_zero_weight_is_not(self):
        summary, _, _ = self.render([result(visits=3, spatial_located_visits=0, spatial_coordinate_coverage=0,
                                           within_day_dispersion_m=None, spatial_complete_days=0,
                                           visit_minutes_known_count=0)])
        item = summary["full"]
        self.assertEqual(item["mean"]["spatial_coordinate_coverage"], 0)
        self.assertEqual(item["valid_runs"]["spatial_coordinate_coverage"], 1)
        self.assertIsNone(item["mean"]["within_day_dispersion_m"])
        self.assertEqual(item["valid_runs"]["within_day_dispersion_m"], 0)
        self.assertEqual(item["sum"]["spatial_complete_days"], 0)

    def test_coverage_uses_ratio_of_totals_and_dispersion_uses_located_visits(self):
        rows = [result(visits=4, spatial_located_visits=1, spatial_coordinate_coverage=0.25,
                       within_day_dispersion_m=1000, visit_path_geo_m=100, cross_day_outlier_count=0,
                       visit_minutes=60, spatial_complete_days=1, visit_minutes_known_count=1),
                result("other", repeat=2, visits=6, spatial_located_visits=6, spatial_coordinate_coverage=1,
                       within_day_dispersion_m=100, visit_path_geo_m=900, cross_day_outlier_count=2,
                       visit_minutes=300, spatial_complete_days=2, visit_minutes_known_count=5)]
        summary, markdown, exported = self.render(rows)
        item = summary["full"]
        self.assertAlmostEqual(item["mean"]["within_day_dispersion_m"], 1600 / 7)
        self.assertAlmostEqual(item["mean"]["spatial_coordinate_coverage"], 0.7)
        expected = {"visit_path_geo_m": 1000, "cross_day_outlier_count": 2, "visits": 10,
                    "visit_minutes": 360, "spatial_located_visits": 7, "spatial_complete_days": 3,
                    "visit_minutes_known_count": 6}
        for name, value in expected.items():
            self.assertEqual(item["sum"][name], value)
        for name in (*TOTALS, *WEIGHTED):
            self.assertEqual(item["valid_runs"][name], 2)
        self.assertIn("合计/加权均值/比例", markdown)
        self.assertIn("有效样本（运行数）", markdown)
        self.assertIn("| visit 路径直线距离（米） | 合计 | 1000 | 2 |", markdown)
        self.assertIn("| 景点访问数（visits） | 合计 | 10 | 2 |", markdown)
        self.assertIn("| 已知游玩分钟（visit_minutes） | 合计 | 360 | 2 |", markdown)
        self.assertIn("| 日内分散度（米） | 定位数加权均值 | 228.57 | 2 |", markdown)
        self.assertIn("| 坐标覆盖率 | 比例（合计定位数/合计 visits） | 0.70 | 2 |", markdown)
        self.assertEqual([row["within_day_dispersion_m"] for row in exported], ["1000", "100"])
        self.assertEqual([row["spatial_coordinate_coverage"] for row in exported], ["0.25", "1"])

    def test_missing_metrics_and_denominators_do_not_dilute_weighted_values(self):
        rows = [result(visits=4, spatial_located_visits=2, spatial_coordinate_coverage=0.5,
                       within_day_dispersion_m=80, visit_path_geo_m=0, visit_minutes=90),
                result("legacy", visits=100),
                result("missing", visits=50, spatial_located_visits=20, spatial_coordinate_coverage=None,
                       within_day_dispersion_m=None, visit_minutes=None),
                result("no-denominator", within_day_dispersion_m=999, spatial_coordinate_coverage=1),
                result("no-numerator", visits=10, spatial_coordinate_coverage=1)]
        summary, _, _ = self.render(rows)
        item = summary["full"]
        self.assertEqual(item["mean"]["within_day_dispersion_m"], 80)
        self.assertEqual(item["mean"]["spatial_coordinate_coverage"], 0.5)
        for name in ("visit_path_geo_m", "visit_minutes", *WEIGHTED):
            self.assertEqual(item["valid_runs"][name], 1)
        self.assertEqual(item["sum"]["visit_minutes"], 90)
        self.assertEqual(item["sum"]["visits"], 164)

    def test_empty_weighted_denominators_produce_null(self):
        rows = [result(visits=0, spatial_located_visits=0, spatial_coordinate_coverage=0,
                       within_day_dispersion_m=0),
                result("missing", visits=None, spatial_located_visits=None,
                       spatial_coordinate_coverage=1, within_day_dispersion_m=100)]
        summary, _, _ = self.render(rows)
        for name in WEIGHTED:
            self.assertIsNone(summary["full"]["mean"][name])
            self.assertEqual(summary["full"]["valid_runs"][name], 0)

    def test_failed_runs_do_not_enter_spatial_totals_or_valid_samples(self):
        rows = [result(visits=2, visit_path_geo_m=0, visit_minutes=60, total_violations=3,
                       escaped_violations=3, confirmable=1, model_calls=2),
                result("failed", has_plan=False, outcome="failed", error="synthetic failure",
                       visits=100, visit_path_geo_m=90000, visit_minutes=10000, total_violations=100,
                       spatial_located_visits=100, spatial_coordinate_coverage=1,
                       within_day_dispersion_m=9000, model_calls=4)]
        summary, markdown, exported = self.render(rows)
        item = summary["full"]
        self.assertEqual((item["runs"], item["plans"]), (2, 1))
        self.assertEqual(item["sum"]["visits"], 2)
        self.assertEqual(item["sum"]["visit_path_geo_m"], 0)
        self.assertEqual(item["sum"]["visit_minutes"], 60)
        self.assertEqual(item["valid_runs"]["visit_minutes"], 1)
        self.assertIsNone(item["mean"]["spatial_coordinate_coverage"])
        self.assertEqual(item["valid_runs"]["spatial_coordinate_coverage"], 0)
        self.assertEqual(item["sum"]["total_violations"], 3)
        self.assertEqual(item["sum"]["escaped_violations"], 3)
        self.assertEqual(item["sum"]["confirmable"], 1)
        self.assertEqual(item["mean"]["model_calls"], 3)
        self.assertEqual(len(exported), 2)
        self.assertIn("synthetic failure", markdown)


if __name__ == "__main__":
    unittest.main()
