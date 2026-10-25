"""Shared place identities; coordinates alone never identify a venue or branch."""
import hashlib
import json
import re
import unicodedata

from . import ablation
from .routing import coordinates, distance


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def plan_fingerprint(itinerary):
    # Export links are delivery metadata, not an arrangement or an evidence change.
    return fingerprint({key:value for key,value in itinerary.items() if key != "personal_map"})


def normalized(value):
    return re.sub(r"[\s·・，,。]", "", unicodedata.normalize("NFKC",str(value or ""))).casefold()


def local_name(value, city):
    name = normalized(value)
    prefix = normalized(city).removesuffix("市")
    if prefix and name.startswith(prefix):
        remainder = name[len(prefix):].removeprefix("市")
        if not remainder.startswith(("路","街","大道")):
            name = remainder
    return name


def strip_suffix(name):
    return re.sub(r"(?:步行街|步行区|风景名胜区|风景区|旅游区|景区)$","",name)


def street_alias(item, city=""):
    if item.get("category") == "dining" or item.get("source_kind") == "flyai":
        return None
    place = item.get("map_place") or {}
    city = city or item.get("city") or place.get("cityname")
    name = local_name(item.get("name"),city)
    base = re.sub(r"(?:步行街|步行区)$","",name)
    if not base.endswith(("路","街","大道")):
        return None
    mapped = local_name(place.get("name"),city)
    if mapped and re.sub(r"(?:步行街|步行区)$","",mapped) != base:
        return None
    kind = str(place.get("type") or "")
    if kind and not any(word in kind for word in ("风景","道路","地名","步行街")):
        return None
    return normalized(city).removesuffix("市"),base,name != base or (bool(mapped) and mapped != base)


def place_key(item, city=""):
    place = item.get("map_place") or {}
    if ablation.disabled("dedupe"):
        return "source:"+str(item.get("id") or fingerprint([city,item.get("name"),item.get("source_id")])[:24])
    city = normalized(city or item.get("city") or place.get("cityname"))
    if place.get("id"):
        return "poi:"+fingerprint([city,str(place["id"])])[:24]
    if coordinates(place) and place.get("address") and place.get("name"):
        return "address:"+fingerprint([city,normalized(place["name"]),normalized(place["address"]),coordinates(place)])[:24]
    if not coordinates(place):
        alias = street_alias(item,city)
        if alias:
            return "name:"+fingerprint(alias[:2])[:24]
    return "source:"+str(item.get("id") or fingerprint([city,item.get("name"),item.get("source_id")])[:24])


