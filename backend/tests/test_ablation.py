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

    def test_wrong_dates_and_city_order_are_counted(self):
        candidate = {"days":[{"date":"2026-12-12","city":"深圳"}],"draft":{}}
        result = evaluate({**REQUIREMENTS,"cities":["广州"]},candidate,set())
        self.assertEqual((result["date_coverage_error"],result["city_order_error"],result["has_plan"]),(1,1,False))


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
