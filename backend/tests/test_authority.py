import copy
import unittest

from app.authority import assert_system_action, authorize, user_changes, user_state, user_texts
from app.conflicts import apply_option
from app.models import Requirements


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.state = {"requirements": Requirements(cities=["广州"], budget=1000).model_dump(mode="json"),
                      "stops": {"a": {"id":"a", "name":"公园"}},
                      "stop_constraints": {"required":[], "excluded":[]},
                      "candidates": [{"id":"route", "days":[{"date":"2026-11-01", "stop_ids":["a", "b"]}]}]}

    def changes(self, after):
        return user_changes(user_state(self.state), user_state(after))

    def test_every_requirement_and_unknown_field_is_protected_except_derived_basis(self):
        for field in Requirements.model_fields:
            if field in {"constraints_basis", "constraints", "required_stop_ids", "excluded_stop_ids"}:
                continue
            with self.subTest(field=field):
                after = copy.deepcopy(self.state)
                after["requirements"][field] = "changed"
                self.assertTrue(self.changes(after))
        after = copy.deepcopy(self.state)
        after["requirements"]["future_field"] = 1
        self.assertTrue(self.changes(after))
        after = copy.deepcopy(self.state)
        after["requirements"]["constraints_basis"] = "new"
        self.assertEqual(self.changes(after), [])

    def test_constraint_sources_and_all_parameters_are_preserved(self):
        for source in ("form", "message", "interview", "decision", None):
            item = {"kind":"visit_duration", "minutes":60, "strength":"hard", "scope":{"stop_ids":["a"]}, "revoke":False}
            if source:
                item["source"] = source
            self.state["requirements"]["constraints"] = [item]
            for field, value in (("minutes",30), ("strength","soft"), ("scope",{}), ("revoke",True), ("superseded_by","new")):
                with self.subTest(source=source, field=field):
                    after = copy.deepcopy(self.state)
                    after["requirements"]["constraints"][0][field] = value
                    self.assertTrue(self.changes(after))
        self.state["requirements"]["constraints"] = [{"source":"system", "minutes":90}]
        after = copy.deepcopy(self.state)
        after["requirements"]["constraints"][0]["minutes"] = 30
        self.assertEqual(self.changes(after), [])

    def test_legacy_choices_marks_appointments_and_locks_are_protected(self):
        self.state.update(locks=["a"], selected_source_ids=["guide"], reference_product_ids=["product"],
                          supplements=[{"id":"product:p", "assigned_date":"2026-11-01"}],
                          itinerary={"transport":[{"id":"train", "selected":{"id":"G1"}}],
                                     "hotels":[{"id":"hotel", "selected":{"name":"旅馆"}, "selected_by_user":True}],
                                     "days":[{"date":"2026-11-01", "city":"广州", "activities":[{
                                         "id":"a", "start":"09:00", "appointment_time":"09:00"}]}]},
                          decision_history=[{"hub_selection":{"city":"广州", "name":"机场", "poi_id":"hub"}}])
        self.state["stops"]["a"].update(map_selected_id="poi")
        projected = user_state(self.state)
        self.assertEqual(projected["pois"]["a"]["source"], "legacy_unknown")
        self.assertEqual(projected["appointments"]["2026-11-01:a"]["source"], "legacy_unknown")
        for key in ("resources", "locks", "products", "hubs", "pois", "appointments", "selected_source_ids", "reference_product_ids"):
            self.assertTrue(projected[key], key)
        after = copy.deepcopy(self.state)
        after["itinerary"]["days"][0]["activities"][0]["start"] = "10:00"
        self.assertTrue(self.changes(after))

    def test_temporary_locks_system_pois_and_appointments_are_not_user_state(self):
        after = copy.deepcopy(self.state)
        after.update(locks=["a"], temporary_locks=["a"], itinerary={"days":[{"date":"2026-11-01", "activities":[{
            "id":"a", "appointment_time":"09:00", "appointment_time_source":"system"}]}]})
        after["stops"]["a"].update(map_selected_id="automatic", map_selected_by="system")
        self.assertEqual(self.changes(after), [])

    def test_system_actions_and_order_changes_leave_user_state_unchanged(self):
        for action in ({"type":"skip", "stop_id":"a", "date":"2026-11-01"},
                       {"type":"default_duration", "stop_id":"a", "date":"2026-11-01", "minutes":40}):
            with self.subTest(action=action):
                before = copy.deepcopy(self.state)
                updates, _, _ = apply_option(self.state, action, "系统调整")
                after = {**self.state, **updates}
                self.assertEqual(self.state, before)
                assert_system_action(self.state, after)
        after = copy.deepcopy(self.state)
        after["candidates"][0]["days"][0]["stop_ids"].reverse()
        assert_system_action(self.state, after)

    def test_user_actions_require_exact_grants(self):
        for action in ({"type":"exclude", "stop_id":"a", "date":"2026-11-01"},
                       {"type":"shorten", "stop_id":"a", "date":"2026-11-01", "minutes":30}):
            updates, _, _ = apply_option(self.state, action, "同意调整")
            changes = self.changes({**self.state, **updates})
            self.assertTrue(changes)
            self.assertEqual(authorize(changes, []), (False, changes))
            grants = [{"path":item["path"], "value":item["after"], "operation":item["operation"]} for item in changes]
            self.assertEqual(authorize(changes, grants), (True, []))
        after = copy.deepcopy(self.state)
        after["requirements"]["budget"] = 2000
        changes = self.changes(after)
        self.assertFalse(authorize(changes, [{"path":"/requirements", "value":after["requirements"]}])[0])
        with self.assertRaisesRegex(ValueError, "/requirements/budget"):
            assert_system_action(self.state, after)

    def test_projection_is_detached_and_pointer_escapes_paths(self):
        projected = user_state(self.state)
        projected["requirements"]["cities"].clear()
        self.assertEqual(self.state["requirements"]["cities"], ["广州"])
        self.assertEqual(user_changes({"a/b~":1}, {"a/b~":2})[0]["path"], "/a~1b~0")

    def test_removing_current_choices_cannot_be_hidden_by_the_old_itinerary(self):
        itinerary = {"transport":[{"id":"train", "selected_by_user":True, "selected":{"name":"G1"}}]}
        self.state.update(itinerary=copy.deepcopy(itinerary), old_itinerary=copy.deepcopy(itinerary))
        after = {**self.state, "itinerary":{}}
        self.assertTrue(self.changes(after))
        with self.assertRaisesRegex(ValueError, "/resources/transport/train"):
            assert_system_action(self.state, after)

    def test_changing_actual_poi_is_not_hidden_by_the_decision_record(self):
        self.state["stops"]["a"].update(map_selected_id="chosen", map_selected_by="user", map_place={"id":"chosen"})
        self.state["decision_history"] = [{"place_selection":{"stop_id":"a", "poi_id":"chosen"}}]
        after = copy.deepcopy(self.state)
        after["stops"]["a"]["map_place"]["id"] = "different"
        self.assertTrue(self.changes(after))

    def test_resource_refresh_can_change_quote_but_cannot_change_identity(self):
        self.state["itinerary"] = {"hotels":[{"id":"hotel", "selected_by_user":True,
            "selected":{"id":"quote-old", "name":"酒店", "bed_type":"双床", "price":100}}]}
        after = copy.deepcopy(self.state)
        after["itinerary"]["hotels"][0]["selected"].update(id="quote-new", price=200)
        assert_system_action(self.state, after)
        after["itinerary"]["hotels"][0]["selected"]["bed_type"] = "大床"
        self.assertTrue(self.changes(after))

    def test_raw_texts_ignore_system_summaries_and_assumptions(self):
        state = {"preferences_original":"原始偏好", "requirements":{"preferences":"模型摘要"},
                 "messages":[{"role":"user", "content":"用户消息"}, {"role":"assistant", "content":"助手"}],
                 "interview":{"history":[{"answers":{"q":"访谈原文"}}]},
                 "decision_history":[{"answers":{"q":"决策原文"}, "summary":"摘要"},
                                     {"source":"system", "note":"系统假设"}]}
        self.assertEqual(user_texts(state), ["原始偏好", "用户消息", "访谈原文", "决策原文"])

    def test_materializing_an_existing_user_anchor_is_not_a_new_authorization(self):
        self.state["requirements"].update(start_date="2026-11-01",end_date="2026-11-01",constraints=[{
            "kind":"time_anchor","anchor":"appointment","at":"10:00","source":"decision",
            "scope":{"stop_ids":["a"],"dates":["2026-11-01"]}}])
        after = copy.deepcopy(self.state)
        after["itinerary"] = {"days":[{"date":"2026-11-01","activities":[{
            "id":"a","appointment_time":"10:00","appointment_time_source":"decision"}]}]}
        self.assertEqual(self.changes(after),[])
        after["requirements"]["constraints"][0]["at"] = "11:00"
        self.assertTrue(self.changes(after))
