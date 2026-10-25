"""Keep one previous round and reject widespread regressions before publishing a draft."""
import copy
from uuid import uuid4


def route_moves(plan):
    result = {}
    for day in plan.get("days",[]):
        previous = day.get("start_place")
        for activity in day.get("activities",[]):
            if activity.get("kind") != "visit":
                continue
            move = activity.get("move_before") or {}
            if previous or move:
                result[(day["date"],day["city"],previous,activity["name"])] = move
            previous = activity["name"]
        if day.get("end_place"):
            result[(day["date"],day["city"],previous,day["end_place"])] = day.get("return_move") or {}
        for transfer in day.get("transfers",[]):
            move = transfer.get("move") or {}
            result[(day["date"],day["city"],transfer.get("label"),move.get("destination"))] = move
    return result


def regression_reason(previous, current):
    if not previous.get("days"):
        return None
    if not current.get("days"):
        return "本轮排程未返回逐日草稿，已保留上一版路线"
    before, after = route_moves(previous),route_moves(current)
    failures = {code for move in after.values() if move.get("minutes") is None for code in (move.get("failure_codes") or [])}
    if "amap_quota_limit" in failures:
        return "高德日调用配额已耗尽，本轮未继续查询"
    if "amap_usage_limit" in failures:
        return "高德返回调用限制，本轮未继续查询，限制类型需依据错误码核实"
    if failures & {"amap_limit","amap_rate_limit"}:
        return "本轮地图查询超限，后续查询已停止"
    known = {key for key,move in before.items() if move.get("minutes") is not None}
    # Reordered/removed legs no longer need evidence; a failed refresh does not erase a retained result.
    lost = [key for key in known if key in after and after[key].get("minutes") is None]
    if len(lost) >= 2 and len(lost)*2 >= len(known):
        return f"本轮有 {len(lost)} 个仍需使用的原有路段未取得可用耗时；旧查询记录仍保留"
    # Scheduling conflicts go through bounded repair; failed map evidence can still require immediate recovery.
    return None


def previous_round(document):
    candidates = document.get("candidates",[])
    candidate = next((item for item in candidates if item.get("draft",{}).get("days")),None)
    plan = (candidate or {}).get("draft") or document.get("itinerary")
    if not plan or not any(activity.get("kind") == "visit" for day in plan.get("days",[]) for activity in day.get("activities",[])):
        return None
    candidate = candidate or document.get("selected_route") or {"id":uuid4().hex,"title":"上一轮行程","reason":"保留上一轮安排","gaps":[],
        "days":[{"date":day["date"],"city":day["city"],"stop_ids":[activity["id"] for activity in day["activities"] if activity.get("kind") == "visit"],"adjustment":""}
                for day in plan["days"]]}
    return copy.deepcopy({"candidate":candidate,"itinerary":plan,"requirements":document.get("requirements",{}),
                          "selected_source_ids":document.get("selected_source_ids",[])})
