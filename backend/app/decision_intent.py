"""Deterministic guards against reversing a user's keep/remove answer."""
import re
import unicodedata

from .places import canonical_key, canonicalize_stops


DELETE_INTENT = r"删除|删掉|去掉|移除|排除|不再安排|不安排|不再去|不去|不想去|不考虑|不需要|不用了|不要(?:去|安排)?|不用去|取消|放弃|跳过|换掉|替换|代替|替代|改为|改成|换成|算了"
NEGATED_DELETE = r"(?:不要|不用|不必|不能|不准|无需|请勿|禁止|别|不)(?:再)?(?:删除|删掉?|去掉|移除|排除|取消|放弃|跳过|换掉|替换|代替|替代|改为|改成|换成)"
KEEP_INTENT = rf"{NEGATED_DELETE}|保留|留下|继续安排|要去|必去|撤销排除|取消排除|解除排除"
# "A 改为 B" keeps B; "用 A 代替 B" keeps A. Match negations first.
INTENTS = re.compile(rf"(?P<remove_keep>不(?:要)?(?:保留|留下)|不必去)|(?P<keep>{KEEP_INTENT})|"
                     rf"(?P<replace>替换(?:为|成)|改为|改成|换成)|(?P<substitute>代替|替代)|"
                     rf"(?P<give_up>算了)|(?P<remove>{DELETE_INTENT})")
CLAUSES = re.compile(r"[，,。；;！!？?\n]|但是|不过|但")


def _text(value):
    return re.sub(r"[\s‘’“”\"'「」『』]","",unicodedata.normalize("NFKC",value)).casefold()


def has_delete_intent(answer_text):
    # "算了" authorizes only its adjacent place, never unrelated exclusions.
    return any(match.lastgroup not in {"keep","give_up"} for match in INTENTS.finditer(_text(answer_text)))


def _place_intents(answer_text, names):
    result = set()
    for clause in CLAUSES.split(unicodedata.normalize("NFKC",answer_text)):
        clause = _text(clause)
        intents = list(INTENTS.finditer(clause))
        for name in names:
            if not name:
                continue
            for occurrence in re.finditer(re.escape(name),clause):
                neighbours = []
                for intent in intents:
                    before = intent.end() <= occurrence.start()
                    after = intent.start() >= occurrence.end()
                    if not (before or after):
                        continue
                    gap = occurrence.start()-intent.end() if before else intent.start()-occurrence.end()
                    kind = intent.lastgroup
                    if kind == "give_up" and gap:
                        continue
                    meaning = "keep" if (kind == "keep" or kind == "replace" and before
                                         or kind == "substitute" and after) else "remove"
                    neighbours.append((gap,not before,meaning))
                if neighbours:
                    result.add(min(neighbours)[2])
    return result


def unauthorized_stop_changes(answer_text, required_ids, excluded_ids, stops):
    """Scope checks still reject unknown IDs; this guard handles only known places."""
    resolved = canonicalize_stops(stops)
    groups = {}
    for stop in resolved.values():
        names = [stop.get("name"),*(stop.get("alias_names") or []),(stop.get("map_place") or {}).get("name")]
        groups.setdefault(canonical_key(stop),set()).update(_text(name) for name in names if name)
    deletion = has_delete_intent(answer_text)
    issues = []
    for field,identities in (("required_ids",required_ids),("excluded_ids",excluded_ids)):
        unauthorized = []
        for identity in dict.fromkeys(identities):
            if identity not in resolved:
                continue
            meanings = _place_intents(answer_text,groups[canonical_key(resolved[identity])])
            if (field == "excluded_ids" and (not deletion and "remove" not in meanings or "keep" in meanings)
                    or field == "required_ids" and "remove" in meanings):
                unauthorized.append(identity)
        if unauthorized:
            issues.append({"field":field,"unauthorized_ids":unauthorized})
    return issues
