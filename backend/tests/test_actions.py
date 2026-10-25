import copy
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app import actions
from app.actions import AuthorityError, action_grants, authority_of, execute, validate_option
from app.authority import user_state
from app.constraints import active, merge, walking_target
from app.models import ConstraintDraft, DecisionBatch, DecisionInterpretation, DecisionOption, PlanAction, Requirements, UnmetItem
from app.rules import check_itinerary, walking_limit


DAY, OTHER = "2026-11-10", "2026-11-11"


def state():
    req = Requirements(origin="杭州", cities=["杭州"], start_date=DAY, end_date=OTHER, people=1, rooms=1,
                       budget=1000, return_to_origin=False).model_dump(mode="json")
    stops = {identity:{"id":identity, "name":name, "city":"杭州", "source_id":"guide", "source_title":"攻略",
                       "duration_minutes":90, "evidence":{"quote":name}}
             for identity, name in (("a", "甲公园"), ("b", "乙公园"), ("c", "丙公园"), ("d", "丁公园"))}
    stops["a"]["map_candidates"] = [{"id":"poi", "name":"甲公园", "location":"120,30"}]
    route = {"id":"route", "days":[{"date":DAY, "city":"杭州", "stop_ids":["a", "b"]},
                                     {"date":OTHER, "city":"杭州", "stop_ids":["c"]}]}
    plan = {"transport":[], "hotels":[], "days":[{"date":day["date"], "city":"杭州", "gaps":[], "walking_meters":9000,
        "available_start":540, "tail_deadline":1080, "activities":[{
            **stops[identity], "kind":"visit", "start":f"{9+index*2}:00", "end":f"{10+index*2}:30", "move_before":{}}
            for index,identity in enumerate(day["stop_ids"])]} for day in route["days"]]}
    return {"requirements":req, "stops":stops, "selected_source_ids":["guide"], "locks":[],
            "stop_constraints":{"required":[], "excluded":[]}, "candidates":[copy.deepcopy(route)],
            "selected_route":copy.deepcopy(route), "itinerary":plan, "decision_history":[],
            "available_sources":[{"id":"extra", "title":"另一攻略", "extraction":{"stops":[{
                "id":"e", "name":"戊公园", "city":"杭州", "duration_minutes":60}]}}],
            "place_selection":{"kind":"hub", "city":"杭州", "name":"东站", "options":{"东出口":"hub"}}}


