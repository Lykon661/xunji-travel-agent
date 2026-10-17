"""景点高德定位、同名地点澄清与天气上下文。"""
import asyncio
import copy

from ..adapters import ToolError
from ..models import PlanningDecision, ResourceChoice, PlaceChoices
from ..routing import coordinates
from ..agents.runtime import digest
from ..dining import is_dining
from ..places import canonicalize_stops


class LocationNodes:
    @staticmethod
    def place_clarification(stops,identities,days=()):
        for identity in sorted(identities):
            stop = stops.get(identity,{})
            candidates = list({item["id"]:item for item in stop.get("map_candidates",[]) if item.get("id")}.values())
            if coordinates(stop.get("map_place")) or stop.get("map_selected_id") or len(candidates) < 2:
                continue
            options = {f"{index+1}. {item['name']}（{item.get('address') or '地址未返回'}）":item["id"] for index,item in enumerate(candidates[:9])}
            question_id = "place-"+digest([identity,candidates])[:24]
            dates = [day["date"] for day in days if identity in day.get("stop_ids",[])]
            question = PlanningDecision(id=question_id,title="请选择具体地点",
                question="“"+stop["name"]+"”返回了多个高德候选，现有资料还不能唯一对应。你指的是哪一个？",
                options=[*options,"以上都不是，我补充地点线索"],recommendation="按你要去的场馆、分店或地址选择；也可补充名称、地址等线索。",
                why_needed=(stop.get("map_issue") or "候选地点存在歧义")[:1500],affected_dates=dates).model_dump(mode="json")
            return {"decision":question,"place_selection":{"question_id":question_id,"stop_id":identity,"options":options}}
        return None

    async def locate_stops(self,state,stops):
        limit = asyncio.Semaphore(3)
        # Keep verified AMap POIs through reassessment instead of rematching their guide aliases.
        saved = {}
        for plan in (state.get("old_itinerary",{}),state.get("selected_route",{}).get("draft",{}),state.get("itinerary",{})):
            for day in plan.get("days",[]):
                for item in day.get("activities",[]):
                    place = item.get("map_place") or {}
                    if coordinates(place) and str(place.get("evidence_id","")).startswith("query:"):
                        saved[(item["id"],day["city"],item["name"])] = place
        preserved,confirmed_choices = {},{}
        preferences = state.get("requirements",{}).get("preferences","")
        for identity,stop in stops.items():
            confirmed = next((entry["place_selection"] for entry in reversed(state.get("decision_history",[]))
                if entry.get("place_selection",{}).get("stop_id") == identity),{})
            if ((confirmed.get("city"),confirmed.get("name")) == (stop["city"],stop["name"])
                    and confirmed.get("preferences","") == preferences and confirmed.get("poi_id")):
                confirmed_choices[identity] = confirmed["poi_id"]
            place = stop.get("map_place") or {}
            if not (coordinates(place) and str(place.get("evidence_id","")).startswith("query:")):
                place = saved.get((identity,stop["city"],stop["name"]))
            selected = confirmed_choices.get(identity) or stop.get("map_selected_id")
            if selected and (place or {}).get("id") != selected:
                place = None
            if coordinates(place):
                preserved[identity] = copy.deepcopy(place)
        keys = [identity for identity in stops if identity not in preserved]
        candidate_sets,selection_results,failures = {},{},{}
        lookup_locks = {(stops[identity]["city"],stops[identity]["name"]):asyncio.Lock() for identity in keys}
        config_store = getattr(self.models,"config_store",None)
        config = config_store.snapshot().public() if config_store else {}
        place_instruction = ("将攻略地点匹配到高德实际POI，只能选pois中的id。可识别明确简称、城市前缀、同一景区别名；"
            "餐厅/酒店分店必须符合攻略提到的地址或总店等限定。多个同名地点没有足够依据时selected_id=null，不随意选第一个。"
            "用户已明确选择场馆展区时按该选择匹配高德POI；飞猪坐标不参与定位，也不能据其与高德坐标不同拒绝匹配。"
            "preferences是已确认的用户需求，优先使用其中明确选择的展区或地址；原攻略未写展区不代表用户后来选择无效。"
            "综合完整攻略引用、用户最新需求与已回答选择，区分目的地本体、分店、入口、售票处和停车场。"
            "多条候选本身不是查不到：证据足以唯一对应时选中实际POI，并引用具体匹配依据；仍无法区分才返回null，说明欠缺的区分信息。"
            "不得生成坐标或新ID；必须返回selected_id；reason说明匹配依据或歧义。")
        deferred = {}

        def selection_payload(identity,candidates):
            stop = stops[identity]
            return {"stop_id":identity,"city":stop["city"],"name":stop["name"],"evidence":[stop.get("evidence",{})],
                    "preferences":preferences,"decisions":state.get("decision_history",[]),
                    "source_references":stop.get("source_references",[]),"alias_names":stop.get("alias_names",[]),
                    "pois":candidates}

        def selection_keys(payload):
            fingerprint = digest([payload,config])
            # Unresolved choices stay within one run; a confirmed match on identical inputs is reused across runs.
            return ("place_selection",state["trip_id"],state.get("run_id"),fingerprint),("place_selection_matched",state["trip_id"],fingerprint)

        def record(identity,payload,selected_id,reason):
            reason = reason.strip() or ("模型匹配了候选POI，未返回匹配说明" if selected_id else "模型未匹配地点，未返回原因说明")
            if selected_id is not None and selected_id not in {row.get("id") for row in payload["pois"]}:
                raise ToolError("place_choice","地点匹配引用了未知POI，未采用")
            run_key,matched_key = selection_keys(payload)
            self.amap.cache[run_key] = (selected_id,reason)
            if selected_id is not None:
                self.amap.cache[matched_key] = (selected_id,reason)
            selection_results[identity] = (selected_id,reason)

        async def first_pass(identity,candidates):
            stop = stops[identity]
            candidate_sets[identity] = [{field:row.get(field) for field in ("id","name","address","cityname","adname","type","typecode","location")} for row in candidates]
            if identity in confirmed_choices:
                selected = confirmed_choices[identity] if any(row.get("id") == confirmed_choices[identity] for row in candidates) else None
                reason = "用户已明确选择此高德POI" if selected else "用户已选的地点不在本次候选中，需要重新核对"
                selection_results[identity] = (selected,reason)
                return selected,reason
            # Restaurant branches need address evidence; a sight with one exact-name POI needs no model guess.
            exact = [row for row in candidates if row.get("name") == stop["name"] and row.get("id")]
            if len(exact) == 1 and not is_dining(stop):
                selection_results[identity] = (exact[0]["id"],"高德候选中仅此一处与攻略地点名称完全一致")
                return selection_results[identity]
            payload = selection_payload(identity,candidate_sets[identity])
            for cache_key in selection_keys(payload):
                if cache_key in self.amap.cache:
                    selection_results[identity] = self.amap.cache[cache_key]
                    return selection_results[identity]
            deferred[identity] = payload
            return None,"等待地点匹配判断"

        async def lookup(identity,selector):
            async with limit:
                stop = stops[identity]
                key = (stop["city"],stop["name"])
                try:
                    async with lookup_locks[key]:
                        place = await self.amap.place(state["trip_id"],*key,selector=selector)
                    if identity in confirmed_choices and place and place.get("id") != confirmed_choices[identity]:
                        failures[identity] = "高德本次结果不是你已选的POI，未擅自替换"
                        place = None
                    if not place:
                        failures.setdefault(identity,str(self.amap.cache.get(("place_issue",state["trip_id"],*key)) or "高德尚未返回唯一匹配的地点与可用坐标"))
                    return {k:place.get(k) for k in ("id","name","location","address","cityname","adname","business_area","type","evidence_id","opentime2","open_time","match_basis")} if place and coordinates(place) else None
                except ToolError as exc:
                    fail(identity,exc)
                    return None

        def fail(identity,exc):
            failures[identity] = str(exc)
            self.amap.cache[("place_issue",state["trip_id"],stops[identity]["city"],stops[identity]["name"])] = str(exc)

        async def decide(group):
            if len(group) == 1:
                payload = deferred[group[0]]
                choice = await self.models.ask(ResourceChoice,place_instruction+
                    '输出形如 {"selected_id":null,"reason":"存在同名歧义"}；确认匹配时将null替换为pois中的ID，不返回POI原对象。',payload)
                answers = {group[0]:choice}
            else:
                # Ambiguous places of one city share one judgment; short keys avoid copying long guide IDs.
                aliases = {f"p{index}":identity for index,identity in enumerate(group,1)}
                result = await self.models.ask(PlaceChoices,place_instruction+
                    "places中每个键是一处独立的攻略地点，只能从该地点自己的pois中选择；每个键恰好返回一项，key原样复制。"
                    '输出形如 {"choices":[{"key":"p1","selected_id":null,"reason":"存在同名歧义"}]}；确认匹配时将null替换为该地点pois中的ID。',
                    {"places":{alias:{**deferred[identity],"stop_id":alias} for alias,identity in aliases.items()}})
                answers = {}
                for choice in result.choices:
                    if choice.key in aliases:
                        answers.setdefault(aliases[choice.key],choice)
            for identity in group:
                choice = answers.get(identity)
                try:
                    if choice is None:
                        raise ToolError("place_choice","地点匹配未返回该地点的判断，未采用")
                    record(identity,deferred[identity],choice.selected_id,choice.reason)
                except ToolError as exc:
                    fail(identity,exc)

        async def judge(batch):
            try:
                await decide(batch)
            except ToolError as exc:
                # Model failures are not selection decisions and are never cached.
                for identity in batch:
                    fail(identity,exc)

        async def replay(identity):
            failures.pop(identity,None)
            async def chosen(candidates):
                return selection_results[identity]
            return await lookup(identity,chosen)

        async def search(identity):
            async def selector(candidates):
                return await first_pass(identity,candidates)
            return await lookup(identity,selector)

        places = dict(zip(keys,await asyncio.gather(*(search(identity) for identity in keys))))
        if deferred:
            groups = {}
            for identity in deferred:
                groups.setdefault(stops[identity]["city"],[]).append(identity)
            await asyncio.gather(*(judge(members[start:start+8]) for members in groups.values() for start in range(0,len(members),8)))
            decided = [identity for identity in deferred if identity in selection_results]
            places.update(zip(decided,await asyncio.gather(*(replay(identity) for identity in decided))))
        result = {identity:{**stop,"map_place":preserved.get(identity) or places.get(identity)} for identity,stop in stops.items()}
        for identity,stop in result.items():
            if identity in candidate_sets:
                stop["map_candidates"] = candidate_sets[identity]
                stop["map_selected_id"] = selection_results.get(identity,(None,""))[0]
            if coordinates(stop.get("map_place")):
                stop.pop("map_issue",None)
                stop["query_status"] = "resolved"
            else:
                stop["map_issue"] = failures.get(identity,"高德尚未返回唯一匹配的地点与可用坐标")
                stop["query_status"] = "unresolved"
        return canonicalize_stops(result)

    async def weather_context(self,state):
        result = {}
        for city in state["requirements"]["cities"]:
            try:
                result[city] = await self.amap.weather(state["trip_id"],city)
            except ToolError as exc:
                result[city] = {"forecasts":{},"note":str(exc)}
        return result

    @staticmethod
    def daily_weather(weather,city,day_date):
        city_weather = weather.get(city,{})
        forecast = city_weather.get("forecasts",{}).get(day_date)
        if not forecast:
            return {"date":day_date,"available":False,"note":city_weather.get("note") or "当前预报未覆盖此日期，临近出行需再查询"}
        weather_text = str(forecast.get("dayweather","")) + str(forecast.get("nightweather",""))
        return {**forecast,"available":True,"provider":"amap","queried_at":city_weather.get("queried_at"),
                "reporttime":city_weather.get("reporttime"),"evidence_id":city_weather.get("evidence_id"),
                "note":"有雨雪或恶劣天气，优先室内活动；户外安排需临近出行确认" if any(word in weather_text for word in ("雨","雪","雷","暴","沙尘")) else "预报可能变化，出行前请再确认"}
