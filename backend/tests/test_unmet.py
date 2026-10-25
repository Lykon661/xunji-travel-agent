import copy
import unittest

from app.decisions import confirmation_blockers, confirmation_issues
from app.models import Requirements, UnmetItem
from app.unmet import collect, fingerprint


DAY = "2026-11-10"
REQ = Requirements(origin="杭州", cities=["杭州"], start_date=DAY, end_date=DAY, people=1, rooms=1,
                   budget=1000, return_to_origin=False).model_dump(mode="json")


def plan():
    return {"transport":[], "hotels":[], "days":[{"date":DAY, "city":"杭州", "activities":[{
        "id":"a", "name":"公园", "kind":"visit", "start":"09:00", "end":"10:00", "evidence":{"quote":"公园"},
        "map_place":{"id":"poi", "location":"120,30"}, "move_before":{}}], "walking_meters":9000}]}


class StructuredUnmet(unittest.TestCase):
    def test_model_refs_with_both_or_no_identifiers_are_normalized(self):
        item = UnmetItem.model_validate({"kind":"budget", "refs":[
            {"type":"requirement", "id":"budget", "field":"budget"}, {"type":"stop", "id":None, "field":"a"},
            {"type":"requirement", "id":"", "field":"max_walk_per_day"}, {"type":"day", "id":None, "field":None},
            {"type":"day", "id":"", "field":""}]})
        self.assertEqual([ref.model_dump(exclude_none=True) for ref in item.refs],
                         [{"type":"requirement", "id":"budget"}, {"type":"stop", "id":"a"},
                          {"type":"requirement", "field":"max_walk_per_day"}])

    def test_fingerprint_uses_kind_normalized_refs_and_dates_only(self):
        first = UnmetItem(kind="capacity", refs=[{"type":"stop", "id":"a"}, {"type":"day", "id":DAY}], dates=[DAY],
                          numbers={"shortfall_minutes":20}, detail="你必须改变要求")
        second = first.model_copy(deep=True)
        second.refs.reverse()
        second.refs.append(second.refs[0])
        second.detail, second.numbers, second.blocking = "完全不同的文字", {"shortfall_minutes":50}, False
        self.assertEqual(fingerprint(first), fingerprint(second))
        second.kind = "walking"
        self.assertNotEqual(fingerprint(first), fingerprint(second))

    def test_confirmation_records_preserve_the_existing_text_interface(self):
        itinerary = plan()
        itinerary["days"][0]["activities"][0]["map_place"] = None
        issues = confirmation_issues(itinerary, {"required":["b"]}, REQ, {"b":{"id":"b", "name":"博物馆", "city":"杭州"}})
        self.assertEqual(confirmation_blockers(itinerary, {"required":["b"]}, REQ, {"b":{"id":"b", "name":"博物馆", "city":"杭州"}}),
                         list(dict.fromkeys(item["message"] for item in issues)))
        self.assertTrue(any(item["code"] == "stop_unplaced" and {"type":"stop", "id":"b"} in item["refs"] for item in issues))
        self.assertTrue(any(item["code"] == "place_identity" and item["dates"] == [DAY] for item in issues))
        self.assertTrue(all({"code", "refs", "dates", "numbers", "blocking"} <= item.keys() for item in issues))

    def test_collection_uses_structured_codes_and_numbers_without_reading_messages(self):
        itinerary = plan()
        state = {"requirements":REQ, "stops":{"a":{"id":"a", "name":"公园", "city":"杭州"}}}
        before = copy.deepcopy(itinerary)
        result = collect(state, itinerary, {"findings":[{"code":"capacity", "severity":"blocker", "target":"a",
            "field_path":"/days/0/activities/0", "message":"只是展示说明", "numbers":{"shortfall_minutes":30}}]})
        walking = next(item for item in result if item.kind == "walking")
        self.assertEqual(walking.numbers["excess_meters"], 1000)
        capacity = next(item for item in result if item.kind == "capacity")
        self.assertEqual(list(map(str, capacity.dates)), [DAY])
        self.assertEqual(capacity.numbers["shortfall_minutes"], 30)
        self.assertEqual(itinerary, before)
        changed = copy.deepcopy(itinerary)
        changed["issues"] = [{"code":"unknown_code", "target":"a", "blocking":False, "message":"预算超限步行超限时间冲突"}]
        other = next(item for item in collect(state, changed) if item.kind == "other")
        self.assertFalse(other.blocking)

    def test_duplicate_sources_merge_without_dropping_blocking_status(self):
        itinerary = plan()
        itinerary["issues"] = [{"code":"walking_limit", "target":DAY, "message":"重复的步行说明", "blocking":True}]
        result = collect({"requirements":REQ, "stops":{}}, itinerary)
        self.assertEqual(len([item for item in result if item.kind == "walking"]), 1)
