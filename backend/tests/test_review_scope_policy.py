"""Review scope policy with synthetic data and mocked providers."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.adapters import ToolError
from app.agents.reviewer import ReviewerAgent
from app.decisions import confirmation_blockers
from app.models import Constraint, Requirements, ReviewReport, REVIEW_DIMENSIONS
from app.planner import Planner
from app.reviewing import correct_transfer_arithmetic, review_payload, review_scope, review_semantic_errors
from app.rules import check_itinerary


DATE = "2026-10-16"
MUSEUM_HOURS = "7月-8月 周二-周日 09:00-21:00开放 最晚进入20:00； 1月-6月, 9月-12月 周二-周日 09:00-17:00开放 最晚进入16:00； 周一 全天不开放(法定节假日除外)"
REQ = Requirements(origin="广州",cities=["广州"],start_date=DATE,end_date=DATE,
                   people=1,rooms=1,budget=3000,return_to_origin=False).model_dump(mode="json")


def plan():
    return {"days":[{"date":DATE,"city":"广州","activities":[{
        "id":"guide:a","name":"合成场馆","kind":"visit","start":"09:00","end":"10:00",
        "source_id":"guide","evidence":{"quote":"合成攻略"},"map_place":{"location":"113.3,23.1"},
        "move_before":{}}],"available_start":540,"tail_deadline":1200,"return_move":{},"gaps":[]}],
        "hotels":[],"transport":[],"issues":[],"budget":{"status":"incomplete","unknown":["票价未计入"],
        "known_subtotal":0,"known_plus_estimates":100,"estimated_tickets":0}}


def report(message=None, path="/days/0", summary=None):
    value = {"status":"pass_with_warnings","summary":summary or "费用待核实，未发现开放时间冲突",
             "checks":[{"dimension":key,"result":"warning" if key == "budget" else "ok","note":"合成检查"}
                       for key in sorted(REVIEW_DIMENSIONS)],"findings":[]}
    if message:
        value.update(status="revise",summary=summary or message,findings=[{
            "code":"synthetic","severity":"blocker","owner":"route","action":"repair","target":"guide:a",
            "dates":[DATE],"field_path":path,"message":message,"evidence_ids":["requirements"],"suggestion":"按真实依据处理"}])
    return ReviewReport(**value)


class ScopePolicy(unittest.TestCase):
    def setUp(self):
        self.plan = plan()
        self.payload = {"plan":self.plan,"requirements":REQ,"evidence_index":{"requirements":REQ}}

    def transfer_report(self):
        first = self.plan["days"][0]["activities"][0]
        first.update(name="永庆坊",start="12:57",end="15:27")
        destination = copy.deepcopy(first)
        destination.update(id="guide:b",name="北京路",start="16:30",end="19:00",
            move_before={"provider":"amap","evidence_id":"query:route","minutes":43,"origin":"永庆坊","destination":"北京路"})
        self.plan["days"][0]["activities"].append(destination)
        value = report("永庆坊15:27结束，北京路16:30开始，预留63分钟，路线43分钟，换乘缓冲不足",path="/days/0/activities/1")
        value.findings[0].evidence_ids = ["query:route"]
        next(check for check in value.checks if check.dimension == "timing").result = "blocker"
        return value,destination

    def test_code_corrects_a_false_buffer_blocker_without_changing_the_route(self):
        value,_ = self.transfer_report()
        original = copy.deepcopy(self.plan)
        result = correct_transfer_arithmetic(value,self.payload)
        self.assertEqual(result.status,"pass_with_warnings")
        self.assertEqual(result.findings[0].severity,"warning")
        self.assertIn("剩余20分钟",result.summary)
        self.assertEqual(value.findings[0].severity,"blocker")
        self.assertEqual(self.plan,original)
        self.assertFalse(any(check.result == "blocker" for check in result.checks))

    def test_true_shortfall_and_separately_evidenced_deadlines_are_preserved(self):
        value,destination = self.transfer_report()
        destination["move_before"]["minutes"] = 64
        self.assertEqual(correct_transfer_arithmetic(value,self.payload).status,"revise")
        destination["move_before"]["minutes"] = 43
        value.findings[0].evidence_ids.append("query:boarding-deadline")
        self.assertEqual(correct_transfer_arithmetic(value,self.payload).status,"revise")
        value.findings[0].evidence_ids = ["query:route"]
        buffer = Constraint(id="c_buffer",source="form",kind="transfer_buffer",mode="train",minutes=30,quote="至少预留30分钟缓冲")
        self.payload["requirements"] = {**REQ,"constraints":[buffer.model_dump(mode="json")]}
        self.assertEqual(correct_transfer_arithmetic(value,self.payload).status,"revise")

    def test_meals_and_waiting_are_already_excluded_from_available_transfer_time(self):
        value,destination = self.transfer_report()
        self.plan["days"][0]["activities"].insert(1,{
            "kind":"rest","name":"午餐","start":"15:27","end":"16:00"})
        value.findings[0].field_path = "/days/0/activities/2"
        transfer = review_scope(self.plan)["activities"][1]["transfer_timing"]
        self.assertEqual(transfer["available_minutes"],30)
        self.assertFalse(transfer["within_transfer_window"])
        self.assertEqual(correct_transfer_arithmetic(value,self.payload).status,"revise")
        destination["move_before"]["minutes"] = 30
        self.assertEqual(correct_transfer_arithmetic(value,self.payload).status,"pass_with_warnings")

    def test_correcting_one_false_transfer_claim_keeps_other_real_blockers(self):
        value,_ = self.transfer_report()
        other = value.findings[0].model_copy(update={"code":"real_deadline","message":"实际船班检票截止早于到达时间",
                                                   "evidence_ids":["query:boarding-deadline"]})
        value.findings.append(other)
        result = correct_transfer_arithmetic(value,self.payload)
        self.assertEqual(result.status,"revise")
        self.assertEqual([item.severity for item in result.findings],["warning","blocker"])
        self.assertIn("实际船班",result.summary)

    def test_calendar_is_program_computed_and_unselected_hotel_does_not_enable_legs(self):
        self.plan["hotels"] = [{"city":"广州","selected":{"name":"仅推荐酒店"}}]
        scope = review_scope(self.plan)["days"][0]
        self.assertEqual(scope["weekday"],"星期五")
        self.assertFalse(scope["hotel_location_confirmed"])
        self.assertFalse(scope["first_leg_required"])
        self.assertFalse(scope["last_leg_required"])

    def test_no_hotel_stale_boundary_moves_do_not_block_but_between_visits_still_does(self):
        day = self.plan["days"][0]
        day.update(start_place="旧酒店",end_place="旧酒店",return_move={"minutes":None},tail_arrival=1300)
        day["activities"][0]["move_before"] = {"minutes":None,"estimate_minutes":45}
        self.assertEqual(confirmation_blockers(self.plan,requirements=REQ),[])
        self.assertFalse(any(item["code"] in {"move_gap","tail_conflict"} for item in check_itinerary(self.plan,REQ)["issues"]))
        second = copy.deepcopy(day["activities"][0])
        second.update(id="guide:b",name="第二处场馆",start="10:45",end="11:45")
        day["activities"].append(second)
        self.assertTrue(any("第二处场馆" in text for text in confirmation_blockers(self.plan,requirements=REQ)))

    def test_adopted_hotel_enables_real_boundary_checks(self):
        self.plan["hotels"] = [{"id":"hotel:广州","city":"广州","selected_by_user":True,
            "context":{"check_in":DATE,"check_out":"2026-10-17"},"selected":{"name":"用户采用的酒店"}}]
        day = self.plan["days"][0]
        day.update(start_place="用户采用的酒店",end_place="用户采用的酒店",return_move={"minutes":None},tail_arrival=1300)
        scope = review_scope(self.plan)["days"][0]
        self.assertTrue(scope["last_leg_required"])
        self.assertTrue(any("结束路段" in text for text in confirmation_blockers(self.plan,requirements=REQ)))
        self.assertTrue(any(item["code"] == "tail_conflict" for item in check_itinerary(self.plan,REQ)["issues"]))

    def test_selected_train_still_requires_station_transfer_without_a_hotel(self):
        self.plan["transport"] = [{"context":{"date":DATE,"origin":"广州","destination":"深圳"},
                                   "selected":{"name":"用户采用车次"}}]
        scope = review_scope(self.plan)["days"][0]
        self.assertFalse(scope["first_leg_required"])
        self.assertTrue(scope["last_leg_required"])

    def test_weekday_mistake_and_inapplicable_return_blocker_are_rejected(self):
        self.assertTrue(review_semantic_errors(report("2026-10-16为周二闭馆"),self.payload))
        self.assertTrue(review_semantic_errors(report("住宿未选定，首站与返程均未提供路线"),self.payload))
        self.assertFalse(review_semantic_errors(report(),self.payload))

    def test_unknown_cost_is_a_disclosure_not_free_or_automatic_over_budget(self):
        bad = report("未知门票未计入，无法证明预算满足，因此不能通过",path="/budget")
        self.assertTrue(review_semantic_errors(bad,self.payload))
        self.assertFalse(review_semantic_errors(report(),self.payload))
        self.plan["budget"]["status"] = "over"
        self.assertFalse(review_semantic_errors(report("已知加估算费用超过预算",path="/budget"),self.payload))

    def test_missing_ticket_price_is_not_treated_like_proven_unavailability(self):
        self.assertTrue(review_semantic_errors(report("门票票价仍为null，不能通过"),self.payload))
        self.assertFalse(review_semantic_errors(report("门票已售罄，无法预约"),self.payload))

    def test_system_price_placeholder_is_unknown_without_becoming_a_missing_price_blocker(self):
        self.plan["days"][0]["activities"][0]["ticket_quote"] = {"price":0,"assumed_free":True,"status":"price_unavailable"}
        bad = report("门票未返回价格却按免费0元计入，必须取得票价才能通过",path="/days/0/activities/0")
        self.assertTrue(any("预算按0元计算" in text and "不使预算不完整" in text for text in review_semantic_errors(bad,self.payload)))
        self.assertFalse(review_semantic_errors(report("门票已售罄，无法预约",path="/days/0/activities/0"),self.payload))

    def test_zero_ticket_placeholder_is_not_a_budget_level_missing_cost_blocker(self):
        self.plan["days"][0]["activities"][0]["ticket_quote"] = {"price":0,"price_unknown":True,"assumed_free":True}
        self.plan["budget"]["status"] = "within"
        errors = review_semantic_errors(report("门票缺价按0元漏计，预算不完整",path="/budget"),self.payload)
        self.assertTrue(any("预算按0元计算" in item for item in errors))
        self.assertFalse(review_semantic_errors(report("已知加估算费用超过预算",path="/budget"),self.payload))

    def test_explicit_user_booking_prerequisite_is_still_enforced(self):
        booking = Constraint(id="c_booking",source="form",kind="booking_prerequisite",scope={"stop_ids":["guide:a"]},quote="预约成功后才安排参观")
        self.payload["requirements"] = {**REQ,"constraints":[booking.model_dump(mode="json")]}
        self.assertFalse(review_semantic_errors(report("预约未完成，尚未满足用户前置条件"),self.payload))
        self.payload["requirements"]["constraints"][0]["superseded_by"] = "c_newer"
        self.assertTrue(review_semantic_errors(report("预约未完成，尚未满足用户前置条件"),self.payload))

    def test_unsupported_opening_conflict_summary_is_rejected_but_negation_is_not(self):
        self.assertTrue(review_semantic_errors(report(summary="存在明确的开放时间冲突"),self.payload))
        self.assertFalse(review_semantic_errors(report(summary="未发现开放时间冲突，预算尚待核实"),self.payload))

    def test_report_state_inconsistency_cannot_be_handed_to_route_as_a_blocker(self):
        bad = report("16:40-17:40在10:00-18:00内，表面上未超出窗口；但当前审查结果与阻断状态不一致，需路线Agent重新核验并重排",
                     path="/days/0/activities/0")
        bad.findings[0].evidence_ids = ["query:one","query:two"]
        errors = review_semantic_errors(bad,self.payload)
        self.assertTrue(any("审查报告自身" in text for text in errors))
        self.assertFalse(review_semantic_errors(report("活动安排17:40-19:00，晚于18:00闭馆"),self.payload))

    def test_scope_is_present_in_payload_and_evidence_catalog(self):
        payload = review_payload({"requirements":REQ,"selected_source_ids":[]},self.plan,[],[])
        self.assertEqual(payload["review_scope"]["days"][0]["weekday"],"星期五")
        self.assertIn("review_scope",{item["evidence_id"] for item in payload["evidence_catalog"]})

    def museum(self):
        activity = self.plan["days"][0]["activities"][0]
        activity.update(start="13:13",end="16:13")
        activity["map_place"].update(id="museum",evidence_id="query:museum",opentime2=MUSEUM_HOURS)
        return activity

    def opening_report(self,message):
        value = report(message,path="/days/0/activities/0")
        value.findings[0].evidence_ids = ["query:museum"]
        return value

    def test_last_admission_applies_to_entry_not_departure(self):
        self.museum()
        timing = review_scope(self.plan)["activities"][0]
        self.assertTrue(timing["within_opening_window"])
        self.assertTrue(timing["entry_before_last_admission"])
        bad = self.opening_report("13:13-16:13，最晚进入16:00、17:00闭馆；当前结束超过闭馆时间，未明确入馆缓冲")
        self.assertTrue(review_semantic_errors(bad,self.payload))
        self.assertFalse(review_semantic_errors(report(),self.payload))

    def test_genuine_late_entry_and_closing_conflict_are_not_suppressed(self):
        activity = self.museum()
        for start,end in (("16:13","16:50"),("13:13","17:13")):
            with self.subTest(start=start,end=end):
                activity.update(start=start,end=end)
                self.assertFalse(review_semantic_errors(self.opening_report("活动晚于最晚入场16:00或17:00闭馆"),self.payload))

    def test_extra_closure_evidence_is_not_overridden_by_regular_opening_hours(self):
        self.museum()
        finding = self.opening_report("额外公告显示当日提前闭馆，当前安排超过临时开放窗口")
        finding.findings[0].evidence_ids.append("query:closure-announcement")
        self.assertFalse(review_semantic_errors(finding,self.payload))

    def test_review_payload_recovers_hours_from_exact_poi_detail_for_old_draft(self):
        activity = self.museum()
        activity["map_place"].pop("opentime2")
        row = {"evidence_id":"query:museum","provider":"amap","request":{"tool":"maps_search_detail","id":"museum"},
               "result":{"raw":{"id":"museum","location":"113.3,23.1","opentime2":MUSEUM_HOURS}}}
        state = {"requirements":REQ,"selected_source_ids":[]}
        payload = review_payload(state,self.plan,[],[row])
        self.assertTrue(payload["review_scope"]["activities"][0]["entry_before_last_admission"])
        self.assertNotIn("opentime2",activity["map_place"])
        row["result"]["raw"]["id"] = "other-branch"
        payload = review_payload(state,self.plan,[],[row])
        self.assertIsNone(payload["review_scope"]["activities"][0]["within_opening_window"])

    def test_unadopted_ticket_location_difference_only_warrants_a_warning(self):
        activity = self.museum()
        activity["ticket_quote"] = {"price":None,"status":"place_confirmation","candidates":[{
            "name":"场馆旧名","address":"同一地址","location":"113.31,23.11"}]}
        bad = report("活动地图地点与门票查询候选展区不同，不能直接采用该报价",path="/days/0/activities/0")
        self.assertTrue(review_semantic_errors(bad,self.payload))
        for quote in ({"price":100,"status":"reference"},
                      {"price":None,"status":"options","options":[{"price":100}]},
                      {"price":None,"status":"sold_out"}):
            activity["ticket_quote"] = quote
            self.assertFalse(review_semantic_errors(bad,self.payload))
        activity["ticket_quote"] = {"price":None,"status":"place_confirmation"}
        activity["product"] = {"id":"adopted-experience"}
        self.assertFalse(review_semantic_errors(bad,self.payload))

    def test_supplier_coordinates_never_override_a_verified_amap_poi(self):
        activity = self.museum()
        bad = report("飞猪与高德坐标不一致，定位未确认，必须重新选择场馆",path="/days/0/activities/0")
        for quote in ({"price":None,"status":"place_confirmation"},
                      {"price":50,"status":"reference"},
                      {"price":None,"status":"options","options":[{"price":50}]}):
            with self.subTest(quote=quote):
                activity["ticket_quote"] = quote
                self.assertTrue(review_semantic_errors(bad,self.payload))
        activity["product"] = {"id":"adopted-experience"}
        self.assertTrue(review_semantic_errors(bad,self.payload))
        different_venue = report("已采用门票展区不同，名称和地址不符，属于另一场馆",path="/days/0/activities/0")
        self.assertFalse(review_semantic_errors(different_venue,self.payload))
        activity["map_place"] = None
        self.assertFalse(review_semantic_errors(bad,self.payload))


class ReviewCorrection(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Mock()
        self.store.trip.return_value = {"active_run":"run"}
        self.store.run.return_value = {"status":"running"}
        self.state = {"trip_id":"trip","run_id":"run","generation":"generation"}
        self.payload = {"plan":plan(),"requirements":REQ,"evidence_index":{"requirements":REQ}}

    async def test_bad_weekday_gets_one_full_review_correction(self):
        models = SimpleNamespace(ask=AsyncMock(side_effect=[report("2026-10-16为周二闭馆"),report()]))
        result = await ReviewerAgent(models,self.store).review(self.state,self.payload)
        self.assertEqual(result.status,"pass_with_warnings")
        self.assertEqual(models.ask.await_count,2)
        self.assertTrue(models.ask.call_args.args[2]["semantic_errors"])

    async def test_false_transfer_arithmetic_is_corrected_without_another_model_or_route_call(self):
        fixture = ScopePolicy()
        fixture.setUp()
        bad,_ = fixture.transfer_report()
        self.payload = fixture.payload
        self.payload["evidence_index"]["query:route"] = {"provider":"amap"}
        models = SimpleNamespace(ask=AsyncMock(return_value=bad))
        result = await ReviewerAgent(models,self.store).review(self.state,self.payload)
        self.assertEqual(result.status,"pass_with_warnings")
        models.ask.assert_awaited_once()
        self.assertTrue(any(call.args[1].startswith("review_arithmetic_corrected:") for call in self.store.save_agent_result.call_args_list))

    async def test_two_invalid_reviews_are_not_accepted_as_a_pass(self):
        models = SimpleNamespace(ask=AsyncMock(return_value=report("住宿未选定，首站与返程均未提供路线")))
        with self.assertRaises(ToolError) as raised:
            await ReviewerAgent(models,self.store).review(self.state,self.payload)
        self.assertEqual(raised.exception.code,"review_semantics")
        self.assertEqual(models.ask.await_count,2)

    async def test_false_closing_blocker_gets_corrected_without_route_replanning(self):
        activity = self.payload["plan"]["days"][0]["activities"][0]
        activity.update(start="13:13",end="16:13")
        activity["map_place"].update(id="museum",evidence_id="query:museum",opentime2=MUSEUM_HOURS)
        bad = report("13:13-16:13超过闭馆时间17:00，最晚进入16:00",path="/days/0/activities/0")
        bad.findings[0].evidence_ids = ["query:museum"]
        self.payload["evidence_index"]["query:museum"] = {"opentime2":MUSEUM_HOURS}
        models = SimpleNamespace(ask=AsyncMock(side_effect=[bad,report()]))
        result = await ReviewerAgent(models,self.store).review(self.state,self.payload)
        self.assertEqual(result.status,"pass_with_warnings")
        self.assertEqual(models.ask.await_count,2)
        self.assertIn("最晚入场只比较start",models.ask.call_args.args[2]["semantic_errors"][0])

    async def test_theatre_self_contradiction_is_returned_to_reviewer_without_editing_plan(self):
        original = copy.deepcopy(self.payload["plan"])
        bad = report("活动结束17:40早于18:00；但当前审查结果与阻断状态不一致，需路线Agent重排",
                     path="/days/0/activities/0")
        models = SimpleNamespace(ask=AsyncMock(side_effect=[bad,report()]))
        result = await ReviewerAgent(models,self.store).review(self.state,self.payload)
        self.assertEqual(result.status,"pass_with_warnings")
        self.assertEqual(models.ask.await_count,2)
        self.assertEqual(self.payload["plan"],original)
        self.assertTrue(any("审查报告自身" in text for text in models.ask.call_args.args[2]["semantic_errors"]))

    async def test_no_hotel_ignores_origin_address_and_latest_return_without_querying_legs(self):
        planner = Planner.__new__(Planner)
        planner.movement = AsyncMock()
        state = {"requirements":{**REQ,"origin_address":"尚未采用的起点","latest_return":"18:00"},"itinerary":plan()}
        _,end,_,_,tail = await planner.window(state,{"date":DATE,"city":"广州"})
        self.assertEqual(end,1200)
        self.assertIsNone(tail["start_name"])
        self.assertIsNone(tail["name"])
        planner.movement.assert_not_awaited()


class TransportConnectionFacts(unittest.TestCase):
    def test_station_buffer_cannot_be_deducted_twice_but_extra_evidence_is_preserved(self):
        itinerary = plan()
        day = itinerary["days"][0]
        day.update(return_end="15:47",return_move={"minutes":47})
        itinerary["transport"] = [{"label":"广州到武汉","buffer_minutes":60,
            "context":{"origin":"广州","destination":"武汉","date":DATE},
            "selected":{"departure":DATE+" 17:03:00","departure_place":"广州南站"}}]
        payload = {"plan":itinerary,"requirements":REQ}
        value = report("广州南站进站缓冲不足，需要再次预留60分钟")
        self.assertTrue(any("重复扣除" in error for error in review_semantic_errors(value,payload)))
        value.findings[0].evidence_ids.append("query:real-boarding-rule")
        self.assertFalse(review_semantic_errors(value,payload))

    def test_missing_address_never_blocks_even_when_a_train_is_selected(self):
        itinerary = plan()
        itinerary["transport"] = [{"label":"已选班次","context":{"date":DATE,"origin":"广州","destination":"深圳"},
                                    "selected":{"departure_place":"广州南站","departure":DATE+" 18:00:00"}}]
        value = report("尚未提供出发地址，无法检查首末段")
        self.assertTrue(any("没有已选住宿" in error for error in review_semantic_errors(value,{"plan":itinerary,"requirements":REQ})))

    def test_departure_slack_already_includes_the_station_buffer(self):
        from app.reviewing import review_scope
        itinerary = {"transport":[{"label":"广州 → 武汉","buffer_minutes":60,"context":{"origin":"广州","destination":"武汉","date":"2026-12-13"},
                                   "selected":{"departure":"2026-12-13 17:03:00","departure_place":"广州南站"}}],
                     "days":[{"date":"2026-12-13","city":"广州","return_end":"15:47","return_move":{"minutes":47},
                              "activities":[{"kind":"visit","name":"广州塔","start":"14:25","end":"14:40"}]}]}
        connection = review_scope(itinerary)["days"][0]["transport_connections"][0]
        self.assertEqual((connection["latest_station_arrival"],connection["slack_minutes"],connection["within_station_buffer"]),
                         ("16:03",16,True))
