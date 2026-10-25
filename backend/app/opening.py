import hashlib
import logging
import re
from datetime import date, timedelta
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class OpeningRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date_ranges: list[tuple[str, str]] = Field(default_factory=list)
    months: list[int] = Field(default_factory=list)
    weekdays: list[int] = Field(default_factory=list)
    closed: bool = False
    windows: list[tuple[int, int]] = Field(default_factory=list)
    last_entry: int | None = None


class OpeningSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[OpeningRule] = Field(default_factory=list)
    holiday_note: str = ""
    unknown: bool = False


class OpeningResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    schedule: OpeningSchedule


class OpeningBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedules: list[OpeningResult]


TIME = r"(?<!\d)(\d{1,2}):([0-5]\d)"
RANGE = r"(?:-|—|–|~|～|至|到)"
HOURS = TIME + r"\s*" + RANGE + r"\s*(\d{1,2}):([0-5]\d)"
ADMISSION = r"(?:停止(?:入场|入园|售票|入馆)|最晚(?:进入|入场|入馆|入园|售票))"
DAY = r"(?:(?:\d{4})[-/年])?\d{1,2}(?:[-/]|月)\d{1,2}日?"
DATE_PREFIX = re.compile(r"^\s*(?P<first>"+DAY+r")\s*(?:(?P<direction>前|后)|"+RANGE+r"\s*(?P<last>"+DAY+r"|\d{1,2}日))?")
MONTH = r"(\d{1,2})月(?:\s*"+RANGE+r"\s*(\d{1,2})月)?"
WEEK = r"周([一二三四五六日])(?:\s*"+RANGE+r"\s*周?([一二三四五六日]))?"
HOLIDAY = r"法定节假日|节假日|元旦|春节|清明|劳动节|端午|中秋|国庆|元宵|妇女节|青年节|父亲节|母亲节|建党节|建军节|除夕"


def _day(value):
    parts = [int(v) for v in re.findall(r"\d+", value)]
    year, month, day = parts if len(parts) == 3 else (2000, *parts)
    parsed = date(year, month, day)
    return parsed.isoformat() if len(parts) == 3 else parsed.strftime("%m-%d")


def checked(schedule):
    schedule = OpeningSchedule.model_validate(schedule)
    for rule in schedule.rules:
        if any(not 1 <= month <= 12 for month in rule.months) or any(not 0 <= day <= 6 for day in rule.weekdays):
            raise ValueError("营业规则的月份或星期无效")
        for first, last in rule.date_ranges:
            for value in (first, last):
                date.fromisoformat(value if len(value) == 10 else "2000-"+value)
            if len(first) != len(last) or len(first) == 10 and first > last:
                raise ValueError("营业日期范围无效")
        if rule.closed and (rule.windows or rule.last_entry is not None):
            raise ValueError("闭馆规则不能同时有开放时段")
        if not rule.closed and not rule.windows:
            raise ValueError("开放规则缺少时段")
        if any(not 0 <= begin < end <= 1440 for begin, end in rule.windows):
            raise ValueError("营业时段须在00:00至24:00内且开始早于结束")
        if rule.last_entry is not None and not any(begin <= rule.last_entry <= end for begin, end in rule.windows):
            raise ValueError("停止入场须在营业时段内")
    if not schedule.rules:
        schedule.unknown = True
    return schedule


