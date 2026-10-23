import copy
import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.adapters import Amap
from app.cache import AdapterCache
from app.hotel_planning import build_hotel_area_segments
from app.nodes.resources import ResourceNodes
from app.resource_planning import recommended_hotel_areas
from app.store import Store


def fixture(kilometers, city="杭州", start="2026-11-01", nights=None, business=True):
    first = date.fromisoformat(start)
    days,stops = [],{}
    for index,km in enumerate(kilometers):
        identity = f"{city}:{index}"
        place = {"name":f"景点{index}","location":f"{math.degrees(km*1000/6371000)},0"} if km is not None else {}
        if business:
            place["business_area"] = ["商圈甲","商圈乙"]
        stops[identity] = {"name":f"景点{index}","city":city,"map_place":place}
        days.append({"city":city,"date":(first+timedelta(days=index)).isoformat(),"stop_ids":[identity]})
    end = first+timedelta(days=len(days) if nights is None else nights)
    return {"trip_id":"synthetic","selected_route":{"days":days},"stops":stops,
            "requirements":{"cities":[city],"start_date":start,"end_date":end.isoformat(),
                            "budget":10000,"people":1,"rooms":1,"bed_type":""},
            "itinerary":{"days":[],"transport":[],"hotels":[]},"locks":[]}