def canonicalize_stops(stops):
    if ablation.disabled("dedupe"):
        # Ablation: every guide reference is its own place; no cross-guide identity or alias merging.
        return {identity:{**stop,"canonical_place_id":"source:"+str(stop.get("id") or identity),"possible_duplicate_ids":[],
                          "source_references":[{"stop_id":stop.get("id") or identity,"source_id":stop.get("source_id"),
                                                "source_title":stop.get("source_title"),"evidence":stop.get("evidence")}],
                          "alias_names":[stop["name"]] if stop.get("name") else [],
                          "match_status":"resolved" if coordinates(stop.get("map_place")) else "unresolved"}
                for identity,stop in stops.items()}
    result = {identity:{**stop,"canonical_place_id":place_key(stop),"possible_duplicate_ids":[]} for identity,stop in stops.items()}
    parents, streets = {},{}
    def root(key):
        while key in parents:
            key = parents[key]
        return key
    for stop in result.values():
        alias = street_alias(stop)
        if not alias:
            continue
        for previous,was_pedestrian in streets.get(alias[:2],[]):
            if not (alias[2] or was_pedestrian):
                continue
            point, other = coordinates(stop.get("map_place")),coordinates(previous.get("map_place"))
            # A road label and its pedestrian attraction may be different POIs; proximity corroborates the alias.
            if bool(point) != bool(other) or point and other and distance(point,other) > 500:
                continue
            keys = sorted({root(stop["canonical_place_id"]),root(previous["canonical_place_id"])})
            if len(keys) == 2:
                parents[keys[1]] = keys[0]
        streets.setdefault(alias[:2],[]).append((stop,alias[2]))
    aliases = {}
    for stop in result.values():
        if stop.get("category") == "dining" or stop.get("source_kind") == "flyai":
            continue
        place = stop.get("map_place") or {}
        city = stop.get("city") or place.get("cityname")
        name = strip_suffix(local_name(stop.get("name"),city))
        if len(name) < 2:
            continue
        alias = normalized(city).removesuffix("市"),name
        for previous in aliases.get(alias,[]):
            if normalized(stop.get("name")) == normalized(previous.get("name")):
                continue
            point, other = coordinates(place),coordinates(previous.get("map_place"))
            if bool(point) != bool(other) or point and other and distance(point,other) > 800:
                continue
            keys = sorted({root(stop["canonical_place_id"]),root(previous["canonical_place_id"])})
            if len(keys) == 2:
                parents[keys[1]] = keys[0]
        aliases.setdefault(alias,[]).append(stop)
    for stop in result.values():
        stop["canonical_place_id"] = root(stop["canonical_place_id"])
    # Different POI IDs may be distinct branches even at the same address; leave equivalence to evidence review.
    by_address = {}
    for stop in result.values():
        place = stop.get("map_place") or {}
        point = coordinates(place)
        if not point or not place.get("address") or not place.get("name"):
            continue
        key = (normalized(stop.get("city")),normalized(place["name"]),normalized(place["address"]))
        for previous in by_address.get(key,[]):
            if distance(point,coordinates(previous["map_place"])) <= 20:
                if stop["canonical_place_id"] != previous["canonical_place_id"]:
                    stop.setdefault("possible_duplicate_ids",[]).append(previous["canonical_place_id"])
                    previous.setdefault("possible_duplicate_ids",[]).append(stop["canonical_place_id"])
        by_address.setdefault(key,[]).append(stop)
    groups = {}
    for identity,stop in result.items():
        groups.setdefault(stop["canonical_place_id"],[]).append((identity,stop))
    for group in groups.values():
        refs = []
        for identity,stop in group:
            refs.extend(stop.get("source_references") or [])
            refs.append({"stop_id":stop.get("id") or identity,"source_id":stop.get("source_id"),
                         "source_title":stop.get("source_title"),"evidence":stop.get("evidence")})
        refs = list({fingerprint(ref):ref for ref in refs}.values())
        aliases = list(dict.fromkeys(name for _,stop in group for name in [stop.get("name"),*(stop.get("alias_names") or [])] if name))
        for _,stop in group:
            stop.update(source_references=refs,alias_names=aliases,
                        match_status="resolved" if coordinates(stop.get("map_place")) else "unresolved")
    return result


def canonicalize_itinerary(itinerary):
    items = [(day,item) for day in itinerary.get("days",[]) for item in
             [*(a for a in day.get("activities",[]) if a.get("kind") == "visit"),*day.get("meal_recommendations",[])]]
    resolved = canonicalize_stops({str(index):{**item,"city":day.get("city"),
        "category":"dining" if item.get("meal") else item.get("category")} for index,(day,item) in enumerate(items)})
    for index,(_,item) in enumerate(items):
        stop = resolved[str(index)]
        for field in ("canonical_place_id","source_references","alias_names","possible_duplicate_ids"):
            item[field] = stop[field]
    return itinerary


def canonical_key(item, city=""):
    return item.get("canonical_place_id") or place_key(item,city)


def represented_stop_ids(state):
    stops = state.get("stops",{})
    keys = {canonical_key(stop) for stop in stops.values() if stop.get("represented_by")}
    return {identity for identity,stop in stops.items() if canonical_key(stop) in keys}


