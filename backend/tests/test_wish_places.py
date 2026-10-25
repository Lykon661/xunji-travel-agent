"""User wishes use temporary storage and mocked workflow calls; no external services."""
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError

from app.agents.guide import GuideAgent
from app.conflicts import apply_option, visit_minutes
from app.day_clustering import build_spatial_plan
from app.decisions import input_hash, confirmation_blockers, route_structure
from app.dining import is_dining, meal_recommendations, refresh_meals, dining_wish_issues, plan_meal_wishes
from app.stop_types import is_light_wish, with_visit_defaults
from app.models import Candidate, GuideBrief, GuideGroup, OrderChoice, Requirements, RouteTurn, wish_stop_id, wish_stops
from app.places import canonicalize_stops, planning_stop_pool, duplicate_issues, occurrences
from app.planner import Planner
from app.rules import merge_candidate_duplicates, validate_candidate, check_itinerary
from app.reviewing import review_field_index
from app.store import Conflict, Store


A, B = "2026-11-10", "2026-11-11"
WISH = {"city":"杭州","name":"西湖"}
WISH_ID = wish_stop_id(WISH["city"],WISH["name"])
POI = {"id":"amap-west-lake","name":"西湖","cityname":"杭州","location":"120.14,30.25","evidence_id":"query:1"}


def requirements(**values):
    return Requirements(origin="北京",cities=["杭州"],start_date=A,end_date=B,people=2,rooms=1,budget=6000,
                        **values).model_dump(mode="json")


def state(req, stops=None, **values):
    return {"requirements":req,"stops":wish_stops(req) if stops is None else stops,
            "stop_constraints":{"required":list(req["required_stop_ids"]),"excluded":list(req["excluded_stop_ids"])},
            "locks":[],"change":{},"mode":"new","message":"",**values}


class WishPlaceModels(unittest.TestCase):
    def test_wish_ids_are_required_and_stale_wish_ids_are_removed(self):
        req = requirements(wish_places=[WISH],required_stop_ids=["guide:park","wish:deleted",WISH_ID])
        self.assertEqual(req["required_stop_ids"],["guide:park",WISH_ID])
        req = Requirements.model_validate({**req,"wish_places":[]}).model_dump(mode="json")
        self.assertEqual(req["required_stop_ids"],["guide:park"])

    def test_city_must_belong_to_destinations(self):
        with self.assertRaisesRegex(ValidationError,"目的地城市"):
            requirements(wish_places=[{**WISH,"city":"苏州"}])

    def test_wish_date_must_be_in_trip_range(self):
        for day in ("2026-11-09","2026-11-12"):
            with self.subTest(day=day), self.assertRaisesRegex(ValidationError,"旅行日期内"):
                requirements(wish_places=[{**WISH,"date":day}])
        for day in (A,B):
            self.assertEqual(requirements(wish_places=[{**WISH,"date":day}])["wish_places"][0]["date"],day)

    def test_duplicate_wishes_are_rejected_even_with_different_date_and_duration(self):
        with self.assertRaisesRegex(ValidationError,"重复填写"):
            requirements(wish_places=[WISH,{**WISH,"name":" 西 湖 ","date":B,"duration_minutes":120}])

    def test_required_wish_cannot_be_excluded(self):
        with self.assertRaisesRegex(ValidationError,"必去景点不能同时设为排除"):
            requirements(wish_places=[WISH],excluded_stop_ids=[WISH_ID])

    def test_stop_fields_and_identity_are_stable_across_schedule_edits(self):
        stop = wish_stops(requirements(wish_places=[{**WISH,"date":A,"duration_minutes":120}]))[WISH_ID]
        self.assertEqual({key:stop[key] for key in ("id","city","category","source_kind","assigned_date","duration_minutes")},
                         {"id":WISH_ID,"city":"杭州","category":"sight","source_kind":"user","assigned_date":A,"duration_minutes":120})
        self.assertTrue(WISH_ID.startswith("wish:"))
        self.assertEqual(stop["source_id"],WISH_ID)
        edited = wish_stops(requirements(wish_places=[{**WISH,"date":B,"duration_minutes":60}]))
        self.assertEqual(list(edited),[WISH_ID])
        self.assertEqual((edited[WISH_ID]["assigned_date"],edited[WISH_ID]["duration_minutes"]),(B,60))

    def test_optional_stop_fields_are_absent_when_not_specified(self):
        stop = wish_stops(requirements(wish_places=[WISH]))[WISH_ID]
        self.assertNotIn("assigned_date",stop)
        self.assertNotIn("duration_minutes",stop)
        self.assertEqual(wish_stops(requirements()),{})