class HotelAreaSegments(unittest.IsolatedAsyncioTestCase):
    async def test_three_plus_two_at_twelve_km(self):
        state = fixture([0,0,0,12,12])
        before = copy.deepcopy(state)
        segments = await build_hotel_area_segments(state)
        self.assertEqual([segment["nights"] for segment in segments],[3,2])
        self.assertEqual([segment["day_range"] for segment in segments],[{"start":1,"end":3},{"start":4,"end":5}])
        self.assertEqual(segments[0]["check_out"],segments[1]["check_in"])
        self.assertIn("12.0公里",segments[0]["reason"])
        self.assertEqual(state,before)

    async def test_three_plus_one_merges(self):
        segments = await build_hotel_area_segments(fixture([0,0,0,12]))
        self.assertEqual([segment["nights"] for segment in segments],[4])

    async def test_two_plus_two_at_six_km_merges(self):
        segments = await build_hotel_area_segments(fixture([0,0,6,6]))
        self.assertEqual([segment["nights"] for segment in segments],[4])

    async def test_exact_eight_km_merges(self):
        segments = await build_hotel_area_segments(fixture([0,0,8,8]))
        self.assertEqual(len(segments),1)

    async def test_end_date_truncates_the_last_day(self):
        segments = await build_hotel_area_segments(fixture([0,0,0,12,12],nights=4))
        self.assertEqual([segment["nights"] for segment in segments],[4])
        self.assertEqual(segments[0]["check_out"],"2026-11-05")
        self.assertEqual(segments[0]["day_range"]["end"],5)

    async def test_checkout_day_joins_the_last_overnight_group(self):
        segments = await build_hotel_area_segments(fixture([0,0,0,12,12,12],nights=5))
        self.assertEqual([segment["nights"] for segment in segments],[3,2])
        self.assertEqual(segments[-1]["dates"],['2026-11-04','2026-11-05','2026-11-06'])

    async def test_short_and_single_night_stays(self):
        for points,nights in (([0,12,12],2),([0,12],1)):
            with self.subTest(nights=nights):
                segments = await build_hotel_area_segments(fixture(points,nights=nights))
                self.assertEqual([segment["nights"] for segment in segments],[nights])

    async def test_cities_and_revisits_are_separate(self):
        state = fixture([0,0],city="杭州")
        second = fixture([12,12],city="苏州",start="2026-11-03")
        third = fixture([0,0],city="杭州",start="2026-11-05")
        third["stops"] = {"return:"+key:value for key,value in third["stops"].items()}
        for day in third["selected_route"]["days"]:
            day["stop_ids"] = ["return:"+identity for identity in day["stop_ids"]]
        for extra in (second,third):
            state["selected_route"]["days"].extend(extra["selected_route"]["days"])
            state["stops"].update(extra["stops"])
        state["requirements"].update(cities=["杭州","苏州"],end_date="2026-11-07")
        segments = await build_hotel_area_segments(state)
        self.assertEqual([segment["city"] for segment in segments],["杭州","苏州","杭州"])
        self.assertEqual([segment["nights"] for segment in segments],[2,2,2])
        self.assertEqual([segment["day_range"]["start"] for segment in segments],[1,3,5])

    async def test_dining_is_excluded_and_daily_center_is_medoid(self):
        state = fixture([0,0,0,12,12])
        day = state["selected_route"]["days"][0]
        for identity,km,category in (("near",1,"sight"),("middle",2,"sight"),("food",100,"dining"),("invalid",None,"sight")):
            state["stops"][identity] = {"name":identity,"category":category,"map_place":{
                "location":f"{math.degrees(km*1000/6371000)},0" if km is not None else "invalid",
                "business_area":"餐饮商圈" if category == "dining" else []}}
            day["stop_ids"].append(identity)
        segments = await build_hotel_area_segments(state)
        self.assertEqual([segment["nights"] for segment in segments],[3,2])
        self.assertNotIn("餐饮商圈",[area["name"] for segment in segments for area in segment["areas"]])
        self.assertEqual(segments[0]["center"],"0.0,0.0")
        daily = await build_hotel_area_segments({**state,"selected_route":{"days":[day]}})
        self.assertEqual(daily[0]["center"],state["stops"]["near"]["map_place"]["location"].replace(",0",",0.0"))

    async def test_no_coordinates_uses_poi_names_without_distances(self):
        amap = SimpleNamespace(business_areas=AsyncMock())
        segments = await build_hotel_area_segments(fixture([None,None],business=False),amap)
        self.assertIsNone(segments[0]["center"])
        self.assertTrue(segments[0]["areas"])
        for area in segments[0]["areas"]:
            self.assertEqual(area["source"],"poi_fallback")
            self.assertIsNone(area["avg_km"])
            self.assertIsNone(area["max_km"])
            self.assertEqual(area["reason"],"靠近本段景点，距离待确认")
        amap.business_areas.assert_not_awaited()

    async def test_majority_of_days_missing_coordinates_prevents_split(self):
        segments = await build_hotel_area_segments(fixture([0,None,None,None,12]))
        self.assertEqual(len(segments),1)

    async def test_existing_business_areas_skip_amap(self):
        state = fixture([0,0,0,12,12])
        state["stops"]["杭州:0"]["map_place"]["business_area"] = "商圈甲;商圈乙"
        amap = SimpleNamespace(business_areas=AsyncMock())
        segments = await build_hotel_area_segments(state,amap)
        self.assertTrue(all(len(segment["areas"]) == 2 for segment in segments))
        self.assertTrue(all("锚点估算" in area["reason"] for segment in segments for area in segment["areas"]))
        amap.business_areas.assert_not_awaited()

    async def test_amap_failure_uses_nearest_poi(self):
        amap = SimpleNamespace(business_areas=AsyncMock(side_effect=RuntimeError("mock failure")))
        segments = await build_hotel_area_segments(fixture([0,1],business=False),amap)
        self.assertEqual(amap.business_areas.await_count,2)
        self.assertEqual(segments[0]["areas"][0]["source"],"poi_fallback")
        self.assertIsNotNone(segments[0]["areas"][0]["avg_km"])

    async def test_regeocode_areas_are_sorted_and_capped(self):
        amap = SimpleNamespace(business_areas=AsyncMock(return_value=[
            {"name":"远","location":f"{math.degrees(3000/6371000)},0"},
            {"name":"近","location":"0,0"},{"name":"中","location":f"{math.degrees(1000/6371000)},0"},
            {"name":"更远","location":"1,0"}]))
        segments = await build_hotel_area_segments(fixture([0,0],business=False),amap)
        self.assertEqual([area["name"] for area in segments[0]["areas"]],["近","中","远"])
        amap.business_areas.assert_awaited_once()

    async def test_segment_and_trip_call_limits(self):
        state = fixture([0,1],business=False)
        for index in range(1,8):
            extra = fixture([0,1],city=f"城市{index}",start=(date(2026,11,1)+timedelta(days=2*index)).isoformat(),business=False)
            state["selected_route"]["days"].extend(extra["selected_route"]["days"])
            state["stops"].update(extra["stops"])
            state["requirements"]["cities"].extend(extra["requirements"]["cities"])
        state["requirements"]["end_date"] = "2026-11-17"
        amap = SimpleNamespace(business_areas=AsyncMock(return_value=[]))
        segments = await build_hotel_area_segments(state,amap)
        self.assertEqual(len(segments),8)
        self.assertEqual(amap.business_areas.await_count,12)

    async def test_existing_real_commute_can_keep_close_groups_apart(self):
        state = fixture([0,0,6,6])
        a,b = [state["stops"][f"杭州:{index}"]["map_place"] for index in (0,2)]
        state["itinerary"]["days"] = [{"date":"2026-11-01","city":"杭州","activities":[{
            "move_before":{"origin_place":a,"destination_place":b,"minutes":41}}]}]
        segments = await build_hotel_area_segments(state)
        self.assertEqual([segment["nights"] for segment in segments],[2,2])

    async def test_recommended_areas_include_segments_after_legacy_areas(self):
        state = fixture([0,0])
        state["itinerary"]["hotel_area_segments"] = await build_hotel_area_segments(state)
        state["itinerary"]["hotels"] = [{"city":"杭州","search_areas":["旧区域","商圈甲"]}]
        self.assertEqual(recommended_hotel_areas(state,"杭州"),["旧区域","商圈甲","商圈乙"])
        self.assertEqual(recommended_hotel_areas(state,"苏州"),[])
        self.assertEqual(recommended_hotel_areas({**state,"itinerary_outdated":True},"杭州"),[])


