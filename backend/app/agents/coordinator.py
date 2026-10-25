import copy
import logging
import re

from ..models import DecisionWording, DecisionBatch, DecisionInterpretation
from .runtime import AgentRuntime


class CoordinatorAgent(AgentRuntime):
    def __init__(self, models, store):
        super().__init__("主理人 Agent", models, store)

    def model_references(self,state,payload):
        from .route import RouteAgent
        return RouteAgent.model_references(self,state,{"available_stops":state.get("stops",{})})

    async def word(self, state, batch):
        original = batch.model_dump(mode="json")
        payload = {"questions":[{key:copy.deepcopy(question[key]) for key in
            ("id","title","question","why_needed","recommended_option_id")} | {
                "options":[{key:copy.deepcopy(option[key]) for key in ("id","label","description","impact")}
                           for option in question["options"]]} for question in original["questions"]]}
        try:
            result = await self.ask(state,DecisionWording,
                "仅润色代码生成的行程取舍题。按id逐项返回title/question/why_needed和每个选项description。"
                "题目和选项必须一一对应，不添加或删除。不得改变事实、数字、选项含义和推荐项，不提出新动作。",payload,cache=False)
            rows = result.model_dump(mode="json")["questions"]
            if len(rows) != len(original["questions"]) or {row["id"] for row in rows} != {row["id"] for row in original["questions"]}:
                raise ValueError("Question IDs do not correspond one to one")
            for question in original["questions"]:
                row = next(item for item in rows if item["id"] == question["id"])
                if len(row["options"]) != len(question["options"]) or {item["id"] for item in row["options"]} != {item["id"] for item in question["options"]}:
                    raise ValueError("Option IDs do not correspond one to one")
                numbers = lambda text: re.findall(r"\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}|\d+(?:\.\d+)?",text)
                if any(numbers(row[key]) != numbers(question[key]) for key in ("title","question","why_needed")):
                    raise ValueError("Wording changed factual numbers")
                if any(numbers(item["description"]) != numbers(next(option["description"] for option in question["options"] if option["id"] == item["id"])) for item in row["options"]):
                    raise ValueError("Option wording changed factual numbers")
                question.update({key:row[key] for key in ("title","question","why_needed")})
                for option in question["options"]:
                    option["description"] = next(item["description"] for item in row["options"] if item["id"] == option["id"])
            return DecisionBatch.model_validate(original)
        except Exception as error:
            logging.getLogger(__name__).warning("Decision wording unavailable; using code templates: %s",error)
            return batch

    async def interpret(self, state, batch, answers, note, context=None):
        return await self.ask(state, DecisionInterpretation,
            "理解用户对当前取舍批次的回答，只提出本次明确授权的动作。逐个用户权限动作在quotes中给出action_index和用户本次完整分句，保留完整表达，不截取句内片段。"
            "不要把推荐选项、系统假设、旧问题文字或你自己的推断当成用户授权。matched_option_ids只引用当前选项。"
            "任何猜测、缺失回答或相互冲突的理解写入ambiguities；summary、changes、relaxed供用户核对，不声称已执行。",
            {"batch":batch, "answers":answers, "note":note, "situation":context or {}})
