from ..adapters import ToolError
from ..models import ResourceChoice
from .runtime import AgentRuntime


class ResourceAgent(AgentRuntime):
    def __init__(self, models, store, flyai):
        super().__init__("资源 Agent",models,store)
        self.flyai = flyai

    async def search(self, state, kind, context, flags, fresh=False):
        self.assert_current(state)
        if kind not in {"train", "flight", "hotel"}:
            raise ToolError("resource_scope","资源 Agent 只能查询交通与住宿")
        if any(city not in [state["requirements"]["origin"],*state["requirements"]["cities"]]
               for key,city in context.items() if key in {"city", "origin", "destination"}):
            raise ToolError("resource_scope","资源查询超出当前旅行城市")
        try:
            result = await self.flyai.search(state["trip_id"],kind,context,flags,fresh=fresh)
        except ToolError as exc:
            identity = self.store.query(state["trip_id"],self.role,{"kind":kind,"context":context,"flags":flags},
                                        {"status":"error","code":exc.code,"message":str(exc)})
            exc.evidence_id = f"query:{identity}"
            raise
        self.assert_current(state)
        return result

    async def ticket(self, state, city, name, day_date, fresh=False):
        self.assert_current(state)
        activities = [item for day in state["itinerary"].get("days",[]) if day["date"] == day_date and day["city"] == city
                      for item in day.get("activities",[]) if item.get("kind") == "visit" and
                      name in {item.get("name"),(item.get("map_place") or {}).get("name")}]
        if not activities:
            raise ToolError("resource_scope","门票查询必须对应当前行程活动")
        aliases = list(dict.fromkeys(value for item in activities for value in
                      [item.get("name"),*(item.get("alias_names") or [])] if value and value != name))
        place = next((item["map_place"] for item in activities if item.get("map_place")),None)
        result = await self.flyai.ticket(state["trip_id"],city,name,day_date,fresh=fresh,aliases=aliases,place=place)
        self.assert_current(state)
        return result

    async def choose(self, state, options, request):
        options = [option for option in options if not option.get("constraint_issues")]
        if not options:
            return None,"未查询到满足硬性条件的资源，请查看查询缺口"
        try:
            choice = await self.ask(state,ResourceChoice,
                "只从已查询资源选择一个ID，优先用户条件，再比较费用口径、时间、接驳和住宿通勤。"
                "不安排游览、不修改日期和需求。未知费用不能当便宜，不把展示价或床型当最终总价或库存保证。"
                "必须返回selected_id，没有合适资源返回null，并用reason说明。",
                {"requirements":state["requirements"],"task":request,
                 "options":[{k:v for k,v in option.items() if k != "raw"} for option in options]})
        except ToolError as exc:
            if exc.code != "model_format":
                raise
            return None,"资源 Agent 返回未通过结构校验，备选已保留，请手动选择："+str(exc)
        selected = next((option for option in options if option["id"] == choice.selected_id),None)
        if choice.selected_id is not None and selected is None:
            return None,"模型选择了不存在的资源，未采用；备选已保留，请手动选择"
        return selected,choice.reason.strip() or "模型未返回取舍说明，需核对"
