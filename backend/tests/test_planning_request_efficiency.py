"""Synthetic regressions for the user to run; no provider or real model calls."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.adapters import Amap, ToolError
from app.models import PlaceChoices, ResourceChoice
from app.planner import Planner
from app.reviewing import compact_review_payload, review_payload, review_repair_dates


class PlaceSelectionReuse(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.models = SimpleNamespace(ask=AsyncMock(return_value=ResourceChoice(selected_id=None,reason="同名，无法确定")))
        self.planner.amap = Amap.__new__(Amap)
        self.planner.amap.cache = {}
        self.candidates = [{"id":"poi-a","name":"公园一店","address":"东路","location":"120,30"},
                           {"id":"poi-b","name":"公园二店","address":"西路","location":"121,31"}]
        self.planner.amap.call = AsyncMock(side_effect=lambda *args: {"pois":copy.deepcopy(self.candidates)})
        self.state = {"trip_id":"trip","run_id":"run-a"}
        self.stops = {"a":{"id":"a","city":"杭州","name":"公园","evidence":{"quote":"公园"}}}

    async def test_unresolved_selection_is_reused_in_one_run_and_stays_unresolved(self):
        first = await self.planner.locate_stops(self.state,self.stops)
        second = await self.planner.locate_stops(self.state,self.stops)
        self.planner.models.ask.assert_awaited_once()
        self.assertIsNone(first["a"]["map_place"])
        self.assertIsNone(second["a"]["map_place"])

    async def test_new_evidence_candidates_and_runs_allow_a_new_selection(self):
        await self.planner.locate_stops(self.state,self.stops)
        changed = copy.deepcopy(self.stops)
        changed["a"]["evidence"]["quote"] = "西路的公园"
        await self.planner.locate_stops(self.state,changed)
        self.planner.amap.cache[("place_candidates","trip","杭州","公园")]["pois"][0]["address"] = "新地址"
        await self.planner.locate_stops(self.state,changed)
        await self.planner.locate_stops({**self.state,"run_id":"run-b"},changed)
        self.assertEqual(self.planner.models.ask.await_count,4)

    async def test_network_failures_are_not_cached_as_selection_decisions(self):
        self.planner.models.ask.side_effect = [ToolError("model_network","合成超时"),ResourceChoice(selected_id=None,reason="仍有歧义")]
        await self.planner.locate_stops(self.state,self.stops)
        await self.planner.locate_stops(self.state,self.stops)
        self.assertEqual(self.planner.models.ask.await_count,2)

    async def test_model_resolves_candidates_using_confirmed_preferences_and_full_context(self):
        self.stops["a"].update(city="广州",name="南越王博物馆")
        candidates = [{"id":"museum","name":"南越王博物院(王墓展区)","address":"解放北路867号"},
                      {"id":"palace","name":"南越王博物院(王宫展区)","address":"中山四路316号"},
                      {"id":"ticket-desk","name":"南越王博物院(王墓展区)售票处","address":"解放北路867号"}]
        async def amap_call(trip_id,tool,args):
            if tool == "maps_text_search":
                return {"pois":copy.deepcopy(candidates)}
            self.assertEqual(tool,"maps_search_detail")
            self.assertEqual(args["id"],"museum")
            return {"id":"museum","name":candidates[0]["name"],"location":"113.261015,23.137823"}
        self.planner.amap.call.side_effect = amap_call
        self.planner.models.ask.return_value = ResourceChoice(selected_id="museum",reason="用户明确要求王墓展区，对应高德主场馆，地址解放北路867号")
        state = {**self.state,"requirements":{"preferences":"保留南越王博物馆王墓展区于2026-10-15行程，接受票务资源缺口"}}
        result = await self.planner.locate_stops(state,self.stops)
        self.assertEqual(result["a"]["map_place"]["id"],"museum")
        self.assertEqual(result["a"]["map_place"]["location"],"113.261015,23.137823")
        self.planner.models.ask.assert_awaited_once()
        payload = self.planner.models.ask.call_args.args[2]
        self.assertEqual(payload["preferences"],state["requirements"]["preferences"])
        self.assertEqual([item["id"] for item in payload["pois"]],["museum","palace","ticket-desk"])
        self.assertIn("用户明确要求",result["a"]["map_place"]["match_basis"])
        self.assertNotIn("map_issue",result["a"])

    async def test_preference_changes_reach_the_selector_and_invalidate_an_unresolved_selection(self):
        await self.planner.locate_stops(self.state,self.stops)
        state = {**self.state,"requirements":{"preferences":"优先选西路的公园二店"}}
        await self.planner.locate_stops(state,self.stops)
        self.assertEqual(self.planner.models.ask.await_count,2)
        self.assertEqual(self.planner.models.ask.call_args.args[2]["preferences"],"优先选西路的公园二店")

    async def test_multiple_exhibition_areas_and_a_negative_preference_still_need_matching(self):
        self.stops["a"].update(city="广州",name="南越王博物馆")
        self.candidates = [{"id":"museum","name":"南越王博物院(王墓展区)","location":"113.26,23.13"},
                           {"id":"palace","name":"南越王博物院(王宫展区)","location":"113.27,23.12"}]
        for preferences in ("南越王博物院(王墓展区)与南越王博物院(王宫展区)待选", "不去南越王博物院(王墓展区)"):
            with self.subTest(preferences=preferences):
                result = await self.planner.locate_stops({**self.state,"requirements":{"preferences":preferences}},self.stops)
                self.assertIsNone(result["a"]["map_place"])
                self.assertEqual(result["a"]["map_issue"],"同名，无法确定")
        self.assertEqual(self.planner.models.ask.await_count,2)

    async def test_verified_amap_poi_is_kept_without_rematching(self):
        place = {"id":"poi-a","name":"公园一店","location":"120,30","evidence_id":"query:map"}
        self.stops["a"]["map_place"] = copy.deepcopy(place)
        result = await self.planner.locate_stops(self.state,self.stops)
        self.assertEqual(result["a"]["map_place"],place)
        self.planner.amap.call.assert_not_awaited()
        self.planner.models.ask.assert_not_awaited()
        result["a"]["map_place"]["location"] = "121,31"
        self.assertEqual(self.stops["a"]["map_place"],place)

    async def test_reassessment_recovers_the_same_stop_amap_poi_from_the_draft(self):
        place = {"id":"museum","name":"南越王博物院(王墓展区)","location":"113.261015,23.137823",
                 "address":"解放北路867号","evidence_id":"query:map"}
        self.stops["a"].update(city="广州",name="南越王博物馆")
        draft = {"days":[{"city":"广州","activities":[{
            **self.stops["a"],"map_place":place,"ticket_quote":{"candidates":[{
                "name":"西汉南越王博物馆","location":"113.267474,23.144033"}]}}]}]}
        for field in ("old_itinerary","selected_route","itinerary"):
            with self.subTest(field=field):
                state = {**self.state,field:{"draft":draft} if field == "selected_route" else draft}
                result = await self.planner.locate_stops(state,self.stops)
                self.assertEqual(result["a"]["map_place"],place)
        self.planner.amap.call.assert_not_awaited()
        self.planner.models.ask.assert_not_awaited()

    async def test_a_different_stop_id_does_not_inherit_an_old_poi_with_the_same_name(self):
        state = {**self.state,"old_itinerary":{"days":[{"city":"杭州","activities":[{
            "id":"other","name":"公园","map_place":{"id":"poi-a","location":"120,30","evidence_id":"query:map"}}]}]}}
        result = await self.planner.locate_stops(state,self.stops)
        self.assertIsNone(result["a"]["map_place"])
        self.planner.models.ask.assert_awaited_once()


    async def test_one_exact_name_sight_is_adopted_without_a_model_call(self):
        self.candidates = [{"id":"lake","name":"公园","address":"湖滨路","location":"120,30"},
                           {"id":"gate","name":"公园-南门","address":"湖滨路1号","location":"120.01,30"}]
        result = await self.planner.locate_stops(self.state,self.stops)
        self.assertEqual(result["a"]["map_place"]["id"],"lake")
        self.assertIn("名称完全一致",result["a"]["map_place"]["match_basis"])
        self.planner.models.ask.assert_not_awaited()

    async def test_an_exact_name_restaurant_still_needs_branch_evidence(self):
        self.stops["a"].update(name="合成面馆",category="dining")
        self.candidates = [{"id":"main","name":"合成面馆","location":"120,30"},
                           {"id":"branch","name":"合成面馆(西湖店)","location":"121,31"}]
        await self.planner.locate_stops(self.state,self.stops)
        self.planner.models.ask.assert_awaited_once()

    async def test_a_confirmed_match_is_reused_by_later_runs_but_an_unresolved_one_is_not(self):
        self.planner.models.ask.return_value = ResourceChoice(selected_id="poi-a",reason="攻略写明东路")
        await self.planner.locate_stops(self.state,self.stops)
        result = await self.planner.locate_stops({**self.state,"run_id":"run-b"},self.stops)
        self.assertEqual(result["a"]["map_place"]["id"],"poi-a")
        self.planner.models.ask.assert_awaited_once()

    def ambiguous_stops(self, count):
        return {f"s{index}":{"id":f"s{index}","city":"杭州","name":f"公园{index}","evidence":{"quote":f"公园{index}"}}
                for index in range(count)}

    async def test_batched_answers_are_checked_one_place_at_a_time(self):
        stops = self.ambiguous_stops(3)
        self.planner.models.ask.return_value = PlaceChoices(choices=[
            {"key":"p1","selected_id":"poi-a","reason":"东路"},{"key":"p2","selected_id":"invented","reason":"编造"}])
        result = await self.planner.locate_stops(self.state,stops)
        self.planner.models.ask.assert_awaited_once()
        self.assertEqual(result["s0"]["map_place"]["id"],"poi-a")
        self.assertIsNone(result["s1"]["map_place"])
        self.assertIn("未知POI",result["s1"]["map_issue"])
        self.assertIsNone(result["s2"]["map_place"])
        self.assertIn("未返回该地点",result["s2"]["map_issue"])

    async def test_a_failed_batch_is_not_cached_and_large_cities_are_split(self):
        stops = self.ambiguous_stops(9)
        self.planner.models.ask.side_effect = ToolError("model_network","合成超时")
        result = await self.planner.locate_stops(self.state,stops)
        self.assertEqual(self.planner.models.ask.await_count,2)
        self.assertTrue(all("合成超时" in stop["map_issue"] for stop in result.values()))
        self.planner.models.ask.side_effect = None
        self.planner.models.ask.return_value = ResourceChoice(selected_id=None,reason="仍有歧义")
        await self.planner.locate_stops(self.state,{"s8":stops["s8"]})
        self.assertEqual(self.planner.models.ask.await_count,3)

class ReviewMaterialReuse(unittest.TestCase):
    def test_references_preserve_every_fact_and_leave_plan_paths_and_inputs_intact(self):
        evidence = {"quote":"原始攻略与票价依据"*80,"price":120,"queried_at":"2026-10-15T08:00:00+00:00"}
        plan = {"days":[{"date":"2026-11-10","activities":[{"id":"a","evidence":evidence}]}]}
        payload = {"plan":plan,"previous_plan":copy.deepcopy(plan),
                   "evidence_index":{"query:1":{"result":copy.deepcopy(evidence)},"a/b~c":copy.deepcopy(evidence)},
                   "decisions":[copy.deepcopy(evidence)],"evidence_catalog":[{"evidence_id":"query:1"}]}
        original = copy.deepcopy(payload)
        compact = compact_review_payload(payload)

        def expand(value):
            if isinstance(value,dict) and set(value) == {"$ref"}:
                target = compact
                for part in value["$ref"][2:].split("/"):
                    part = part.replace("~1","/").replace("~0","~")
                    target = target[int(part)] if isinstance(target,list) else target[part]
                return expand(target)
            if isinstance(value,dict):
                return {key:expand(item) for key,item in value.items()}
            if isinstance(value,list):
                return [expand(item) for item in value]
            return value

        self.assertEqual(expand(compact),original)
        self.assertEqual(compact["plan"],original["plan"])
        self.assertEqual(payload,original)
        self.assertEqual(compact["previous_plan"],{"$ref":"#/plan"})
        self.assertLess(len(json.dumps(compact)),len(json.dumps(original))//2)

    def test_catalog_keeps_real_query_request_status_and_date_for_patch_selection(self):
        state = {"requirements":{},"selected_source_ids":[]}
        plan = {"days":[],"transport":[],"hotels":[]}
        row = {"evidence_id":"query:1","provider":"amap","request":{"tool":"maps_direction_transit_integrated"},
               "result":{"status":"error","code":"amap_limit"},"created_at":"2026-10-15"}
        payload = review_payload(state,plan,[],[row])
        entry = next(item for item in payload["evidence_catalog"] if item["evidence_id"] == "query:1")
        self.assertEqual(entry["request"],row["request"])
        self.assertEqual(entry["status"],"error")
        self.assertEqual(entry["created_at"],row["created_at"])

    def test_evidence_index_ids_stay_visible_even_when_the_whole_index_is_repeated(self):
        index = {"query:1":{"raw":"原始依据"*150}}
        payload = {"plan":{"same_index":copy.deepcopy(index)},"evidence_index":index}
        compact = compact_review_payload(payload)
        self.assertEqual(set(compact["evidence_index"]),{"query:1"})
        self.assertEqual(compact["evidence_index"]["query:1"],{"$ref":"#/plan/same_index/query:1"})

    def test_nested_supporting_references_resolve_through_original_escaped_paths(self):
        material = {"quote":"合成原文"*150,"amount":120}
        payload = {"plan":{},"requirements":{},"decisions":[{"a/b~c":material}],
                   "previous_plan":{"different":copy.deepcopy(material)},"evidence_index":{"query:1":copy.deepcopy(material)}}
        compact = compact_review_payload(payload)
        self.assertEqual(compact["previous_plan"]["different"],{"$ref":"#/decisions/0/a~1b~0c"})
        self.assertEqual(compact["evidence_index"]["query:1"],{"$ref":"#/decisions/0/a~1b~0c"})
        self.assertEqual(compact["decisions"][0]["a/b~c"],material)

    def test_sensitive_keys_are_removed_from_all_materials_before_deduplication(self):
        state = {"requirements":{"preferences":"保留事实","Headers":{"Authorization":"secret"}},
                 "selected_source_ids":["guide"],"change":{"diagnostic":"secret"}}
        source = {"id":"guide","title":"攻略","extraction":{"stops":[],"Authorization":"secret","cautions":["需预约"]}}
        row = {"evidence_id":"query:1","request":{"Token":"secret","date":"2026-11-10"},
               "result":{"raw":{"Access_Token":"secret","duration":30}}}
        payload = review_payload(state,{"days":[]},[source],[row])
        self.assertNotIn("secret",json.dumps(payload))
        self.assertEqual(payload["requirements"],{"preferences":"保留事实"})
        self.assertEqual(payload["evidence_index"]["source:guide"]["extraction"]["cautions"],["需预约"])
        self.assertEqual(payload["evidence_index"]["query:1"]["result"]["raw"]["duration"],30)


class LocalRepairScope(unittest.TestCase):
    def setUp(self):
        self.plan = {"days":[{"date":"2026-11-10","activities":[{"id":"a","name":"公园"}]},
                             {"date":"2026-11-11","activities":[{"id":"b","name":"博物馆"}]}]}

    def test_missing_dates_are_inferred_from_path_or_target(self):
        for finding in ({"field_path":"/days/1/activities/0"},{"target":"b"},
                        {"target":"route","message":"2026-11-11 午餐缺失"}):
            with self.subTest(finding=finding):
                self.assertEqual(review_repair_dates([finding],self.plan),["2026-11-11"])

    def test_global_findings_keep_full_scope_and_cross_day_findings_keep_both_dates(self):
        self.assertEqual(review_repair_dates([{"field_path":"/budget","target":"route"}],self.plan),
                         ["2026-11-10","2026-11-11"])
        self.assertEqual(review_repair_dates([{"dates":["2026-11-10"],"field_path":"/days/1"}],self.plan),
                         ["2026-11-10","2026-11-11"])
        self.assertEqual(review_repair_dates([{"dates":["2026-12-01"],"field_path":"/days/1"}],self.plan),["2026-11-11"])
