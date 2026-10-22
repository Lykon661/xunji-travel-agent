import unittest

from app import ablation
from app.places import canonicalize_stops, duplicate_issues, strip_suffix


def stop(identity, name, location=None, **extra):
    return {"id":identity,"name":name,"city":"广州","category":"sight",
            "map_place":{"id":identity,"name":name,"location":location} if location else None,**extra}


class PlaceAliases(unittest.TestCase):
    def test_suffixes_are_stripped(self):
        for suffix in ("步行街","步行区","风景名胜区","风景区","旅游区","景区"):
            with self.subTest(suffix=suffix):
                self.assertEqual(strip_suffix("上下九"+suffix),"上下九")

    def test_nearby_suffix_aliases_share_identity_and_flag_duplicates(self):
        first = stop("a","上下九","113.248,23.114")
        second = stop("b","上下九步行街","113.251,23.114")
        stops = canonicalize_stops({"a":first,"b":second})
        self.assertEqual(stops["a"]["canonical_place_id"],stops["b"]["canonical_place_id"])
        plan = {"days":[{"date":"2026-12-26","city":"广州","activities":[{**first,"kind":"visit"}]},
                        {"date":"2026-12-27","city":"广州","activities":[{**second,"kind":"visit"}]}]}
        self.assertEqual([issue["code"] for issue in duplicate_issues(plan)],["duplicate_place"])

    def test_nearby_city_prefix_aliases_share_identity(self):
        stops = canonicalize_stops({"a":stop("a","西安钟楼","108.940,34.261",city="西安"),
                                    "b":stop("b","钟楼","108.943,34.261",city="西安")})
        self.assertEqual(stops["a"]["canonical_place_id"],stops["b"]["canonical_place_id"])

    def test_nearby_identical_names_remain_distinct(self):
        stops = canonicalize_stops({"a":stop("a","钟楼","108.940,34.261",city="西安"),
                                    "b":stop("b","钟楼","108.943,34.261",city="西安")})
        self.assertNotEqual(stops["a"]["canonical_place_id"],stops["b"]["canonical_place_id"])

    def test_distant_suffix_aliases_remain_distinct(self):
        stops = canonicalize_stops({"a":stop("a","上下九","113.248,23.114"),
                                    "b":stop("b","上下九步行街","113.278,23.114")})
        self.assertNotEqual(stops["a"]["canonical_place_id"],stops["b"]["canonical_place_id"])

    def test_beijing_road_pedestrian_alias_still_merges(self):
        stops = canonicalize_stops({"a":stop("a","北京路","113.268,23.121"),
                                    "b":stop("b","北京路步行街","113.271,23.121")})
        self.assertEqual(stops["a"]["canonical_place_id"],stops["b"]["canonical_place_id"])

    def test_suffix_aliases_without_coordinates_merge(self):
        stops = canonicalize_stops({"a":stop("a","上下九"),"b":stop("b","广州上下九步行街")})
        self.assertEqual(stops["a"]["canonical_place_id"],stops["b"]["canonical_place_id"])

    def test_suffix_aliases_require_matching_city_and_coordinate_availability(self):
        for extra in ({"city":"杭州"},{"map_place":{"id":"b","location":"113.251,23.114"}},
                      {"category":"dining"},{"source_kind":"flyai"}):
            with self.subTest(extra=extra):
                stops = canonicalize_stops({"a":stop("a","上下九"),"b":stop("b","上下九步行街",**extra)})
                self.assertNotEqual(stops["a"]["canonical_place_id"],stops["b"]["canonical_place_id"])

    def test_dedupe_ablation_leaves_suffix_aliases_distinct(self):
        with ablation.use(frozenset({"dedupe"})):
            stops = canonicalize_stops({"a":stop("a","上下九"),"b":stop("b","上下九步行街")})
        self.assertNotEqual(stops["a"]["canonical_place_id"],stops["b"]["canonical_place_id"])
