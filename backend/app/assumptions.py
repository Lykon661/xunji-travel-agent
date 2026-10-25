import copy
import re

from .authority import USER_SOURCES, user_state
from .models import SystemAssumption


ASSUMPTION_NOTE = "system_assumptions 是系统默认选择，不得说成用户要求；用户条件、用户决定与系统假设分别使用各自字段。"
MAX_ASSUMPTIONS = 200


def _records(state):
    records = [SystemAssumption.model_validate(item).model_dump(mode="json") for item in state.get("system_assumptions", [])]
    if len(records) > MAX_ASSUMPTIONS or len({item["id"] for item in records}) != len(records):
        raise ValueError("System assumption ledger is too long or contains duplicate IDs")
    return records


def active(state):
    return copy.deepcopy([item for item in _records(state) if item["status"] == "active"])


def record(state, assumption):
    item = SystemAssumption.model_validate(assumption).model_dump(mode="json")
    records = _records(state)
    previous = next((entry for entry in records if entry["id"] == item["id"]), None)
    item["revision"] = (previous["revision"] if previous else 0) + 1
    item = SystemAssumption.model_validate(item).model_dump(mode="json")
    if previous:
        records[records.index(previous)] = item
    else:
        if len(records) >= MAX_ASSUMPTIONS:
            raise ValueError("System assumption ledger is full")
        records.append(item)
    return {"system_assumptions":records}


def replace(state, assumption_id, alternative_id):
    records = _records(state)
    item = next((entry for entry in records if entry["id"] == assumption_id and entry["status"] == "active"), None)
    if item is None:
        raise ValueError("Unknown active assumption: " + assumption_id)
    alternative = next((entry for entry in item["alternatives"] if entry["id"] == alternative_id), None)
    if alternative is None:
        raise ValueError("Unknown assumption alternative: " + alternative_id)
    item.update(status="replaced", revision=item["revision"]+1)
    SystemAssumption.model_validate(item)
    return copy.deepcopy(alternative["actions"]), {"system_assumptions":records}


def agent_context(state):
    decisions = [copy.deepcopy(entry) for entry in state.get("decision_history", [])
                 if not entry.get("auto_settled") and entry.get("source", "legacy_unknown") in USER_SOURCES]
    adjustments = system_records(state.get("decision_history", []))
    for entry in state.get("system_adjustments", []):
        if entry not in adjustments:
            adjustments.append(copy.deepcopy(entry))
    return {"user_requirements":user_state(state)["requirements"], "user_decisions":decisions,
            "system_assumptions":active(state), "system_adjustments":adjustments}


def user_decisions(history):
    return [entry for entry in history if not entry.get("auto_settled") and entry.get("source", "legacy_unknown") in USER_SOURCES]


def system_records(history):
    return [{"source":"system", "auto_settled":entry.get("auto_settled","other"),
             "status":entry.get("status","legacy"),"affected_dates":entry.get("affected_dates",[]),
             "instruction":entry.get("instruction") or "沿用系统调整记录"}
            for entry in history if entry not in user_decisions(history)]


def agent_payload(state, payload):
    def split(value):
        if isinstance(value,dict):
            return {key:user_decisions(item) if key in {"decisions","decision_history","history"} and isinstance(item,list)
                    else item if key == "available_stops" else split(item) for key,item in value.items()}
        if isinstance(value,list):
            return [split(item) for item in value]
        return value
    return {**visible_text(split(payload),state), **agent_context(state)}


def visible_text(value, state):
    if not system_records(state.get("decision_history",[])):
        return value
    from .authority import user_texts
    original = " ".join(user_texts(state))
    attribution = re.compile(r"用户明确(?:要求|建议|指定|提出|指示)?|用户要求|你要求|按你的要求")
    def clean(item, field=""):
        if isinstance(item,str) and field in {"gaps","adjustment","explanation","reason","short_day_reason","summary","change_summary"}:
            return "".join(part if not attribution.search(part) or attribution.sub("",part).strip(" ：，。；") in original
                           else attribution.sub("系统建议",part) for part in re.split(r"(?<=[。；\n])",item))
        if isinstance(item,list):
            return [clean(part,field) for part in item]
        if isinstance(item,dict):
            return {key:part if key in {"requirements","constraints","questions","evidence","available_stops"}
                    else clean(part,key) for key,part in item.items()}
        return item
    return clean(value)
