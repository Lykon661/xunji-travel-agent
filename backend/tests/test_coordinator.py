import copy
import unittest
from unittest.mock import AsyncMock, Mock

from app import actions, assumptions, authority
from app.coordinator import apply_answer, run, situation
from app.agents.coordinator import CoordinatorAgent
from app.models import DecisionBatch
from app.unmet import fingerprint
from tests.test_actions import DAY, OTHER, state
CASES = [
    ("10月28日安排至16:36结束，尚有约184分钟余量；是否加入广州动物园？","schedule_fill"),
    ("10月29日尚有约248分钟余量；是否加入其他现有景点，或保留休闲时间？","schedule_fill"),
    ("10月26日长隆需09:30前抵达，但当前没有住宿或出发地址，请提供出发点","start_end_location"),
    ("长隆当前安排90分钟，与大型动物园游览目的不匹配，如何调整？","duration"),
    ("当前反馈提示行程包含用户明确排除的地点，但requirements中未列出excluded_stop_ids。请确认广东省博物馆是否保留？","other"),
    ("华南植物园不在当前可用景点列表中，10月29日是否按已定位景点安排？","fact_lookup"),
    ("红砖厂当前未能定位到广州对应的历史文化园区，是否从本日行程中暂不安排？","fact_lookup"),
    ("南越王博物院票务候选名称为西汉南越王博物馆，坐标不一致，票是否适用？","ticket_match"),
    ("长隆可指欢乐世界、野生动物世界或度假区，安排哪个？","place_identity"),
]


def unmet(kind="capacity", stop="a", **extra):
    return {"kind":kind,"refs":[{"type":"stop","id":stop}],"dates":[DAY],"blocking":True,**extra}


def option(identity, action):
    return {"id":identity,"label":identity,"description":"具体安排","actions":[action]}


def question(first=None, second=None, identity="q", refs=None):
    return {"id":identity,"title":"安排取舍","question":"采用哪个安排？","why_needed":"存在待解决的差额。",
            "unmet_refs":refs or [{"type":"requirement","id":"budget"}],"affected_dates":[DAY],
            "options":[option("one",first or {"type":"set_budget","amount":2000}),
                       option("two",second or {"type":"set_budget","amount":3000})],"recommended_option_id":"one"}


def batch(q=None):
    return {"batch_id":"batch","draft_revision":1,"stage_scope":"draft_tradeoff","summary":"行程取舍",
            "questions":[q or question()]}


def prepared():
    result = state()
    result["draft_revision"] = 1
    result["unmet"] = [unmet("budget",refs=[{"type":"requirement","id":"budget"}],numbers={"excess_amount":500})]
    result["coordinator_questions"] = {"q":{"reason_kind":"needs_user_authority",
        "fingerprints":[fingerprint(result["unmet"][0])]}}
    return result


def agent(resolution=None, interpretation=None):
    result = AsyncMock()
    result.interpret.return_value = interpretation or {"interpretation_id":"i","summary":"模型描述不应用于执行消息",
        "actions":[{"type":"set_budget","amount":2000}],"quotes":[{"action_index":0,"quote":"预算2000"}]}
    return result


class QuoteBoundaries(unittest.IsolatedAsyncioTestCase):
    async def test_direction_reversing_substrings_require_confirmation(self):
        samples = [("别删掉博物馆","删掉博物馆"),("博物馆还是留着，删公园","博物馆"),
                   ("用博物馆替换公园","博物馆"),("不想放弃博物馆","放弃博物馆"),
                   ("保留原方案","原方案")]
        for text,quote in samples:
            for action in ({"type":"exclude","stop_id":"a","date":DAY},{"type":"exempt_walk_limit","dates":[DAY]}):
                with self.subTest(text=text,action=action):
                    current = prepared()
                    current["requirements"]["required_stop_ids"] = ["a"]
                    original = authority.user_state(current)
                    model = agent(interpretation={"interpretation_id":"i","summary":"模型理解",
                        "actions":[action],"quotes":[{"action_index":0,"quote":quote}]})
                    result = await apply_answer(current,batch(),{"round_id":"batch","draft_revision":1,"note":text},model)
                    self.assertIsNotNone(result.confirmation)
                    self.assertEqual(authority.user_state({**current,**result.state_updates}),original)
                    self.assertFalse(result.state_updates.get("decision_history"))

    async def test_complete_clause_can_execute_correct_exclusion(self):
        current = prepared()
        model = agent(interpretation={"interpretation_id":"i","summary":"取消公园",
            "actions":[{"type":"exclude","stop_id":"b","date":DAY}],"quotes":[{"action_index":0,"quote":"删公园"}]})
        result = await apply_answer(current,batch(),{"round_id":"batch","draft_revision":1,"note":"博物馆还是留着，删公园"},model)
        self.assertIsNone(result.confirmation)
        self.assertIn("b",result.state_updates["requirements"]["excluded_stop_ids"])
        self.assertNotIn("a",result.state_updates["requirements"]["excluded_stop_ids"])

    async def test_quotes_only_join_adjacent_complete_clauses(self):
        for text,quote,accepted in [("预算2000，日期照旧。","预算2000，日期照旧。",True),
                ("预算 2000；日期照旧","预算2000日期照旧",True),
                ("预算2000，日期照旧，步行照旧","预算2000步行照旧",False)]:
            with self.subTest(text=text,quote=quote):
                model = agent()
                model.interpret.return_value["quotes"] = [{"action_index":0,"quote":quote}]
                result = await apply_answer(prepared(),batch(),{"round_id":"batch","draft_revision":1,"note":text},model)
                self.assertEqual(result.confirmation is None,accepted)