def verified_review_duplicates(state, itinerary, suspected):
    from .planning_repairs import protected_stops
    protected = protected_stops(state)
    protected |= {identity for identity,stop in state.get("stops",{}).items() if stop.get("assigned_date")}
    current = {item["id"]:({**state.get("stops",{}).get(item["id"],{}),**item,"city":day["city"],
                           "map_place":state.get("stops",{}).get(item["id"],{}).get("map_place") or item.get("map_place")},day["date"],index)
               for day in itinerary.get("days",[]) for index,item in enumerate(day.get("activities",[])) if item.get("kind") == "visit"}
    def names(stop):
        values = [stop.get("name"),(stop.get("map_place") or {}).get("name"),*(stop.get("alias_names") or [])]
        return {local_name(re.sub(r"[（(][^）)]*[）)]","",str(value or "")),stop.get("city")) for value in values if value} - {""}
    result,removed,retained = [],set(),set()
    for group in (suspected or [])[:6]:
        ids = group.get("stop_ids",[])
        if len(ids) != 2 or len(set(ids)) != 2 or not set(ids) <= current.keys() or set(ids) & removed:
            continue
        first,second = (current[identity] for identity in ids)
        a,b = first[0],second[0]
        pa,pb = coordinates(a.get("map_place")),coordinates(b.get("map_place"))
        if normalized(a["city"]).removesuffix("市") != normalized(b["city"]).removesuffix("市") or not pa or not pb or distance(pa,pb) > 2000:
            continue
        na,nb = names(a),names(b)
        if not (na & nb or any(min(len(x),len(y)) >= 2 and (x in y or y in x) for x in na for y in nb)):
            continue
        if set(ids) <= protected:
            continue
        keep = min(ids,key=lambda identity:(identity not in protected,current[identity][1],current[identity][2]))
        drop = next(identity for identity in ids if identity != keep)
        if drop in retained:
            continue
        result.append({"keep":keep,"drop":drop,"date":current[drop][1],"reason":group.get("reason","")})
        removed.add(drop)
        retained.add(keep)
    return result


def planning_stop_pool(stops, preferred_ids=(), locked_ids=()):
    preferred = set(preferred_ids) | set(locked_ids)
    groups, representatives = {},{}
    for identity,stop in stops.items():
        key = canonical_key(stop)
        groups.setdefault(key,[]).append(identity)
        if key not in representatives or (identity in preferred and representatives[key] not in preferred):
            representatives[key] = identity
    available = {identity:stops[identity] for identity in representatives.values()}
    available.update({identity:stop for identity,stop in stops.items() if identity in locked_ids or
                      stop.get("assigned_date") or stop.get("product") or stop.get("source_kind") == "flyai"})
    return available,{key:identities for key,identities in groups.items() if len(identities) > 1}


def repeat_allowed(item, day, role, requirements, meal=None):
    identities = {item.get("id"),canonical_key(item,day.get("city",""))}
    identities.update(ref.get("stop_id") for ref in (item.get("source_references") or []))
    for permission in requirements.get("repeat_exceptions",[]):
        if (permission.get("place_id") in identities and permission.get("role") == role
                and day.get("date") in permission.get("dates",[]) and permission.get("authorization")):
            if role == "visit" or meal in permission.get("meals",[]):
                return True
    return False


def occurrences(itinerary):
    # Older drafts can retain obsolete canonical IDs; re-derive identities before every submission check.
    itinerary = canonicalize_itinerary({"days":[{**day,"activities":[dict(item) for item in day.get("activities",[])],
        "meal_recommendations":[dict(item) for item in day.get("meal_recommendations",[])]} for day in itinerary.get("days",[])]})
    result = []
    for day in itinerary.get("days",[]):
        for role,items in (("visit",[a for a in day.get("activities",[]) if a.get("kind") == "visit"]),
                           ("meal_option",day.get("meal_recommendations",[]))):
            for item in items:
                result.append({"canonical_place_id":canonical_key(item,day.get("city","")),"id":item.get("id"),
                               "name":item.get("name"),"city":day.get("city"),"date":day.get("date"),
                               "role":role,"meal":item.get("meal"),"map_place":item.get("map_place"),
                               "source_kind":item.get("source_kind"),"wish":item.get("wish",False),
                               "possible_duplicate_ids":item.get("possible_duplicate_ids",[]),
                               "source_id":item.get("source_id"),"source_references":item.get("source_references",[])})
    return result


def represented_ids(items):
    result = {item.get("id") for item in items}
    result.update(ref.get("stop_id") for item in items for ref in (item.get("source_references") or []))
    return result


def duplicate_issues(itinerary, requirements=None):
    if ablation.disabled("dedupe") or ablation.disabled("code_checks"):
        return []
    requirements = requirements or {}
    seen, issues = {},[]
    for item in occurrences(itinerary):
        key = (item["role"],item["canonical_place_id"])
        role = "visit" if item["role"] == "visit" else "dining"
        if key in seen and not repeat_allowed(item,item,role,requirements,item.get("meal")):
            first = seen[key]
            issues.append({"code":"duplicate_place" if role == "visit" else "duplicate_dining",
                           "target":item["id"] or item["date"],"blocking":True,
                           "message":f"{item['name']}在{first['date']}与{item['date']}重复"+
                                      ("安排" if role == "visit" else "推荐（含早午晚餐）")})
        seen.setdefault(key,item)
    return issues
