"""消融开关、离线判分与录制回放：只用合成数据，不调用模型或外部接口。"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app import ablation, adapters
from app.decisions import confirmation_blockers
from app.dining import refresh_meals
from app.places import canonicalize_stops, duplicate_issues
from app.rules import check_itinerary
from eval import replay
from eval.metrics import evaluate
from eval.run_ablation import auto_answer


def visit(identity, name, poi, start, end):
    return {"id":identity,"kind":"visit","name":name,"start":start,"end":end,"source_id":"guide",
            "evidence":{"kind":"text","quote":name},"map_place":{"id":poi,"name":name,"location":"113.3,23.1"},
            "move_before":{"minutes":0}}


def plan():
    return {"days":[
        {"date":"2026-12-12","city":"广州","activities":[visit("a","广州塔","tower","09:00","11:00"),
                                                       visit("b","陈家祠","chen","10:30","12:00")],
         "meal_recommendations":[{"id":"m1","name":"点都德","meal":"lunch","map_place":{"id":"dim"}}],"gaps":[]},
        {"date":"2026-12-13","city":"广州","activities":[visit("c","小蛮腰","tower","09:00","11:00"),
                                                       visit("d","北京路步行街","road","13:00","14:00"),
                                                       visit("e","北京路","road2","15:00","16:00")],
         "meal_recommendations":[{"id":"m2","name":"点都德","meal":"dinner","map_place":{"id":"dim"}}],"gaps":[]}],
        "transport":[],"hotels":[]}


REQUIREMENTS = {"origin":"武汉","cities":["广州"],"start_date":"2026-12-12","end_date":"2026-12-13","people":2,"rooms":1,
                "budget":4000,"local_transport":"public","max_walk_per_day":8000}


class Switches(unittest.TestCase):
    def test_default_keeps_every_design_and_names_are_validated(self):
        self.assertEqual(ablation.active(),frozenset())
        self.assertEqual(ablation.parse("no_reviewer+no_dedupe"),{"reviewer","dedupe"})
        self.assertEqual(ablation.parse("full"),frozenset())
        with self.assertRaises(ValueError):
            ablation.parse("no_planner")

    def test_dedupe_switch_drops_shared_identity_and_duplicate_issues(self):
        stops = {"a":{"id":"a","city":"广州","name":"广州塔","map_place":{"id":"tower","location":"113.3,23.1"}},
                 "b":{"id":"b","city":"广州","name":"小蛮腰","map_place":{"id":"tower","location":"113.3,23.1"}}}
        merged = canonicalize_stops(stops)
        self.assertEqual(merged["a"]["canonical_place_id"],merged["b"]["canonical_place_id"])
        self.assertTrue(duplicate_issues(plan()))
        with ablation.use("no_dedupe"):
            split = canonicalize_stops(stops)
            self.assertNotEqual(split["a"]["canonical_place_id"],split["b"]["canonical_place_id"])
            self.assertEqual(duplicate_issues(plan()),[])

    def test_dedupe_switch_lets_one_restaurant_repeat_across_meals(self):
        day = lambda date: {"date":date,"city":"广州","activities":[visit("a"+date,"广州塔","tower","10:00","17:00")],"gaps":[]}
        stops = {"r":{"id":"r","city":"广州","name":"点都德","category":"dining","map_place":{"id":"dim","location":"113.3,23.1"}}}
        itinerary = {"days":[day("2026-12-12"),day("2026-12-13")]}
        refresh_meals(itinerary,stops)
        self.assertEqual(sum(len(d["meal_recommendations"]) for d in itinerary["days"]),1)
        with ablation.use("no_dedupe"):
            refresh_meals(itinerary,stops)
        self.assertGreater(sum(len(d["meal_recommendations"]) for d in itinerary["days"]),1)

    def test_code_check_switch_silences_rule_issues_and_blockers(self):
        self.assertTrue(check_itinerary(plan(),REQUIREMENTS)["issues"])
        with ablation.use("no_code_checks"):
            self.assertEqual(check_itinerary(plan(),REQUIREMENTS)["issues"],[])
            self.assertEqual(confirmation_blockers(plan(),{},REQUIREMENTS),[])


class Scoring(unittest.TestCase):
    def test_metrics_find_injected_errors_even_when_the_run_disabled_checks(self):
        candidate = {"draft":plan(),"days":[{"date":"2026-12-12","city":"广州"},{"date":"2026-12-13","city":"广州"}],
                     "review":{"status":"pass","findings":[]},"confirmation_blockers":[]}
        with ablation.use("no_dedupe+no_code_checks"):
            result = evaluate(REQUIREMENTS,candidate,{"a","b","c","d"})
        self.assertEqual(result["dup_visit"],2)      # 小蛮腰 = 广州塔（同 POI），北京路 = 北京路步行街（同名去后缀）
        self.assertEqual(result["dup_meal"],1)
        self.assertEqual(result["sourceless_visit"],1)
        self.assertEqual(result["time_conflict"],1)
        self.assertEqual(result["date_coverage_error"],0)
        self.assertEqual(result["confirmable"],1)
        self.assertEqual(result["escaped_violations"],result["total_violations"])
        self.assertGreater(result["oracle_code_blockers"],0)

    def test_known_aliases_with_different_pois_count_as_one_place(self):
        from eval.metrics import visit_duplicates
        day = {"date":"2026-12-26","city":"广州"}
        visits = [(day,visit("a","陈氏书院（陈家祠）","poi-1","09:00","10:00")),
                  ({**day,"date":"2026-12-27"},visit("b","陈家祠","poi-2","09:00","10:00")),
                  ({**day,"date":"2026-12-27"},visit("c","北京路千年古道","poi-3","11:00","12:00"))]
        self.assertEqual(visit_duplicates(visits),(0,0))
        self.assertEqual(visit_duplicates(visits,[["陈家祠","陈氏书院"],["北京路步行街","北京路"]]),(1,0))

    def test_user_constraints_are_scored_and_extraction_is_compared_by_kind(self):
        from app.constraints import merge
        from app.models import ConstraintDraft
        order = ConstraintDraft(kind="fixed_order",before="c",after="a",quote="先去小蛮腰再去广州塔").model_dump(mode="json")
        requirements = {**REQUIREMENTS,"constraints":merge([],[order],"form")}
        candidate = {"draft":plan(),"days":[{"date":"2026-12-12","city":"广州"},{"date":"2026-12-13","city":"广州"}]}
        result = evaluate(requirements,candidate,{"a","b","c","d","e"},expected_constraints=[{"kind":"fixed_order"},{"kind":"fewer_walking"}])
        self.assertEqual(result["user_constraint_violation"],1)
        self.assertEqual((result["constraints_expected"],result["constraints_extracted"],result["constraints_matched"]),(2,1,1))
        self.assertEqual(result["constraints_active_form"],1)
        self.assertNotIn("constraints_expected",evaluate(REQUIREMENTS,candidate,set()))

    def test_superseded_form_constraint_still_counts_as_extracted(self):
        from app.constraints import merge
        from app.models import ConstraintDraft
        form = ConstraintDraft(kind="daily_min_stops",count=3,quote="每天至少三个景点").model_dump(mode="json")
        constraints = merge([],[form],"form")
        constraints = merge(constraints,[{**form,"strength":"soft","quote":"少于三个也可以"}],"decision")
        result = evaluate({**REQUIREMENTS,"constraints":constraints},None,set(),expected_constraints=[{"kind":"daily_min_stops"}])
        self.assertEqual((result["constraints_extracted"],result["constraints_matched"],result["constraints_active_form"]),(1,1,0))

    def test_a_partially_overridden_form_constraint_counts_once(self):
        from app.constraints import merge
        from app.models import ConstraintDraft
        form = ConstraintDraft(kind="daily_min_stops",count=3,quote="每天至少三个景点").model_dump(mode="json")
        days = ["2026-12-26","2026-12-27"]
        constraints = merge([],[form],"form",trip_dates=days)
        constraints = merge(constraints,[{**form,"scope":{**form["scope"],"dates":days[1:]},"revoke":True,"quote":"这天少排"}],"decision",trip_dates=days)
        result = evaluate({**REQUIREMENTS,"constraints":constraints},None,set(),expected_constraints=[{"kind":"daily_min_stops"}])
        self.assertEqual((result["constraints_extracted"],result["constraints_matched"],result["constraints_active_form"]),(1,1,1))

    def test_extraction_counts_form_quotes_once_and_excludes_revokes_and_other_sources(self):
        from app.constraints import merge
        from app.models import ConstraintDraft
        form = ConstraintDraft(kind="daily_min_stops",count=3,quote="每天至少三个景点").model_dump(mode="json")
        constraints = merge([],[form],"form")
        constraints = merge(constraints,[{**form,"count":2}],"form")
        constraints = merge(constraints,[{**form,"revoke":True}],"form")
        constraints = merge(constraints,[ConstraintDraft(kind="fewer_walking",quote="少走路").model_dump(mode="json")],"message")
        result = evaluate({**REQUIREMENTS,"constraints":constraints},None,set(),expected_constraints=[{"kind":"daily_min_stops"}])
        self.assertEqual((result["constraints_extracted"],result["constraints_matched"],result["constraints_active_form"]),(1,1,0))

    def test_failed_run_keeps_its_events_and_app_warnings(self):
        import json, logging
        from app.store import Store
        from eval.run_ablation import CapturedLogs, save_failure
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            store = Store(Path(folder) / "work")
            trip = store.create_trip("合成失败", REQUIREMENTS)
            run_id = store.start_run(trip["id"],trip["version"],"thread",{})
            store.event(run_id,"progress","正在安排每日行程",stage="activities")
            store.finish_run(run_id,"failed",{"code":"route_format","message":"路线 Agent 未交接当前步骤需要的结构"})
            captured = CapturedLogs()
            logging.getLogger("app").addHandler(captured)
            try:
                logging.getLogger("app.planner").warning("合成告警")
            finally:
                logging.getLogger("app").removeHandler(captured)
            target = Path(folder) / "failures" / "case.json"
            save_failure(target,store,[run_id,run_id],{"case":"case","variant":"full","repeat":1,"outcome":"failed","error":"route_format"},captured.records)
            saved = json.loads(target.read_text(encoding="utf-8"))
        [run] = saved["runs"]
        self.assertEqual((run["status"],run["document"]["code"]),("failed","route_format"))
        self.assertEqual([event["message"] for event in run["events"]],["正在安排每日行程"])
        self.assertEqual([entry["message"] for entry in saved["logs"]],["合成告警"])

    def test_wrong_dates_and_city_order_are_counted(self):
        candidate = {"days":[{"date":"2026-12-12","city":"深圳"}],"draft":{}}
        result = evaluate({**REQUIREMENTS,"cities":["广州"]},candidate,set())
        self.assertEqual((result["date_coverage_error"],result["city_order_error"],result["has_plan"]),(1,1,False))


class AutoAnswer(unittest.TestCase):
    def test_similar_recommendation_returns_the_exact_option(self):
        self.assertEqual(auto_answer({"recommendation":"建议合并为一次，避免重复","options":["合并为一次游览","分别安排两次"]}),"合并为一次游览")

    def test_recommendation_containing_an_option_takes_priority(self):
        self.assertEqual(auto_answer({"recommendation":"建议分别安排两次，留出时间","options":["合并为一次游览","分别安排两次"]}),"分别安排两次")

    def test_place_and_hub_still_choose_the_first_option(self):
        for identity in ("place-x","hub-x"):
            self.assertEqual(auto_answer({"id":identity,"recommendation":"第二项","options":["第一项","第二项"]}),"第一项")

    def test_without_options_returns_the_recommendation(self):
        self.assertEqual(auto_answer({"recommendation":"建议合并为一次，避免重复","options":[]}),"建议合并为一次，避免重复")
        self.assertEqual(auto_answer({}),"你决定，按推荐处理")

    def test_empty_recommendation_uses_the_first_nonblank_option(self):
        self.assertEqual(auto_answer({"options":[" ","合并为一次游览","分别安排两次"]}),"合并为一次游览")


class Replay(unittest.TestCase):
    def test_flyai_output_is_recorded_once_and_replayed_offline(self):
        calls = []

        async def live(args, cwd=None, timeout=60, env=None):
            calls.append(args)
            return 0, '{"items":[]}', "token=secret"
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "case.json"
            planner = SimpleNamespace(amap=SimpleNamespace(call=None,batch=None,api_key="",cache={},batch_state=None),
                                      flyai=SimpleNamespace(command=lambda: ["node","cli.js"],api_key="secret"),store=None)
            original = adapters.process
            adapters.process = live
            try:
                recorder = replay.Cassette(file,"record")
                undo = replay.install(planner,recorder)
                asyncio.run(adapters.process(["node","cli.js","search-poi","--keyword","广州塔"]))
                undo()
                recorder.flush()
                self.assertNotIn("secret",file.read_text(encoding="utf-8"))
                player = replay.Cassette(file,"replay")
                undo = replay.install(planner,player)
                self.assertEqual(planner.flyai.command(),["flyai"])
                result = asyncio.run(adapters.process(["flyai","search-poi","--keyword","广州塔"]))
                missing = asyncio.run(adapters.process(["flyai","search-poi","--keyword","陈家祠"]))
                undo()
            finally:
                adapters.process = original
        self.assertEqual(len(calls),1)
        self.assertEqual(result[:2],(0,'{"items":[]}'))
        self.assertEqual(missing[0],1)
        self.assertEqual(player.stats["hits"],1)


if __name__ == "__main__":
    unittest.main()
