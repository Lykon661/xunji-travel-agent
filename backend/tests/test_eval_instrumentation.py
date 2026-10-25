import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from eval.run_ablation import count_model_calls, report


class ReviewReport:
    pass


class OtherSchema:
    pass


class CallInstrumentation(unittest.IsolatedAsyncioTestCase):
    async def test_schema_counts_seconds_and_positional_or_keyword_payloads(self):
        result = object()
        ask = AsyncMock(return_value=result)
        row = {}
        counted = count_model_calls(ask,row)
        with patch("eval.run_ablation.perf_counter",side_effect=[10,10.14,20,20.26,30,30.05,40,40.25]):
            self.assertIs(await counted(ReviewReport,"full",{}),result)
            await counted(ReviewReport,"incremental",payload={"review_mode":"changed_dates"})
            await counted(schema=OtherSchema,instruction="other",payload=None)
            await counted(OtherSchema,"changed payload",{"review_mode":"changed_dates"})
        self.assertEqual(ask.await_count,4)
        self.assertEqual(row["calls_by_schema"],{"ReviewReport":2,"OtherSchema":2})
        self.assertEqual(row["seconds_by_schema"],{"ReviewReport":0.4,"OtherSchema":0.3})
        self.assertEqual((row["incremental_reviews"],row["full_reviews"]),(2,1))
        self.assertEqual(row["model_calls"],4)
        self.assertAlmostEqual(row["model_seconds"],0.7)
        self.assertEqual(ask.await_args_list[1].kwargs["payload"],{"review_mode":"changed_dates"})

    async def test_failed_calls_are_counted_and_exceptions_are_preserved(self):
        ask = AsyncMock(side_effect=RuntimeError("synthetic failure"))
        row = {}
        counted = count_model_calls(ask,row)
        with patch("eval.run_ablation.perf_counter",side_effect=[1,2.2]):
            with self.assertRaisesRegex(RuntimeError,"synthetic failure"):
                await counted(ReviewReport,"full",{})
        self.assertEqual(row["calls_by_schema"],{"ReviewReport":1})
        self.assertEqual(row["seconds_by_schema"],{"ReviewReport":1.2})
        self.assertEqual((row["full_reviews"],row["incremental_reviews"]),(1,0))

    async def test_small_durations_are_accumulated_before_rounding(self):
        row = {}
        counted = count_model_calls(AsyncMock(),row)
        with patch("eval.run_ablation.perf_counter",side_effect=[0,0.04,1,1.04,2,2.04]):
            for _ in range(3):
                await counted(OtherSchema,"small",{})
        self.assertEqual(row["seconds_by_schema"],{"OtherSchema":0.1})


class InstrumentationReports(unittest.TestCase):
    def summarize(self, rows):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            out = Path(directory)
            results = out / "results.jsonl"
            results.write_text("".join(json.dumps(row)+"\n" for row in rows),encoding="utf-8")
            original = results.read_bytes()
            with redirect_stdout(io.StringIO()):
                report(out)
            self.assertEqual(results.read_bytes(),original)
            return json.loads((out / "summary.json").read_text(encoding="utf-8")),(out / "summary.md").read_text(encoding="utf-8")

    def test_group_averages_include_legacy_missing_values_as_zero(self):
        old = {"case":"old","variant":"full","repeat":1,"outcome":"selection","has_plan":True}
        rows = [old,{**old,"case":"new","calls_by_schema":{"ReviewReport":4},"seconds_by_schema":{"ReviewReport":8.2},
                     "incremental_reviews":2,"full_reviews":2},
                {**old,"case":"other","variant":"no_reviewer","calls_by_schema":{"OtherSchema":2},
                 "seconds_by_schema":{"OtherSchema":3.0}}]
        summary,markdown = self.summarize(rows)
        full = summary["full"]["mean"]
        self.assertEqual(full["calls_by_schema"],{"OtherSchema":0.0,"ReviewReport":2.0})
        self.assertEqual(full["seconds_by_schema"],{"OtherSchema":0.0,"ReviewReport":4.1})
        self.assertEqual((full["incremental_reviews"],full["full_reviews"]),(1.0,1.0))
        self.assertEqual(summary["no_reviewer"]["mean"]["calls_by_schema"]["ReviewReport"],0)
        self.assertIn("平均调用次数",markdown)
        self.assertIn("平均秒数",markdown)
        self.assertIn("增量审查次数",markdown)
        self.assertIn("全量审查次数",markdown)

    def test_legacy_directory_without_any_instrumentation_can_be_reported(self):
        summary,markdown = self.summarize([{"case":"legacy","variant":"full","repeat":1,
                                           "outcome":"selection","has_plan":True}])
        means = summary["full"]["mean"]
        self.assertEqual(means["calls_by_schema"],{})
        self.assertEqual(means["seconds_by_schema"],{})
        self.assertEqual((means["incremental_reviews"],means["full_reviews"]),(0,0))
        self.assertIn("旧结果缺失埋点字段按0汇总",markdown)
