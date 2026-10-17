"""往返交通由用户自行安排时，只保留目的地之间的城际交通，不再生成或校验出发地往返班次。"""
import unittest

from app.models import Requirements
from app.nodes.resources import ResourceNodes
from app.resource_planning import resource_context_issues


def state(self_arranged, cities=("广州", "深圳")):
    req = Requirements(origin="北京", cities=list(cities), start_date="2026-12-19", end_date="2026-12-22",
                       people=2, rooms=1, budget=8000, self_arranged_round_trip=self_arranged).model_dump(mode="json")
    days = [{"date":"2026-12-19","city":cities[0]},{"date":"2026-12-20","city":cities[0]},
            {"date":"2026-12-21","city":cities[-1]},{"date":"2026-12-22","city":cities[-1]}]
    return {"requirements":req,"selected_route":{"days":days}}


class SelfArrangedRoundTrip(unittest.TestCase):
    def test_default_plans_outbound_inter_city_and_return(self):
        labels = [segment["label"] for segment in ResourceNodes.segments(None,state(False))]
        self.assertEqual(labels,["北京 → 广州","广州 → 深圳","深圳 → 北京"])

    def test_self_arranged_keeps_only_legs_between_destinations(self):
        labels = [segment["label"] for segment in ResourceNodes.segments(None,state(True))]
        self.assertEqual(labels,["广州 → 深圳"])

    def test_single_city_trip_has_no_transport_to_plan(self):
        self.assertEqual(ResourceNodes.segments(None,state(True,("广州",))),[])

    def test_inter_city_leg_still_matches_the_expected_route(self):
        req = state(True)["requirements"]
        itinerary = {"days":[{"date":"2026-12-19","city":"广州"},{"date":"2026-12-21","city":"深圳"}],"hotels":[],
                     "transport":[{"label":"广州 → 深圳","context":{"origin":"广州","destination":"深圳","date":"2026-12-21"},
                                   "selected":{"departure":"2026-12-21 09:00","arrival":"2026-12-21 09:40"}}]}
        self.assertEqual(resource_context_issues(itinerary,req),[])


if __name__ == "__main__":
    unittest.main()
