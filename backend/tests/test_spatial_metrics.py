import copy
import math
import unittest
from unittest.mock import patch

from app import ablation
from eval.metrics import VIOLATIONS, evaluate, spatial_metrics


def visit(location=None, **fields):
    return {"id": "a", "kind": "visit", "name": "合成景点", "map_place": {"location": location},
            "evidence": {"quote": "合成景点"}, "move_before": {"minutes": 0}, **fields}


def day(*activities, city="广州", date="2026-12-12"):
    return {"city": city, "date": date, "activities": list(activities), "gaps": []}


class SpatialMetrics(unittest.TestCase):
    def test_known_spherical_distance_follows_visit_order_only(self):
        activities = [visit("0,0"), {"kind": "hotel", "map_place": {"location": "90,0"}},
                      visit("2,0"), {"kind": "meal", "duration_minutes": 60}, visit("1,0"),
                      {"kind": "transport", "map_place": {"location": "180,0"}}]
        result = spatial_metrics([day(*activities), day(visit("90,0"), date="2026-12-13")])
        self.assertAlmostEqual(result["visit_path_geo_m"], 333584.77993367624)
        self.assertEqual(result["spatial_complete_days"], 2)
        self.assertEqual(result["spatial_located_visits"], 4)

    def test_dispersion_uses_daily_medoid_and_all_located_visits(self):
        result = spatial_metrics([day(visit("0,0"), visit("0.01,0"), visit("0.1,0"), visit()),
                                  day(visit("1,0"), date="2026-12-13")])
        self.assertAlmostEqual(result["within_day_dispersion_m"], 11119.492664455875 / 4)
        self.assertEqual(result["spatial_coordinate_coverage"], 4 / 5)
        self.assertEqual(result["spatial_complete_days"], 1)
        self.assertEqual(result["visit_path_geo_m"], 0)

    def test_medoid_tie_uses_original_activity_position(self):
        first, second = visit("0,0", id="z"), visit("0.03,0", id="a")
        other = day(visit("0.03,0"), date="2026-12-13")
        self.assertEqual(spatial_metrics([day(first, second), other])["cross_day_outlier_count"], 1)
        self.assertEqual(spatial_metrics([day(second, first), other])["cross_day_outlier_count"], 0)

    def test_outlier_threshold_is_strictly_more_than_1000_meters(self):
        for separation, expected in [(999, 0), (1000, 0), (1001, 1)]:
            with self.subTest(separation=separation):
                point = f"{separation / 100000},0"
                days = [day(visit("0,0"), visit("0,0"), visit(point)),
                        day(visit(point), date="2026-12-13")]
                with patch("eval.metrics._spatial_distance", side_effect=lambda a, b: abs(a[0] - b[0]) * 100000):
                    self.assertEqual(spatial_metrics(days)["cross_day_outlier_count"], expected)

    def test_outlier_counts_each_point_once_with_multiple_other_days(self):
        days = [day(visit("0,0"), visit("0,0"), visit("0.03,0")),
                day(visit("0.03,0"), date="2026-12-13"),
                day(visit("0.031,0"), date="2026-12-14")]
        self.assertEqual(spatial_metrics(days)["cross_day_outlier_count"], 1)

    def test_outliers_require_same_city_complete_nonempty_different_days(self):
        base = day(visit("0,0"), visit("0,0"), visit("0.03,0"))
        others = [day(visit("0.03,0"), city="深圳", date="2026-12-13"),
                  day(visit("0.03,0"), visit(), date="2026-12-13"),
                  day(date="2026-12-13"), day(visit("0.03,0"))]
        for other in others:
            with self.subTest(other=other):
                self.assertIsNone(spatial_metrics([base, other])["cross_day_outlier_count"])

    def test_incomplete_day_is_not_used_as_outlier_source(self):
        days = [day(visit("0,0"), visit("0,0"), visit("0.03,0"), visit()),
                day(visit("0.03,0"), date="2026-12-13"),
                day(visit("0.03,0"), date="2026-12-14")]
        self.assertEqual(spatial_metrics(days)["cross_day_outlier_count"], 0)

    def test_missing_coordinates_never_bridge_or_partially_count_a_day(self):
        result = spatial_metrics([day(visit("0,0"), visit(), visit("1,0"))])
        self.assertIsNone(result["visit_path_geo_m"])
        self.assertEqual(result["spatial_complete_days"], 0)
        self.assertEqual(result["spatial_coordinate_coverage"], 2 / 3)
        self.assertAlmostEqual(result["within_day_dispersion_m"], 111194.92664455874 / 2)

    def test_empty_days_have_null_metrics_and_zero_auxiliary_counts(self):
        for days in [[], [day()], [day({"kind": "meal", "duration_minutes": 60})]]:
            with self.subTest(days=days):
                result = spatial_metrics(days)
                for key in ("visit_path_geo_m", "within_day_dispersion_m", "cross_day_outlier_count",
                            "spatial_coordinate_coverage", "visit_minutes"):
                    self.assertIsNone(result[key])
                for key in ("spatial_located_visits", "spatial_complete_days", "visit_minutes_known_count"):
                    self.assertEqual(result[key], 0)

    def test_single_located_visit_has_valid_zero_distances(self):
        result = spatial_metrics([day(visit("0,0"))])
        self.assertEqual(result["visit_path_geo_m"], 0)
        self.assertEqual(result["within_day_dispersion_m"], 0)
        self.assertEqual(result["spatial_coordinate_coverage"], 1)
        self.assertEqual(result["spatial_complete_days"], 1)
        self.assertIsNone(result["cross_day_outlier_count"])

    def test_invalid_and_nonfinite_coordinates_are_unlocated(self):
        invalid = [None, "", "unknown", "0", "0,0,0", "181,0", "-181,0", "0,91", "0,-91",
                   "nan,0", "0,NaN", "inf,0", "0,-inf", "1e999,0", [0, 0], {}, 0]
        places = [{"location": value} for value in invalid] + [None, [], "0,0", {}]
        for place in places:
            with self.subTest(place=place):
                result = spatial_metrics([day(visit(map_place=place))])
                self.assertEqual(result["spatial_coordinate_coverage"], 0)
                self.assertEqual(result["spatial_located_visits"], 0)
                self.assertIsNone(result["visit_path_geo_m"])
                self.assertIsNone(result["within_day_dispersion_m"])
                self.assertIsNone(result["cross_day_outlier_count"])

    def test_legal_coordinate_boundaries_and_antipodes(self):
        result = spatial_metrics([day(visit("-180,-90"), visit("180,90"))])
        self.assertEqual(result["spatial_coordinate_coverage"], 1)
        self.assertAlmostEqual(result["visit_path_geo_m"], math.pi * 6371000)

    def test_visit_minutes_priority_and_internal_meal_subtraction(self):
        meal = {"start": "12:00", "end": "13:00", "duration_minutes": 10}
        activities = [visit(visit_minutes=37, start="09:00", end="17:00", duration_minutes=600, meal_break=meal),
                      visit(start="09:00", end="17:00", duration_minutes=600, meal_break=meal),
                      visit(duration_minutes=480, meal_break=meal),
                      visit(duration_minutes=120, meal_break={"duration_minutes": 30}),
                      visit(start="10:00", end="11:00"), visit(duration_minutes=45), visit()]
        result = spatial_metrics([day(*activities, {"kind": "meal", "duration_minutes": 60})])
        self.assertEqual(result["visit_minutes"], 37 + 420 + 420 + 90 + 60 + 45)
        self.assertEqual(result["visit_minutes_known_count"], 6)

    def test_missing_invalid_and_known_zero_visit_minutes(self):
        invalid = [None, -1, float("nan"), float("inf"), True, "90"]
        result = spatial_metrics([day(*(visit(visit_minutes=value, duration_minutes=value) for value in invalid))])
        self.assertIsNone(result["visit_minutes"])
        self.assertEqual(result["visit_minutes_known_count"], 0)
        result = spatial_metrics([day(visit(visit_minutes=0, duration_minutes=90), visit())])
        self.assertEqual(result["visit_minutes"], 0)
        self.assertEqual(result["visit_minutes_known_count"], 1)

    def test_invalid_primary_minutes_fall_back_to_known_actual_times(self):
        result = spatial_metrics([day(visit(visit_minutes=float("nan"), start="10:00", end="11:30"),
                                      visit(start="11:00", end="10:00", duration_minutes=30))])
        self.assertEqual(result["visit_minutes"], 120)
        self.assertEqual(result["visit_minutes_known_count"], 2)

    def test_spatial_metrics_preserve_input(self):
        days = [day(visit("0,0", start="09:00", end="11:00"), visit(), visit("1,0"))]
        original = copy.deepcopy(days)
        spatial_metrics(days)
        self.assertEqual(days, original)

    def test_geometry_does_not_call_production_distance_or_clustering(self):
        def reject_import(name, *args, **kwargs):
            if name.endswith("day_clustering"):
                raise AssertionError("production clustering imported")
            return original_import(name, *args, **kwargs)

        original_import = __import__
        with patch("builtins.__import__", side_effect=reject_import), \
                patch("app.routing.distance", side_effect=AssertionError("production distance called")), \
                patch("app.routing.coordinates", side_effect=AssertionError("production coordinates called")):
            result = spatial_metrics([day(visit("0,0"), visit("1,0")),
                                      day(visit("1,0"), date="2026-12-13")])
        self.assertAlmostEqual(result["visit_path_geo_m"], 111194.92664455874)
        self.assertEqual(result["cross_day_outlier_count"], 1)

    def test_evaluate_scores_final_draft_under_all_ablation_switches(self):
        requirements = {"origin": "武汉", "cities": ["广州"], "start_date": "2026-12-12", "end_date": "2026-12-12",
                        "people": 2, "rooms": 1, "budget": 4000, "local_transport": "public", "max_walk_per_day": 8000}
        days = [day(visit("0,0", start="09:00", end="10:00"),
                    visit("1,0", id="b", name="另一景点", start="11:00", end="12:00"))]
        candidate = {"draft": {"days": days, "transport": [], "hotels": []},
                     "days": [day(visit("90,0"))], "spatial_plan": {"days": [day(visit("180,0"))]},
                     "review": {"status": "pass", "findings": []}, "confirmation_blockers": []}
        original = copy.deepcopy(candidate)
        baseline = evaluate(requirements, candidate, {"a", "b"})
        with ablation.use(frozenset({"reviewer", "dedupe", "code_checks", "clustering"})):
            result = evaluate(requirements, candidate, {"a", "b"})
            self.assertEqual(ablation.active(), {"reviewer", "dedupe", "code_checks", "clustering"})
        self.assertEqual(result, baseline)
        for key, value in spatial_metrics(days).items():
            self.assertEqual(result[key], value)
            self.assertNotIn(key, VIOLATIONS)
        self.assertEqual(result["visits"], 2)
        self.assertEqual(result["visit_minutes"], 120)
        self.assertEqual(result["confirmable"], 1)
        self.assertEqual(result["total_violations"], sum(result[key] for key in VIOLATIONS))
        self.assertEqual(result["escaped_violations"], result["total_violations"])
        self.assertEqual(candidate, original)

    def test_evaluate_does_not_use_unscheduled_route_or_spatial_plan(self):
        requirements = {"cities": ["广州"], "start_date": "2026-12-12", "end_date": "2026-12-12"}
        candidate = {"days": [day(visit("0,0"))], "spatial_plan": {"days": [day(visit("1,0"))]}}
        result = evaluate(requirements, candidate, {"a"})
        self.assertEqual(result["visits"], 0)
        self.assertIsNone(result["visit_path_geo_m"])
        self.assertIsNone(result["visit_minutes"])
        self.assertEqual(result["visit_minutes_known_count"], 0)
