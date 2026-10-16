import logging

from ..adapters import ToolError
from ..models import GuideBrief, GuideFeedback, GuideExtraction, GuideGroup
from .runtime import AgentRuntime
from .route import resolve_stop_id
from ..dining import is_dining


class GuideAgent(AgentRuntime):
    def __init__(self, models, store):
        super().__init__("攻略 Agent",models,store)

    async def extract(self, instruction, payload, images, indices):
        return await self.models.ask(GuideExtraction,"你是攻略 Agent，只负责忠实提取所选资料，不安排真实旅行日期。"+instruction,
                                     payload,images=images,image_indices=indices)

    async def brief(self, state):
        instruction = (
            "从所选攻略景点池整理建议区域分组，不把原攻略第几天固定为旅行日期。"
            "required_ids仅包含用户明确必去的地点，excluded_ids仅包含明确不去的地点，不能把推荐当成用户决定。"
            "餐厅、咖啡店、小吃和甜品店仅作餐饮推荐，不加入景点分组或required_ids，不因分店未定位让用户反复回答。"
            "同名分店不直接合并，groups只能引用现有景点stop_ids，说明来源冲突与时长缺口。"
            "groups按城市内的建议区域整理资料，不是每天的游览安排，每组可以超过12个景点；实际每日取舍由行程 Agent 负责。"
            "每组stop_ids必须是available_stops完整ID组成的字符串数组，不能用名称、序号、对象或逗号分隔的字符串。"
            "分组city使用allowed_cities中的城市原值；不同城市的景点分别成组，以available_stops的city为准。"
            "存在会影响路线而无法由授权的普通取舍解决的用户决策时，question返回一道具体问题和影响日期；"
            "此时status=needs_decision，其他情况status=ready。"
            "尤其summary说尚未明确的用户选择必须成为question，不能只放在总结中。"
            "普通可选景点可以由行程 Agent 取舍；可查的价格、天气、开放时间不是让用户代查的问题。"
            "公交路线是否可用、场馆是否开放等事实由后续工具查证，不询问用户是否允许查询；已有条件式授权按原条件执行，不再重复确认。"
            "用餐不需要默认规划，仅可选择攻略明确推荐的餐饮地点；没有门票说明不是必需补齐的资料缺口。"
            "用户最新明确选择优先于默认规则与历史偏好。已豁免步行上限的日期不能因旧上限再次追问或削减。"
            "没有重要用户决策时question为null，直接进入协作规划，无需用户再确认总结。")
        payload = {"requirements":state["requirements"],"available_stops":state["stops"],
                   "allowed_cities":state["requirements"]["cities"],
                   "decisions":state.get("decision_history",[]),"constraints":state.get("stop_constraints",{})}
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
                    result.groups = self.normalize_groups(state,result.groups,result.excluded_ids)
                    if original_groups != [group.model_dump(mode="json") for group in result.groups]:
                        result.summary += "；建议分组已按现有攻略地点校正，实际区域顺序仍需地图核实。"
                    return GuideBrief.model_validate(result.model_dump(mode="json"))
                except ToolError as exc:
                    problem = str(exc)
            logging.getLogger(__name__).warning("Guide brief rejected: attempt=%s issues=%s",attempt+1,problem)
            if attempt:
                if invalid_constraints:
                    raise ToolError("guide_constraints","攻略 Agent 纠正后仍返回无来源或矛盾的必去/排除约束，未采用")
                # Advisory grouping is optional. Preserve decisions and constraints, replace only unsafe suggestions.
                result.groups = self.basic_groups(state,result.excluded_ids)
                result.summary = "区域建议未通过来源校验，已按所选攻略地点的所属城市做基础分组；区域顺序、日期与时段由行程 Agent 结合地图核实。"
                return GuideBrief.model_validate(result.model_dump(mode="json"))
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
