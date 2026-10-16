"""User-run cases with mocked queries; no CLI, network, or real trip data."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError
from fastapi import HTTPException

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

    async def test_range_is_inclusive_and_requires_comparable_nightly_prices(self):
        result = await self.query(min_price=200,max_price=400)
        self.assertEqual([item["id"] for item in result["products"]],["minimum","maximum","reference"])
        self.assertEqual(result["status"],"ok")
        context, flags = self.planner.flyai.search.call_args.args[2:4]
        self.assertEqual(context["min_price"],"200")
        self.assertEqual(context["max_price"],"400")
        self.assertEqual(flags["max-price"],400)
        self.assertNotIn("min-price",flags)
        self.assertEqual(self.doc["product_search"]["min_price"],200)
        self.assertEqual(self.doc["product_search"]["max_price"],400)
        self.assertIn("previous",{item["id"] for item in self.doc["products"]})

    async def test_no_bounds_preserves_unpriced_and_unknown_candidates(self):
        result = await self.query()
        self.assertEqual(len(result["products"]),len(self.options))
        self.assertIsNone(self.planner.flyai.search.call_args.args[3]["max-price"])

    async def test_one_sided_ranges_and_empty_filtered_results(self):
        lower = await self.query(min_price=400)
        self.assertEqual([item["id"] for item in lower["products"]],["maximum","above"])
        upper = await self.query(max_price=200)
        self.assertEqual([item["id"] for item in upper["products"]],["below","minimum"])
        empty = await self.query(min_price=500,max_price=600)
        self.assertEqual(empty["status"],"empty")
        self.assertEqual(empty["products"],[])
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