class WishSpatialPlan(unittest.TestCase):
    def located(self, **values):
        req = requirements(wish_places=[{**WISH,**values}])
        stops = wish_stops(req)
        stops[WISH_ID]["map_place"] = deepcopy(POI)
        return req,canonicalize_stops(stops)

    def test_wish_enters_required_ids_and_is_scheduled(self):
        req,stops = self.located()
        plan = build_spatial_plan(state(req,stops),stops)
        self.assertEqual(plan["status"],"ready",plan["issues"])
        self.assertEqual(plan["required_ids"],[WISH_ID])
        self.assertIn(WISH_ID,{identity for day in plan["days"] for identity in day["stop_ids"]})
        self.assertEqual(plan["fixed_dates"],{})

    def test_assigned_date_enters_fixed_dates(self):
        req,stops = self.located(date=B,duration_minutes=120)
        plan = build_spatial_plan(state(req,stops),stops)
        self.assertEqual(plan["status"],"ready",plan["issues"])
        self.assertEqual(plan["fixed_dates"],{WISH_ID:B})
        self.assertEqual(next(day["date"] for day in plan["days"] if WISH_ID in day["stop_ids"]),B)
        self.assertEqual(plan["visit_minutes"][WISH_ID],120)

    def test_guide_duplicate_is_merged_and_stays_required(self):
        req,stops = self.located()
        guide_id = "guide:lake"
        stops = canonicalize_stops({guide_id:{**stops[WISH_ID],"id":guide_id,"source_id":"guide","source_kind":"guide"},**stops})
        for preferred in ([WISH_ID],[guide_id]):
            with self.subTest(preferred=preferred):
                pool,groups = planning_stop_pool(stops,preferred)
                self.assertEqual(len(pool),1)
                self.assertEqual(len(groups),1)
                plan = build_spatial_plan(state(req,stops),pool)
                self.assertEqual(plan["status"],"ready",plan["issues"])
                self.assertEqual(plan["required_ids"],preferred)
                self.assertEqual([identity for day in plan["days"] for identity in day["stop_ids"]],preferred)

    def test_unplaceable_wish_is_reported_instead_of_silently_dropped(self):
        req,stops = self.located(date=A,duration_minutes=720)
        plan = build_spatial_plan(state(req,stops),stops)
        self.assertEqual(plan["status"],"partial")
        self.assertIn(WISH_ID,plan["required_ids"])
        self.assertTrue(any("超出每日估计时间容量" in issue for issue in plan["issues"]),plan["issues"])
        itinerary = {"days":[{"date":A,"city":"杭州","available_start":540,"tail_deadline":720,"activities":[]}]}
        current = state(req,stops,selected_route={"days":[{"date":A,"city":"杭州","stop_ids":[WISH_ID]}]})
        from app.conflicts import day_demand
        demand = day_demand(current,itinerary,itinerary["days"][0],current["selected_route"]["days"][0])
        self.assertTrue(any(item["id"] == WISH_ID for item in demand))


    def test_rules_and_guide_agent_accept_user_stops(self):
        req,stops = self.located(date=B)
        current = state(req,stops)
        groups = GuideAgent.normalize_groups(current,[GuideGroup(city="杭州",stop_ids=[WISH_ID])])
        self.assertEqual(groups[0].stop_ids,[WISH_ID])
        candidate = Candidate(title="指定地点",reason="用户必去",days=[
            {"date":A,"city":"杭州","stop_ids":[]}, {"date":B,"city":"杭州","stop_ids":[WISH_ID]}])
        self.assertEqual(validate_candidate(candidate,Requirements(**req),stops).days[1].stop_ids,[WISH_ID])
        guide_id = "guide:lake"
        stops = canonicalize_stops({guide_id:{**stops[WISH_ID],"id":guide_id,"source_id":"guide","source_kind":"guide","assigned_date":None},**stops})
        candidate.days[0].stop_ids = [guide_id]
        merged = merge_candidate_duplicates(candidate,Requirements(**req),stops)
        self.assertEqual(merged.days[0].stop_ids,[])
        self.assertEqual(merged.days[1].stop_ids,[WISH_ID])

    def test_conflict_exclude_removes_wish_from_requirements_and_pool(self):
        req,stops = self.located(date=A)
        current = state(req,stops,candidates=[{"days":[{"date":A,"stop_ids":[WISH_ID]}]}])
        updates,_,_ = apply_option(current,{"type":"exclude","stop_id":WISH_ID,"date":A},"这次不去西湖")
        self.assertEqual(updates["requirements"]["wish_places"],[])
        self.assertEqual(updates["stop_constraints"],{"required":[],"excluded":[]})
        self.assertNotIn(WISH_ID,updates["stops"])
        self.assertEqual(updates["candidates"][0]["days"][0]["stop_ids"],[])
        self.assertEqual(Requirements(**updates["requirements"]).required_stop_ids,[])

    def test_conflict_move_updates_persisted_wish_date(self):
        req,stops = self.located(date=A)
        current = state(req,stops,candidates=[{"days":[{"date":A,"stop_ids":[WISH_ID]},{"date":B,"stop_ids":[]}]}])
        updates,dates,_ = apply_option(current,{"type":"move","stop_id":WISH_ID,"date":A,"to":B},"改到第二天")
        self.assertEqual(dates,[A,B])
        self.assertEqual(updates["requirements"]["wish_places"][0]["date"],B)
        self.assertEqual(wish_stops(updates["requirements"])[WISH_ID]["assigned_date"],B)
        self.assertEqual(updates["stops"][WISH_ID]["assigned_date"],B)
        self.assertEqual(Requirements(**updates["requirements"]).required_stop_ids,[WISH_ID])
        self.assertEqual(req["wish_places"][0]["date"],A)


class WishPlannerFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.req = requirements()
        self.trip = self.store.create_trip("想去地点测试",self.req)
        self.store.add_source(self.trip["id"],{
            "id":"guide","title":"已选攻略","kind":"manual","text":"合成资料","images":[],
            "extraction_cities":["杭州"],"extraction_version":4,
            "extraction":{"days":2,"stops":[{"id":"guide:park","city":"杭州","name":"合成公园","day":1,"category":"sight"}]}})
        self.planner = Planner.__new__(Planner)
        self.planner.store = self.store
        self.planner.tasks = {}
        self.planner.gates = {}
        self.planner.emit = Mock()
        self.planner.models = SimpleNamespace(ask=AsyncMock())
        self.planner.amap = SimpleNamespace(cache={})
        self.planner.graph = SimpleNamespace(aget_state=AsyncMock(),aupdate_state=AsyncMock())
        self.planner.execute = AsyncMock()
        self.planner.parse_source = AsyncMock()

    def save_requirements(self, req):
        doc = self.store.trip(self.trip["id"])["document"]
        doc["requirements"] = req
        doc["selected_source_ids"] = ["guide"]
        self.store.update(self.trip["id"],doc)

    async def submitted(self, **values):
        run = await self.planner.submit(self.trip["id"],0,selected_source_ids=["guide"],**values)
        task = self.planner.tasks.get(run)
        if task:
            await task
        return self.planner.execute.await_args.args[1]

    def test_source_stops_defaults_to_saved_wishes(self):
        self.save_requirements(requirements(wish_places=[WISH]))
        self.assertEqual(set(self.planner.source_stops(self.trip["id"],["guide"])),{"guide:park",WISH_ID})

    def test_source_stops_uses_explicit_unsaved_requirements(self):
        self.save_requirements(requirements(wish_places=[WISH]))
        other = {"city":"杭州","name":"灵隐寺","date":B,"duration_minutes":90}
        req = requirements(wish_places=[other])
        stops = self.planner.source_stops(self.trip["id"],["guide"],req)
        self.assertEqual(set(stops),{"guide:park",wish_stop_id("杭州","灵隐寺")})
        self.assertNotIn(WISH_ID,self.planner.source_stops(self.trip["id"],["guide"],requirements()))
        self.assertEqual(self.store.trip(self.trip["id"])["document"]["requirements"]["wish_places"][0]["name"],"西湖")

    async def test_prepare_guides_keeps_unsaved_user_wishes(self):
        req = requirements(wish_places=[{**WISH,"date":B}])
        result = await self.planner.prepare_guides(state(req,trip_id=self.trip["id"],selected_source_ids=["guide"]))
        self.assertEqual(result["stops"][WISH_ID]["assigned_date"],B)
        self.assertEqual(result["stop_constraints"]["required"],[WISH_ID])
        self.planner.parse_source.assert_not_awaited()

    async def test_prepare_guides_classifies_coffee_wish_as_required_visit(self):
        req = requirements(wish_places=[{"city":"杭州","name":"湖畔咖啡"}])
        identity = wish_stop_id("杭州","湖畔咖啡")
        result = await self.planner.prepare_guides(state(req,trip_id=self.trip["id"],selected_source_ids=["guide"]))
        self.assertEqual(result["stops"][identity]["category"],"sight")
        self.assertEqual(result["stops"][identity]["duration_minutes"],45)
        self.assertIn(identity,result["stop_constraints"]["required"])

    async def test_prepare_guides_classifies_amap_dining_wish_as_required_meal(self):
        await self.check_dining_wish("指定体验店",{"type":"餐饮服务","typecode":"050100"})

    async def check_dining_wish(self, name, place_fields):
        req = requirements(wish_places=[{"city":"杭州","name":name,"duration_minutes":60}])
        identity = wish_stop_id("杭州",name)
        current = state(req,trip_id=self.trip["id"],selected_source_ids=["guide"])
        original = self.planner.source_stops
        def located_stops(*args):
            stops = original(*args)
            stops[identity]["map_place"] = {**POI,**place_fields,"name":name}
            return stops
        with patch.object(self.planner,"source_stops",side_effect=located_stops):
            prepared = await self.planner.prepare_guides(current)
        stop = prepared["stops"][identity]
        self.assertEqual(stop["category"],"dining")
        self.assertNotIn(identity,prepared["stop_constraints"]["required"])
        self.assertTrue(is_dining(stop))
        self.assertTrue(is_dining({**stop,"source_kind":"guide"}))
        plan = build_spatial_plan({**current,**prepared},canonicalize_stops(prepared["stops"]))
        self.assertNotIn(identity,plan["required_ids"])
        self.assertFalse(any("必去地点未进入" in issue for issue in plan["issues"]),plan["issues"])
        # Revalidating persisted requirements re-adds wish IDs; spatial planning must still skip dining wishes.
        plan = build_spatial_plan({**current,"stops":prepared["stops"]},canonicalize_stops(prepared["stops"]))
        self.assertNotIn(identity,plan["required_ids"])
        self.assertFalse(any("必去地点未进入" in issue for issue in plan["issues"]),plan["issues"])
        self.planner.parse_source.assert_not_awaited()

    async def test_restaurant_wish_is_a_hard_meal_with_safe_links_when_unlocated(self):
        req = requirements(wish_places=[{"city":"杭州","name":"湖畔餐厅","duration_minutes":60}])
        identity = wish_stop_id("杭州","湖畔餐厅")
        stops = wish_stops(req)
        current = state(req,stops,trip_id=self.trip["id"])
        day = {"date":A,"city":"杭州","activities":[]}
        self.planner.update_day_places(current,day)
        [meal] = day["meal_recommendations"]
        self.assertEqual(meal["source_kind"],"user")
        self.assertTrue(meal["wish"])
        self.assertEqual(meal["meal"],"lunch")
        self.assertIsNone(meal["map_url"])
        self.assertIn("未能在高德唯一定位，请自行确认地址",meal["note"])

    async def test_guide_analysis_does_not_treat_meal_wish_as_a_visit_or_exclude_it(self):
        req = requirements(wish_places=[{"city":"杭州","name":"湖畔餐厅"}])
        identity = wish_stop_id("杭州","湖畔餐厅")
        self.planner.guide_agent = SimpleNamespace(brief=AsyncMock(return_value=GuideBrief(status="ready",summary="按用户需求安排",excluded_ids=[identity])))
        result = await self.planner.guide_analysis(state(req,trip_id=self.trip["id"]))
        self.assertEqual(result["stop_constraints"],{"required":[],"excluded":[]})
        self.assertEqual(result["requirements"]["wish_places"],req["wish_places"])

    async def test_discover_supports_an_itinerary_with_only_dining_wishes(self):
        req = requirements(wish_places=[{"city":"杭州","name":"湖畔餐厅"}])
        current = state(req,trip_id=self.trip["id"],selected_source_ids=["guide"])
        self.planner.locate_stops = AsyncMock(side_effect=lambda state,pool:pool)
        self.planner.weather_context = AsyncMock(return_value={})
        result = await self.planner.discover(current)
        [candidate] = result["candidates"]
        self.assertEqual([day["stop_ids"] for day in candidate["days"]],[[],[]])
        validate_candidate(Candidate(**candidate),Requirements(**req),result["stops"])
        itinerary = {"days":[{**day,"activities":[]} for day in candidate["days"]]}
        refresh_meals(itinerary,result["stops"],requirements=req)
        self.assertEqual(dining_wish_issues(itinerary,result["stops"],req),[])
        self.assertEqual(sum(len(day["meal_recommendations"]) for day in itinerary["days"]),1)

    async def test_discover_reclassifies_amap_dining_and_skips_visit_required_check(self):
        req = requirements(wish_places=[{"city":"杭州","name":"指定体验店"}])
        identity = wish_stop_id("杭州","指定体验店")
        stops = self.planner.source_stops(self.trip["id"],["guide"],req)
        async def locate(current,pool):
            return {key:{**stop,"map_place":{**POI,"id":key,"typecode":"050000" if key == identity else "110000"}} for key,stop in pool.items()}
        self.planner.locate_stops = AsyncMock(side_effect=locate)
        self.planner.weather_context = AsyncMock(return_value={})
        candidate = Candidate(title="两天",reason="餐饮由系统另行安排",days=[
            {"date":A,"city":"杭州","stop_ids":["guide:park"]},{"date":B,"city":"杭州","stop_ids":[]}])
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=RouteTurn(status="ready",candidate=candidate)))
        result = await self.planner.discover(state(req,stops,trip_id=self.trip["id"],selected_source_ids=["guide"]))
        self.assertEqual(len(result["candidates"]),1)
        payload = self.planner.route_agent.plan.await_args.args[2]
        self.assertNotIn(identity,payload["available_stops"])
        self.assertNotIn(identity,payload["spatial_plan"]["required_ids"])
        self.assertIn(identity,result["stops"])


    async def test_light_wish_is_scheduled_as_visit_with_default_or_user_duration(self):
        for minutes in (None,75):
            with self.subTest(minutes=minutes):
                wish = {"city":"杭州","name":"湖畔甜品"}
                if minutes:
                    wish["duration_minutes"] = minutes
                req = requirements(wish_places=[wish])
                identity = wish_stop_id("杭州",wish["name"])
                stops = wish_stops(req)
                stops[identity]["map_place"] = POI
                day = {"date":A,"city":"杭州","stop_ids":[identity]}
                window = (540,720,[],[],{"name":None,"start_name":None,"label":"结束当天安排"})
                order = OrderChoice(stop_ids=[identity],durations={identity:30} if minutes else {})
                result = await self.planner.schedule_activity_day(state(req,stops),day,order,{identity},window,{})
                [activity] = [item for item in result["activities"] if item["kind"] == "visit"]
                self.assertEqual(activity["duration_minutes"],minutes or 45)
                self.assertEqual(activity["source_kind"],"user")
                self.assertFalse(is_dining(activity))

    async def test_daily_route_payload_contains_planned_user_restaurant(self):
        req = requirements(wish_places=[{"city":"杭州","name":"湖畔餐厅","date":A}])
        meal_id = wish_stop_id("杭州","湖畔餐厅")
        stops = self.planner.source_stops(self.trip["id"],["guide"],req)
        stops[meal_id]["map_place"] = POI
        stops["guide:park"]["map_place"] = POI
        route_day = {"date":A,"city":"杭州","stop_ids":["guide:park"]}
        meal_plan = plan_meal_wishes([route_day],stops,req)
        current = state(req,stops,selected_route={"days":[route_day],"feasibility":{"meal_wishes":meal_plan}})
        self.planner.route_agent = SimpleNamespace(plan=AsyncMock(return_value=RouteTurn(status="ready",order=OrderChoice(stop_ids=["guide:park"]))))
        await self.planner.daily_order(current,route_day,["guide:park"],{"guide:park"},{})
        args = self.planner.route_agent.plan.await_args.args
        [meal] = args[2]["meal_wishes"]
        self.assertEqual((meal["id"],meal["name"],meal["date"]),(meal_id,"湖畔餐厅",A))
        self.assertEqual(meal["map_place"],POI)
        self.assertIn(meal["meal"],{"breakfast","lunch","dinner"})
        self.assertIn("用户指定的必去正餐",args[1])
        self.assertNotIn(meal_id,args[2]["available_stops"])

    async def test_editing_light_wish_duration_clears_the_default_marker(self):
        wish = {"city":"杭州","name":"湖畔咖啡"}
        before = requirements(wish_places=[wish])
        after = requirements(wish_places=[{**wish,"duration_minutes":75}])
        identity = wish_stop_id("杭州",wish["name"])
        result = await self.planner.coordinate(state(after,wish_stops(before),mode="modify",old_requirements=before))
        self.assertFalse(result["stops"][identity].get("duration_is_default",False))
        self.assertEqual(visit_minutes(result["stops"][identity],{},scheduled_minutes=30),75)



    async def test_coordinate_removes_wish_without_excluding_it(self):
        req = requirements(wish_places=[WISH])
        current = state(req,trip_id=self.trip["id"],change={"remove_stop_ids":[WISH_ID]})
        result = await self.planner.coordinate(current)
        self.assertEqual(result["requirements"]["wish_places"],[])
        self.assertEqual(result["stop_constraints"],{"required":[],"excluded":[]})
        self.assertNotIn(WISH_ID,result["stops"])
        self.assertEqual(Requirements(**result["requirements"]).required_stop_ids,[])
        self.planner.models.ask.assert_not_awaited()

    async def test_coordinate_removal_from_existing_route_replans_before_validation(self):
        req = requirements(wish_places=[WISH])
        current = state(req,trip_id=self.trip["id"],mode="modify",old_requirements=deepcopy(req),
                        change={"remove_stop_ids":[WISH_ID],"scopes":["activity"]},selected_source_ids=["guide"],
                        selected_route={"days":[{"date":A,"stop_ids":[WISH_ID]}]})
        result = await self.planner.coordinate(current)
        self.assertEqual(result["change"]["scopes"],["full"])
        self.assertEqual(self.planner.after_coordinate({**current,**result}),"guides")

    async def test_coordinate_preserves_user_location_and_allows_locked_user_source(self):
        req = requirements(wish_places=[WISH])
        stops = wish_stops(req)
        stops[WISH_ID].update(map_place=deepcopy(POI),map_selected_id=POI["id"],query_status="resolved")
        old = {"days":[{"date":A,"activities":[{"id":WISH_ID,"kind":"visit","source_id":WISH_ID,"source_kind":"user"}]}]}
        result = await self.planner.coordinate(state(req,stops,mode="modify",locks=[WISH_ID],old_itinerary=old,
                                                    old_requirements=req,selected_source_ids=["guide"]))
        self.assertEqual(result["stops"][WISH_ID]["map_place"],POI)
        self.assertEqual(result["stops"][WISH_ID]["map_selected_id"],POI["id"])

    async def test_coordinate_replans_changed_wishes_and_clears_old_optional_values(self):
        before = requirements(wish_places=[{**WISH,"date":A,"duration_minutes":120}])
        after = requirements(wish_places=[WISH])
        stops = wish_stops(before)
        stops[WISH_ID]["map_place"] = deepcopy(POI)
        result = await self.planner.coordinate(state(after,stops,mode="modify",old_requirements=before))
        self.assertEqual(result["change"]["scopes"],["full"])
        self.assertNotIn("assigned_date",result["stops"][WISH_ID])
        self.assertNotIn("duration_minutes",result["stops"][WISH_ID])
        self.assertEqual(result["stops"][WISH_ID]["map_place"],POI)

    async def test_submit_accepts_unsaved_wish_with_only_guide_selected(self):
        invocation = await self.submitted(requirements={"wish_places":[{**WISH,"date":B,"duration_minutes":120}]})
        self.assertEqual(invocation["selected_source_ids"],["guide"])
        self.assertEqual(invocation["stop_constraints"]["required"],[WISH_ID])
        self.assertEqual(invocation["stops"][WISH_ID]["assigned_date"],B)
        self.assertEqual(invocation["stops"][WISH_ID]["duration_minutes"],120)
        self.assertIn("guide:park",invocation["stops"])

    async def test_submit_accepts_saved_wishes(self):
        self.save_requirements(requirements(wish_places=[WISH]))
        invocation = await self.submitted()
        self.assertIn(WISH_ID,invocation["stops"])
        self.assertEqual(invocation["stop_constraints"]["required"],[WISH_ID])

    async def test_submit_removes_deleted_wishes(self):
        self.save_requirements(requirements(wish_places=[WISH]))
        invocation = await self.submitted(requirements={"wish_places":[]})
        self.assertNotIn(WISH_ID,invocation["stops"])
        self.assertEqual(invocation["stop_constraints"]["required"],[])

    async def test_submit_still_rejects_unknown_guide_constraints(self):
        with self.assertRaisesRegex(Conflict,"景点约束必须来自当前勾选的攻略"):
            await self.submitted(requirements={"wish_places":[WISH],"required_stop_ids":["other:park"]})
        self.planner.execute.assert_not_awaited()

    async def test_resource_refresh_validates_wishes_with_explicit_requirements(self):
        req = requirements(wish_places=[{**WISH,"date":A}])
        self.save_requirements(req)
        route = Candidate(title="现有路线",reason="指定地点",days=[
            {"date":A,"city":"杭州","stop_ids":[WISH_ID]}, {"date":B,"city":"杭州","stop_ids":["guide:park"]}])
        doc = self.store.trip(self.trip["id"])["document"]
        doc.update(selected_route=route.model_dump(mode="json"),itinerary={"days":[],"transport":[],"hotels":[]})
        self.store.update(self.trip["id"],doc)
        original = self.planner.source_stops
        self.planner.source_stops = Mock(wraps=original)
        invocation = await self.submitted(refresh_resources=True)
        self.assertIn(WISH_ID,invocation["stops"])
        self.assertTrue(invocation["change"]["refresh_resources"])
        self.assertEqual(self.planner.source_stops.call_count,3)
        for call in self.planner.source_stops.call_args_list:
            self.assertEqual(call.args,(self.trip["id"],["guide"],req))

    async def test_candidate_hash_and_validation_use_same_explicit_requirements(self):
        req = requirements(wish_places=[WISH])
        self.save_requirements(req)
        candidate = Candidate(id="candidate",title="路线",reason="用户要求",days=[
            {"date":A,"city":"杭州","stop_ids":[WISH_ID]}, {"date":B,"city":"杭州","stop_ids":["guide:park"]}],
            draft={"days":[]},draft_revision=1,plan_hash="plan")
        candidate.input_hash = input_hash(req,["guide"],self.planner.source_stops(self.trip["id"],["guide"],req),
                                         self.planner.selected_sources(self.trip["id"],["guide"]))
        doc = self.store.trip(self.trip["id"])["document"]
        doc.update(pending={"kind":"selection"},candidates=[candidate.model_dump(mode="json")])
        self.store.update(self.trip["id"],doc)
        run = self.store.start_run(self.trip["id"],0,"checkpoint",{})
        self.store.finish_run(run,"waiting",{})
        self.planner.graph.aget_state.return_value = SimpleNamespace(next=("selection",),values={"selected_source_ids":["guide"]})
        self.planner.assert_review = Mock()
        original = self.planner.source_stops
        self.planner.source_stops = Mock(wraps=original)
        await self.submitted(candidate_id="candidate",candidate_revision=1,candidate_hash="plan")
        self.assertEqual(self.planner.source_stops.call_count,3)
        for call in self.planner.source_stops.call_args_list:
            self.assertEqual(call.args,(self.trip["id"],["guide"],req))