class BusinessAreaAdapter(unittest.IsolatedAsyncioTestCase):
    def adapter(self, payload):
        amap = Amap.__new__(Amap)
        amap.cache = AdapterCache()
        amap.call = AsyncMock(return_value=payload)
        return amap

    async def test_parses_only_business_fields_and_caches(self):
        amap = self.adapter({"regeocode":{"addressComponent":{"businessAreas":[{"name":"天河路","location":"113,23"}]},
                                          "pois":[{"name":"不应提取"}],"business_area":"体育中心"}})
        expected = [{"name":"天河路","location":"113.0,23.0"},{"name":"体育中心"}]
        actual = await amap.business_areas("trip","113,23")
        self.assertCountEqual(actual,expected)
        self.assertEqual(await amap.business_areas("trip","113,23"),actual)
        amap.call.assert_awaited_once_with("trip","maps_regeocode",{"location":"113,23","longitude":"113.0","latitude":"23.0"})

    async def test_failure_and_invalid_location_return_empty(self):
        amap = self.adapter({})
        amap.call.side_effect = RuntimeError("mock failure")
        self.assertEqual(await amap.business_areas("trip","invalid"),[])
        amap.call.assert_not_awaited()
        self.assertEqual(await amap.business_areas("trip","113,23"),[])


class HotelNodeIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.node = ResourceNodes()
        self.node.emit = Mock()
        self.node.affected = Mock(return_value=True)
        self.node.hotel_areas = Mock(return_value=["旧区域"])
        self.node.resource_agent = SimpleNamespace(search=AsyncMock(return_value={"options":[],"status":"empty"}),
                                                   choose=AsyncMock(return_value=(None,"住宿待查询")))
        self.node.amap = SimpleNamespace(business_areas=AsyncMock(return_value=[]))
        self.state = fixture([0,0,0,12,12])

    async def test_writes_segments_without_changing_hotel_entries(self):
        with patch("app.nodes.resources.build_hotel_area_segments",AsyncMock(return_value=[])):
            baseline = await self.node.hotels(self.state)
        result = await self.node.hotels(self.state)
        self.assertEqual(result["itinerary"]["hotels"],baseline["itinerary"]["hotels"])
        self.assertEqual([entry["id"] for entry in result["itinerary"]["hotels"]],["hotel:杭州"])
        self.assertEqual([segment["nights"] for segment in result["itinerary"]["hotel_area_segments"]],[3,2])
        self.assertEqual(result["itinerary"]["hotels"][0]["context"],{"city":"杭州","check_in":"2026-11-01","check_out":"2026-11-06"})

    async def test_builder_exception_only_adds_warning(self):
        with patch("app.nodes.resources.build_hotel_area_segments",AsyncMock(side_effect=RuntimeError("mock broken"))):
            result = await self.node.hotels(self.state)
        self.assertEqual(result["itinerary"]["hotels"][0]["id"],"hotel:杭州")
        self.assertTrue(any("mock broken" in warning for warning in result["warnings"]))

    async def test_resource_choice_preserves_existing_segments(self):
        self.state["itinerary"]["hotel_area_segments"] = [{"city":"杭州","marker":"old"}]
        self.state["change"] = {"resource_choice":True}
        with patch("app.nodes.resources.build_hotel_area_segments",AsyncMock()) as build:
            result = await self.node.hotels(self.state)
        self.assertEqual(result["itinerary"],self.state["itinerary"])
        self.assertIsNot(result["itinerary"],self.state["itinerary"])
        build.assert_not_awaited()

    async def test_document_round_trip_keeps_segments(self):
        itinerary = (await self.node.hotels(self.state))["itinerary"]
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            trip = store.create_trip("synthetic",self.state["requirements"])
            document = trip["document"]
            document["itinerary"] = itinerary
            store.update(trip["id"],document)
            self.assertEqual(store.trip(trip["id"])["document"]["itinerary"],itinerary)
