import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app import ablation
from app.adapters import ToolError
from app.constraints import merge
from app.day_clustering import build_spatial_plan, cluster_deviation, spatial_errors
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

    async def test_ordinary_location_budget_does_not_shrink_available_pool(self):
        self.state["stops"] = {s["id"]:s for s in (stop(i,False) for i in reversed(range(80)))}
        self.state["stop_constraints"]["excluded"] = ["guide:005"]
        self.state["stops"]["guide:006"]["category"] = "dining"
        self.state["guide_brief"] = {"groups":[{"stop_ids":["guide:071","guide:070","guide:069"]}]}
        self.decision_model()
        await self.planner.discover(self.state)
        pool = self.planner.locate_stops.await_args.args[1]
        self.assertEqual(len(pool),48)
        self.assertEqual(list(pool)[:3],["guide:000","guide:069","guide:070"])
        available = self.planner.route_agent.plan.await_args.args[2]["available_stops"]
        self.assertEqual(len(available),78)
        self.assertNotIn("guide:005",available)
        self.assertNotIn("guide:006",available)
        self.assertIn("guide:079",available)

    async def test_required_locked_and_assigned_places_are_not_truncated(self):
        self.state["stops"] = {s["id"]:s for s in (stop(i,False) for i in range(130))}
        required = [f"guide:{i:03d}" for i in range(55)]
        self.state["stop_constraints"]["required"] = required
        self.state["locks"] = ["guide:055"]
        self.state["stops"]["guide:056"]["assigned_date"] = A
        self.state["requirements"]["required_stop_ids"] = ["guide:057"]
        self.decision_model()
        await self.planner.discover(self.state)
        pool = self.planner.locate_stops.await_args.args[1]
        self.assertEqual(len(pool),58+48)
        self.assertTrue(set(required+["guide:055","guide:056","guide:057"]) <= pool.keys())

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

    async def test_payload_prompt_single_call_and_inputs_are_preserved(self):
        before = copy.deepcopy(self.state)
        result = await self.planner.discover(self.state)
        self.planner.route_agent.plan.assert_awaited_once()
        instruction,payload = self.planner.route_agent.plan.await_args.args[1:3]
        self.assertEqual(payload["spatial_plan"]["status"],"ready")
        self.assertIn("移日：地点名|原日期|新日期|具体原因",instruction)
        self.assertIn("跨天移动加删除的游玩时长最多占初始已定位时长 25%",instruction)
        self.assertIn("status=partial 时补齐 issues",instruction)
        self.assertEqual(self.state,before)
        self.assertEqual(result["candidates"][0]["days"],payload["spatial_plan"]["days"])
        self.assertNotIn("spatial_plan",result)

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

    async def test_ready_prompt_keeps_city_date_segments_fixed(self):
        await self.planner.discover(self.state)
        instruction,payload = self.planner.route_agent.plan.await_args.args[1:3]
        self.assertEqual(payload["spatial_plan"]["status"],"ready")
        self.assertIn("status=ready 时城市日期段已由代码确定，不得改变。",instruction)
        self.assertNotIn("可调整各城天数",instruction)

    async def test_partial_prompt_allows_city_day_changes_with_order_and_full_dates(self):
        self.state["stops"]["guide:007"]["map_place"] = None
        self.state["stop_constraints"]["required"] = ["guide:007"]
        self.decision_model()
        await self.planner.discover(self.state)
        instruction,payload = self.planner.route_agent.plan.await_args.args[1:3]
        self.assertEqual(payload["spatial_plan"]["status"],"partial")
        self.assertIn("status=partial 时可调整各城天数以补齐必去项，但须保持城市顺序与日期全覆盖。",instruction)
        self.assertNotIn("城市日期段已由代码确定，不得改变",instruction)

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

    async def test_one_policy_error_retries_once_and_accepts_correction(self):
        plan = self.baseline()
        self.planner.route_agent.plan.side_effect = [self.proposal(plan,3),self.proposal(plan)]
        result = await self.planner.discover(self.state)
        self.assertEqual(self.planner.route_agent.plan.await_count,2)
        payload = self.planner.route_agent.plan.await_args.args[2]
        self.assertTrue(payload["cluster_deviation"]["exceeded"])
        self.assertIn("validation_error",payload)
        self.assertIn("previous_candidate",payload)
        self.assertNotIn(WARNING,result["warnings"])
        self.assertEqual([d["stop_ids"] for d in result["candidates"][0]["days"]],[d["stop_ids"] for d in plan["days"]])

    async def test_deviation_receives_user_visit_duration_requirements(self):
        self.state["requirements"]["constraints"] = merge([],[ConstraintDraft(kind="visit_duration",minutes=100,
            scope={"stop_ids":["guide:000"]},quote="游玩100分钟").model_dump(mode="json")],"form")
        with patch("app.nodes.candidates.cluster_deviation",wraps=cluster_deviation) as deviation:
            result = await self.planner.discover(self.state)
        self.assertTrue(result["candidates"])
        self.assertEqual(deviation.call_args.args[3],self.state["requirements"])

    async def test_two_policy_errors_fall_back_and_repeat_hard_validation(self):
        plan = self.baseline()
        self.planner.route_agent.plan.side_effect = [self.proposal(plan,3),self.proposal(plan,3)]
        with patch("app.nodes.candidates.spatial_errors",wraps=spatial_errors) as errors:
            result = await self.planner.discover(self.state)
        self.assertEqual(errors.call_count,3)
        self.assertEqual(errors.call_args.args[1]["days"],plan["days"])
        candidate = result["candidates"][0]
        self.assertEqual(candidate["days"],plan["days"])
        self.assertEqual(candidate["title"],"2天空间分日方案")
        self.assertEqual(candidate["reason"],"路线模型微调超出约定，采用代码初始分配")
        self.assertIn(WARNING,result["warnings"])
        self.assertEqual(plan,self.baseline())

    async def test_fallback_with_a_hard_error_is_rejected(self):
        plan = self.baseline()
        self.planner.route_agent.plan.side_effect = [self.proposal(plan,3),self.proposal(plan,3)]
        with patch("app.nodes.candidates.spatial_errors",side_effect=[[],[],["回退固定日期不合法"]]) as errors:
            result = await self.planner.discover(self.state)
            self.assertTrue(any(item["blocking"] and "回退固定日期不合法" in item["detail"] for item in result["unmet"]))
        self.assertEqual(errors.call_count,3)

    async def test_second_hard_error_does_not_fall_back(self):
        plan = self.baseline()
        invalid = self.proposal(plan)
        invalid.candidate.days[0].stop_ids.append("invented")
        self.planner.route_agent.plan.side_effect = [self.proposal(plan,3),invalid]
        with self.assertRaisesRegex(ToolError,"未知"):
            await self.planner.discover(self.state)
        self.assertEqual(self.planner.route_agent.plan.await_count,2)

    async def test_new_spatial_hard_error_retries_and_remains_an_error(self):
        plan = self.baseline()
        invalid = self.proposal(plan)
        invalid.candidate.days[0].stop_ids = list(self.state["stops"])
        invalid.candidate.days[1].stop_ids = []
        self.planner.route_agent.plan.side_effect = [invalid,copy.deepcopy(invalid)]
        result = await self.planner.discover(self.state)
        self.assertTrue(any(item["blocking"] and "容量" in item["detail"] for item in result["unmet"]))
        self.assertEqual(self.planner.route_agent.plan.await_count,2)


    async def test_missing_move_reason_retries_then_accepts_explicit_reason(self):
        plan = self.baseline()
        changed = self.proposal(plan)
        left,right = changed.candidate.days
        first,second = left.stop_ids.pop(),right.stop_ids.pop()
        left.stop_ids.append(second)
        right.stop_ids.append(first)
        corrected = copy.deepcopy(changed)
        corrected.candidate.days[0].adjustment += f"\n移日：{second}|{B}|{A}|配合预约时段"
        corrected.candidate.days[1].adjustment += f"\n移日：{first}|{A}|{B}|配合预约时段"
        self.planner.route_agent.plan.side_effect = [changed,corrected]
        result = await self.planner.discover(self.state)
        self.assertEqual(self.planner.route_agent.plan.await_count,2)
        deviation = self.planner.route_agent.plan.await_args.args[2]["cluster_deviation"]
        self.assertEqual(set(deviation["missing_reasons"]),{first,second})
        self.assertTrue(all(d["adjustment"].startswith("空间分日：") for d in result["candidates"][0]["days"]))
        self.assertIn("移日：",result["candidates"][0]["days"][0]["adjustment"])
        self.assertEqual(cluster_deviation(plan,result["candidates"][0],canonicalize_stops(self.state["stops"]))["missing_reasons"],[])

    async def test_unchanged_days_get_initial_note_before_legacy_validation(self):
        for index,s in enumerate(self.state["stops"].values()):
            s["source_id"] = f"source-{index}"
        plan = self.baseline()
        turn = self.proposal(plan)
        for day in turn.candidate.days:
            day.adjustment = ""
        self.planner.route_agent.plan.side_effect = [turn]
        result = await self.planner.discover(self.state)
        self.assertEqual(result["candidates"][0]["days"],plan["days"])

    async def test_changed_days_do_not_get_a_template_to_mask_missing_reason(self):
        for index,s in enumerate(self.state["stops"].values()):
            s["source_id"] = f"source-{index}"
        plan = self.baseline()
        changed = self.proposal(plan)
        changed.candidate.days[0].stop_ids.reverse()
        for day in changed.candidate.days:
            day.adjustment = ""
        self.planner.route_agent.plan.side_effect = [changed,copy.deepcopy(changed)]
        with self.assertRaisesRegex(ToolError,"说明调整原因"):
            await self.planner.discover(self.state)

    async def test_changed_day_omits_initial_empty_and_shortage_notes(self):
        self.state["stops"] = {"guide:000":stop(0)}
        self.state["requirements"]["constraints"] = merge([],[ConstraintDraft(
            kind="daily_min_stops",count=3,strength="soft",quote="接受不足").model_dump(mode="json")],"form")
        plan = self.baseline()
        changed = self.proposal(plan)
        source = next(day for day in changed.candidate.days if day.stop_ids)
        target = next(day for day in changed.candidate.days if not day.stop_ids)
        target.stop_ids = source.stop_ids
        source.stop_ids = []
        target.adjustment += f"\n移日：guide:000|{source.date}|{target.date}|配合预约时段"
        self.planner.route_agent.plan.side_effect = [changed]
        result = await self.planner.discover(self.state)
        for day in result["candidates"][0]["days"]:
            self.assertTrue(day["adjustment"].startswith("空间分日："))
            self.assertNotIn("暂无已定位景点",day["adjustment"])
            self.assertNotIn("低于已接受不足",day["adjustment"])
        self.assertIn("移日：",result["candidates"][0]["days"][1]["adjustment"])

    async def test_filled_short_day_omits_initial_shortage_note(self):
        self.state["stops"] = {"guide:000":stop(0),"guide:001":stop(1,False),"guide:002":stop(2,False)}
        self.state["requirements"]["constraints"] = merge([],[ConstraintDraft(
            kind="daily_min_stops",count=3,strength="soft",quote="接受不足").model_dump(mode="json")],"form")
        plan = self.baseline()
        changed = self.proposal(plan)
        day = next(day for day in changed.candidate.days if day.stop_ids)
        day.stop_ids.extend(["guide:001","guide:002"])
        self.planner.route_agent.plan.side_effect = [changed]
        result = await self.planner.discover(self.state)
        final = next(day for day in result["candidates"][0]["days"] if len(day["stop_ids"]) == 3)
        self.assertNotIn("低于已接受不足",final["adjustment"])
        self.assertIn("按游玩偏好调整安排",final["adjustment"])

    async def test_same_stop_set_keeps_full_initial_note_after_reordering(self):
        plan = self.baseline()
        plan["days"][0]["adjustment"] += "保留同一集合的补充说明。"
        changed = self.proposal(plan)
        changed.candidate.days[0].stop_ids.reverse()
        self.planner.route_agent.plan.side_effect = [changed]
        with patch("app.nodes.candidates.build_spatial_plan",return_value=plan):
            result = await self.planner.discover(self.state)
        self.assertIn("保留同一集合的补充说明",result["candidates"][0]["days"][0]["adjustment"])

    async def test_copied_initial_notes_are_removed_when_filling_an_empty_day(self):
        self.state["stops"] = {"guide:000":stop(0)}
        self.state["requirements"]["constraints"] = merge([],[ConstraintDraft(
            kind="daily_min_stops",count=3,strength="soft",quote="接受不足").model_dump(mode="json")],"form")
        plan = self.baseline()
        changed = RouteTurn(status="ready",candidate=Candidate(title="调整日期",reason="配合预约",days=copy.deepcopy(plan["days"])))
        source = next(day for day in changed.candidate.days if day.stop_ids)
        target = next(day for day in changed.candidate.days if not day.stop_ids)
        target.stop_ids,source.stop_ids = source.stop_ids,[]
        for day in changed.candidate.days:
            day.adjustment += "按预约时段调整日期。"
        move = f"移日：guide:000|{source.date}|{target.date}|配合预约时段"
        target.adjustment += "\n"+move
        self.planner.route_agent.plan.side_effect = [changed]
        result = await self.planner.discover(self.state)
        for day in result["candidates"][0]["days"]:
            self.assertNotIn("当日暂无已定位景点安排",day["adjustment"])
            self.assertNotIn("低于已接受不足",day["adjustment"])
            self.assertIn("按预约时段调整日期。",day["adjustment"])
        self.assertIn(move,result["candidates"][0]["days"][1]["adjustment"])

    async def test_copied_shortage_note_is_removed_when_adding_unlocated_stops(self):
        self.state["stops"] = {"guide:000":stop(0),"guide:001":stop(1,False),"guide:002":stop(2,False)}
        self.state["requirements"]["constraints"] = merge([],[ConstraintDraft(
            kind="daily_min_stops",count=3,strength="soft",quote="接受不足").model_dump(mode="json")],"form")
        plan = self.baseline()
        changed = RouteTurn(status="ready",candidate=Candidate(title="补齐景点",reason="补充安排",days=copy.deepcopy(plan["days"])))
        day = next(day for day in changed.candidate.days if day.stop_ids)
        day.stop_ids.extend(["guide:001","guide:002"])
        day.adjustment += "补入尚未定位的攻略景点。"
        self.planner.route_agent.plan.side_effect = [changed]
        result = await self.planner.discover(self.state)
        final = next(day for day in result["candidates"][0]["days"] if len(day["stop_ids"]) == 3)
        self.assertNotIn("低于已接受不足",final["adjustment"])
        self.assertIn("补入尚未定位的攻略景点。",final["adjustment"])

    async def test_partial_keeps_suggestions_without_deviation_or_fallback(self):
        self.state["stops"]["guide:007"]["map_place"] = None
        self.state["stop_constraints"]["required"] = ["guide:007"]
        def complete(state,instruction,payload,tools):
            turn = self.initial_turn(state,instruction,payload,tools)
            turn.candidate.days[0].stop_ids.append("guide:007")
            turn.candidate.days[0].adjustment = "补齐尚未定位的用户必去项"
            return turn
        self.planner.route_agent.plan.side_effect = complete
        with patch("app.nodes.candidates.cluster_deviation",side_effect=AssertionError("partial不能检查偏离")):
            result = await self.planner.discover(self.state)
        plan = self.planner.route_agent.plan.await_args.args[2]["spatial_plan"]
        self.assertEqual(plan["status"],"partial")
        self.assertTrue(any(d["stop_ids"] for d in plan["days"]))
        self.assertTrue(any(w.startswith("空间分日初始分配未完整通过校验，已保留建议并交路线模型补齐：") for w in result["warnings"]))
        self.planner.route_agent.plan.assert_awaited_once()

    async def test_partial_hard_errors_do_not_use_initial_allocation(self):
        self.state["stops"]["guide:007"]["map_place"] = None
        self.state["stop_constraints"]["required"] = ["guide:007"]
        with patch("app.nodes.candidates.cluster_deviation",side_effect=AssertionError("partial不能检查偏离")):
            result = await self.planner.discover(self.state)
        self.assertTrue(any(item["kind"] == "stop_unplaced" and item["blocking"] for item in result["unmet"]))

    async def test_partial_allows_model_to_change_city_day_counts(self):
        last = "2026-11-12"
        self.state["requirements"].update(cities=[CITY,"苏州"],end_date=last)
        self.state["stops"] = {s["id"]:s for s in (stop(i,duration_minutes=90) for i in range(12))}
        self.state["stops"].update({s["id"]:s for s in (stop(i,city="苏州",duration_minutes=360) for i in (12,13))})
        initial = self.baseline()
        self.assertEqual(initial["city_days"],{CITY:2,"苏州":1})
        initial.update(status="partial",required_ids=["guide:012","guide:013"],issues=["必去全天项目尚未安排完整"])
        self.state["stop_constraints"]["required"] = initial["required_ids"][:]
        days = [{"date":day,"city":city,"stop_ids":[identity],"adjustment":"为必去全天项目调整城市天数"}
                for day,city,identity in ((A,CITY,"guide:000"),(B,"苏州","guide:012"),(last,"苏州","guide:013"))]
        turn = RouteTurn(status="ready",candidate=Candidate(title="三天必去方案",reason="优先必去全天项目",days=days))
        self.planner.route_agent.plan.side_effect = [turn,copy.deepcopy(turn)]
        with patch("app.nodes.candidates.build_spatial_plan",return_value=initial),patch(
                "app.nodes.candidates.cluster_deviation",side_effect=AssertionError("partial不能检查偏离")):
            result = await self.planner.discover(self.state)
        self.assertEqual([d["city"] for d in result["candidates"][0]["days"]],[CITY,"苏州","苏州"])
        self.planner.route_agent.plan.assert_awaited_once()

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


    async def test_no_code_checks_keeps_clustering_but_skips_policy_and_hard_rejection(self):
        plan = self.baseline()
        self.planner.route_agent.plan.side_effect = [self.proposal(plan,3)]
        with ablation.use("no_code_checks"),patch("app.nodes.candidates.spatial_errors",side_effect=AssertionError()),patch(
                "app.nodes.candidates.cluster_deviation",side_effect=AssertionError()):
            result = await self.planner.discover(self.state)
        self.planner.route_agent.plan.assert_awaited_once()
        self.assertIn("spatial_plan",self.planner.route_agent.plan.await_args.args[2])
        self.assertNotIn(WARNING,result["warnings"])
        self.assertEqual(sum(len(d["stop_ids"]) for d in result["candidates"][0]["days"]),5)

    async def test_no_code_checks_does_not_reject_a_conflicting_initial_plan(self):
        self.state["stops"]["guide:000"]["assigned_date"] = "2026-11-12"
        plan = {"days":[{"date":A,"city":CITY,"stop_ids":["guide:000"]},{"date":B,"city":CITY,"stop_ids":[]}]}
        self.planner.route_agent.plan.side_effect = [self.proposal(plan)]
        with ablation.use("no_code_checks"):
            result = await self.planner.discover(self.state)
        self.assertTrue(result["candidates"])
        self.assertEqual(self.planner.route_agent.plan.await_args.args[2]["spatial_plan"]["status"],"conflict")

    async def test_no_clustering_keeps_original_budget_prompt_payload_and_notes(self):
        self.state["stops"] = {s["id"]:s for s in (stop(i,False) for i in range(80))}
        plan = {"days":[{"date":A,"city":CITY,"stop_ids":["guide:000"]},{"date":B,"city":CITY,"stop_ids":["guide:001"]}]}
        turn = self.proposal(plan)
        self.planner.route_agent.plan.side_effect = [turn]
        expected_days = copy.deepcopy(turn.candidate.model_dump(mode="json")["days"])
        with ablation.use("no_clustering"),patch("app.nodes.candidates.build_spatial_plan",side_effect=AssertionError()),patch(
                "app.nodes.candidates.spatial_errors",side_effect=AssertionError()),patch(
                "app.nodes.candidates.cluster_deviation",side_effect=AssertionError()):
            result = await self.planner.discover(self.state)
        self.assertEqual(len(self.planner.locate_stops.await_args.args[1]),24)
        instruction,payload = self.planner.route_agent.plan.await_args.args[1:3]
        self.assertNotIn("spatial_plan",payload)
        self.assertNotIn("spatial_plan",instruction)
        self.assertNotIn("城市日期段已由代码确定，不得改变",instruction)
        self.assertNotIn("可调整各城天数",instruction)
        self.assertEqual(result["candidates"][0]["days"],expected_days)