class DiningWishPlanning(unittest.TestCase):
    def visit(self, identity, longitude, start, end):
        return {"id":identity,"kind":"visit","name":"公园"+identity,"start":start,"end":end,
                "map_place":{"id":identity,"location":f"{longitude},30"}}

    def setup_meals(self, places):
        req = requirements(wish_places=[item for item,_ in places])
        stops = wish_stops(req)
        for item,longitude in places:
            identity = wish_stop_id(item["city"],item["name"])
            if longitude is not None:
                stops[identity]["map_place"] = {"id":"poi-"+item["name"],"name":item["name"],"typecode":"050000",
                                                "location":f"{longitude},30"}
        itinerary = {"days":[
            {"date":A,"city":"杭州","activities":[self.visit("a-noon",120,"11:00","13:00"),self.visit("a-evening",120.1,"17:00","19:00")]},
            {"date":B,"city":"杭州","activities":[self.visit("b-noon",121,"11:00","13:00"),self.visit("b-evening",121.1,"17:00","19:00")]}],
            "hotels":[],"transport":[]}
        return req,canonicalize_stops(stops),itinerary

    def wish(self, name="指定餐厅", **values):
        return {"city":"杭州","name":name,**values}

    def placed(self, itinerary):
        return [(day["date"],row) for day in itinerary["days"] for row in day.get("meal_recommendations",[]) if row.get("wish")]

    def test_nearest_activity_selects_date_and_lunch_or_dinner(self):
        req,stops,itinerary = self.setup_meals([(self.wish("湖畔餐厅"),120),(self.wish("晚霞餐厅"),121.1)])
        refresh_meals(itinerary,stops,requirements=req)
        self.assertEqual([(day,row["meal"]) for day,row in self.placed(itinerary)],[(A,"lunch"),(B,"dinner")])
        self.assertTrue(all(row["source_kind"] == "user" for _,row in self.placed(itinerary)))
        self.assertEqual(dining_wish_issues(itinerary,stops,req),[])

    def test_breakfast_names_use_breakfast_and_first_visit(self):
        for name in ("合成早餐","合成早茶","合成早点","合成豆浆","合成包子","合成油条","肠粉(早)"):
            with self.subTest(name=name):
                req,stops,itinerary = self.setup_meals([(self.wish(name),121)])
                refresh_meals(itinerary,stops,requirements=req)
                [(day,row)] = self.placed(itinerary)
                self.assertEqual((day,row["meal"],row["nearby_activity"]),(B,"breakfast","公园b-noon"))

    def test_assigned_date_is_respected_even_when_other_date_is_closer(self):
        req,stops,itinerary = self.setup_meals([(self.wish(date=A),121.1)])
        refresh_meals(itinerary,stops,requirements=req)
        [(day,row)] = self.placed(itinerary)
        self.assertEqual(day,A)
        self.assertEqual(row["meal"],"dinner")
        self.assertEqual(dining_wish_issues(itinerary,stops,req),[])

    def test_date_outside_city_stay_is_reported_without_changing_date(self):
        req,stops,itinerary = self.setup_meals([(self.wish(date=B),120)])
        itinerary["days"][1]["city"] = "苏州"
        refresh_meals(itinerary,stops,requirements=req)
        self.assertEqual(self.placed(itinerary),[])
        [issue] = dining_wish_issues(itinerary,stops,req)
        self.assertTrue(issue["blocking"])
        self.assertIn("指定日期不在该城市",issue["message"])
        self.assertEqual(next(iter(stops.values()))["assigned_date"],B)

    def test_far_wish_is_kept_with_straight_line_distance_note(self):
        req,stops,itinerary = self.setup_meals([(self.wish(),123)])
        refresh_meals(itinerary,stops,requirements=req)
        [(_,row)] = self.placed(itinerary)
        self.assertGreater(row["distance_meters"],2000)
        self.assertIn("直线距离",row["note"])
        self.assertIn("距当天活动较远，需单独前往",row["note"])

    def test_unlocated_wish_is_kept_on_first_city_day_at_lunch(self):
        req,stops,itinerary = self.setup_meals([(self.wish("无坐标餐厅"),None)])
        refresh_meals(itinerary,stops,requirements=req)
        [(day,row)] = self.placed(itinerary)
        self.assertEqual((day,row["meal"]),(A,"lunch"))
        self.assertIsNone(row["distance_meters"])
        self.assertIn("未能在高德唯一定位，请自行确认地址",row["note"])
        self.assertEqual(dining_wish_issues(itinerary,stops,req),[])

    def test_unlocated_wish_respects_assigned_date_and_needs_no_visits(self):
        req,stops,itinerary = self.setup_meals([(self.wish(date=B),None)])
        for day in itinerary["days"]:
            day["activities"] = []
        refresh_meals(itinerary,stops,requirements=req)
        self.assertEqual([(day,row["meal"]) for day,row in self.placed(itinerary)],[(B,"lunch")])
        self.assertFalse(any("必去景点" in issue for issue in confirmation_blockers(itinerary,{"required":req["required_stop_ids"]},req,stops)))

    def test_slots_are_spread_before_stacking_wishes(self):
        wishes = [(self.wish("餐厅"+str(index),date=A),120) for index in range(3)]
        req,stops,itinerary = self.setup_meals(wishes)
        itinerary["days"][0]["activities"][1]["map_place"]["location"] = "120.005,30"
        refresh_meals(itinerary,stops,requirements=req)
        meals = [row["meal"] for _,row in self.placed(itinerary)]
        self.assertEqual(len(meals),3)
        self.assertEqual(set(meals),{"lunch","dinner"})
        self.assertEqual(dining_wish_issues(itinerary,stops,req),[])

    def test_empty_far_meal_slot_does_not_displace_a_nearby_meal(self):
        req,stops,itinerary = self.setup_meals([(self.wish("正餐一",date=A),120),(self.wish("正餐二",date=A),120.001)])
        refresh_meals(itinerary,stops,requirements=req)
        self.assertEqual([row["meal"] for _,row in self.placed(itinerary)],["lunch","lunch"])
        self.assertTrue(all(row["distance_meters"] <= 2000 and "距当天活动较远" not in row["note"] for _,row in self.placed(itinerary)))

    def test_planned_date_is_used_when_it_is_nearby(self):
        req,stops,itinerary = self.setup_meals([(self.wish(),120)])
        for activity in itinerary["days"][1]["activities"]:
            activity["map_place"]["location"] = "120.001,30"
        identity = next(iter(stops))
        refresh_meals(itinerary,stops,requirements=req,meal_plan={identity:{"date":B}})
        self.assertEqual(self.placed(itinerary)[0][0],B)
        self.assertEqual(itinerary["meal_wish_plan"][identity]["date"],B)

    def test_far_planned_date_falls_back_to_nearby_same_city_date(self):
        req,stops,itinerary = self.setup_meals([(self.wish(),120)])
        identity = next(iter(stops))
        refresh_meals(itinerary,stops,requirements=req,meal_plan={identity:{"date":B}})
        [(day,row)] = self.placed(itinerary)
        self.assertEqual(day,A)
        self.assertLessEqual(row["distance_meters"],2000)
        self.assertNotIn("距当天活动较远",row["note"])
        self.assertEqual(itinerary["meal_wish_plan"][identity]["date"],A)

    def test_unlocated_meal_map_place_is_explicitly_null_even_if_input_is_empty(self):
        req,stops,itinerary = self.setup_meals([(self.wish(),None)])
        next(iter(stops.values()))["map_place"] = {}
        refresh_meals(itinerary,stops,requirements=req)
        row = self.placed(itinerary)[0][1]
        self.assertIn("map_place",row)
        self.assertIsNone(row["map_place"])

    def test_feasibility_keeps_the_restaurant_date_after_review(self):
        req,stops,itinerary = self.setup_meals([(self.wish(),121)])
        refresh_meals(itinerary,stops,requirements=req)
        itinerary.update(budget={},issues=[])
        for day in itinerary["days"]:
            day.update(available_start=540,tail_deadline=1200,gaps=[])
        result = Planner.feasibility(itinerary)
        self.assertEqual(result["meal_wishes"],itinerary["meal_wish_plan"])
        self.assertEqual(next(iter(result["meal_wishes"].values()))["date"],B)

    def test_normal_recommendations_cannot_repeat_wish_and_wish_comes_first(self):
        req,stops,itinerary = self.setup_meals([(self.wish(),120)])
        identity = next(iter(stops))
        stops["guide:alias"] = {**stops[identity],"id":"guide:alias","source_kind":"guide"}
        stops["guide:other"] = {**stops[identity],"id":"guide:other","source_kind":"guide","name":"另一餐厅",
                                "map_place":{"id":"other","location":"120.001,30"}}
        stops = canonicalize_stops(stops)
        refresh_meals(itinerary,stops,requirements=req)
        rows = itinerary["days"][0]["meal_recommendations"]
        self.assertEqual(rows[0]["id"],identity)
        self.assertNotIn("guide:alias",{row["id"] for day in itinerary["days"] for row in day["meal_recommendations"]})
        self.assertIn("guide:other",{row["id"] for row in rows})
        self.assertEqual(duplicate_issues(itinerary,req),[])

    def test_partial_refresh_preserves_wish_on_untouched_day(self):
        req,stops,itinerary = self.setup_meals([(self.wish(),121.1)])
        refresh_meals(itinerary,stops,requirements=req)
        untouched = deepcopy(itinerary["days"][1])
        for _ in range(2):
            refresh_meals(itinerary,stops,dates={A},requirements=req)
            self.assertEqual(itinerary["days"][1],untouched)
            self.assertEqual(len(self.placed(itinerary)),1)
            self.assertEqual(dining_wish_issues(itinerary,stops,req),[])

    def test_partial_refresh_replaces_wish_on_rebuilt_day_exactly_once(self):
        req,stops,itinerary = self.setup_meals([(self.wish(date=B),121.1)])
        refresh_meals(itinerary,stops,requirements=req)
        untouched = deepcopy(itinerary["days"][0])
        for _ in range(2):
            refresh_meals(itinerary,stops,dates={B},requirements=req)
            self.assertEqual(itinerary["days"][0],untouched)
            self.assertEqual(len(self.placed(itinerary)),1)
            self.assertEqual(dining_wish_issues(itinerary,stops,req),[])

    def test_alias_wishes_share_one_meal_with_both_required_references(self):
        req,stops,itinerary = self.setup_meals([(self.wish("合成餐厅"),120),(self.wish("合成饭店"),120)])
        for stop in stops.values():
            stop["map_place"]["id"] = "same-place"
        stops = canonicalize_stops(stops)
        refresh_meals(itinerary,stops,requirements=req)
        self.assertEqual(len(self.placed(itinerary)),1)
        self.assertEqual(dining_wish_issues(itinerary,stops,req),[])

    def test_missing_duplicate_or_wrong_date_wish_is_a_blocking_issue(self):
        req,stops,itinerary = self.setup_meals([(self.wish(date=B),121)])
        [missing] = dining_wish_issues(itinerary,stops,req)
        self.assertTrue(missing["blocking"])
        self.assertTrue(any(item["code"] == "dining_wish" for item in check_itinerary(itinerary,req,stops)["issues"]))
        self.assertTrue(any("餐饮必去" in text for text in confirmation_blockers(itinerary,{"required":req["required_stop_ids"]},req,stops)))
        refresh_meals(itinerary,stops,requirements=req)
        row = deepcopy(self.placed(itinerary)[0][1])
        itinerary["days"][0]["meal_recommendations"].append(row)
        self.assertIn("重复安排",dining_wish_issues(itinerary,stops,req)[0]["message"])
        itinerary["days"][1]["meal_recommendations"] = []
        self.assertIn("指定日期",dining_wish_issues(itinerary,stops,req)[0]["message"])

    def test_breakfast_and_wish_fields_survive_review_and_structure_readers(self):
        req,stops,itinerary = self.setup_meals([(self.wish("早餐包子"),120)])
        refresh_meals(itinerary,stops,requirements=req)
        for day in itinerary["days"]:
            day["transfers"] = []
        rows = occurrences(itinerary)
        breakfast = next(row for row in rows if row["role"] == "meal_option")
        self.assertEqual(breakfast["meal"],"breakfast")
        self.assertEqual(breakfast["source_kind"],"user")
        self.assertTrue(breakfast["wish"])
        self.assertEqual(route_structure(itinerary)["meals"][0]["items"][0]["meal"],"breakfast")
        self.assertTrue(any("meal_recommendations/0" in row["field_path"] for row in review_field_index(itinerary)))
        self.assertEqual(duplicate_issues(itinerary,req),[])