class ActionSchemas(unittest.TestCase):
    def test_empty_parameters_and_misplaced_day_are_normalized(self):
        skip = PlanAction.model_validate({"type":"skip", "stop_id":"a", "date":None, "dates":["2026-11-10"], "minutes":None, "stop_ids":[]})
        self.assertEqual((str(skip.date), skip.dates), ("2026-11-10", []))
        exempt = PlanAction.model_validate({"type":"exempt_walk_limit", "date":"2026-11-10", "dates":[]})
        self.assertEqual([str(item) for item in exempt.dates], ["2026-11-10"])
        with self.assertRaises(ValidationError):
            PlanAction.model_validate({"type":"skip", "stop_id":"a", "dates":["2026-11-10", "2026-11-11"]})


    def test_models_forbid_unknown_fields_at_all_levels_and_bound_lengths(self):
        with self.assertRaises(ValidationError):
            UnmetItem(kind="other", made_up=True)
        with self.assertRaises(ValidationError):
            UnmetItem(kind="other", refs=[{"type":"stop", "id":"a", "extra":True}])
        with self.assertRaises(ValidationError):
            UnmetItem(kind="other", detail="x"*2001)
        for value in ({"type":"unknown"}, {"type":"set_budget", "amount":2000, "stop_id":"a"},
                      {"type":"move", "stop_id":"a", "date":DAY}, {"type":"set_walk_limit", "dates":[], "meters":4000}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                PlanAction.model_validate(value)
        with self.assertRaises(ValidationError):
            DecisionOption(id="o", label="选择", actions=[{"type":"set_budget", "amount":2000}], impact={"extra":1})

    def test_batch_references_and_interpretation_quotes_are_checked(self):
        option = {"id":"one", "label":"调整预算", "actions":[{"type":"set_budget", "amount":2000}]}
        question = {"id":"q", "title":"预算", "question":"如何调整？", "why_needed":"预算不足",
                    "options":[option, {**option,"id":"two"}], "recommended_option_id":"one"}
        batch = DecisionBatch(batch_id="batch", draft_revision=1, stage_scope="draft_tradeoff", summary="取舍", questions=[question])
        self.assertEqual(batch.questions[0].options[0].actions[0].type, "set_budget")
        with self.assertRaises(ValidationError):
            DecisionBatch(**{**batch.model_dump(), "questions":[question]*7})
        with self.assertRaises(ValidationError):
            DecisionBatch(batch_id="batch", draft_revision=1, stage_scope="draft_tradeoff", summary="取舍",
                          questions=[{**question, "recommended_option_id":"missing"}])
        with self.assertRaises(ValidationError):
            DecisionInterpretation(interpretation_id="i", summary="解释", actions=[], quotes=[{"action_index":0, "quote":"预算2000"}])


class ActionExecution(unittest.TestCase):
    def setUp(self):
        self.state = state()

    def apply(self, action, actor="user", **kwargs):
        before = copy.deepcopy(self.state)
        updates, dates, summary = execute(self.state, [action], actor, **kwargs)
        self.assertEqual(before, self.state)
        self.assertTrue(summary)
        return {**self.state, **updates}, dates

    def test_every_action_executes(self):
        count = ConstraintDraft(kind="daily_min_stops", count=3, quote="每天三个景点").model_dump(mode="json")
        self.state["requirements"]["constraints"] = merge([], [count], "form")
        cases = [
            {"type":"skip", "stop_id":"a", "date":DAY},
            {"type":"default_duration", "stop_id":"a", "date":DAY, "minutes":60},
            {"type":"exclude", "stop_id":"a", "date":DAY},
            {"type":"move", "stop_id":"a", "date":DAY, "to":OTHER},
            {"type":"shorten", "stop_id":"a", "date":DAY, "minutes":60},
            {"type":"add_source", "source_id":"extra", "date":DAY},
            {"type":"lower_count", "count":2, "dates":[DAY]},
            {"type":"accept_short", "dates":[DAY]}, {"type":"keep_short", "date":DAY},
            {"type":"set_walk_limit", "dates":[DAY], "meters":10000},
            {"type":"exempt_walk_limit", "dates":[DAY]}, {"type":"restore_walk_limit", "dates":[DAY]},
            {"type":"set_budget", "amount":2000},
            {"type":"set_time_window", "field":"earliest_departure", "value":"10:00"},
            {"type":"set_time_window", "stop_id":"a", "at":"09:30", "dates":[DAY]},
            {"type":"set_hotel_area", "city":"杭州", "area":"湖滨"},
            {"type":"select_poi", "stop_id":"a", "poi_id":"poi"},
            {"type":"select_hub", "city":"杭州", "name":"东站", "poi_id":"hub"},
            {"type":"grant_repeat", "stop_id":"a", "role":"visit", "dates":[DAY,OTHER]},
            {"type":"reorder_day", "date":DAY, "stop_ids":["b","a"]},
            {"type":"replace_optional_stop", "date":DAY, "stop_id":"a", "replacement_stop_id":"d"},
        ]
        for action in cases:
            with self.subTest(action=action):
                self.apply(action)

    def test_system_changes_to_user_state_are_rejected_atomically(self):
        before = copy.deepcopy(self.state)
        with self.assertRaises(AuthorityError) as caught:
            execute(self.state, [{"type":"exclude", "stop_id":"a", "date":DAY}], "system")
        self.assertIn("/requirements/excluded_stop_ids", caught.exception.paths)
        self.assertEqual(self.state, before)
        self.state["requirements"]["required_stop_ids"] = ["a"]
        with self.assertRaisesRegex(ValueError, "protected"):
            execute(self.state, [{"type":"skip", "stop_id":"a", "date":DAY}], "system")

    def test_user_execution_rejects_any_extra_write_beyond_declared_grants(self):
        original = actions.apply_option
        def polluted(*args):
            updates, dates, summary = original(*args)
            updates["requirements"]["budget"] = 9000
            return updates, dates, summary
        with patch("app.actions.apply_option", side_effect=polluted), self.assertRaises(AuthorityError) as caught:
            execute(self.state, [{"type":"shorten", "stop_id":"a", "date":DAY, "minutes":60}], "user")
        self.assertIn("/requirements/budget", caught.exception.paths)
        with patch("app.actions.action_grants", return_value=[]), self.assertRaises(AuthorityError):
            execute(self.state, [{"type":"set_budget", "amount":2000}], "user")

    def test_authority_is_based_on_actual_changes(self):
        for action in ({"type":"skip", "stop_id":"a", "date":DAY},
                       {"type":"default_duration", "stop_id":"a", "date":DAY, "minutes":60},
                       {"type":"reorder_day", "date":DAY, "stop_ids":["b","a"]}):
            self.assertEqual(authority_of(self.state, [action]), "system")
            self.apply(action, "system")
        for action in ({"type":"exclude", "stop_id":"a", "date":DAY}, {"type":"set_budget", "amount":2000},
                       {"type":"shorten", "stop_id":"a", "date":DAY, "minutes":60}):
            self.assertEqual(authority_of(self.state, [action]), "user")
        self.assertEqual(authority_of(self.state, [{"type":"set_budget", "amount":1000}]), "system")

    def test_dated_walk_override_relaxes_or_tightens_only_the_selected_day(self):
        for meters in (4000, 10000, 0):
            with self.subTest(meters=meters):
                after, dates = self.apply({"type":"set_walk_limit", "dates":[DAY], "meters":meters})
                req = after["requirements"]
                self.assertEqual(dates, [DAY])
                self.assertEqual(walking_limit(req, DAY), meters)
                self.assertEqual(walking_target(req, DAY), meters)
                self.assertEqual(walking_limit(req, OTHER), 8000)
                result = check_itinerary(after["itinerary"], req)
                blockers = [item for item in result["issues"] if item["code"] == "walking_limit" and item["target"] == DAY]
                self.assertEqual(bool(blockers), meters < 9000)
        self.assertEqual(len(active(after["requirements"], "fewer_walking", date=DAY)), 1)

    def test_walk_exemption_and_restore_are_explicit(self):
        after, _ = self.apply({"type":"exempt_walk_limit", "dates":[DAY]})
        self.assertIsNone(walking_limit(after["requirements"], DAY))
        self.state = after
        after, _ = self.apply({"type":"restore_walk_limit", "dates":[DAY]})
        self.assertEqual(walking_limit(after["requirements"], DAY), 8000)

    def test_reorder_and_replace_do_not_change_user_state(self):
        for action in ({"type":"reorder_day", "date":DAY, "stop_ids":["b","a"]},
                       {"type":"replace_optional_stop", "date":DAY, "stop_id":"a", "replacement_stop_id":"d"}):
            after, _ = self.apply(action, "system")
            self.assertEqual(user_state(after), user_state(self.state))
        self.state["locks"] = ["a"]
        with self.assertRaisesRegex(ValueError, "locked"):
            self.apply({"type":"reorder_day", "date":DAY, "stop_ids":["b","a"]}, "system")

    def test_unknown_objects_and_inapplicable_parameters_are_rejected(self):
        invalid = [
            {"type":"typo"}, {"type":"skip", "stop_id":"missing", "date":DAY},
            {"type":"move", "stop_id":"a", "date":DAY, "to":"2026-12-01"},
            {"type":"select_poi", "stop_id":"a", "poi_id":"invented"},
            {"type":"select_hub", "city":"杭州", "name":"东站", "poi_id":"invented"},
            {"type":"set_hotel_area", "city":"未知城市", "area":"中心"},
            {"type":"add_source", "source_id":"missing"},
            {"type":"replace_optional_stop", "date":DAY, "stop_id":"a", "replacement_stop_id":"c"},
            {"type":"reorder_day", "date":DAY, "stop_ids":["a","missing"]},
        ]
        for action in invalid:
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.apply(action)

    def test_free_text_requires_a_found_quote_for_every_user_change(self):
        action = {"type":"set_budget", "amount":2000}
        for quotes in ([], ["编造原文"], [""]):
            with self.assertRaisesRegex(ValueError, "quote"):
                self.apply(action, quotes=quotes)
        self.state["messages"] = [{"role":"user", "content":"预算提高到2000元"}]
        self.apply(action, quotes=[{"action_index":0, "quote":"预算提高到2000元"}])
        self.apply(action, quotes={"text":"我同意预算2000", "quotes":["预算2000"]})
        self.apply({"type":"shorten", "stop_id":"a", "date":DAY, "minutes":60},
                   quotes={"answer_text":"公园玩60分钟", "quotes":["公园玩60分钟"]})

    def test_grants_are_exact_paths_and_values_for_composed_actions(self):
        grants = action_grants(self.state, [{"type":"set_budget", "amount":2000}, {"type":"set_budget", "amount":3000}])
        self.assertEqual(grants, [{"path":"/requirements/budget", "value":3000, "operation":"replace"}])

    def test_multi_action_failure_keeps_the_entire_input_unchanged(self):
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError):
            execute(self.state, [{"type":"set_budget", "amount":2000}, {"type":"select_poi", "stop_id":"a", "poi_id":"missing"}], "user")
        self.assertEqual(self.state, before)

    def test_legacy_assigned_day_changes_require_user_authority(self):
        self.state["stops"]["a"]["assigned_date"] = DAY
        action = {"type":"move", "stop_id":"a", "date":DAY, "to":OTHER}
        self.assertEqual(authority_of(self.state, [action]), "user")
        with self.assertRaises(AuthorityError):
            self.apply(action, "system")
        after, _ = self.apply(action)
        self.assertEqual(after["stops"]["a"]["assigned_date"], OTHER)

    def test_reordering_preserves_fixed_orders_and_locked_positions(self):
        fixed = ConstraintDraft(kind="fixed_order", before="a", after="b", quote="先甲后乙").model_dump(mode="json")
        self.state["requirements"]["constraints"] = merge([], [fixed], "form")
        with self.assertRaisesRegex(ValueError, "fixed user order"):
            self.apply({"type":"reorder_day", "date":DAY, "stop_ids":["b","a"]}, "system")

    def test_dated_walk_override_preserves_constraints_on_other_days(self):
        fixed = ConstraintDraft(kind="fewer_walking", meters=4000, quote="每天最多4000米").model_dump(mode="json")
        self.state["requirements"]["constraints"] = merge([], [fixed], "form", trip_dates=[DAY,OTHER])
        after, _ = self.apply({"type":"set_walk_limit", "dates":[DAY], "meters":10000})
        self.assertEqual(walking_limit(after["requirements"], DAY), 10000)
        self.assertEqual(walking_limit(after["requirements"], OTHER), 4000)
        self.assertTrue(active(after["requirements"], "fewer_walking", date=OTHER))

    def test_poi_selection_can_be_system_default_until_user_adopts_it(self):
        action = {"type":"select_poi", "stop_id":"a", "poi_id":"poi"}
        self.assertEqual(authority_of(self.state, [action]), "system")
        after, _ = self.apply(action, "system")
        self.assertEqual(after["stops"]["a"]["map_selected_by"], "system")
        after, _ = self.apply(action, "user")
        self.assertEqual(after["stops"]["a"]["map_selected_by"], "user")
        self.assertEqual(after["decision_history"][-1]["place_selection"], {"stop_id":"a", "city":"杭州", "name":"甲公园", "poi_id":"poi"})
        self.assertEqual(after["decision_history"][-1]["source"], "decision")
        self.assertEqual(user_state(after)["poi_decisions"]["a"]["id"], "poi")
        self.state["requirements"]["required_stop_ids"] = ["a"]
        self.assertEqual(authority_of(self.state, [action]), "user")

    def test_adopting_a_hub_is_not_inferred_from_a_prior_system_choice(self):
        self.state["decision_history"] = [{"source":"system", "hub_selection":{"city":"杭州", "name":"东站", "poi_id":"hub"}}]
        action = {"type":"select_hub", "city":"杭州", "name":"东站", "poi_id":"hub"}
        self.assertEqual(authority_of(self.state, [action]), "user")
        after, _ = self.apply(action)
        self.assertEqual(after["decision_history"][-1]["source"], "decision")

    def test_count_changes_only_relax_the_requested_dates(self):
        count = ConstraintDraft(kind="daily_min_stops", count=3, quote="每天至少三个").model_dump(mode="json")
        self.state["requirements"]["constraints"] = merge([], [count], "form", trip_dates=[DAY,OTHER])
        after, _ = self.apply({"type":"lower_count", "count":2, "dates":[DAY]})
        self.assertEqual(active(after["requirements"], "daily_min_stops", date=DAY)[0]["count"], 2)
        self.assertEqual(active(after["requirements"], "daily_min_stops", date=OTHER)[0]["count"], 3)

    def test_validate_option_checks_ticket_savings_without_assuming_unknown_tickets_are_free(self):
        activity = self.state["itinerary"]["days"][0]["activities"][0]
        activity["ticket_quote"] = {"price":100, "price_date":DAY}
        option = {"id":"o", "label":"减少门票费用", "actions":[{"type":"exclude", "stop_id":"a", "date":DAY}], "impact":{"amount":100}}
        self.assertTrue(validate_option(self.state, option)[0])
        activity["ticket_quote"]["price_unknown"] = True
        self.assertFalse(validate_option(self.state, option)[0])

    def test_validate_option_checks_the_destination_capacity_of_a_move(self):
        self.state["itinerary"]["days"][1].update(available_start=540, tail_deadline=600)
        option = {"id":"o", "label":"移到次日", "actions":[{"type":"move", "stop_id":"a", "date":DAY, "to":OTHER}]}
        ok, problems = validate_option(self.state, option)
        self.assertFalse(ok)
        self.assertTrue(any("destination day" in problem for problem in problems))

    def test_option_validation_uses_references_without_requiring_it_to_fix_unrelated_unmet_items(self):
        self.state["unmet"] = [
            {"kind":"other", "refs":[{"type":"requirement", "field":"budget"}], "numbers":{"excess_amount":100}},
            {"kind":"budget", "refs":[{"type":"stop", "id":"a"}], "dates":[DAY], "numbers":{"shortfall_minutes":30}}]
        option = {"id":"o", "label":"增加预算", "actions":[{"type":"set_budget", "amount":1200}]}
        self.assertTrue(validate_option(self.state, option)[0])
        option = {"id":"o", "label":"缩短游玩", "actions":[{"type":"shorten", "stop_id":"a", "date":DAY, "minutes":60}]}
        self.assertTrue(validate_option(self.state, option)[0])

    def test_system_default_duration_cannot_claim_savings_from_an_explicit_user_duration(self):
        self.state["stops"]["a"].update(source_kind="user", duration_minutes=90)
        option = {"id":"o", "label":"修改参考时长", "actions":[{"type":"default_duration", "stop_id":"a", "date":DAY, "minutes":30}],
                  "impact":{"minutes":60}}
        self.assertFalse(validate_option(self.state, option)[0])

    def test_validate_option_recomputes_minutes_budget_and_meters(self):
        cases = [({"type":"shorten", "stop_id":"a", "date":DAY, "minutes":60}, {"shortfall_minutes":40}, False),
                 ({"type":"shorten", "stop_id":"a", "date":DAY, "minutes":40}, {"shortfall_minutes":40}, True),
                 ({"type":"set_budget", "amount":1100}, {"excess_amount":200}, False),
                 ({"type":"set_budget", "amount":1300}, {"excess_amount":200}, True),
                 ({"type":"set_walk_limit", "dates":[DAY], "meters":9000}, {"excess_meters":2000}, False),
                 ({"type":"set_walk_limit", "dates":[DAY], "meters":10000}, {"excess_meters":2000}, True)]
        for action, numbers, expected in cases:
            with self.subTest(action=action, numbers=numbers):
                self.state["unmet"] = [{"kind":"other", "dates":[DAY], "numbers":numbers}]
                option = {"id":"option", "label":"调整", "actions":[action]}
                before = copy.deepcopy(self.state)
                ok, problems = validate_option(self.state, option)
                self.assertEqual(ok, expected, problems)
                self.assertEqual(self.state, before)
        self.state.pop("unmet")
        ok, _ = validate_option(self.state, {"id":"o", "label":"缩短", "actions":[cases[0][0]], "impact":{"minutes":60}})
        self.assertFalse(ok)
