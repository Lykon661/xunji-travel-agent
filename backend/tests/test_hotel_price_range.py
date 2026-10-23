"""Hotel price filtering with mocked queries."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError
from fastapi import HTTPException

from app.adapters import ToolError
from app.main import app, search_products, select_product
from app.models import ProductSearchInput, ProductSelectionInput
from app.store import Conflict


def hotel(identity, price, basis="room_night", currency="CNY", reference_unit=None):
    return {"id":identity,"name":identity,"kind":"hotel","price":price,"price_basis":basis,
            "currency":currency,"reference_unit":reference_unit,"raw":{}}


class HotelPriceRange(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.options = [hotel("below",199),hotel("minimum",200),hotel("maximum",400),hotel("above",401),
                        hotel("reference",300,"unknown",reference_unit="room_night"),hotel("unpriced",None),
                        hotel("total",300,"segment_total",reference_unit="room_night"),hotel("unknown",300,"unknown"),
                        hotel("foreign",300,currency="USD")]
        self.doc = {"requirements":{"cities":["杭州"],"start_date":"2026-11-10","end_date":"2026-11-12"},
                    "products":[{"id":"previous","name":"此前收藏"}]}
        self.planner = SimpleNamespace(gate=Mock(return_value=asyncio.Lock()),
            active=Mock(return_value={"version":0,"document":self.doc}),
            flyai=SimpleNamespace(search=AsyncMock(return_value={"options":self.options,"status":"ok","queried_at":"now","message":""})))
        self.store = Mock()
        self.store.trip.return_value = {"updated_at":"updated"}
        self.enterContext(patch.object(app.state,"planner",self.planner,create=True))
        self.enterContext(patch.object(app.state,"store",self.store,create=True))

    async def query(self, **fields):
        return await search_products("trip",ProductSearchInput(mode="hotel",base_version=0,**fields))

    def batches(self, *options):
        self.planner.flyai.search.side_effect = [
            {"options":batch,"status":"ok" if batch else "empty","queried_at":"now","message":""} for batch in options]

    async def test_range_is_inclusive_and_requires_comparable_nightly_prices(self):
        result = await self.query(min_price=200,max_price=400)
        self.assertEqual([item["id"] for item in result["products"]],["minimum","maximum","reference"])
        self.assertEqual(result["status"],"ok")
        context, flags = self.planner.flyai.search.call_args.args[2:4]
        self.assertEqual(context["min_price"],"200")
        self.assertEqual(context["max_price"],"400")
        self.assertEqual(flags["max-price"],400)
        self.assertEqual(flags["sort"],"price_desc")
        self.assertNotIn("min-price",flags)
        self.assertEqual(self.doc["product_search"]["min_price"],200)
        self.assertEqual(self.doc["product_search"]["max_price"],400)
        self.assertIn("previous",{item["id"] for item in self.doc["products"]})

    async def test_no_bounds_preserves_unpriced_and_unknown_candidates(self):
        result = await self.query()
        self.planner.flyai.search.assert_awaited_once()
        self.assertEqual(len(result["products"]),len(self.options))
        self.assertIsNone(self.planner.flyai.search.call_args.args[3]["max-price"])
        self.assertEqual(self.planner.flyai.search.call_args.args[3]["sort"],"price_asc")

    async def test_one_sided_ranges_and_empty_filtered_results(self):
        lower = await self.query(min_price=400)
        self.assertEqual([item["id"] for item in lower["products"]],["maximum","above"])
        self.assertEqual(self.planner.flyai.search.call_args.args[3]["sort"],"price_desc")
        upper = await self.query(max_price=200)
        self.assertEqual([item["id"] for item in upper["products"]],["below","minimum"])
        self.assertEqual(self.planner.flyai.search.call_args.args[3]["sort"],"price_asc")
        empty = await self.query(min_price=500,max_price=600)
        self.assertEqual(empty["status"],"empty")
        self.assertEqual(empty["products"],[])
        self.assertEqual(self.doc["product_search"]["result_ids"],[])

    async def test_price_staircase_merges_three_batches_with_inclusive_bounds(self):
        first = [hotel(str(price),price) for price in (200,195,190,185,180,175,170,168,165)]
        second = [hotel(str(price),price) for price in (164,160,155,150,145,140,135,130)]
        third = [hotel(str(price),price) for price in (129,120,110,100,99,90)]
        self.batches(first,[{**hotel("duplicate-165",165),"supplier_id":"165"},*second],
                     [hotel("130",130),*third])
        result = await self.query(min_price=100,max_price=200)
        calls = self.planner.flyai.search.await_args_list
        self.assertEqual(len(calls),3)
        self.assertEqual([call.args[3]["max-price"] for call in calls],[200,165,130])
        self.assertTrue(all(call.args[3]["sort"] == "price_desc" for call in calls))
        self.assertEqual([item["id"] for item in result["products"]],
                         [item["id"] for item in first+second+third if 100 <= item["price"] <= 200])
        self.assertNotIn("已连续查询5次",result["message"])

    async def test_full_page_at_same_price_decreases_maximum(self):
        self.batches([hotel(str(index),150) for index in range(10)],[])
        result = await self.query(min_price=100,max_price=150)
        self.assertEqual([call.args[3]["max-price"] for call in self.planner.flyai.search.await_args_list],[150,149])
        self.assertEqual(len(result["products"]),10)

    async def test_staircase_stops_after_five_queries_and_appends_hint(self):
        self.batches(*[[hotel(str(price),price)] for price in (200,180,160,140,120)])
        result = await self.query(min_price=100,max_price=220)
        self.assertEqual(self.planner.flyai.search.await_count,5)
        self.assertEqual(len(result["products"]),5)
        self.assertTrue(result["message"].endswith("已连续查询5次，价格区间内可能还有更多酒店，可缩小区间后再查。"))
        self.assertEqual(self.doc["product_search"]["message"],result["message"])

    async def test_each_area_has_its_own_staircase_and_hint_is_added_once(self):
        areas = ["西湖","武林广场"]
        self.doc["itinerary"] = {"hotels":[{"city":"杭州","search_areas":areas,"context":{}}]}
        self.batches(*[[hotel(f"{area}-{price}",price)] for area in areas for price in (200,180,160,140,120)])
        result = await self.query(min_price=100,max_price=220)
        calls = self.planner.flyai.search.await_args_list
        self.assertEqual(len(calls),10)
        self.assertEqual([call.args[3]["max-price"] for call in calls],[220,200,180,160,140]*2)
        self.assertEqual([call.args[3]["poi-name"] for call in calls],[areas[0]]*5+[areas[1]]*5)
        self.assertEqual([item["search_area"] for item in result["products"]],[areas[0]]*5+[areas[1]]*5)
        self.assertEqual(result["message"].count("已连续查询5次"),1)

    async def test_staircase_stops_without_new_candidates_and_preserves_first(self):
        first = {**hotel("first",180),"supplier_id":"same-hotel"}
        duplicate = {**hotel("duplicate",150),"supplier_id":"same-hotel"}
        self.batches([first],[duplicate])
        result = await self.query(min_price=100,max_price=200)
        self.assertEqual(self.planner.flyai.search.await_count,2)
        self.assertEqual([(item["id"],item["price"]) for item in result["products"]],[("first",180)])

    async def test_minimum_only_starts_without_maximum(self):
        self.batches([hotel("high",180)],[hotel("low",90)])
        result = await self.query(min_price=100)
        calls = self.planner.flyai.search.await_args_list
        self.assertEqual([call.args[3]["max-price"] for call in calls],[None,180])
        self.assertTrue(all(call.args[3]["sort"] == "price_desc" for call in calls))
        self.assertEqual([item["id"] for item in result["products"]],["high"])

    async def test_maximum_only_keeps_single_ascending_query(self):
        await self.query(max_price=200)
        self.planner.flyai.search.assert_awaited_once()
        flags = self.planner.flyai.search.await_args.args[3]
        self.assertEqual(flags["max-price"],200)
        self.assertEqual(flags["sort"],"price_asc")

    async def test_empty_or_unpriced_batch_stops_staircase(self):
        for options in ([],[hotel("unpriced",None)]):
            with self.subTest(options=options):
                self.planner.flyai.search.reset_mock()
                self.batches(options)
                result = await self.query(min_price=100,max_price=200)
                self.planner.flyai.search.assert_awaited_once()
                self.assertEqual(result["products"],[])
                self.assertEqual(result["status"],"empty")

    async def test_next_maximum_below_minimum_stops_staircase(self):
        self.batches([hotel("free",0)])
        result = await self.query(min_price=0,max_price=0)
        self.planner.flyai.search.assert_awaited_once()
        self.assertEqual([item["id"] for item in result["products"]],["free"])

    async def test_fractional_low_price_is_floored(self):
        self.batches([hotel("fractional",150.75)],[])
        await self.query(min_price=100,max_price=200)
        self.assertEqual([call.args[3]["max-price"] for call in self.planner.flyai.search.await_args_list],[200,150])

    async def test_fifth_batch_below_minimum_does_not_append_limit_hint(self):
        self.batches(*[[hotel(str(price),price)] for price in (200,180,160,140,90)])
        result = await self.query(min_price=100,max_price=220)
        self.assertEqual(self.planner.flyai.search.await_count,5)
        self.assertNotIn("已连续查询5次",result["message"])

    async def test_tool_error_keeps_previous_batches_and_stops_other_areas(self):
        self.doc["itinerary"] = {"hotels":[{"city":"杭州","search_areas":["西湖","武林广场"],"context":{}}]}
        failure = ToolError("query_failed","模拟查询失败")
        self.planner.flyai.search.side_effect = [
            {"options":[hotel("saved",180)],"status":"ok","queried_at":"now","message":"首批结果"},failure]
        result = await self.query(min_price=100,max_price=200)
        self.assertEqual(self.planner.flyai.search.await_count,2)
        self.assertTrue(all(call.args[3]["poi-name"] == "西湖" for call in self.planner.flyai.search.await_args_list))
        self.assertEqual([item["id"] for item in result["products"]],["saved"])
        self.assertEqual(result["status"],"ok")
        self.assertIn(str(failure),result["message"])
        self.assertIn("saved",{item["id"] for item in self.doc["products"]})

    async def test_tool_error_without_any_candidates_is_raised_and_recorded(self):
        for empty_first in (False,True):
            with self.subTest(empty_first=empty_first):
                self.doc["itinerary"] = {"hotels":[{"city":"杭州","search_areas":["西湖","武林广场"],"context":{}}]}
                self.planner.flyai.search.reset_mock()
                failure = ToolError("query_failed","模拟查询失败")
                self.planner.flyai.search.side_effect = ([
                    {"options":[],"status":"empty","queried_at":"now","message":""}] if empty_first else [])+[failure]
                with self.assertRaises(ToolError) as raised:
                    await self.query(min_price=100,max_price=200)
                self.assertIs(raised.exception,failure)
                self.assertEqual(self.planner.flyai.search.await_count,2 if empty_first else 1)
                self.assertEqual(self.doc["product_search"]["code"],failure.code)
                self.assertEqual(self.doc["product_search"]["result_ids"],[])

    def test_queries_only_accept_hotels_and_tickets(self):
        self.assertEqual(ProductSearchInput(base_version=0).mode,"hotel")
        for mode in ("keyword","ai","marriott_package","marriott_hotel"):
            with self.subTest(mode=mode),self.assertRaises(ValidationError):
                ProductSearchInput(mode=mode,base_version=0)

    async def test_ticket_query_uses_poi_search_and_returns_references(self):
        self.planner.flyai.search.return_value = {"options":[{"id":"ticket","name":"博物馆","kind":"poi","raw":{}}],
            "status":"ok","queried_at":"now","message":""}
        result = await search_products("trip",ProductSearchInput(mode="poi",query="博物馆",visit_date="2026-11-10",base_version=0))
        _,kind,context,flags = self.planner.flyai.search.call_args.args
        self.assertEqual(kind,"poi")
        self.assertEqual(context["date"],"2026-11-10")
        self.assertEqual(flags,{"city-name":"杭州","keyword":"博物馆"})
        self.assertTrue(result["products"][0]["reference_only"])

    async def test_favorites_accept_query_references_but_not_legacy_generic_products(self):
        self.planner.active.return_value["updated_at"] = "updated"
        self.doc["products"] = [{"id":"hotel","search_mode":"hotel"},{"id":"ticket","search_mode":"poi"},
            {"id":"legacy","search_mode":"keyword"}]
        for identity in ("hotel","ticket"):
            await select_product("trip",ProductSelectionInput(product_id=identity,base_version=0,updated_at="updated"))
        self.assertEqual(self.doc["reference_product_ids"],["hotel","ticket"])
        with self.assertRaises(Conflict):
            await select_product("trip",ProductSelectionInput(product_id="legacy",base_version=0,updated_at="updated"))

    async def test_route_areas_are_sent_to_queries_and_candidates_are_deduplicated(self):
        self.doc["itinerary"] = {"hotels":[{"city":"杭州","search_areas":["西湖","武林广场"],
            "context":{"check_in":"2026-11-10","check_out":"2026-11-11"}}]}
        result = await self.query()
        self.assertEqual([call.args[3]["poi-name"] for call in self.planner.flyai.search.await_args_list],["西湖","武林广场"])
        self.assertEqual(self.planner.flyai.search.call_args.args[2]["check_out"],"2026-11-11")
        self.assertEqual(len(result["products"]),len(self.options))

    async def test_custom_area_overrides_route_recommendations(self):
        await self.query(hotel_location="custom",hotel_area="  湖滨路  ")
        self.assertEqual(self.planner.flyai.search.call_args.args[3]["poi-name"],"湖滨路")
        self.assertEqual(self.doc["product_search"]["hotel_area"],"湖滨路")

    async def test_unknown_recommended_area_does_not_query(self):
        with self.assertRaises(HTTPException):
            await self.query(hotel_location="route",hotel_area="过期区域")
        self.planner.flyai.search.assert_not_awaited()

    def test_invalid_ranges_are_rejected_before_queries(self):
        for fields in ({"min_price":-1},{"max_price":-1},{"min_price":400,"max_price":200},
                       {"min_price":1.5},{"max_price":10000001},{"mode":"poi","min_price":200}):
            with self.subTest(fields=fields),self.assertRaises(ValidationError):
                ProductSearchInput(base_version=0,**{"mode":"hotel",**fields})
        self.assertEqual(ProductSearchInput(mode="hotel",base_version=0,min_price=0,max_price=0).min_price,0)
