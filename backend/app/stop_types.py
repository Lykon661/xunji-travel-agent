"""User-requested drinks and desserts are visits; guide dining remains optional."""
import re


LIGHT_FOOD = re.compile(r"咖啡|甜品|甜点|糖水|奶茶|茶饮|茶艺馆|冷饮|冰淇淋|冰激凌|烘焙|糕饼|面包|蛋糕|饼店|\b(?:coffee|cafe|café|bakery)\b",re.I)


def is_light_wish(stop):
    if stop.get("source_kind") != "user":
        return False
    place = stop.get("map_place") or {}
    if re.search(r"博物馆|展览馆|文化馆",stop.get("name","")):
        return False
    codes = re.split(r"[|;,\s]+",str(place.get("typecode") or ""))
    return (any(code.startswith(("0505","0506","0507","0508","0509")) for code in codes)
            or bool(LIGHT_FOOD.search(stop.get("name","")+" "+str(place.get("type") or ""))))


def with_visit_defaults(stop):
    if is_light_wish(stop):
        stop = {**stop,"category":"sight"}
        if not stop.get("duration_minutes"):
            stop.update(duration_minutes=45,duration_is_default=True)
    return stop
