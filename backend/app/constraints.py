"""用户条件约束：模型从原话抽取，代码校验原话、对象与日期，并按种类和范围决定覆盖关系。"""
import hashlib
import json
import logging
import re
from datetime import date as Date, timedelta

from .models import CONSTRAINT_RECORD_FIELDS, ConstraintDraft, ConstraintExtraction, SINGLE_STOP_KINDS, constraint_id


MAX_CONSTRAINTS = 60
UNQUOTED = "quote不是用户原话中的片段"

CONSTRAINT_GUIDE = (
    "constraints只记录用户原话明确提出、可归入以下种类的条件：fewer_walking少走路（用户给出每天步行距离或时长上限时填meters，时长按每分钟80米换算；没给数字留空）；"
    "fixed_order两个地点的先后（before先去、after后去；城市先后由cities顺序表达，不写入）；"
    "time_anchor某地点的日出/日落/预约时刻（anchor=sunrise/sunset/appointment，已知时刻填at）；"
    "visit_duration某地点的游玩时长（minutes）；transfer_buffer城际班次前预留缓冲（mode=train/flight/ferry，minutes）；"
    "booking_prerequisite某地点必须先完成预约才安排；transit_rule市内交通条件（public_only只用公共交通，auto_by_transit按真实公交查询结果自动取舍）；"
    "daily_min_stops每天至少安排几个景点（count；用户之后接受某些日期少于这个数时，写同种类、scope.dates为其余日期的新条件）。"
    "scope.dates、scope.stop_ids、scope.cities限定生效范围，用户没有限定就留空表示全程；"
    "time_anchor、visit_duration、booking_prerequisite的scope.stop_ids恰好一个地点；地点ID只能使用available_stops中的完整键。"
    "quote必须逐字摘自用户原话。用户取消此前条件时，以同种类、同对象填写revoke=true。"
    "兴趣、节奏、喜好等无法归入上述种类的内容不写入constraints，没有就返回[]。"
    "已记录的条件见requirements.constraints（superseded_by非空的已失效），只返回本次新增或取消的条件；"
    "输入顶层的constraints是必去/排除标记，与此不同。")


def digest(value):
    # Local copy of agents.runtime.digest: importing the agents package here would cycle through reviewing.
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def key(item):
    """Constraints with the same key and overlapping scope describe the same condition; the newer one wins."""
    kind = item["kind"]
    if kind == "fixed_order":
        return kind,tuple(sorted((item["before"],item["after"])))
    if kind in SINGLE_STOP_KINDS:
        return kind,tuple(item["scope"]["stop_ids"])
    if kind == "transfer_buffer":
        return kind,item["mode"]
    return kind,


def _overlap(left, right):
    return not left or not right or bool(set(map(str,left)) & set(map(str,right)))


def overlaps(a, b):
    return all(_overlap(a["scope"].get(field,[]),b["scope"].get(field,[])) for field in ("dates","stop_ids","cities"))


def _compact(text):
    return re.sub(r"\s+","",text or "")


def check(drafts, text, req, stops):
    """Return (valid, issues): quotes must come from the user's text, stops and dates from this trip."""
    valid, issues = [], []
    source = _compact(text)
    dates = (str(req.get("start_date") or ""),str(req.get("end_date") or ""))
    for index,draft in enumerate(drafts):
        item = draft.model_dump(mode="json") if isinstance(draft,ConstraintDraft) else draft
        problems = []
        if _compact(item.get("quote")) not in source:
            problems.append(UNQUOTED)
        unknown = [identity for identity in item["scope"]["stop_ids"] if identity not in stops]
        if unknown:
            problems.append("未知地点ID："+"、".join(unknown[:5]))
        outside = [day for day in item["scope"]["dates"] if all(dates) and not dates[0] <= day <= dates[1]]
        if outside:
            problems.append("日期不在旅行范围内："+"、".join(outside[:5]))
        strange = [city for city in item["scope"]["cities"] if city not in req.get("cities",[])]
        if strange:
            problems.append("城市不在目的地中："+"、".join(strange[:5]))
        if problems:
            issues.append({"index":index,"kind":item["kind"],"problems":problems})
        else:
            valid.append(item)
    return valid,issues


def trip_dates(req):
    if not req.get("start_date") or not req.get("end_date"):
        return []
    start,end = Date.fromisoformat(str(req["start_date"])),Date.fromisoformat(str(req["end_date"]))
    return [(start+timedelta(days=offset)).isoformat() for offset in range((end-start).days+1)]


def merge(existing, drafts, source, round_id="", *, trip_dates=()):
    """Append validated drafts; an equal active constraint is kept once, an overlapping older one is superseded."""
    result = [dict(item) for item in existing]
    for draft in drafts:
        item = {key_:value for key_,value in draft.items() if key_ not in CONSTRAINT_RECORD_FIELDS-{"quote"}}
        identity = constraint_id(item)
        live = [entry for entry in result if not entry.get("superseded_by")]
        if any(entry["id"] == identity for entry in live):
            continue
        for entry in live:
            if key(entry) == key(item) and overlaps(entry,item):
                entry["superseded_by"] = identity
                if item["scope"]["dates"]:
                    remaining = sorted(set(entry["scope"]["dates"] or trip_dates)-set(item["scope"]["dates"]))
                    if remaining:
                        rest = {**entry,"scope":{**entry["scope"],"dates":remaining},"superseded_by":""}
                        result.append({**rest,"id":constraint_id(rest)})
        result.append({**item,"id":identity,"source":source,"round_id":round_id,"superseded_by":""})
    # Stay within Requirements.constraints max_length by forgetting the oldest superseded entries first.
    while len(result) > MAX_CONSTRAINTS and any(entry["superseded_by"] for entry in result):
        result.remove(next(entry for entry in result if entry["superseded_by"]))
    return result