class CoordinatorExecution(unittest.IsolatedAsyncioTestCase):




    async def test_pending_confirmation_accepts_a_new_complete_answer(self):
        current = prepared()
        model = agent()
        model.interpret.return_value["quotes"] = []
        pending = await apply_answer(current,batch(),{"batch_id":"batch","draft_revision":1,"note":"原回答"},model)
        current.update(pending.state_updates)
        model.interpret.return_value.update(actions=[{"type":"set_budget","amount":2500}],quotes=[{"action_index":0,"quote":"改为预算2500"}])
        updated = await apply_answer(current,batch(),{"batch_id":"batch","draft_revision":1,"note":"改为预算2500"},model)
        self.assertIsNone(updated.confirmation)
        self.assertEqual(updated.state_updates["requirements"]["budget"],2500)








    async def test_free_text_with_current_quote_executes(self):
        current = prepared()
        model = agent()
        outcome = await apply_answer(current,batch(),{"batch_id":"batch","draft_revision":1,"note":"预算2000"},model)
        self.assertIsNone(outcome.confirmation)
        self.assertEqual(outcome.state_updates["requirements"]["budget"],2000)
        self.assertNotIn("模型描述",outcome.messages[0]["text"])

    async def test_missing_quote_or_ambiguity_requires_confirmation_then_no_model(self):
        for change in ({"quotes":[]},{"ambiguities":["是否仅限一天"]},{"quotes":[{"action_index":0,"quote":"旧消息预算2000"}]}):
            current = prepared()
            current["messages"] = [{"role":"user","content":"旧消息预算2000"}]
            model = agent()
            model.interpret.return_value.update(change)
            original = copy.deepcopy(current)
            outcome = await apply_answer(current,batch(),{"batch_id":"batch","draft_revision":1,"note":"预算2000"},model)
            self.assertIsNotNone(outcome.confirmation)
            self.assertNotIn("requirements",outcome.state_updates)
            self.assertEqual(current,original)
            current.update(outcome.state_updates)
            model.interpret.reset_mock()
            accepted = await apply_answer(current,batch(),{"batch_id":"batch","draft_revision":1,
                "interpretation_id":"i","confirm":True},model)
            self.assertEqual(accepted.state_updates["requirements"]["budget"],2000)
            model.interpret.assert_not_awaited()

    async def test_interpretation_with_no_concrete_action_requests_confirmation(self):
        current = prepared()
        model = agent(interpretation={"interpretation_id":"i","summary":"需要补充理解","actions":[],"ambiguities":["没有明确金额"]})
        outcome = await apply_answer(current,batch(),{"batch_id":"batch","draft_revision":1,"note":"便宜点"},model)
        self.assertIsNotNone(outcome.confirmation)
        self.assertNotIn("requirements",outcome.state_updates)

    async def test_user_poi_selection_needs_a_quote_even_for_optional_stop(self):
        current = prepared()
        model = agent(interpretation={"interpretation_id":"i","summary":"具体地点", "actions":[{
            "type":"select_poi","stop_id":"a","poi_id":"poi"}],"quotes":[]})
        outcome = await apply_answer(current,batch(),{"batch_id":"batch","draft_revision":1,"note":"这边"},model)
        self.assertIsNotNone(outcome.confirmation)
        self.assertNotIn("stops",outcome.state_updates)

    async def test_stale_answer_confirmation_and_conflicting_clicks_are_atomic(self):
        current = prepared()
        with self.assertRaises(ValueError):
            await apply_answer(current,batch(),{"batch_id":"old","draft_revision":1,"selected_options":{"q":"one"}},agent())
        model = agent()
        model.interpret.return_value["quotes"] = []
        pending = await apply_answer(current,batch(),{"batch_id":"batch","draft_revision":1,"note":"同意"},model)
        current.update(pending.state_updates)
        current["requirements"]["budget"] = 500
        with self.assertRaises(ValueError):
            await apply_answer(current,batch(),{"batch_id":"batch","draft_revision":1,"interpretation_id":"i","confirm":True},model)
        raw = batch()
        raw["questions"].append(question({"type":"set_budget","amount":2500},identity="q2"))
        before = copy.deepcopy(current)
        with self.assertRaises(ValueError):
            await apply_answer(current,raw,{"batch_id":"batch","draft_revision":1,"selected_options":{"q":"one","q2":"one"}},agent())
        self.assertEqual(current,before)









if __name__ == "__main__":
    unittest.main()
