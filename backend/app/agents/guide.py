import logging

from ..adapters import ToolError
from ..models import GuideBrief, GuideFeedback, GuideExtraction, GuideGroup
from .runtime import AgentRuntime, agent_payload, digest
from .route import resolve_stop_id
from ..dining import is_dining
from ..assumptions import ASSUMPTION_NOTE


class GuideAgent(AgentRuntime):
    def __init__(self, models, store):
        super().__init__("攻略 Agent",models,store)

    async def extract(self, instruction, payload, images, indices):
        return await self.models.ask(GuideExtraction,"你是攻略 Agent，只负责忠实提取所选资料，不安排真实旅行日期。"+instruction,
                                     payload,images=images,image_indices=indices)

    def model_references(self, state, payload):
        stops = payload.get("available_stops",{})
        aliases = {}
        occupied = set(stops) | {stop.get("name") for stop in stops.values() if isinstance(stop,dict)}
        for identity in sorted(stops):
            if len(identity) <= 24:
                continue
            alias = f"stop_{len(aliases)+1}"
            while alias in occupied:
                alias = "_"+alias
            occupied.add(alias)
            aliases[alias] = identity
        return aliases

    @staticmethod
    def brief_payload(state):
        fields = ("id","day","city","name","duration_minutes","opening_claim","ticket_claim","category",
                  "evidence","source_id","source_title","source_kind","assigned_date","alias_names")
        stops = {identity:{key:stop[key] for key in fields if key in stop} for identity,stop in state["stops"].items()}
        return agent_payload(state,{"requirements":state["requirements"],"available_stops":stops,
                   "allowed_cities":state["requirements"]["cities"],
                   "decisions":state.get("decision_history",[]),"constraints":state.get("stop_constraints",{})})

    async def brief(self, state):
        instruction = (
            ASSUMPTION_NOTE+"从所选攻略景点池整理建议区域分组，不把原攻略第几天固定为旅行日期。"
            "required_ids仅包含用户明确必去的地点，excluded_ids仅包含明确不去的地点，不能把推荐当成用户决定。"
            "普通攻略餐饮仅作推荐，不加入景点分组或required_ids；source_kind=user的咖啡甜品饮品属于必去游览，默认45分钟，须参与分组与required_ids；用户正餐餐厅另按必去用餐处理，不因分店未定位让用户反复回答。"
            "同名分店不直接合并，groups只能引用现有景点stop_ids，说明来源冲突与时长缺口。"
            "groups按城市内的建议区域整理资料，不是每天的游览安排，每组可以超过12个景点；行程 Agent 选择整趟景点，实际分天与排程由代码负责。"
            "每组stop_ids必须是available_stops完整ID组成的字符串数组，不能用名称、序号、对象或逗号分隔的字符串。"
            "分组city使用allowed_cities中的城市原值；不同城市的景点分别成组，以available_stops的city为准。"
            "遇到未满足条件或身份歧义时写入结构化unmet，引用现有stop/requirement/constraint及日期。"
            "不向用户提问；资料缺口说明只用于展示，不能据此改写用户要求。")
        payload = self.brief_payload(state)
        config_store = getattr(self.models,"config_store",None)
        key = "guide_brief:v1:"+digest({"payload":payload,"instruction":instruction,
            "model":config_store.snapshot().public() if config_store else {},
            "schema":GuideBrief.model_json_schema(),"source_ids":state.get("selected_source_ids",[]),
            "source_versions":state.get("sources_snapshot",[])})
        self.assert_current(state)
        cached = self.store.agent_result(state["trip_id"],key)
        if isinstance(cached,dict):
            try:
                result = GuideBrief.model_validate(cached["result"])
                if not set(result.required_ids+result.excluded_ids) <= set(state["stops"]) or set(result.required_ids) & set(result.excluded_ids):
                    raise ValueError("cached guide constraints changed")
                result.groups = self.normalize_groups(state,result.groups)
                self.assert_current(state)
                logging.getLogger(__name__).info("Reused validated GuideBrief for trip=%s",state["trip_id"])
                return result
            except (KeyError,ValueError,ToolError):
                pass
        def validated(result):
            result = GuideBrief.model_validate(result.model_dump(mode="json"))
            self.assert_current(state)
            self.store.save_agent_result(state["trip_id"],key,{"result":result.model_dump(mode="json")})
            return result
        for attempt in range(2):
            # Runtime schema validation alone cannot establish source membership; do not cache unvalidated briefs.
            result = await self.ask(state,GuideBrief,instruction,payload,cache=False)
            result.required_ids = list(dict.fromkeys(resolve_stop_id(identity,state["stops"]) or identity.strip() for identity in result.required_ids))
            result.excluded_ids = list(dict.fromkeys(resolve_stop_id(identity,state["stops"]) or identity.strip() for identity in result.excluded_ids))
            invalid_constraints = (not set(result.required_ids+result.excluded_ids) <= set(state["stops"]) or
                                   bool(set(result.required_ids) & set(result.excluded_ids)))
            if invalid_constraints:
                problem = "required_ids/excluded_ids只能引用available_stops的完整ID，二者不能重叠"
            else:
                result.required_ids = [identity for identity in result.required_ids if not is_dining(state["stops"][identity])]
                try:
                    original_groups = [group.model_dump(mode="json") for group in result.groups]
                    result.groups = self.normalize_groups(state,result.groups)
                    if original_groups != [group.model_dump(mode="json") for group in result.groups]:
                        result.summary += "；建议分组已按现有攻略地点校正，实际区域顺序仍需地图核实。"
                    return validated(result)
                except ToolError as exc:
                    problem = str(exc)
            logging.getLogger(__name__).warning("Guide brief rejected: attempt=%s issues=%s",attempt+1,problem)
            if attempt:
                if invalid_constraints:
                    raise ToolError("guide_constraints","攻略 Agent 纠正后仍返回无来源或矛盾的必去/排除约束，未采用")
                # Advisory grouping is optional. Preserve decisions and constraints, replace only unsafe suggestions.
                result.groups = self.basic_groups(state)
                result.summary = "区域建议未通过来源校验，已按所选攻略地点的所属城市做基础分组；分天、顺序与时段由代码结合地图安排。"
                return validated(result)
            payload = {**payload,"previous_brief":result.model_dump(mode="json"),"validation_error":problem}

    @staticmethod
    def normalize_groups(state, groups, excluded=()):
        normalized, seen = [],set()
        excluded = set(excluded) | set(state.get("stop_constraints",{}).get("excluded",[])) | set(state["requirements"].get("excluded_stop_ids",[]))
        for index,group in enumerate(groups):
            city = group.city if group.city in state["requirements"]["cities"] else None
            identities = list(dict.fromkeys(identity if identity in state["stops"] else
                resolve_stop_id(identity,state["stops"],city) or identity.strip() for identity in group.stop_ids))
            unknown = [identity for identity in identities if identity not in state["stops"]]
            if unknown:
                raise ToolError("guide_groups",f"groups.{index}.stop_ids含{len(unknown)}个未知ID；只能使用available_stops的完整键，不能用名称、POI ID或canonical_place_id")
            by_city = {}
            for identity in identities:
                stop = state["stops"][identity]
                if identity in excluded or is_dining(stop):
                    continue
                city = stop.get("city")
                if city not in state["requirements"]["cities"]:
                    raise ToolError("guide_groups",f"groups.{index}.stop_ids含不属于本次目的地城市的地点")
                if identity not in seen:
                    by_city.setdefault(city,[]).append(identity)
                    seen.add(identity)
            for city,ids in by_city.items():
                reason = group.reason if group.city == city and len(by_city) == 1 else "按所选攻略地点的实际所属城市校正分组；原区域建议仍需地图核实"
                normalized.append(GuideGroup(city=city,stop_ids=ids,reason=reason))
        if len(normalized) > 30:
            by_city = {}
            for group in normalized:
                by_city.setdefault(group.city,[]).extend(group.stop_ids)
            normalized = [GuideGroup(city=city,stop_ids=ids,reason="跨城市建议已合并为基础资料分组，不代表同日游览") for city,ids in by_city.items()]
        return normalized

    @staticmethod
    def basic_groups(state, excluded=()):
        excluded = set(excluded) | set(state.get("stop_constraints",{}).get("excluded",[])) | set(state["requirements"].get("excluded_stop_ids",[]))
        result = []
        for city in state["requirements"]["cities"]:
            ids = [identity for identity,stop in state["stops"].items() if stop.get("city") == city and identity not in excluded and not is_dining(stop)]
            if ids:
                result.append(GuideGroup(city=city,stop_ids=ids,reason="按所选攻略的城市作基础资料分组；不代表推荐同日游览或已查证地理邻近"))
        return result

    async def feedback(self, state, request):
        return await self.ask(state,GuideFeedback,
            "根据行程 Agent 的反馈，从现有已选攻略景点池提供替代或补充景点。只返回available_stops中的景点ID，餐厅和咖啡店不能作为替代景点，"
            "尊重必去和排除项，不新增地点，不虚构时长、地理关系或价格。解释为何可用；没有替代则返回空列表。",
            {"request":request,"requirements":state["requirements"],"available_stops":state["stops"],
             "constraints":state.get("stop_constraints",{})})