def drop_source(existing, source):
    """Remove one source's constraints and reinstate whatever only they had superseded."""
    removed = {item["id"] for item in existing if item["source"] == source}
    return [{**item,"superseded_by":"" if item.get("superseded_by") in removed else item.get("superseded_by","")}
            for item in existing if item["id"] not in removed]


def active(req, kind, *, date=None, stop_id=None):
    """Active constraints of one kind that apply on a date / to a stop; None means do not filter by it."""
    return [item for item in req.get("constraints",[])
            if item["kind"] == kind and not item.get("superseded_by") and not item.get("revoke")
            and (date is None or not item["scope"]["dates"] or str(date) in item["scope"]["dates"])
            and (stop_id is None or not item["scope"]["stop_ids"] or stop_id in item["scope"]["stop_ids"])]


# 「少走路」没有给出数字时的每日步行目标（约一小时步行），超过只提示、不阻断。
FEWER_WALKING_METERS = 5000


def walking_target(req, date=None):
    """Daily target from a dated decision or soft fewer_walking constraints."""
    override = dated_walking_limit(req, date)
    if override is not None:
        return override
    targets = [item["meters"] if item.get("meters") is not None else FEWER_WALKING_METERS for item in active(req,"fewer_walking",date=date)
               if item.get("meters") is None or item["strength"] == "soft"]
    return min(targets) if targets else None


def dated_walking_limit(req, date=None):
    return next((item["meters"] for item in reversed(active(req,"fewer_walking",date=date))
                 if date is not None and item.get("source") == "decision" and item["scope"]["dates"]
                 and not item["scope"].get("stop_ids") and not item["scope"].get("cities")
                 and item.get("meters") is not None and item["strength"] == "hard"), None)


def public_only(req):
    """A trip-wide "public transport only" rule; date- or stop-scoped rules are checked on the plan instead."""
    return any(item["rule"] == "public_only" and not item["scope"]["dates"] and not item["scope"]["stop_ids"]
               for item in active(req,"transit_rule"))


def station_buffer(req, kind, date=None):
    """Pre-departure buffer for a train or flight, raised to any stricter user transfer_buffer."""
    default = 120 if kind == "flight" else 60
    return max([default,*(item["minutes"] for item in active(req,"transfer_buffer",date=date) if item["mode"] == kind)])


def stop_catalog(stops):
    return {identity:{"name":stop.get("name"),"city":stop.get("city")} for identity,stop in stops.items()}


def preferences_basis(text):
    return digest(text.strip())[:16] if text.strip() else ""


async def refresh_form_constraints(models, req, stops):
    """Re-extract form constraints only when the preferences text changed since the last extraction."""
    text = req.get("preferences","")
    basis = preferences_basis(text)
    if req.get("constraints_basis","") == basis:
        return req
    constraints = drop_source(req.get("constraints",[]),"form")
    updates = {}
    if basis:
        instruction = ("从用户填写的旅行偏好原文中抽取可执行的条件约束。"+CONSTRAINT_GUIDE+
                       "allow_taxi只在原文明确不打车或明确允许打车时填写。")
        payload = {"preferences":text,"cities":req.get("cities",[]),"start_date":req.get("start_date"),
                   "end_date":req.get("end_date"),"available_stops":stop_catalog(stops)}
        valid = []
        for attempt in range(2):
            extraction = await models.ask(ConstraintExtraction,instruction,payload)
            valid,issues = check(extraction.constraints,text,req,stops)
            if not issues:
                break
            logging.getLogger(__name__).warning("Form constraints rejected: attempt=%s issues=%s",attempt+1,issues)
            payload = {**payload,"previous_constraints":[item.model_dump(mode="json") for item in extraction.constraints],
                       "validation_errors":issues}
        if not any(item["kind"] == "daily_min_stops" for item in valid):
            for match in re.finditer(r"每[天日](?:都)?(?:至少|最少|起码)(?:要)?(?:安排|去|逛|玩|看)?\s*([0-9]+|[一二两三四五六七八九十]+)\s*个(?:景点|地方|地点)",text):
                if re.search(r"不|不用|不要|无需|不必",text[max(0,match.start()-4):match.start()]):
                    continue
                value = match.group(1)
                count = int(value) if value.isascii() and value.isdigit() else {"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}.get(value,0)
                if not 1 <= count <= 12:
                    continue
                fallback,issues = check([ConstraintDraft(kind="daily_min_stops",count=count,quote=match.group())],text,req,stops)
                if fallback:
                    valid += fallback
                    logging.getLogger(__name__).warning("Form daily_min_stops added by deterministic fallback")
                    break
        constraints = merge(constraints,valid,"form",trip_dates=trip_dates(req))
        if extraction.allow_taxi is False:
            updates["allow_taxi"] = False
    return {**req,**updates,"constraints":constraints,"constraints_basis":basis}
