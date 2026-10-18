"""自动判分：只用代码判定，不依赖人工或模型。

判分在「全部设计开启」的条件下进行（ablation.use(frozenset())），因此关掉某项设计的运行，
依然按完整规则被检查；另外地点重复、来源、时间线、营业时间等用本文件独立实现的规则判断，
不复用被消融的去重代码，避免“关掉检测就测不出问题”。
"""
import copy
import re
import unicodedata
from datetime import date, timedelta

from app import ablation
from app.decisions import confirmation_blockers
from app.places import duplicate_issues
from app.routing import reference_opening
from app.rules import check_itinerary

# 计入「硬约束违反」总数的指标；其余为辅助信息。
VIOLATIONS = ("date_coverage_error", "city_order_error", "dup_visit", "dup_meal", "sourceless_visit",
              "time_conflict", "opening_violation", "lunch_missing", "walking_over_limit", "over_budget")
SUFFIXES = ("风景名胜区", "风景区", "旅游区", "步行街", "步行区", "景区")


def clean(value):
    return re.sub(r"[\s·・，,。、\-—_]", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


def base_name(value, city):
    name = re.sub(r"[(（][^)）]*[)）]", "", clean(value))
    prefix = clean(city).removesuffix("市")
    if prefix and name.startswith(prefix + "市"):
        name = name[len(prefix) + 1:]
    for suffix in SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix) + 1:
            name = name[:-len(suffix)]
            break
    return name


def extra_occurrences(items):
    """Union by AMap POI ID or name key; returns how many items repeat an earlier place."""
    parent = list(range(len(items)))

    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    owners = {}
    for index, keys in enumerate(items):
        for key in keys:
            if key in owners:
                parent[root(index)] = root(owners[key])
            else:
                owners[key] = index
    return len(items) - len({root(index) for index in range(len(items))})


def minutes(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", value):
        return None
    return int(value[:2]) * 60 + int(value[3:5])


def expected_dates(req):
    start, end = date.fromisoformat(req["start_date"]), date.fromisoformat(req["end_date"])
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def evaluate(requirements: dict, candidate: dict | None, stop_pool: set[str]) -> dict:
    candidate = candidate if isinstance(candidate, dict) else {}
    draft = copy.deepcopy(candidate.get("draft") or {}) if isinstance(candidate.get("draft"), dict) else {}
    days = [day for day in draft.get("days") or [] if isinstance(day, dict)] if isinstance(draft.get("days"), list) else []
    # 草稿还没有逐日安排时退回到候选路线的日期；任一处结构异常都按“没有方案”判分，而不是让判分崩溃。
    fallback = candidate.get("days") if isinstance(candidate.get("days"), list) else []
    route_days = [{"date": day.get("date"), "city": day.get("city")} for day in (days or fallback) if isinstance(day, dict)]
    visits = [(day, item) for day in days for item in day.get("activities", []) if item.get("kind") == "visit"]
    meals = [(day, item) for day in days for item in day.get("meal_recommendations", [])]
    result = {"has_plan": bool(visits), "days": len(route_days), "visits": len(visits), "meal_options": len(meals)}

    result["date_coverage_error"] = int(bool(route_days) and [day["date"] for day in route_days] != expected_dates(requirements))
    cities = [day["city"] for day in route_days]
    compressed = [city for index, city in enumerate(cities) if index == 0 or city != cities[index - 1]]
    result["city_order_error"] = int(bool(route_days) and compressed != requirements["cities"])

    result["dup_visit"] = extra_occurrences([
        {*(["poi:" + str(item["map_place"]["id"])] if (item.get("map_place") or {}).get("id") else []),
         "name:" + day["city"] + ":" + base_name(item.get("name"), day["city"])} for day, item in visits])
    # 餐厅门店按 POI 区分分店；没有 POI 时用完整名称（保留括号内分店名）。
    result["dup_meal"] = extra_occurrences([
        {"poi:" + str(item["map_place"]["id"])} if (item.get("map_place") or {}).get("id")
        else {"name:" + day["city"] + ":" + clean(item.get("name"))} for day, item in meals])

    result["sourceless_visit"] = sum(
        item.get("id") not in stop_pool and item.get("source_kind", "guide") == "guide"
        or not ((item.get("evidence") or {}).get("quote") or (item.get("evidence") or {}).get("image_index"))
        for _, item in visits)

    conflicts = 0
    for day in days:
        previous_end = None
        for item in day.get("activities", []):
            start, end = minutes(item.get("start")), minutes(item.get("end"))
            if start is None or end is None or end <= start or previous_end is not None and start < previous_end:
                conflicts += 1
            previous_end = end if end is not None else previous_end
    result["time_conflict"] = conflicts

    opening = 0
    for day, item in visits:
        reference = reference_opening(item, day["date"])
        start, end = minutes(item.get("start")), minutes(item.get("end"))
        if not reference:
            continue
        if reference.get("closed"):
            opening += 1
        elif reference.get("windows") and start is not None and end is not None and not any(
                window[0] <= start and end <= window[1] for window in reference["windows"]):
            opening += 1
        elif reference.get("last_entry_minutes") is not None and start is not None and start > reference["last_entry_minutes"]:
            opening += 1
    result["opening_violation"] = opening

    # 完整代码规则作为另一位“判分员”：即使运行时关掉了代码校验或去重，这里仍按完整规则计算。
    with ablation.use(frozenset()):
        oracle = copy.deepcopy(draft) if days else {}
        if days:
            oracle.update(check_itinerary(oracle, requirements))
            oracle["issues"].extend(duplicate_issues(oracle, requirements))
        codes = [issue["code"] for issue in oracle.get("issues", []) if issue.get("blocking")]
        blockers = confirmation_blockers(oracle, {"required": requirements.get("required_stop_ids", []),
                                                   "excluded": requirements.get("excluded_stop_ids", [])}, requirements) if days else []
    budget = oracle.get("budget") or {}
    result["lunch_missing"] = codes.count("lunch_missing")
    result["walking_over_limit"] = codes.count("walking_limit")
    result["over_budget"] = int(budget.get("status") == "over")
    result["budget_estimate"] = budget.get("known_plus_estimates")
    result["oracle_code_blockers"] = len(blockers)
    result["missing_route_times"] = sum("实际交通耗时尚未取得" in blocker or "实际耗时尚未取得" in blocker for blocker in blockers)
    result["total_violations"] = sum(result[name] for name in VIOLATIONS)

    report = candidate.get("review") or {}
    findings = [item for item in report.get("findings", []) if item.get("severity") == "blocker"]
    result["review_status"] = report.get("status") or "none"
    result["reviewer_blockers"] = sum(item.get("code") != "code_constraint" for item in findings)
    result["code_blockers_reported"] = sum(item.get("code") == "code_constraint" for item in findings)
    result["system_blockers"] = len(candidate.get("confirmation_blockers") or [])
    # 系统认为可以确认：审查通过且没有任何阻断。此时仍存在的违反就是“漏检”。
    result["confirmable"] = int(result["has_plan"] and result["review_status"] in {"pass", "pass_with_warnings"}
                                and not candidate.get("confirmation_blockers"))
    result["escaped_violations"] = result["total_violations"] if result["confirmable"] else 0
    return result
