"""User-run cases; synthetic coordinates only, no real model or map calls."""
import copy
import unittest
from unittest.mock import patch

from app.dining import is_dining, meal_recommendations
from app.models import Candidate, Requirements
from app.planner import Planner
from app.rules import validate_candidate


class DiningRecommendations(unittest.TestCase):
    def setUp(self):
        self.day = {"date":"2026-11-10","city":"广州","activities":[
            {"id":"sight","name":"合成公园","kind":"visit","start":"11:00","end":"13:00",
             "map_place":{"id":"park","location":"113.3,23.1"}}]}
        self.stops = {
            "sight":{**self.day["activities"][0],"city":"广州"},
            "food": {"id":"food","name":"推荐餐厅","city":"广州","category":"dining",
                     "source_id":"guide","evidence":{"quote":"推荐餐厅"},
                     "map_place":{"id":"restaurant","location":"113.301,23.1"}},
            "unknown":{"id":"unknown","name":"咖啡店","city":"广州","category":"dining"},
            "far":{"id":"far","name":"远处餐厅","city":"广州","category":"dining",
                   "map_place":{"id":"far","location":"114.3,23.1"}},
            "other_city":{"id":"other_city","name":"外市餐厅","city":"深圳","category":"dining",
                          "map_place":{"id":"other","location":"113.301,23.1"}}}

    def test_only_verified_nearby_dining_is_recommended_for_the_meal_window(self):
        result = meal_recommendations(self.day,self.stops)
        self.assertEqual([(item["id"],item["meal"]) for item in result],[("food","lunch")])
        self.assertEqual(result[0]["nearby_activity"],"合成公园")
        self.assertEqual(result[0]["evidence"],self.stops["food"]["evidence"])
        self.assertEqual(meal_recommendations(self.day,self.stops,excluded=["food"]),[])

    def test_same_poi_from_multiple_guides_is_one_option(self):
        self.stops["duplicate"] = {**self.stops["food"],"id":"duplicate"}
        self.assertEqual(len(meal_recommendations(self.day,self.stops)),1)

    def test_restaurant_classification_does_not_remove_food_museums_or_streets(self):
        self.assertTrue(is_dining({"name":"JPG coffee"}))
        self.assertTrue(is_dining({"name":"某品牌","map_place":{"type":"餐饮服务;中餐厅"}}))
        self.assertFalse(is_dining({"name":"咖啡博物馆","category":"sight"}))
        self.assertFalse(is_dining({"name":"传统小吃街","category":"sight"}))

    def test_restaurants_cannot_be_validated_as_sightseeing(self):
        req = Requirements(origin="广州",cities=["广州"],start_date="2026-11-10",end_date="2026-11-10")
        candidate = Candidate(title="一天",reason="合成路线",days=[{"date":"2026-11-10","city":"广州","stop_ids":["food"]}])
        with self.assertRaisesRegex(ValueError,"餐饮推荐"):
            validate_candidate(candidate,req,self.stops)

    def test_new_coordinates_are_copied_into_a_preserved_activity(self):
        planner = Planner.__new__(Planner)
        day = copy.deepcopy(self.day)
        day["activities"][0]["map_place"] = None
        planner.update_day_places({"stops":self.stops},day)
        self.assertEqual(day["activities"][0]["map_place"],self.stops["sight"]["map_place"])
        self.assertEqual(day["activities"][0]["start"],"11:00")
        self.assertEqual(day["meal_recommendations"][0]["id"],"food")


class QuestionPlacement(unittest.IsolatedAsyncioTestCase):
    async def test_route_conflict_interrupt_belongs_to_interview(self):
        planner = Planner.__new__(Planner)
        state = {"decision":{"id":"choice","question":"保留哪个景点？","why_needed":"当天时间不足"},
                 "generation":"synthetic","decision_resume":"assess","draft_revision":2}
        with patch("app.planner.interrupt",side_effect=RuntimeError("interrupt")) as gate:
            with self.assertRaisesRegex(RuntimeError,"interrupt"):
                await planner.decision_gate(state)
        self.assertEqual(gate.call_args.args[0]["origin_step"],"interview")
        self.assertEqual(gate.call_args.args[0]["draft_revision"],2)
