import hashlib
import json

from ..adapters import ToolError
from ..store import Conflict, now
from ..views import model_view
from ..assumptions import ASSUMPTION_NOTE, agent_payload as context_payload, visible_text


def agent_payload(state, payload):
    return context_payload(state,payload)


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def replace_references(value, mapping):
    if isinstance(value,str):
        return mapping.get(value,value)
    if isinstance(value,list):
        return [replace_references(item,mapping) for item in value]
    if isinstance(value,dict):
        result = {}
        for key,item in value.items():
            key,item = mapping.get(key,key),replace_references(item,mapping)
            if key in result and result[key] != item:
                raise ToolError("route_reference","同一景点的引用字段重复且值冲突，未覆盖原值；请只返回一份时长或开始时刻")
            result[key] = item
        return result
    return value


class AgentRuntime:
    def __init__(self, role, models, store):
        self.role, self.models, self.store = role, models, store

    def assert_current(self, state):
        trip = self.store.trip(state["trip_id"])
        if trip["active_run"] != state["run_id"] or self.store.run(state["run_id"])["status"] != "running":
            raise Conflict("规划任务已变化，旧 Agent 结果未采用")

    async def ask(self, state, schema, instruction, payload, tools=None, cache=True, completion_schema=None):
        self.assert_current(state)
        payload = agent_payload(state,payload)
        if ASSUMPTION_NOTE not in instruction:
            instruction += ASSUMPTION_NOTE
        if self.role in {"行程 Agent","主理人 Agent"}:
            instruction += "景点摘要的coordinates为经纬度；opening给出open/closed/unknown、分钟时段和停止入场，default为常规结果，exceptions只覆盖列出的日期。未知开放时间保留未核实提示，具体排程由代码负责。必去或想去全程任一天安排即满足。"
        config_store = getattr(self.models,"config_store",None)
        config = config_store.snapshot().public() if config_store else {}
        references = self.model_references(state,payload)
        outward = {identity:alias for alias,identity in references.items()}
        key = digest({"runtime_version":4 if references else 3,"role":self.role,"model":config,"schema":schema.model_json_schema(),"instruction":instruction,
                      "payload":payload,"generation":state["generation"]})
        cached = self.store.agent_result(state["trip_id"],key) if cache else None
        if cached:
            result = schema.model_validate(visible_text(cached["result"],state))
            if self.result_cacheable(state,payload,result):
                for observation in cached.get("observations",[]):
                    self.apply_tool_observation(state,payload,observation)
                return result
        observations, seen = [],{}
        finish = False
        for turn in range(4):
            final_turn = tools is not None and (finish or turn == 3)
            closing = ("本轮是收尾轮，禁止再请求工具，tools必须为空，status只能是ready。"
                       "直接利用已有依据完成当前任务；查不到的事实保留明确缺口，不编造耗时、位置或价格，"
                       "资料缺口本身不要求用户代查。不得删除必去、锁定项目或扩大所选攻略范围。") if final_turn else ""
            response_schema = completion_schema if final_turn and completion_schema else schema
            model_payload = {**payload,"tool_observations":observations,"remaining_tool_rounds":0 if final_turn else 3-turn}
            reference_note = ("本次available_stops的键是唯一允许的景点引用；内部长ID已换成stop_短编号。stop_ids、feasibility.durations/priorities的键及工具origin_id/destination_id必须直接复制这些键；"
                              "不要生成、还原或抄写内部长ID，也不要只复制错误说明里的未知ID。程序会还原真实攻略引用。") if references else ""
            result = await self.models.ask(response_schema,f"你是{self.role}。"+instruction+closing+reference_note,
                                           model_view(self.role,replace_references(state,outward),replace_references(model_payload,outward)))
            if references:
                restored = replace_references(result.model_dump(mode="json"),references)
                result = response_schema.model_validate(restored)
            self.assert_current(state)
            requests = getattr(result,"tools",[]) if getattr(result,"status",None) == "tools" else []
            if not requests:
                actual = result.model_dump(mode="json")
                cleaned = visible_text(actual,state)
                if cleaned != actual:
                    result = type(result).model_validate(cleaned)
                if cache and self.result_cacheable(state,payload,result):
                    self.store.save_agent_result(state["trip_id"],key,{"result":result.model_dump(mode="json"),"observations":observations})
                self.store.event(state["run_id"],"agent",f"{self.role}已交接结果",agent=self.role,
                                 evidence_ids=[item["evidence_id"] for item in observations])
                return result
            if not tools or final_turn:
                raise ToolError("agent_budget","路线 Agent 的工具轮次已用尽，已有草稿与依据已保留，可继续任务")
            fresh = False
            for request in requests:
                self.assert_current(state)
                request = self.prepare_tool_request(state,payload,request)
                args = request.model_dump(mode="json")
                tool_key = self.tool_key(state,payload,args,config)
                if tool_key in seen:
                    continue
                fresh = True
                saved = self.store.agent_result(state["trip_id"],tool_key)
                if saved:
                    observation = saved["observation"]
                else:
                    try:
                        value = await tools(request)
                        status = "unresolved" if isinstance(value,dict) and value.get("query_status") == "unresolved" else "ok"
                    except ToolError as exc:
                        value, status = {"code":exc.code,"message":str(exc)},"error"
                    self.assert_current(state)
                    evidence = self.store.query(state["trip_id"],self.role,args,{"status":status,"result":value,"queried_at":now()})
                    observation = {"evidence_id":f"query:{evidence}","request":args,"status":status,"result":value}
                    if status == "ok":
                        self.store.save_agent_result(state["trip_id"],tool_key,{"observation":observation})
                seen[tool_key] = observation
                observations.append(observation)
                self.apply_tool_observation(state,payload,observation)
            # Repeating a successful or failed query cannot add evidence; use the next call to finish.
            finish = not fresh
        raise ToolError("agent_budget","协作规划未完成，草稿已保留")

    def prepare_tool_request(self, state, payload, request):
        return request

    def model_references(self, state, payload):
        return {}

    def result_cacheable(self, state, payload, result):
        return True

    def apply_tool_observation(self, state, payload, observation):
        pass

    def tool_key(self, state, payload, args, config):
        kind = args.get("tool")
        fields = {"place":("tool","city","destination_id"),"route":("tool","city","origin_id","destination_id","mode"),
                  "weather":("tool","city"),"guide_feedback":("tool","request")}.get(kind)
        semantic = {field:args.get(field) for field in fields} if fields else args
        stops = payload.get("available_stops",state.get("stops",{}))
        if kind in {"place","route"}:
            identities = (args.get("origin_id"),args.get("destination_id")) if kind == "route" else (args.get("destination_id"),)
            stops = {identity:stops.get(identity) for identity in identities if identity} if isinstance(stops,dict) else {}
            if kind == "place":
                stops = {identity:{field:stop.get(field) for field in ("city","name","evidence","source_references","alias_names")}
                         if isinstance(stop,dict) else stop for identity,stop in stops.items()}
        elif kind == "weather":
            stops = {}
        return "agent_tool:"+digest({"version":2,"role":self.role,"run":state["run_id"],"generation":state["generation"],
            "model":config,"request":semantic,"requirements":state.get("requirements",{}),"stops":stops,
            "constraints":state.get("stop_constraints",{})})