@lru_cache(maxsize=2048)
def parse(text):
    if not isinstance(text, str) or not text.strip():
        return OpeningSchedule(unknown=True)
    claim = re.sub(r"星期|礼拜", "周", text).replace("周天", "周日").replace("：", ":")
    holiday_note = "按常规规则计算，未应用节假日例外；节假日开放须另行核实" if re.search(HOLIDAY, claim) else ""
    claim = re.sub(r"[（(][^()（）]*(?:除外|顺延)[^()（）]*[）)]", "", claim)
    claim = re.sub(r"\s+(?="+DAY+r"\s*"+RANGE+r"\s*"+DAY+r")", "；", claim)
    rules = []
    try:
        for raw in re.split(r"[;；\n]", claim):
            clause = raw.strip(" ，,。")
            if not clause:
                continue
            if re.search(HOLIDAY, clause):
                continue
            clause = re.split(r"注:|具体营业状态|营业时间仅供参考|如遇|具体以|每周.闭园", clause)[0].strip()
            if not clause:
                continue
            rule = OpeningRule()
            match = DATE_PREFIX.match(clause)
            if match:
                first = _day(match["first"])
                if match["direction"]:
                    point = date.fromisoformat("2000-"+first[-5:])
                    if match["direction"] == "前":
                        rule.date_ranges = [("01-01", (point-timedelta(days=1)).strftime("%m-%d"))]
                    else:
                        rule.date_ranges = [(first[-5:], "12-31")]
                else:
                    last = match["last"]
                    if last and re.fullmatch(r"\d{1,2}日", last):
                        last = first[-5:-2]+"-"+last[:-1]
                    rule.date_ranges = [(first, _day(last) if last else first)]
                clause = clause[match.end():].lstrip(" ,:")
            elif re.match(MONTH, clause):
                prefix = re.match(r"(?:"+MONTH+r")(?:\s*[,，、]\s*(?:"+MONTH+r"))*", clause)
                for first, last in re.findall(MONTH, prefix[0]):
                    first, last = int(first), int(last or first)
                    rule.months.extend(range(first, last+1) if first <= last else [*range(first, 13), *range(1, last+1)])
                clause = clause[prefix.end():].lstrip(" ,:")
            week_matches = list(re.finditer(WEEK, clause))
            for week in week_matches:
                first, last = "一二三四五六日".index(week[1]), "一二三四五六日".index(week[2] or week[1])
                rule.weekdays.extend(range(first, last+1) if first <= last else [*range(first, 7), *range(last+1)])
            clause = re.sub(WEEK, "", clause)
            rule.closed = bool(re.search(r"全天(?:不开放|关闭)|闭馆|闭园|休馆|不营业|暂停开放", clause))
            if rule.closed:
                clause = re.sub(r"全天(?:不开放|关闭)|闭馆|闭园|休馆|不营业|暂停开放", "", clause)
            else:
                admissions = [int(m[1])*60+int(m[2]) for m in re.finditer(TIME+r"\s*"+ADMISSION, clause)]
                admissions += [int(m[1])*60+int(m[2]) for m in re.finditer(ADMISSION+r"\s*"+TIME, clause)]
                rule.last_entry = min(admissions) if admissions else None
                for hours in re.finditer(HOURS, clause):
                    first, minute, last, end_minute = map(int, hours.groups())
                    rule.windows.append((first*60+minute, last*60+end_minute))
                    if re.match(r"\s*"+ADMISSION, clause[hours.end():]):
                        rule.last_entry = min(rule.last_entry if rule.last_entry is not None else 1440, last*60+end_minute)
                clause = re.sub(HOURS, "", clause)
                clause = re.sub(TIME+r"\s*"+ADMISSION+"|"+ADMISSION+r"\s*"+TIME, "", clause)
                clause = re.sub(ADMISSION, "", clause)
            remainder = re.sub(r"每日|每天|全天开放|全天|营业时间|开放时间|开放时段|营业时段|开放|[\s,，、:()（）。]", "", clause)
            if remainder or not rule.closed and not rule.windows:
                return OpeningSchedule(unknown=True, holiday_note=holiday_note)
            rules.append(rule)
        return checked(OpeningSchedule(rules=rules, holiday_note=holiday_note))
    except (ValueError, TypeError, ValidationError):
        return OpeningSchedule(unknown=True, holiday_note=holiday_note)


def claim_for(stop):
    place = stop.get("map_place") or {}
    return next((place[key].strip() for key in ("opentime2", "open_time") if isinstance(place.get(key), str) and place[key].strip()), None) or stop.get("opening_claim") or ""


