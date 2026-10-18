"""同一道追问回答两次仍未解决时不再追问：保留草稿与阻断，转入路线确认，由用户在对应面板处理。"""
import unittest
from unittest.mock import patch

from app.planner import Planner, repeated_question

QUESTION = {"id":"hotel_selection","title":"选择住宿","question":"当前没有已采用的酒店，请先选择一间酒店。",
            "why_needed":"需要住宿起终点","options":[],"recommendation":"选择一间酒店"}


def answered(question):
    return {"questions":[question],"answers":{question["id"]:"选择一间酒店"},"note":"","summary":"未提供"}


def state(history):
    return {"decision":QUESTION,"decision_history":history,"generation":"g","draft_revision":2,"decision_resume":"assess",
            "requirements":{},"warnings":[],
            "candidates":[{"id":"draft","days":[],"draft":{"days":[]},"confirmation_blockers":["住宿尚未选定"]}]}


class RepeatedQuestionGuard(unittest.IsolatedAsyncioTestCase):
    async def gate(self, history):
        with patch("app.planner.interrupt", side_effect=AssertionError("should not ask again")) as asked:
            try:
                return await Planner.__new__(Planner).decision_gate(state(history)), asked
            except AssertionError:
                return None, asked

    async def test_third_ask_goes_to_route_confirmation_with_a_visible_blocker(self):
        result, _ = await self.gate([answered(QUESTION),answered(QUESTION)])
        self.assertEqual((result["decision"],result["decision_resume"],result["selected_route"]),({},"selection_gate",{}))
        blockers = result["candidates"][0]["confirmation_blockers"]
        self.assertEqual(blockers[0],"住宿尚未选定")
        self.assertIn("已回答两次仍无法解决",blockers[-1])
        self.assertIn(blockers[-1],result["warnings"])

    async def test_second_ask_is_still_shown_to_the_user(self):
        result, asked = await self.gate([answered(QUESTION)])
        self.assertIsNone(result)
        asked.assert_called_once()

    def test_reworded_question_under_a_new_id_still_counts(self):
        reworded = {**QUESTION,"id":"hotel_selection_2","question":"当前没有已采用的酒店。请先选择一间酒店。"}
        self.assertEqual(repeated_question(reworded,[answered(QUESTION),answered(QUESTION)]),2)
        different = {**QUESTION,"id":"lunch","question":"12月13日午餐安排在永庆坊附近还是广州塔附近？"}
        self.assertEqual(repeated_question(different,[answered(QUESTION)]),0)


if __name__ == "__main__":
    unittest.main()