class LightWishTypes(unittest.TestCase):
    def test_non_user_dining_classification_matches_head_rules(self):
        expected = {"老舍茶馆":False,"某某茶艺馆":False,"面包博物馆":False,"合成面包店":False,
                    "合成蛋糕店":False,"合成冷饮店":False,"合成冰淇淋店":False,"合成甜点店":False,
                    "合成早茶":False,"合成早餐":False,"合成早点":False,"合成豆浆":False,"合成包子":False,"合成油条":False,
                    "湖畔咖啡":True,"合成餐厅":True,"合成烘焙店":True,"合成饼店":True,"合成糖水店":True,
                    "咖啡博物馆":False,"传统小吃街":False}
        for source in ({},{"source_kind":"guide"},{"source_kind":"flyai"},{"source_kind":""}):
            for name,result in expected.items():
                with self.subTest(source=source,name=name):
                    self.assertEqual(is_dining({**source,"name":name}),result)
                    self.assertEqual(is_dining({**source,"name":name,"category":"sight"}),result)
                    self.assertTrue(is_dining({**source,"name":name,"category":"dining"}))
                    self.assertTrue(is_dining({**source,"name":name,"map_place":{"typecode":"050600"}}))
                    self.assertTrue(is_dining({**source,"name":name,"map_place":{"type":"餐饮服务"}}))

    def test_new_breakfast_keywords_only_expand_user_dining(self):
        for name in ("合成早餐","合成早茶","合成早点","合成豆浆","合成包子","合成油条"):
            with self.subTest(name=name):
                self.assertTrue(is_dining({"name":name,"source_kind":"user"}))
                self.assertFalse(is_dining({"name":name,"source_kind":"guide"}))

    def test_names_and_amap_light_food_types_are_user_visits(self):
        items = [{"name":name} for name in ("湖畔咖啡","甜品店","糖水铺","奶茶店","茶饮店","冷饮店","冰淇淋店","烘焙店","糕饼店","面包店")]
        items += [{"name":"品牌店","map_place":{"typecode":code}} for code in ("050500","050600","050700","050800","050900")]
        for item in items:
            with self.subTest(item=item):
                user = with_visit_defaults({**item,"source_kind":"user","category":"dining"})
                self.assertTrue(is_light_wish(user))
                self.assertFalse(is_dining(user))
                self.assertEqual(user["category"],"sight")
                self.assertEqual(user["duration_minutes"],45)
                guide = {**item,"source_kind":"guide","category":"dining"}
                self.assertTrue(is_dining(guide))
                self.assertNotIn("duration_minutes",with_visit_defaults(guide))

    def test_light_wish_uses_user_duration_and_does_not_change_id(self):
        wish = {"city":"杭州","name":"湖畔咖啡"}
        plain = wish_stops(requirements(wish_places=[wish]))
        explicit = wish_stops(requirements(wish_places=[{**wish,"duration_minutes":80}]))
        identity = next(iter(plain))
        self.assertEqual(list(explicit),[identity])
        self.assertEqual(visit_minutes(plain[identity],{}),45)
        self.assertEqual(visit_minutes(explicit[identity],{},scheduled_minutes=30),80)
        self.assertEqual(visit_minutes(plain[identity],{}),plain[identity]["duration_minutes"])

    def test_coffee_and_dessert_enter_spatial_required_visits(self):
        for name in ("湖畔咖啡","湖畔甜品"):
            with self.subTest(name=name):
                req = requirements(wish_places=[{"city":"杭州","name":name,"date":B}])
                stops = wish_stops(req)
                identity = next(iter(stops))
                stops[identity]["map_place"] = {**POI,"typecode":"050900"}
                plan = build_spatial_plan(state(req,stops),stops)
                self.assertIn(identity,plan["required_ids"])
                self.assertEqual(plan["fixed_dates"][identity],B)
                self.assertEqual(plan["visit_minutes"][identity],45)
                self.assertIn(identity,next(day["stop_ids"] for day in plan["days"] if day["date"] == B))
                self.assertEqual(plan["meal_wishes"],{})
                self.assertEqual(dining_wish_issues({"days":[]},stops,req),[])
                self.assertIn("必去景点尚未全部纳入路线",confirmation_blockers({"days":[]},{"required":[identity]},req,stops))

    def test_meal_wish_selects_nearby_visit_day_without_consuming_visit_time(self):
        for assigned in (None,A):
            with self.subTest(assigned=assigned):
                wish = {"city":"杭州","name":"指定餐厅"}
                if assigned:
                    wish["date"] = assigned
                req = requirements(wish_places=[wish])
                stops = wish_stops(req)
                identity = next(iter(stops))
                stops[identity]["map_place"] = {"id":"meal","location":"121.001,30","typecode":"050100"}
                visits = {key:{"id":key,"city":"杭州","name":key,"source_id":"guide","category":"sight","assigned_date":day,
                              "duration_minutes":90,"map_place":{"id":key,"location":f"{longitude},30"}}
                          for key,day,longitude in (("guide:a",A,120),("guide:b",B,121))}
                stops.update(visits)
                plan = build_spatial_plan(state(req,stops),visits)
                selected_date = assigned or B
                self.assertEqual(plan["meal_wishes"][identity]["date"],selected_date)
                self.assertNotIn(identity,plan["required_ids"])
                self.assertNotIn(identity,plan["visit_minutes"])
                self.assertEqual(sum(plan["visit_minutes"].values()),180)
                itinerary = {"days":[{**day,"activities":[{**visits[key],"kind":"visit","start":"11:00","end":"13:00"} for key in day["stop_ids"]]} for day in plan["days"]]}
                refresh_meals(itinerary,stops,requirements=req,meal_plan=plan["meal_wishes"])
                day,row = next((day,row) for day in itinerary["days"] for row in day["meal_recommendations"] if row.get("wish"))
                self.assertEqual(day["date"],selected_date)
                self.assertEqual("距当天活动较远" in row["note"],bool(assigned))