def evaluate(schedule, day_date=None):
    note = schedule.holiday_note
    unknown = {"status":"unknown", "unknown":True, "closed":False, "windows":[[540, 1020]],
               "last_entry_minutes":None, "note":"开放时间未核实"+("；"+note if note else "")}
    if schedule.unknown:
        return unknown
    try:
        target = date.fromisoformat(str(day_date)) if day_date else None
    except ValueError:
        return unknown
    if target is None and any(rule.date_ranges or rule.months or rule.weekdays for rule in schedule.rules):
        return unknown
    def applies(rule):
        if rule.date_ranges:
            return any(first <= (str(target) if len(first) == 10 else target.strftime("%m-%d")) <= last
                       if first <= last else target.strftime("%m-%d") >= first or target.strftime("%m-%d") <= last
                       for first, last in rule.date_ranges)
        return not rule.months or target.month in rule.months
    seasonal = [rule for rule in schedule.rules if applies(rule)]
    matching = [rule for rule in seasonal if not rule.weekdays or target.weekday() in rule.weekdays]
    rank = lambda rule: 2 if rule.date_ranges else 1 if rule.months else 0
    highest = max((rank(rule) for rule in matching), default=-1)
    weekday_rank = max((rank(rule) for rule in seasonal if rule.weekdays), default=-1)
    if not matching or weekday_rank > highest:
        if weekday_rank >= 0:
            return {"status":"closed", "closed":True, "unknown":False, "windows":[], "last_entry_minutes":None,
                    "note":"星期开放规则未覆盖目标星期，按闭馆处理"+("；"+note if note else "")}
        return unknown
    matching = [rule for rule in matching if rank(rule) == highest]
    if any(rule.closed for rule in matching):
        if any(not rule.closed for rule in matching):
            return unknown
        return {"status":"closed", "closed":True, "unknown":False, "windows":[], "last_entry_minutes":None, "note":note}
    windows = sorted({window for rule in matching for window in rule.windows})
    entries = [rule.last_entry for rule in matching if rule.last_entry is not None]
    return {"status":"open", "closed":False, "unknown":False, "windows":[list(window) for window in windows],
            "last_entry_minutes":min(entries) if entries else None, "note":note}


def reference(stop, day_date=None):
    text = claim_for(stop)
    stored = stop.get("opening_schedule") or {}
    try:
        schedule = checked(stored["schedule"]) if stored.get("text") == text else parse(text)
    except (ValueError, TypeError, KeyError, ValidationError):
        schedule = OpeningSchedule(unknown=True)
    result = evaluate(schedule, day_date)
    place = stop.get("map_place") or {}
    mapped = any(isinstance(place.get(key),str) and place[key].strip() == text for key in ("opentime2","open_time"))
    if mapped:
        result["note"] = "；".join(filter(None,[result["note"],"地图参考营业时间，不代表已核实出行当日营业或预约"]))
    return {**result, "claim":text, "evidence_id":place.get("evidence_id"), "applicable_date":day_date,
            "kind":"amap_reference" if mapped else "guide_reference",
            "parsed_by":stored.get("parsed_by", "code") if stored.get("text") == text else "code"}


async def prepare(models, store, state, stops, dates):
    missing = {}
    for stop in stops.values():
        text = claim_for(stop)
        if not text or stop.get("assigned_date") and stop["assigned_date"] not in dates:
            continue
        schedule = parse(text)
        if not schedule.unknown:
            continue
        key = "opening:v1:"+hashlib.sha256(text.encode()).hexdigest()
        cached = store.agent_result(state["trip_id"], key)
        if cached:
            stop["opening_schedule"] = cached
        else:
            missing.setdefault(text, {"key":key, "stops":[]})["stops"].append(stop)
    if not missing:
        return
    schedules = {}
    batch_key = "opening_batch:v1:"+str(state.get("generation") or state.get("run_id"))
    attempted = store.agent_result(state["trip_id"],batch_key)
    try:
        if attempted:
            raise ValueError("本次行程已批量解析过营业时间；新增未识别文本暂记未知")
        store.save_agent_result(state["trip_id"],batch_key,{"texts":list(missing)})
        result = await models.ask(OpeningBatch,
            "只将texts中的营业时间原文转换为规则，不推测原文没有的事实。每条text原样返回一次。"
            "date_ranges端点用MM-DD或YYYY-MM-DD，months为1至12，weekdays为周一0至周日6；空数组表示不限定。"
            "日期范围优先于月份，月份优先于默认；同层星期规则未覆盖的星期闭馆。"
            "windows和last_entry单位为午夜起分钟，00:00至24:00为0至1440；停止入场必须在时段内。"
            "法定节假日例外仅记holiday_note，不混入常规规则。多个场馆的不同时间无法确定主场馆时unknown=true。"
            "不能可靠解析则unknown=true且rules为空。", {"texts":list(missing)})
        for item in result.schedules:
            if item.text in missing and item.text not in schedules:
                try:
                    schedules[item.text] = checked(item.schedule)
                except (ValueError, TypeError, ValidationError) as error:
                    logging.getLogger(__name__).warning("Opening fallback result invalid: %s", error)
    except Exception as error:
        logging.getLogger(__name__).warning("Opening fallback unavailable: %s", error)
    for text, entry in missing.items():
        value = {"text":text, "schedule":schedules.get(text, OpeningSchedule(unknown=True)).model_dump(mode="json"), "parsed_by":"model"}
        if text in schedules:
            store.save_agent_result(state["trip_id"], entry["key"], value)
        for stop in entry["stops"]:
            stop["opening_schedule"] = value
