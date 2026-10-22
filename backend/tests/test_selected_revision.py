"""Selected revision scope with synthetic data."""
import unittest

from app.models import CandidateRevision
from app.planner import Planner


class SelectedRevision(unittest.TestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.original = {"id":"candidate","title":"一天","reason":"合成路线","days":[
            {"date":"2026-11-10","city":"广州","stop_ids":["a","b","c"],"adjustment":""}]}
        self.requirements = {"origin":"广州","cities":["广州"],"start_date":"2026-11-10","end_date":"2026-11-10"}
        self.stops = {identity:{"id":identity,"city":"广州","name":"合成景点"+identity,"source_id":"guide"}
                      for identity in ("a","b","c","d")}

    def revise(self, ids):
        patch = CandidateRevision(days=[{"date":"2026-11-10","city":"广州","stop_ids":ids,
                                          "adjustment":"只调整选中的景点"}],explanation="修改选中项")
        return self.planner.apply_candidate_revision(self.original,patch,["2026-11-10"],self.requirements,self.stops,["b"])

    def test_selected_item_can_be_replaced_without_changing_other_items(self):
        self.assertEqual(self.revise(["a","d","c"])["days"][0]["stop_ids"],["a","d","c"])

    def test_unselected_item_cannot_be_deleted(self):
        with self.assertRaisesRegex(ValueError,"未选中"):
            self.revise(["b","c"])

    def test_unselected_items_cannot_be_reordered(self):
        with self.assertRaisesRegex(ValueError,"相对顺序"):
            self.revise(["c","b","a"])