class WishRollback(unittest.TestCase):
    def test_rollback_preserves_wishes_and_synchronizes_constraints_at_every_early_step(self):
        for step in ("requirements","guides","interview"):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as folder:
                store = Store(Path(folder))
                req = requirements(wish_places=[{**WISH,"date":B}],required_stop_ids=["guide:park","product:old"],
                                   excluded_stop_ids=["guide:skip","product:skip"])
                trip = store.create_trip("回退",req)
                store.add_source(trip["id"],{"id":"guide","title":"攻略","extraction":{"stops":[{"id":"guide:park"},{"id":"guide:skip"}]}})
                doc = trip["document"]
                doc["selected_source_ids"] = ["guide"]
                store.update(trip["id"],doc)
                trip = store.trip(trip["id"])
                result = store.back_to_step(trip["id"],trip["version"],trip["updated_at"],step)["document"]
                expected = {"required":["guide:park",WISH_ID],"excluded":["guide:skip"]} if step == "interview" else {"required":[WISH_ID],"excluded":[]}
                self.assertEqual(result["stop_constraints"],expected)
                self.assertEqual(result["requirements"]["required_stop_ids"],expected["required"])
                self.assertEqual(result["requirements"]["excluded_stop_ids"],expected["excluded"])
                self.assertEqual(result["requirements"]["wish_places"],req["wish_places"])
                self.assertEqual(Requirements(**result["requirements"]).required_stop_ids,expected["required"])


if __name__ == "__main__":
    unittest.main()
