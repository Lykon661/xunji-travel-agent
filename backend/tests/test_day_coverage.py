"""Synthetic regression cases for the user to run; no live model or provider calls."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.adapters import ToolError
from app.models import OrderChoice, RouteTurn
from app.planner import Planner
from app.routing import early_finish, reference_opening
from app.rules import check_itinerary


DATE = "2026-11-10"
ZOO_DATE = "2026-10-16"
ZOO_HOURS = "10月1日-10月6日:8:30-19:00；10月7日-10月31日:9:30-18:00注:营业时间与设施运营计划如有调整，请以官网/现场公示为准"
MUSEUM_HOURS = "7月-8月 周二-周日 09:00-21:00开放 最晚进入20:00； 1月-6月, 9月-12月 周二-周日 09:00-17:00开放 最晚进入16:00； 周一 全天不开放(法定节假日除外)"
REQ = {"origin":"北京","cities":["杭州"],"start_date":DATE,"end_date":DATE,
       "people":1,"rooms":1,"budget":5000,"preferences":"多点玩的项目，不怕走路","transport":"train"}


def stop(identity, duration=None, **extra):
    return {"id":identity,"name":identity,"city":"杭州","duration_minutes":duration,
            "source_id":"guide","source_title":"合成攻略","evidence":{"quote":identity},**extra}


def ready(ids, **kwargs):
    return RouteTurn(status="ready",order=OrderChoice(stop_ids=ids,**kwargs))


def opening_place(claim="周一至周日 11:00-22:00"):
    return {"id":"mall","name":"花城汇","location":"113.324606,23.118082",
            "evidence_id":"query:synthetic-opening","opentime2":claim,"open_time":"11:00-22:00"}


class OpeningReferences(unittest.TestCase):
    def test_explicit_map_closure_is_not_replaced_by_guide_hours(self):
        museum = stop("museum",opening_claim="09:00-17:00",
            map_place=opening_place("周二至周日 09:00-17:00；周一 闭馆"))
        monday = reference_opening(museum,"2026-10-12")
        self.assertTrue(monday["closed"])
        self.assertEqual(monday["windows"],[])
        self.assertEqual(reference_opening(museum,"2026-10-13")["windows"],[[540,1020]])

    def test_unresolved_map_calendar_does_not_fall_back_to_unqualified_guide_hours(self):
        museum = stop("museum",opening_claim="09:00-17:00",map_place=opening_place(MUSEUM_HOURS))
        self.assertIsNone(reference_opening(museum,"2026-10-12"))

    def test_guide_hours_keep_all_windows_and_weekday_exceptions(self):
        museum = stop("museum",opening_claim="09:00-12:00，14:00-18:00；周一闭馆")
        self.assertTrue(reference_opening(museum,"2026-10-12")["closed"])
        self.assertEqual(reference_opening(museum,"2026-10-13")["windows"],[[540,720],[840,1080]])
        self.assertIsNone(reference_opening(stop("museum",opening_claim="暑期09:00-17:00"),DATE))

    def test_evidenced_daily_map_hours_work_without_a_guide_claim(self):
        value = reference_opening(stop("mall",45,map_place=opening_place()))
        self.assertEqual(value["windows"],[[660,1320]])
        self.assertEqual(value["evidence_id"],"query:synthetic-opening")
        self.assertIn("不代表",value["note"])

    def test_weekday_seasonal_overnight_and_unproved_hours_are_not_assumed_daily(self):
        for claim in ("周一 11:00-22:00","暑期11:00-22:00","10月1日-7日11:00-22:00","每日22:00-02:00"):
            with self.subTest(claim=claim):
                self.assertIsNone(reference_opening(stop("mall",map_place=opening_place(claim))))
        place = opening_place()
        place.pop("evidence_id")
        self.assertIsNone(reference_opening(stop("mall",map_place=place)))

    def test_exact_visit_date_selects_the_right_seasonal_clause(self):
        zoo = stop("zoo",540,map_place=opening_place(ZOO_HOURS))
        october = reference_opening(zoo,ZOO_DATE)
        self.assertEqual(october["windows"],[[570,1080]])
        self.assertEqual(october["date_range"],["2026-10-07","2026-10-31"])
        self.assertEqual(october["applicable_date"],ZOO_DATE)
        self.assertEqual(reference_opening(zoo,"2026-10-03")["windows"],[[510,1140]])
        self.assertIsNone(reference_opening(zoo,"2026-11-01"))
        self.assertIsNone(reference_opening(zoo))

    def test_ambiguous_dates_invalid_dates_and_meaningful_notes_stay_unresolved(self):
        for claim in ("10月7日-10月31日:9:30-18:00；10月16日-10月20日:9:30-17:00",
                      "10月7日-10月32日:9:30-18:00","10月7日-10月31日:9:30-18:00注:周五闭园"):
            with self.subTest(claim=claim):
                self.assertIsNone(reference_opening(stop("zoo",map_place=opening_place(claim)),ZOO_DATE))

    def test_month_weekday_rules_keep_last_entry_separate_from_closing(self):
        museum = stop("museum",map_place=opening_place(MUSEUM_HOURS))
        october = reference_opening(museum,"2026-10-14")
        self.assertEqual(october["windows"],[[540,1020]])
        self.assertEqual(october["last_entry_minutes"],960)
        summer = reference_opening(museum,"2026-07-15")
        self.assertEqual(summer["windows"],[[540,1260]])
        self.assertEqual(summer["last_entry_minutes"],1200)
        self.assertIsNone(reference_opening(museum,"2026-10-12"))
        self.assertIsNone(reference_opening(museum))
        for claim in (MUSEUM_HOURS+"；节假日另行通知",MUSEUM_HOURS.replace("最晚进入16:00","最晚进入18:00"),
                      MUSEUM_HOURS.replace("9月-12月","9月-13月")):
            self.assertIsNone(reference_opening(stop("museum",map_place=opening_place(claim)),"2026-10-14"))


class CoveragePolicy(unittest.TestCase):
    def test_spare_afternoon_is_detected_from_visits_instead_of_waiting_blocks(self):
        day = {"available_start":540,"tail_deadline":1200,"activities":[
            {"kind":"visit","end":"14:30"},{"kind":"rest","end":"19:00"}]}
        self.assertEqual(early_finish(day)["last_visit_end"],"14:30")

    def test_early_return_or_long_station_transfer_does_not_request_filler(self):
        day = {"available_start":540,"tail_deadline":900,"activities":[{"kind":"visit","end":"14:00"}]}
        self.assertIsNone(early_finish(day))
        day.update(tail_deadline=1080,return_move={"minutes":160})
        self.assertIsNone(early_finish(day))

    def test_short_available_window_and_complete_afternoon_are_exempt(self):
        day = {"available_start":900,"tail_deadline":1200,"activities":[{"kind":"visit","end":"16:00"}]}
        self.assertIsNone(early_finish(day))
        day.update(available_start=540,activities=[{"kind":"visit","end":"17:30"}])
        self.assertIsNone(early_finish(day))


class DailyCoverage(unittest.IsolatedAsyncioTestCase):
    async def test_closed_stop_is_not_scheduled_and_locked_stop_remains_blocked(self):
        state = self.state({"museum":stop("museum",60,opening_claim="周二至周日 09:00-17:00；周一闭馆")},["museum"])
        day = {**state["selected_route"]["days"][0],"date":"2026-10-12"}
        window = (540,1200,[],[],{"name":None,"start_name":None,"label":"结束当天安排"})
        result = await self.planner.schedule_activity_day(state,day,OrderChoice(stop_ids=["museum"]),set(),window,{})
        self.assertFalse(any(item["kind"] == "visit" for item in result["activities"]))
        self.assertTrue(any("闭馆" in gap for gap in result["gaps"]))
        locked = await self.planner.schedule_activity_day(state,day,OrderChoice(stop_ids=["museum"]),{"museum"},window,{})
        state.update(itinerary={"days":[locked],"hotels":[],"transport":[]},locks=["museum"])
        checked = await self.planner.check(state)
        self.assertTrue(any(issue["code"] == "closed_day" and issue["blocking"] for issue in checked["itinerary"]["issues"]))

    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.store = Mock()
        self.planner.amap = SimpleNamespace(cache={})
        self.planner.flyai = SimpleNamespace(availability=lambda:{"ready":False})
        self.planner.locate_stops = AsyncMock(side_effect=lambda state,stops:stops)
        self.planner.weather_context = AsyncMock(return_value={})
        self.planner.movement = AsyncMock(return_value=({"minutes":15,"mode":"transit"},None))
        self.planner.map_order = AsyncMock(side_effect=lambda state,city,ids,*anchors:(list(ids),"合成顺序"))
        self.planner.update_day_places = Mock()
        self.planner.refresh_day_routes = AsyncMock()
        self.planner.query_ticket_prices = AsyncMock()

    def state(self, stops=None, ids=None):
        stops = stops or {"a":stop("a",90),"b":stop("b",90),"c":stop("c",240)}
        day = {"date":DATE,"city":"杭州","stop_ids":["a","b"] if ids is None else ids,
               "adjustment":"按所选攻略安排全天游览"}
        return {"trip_id":"trip","mode":"new","requirements":copy.deepcopy(REQ),"stops":stops,
                "locks":[],"selected_route":{"days":[day]},"itinerary":{"days":[],"transport":[],"hotels":[]}}

    async def test_missing_park_duration_is_corrected_instead_of_defaulting_to_90(self):
        state = self.state({"park":stop("park")},["park"])
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(side_effect=[
            ready(["park"],explanation="全天园区"),ready(["park"],durations={"park":420})]))
        day = state["selected_route"]["days"][0]
        turn = await self.planner.daily_order(state,day,["park"],set(),{"time_window":{"start":"09:00","end":"20:00"}})
        self.assertEqual(turn.order.durations["park"],420)
        correction = self.planner.route_agent.plan.call_args.args[2]
        self.assertIn("park",correction["validation_error"])
        window = (540,1200,[],[],{"name":None,"start_name":None,"label":"结束当天安排"})
        scheduled = await self.planner.schedule_activity_day(state,day,turn.order,set(),window,{})
        self.assertEqual(scheduled["activities"][0]["end"],"17:00")
        self.assertEqual(scheduled["activities"][0]["visit_minutes"],420)
        self.assertEqual(scheduled["activities"][0]["meal_break"]["duration_minutes"],60)

    async def test_still_missing_duration_fails_after_one_correction(self):
        state = self.state({"park":stop("park")},["park"])
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=ready(["park"])))
        with self.assertRaises(ToolError):
            await self.planner.daily_order(state,state["selected_route"]["days"][0],["park"],set(),
                                           {"time_window":{"start":"09:00","end":"20:00"}})
        self.assertEqual(self.planner.route_agent.plan.await_count,2)

    async def test_sparse_day_gets_one_actual_schedule_feedback_and_source_pool_refill(self):
        state = self.state()
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(side_effect=[ready(["a","b"]),ready(["a","b","c"])]))
        result = await self.planner.activities(state)
        day = result["itinerary"]["days"][0]
        self.assertEqual([a["id"] for a in day["activities"] if a["kind"] == "visit"],["a","b","c"])
        self.assertEqual(self.planner.route_agent.plan.await_count,2)
        payload = self.planner.route_agent.plan.call_args.args[2]
        self.assertIn("schedule_feedback",payload)
        self.assertEqual(payload["time_window"]["end"],"20:00")
        self.assertIn("c",payload["available_stops"])
        self.assertFalse(any("仍余约" in gap for gap in day["gaps"]))

    async def test_empty_candidate_day_can_use_remaining_chosen_guide_stops(self):
        state = self.state(ids=[])
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=ready(["a","b","c"])))
        result = await self.planner.activities(state)
        self.assertEqual(sum(a["kind"] == "visit" for a in result["itinerary"]["days"][0]["activities"]),3)

    async def test_refill_cannot_reuse_another_day_or_add_a_restaurant(self):
        state = self.state()
        state["stops"]["food"] = stop("food",60,category="dining")
        state["selected_route"]["days"].append({"date":"2026-11-11","city":"杭州","stop_ids":["c"]})
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=ready(["a","b"])))
        await self.planner.daily_order(state,state["selected_route"]["days"][0],["a","b"],set(),{})
        available = self.planner.route_agent.plan.call_args.args[2]["available_stops"]
        self.assertNotIn("c",available)
        self.assertNotIn("food",available)

    async def test_continue_repair_does_not_add_unused_guide_stops(self):
        state = self.state()
        state["repair_only"] = True
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=ready(["a","b"])))
        await self.planner.daily_order(state,state["selected_route"]["days"][0],["a","b"],set(),{})
        available = self.planner.route_agent.plan.call_args.args[2]["available_stops"]
        self.assertEqual(set(available),{"a","b"})

    async def test_continue_repair_preserves_other_day_and_skips_unrelated_queries(self):
        state = self.state({"a":stop("a",60),"b":stop("b",60),"food":stop("food",60,category="dining")},["a"])
        state["selected_route"]["days"].append({"date":"2026-11-11","city":"杭州","stop_ids":["b"]})
        days = [{"date":day_date,"city":"杭州","activities":[{
            **copy.deepcopy(state["stops"][identity]),"kind":"visit","start":"09:00","end":"10:00",
            "map_place":{"name":identity,"location":"120,30"},"move_before":{}}],
            "weather":{"available":True},"gaps":[],"return_move":{},"meal_recommendations":[],"custom":"保留原字段"}
            for day_date,identity in ((DATE,"a"),("2026-11-11","b"))]
        original = {"days":days,"transport":[],"hotels":[]}
        state.update(repair_only=True,mode="modify",old_itinerary=copy.deepcopy(original),itinerary=copy.deepcopy(original),
                     change={"scopes":["activity"],"dates":[DATE]},assessment_dates=[DATE])
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=ready(["a"])))
        result = await self.planner.activities(state)
        self.assertEqual(result["itinerary"]["days"][1],original["days"][1])
        self.assertEqual(self.planner.route_agent.plan.await_count,1)
        self.planner.locate_stops.assert_not_awaited()
        self.planner.weather_context.assert_not_awaited()
        self.planner.query_ticket_prices.assert_not_awaited()

    async def test_explicit_half_day_is_preserved_with_visible_reason(self):
        state = self.state()
        state["requirements"]["preferences"] = "只玩半天，下午自己安排"
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=ready(["a","b"],
            short_day_reason="用户明确要求只玩半天，下午自行安排")))
        result = await self.planner.activities(state)
        self.assertEqual(self.planner.route_agent.plan.await_count,1)
        self.assertTrue(any("用户明确要求只玩半天" in gap for gap in result["itinerary"]["days"][0]["gaps"]))

    async def test_unfilled_day_is_explained_without_unbounded_retries(self):
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=ready(["a","b"])))
        result = await self.planner.activities(self.state())
        self.assertEqual(self.planner.route_agent.plan.await_count,2)
        self.assertTrue(any("仍未补齐下午活动" in gap for gap in result["itinerary"]["days"][0]["gaps"]))

    async def test_fewer_activities_and_selected_item_revisions_do_not_refill_the_day(self):
        for extra in ({"change":{"fewer_activities":True}}, {"revision_selected_ids":["a"]}, {"keep_current_plan":True}):
            with self.subTest(extra=extra):
                self.planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=ready(["a","b"])))
                state = {**self.state(),**extra}
                await self.planner.activities(state)
                self.assertEqual(self.planner.route_agent.plan.await_count,1)

    async def test_resource_refresh_retains_actual_old_duration_and_times(self):
        state = self.state({"park":stop("park")},["park"])
        state["change"] = {"refresh_resources":True}
        state["old_itinerary"] = {"days":[{"date":DATE,"activities":[
            {"id":"park","kind":"visit","start":"10:00","end":"17:00"}]}]}
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock())
        result = await self.planner.activities(state)
        visit = next(a for a in result["itinerary"]["days"][0]["activities"] if a["kind"] == "visit")
        self.assertEqual((visit["start"],visit["end"]),("10:00","17:00"))
        self.assertEqual(visit["duration_minutes"],420)
        self.assertIsNone(visit.get("appointment_time"))
        self.planner.route_agent.plan.assert_not_awaited()

    async def test_required_visit_is_not_given_an_appointment_from_its_old_start(self):
        state = self.state({"museum":stop("museum",60)},["museum"])
        original = {"days":[{"date":DATE,"city":"杭州","activities":[{
            **state["stops"]["museum"],"kind":"visit","start":"14:09","end":"16:09",
            "appointment_time":None,"map_place":{"name":"museum","location":"120,30"},"move_before":{},
            "ticket_quote":{"price":50,"status":"reference","evidence_id":"query:ticket"}}],
            "weather":{},"gaps":[],"return_move":{}}],"transport":[],"hotels":[]}
        state.update(repair_only=True,mode="modify",old_itinerary=copy.deepcopy(original),itinerary=copy.deepcopy(original),
                     change={"scopes":["activity"],"dates":[DATE]},assessment_dates=[DATE],
                     stop_constraints={"required":["museum"]})
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=ready(["museum"],durations={"museum":60})))
        result = await self.planner.activities(state)
        visit = next(item for item in result["itinerary"]["days"][0]["activities"] if item["kind"] == "visit")
        self.assertEqual(visit["start"],"09:00")
        self.assertEqual(visit["duration_minutes"],60)
        self.assertIsNone(visit["appointment_time"])
        self.assertEqual(visit["ticket_quote"],original["days"][0]["activities"][0]["ticket_quote"])
        self.assertEqual(self.planner.route_agent.plan.call_args.args[2]["preserved_activities"],[])

    async def test_preserved_schedule_keeps_a_real_appointment_without_creating_one(self):
        state = self.state({"museum":stop("museum",60)},["museum"])
        window = (540,1200,[],[],{"name":None,"start_name":None,"label":"结束当天安排"})
        for appointment in (None,"10:00"):
            with self.subTest(appointment=appointment):
                preserved = {"museum":{"start":"10:00","end":"11:00","appointment_time":appointment}}
                ordering = OrderChoice(stop_ids=["museum"],start_times={"museum":"09:30"})
                day = await self.planner.schedule_activity_day(state,state["selected_route"]["days"][0],
                    ordering,{"museum"},window,{},preserved)
                visit = next(item for item in day["activities"] if item["kind"] == "visit")
                self.assertEqual(visit["start"],"10:00")
                self.assertEqual(visit["appointment_time"],appointment)

    async def test_rejected_late_opening_stop_does_not_consume_later_stops_time(self):
        state = self.state({"a":stop("a",60),"late":stop("late",120,opening_claim="17:00-18:00"),"b":stop("b",60)},
                           ["a","late","b"])
        day = state["selected_route"]["days"][0]
        window = (540,1200,[],[],{"name":None,"start_name":None,"label":"结束当天安排"})
        scheduled = await self.planner.schedule_activity_day(state,day,OrderChoice(stop_ids=day["stop_ids"]),set(),window,{})
        visits = [a for a in scheduled["activities"] if a["kind"] == "visit"]
        self.assertEqual([a["id"] for a in visits],["a","b"])
        self.assertEqual(visits[1]["start"],"10:35")
        self.assertFalse(any(a["kind"] == "rest" and a.get("meal") != "lunch" for a in scheduled["activities"]))

    async def test_daily_poi_opening_is_enforced_even_when_the_model_omits_start_times(self):
        ids = ["mall","museum","theatre","park","garden"]
        stops = {identity:stop(identity,duration) for identity,duration in zip(ids,(45,120,45,60,45))}
        stops["mall"]["map_place"] = opening_place()
        state = self.state(stops,ids)
        day = state["selected_route"]["days"][0]
        window = (540,1200,[],[],{"name":None,"start_name":None,"label":"结束当天安排"})
        scheduled = await self.planner.schedule_activity_day(state,day,OrderChoice(stop_ids=ids),set(),window,{})
        visits = [item for item in scheduled["activities"] if item["kind"] == "visit"]
        self.assertEqual([item["id"] for item in visits],ids)
        self.assertEqual((visits[0]["start"],visits[0]["end"]),("11:00","11:45"))
        self.assertEqual(visits[0]["opening_window"],[660,1320])
        self.assertEqual(visits[0]["opening_reference"]["evidence_id"],"query:synthetic-opening")
        self.assertLessEqual(visits[-1]["end"],"20:00")
        self.assertTrue(any(item.get("meal") == "lunch" for item in scheduled["activities"]))
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=ready(ids)))
        await self.planner.daily_order(state,day,ids,set(),{})
        payload = self.planner.route_agent.plan.call_args.args[2]
        self.assertEqual(payload["opening_constraints"]["mall"]["windows"],[[660,1320]])

    async def test_daily_split_windows_choose_the_afternoon_instead_of_rejecting_the_stop(self):
        state = self.state({"mall":stop("mall",45,map_place=opening_place("每日09:00-11:00；15:00-17:00"))},["mall"])
        window = (720,1200,[],[],{"name":None,"start_name":None,"label":"结束当天安排"})
        scheduled = await self.planner.schedule_activity_day(state,state["selected_route"]["days"][0],
            OrderChoice(stop_ids=["mall"]),set(),window,{})
        visit = next(item for item in scheduled["activities"] if item["kind"] == "visit")
        self.assertEqual(visit["start"],"15:00")
        self.assertEqual(visit["opening_window"],[900,1020])

    async def test_visit_may_end_after_last_entry_but_not_start_after_it(self):
        state = self.state({"museum":stop("museum",45,map_place=opening_place(MUSEUM_HOURS))},["museum"])
        state["selected_route"]["days"][0]["date"] = "2026-10-14"
        day = state["selected_route"]["days"][0]
        window = (15*60+30,1200,[],[],{"name":None,"start_name":None,"label":"结束当天安排"})
        scheduled = await self.planner.schedule_activity_day(state,day,OrderChoice(stop_ids=["museum"]),set(),window,{})
        visit = next(item for item in scheduled["activities"] if item["kind"] == "visit")
        self.assertEqual((visit["start"],visit["end"]),("15:30","16:15"))
        late_window = (16*60+1,1200,[],[],window[-1])
        scheduled = await self.planner.schedule_activity_day(state,day,OrderChoice(stop_ids=["museum"]),set(),late_window,{})
        self.assertFalse(any(item["kind"] == "visit" for item in scheduled["activities"]))
        scheduled = await self.planner.schedule_activity_day(state,day,OrderChoice(stop_ids=["museum"]),{"museum"},late_window,{})
        self.assertTrue(any(item["kind"] == "visit" for item in scheduled["activities"]))

    async def test_park_estimate_is_clipped_to_closing_with_one_internal_lunch(self):
        state = self.state({"zoo":stop("zoo",540,map_place=opening_place(ZOO_HOURS))},["zoo"])
        state["requirements"].update(start_date=ZOO_DATE,end_date=ZOO_DATE)
        state["selected_route"]["days"][0]["date"] = ZOO_DATE
        window = (540,1200,[],[],{"name":None,"start_name":None,"label":"结束当天安排"})
        scheduled = await self.planner.schedule_activity_day(state,state["selected_route"]["days"][0],
            OrderChoice(stop_ids=["zoo"],durations={"zoo":540},start_times={"zoo":"09:30"}),set(),window,{})
        visit = next(item for item in scheduled["activities"] if item["kind"] == "visit")
        self.assertEqual((visit["start"],visit["end"]),("09:30","18:00"))
        self.assertEqual(visit["duration_minutes"],510)
        self.assertEqual(visit["visit_minutes"],450)
        self.assertEqual(visit["requested_visit_minutes"],540)
        self.assertEqual((visit["meal_break"]["start"],visit["meal_break"]["end"]),("12:00","13:00"))
        self.assertTrue(visit["duration_adjustment"])
        self.assertEqual(len([item for item in scheduled["activities"] if item["kind"] == "visit"]),1)

    async def test_park_clipping_reserves_actual_station_transfer_and_buffer(self):
        state = self.state({"zoo":stop("zoo",540,map_place=opening_place(ZOO_HOURS))},["zoo"])
        state["selected_route"]["days"][0]["date"] = ZOO_DATE
        window = (540,1020,[],[],{"name":"车站","start_name":None,"label":"前往车站"})
        scheduled = await self.planner.schedule_activity_day(state,state["selected_route"]["days"][0],
            OrderChoice(stop_ids=["zoo"],durations={"zoo":540},start_times={"zoo":"09:30"}),set(),window,{})
        visit = next(item for item in scheduled["activities"] if item["kind"] == "visit")
        self.assertEqual(visit["end"],"16:25")
        self.assertEqual(scheduled["return_move"]["minutes"],15)

    async def test_explicit_duration_or_locked_activity_is_not_silently_shortened(self):
        for extra in ({"locks":["zoo"]},{"requirements":{"preferences":"长隆至少游玩9小时"}}):
            with self.subTest(extra=extra):
                state = self.state({"zoo":stop("zoo",540,map_place=opening_place(ZOO_HOURS))},["zoo"])
                state["selected_route"]["days"][0]["date"] = ZOO_DATE
                if "requirements" in extra:
                    state["requirements"].update(extra["requirements"])
                else:
                    state.update(extra)
                window = (540,1200,[],[],{"name":None,"start_name":None,"label":"结束当天安排"})
                scheduled = await self.planner.schedule_activity_day(state,state["selected_route"]["days"][0],
                    OrderChoice(stop_ids=["zoo"],durations={"zoo":540},start_times={"zoo":"09:30"}),{"zoo"},window,{})
                visit = next(item for item in scheduled["activities"] if item["kind"] == "visit")
                self.assertEqual(visit["duration_minutes"],600)
                self.assertFalse(visit["duration_adjustment"])

    async def test_user_fixed_time_is_not_silently_erased_by_reference_hours(self):
        state = self.state({"mall":stop("mall",45,map_place=opening_place())},["mall"])
        window = (540,1200,[],[],{"name":None,"start_name":None,"label":"结束当天安排"})
        scheduled = await self.planner.schedule_activity_day(state,state["selected_route"]["days"][0],
            OrderChoice(stop_ids=["mall"],start_times={"mall":"09:00"}),{"mall"},window,{})
        visit = next(item for item in scheduled["activities"] if item["kind"] == "visit")
        self.assertEqual(visit["appointment_time"],"09:00")
        self.assertGreaterEqual(visit["start"],"11:00")

    async def test_tail_and_opening_limits_still_remove_an_infeasible_optional_stop(self):
        state = self.state({"a":stop("a",60),"b":stop("b",60)},["a","b"])
        window = (540,720,[],[],{"name":"车站","start_name":None,"label":"前往车站"})
        scheduled = await self.planner.schedule_activity_day(state,state["selected_route"]["days"][0],
            OrderChoice(stop_ids=["a","b"]),set(),window,{})
        self.assertEqual([a["id"] for a in scheduled["activities"]],["a"])
        self.assertIsNone(early_finish(scheduled))

    async def test_lunch_takes_a_real_hour_between_visits_and_does_not_add_a_restaurant(self):
        state = self.state({"a":stop("a",90),"b":stop("b",90),"c":stop("c",180)},["a","b","c"])
        day = state["selected_route"]["days"][0]
        window = (540,1200,[],[],{"name":None,"start_name":None,"label":"结束当天安排"})
        scheduled = await self.planner.schedule_activity_day(state,day,OrderChoice(stop_ids=day["stop_ids"]),set(),window,{})
        lunch = next(a for a in scheduled["activities"] if a.get("meal") == "lunch")
        self.assertEqual((lunch["start"],lunch["end"]),("12:35","13:35"))
        self.assertNotIn("source_id",lunch)
        self.assertNotIn("move_before",lunch)
        visits = [a for a in scheduled["activities"] if a["kind"] == "visit"]
        self.assertEqual([a["id"] for a in visits],["a","b","c"])
        self.assertEqual(visits[-1]["start"],"14:10")
        issues = check_itinerary({"days":[scheduled]},REQ)["issues"]
        self.assertFalse(any(i["code"] in {"overlap","move_gap","lunch_missing"} for i in issues))

    async def test_whole_day_park_contains_one_lunch_without_duplicate_visits(self):
        state = self.state({"park":stop("park",420)},["park"])
        window = (540,1200,[],[],{"name":None,"start_name":None,"label":"结束当天安排"})
        scheduled = await self.planner.schedule_activity_day(state,state["selected_route"]["days"][0],
            OrderChoice(stop_ids=["park"]),set(),window,{})
        self.assertEqual(len(scheduled["activities"]),1)
        visit = scheduled["activities"][0]
        self.assertEqual((visit["start"],visit["end"]),("09:00","17:00"))
        self.assertEqual((visit["meal_break"]["start"],visit["meal_break"]["end"]),("12:00","13:00"))
        self.assertEqual(visit["visit_minutes"],420)
        self.assertFalse(any(i["code"] == "lunch_missing" for i in check_itinerary({"days":[scheduled]},REQ)["issues"]))

    async def test_visit_ending_at_1317_reserves_lunch_before_it(self):
        state = self.state({"a":stop("a",120),"b":stop("b",90),"c":stop("c",150)},["a","b","c"])
        self.planner.movement = AsyncMock(return_value=({"minutes":27,"mode":"transit"},None))
        window = (540,1200,[],[],{"name":None,"start_name":None,"label":"结束当天安排"})
        scheduled = await self.planner.schedule_activity_day(state,state["selected_route"]["days"][0],
            OrderChoice(stop_ids=["a","b","c"]),set(),window,{})
        lunch = next(a for a in scheduled["activities"] if a.get("meal") == "lunch")
        self.assertEqual((lunch["start"],lunch["end"]),("11:30","12:30"))
        issues = check_itinerary({"days":[scheduled]},REQ)["issues"]
        self.assertFalse(any(i["code"] in {"overlap","move_gap","lunch_missing"} for i in issues))

    async def test_model_start_time_does_not_protect_an_impossible_optional_duration(self):
        state = self.state({"park":stop("park",660)},["park"])
        window = (540,1200,[],[],{"name":None,"start_name":None,"label":"结束当天安排"})
        scheduled = await self.planner.schedule_activity_day(state,state["selected_route"]["days"][0],
            OrderChoice(stop_ids=["park"],start_times={"park":"09:30"}),set(),window,{})
        self.assertFalse(any(item["kind"] == "visit" for item in scheduled["activities"]))
        self.assertTrue(scheduled["gaps"])

    async def test_lunch_fits_before_a_fixed_afternoon_appointment(self):
        state = self.state({"a":stop("a",60),"b":stop("b",120)},["a","b"])
        window = (540,1200,[],[],{"name":None,"start_name":None,"label":"结束当天安排"})
        scheduled = await self.planner.schedule_activity_day(state,state["selected_route"]["days"][0],
            OrderChoice(stop_ids=["a","b"],start_times={"b":"13:00"}),set(),window,{})
        lunch = next(a for a in scheduled["activities"] if a.get("meal") == "lunch")
        visit = next(a for a in scheduled["activities"] if a["id"] == "b")
        self.assertEqual((lunch["start"],lunch["end"]),("11:45","12:45"))
        self.assertEqual(visit["start"],"13:00")

    async def test_resource_refresh_preserves_existing_internal_lunch_without_adding_another_hour(self):
        state = self.state({"park":stop("park",420)},["park"])
        state["change"] = {"refresh_resources":True}
        old = {"id":"park","kind":"visit","start":"09:00","end":"17:00","visit_minutes":420,
               "meal_break":{"start":"12:00","end":"13:00","duration_minutes":60,"adjustment":"内部用餐"}}
        state["old_itinerary"] = {"days":[{"date":DATE,"activities":[old]}]}
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock())
        result = await self.planner.activities(state)
        visit = result["itinerary"]["days"][0]["activities"][0]
        self.assertEqual(visit["end"],"17:00")
        self.assertEqual(visit["meal_break"],old["meal_break"])
        self.assertEqual(visit["visit_minutes"],420)

    def test_continuous_full_day_without_lunch_is_returned_as_a_timing_problem(self):
        visit = {"id":"park","kind":"visit","name":"园区","start":"09:00","end":"17:00","evidence":{"quote":"攻略"}}
        itinerary = {"days":[{"city":"杭州","date":DATE,"activities":[visit]}]}
        self.assertTrue(any(i["code"] == "lunch_missing" and i["blocking"] for i in check_itinerary(itinerary,REQ)["issues"]))
        visit["end"] = "11:00"
        self.assertFalse(any(i["code"] == "lunch_missing" for i in check_itinerary(itinerary,REQ)["issues"]))
