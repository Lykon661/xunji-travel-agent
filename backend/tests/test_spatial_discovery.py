import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app import ablation
from app.adapters import ToolError
from app.constraints import merge
from app.day_clustering import build_spatial_plan, spatial_errors
from app.models import Candidate, ConstraintDraft, Requirements, RouteTurn
from app.places import canonicalize_stops
from app.planner import Planner


A, B, CITY = "2026-11-10", "2026-11-11", "杭州"
WARNING = "路线模型两次微调仍超出空间分日约定，已采用代码初始分配；后续仍将结合实际交通与开放信息检查。"
QUESTION = {"id":"choice","title":"取舍","question":"是否保留这些地点？","options":["保留","删除"],
            "recommendation":"保留","why_needed":"需要用户选择","affected_dates":[A]}


def stop(index, located=True, **extra):
    identity = f"guide:{index:03d}"
    return {"id":identity,"name":f"合成景点{index}","city":CITY,"source_id":"guide","source_title":"合成攻略",
            "category":"sight","duration_minutes":60,"evidence":{"quote":f"合成景点{index}"},
            "map_place":{"id":f"poi-{index}","location":f"{120+index/1000},30"} if located else None,**extra}


class SpatialDiscovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.selected_sources = Mock(return_value=[{"id":"guide","title":"合成攻略","extraction":{"stops":[]}}])
        self.planner.store = SimpleNamespace(sources=Mock(return_value=[]))
        self.planner.locate_stops = AsyncMock(side_effect=lambda state,pool:pool)
        self.planner.weather_context = AsyncMock(return_value={})
        self.state = {"trip_id":"trip","selected_source_ids":["guide"],"requirements":Requirements(
            cities=[CITY],start_date=A,end_date=B).model_dump(mode="json"),
            "stops":{s["id"]:s for s in (stop(i) for i in range(8))},"warnings":[],"locks":[],
            "stop_constraints":{"required":[],"excluded":[]}}
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(side_effect=self.initial_turn))

    def initial_turn(self,state,instruction,payload,tools):
        days = copy.deepcopy(payload["spatial_plan"]["days"])
        if not any(day["stop_ids"] for day in days):
            first = next(iter(payload["available_stops"]))
            days[0]["stop_ids"] = [first]
            days[0]["adjustment"] = "保留一个已有来源地点作为待解决草稿"
        return RouteTurn(status="ready",candidate=Candidate(title="合成两天",reason="根据初始安排",days=days))

    def baseline(self):
        stops = canonicalize_stops(self.state["stops"])
        plan = build_spatial_plan({**self.state,"stops":stops},stops)
        self.assertEqual(plan["status"],"ready",plan["issues"])
        return plan

    def proposal(self, plan, remove=0):
        candidate = Candidate(title="合成两天",reason="模型微调",days=copy.deepcopy(plan["days"]))
        for day in candidate.days:
            day.adjustment = "按游玩偏好调整安排"
            while remove and day.stop_ids:
                day.stop_ids.pop()
                remove -= 1
        return RouteTurn(status="ready",candidate=candidate)

    def decision_model(self):
        self.planner.route_agent.plan.side_effect = self.initial_turn


    async def test_existing_coordinates_do_not_consume_the_ordinary_budget(self):
        self.state["stops"] = {s["id"]:s for s in (stop(i,i < 20) for i in range(90))}
        self.decision_model()
        await self.planner.discover(self.state)
        pool = self.planner.locate_stops.await_args.args[1]
        self.assertEqual(len(pool),48)
        self.assertTrue(all(s["map_place"] is None for s in pool.values()))
        payload = self.planner.route_agent.plan.await_args.args[2]
        self.assertEqual(len(payload["available_stops"]),90)
        self.assertTrue(all(payload["available_stops"][f"guide:{i:03d}"]["map_place"] for i in range(20)))

    async def test_location_priority_does_not_expand_place_clarification(self):
        for kind in ("lock","assigned","requirement"):
            with self.subTest(kind=kind):
                self.setUp()
                self.state["stops"]["guide:007"].update(map_place=None,map_candidates=[
                    {"id":"branch-a","name":"分店甲"},{"id":"branch-b","name":"分店乙"}])
                if kind == "lock":
                    self.state["locks"] = ["guide:007"]
                elif kind == "assigned":
                    self.state["stops"]["guide:007"]["assigned_date"] = A
                else:
                    self.state["requirements"]["required_stop_ids"] = ["guide:007"]
                self.decision_model()
                result = await self.planner.discover(self.state)
                self.assertTrue(result.get("candidates"))
                self.assertIn("guide:007",self.planner.locate_stops.await_args.args[1])
                self.assertIn(self.planner.route_agent.plan.await_count,(1,2))

    async def test_located_representative_prevents_required_alias_clarification(self):
        self.state["stops"]["guide:alias"] = stop(0,id="guide:alias",name="合成景点0别名",
            map_place={"id":"poi-0"},map_candidates=[{"id":"branch-a","name":"分店甲"},{"id":"branch-b","name":"分店乙"}])
        self.state["stop_constraints"]["required"] = ["guide:alias"]
        self.decision_model()
        result = await self.planner.discover(self.state)
        self.assertIn(self.planner.route_agent.plan.await_count,(1,2))
        self.assertTrue(result.get("candidates"))

    async def test_unresolved_original_required_still_needs_clarification(self):
        self.state["stops"]["guide:007"].update(map_place=None,map_candidates=[
            {"id":"branch-a","name":"分店甲"},{"id":"branch-b","name":"分店乙"}])
        self.state["stop_constraints"]["required"] = ["guide:007"]
        result = await self.planner.discover(self.state)
        self.assertEqual(result["unmet"][0]["refs"][0]["id"],"guide:007")
        self.planner.route_agent.plan.assert_not_awaited()

    async def test_excluded_required_does_not_need_clarification(self):
        self.state["stops"]["guide:007"].update(map_place=None,map_candidates=[
            {"id":"branch-a","name":"分店甲"},{"id":"branch-b","name":"分店乙"}])
        self.state["stop_constraints"] = {"required":["guide:007"],"excluded":["guide:007"]}
        self.decision_model()
        result = await self.planner.discover(self.state)
        self.assertTrue(result.get("candidates"))
        self.assertIn(self.planner.route_agent.plan.await_count,(1,2))


    async def test_accepted_soft_shortage_keeps_empty_day_without_rejection(self):
        self.state["stops"] = {"guide:000":stop(0)}
        self.state["requirements"]["constraints"] = merge([],[ConstraintDraft(
            kind="daily_min_stops",count=3,strength="soft",quote="接受不足").model_dump(mode="json")],"form")
        result = await self.planner.discover(self.state)
        self.planner.route_agent.plan.assert_awaited_once()
        plan = self.planner.route_agent.plan.await_args.args[2]["spatial_plan"]
        self.assertEqual(plan["status"],"ready",plan["issues"])
        self.assertTrue(any("参考数量3个" in issue for issue in plan["issues"]))
        self.assertTrue(result["candidates"][0]["gaps"])
        self.assertEqual(sorted(len(day["stop_ids"]) for day in result["candidates"][0]["days"]),[0,1])


    async def test_location_results_do_not_mutate_input_stops(self):
        self.state["stops"]["guide:000"]["map_place"] = None
        before = copy.deepcopy(self.state)
        def locate(state,pool):
            pool["guide:000"]["map_place"] = stop(0)["map_place"]
            return pool
        self.planner.locate_stops.side_effect = locate
        await self.planner.discover(self.state)
        self.assertEqual(self.state,before)

    async def test_empty_short_previous_window_does_not_reject_discovery(self):
        self.state["stops"] = {"guide:000":stop(0)}
        self.state["requirements"]["constraints"] = merge([],[ConstraintDraft(
            kind="daily_min_stops",count=3,strength="soft",quote="接受不足").model_dump(mode="json")],"form")
        self.state["old_requirements"] = copy.deepcopy(self.state["requirements"])
        self.state["old_itinerary"] = {"days":[{"date":A,"city":CITY,"available_start":780,"tail_deadline":810}]}
        result = await self.planner.discover(self.state)
        self.planner.route_agent.plan.assert_awaited_once()
        plan = self.planner.route_agent.plan.await_args.args[2]["spatial_plan"]
        self.assertEqual(plan["status"],"ready",plan["issues"])
        self.assertEqual(plan["day_budgets"][A]["load"],0)
        self.assertEqual(result["candidates"][0]["days"][0]["stop_ids"],[])


    async def test_conflict_reports_fixed_place_and_date_without_model_call(self):
        self.state["stops"]["guide:000"]["assigned_date"] = "2026-11-12"
        with self.assertRaisesRegex(ToolError,"guide:000.*2026-11-12") as error:
            await self.planner.discover(self.state)
        self.assertEqual(error.exception.code,"route_constraints")
        self.planner.route_agent.plan.assert_not_awaited()

    async def test_ready_plan_keeps_optional_full_day_without_a_question(self):
        result = await self.planner.discover(self.state)
        self.assertTrue(result["candidates"])
        self.assertNotIn("decision",result)


    async def test_no_code_checks_does_not_reject_a_conflicting_initial_plan(self):
        self.state["stops"]["guide:000"]["assigned_date"] = "2026-11-12"
        plan = {"days":[{"date":A,"city":CITY,"stop_ids":["guide:000"]},{"date":B,"city":CITY,"stop_ids":[]}]}
        self.planner.route_agent.plan.side_effect = [self.proposal(plan)]
        with ablation.use("no_code_checks"):
            result = await self.planner.discover(self.state)
        self.assertTrue(result["candidates"])
        self.assertEqual(self.planner.route_agent.plan.await_args.args[2]["spatial_plan"]["status"],"conflict")
