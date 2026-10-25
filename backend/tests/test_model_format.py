"""Mocked model responses only; no provider requests."""
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from app.agents.resource import ResourceAgent

from app.adapters import Models, ToolError
from app.llm import ModelRuntimeError, StructuredOutputError
from app.models import CandidateDay, GuideBrief, OrderChoice, ResourceChoice
from app.planner import Planner


class ModelFormat(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.models = Models.__new__(Models)
        self.models.config_store = SimpleNamespace(snapshot=Mock(return_value=SimpleNamespace(configured=True,public=lambda:{})))
        self.messages = []

    def resource_agent(self):
        agent = ResourceAgent(self.models,Mock(),None)
        agent.assert_current = Mock()
        agent.store.agent_result.return_value = None
        return agent

    def resource_state(self):
        return {"requirements":{},"trip_id":"synthetic","run_id":"run","generation":"generation"}

    def client(self, responses):
        def complete(messages, **kwargs):
            self.messages.append(deepcopy(messages))
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response
        self.models.client = SimpleNamespace(complete=Mock(side_effect=complete))

    async def test_compact_request_preserves_every_payload_value_and_logs_only_counts(self):
        private = "PRIVATE_GUIDE_QUOTE_DO_NOT_LOG"
        payload = {"stops":[{"id":"a","quote":private,"details":"原文中保留: 空格与换行\n下一行"}],"constraints":{"required":["a"]}}
        original = deepcopy(payload)
        self.client(iter(['{"stop_ids":["a"]}']))
        with self.assertLogs("uvicorn.error",level="INFO") as captured:
            result = await self.models.ask(OrderChoice,"保留来源证据",payload)
        content = self.messages[0][1]["content"]
        self.assertEqual(json.loads(content),original)
        self.assertEqual(content,json.dumps(original,ensure_ascii=False,separators=(",",":"),default=str))
        self.assertEqual(payload,original)
        self.assertEqual(result.stop_ids,["a"])
        logs = "\n".join(captured.output)
        self.assertIn("input_text_chars=",logs)
        self.assertIn("elapsed_seconds=",logs)
        self.assertNotIn(private,logs)

    async def test_compact_image_request_keeps_image_data_and_evidence_number(self):
        self.client(iter(['{"stop_ids":["a"]}']))
        payload = {"source":"guide","quote":"原始图片说明"}
        await self.models.ask(OrderChoice,"图片提取",payload,images=["data:image/png;base64,QUJD"],image_indices=[9])
        blocks = self.messages[0][1]["content"]
        self.assertEqual(json.loads(blocks[0]["text"]),payload)
        self.assertIn("图片 9",blocks[1]["text"])
        self.assertEqual(blocks[2],{"type":"image","data":"QUJD","mimeType":"image/png"})

    async def test_timeout_logs_stage_and_elapsed_without_logging_provider_message_or_retrying(self):
        private = "PRIVATE_PROVIDER_RESPONSE_DO_NOT_LOG"
        self.client(iter([ModelRuntimeError(private,timed_out=True)]))
        with self.assertLogs("uvicorn.error",level="INFO") as captured:
            with self.assertRaises(ToolError) as caught:
                await self.models.ask(OrderChoice,"保留来源",{})
        self.assertEqual(caught.exception.code,"model_network")
        self.assertEqual(self.models.client.complete.call_count,1)
        logs = "\n".join(captured.output)
        self.assertIn("schema=OrderChoice",logs)
        self.assertIn("timed_out=True",logs)
        self.assertNotIn(private,logs)

    async def test_invalid_field_gets_one_correction_with_original_task(self):
        self.client(iter(['{"stop_ids":"place-a"}', '{"stop_ids":["place-a"],"explanation":"保留顺序"}']))
        result = await self.models.ask(OrderChoice, "只能选原有活动", {"stops":[{"id":"place-a"}]})
        self.assertEqual(result.stop_ids, ["place-a"])
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[0][:2], self.messages[1][:2])
        self.assertIn("stop_ids", self.messages[1][-1]["content"])

    async def test_region_groups_over_twelve_stops_do_not_require_correction(self):
        groups = [{"city":"杭州","stop_ids":[f"source:{region}:{index}" for index in range(20)],"reason":"同一区域资料"}
                  for region in range(3)]
        self.client(iter([json.dumps({"status":"ready","summary":"建议区域分组","groups":groups})]))
        result = await self.models.ask(GuideBrief,"整理区域资料",{"available_stops":{identity:{"city":"杭州"}
            for group in groups for identity in group["stop_ids"]}})
        self.assertEqual([group.stop_ids for group in result.groups],[group["stop_ids"] for group in groups])
        self.assertEqual(len(self.messages),1)

    async def test_daily_stop_limit_remains_and_correction_identifies_the_bound(self):
        too_many = {"date":"2026-11-10","city":"杭州","stop_ids":[f"source:{index}" for index in range(13)]}
        corrected = {**too_many,"stop_ids":too_many["stop_ids"][:12]}
        self.client(iter([json.dumps(too_many),json.dumps(corrected)]))
        result = await self.models.ask(CandidateDay,"安排一天，不超过12项",{"available_stops":too_many["stop_ids"]})
        self.assertEqual(result.stop_ids,corrected["stop_ids"])
        message = self.messages[1][-1]["content"]
        self.assertIn("stop_ids：最多12项（当前 13项）",message)
        self.assertNotIn("source:12",message)






    async def test_repeated_invalid_field_exposes_issue_without_returned_values(self):
        self.client(iter(['{"stop_ids":"private-value"}', '{"stop_ids":"private-value"}']))
        with self.assertRaises(ToolError) as caught:
            await self.models.ask(OrderChoice, "安排活动", {})
        self.assertEqual(caught.exception.code, "model_format")
        self.assertIn("stop_ids：应为数组", str(caught.exception))
        self.assertNotIn("private-value", str(caught.exception))
        self.assertEqual(len(self.messages), 2)

    async def test_truncation_keeps_actionable_error_without_identical_retry(self):
        self.client(iter([StructuredOutputError("模型输出被长度限制截断，请增加最大输出 token 数。")]))
        with self.assertRaises(ToolError) as caught:
            await self.models.ask(OrderChoice, "安排活动", {})
        self.assertIn("长度限制截断", str(caught.exception))
        self.assertEqual(len(self.messages), 1)

    async def test_resource_choice_without_reason_accepts_only_a_real_candidate(self):
        self.client(iter(['{"selected_id":"hotel-a"}']))
        planner = Planner.__new__(Planner)
        planner.models = self.models
        option = {"id":"hotel-a","name":"合成酒店"}
        selected, reason = await self.resource_agent().choose(self.resource_state(),[option],"选择住宿")
        self.assertEqual(selected, option)
        self.assertIn("未返回取舍说明", reason)
        self.assertEqual(len(self.messages), 1)

    async def test_omitted_reason_does_not_allow_invented_candidate_id(self):
        self.client(iter(['{"selected_id":"invented"}']))
        planner = Planner.__new__(Planner)
        planner.models = self.models
        options = [{"id":"hotel-a"}]
        selected, reason = await self.resource_agent().choose(self.resource_state(),options,"选择住宿")
        self.assertIsNone(selected)
        self.assertIn("不存在的资源", reason)
        self.assertEqual(options, [{"id":"hotel-a"}])

    async def test_missing_selection_preserves_candidates_without_aborting_planning(self):
        self.client(iter(['{"reason":"无法决定"}', '{"reason":"仍无法决定"}']))
        planner = Planner.__new__(Planner)
        planner.models = self.models
        options = [{"id":"hotel-a"}]
        selected, reason = await self.resource_agent().choose(self.resource_state(),options,"选择住宿")
        self.assertIsNone(selected)
        self.assertIn("手动选择", reason)
        self.assertIn("selected_id", reason)
        self.assertEqual(options, [{"id":"hotel-a"}])
        self.assertEqual(len(self.messages), 2)

    async def test_model_connection_failure_is_not_treated_as_an_empty_selection(self):
        self.models.ask = Mock(side_effect=ToolError("model_network", "模型服务连接失败"))
        planner = Planner.__new__(Planner)
        planner.models = self.models
        with self.assertRaises(ToolError) as caught:
            await self.resource_agent().choose(self.resource_state(),[{"id":"hotel-a"}],"选择住宿")
        self.assertEqual(caught.exception.code, "model_network")

    async def test_resource_choice_requires_explicit_selection_or_null(self):
        self.client(iter(['{}', '{"selected_id":null}']))
        result = await self.models.ask(ResourceChoice, "选择资源", {})
        self.assertIsNone(result.selected_id)
        self.assertEqual(len(self.messages), 2)
        self.assertIn("selected_id：缺少字段", self.messages[1][-1]["content"])

    async def test_poi_choice_without_reason_still_checks_membership(self):
        self.client(iter(['{"selected_id":"poi-a"}', '{"selected_id":"invented"}']))
        planner = Planner.__new__(Planner)
        planner.models = self.models
        # Not an exact-name hit, so the model must judge the candidate.
        poi = {"id":"poi-a","name":"合成景点东门","location":"120,30","cityname":"杭州"}
        reasons = []
        async def place(trip, city, name, selector):
            selected_id, reason = await selector([poi])
            reasons.append(reason)
            return poi if selected_id else None
        planner.amap = SimpleNamespace(place=place, cache={})
        state = {"trip_id":"trip"}
        stops = {"stop-a":{"city":"杭州","name":"合成景点","evidence":{"quote":"合成景点"}}}
        result = await planner.locate_stops(state, stops)
        self.assertEqual(result["stop-a"]["map_place"]["id"], "poi-a")
        self.assertIn("未返回匹配说明", reasons[-1])
        # New evidence cannot reuse the earlier match, so the invented ID reaches the membership check.
        stops = {"stop-a":{**stops["stop-a"],"evidence":{"quote":"合成景点的另一处入口"}}}
        result = await planner.locate_stops(state, stops)
        self.assertIsNone(result["stop-a"]["map_place"])
        self.assertIn("未知POI", planner.amap.cache[("place_issue","trip","杭州","合成景点")])
