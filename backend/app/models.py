from datetime import date as Date
import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_core import PydanticCustomError
from .stop_types import with_visit_defaults


class AmapKeyInput(BaseModel):
    api_key: SecretStr


class AmapWebKeyInput(BaseModel):
    js_key: SecretStr
    security_code: SecretStr = SecretStr("")


class FlyAIKeyInput(BaseModel):
    api_key: SecretStr


class GuideSearchInput(BaseModel):
    city: str = Field(default="", max_length=80)
    keywords: str = Field(default="", max_length=200)
    base_version: int = Field(ge=0)


class RepeatPermission(BaseModel):
    place_id: str = Field(min_length=1, max_length=200)
    role: Literal["visit", "dining"]
    dates: list[Date] = Field(min_length=1, max_length=30)
    meals: list[Literal["breakfast", "lunch", "dinner"]] = Field(default_factory=list, max_length=3)
    authorization: str = Field(min_length=1, max_length=2000)


CONSTRAINT_KINDS = ("fewer_walking", "fixed_order", "time_anchor", "visit_duration",
                    "transfer_buffer", "booking_prerequisite", "transit_rule", "daily_min_stops")
# Kinds that apply to exactly one stop, named by scope.stop_ids.
SINGLE_STOP_KINDS = frozenset({"time_anchor", "visit_duration", "booking_prerequisite"})


class ConstraintScope(BaseModel):
    dates: list[Date] = Field(default_factory=list, max_length=30, description="生效日期；空表示全程")
    stop_ids: list[str] = Field(default_factory=list, max_length=20, description="生效地点的完整ID；空表示不限地点")
    cities: list[str] = Field(default_factory=list, max_length=8, description="生效城市；空表示不限城市")


class ConstraintDraft(BaseModel):
    """Model-facing shape: one user condition, quoted verbatim, with typed parameters per kind."""
    kind: Literal["fewer_walking", "fixed_order", "time_anchor", "visit_duration",
                  "transfer_buffer", "booking_prerequisite", "transit_rule", "daily_min_stops"]
    strength: Literal["hard", "soft"] = "hard"
    scope: ConstraintScope = Field(default_factory=ConstraintScope)
    before: str = Field(default="", max_length=200, description="fixed_order先去的地点ID")
    after: str = Field(default="", max_length=200, description="fixed_order后去的地点ID")
    anchor: Literal["", "sunrise", "sunset", "appointment"] = ""
    at: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$", description="time_anchor的指定时刻HH:MM")
    minutes: int | None = Field(default=None, ge=5, le=720, description="visit_duration游玩分钟数或transfer_buffer缓冲分钟数")
    meters: int | None = Field(default=None, ge=0, le=50000, description="fewer_walking用户给出的每天步行上限（米）")
    count: int | None = Field(default=None, ge=1, le=12, description="daily_min_stops每天至少安排的景点个数")
    mode: Literal["", "train", "flight", "ferry"] = ""
    rule: Literal["", "public_only", "auto_by_transit"] = ""
    revoke: bool = Field(default=False, description="true表示取消此前同种类、同对象的条件")
    quote: str = Field(min_length=1, max_length=200, description="逐字摘自用户原话")

    @model_validator(mode="after")
    def typed_parameters(self):
        if self.kind == "fixed_order":
            if not self.before or not self.after or self.before == self.after:
                raise ValueError("fixed_order需要两个不同地点的before与after")
            self.scope.stop_ids = [self.before, self.after]
        elif self.kind in SINGLE_STOP_KINDS and len(self.scope.stop_ids) != 1:
            raise ValueError(self.kind+"必须且只能指定一个地点")
        if self.revoke:
            return self
        if self.kind == "time_anchor" and not self.anchor:
            raise ValueError("time_anchor需要anchor")
        if self.kind == "visit_duration" and self.minutes is None:
            raise ValueError("visit_duration需要minutes")
        if self.kind == "transfer_buffer" and (not self.mode or self.minutes is None):
            raise ValueError("transfer_buffer需要mode与minutes")
        if self.kind == "transit_rule" and not self.rule:
            raise ValueError("transit_rule需要rule")
        if self.kind == "daily_min_stops" and self.count is None:
            raise ValueError("daily_min_stops需要count")
        return self


CONSTRAINT_RECORD_FIELDS = frozenset({"id", "source", "round_id", "superseded_by", "quote"})


def constraint_id(item: dict) -> str:
    """Identity from the condition itself, so an edited constraint never keeps a stale ID."""
    content = {key:value for key,value in item.items() if key not in CONSTRAINT_RECORD_FIELDS}
    return "c_"+hashlib.sha256(json.dumps(content,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()[:12]


class Constraint(ConstraintDraft):
    id: str = Field(default="", max_length=40)
    source: Literal["form", "message", "interview", "decision"]
    round_id: str = Field(default="", max_length=200)
    superseded_by: str = Field(default="", max_length=40)

    @model_validator(mode="after")
    def content_identity(self):
        self.id = constraint_id(self.model_dump(mode="json"))
        return self


class WishPlace(BaseModel):
    """A place the user wants to visit before any guide is read; always planned as a required stop."""
    city: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=150)
    date: Date | None = None
    duration_minutes: int | None = Field(default=None, ge=10, le=720)


WISH_PREFIX = "wish:"


def wish_stop_id(city: str, name: str) -> str:
    """Identity from city and name only, so editing the date or duration keeps the same stop."""
    key = json.dumps([city.strip(), re.sub(r"\s+", "", name)], ensure_ascii=False)
    return WISH_PREFIX+hashlib.sha256(key.encode()).hexdigest()[:12]


def wish_stops(requirements: dict) -> dict[str, dict]:
    stops = {}
    for item in requirements.get("wish_places") or []:
        identity = wish_stop_id(item["city"], item["name"])
        stop = {"id":identity,"city":item["city"],"name":item["name"].strip(),"day":1,"category":"sight",
                "source_id":identity,"source_kind":"user","source_title":"你指定想去的地点","source_url":None,
                "evidence":{"kind":"text","quote":"你在旅行需求中指定想去："+item["name"].strip()}}
        if item.get("duration_minutes"):
            stop["duration_minutes"] = item["duration_minutes"]
        if item.get("date"):
            stop["assigned_date"] = str(item["date"])
        stops[identity] = with_visit_defaults(stop)
    return stops


class Requirements(BaseModel):
    origin: str | None = None
    cities: list[str] = Field(default_factory=list, max_length=8)
    start_date: Date | None = None
    end_date: Date | None = None
    people: int | None = Field(default=None, ge=1, le=20)
    rooms: int | None = Field(default=None, ge=1, le=10)
    budget: float | None = Field(default=None, gt=0, le=10000000)
    preferences: str = Field(default="", max_length=2000)
    transport: Literal["any", "train", "flight"] = "any"
    local_transport: Literal["public", "walking", "driving", "bicycling"] = "public"
    bed_type: str = "双床房"
    return_to_origin: bool = True
    # The user books the trip to and from the origin; only destination days and inter-city legs are planned.
    self_arranged_round_trip: bool = False
    origin_address: str = Field(default="", max_length=200)
    children: int = Field(default=0, ge=0, le=19)
    children_ages: list[int] = Field(default_factory=list, max_length=19)
    earliest_departure: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    latest_arrival: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    latest_return: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    max_transport_hours: float | None = Field(default=None, gt=0, le=72)
    direct_only: bool = True
    flight_cabin: str = Field(default="", max_length=40)
    train_seat: str = Field(default="", max_length=40)
    hotel_type: Literal["", "酒店", "民宿", "客栈"] = ""
    hotel_stars: str = Field(default="", pattern=r"^(?:[1-5](?:,[1-5])*)?$")
    hotel_areas: dict[str,str] = Field(default_factory=dict, max_length=8)
    max_walk_per_day: int = Field(default=8000, ge=0, le=50000)
    walking_limit_exempt_dates: list[Date] = Field(default_factory=list, max_length=30)
    allow_taxi: bool = False
    required_stop_ids: list[str] = Field(default_factory=list, max_length=80)
    excluded_stop_ids: list[str] = Field(default_factory=list, max_length=80)
    # Places the user named before planning; each one is kept in required_stop_ids under its wish: ID.
    wish_places: list[WishPlace] = Field(default_factory=list, max_length=20)
    repeat_exceptions: list[RepeatPermission] = Field(default_factory=list, max_length=20)
    constraints: list[Constraint] = Field(default_factory=list, max_length=60)
    # Digest of the preferences text the form-sourced constraints were extracted from.
    constraints_basis: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def dates_valid(self):
        self.wish_places = [place.model_copy(update={"city":place.city.strip(),"name":place.name.strip()}) for place in self.wish_places]
        if any(not place.name for place in self.wish_places):
            raise ValueError("想去的地点名称不能为空")
        if any(place.city not in self.cities for place in self.wish_places):
            raise ValueError("想去的地点须属于目的地城市")
        wish_ids = [wish_stop_id(place.city, place.name) for place in self.wish_places]
        if len(set(wish_ids)) != len(wish_ids):
            raise ValueError("想去的地点不能重复填写")
        self.required_stop_ids = list(dict.fromkeys(
            [identity for identity in self.required_stop_ids if not identity.startswith(WISH_PREFIX)]+wish_ids))
        if set(self.required_stop_ids) & set(self.excluded_stop_ids):
            raise ValueError("必去景点不能同时设为排除")
        if any(city not in self.cities or len(area.strip()) > 150 for city,area in self.hotel_areas.items()):
            raise ValueError("住宿区域须对应目的地城市，且每个区域不超过150字")
        self.hotel_areas = {city:area.strip() for city,area in self.hotel_areas.items() if area.strip()}
        if self.people and self.children >= self.people:
            raise ValueError("人数包含儿童，至少需要一位成人")
        if any(age < 0 or age > 17 for age in self.children_ages) or (self.children_ages and len(self.children_ages) != self.children):
            raise ValueError("儿童年龄须为0–17岁，填写时应覆盖每位儿童")
        if self.start_date and self.end_date:
            days = (self.end_date - self.start_date).days + 1
            if days < 1 or days > 30:
                raise ValueError("第一版支持 1–30 天旅行，请检查起止日期")
            if days < len(self.cities):
                raise ValueError("旅行天数不能少于目的地城市数")
            if any(day < self.start_date or day > self.end_date for permission in self.repeat_exceptions for day in permission.dates):
                raise ValueError("重复安排的授权日期必须在旅行日期内")
            if any(place.date and not self.start_date <= place.date <= self.end_date for place in self.wish_places):
                raise ValueError("想去地点的指定日期必须在旅行日期内")
        if len(set(self.cities)) != len(self.cities):
            raise ValueError("第一版城市列表不能重复")
        return self

    def missing(self) -> list[str]:
        names = {"origin": "出发地", "cities": "目的地城市", "start_date": "出发日期", "end_date": "结束日期", "people": "人数", "rooms": "房间数", "budget": "整趟预算"}
        return [label for key, label in names.items() if not getattr(self, key)]


class Evidence(BaseModel):
    kind: Literal["text", "image"]
    quote: str = Field(min_length=1, max_length=1500)
    image_index: int | None = Field(default=None, ge=1, le=9)


class RequirementsSaveInput(BaseModel):
    requirements: Requirements
    base_version: int = Field(ge=0)
    updated_at: str = Field(min_length=1)


class GuideStop(BaseModel):
    id: str = ""
    day: int = Field(ge=1, le=30)
    city: str
    name: str = Field(min_length=1, max_length=150)
    duration_minutes: int | None = Field(default=None, ge=10, le=720)
    opening_claim: str | None = None
    ticket_claim: str | None = None
    category: Literal["sight", "dining"] = "sight"
    evidence: Evidence


class GuideExtraction(BaseModel):
    title: str
    cities: list[str] = Field(default_factory=list)
    days: int | None = Field(default=None, ge=1, le=30)
    seasons: list[str] = Field(default_factory=list)
    transport: str | None = None
    lodging_area: str | None = None
    audience: str | None = None
    stops: list[GuideStop] = Field(default_factory=list, max_length=80)
    cautions: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class CandidateDay(BaseModel):
    date: Date
    city: str
    stop_ids: list[str] = Field(default_factory=list, max_length=12)
    adjustment: str = ""


class Candidate(BaseModel):
    id: str = ""
    title: str
    reason: str
    days: list[CandidateDay] = Field(min_length=1, max_length=30)
    gaps: list[str] = Field(default_factory=list)
    feasibility: dict = Field(default_factory=dict)
    draft: dict = Field(default_factory=dict)
    draft_revision: int = Field(default=0, ge=0)
    plan_hash: str = ""
    input_hash: str = ""
    confirmation_blockers: list[str] = Field(default_factory=list)
    review: dict | None = None


class CandidatePack(BaseModel):
    candidates: list[Candidate] = Field(default_factory=list, max_length=1)


class GuideGroup(BaseModel):
    city: str
    stop_ids: list[str] = Field(default_factory=list, description="建议区域内的现有景点 ID 数组；这是资料分组，不是每日行程，无每日 12 项限制")
    reason: str = Field(default="", max_length=1500)


ActionId = Annotated[str, Field(min_length=1, max_length=200)]
ActionText = Annotated[str, Field(min_length=1, max_length=2000)]
ActionNumber = Annotated[int | float, Field(ge=-10000000, le=10000000)]


class CoordinatorModel(BaseModel):
    model_config = {"extra":"forbid", "allow_inf_nan":False}


class UnmetReference(CoordinatorModel):
    type: Literal["requirement", "constraint", "stop", "resource", "day"]
    id: ActionId | None = None
    field: ActionId | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, value):
        if not isinstance(value, dict):
            return value
        value = {**value, **{key:(value.get(key) or None) for key in ("id", "field")}}
        if value["id"] is not None or value.get("type") != "requirement":
            value["id"], value["field"] = value["id"] or value["field"], None
        return value

    @model_validator(mode="after")
    def one_reference(self):
        if (self.id is None) == (self.field is None) or self.field is not None and self.type != "requirement":
            raise ValueError("引用必须提供id；requirement也可仅提供field")
        return self


class UnmetItem(CoordinatorModel):
    kind: Literal["stop_unplaced", "time_conflict", "capacity", "budget", "walking", "opening", "transfer", "lunch",
                  "place_identity", "hub_identity", "pool_shortage", "fact_unverified", "other"]
    refs: list[UnmetReference] = Field(default_factory=list, max_length=24)
    dates: list[Date] = Field(default_factory=list, max_length=30)
    numbers: dict[Annotated[str, Field(min_length=1, max_length=80)], ActionNumber] = Field(default_factory=dict, max_length=16)
    detail: str = Field(default="", max_length=2000)
    evidence_ids: list[ActionId] = Field(default_factory=list, max_length=40)
    blocking: bool = True

    @field_validator("refs", mode="before")
    @classmethod
    def drop_empty_refs(cls, value):
        if not isinstance(value, list):
            return value
        return [item for item in value if not isinstance(item, dict) or item.get("id") or item.get("field")]


class GuideBrief(BaseModel):
    model_config = {"extra":"forbid"}
    status: Literal["ready"] = "ready"
    summary: str
    required_ids: list[str] = Field(default_factory=list)
    excluded_ids: list[str] = Field(default_factory=list)
    groups: list[GuideGroup] = Field(default_factory=list, max_length=30)
    unmet: list[UnmetItem] = Field(default_factory=list,max_length=40)


class GuideFeedback(BaseModel):
    stop_ids: list[str] = Field(default_factory=list, max_length=20)
    explanation: str


class RouteToolRequest(BaseModel):
    tool: Literal["place", "route", "weather", "guide_feedback"]
    city: str = Field(default="", max_length=80)
    origin_id: str = Field(default="", max_length=200)
    destination_id: str = Field(default="", max_length=200)
    mode: Literal["auto", "walking", "transit", "driving", "bicycling"] = Field(default="auto",
        description="route查询方式：auto按用户约束选合适方式；公交地铁填transit。公共交通偏好下先核实步行，不超过15分钟直接返回walking且不查询公交；不能仅在request备注提出方式，不得超出用户允许方式")
    request: str = Field(default="", max_length=1500)


class RouteTurn(BaseModel):
    status: Literal["tools", "ready"]
    tools: list[RouteToolRequest] = Field(default_factory=list, max_length=4)
    candidate: Candidate | None = None
    unmet: list[UnmetItem] = Field(default_factory=list,max_length=40)

    @model_validator(mode="after")
    def valid_turn(self):
        if self.status == "tools" and not self.tools:
            raise ValueError("工具轮次必须提供工具请求")
        if self.status == "ready" and (self.tools or self.candidate is None):
            raise ValueError("完成轮次须交付草稿及未满足条目")
        return self


REVIEW_DIMENSIONS = {"places", "dining", "sources", "constraints", "timing", "lodging", "budget", "facts", "feasibility", "scope"}


class ReviewCheck(BaseModel):
    dimension: Literal["places", "dining", "sources", "constraints", "timing", "lodging", "budget", "facts", "feasibility", "scope"]
    result: Literal["ok", "warning", "blocker", "unknown", "not_applicable"]
    note: str = Field(default="", max_length=1000)


class ReviewFinding(BaseModel):
    dimension: Literal["places", "dining", "sources"] | None = None
    code: str = Field(min_length=1, max_length=80)
    severity: Literal["warning", "blocker"]
    owner: Literal["guide", "resource", "route", "reviewer"] = Field(
        description="guide=攻略提取；resource=资源查询与报价；route=行程排程、午餐与接驳；reviewer=检验自身。只能使用这四个英文值")
    action: Literal["repair", "evidence", "decision"]
    target: str = Field(min_length=1, max_length=200)
    dates: list[Date] = Field(default_factory=list, max_length=30)
    field_path: str = Field(min_length=1, max_length=300, pattern=r"^/",
                           description="相对于plan的现有JSON Pointer，例如/days/0/activities/0；数组下标从0开始，不加/plan前缀；报告缺失字段时指向现有父对象")
    message: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    suggestion: str = Field(default="", max_length=2000)


class SuspectedDuplicate(BaseModel):
    stop_ids: list[str] = Field(min_length=2, max_length=2)
    reason: str = Field(min_length=1, max_length=500)


class ReviewReport(BaseModel):
    status: Literal["pass", "pass_with_warnings", "revise", "needs_evidence"]
    summary: str = Field(min_length=1, max_length=2000)
    checks: list[ReviewCheck] = Field(min_length=10, max_length=10)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=40)
    suspected_duplicates: list[SuspectedDuplicate] = Field(default_factory=list, max_length=6)
    unmet: list[UnmetItem] = Field(default_factory=list,max_length=40)

    @model_validator(mode="after")
    def complete_review(self):
        if {item.dimension for item in self.checks} != REVIEW_DIMENSIONS:
            raise PydanticCustomError("review_dimensions","重审必须完整覆盖十个检查维度，不得重复或遗漏")
        blocked = any(item.severity == "blocker" for item in self.findings)
        if self.status in {"pass", "pass_with_warnings"} and (blocked or any(item.result == "blocker" for item in self.checks)):
            raise PydanticCustomError("review_blocker_status","有阻断问题不能宣称通过")
        if self.status == "pass" and (self.findings or any(item.result in {"warning", "unknown"} for item in self.checks)):
            raise PydanticCustomError("review_pass_warnings","存在缺口时必须明确显示提示")
        if self.status not in {"pass", "pass_with_warnings"} and not blocked:
            raise PydanticCustomError("review_missing_blocker","未通过的重审必须给出具体阻断发现")
        return self


class SemanticReviewCheck(BaseModel):
    dimension: Literal["places", "dining", "sources"]
    result: Literal["ok", "warning", "unknown", "not_applicable"]
    note: str = Field(default="", max_length=1000)


class SemanticReviewFinding(ReviewFinding):
    dimension: Literal["places", "dining", "sources"]
    severity: Literal["warning"] = "warning"


class SemanticReviewReport(BaseModel):
    status: Literal["pass", "pass_with_warnings"]
    summary: str = Field(min_length=1, max_length=2000)
    checks: list[SemanticReviewCheck] = Field(min_length=3, max_length=3)
    findings: list[SemanticReviewFinding] = Field(default_factory=list, max_length=40)
    suspected_duplicates: list[SuspectedDuplicate] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def semantic_dimensions(self):
        if {item.dimension for item in self.checks} != {"places","dining","sources"}:
            raise ValueError("语义审查只覆盖places、dining、sources，各一次")
        return self


class InterviewResponse(BaseModel):
    round_id: str = Field(min_length=1, max_length=80)
    answers: dict[str, str] = Field(default_factory=dict, max_length=6)
    selected_options: dict[str,str] = Field(default_factory=dict,max_length=6)
    interpretation_id: str | None = Field(default=None,min_length=1,max_length=200)
    note: str = Field(default="", max_length=8000)
    confirm: bool = False
    draft_revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def bounded_answers(self):
        if any(len(key) > 80 or len(value) > 8000 for key, value in self.answers.items()):
            raise ValueError("每个回答最多 8000 字")
        if any(len(key) > 200 or len(value) > 200 for key,value in self.selected_options.items()):
            raise ValueError("选项引用最多200字")
        return self


class CandidateRevisionInput(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=80)
    dates: list[Date] = Field(min_length=1, max_length=30)
    content: str = Field(min_length=1, max_length=4000)
    base_version: int = Field(ge=0)
    updated_at: str | None = Field(default=None, min_length=1)
    recheck: bool = False
    selected_stop_ids: list[str] = Field(default_factory=list, max_length=360)


class CandidateRevision(BaseModel):
    days: list[CandidateDay] = Field(min_length=1, max_length=30)
    explanation: str = Field(min_length=1, max_length=2000)


class RequirementUpdate(BaseModel):
    requirements: dict = Field(default_factory=dict)
    constraints: list[ConstraintDraft] = Field(default_factory=list, max_length=20)
    explanation: str = ""


class ConstraintExtraction(BaseModel):
    constraints: list[ConstraintDraft] = Field(default_factory=list, max_length=20)
    allow_taxi: bool | None = Field(default=None, description="用户原话明确不打车时为false，明确允许打车时为true，否则null")


class ChangeRequest(BaseModel):
    scopes: list[Literal["hotel", "activity", "transport", "full"]] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    preserve_cities: list[str] = Field(default_factory=list)
    dates: list[Date] = Field(default_factory=list)
    requirements: dict = Field(default_factory=dict)
    remove_stop_ids: list[str] = Field(default_factory=list)
    add_stop_ids: list[str] = Field(default_factory=list)
    fewer_activities: bool = False
    cheaper_hotel: bool = False
    constraints: list[ConstraintDraft] = Field(default_factory=list, max_length=20)
    explanation: str = ""


class ResourceChoice(BaseModel):
    selected_id: str | None = Field(min_length=1, description="必须显式返回候选ID；无法选择时返回null")
    reason: str = ""


class PlaceChoice(ResourceChoice):
    key: str = Field(min_length=1, description="places中的键，原样复制")


class PlaceChoices(BaseModel):
    choices: list[PlaceChoice] = Field(max_length=8, description="places中每个键恰好一项")


class OrderChoice(BaseModel):
    stop_ids: list[str]
    explanation: str = ""
    durations: dict[str,int] = Field(default_factory=dict, max_length=12,
        description="所选景点没有攻略参考时长时必须逐项填写预计游玩分钟数；全天园区须落实全天游玩时长，不得漏填后按90分钟处理")
    start_times: dict[str,str] = Field(default_factory=dict, max_length=12,
        description="仅填写用户明确预约或指定的开始时刻；普通建议时间和旧计划start不是预约，不能复制到这里")
    short_day_reason: str = Field(default="", max_length=2000,
        description="确需在下午较早结束时，说明具体依据：用户要求半天/留白、开放或返程限制、步行限制或已选攻略无合适剩余景点；正常完整日留空")

    @model_validator(mode="after")
    def bounded_durations(self):
        if any(value < 10 or value > 720 for value in self.durations.values()):
            raise ValueError("游玩时长须在10至720分钟之间")
        if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",value) for value in self.start_times.values()):
            raise ValueError("指定开始时刻须为HH:MM")
        if not set(self.start_times) <= set(self.stop_ids):
            raise ValueError("指定时刻只能对应已经安排的景点")
        return self


class CreateTrip(BaseModel):
    title: str = Field(default="新的旅行", min_length=1, max_length=100)
    requirements: Requirements = Field(default_factory=Requirements)


class MessageInput(BaseModel):
    content: str = Field(default="", max_length=8000)
    requirements: dict = Field(default_factory=dict)
    base_version: int = Field(ge=0)
    updated_at: str | None = Field(default=None, min_length=1)
    retry: bool = False
    resume: bool = False
    refresh_resources: bool = False
    resource_ids: list[str] | None = Field(default=None, min_length=1, max_length=30)
    selected_source_ids: list[str] | None = Field(default=None, max_length=20)
    interview_response: InterviewResponse | None = None


class SelectionInput(BaseModel):
    candidate_id: str
    base_version: int = Field(ge=0)
    updated_at: str | None = Field(default=None, min_length=1)
    draft_revision: int | None = Field(default=None, ge=0)
    plan_hash: str | None = None


class ResourceSelectionInput(BaseModel):
    entry_id: str = Field(min_length=1, max_length=200)
    option_id: str = Field(min_length=1, max_length=200)
    base_version: int = Field(ge=0)
    updated_at: str = Field(min_length=1)


class ProductSearchInput(BaseModel):
    query: str = Field(default="", max_length=1000)
    mode: Literal["hotel", "poi"] = "hotel"
    city: str = Field(default="", max_length=80)
    check_in: Date | None = None
    check_out: Date | None = None
    visit_date: Date | None = None
    bed_type: str = Field(default="", max_length=40)
    hotel_stars: str = Field(default="", pattern=r"^(?:[1-5](?:,[1-5])*)?$")
    hotel_location: Literal["route", "custom", "city"] = "route"
    hotel_area: str = Field(default="", max_length=150)
    min_price: int | None = Field(default=None, ge=0, le=10000000)
    max_price: int | None = Field(default=None, ge=0, le=10000000)
    base_version: int = Field(ge=0)

    @model_validator(mode="after")
    def price_range_valid(self):
        self.hotel_area = self.hotel_area.strip()
        if self.mode == "hotel" and self.hotel_location == "custom" and not self.hotel_area:
            raise ValueError("请填写指定的住宿区域、路段或附近地标")
        if self.min_price is not None or self.max_price is not None:
            if self.mode != "hotel":
                raise ValueError("价格区间仅适用于酒店查询")
            if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
                raise ValueError("酒店最低价不能高于最高价")
        return self


class WorkflowBackInput(BaseModel):
    step: Literal["requirements", "guides", "interview", "routes", "itinerary"]
    base_version: int = Field(ge=0)
    updated_at: str = Field(min_length=1)


class DecisionReopenInput(BaseModel):
    history_index: int = Field(ge=0, le=1000)
    base_version: int = Field(ge=0)
    updated_at: str = Field(min_length=1)


class AssumptionChangeInput(BaseModel):
    model_config = {"extra":"forbid"}

    alternative_id: str = Field(min_length=1, max_length=200)
    assumption_revision: int = Field(ge=1, le=1000000000)
    draft_revision: int = Field(ge=0, le=1000000000)
    base_version: int = Field(ge=0)


class HotelAreaInput(BaseModel):
    city: str = Field(min_length=1, max_length=80)
    area: str = Field(default="", max_length=150)
    base_version: int = Field(ge=0)
    updated_at: str = Field(min_length=1)


class ProductSelectionInput(BaseModel):
    model_config = {"extra":"forbid"}

    product_id: str = Field(min_length=1, max_length=200)
    base_version: int = Field(ge=0)
    updated_at: str = Field(min_length=1)


class TaxiInput(BaseModel):
    date: Date
    activity_id: str = Field(min_length=1, max_length=200)
    base_version: int = Field(ge=0)
    updated_at: str = Field(min_length=1)


class LockInput(BaseModel):
    item_id: str
    locked: bool
    base_version: int = Field(ge=0)


class ItinerarySaveInput(BaseModel):
    base_version: int = Field(ge=0)
    updated_at: str = Field(min_length=1)


_COMMON_STOP = {"stop_id", "date"}
ACTION_PARAMETERS = {
    "skip":(_COMMON_STOP, _COMMON_STOP), "exclude":(_COMMON_STOP, _COMMON_STOP),
    "default_duration":(_COMMON_STOP | {"minutes"}, _COMMON_STOP | {"minutes"}),
    "shorten":(_COMMON_STOP | {"minutes"}, _COMMON_STOP | {"minutes"}),
    "move":(_COMMON_STOP | {"to"}, _COMMON_STOP | {"to"}),
    "add_source":({"source_id"}, {"source_id", "date", "stop_ids"}),
    "lower_count":({"count"}, {"count", "dates"}), "accept_short":(set(), {"dates"}),
    "keep_short":(set(), {"date", "dates"}),
    "set_walk_limit":({"dates", "meters"}, {"dates", "meters"}),
    "exempt_walk_limit":({"dates"}, {"dates"}), "restore_walk_limit":({"dates"}, {"dates"}),
    "set_budget":({"amount"}, {"amount"}),
    "set_time_window":(set(), {"field", "value", "stop_id", "at", "dates", "unlock", "resource_id"}),
    "set_hotel_area":({"city", "area"}, {"city", "area"}),
    "select_poi":({"stop_id", "poi_id"}, {"stop_id", "poi_id"}),
    "select_hub":({"city", "name", "poi_id"}, {"city", "name", "poi_id", "resource_id"}),
    "grant_repeat":({"stop_id", "role", "dates"}, {"stop_id", "role", "dates", "meals"}),
    "reorder_day":({"date", "stop_ids"}, {"date", "stop_ids"}),
    "replace_optional_stop":(_COMMON_STOP | {"replacement_stop_id"}, _COMMON_STOP | {"replacement_stop_id"}),
    "add_optional_stop":(_COMMON_STOP, _COMMON_STOP),
    "retain_free_time":({"date"}, {"date", "free_minutes"}),
    "omit_unselected_hotel_legs":({"dates"}, {"dates"}),
}


class PlanAction(CoordinatorModel):
    type: Literal["skip", "default_duration", "exclude", "move", "shorten", "add_source", "lower_count", "accept_short", "keep_short",
                  "set_walk_limit", "exempt_walk_limit", "restore_walk_limit", "set_budget", "set_time_window", "set_hotel_area",
                  "select_poi", "select_hub", "grant_repeat", "reorder_day", "replace_optional_stop",
                  "add_optional_stop", "retain_free_time", "omit_unselected_hotel_legs"]
    stop_id: ActionId | None = None
    date: Date | None = None
    dates: list[Date] = Field(default_factory=list, max_length=30)
    to: Date | None = None
    minutes: int | None = Field(default=None, ge=10, le=720, strict=True)
    meters: int | None = Field(default=None, ge=0, le=50000, strict=True)
    amount: float | None = Field(default=None, gt=0, le=10000000)
    count: int | None = Field(default=None, ge=1, le=12, strict=True)
    source_id: ActionId | None = None
    stop_ids: list[ActionId] = Field(default_factory=list, max_length=80)
    replacement_stop_id: ActionId | None = None
    city: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    area: Annotated[str, Field(max_length=150)] | None = None
    poi_id: ActionId | None = None
    resource_id: ActionId | None = None
    unlock: bool | None = Field(default=None, description="仅用户明确取消锁定及预约时刻限制时为true；资源选择用set_time_window的resource_id/value引用已有选项ID")
    field: Literal["earliest_departure", "latest_arrival", "latest_return", "max_transport_hours"] | None = None
    value: Annotated[str, Field(max_length=200)] | float | None = None
    at: Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")] | None = None
    role: Literal["visit", "dining"] | None = None
    meals: list[Literal["breakfast", "lunch", "dinner"]] = Field(default_factory=list, max_length=3)
    quote: str = Field(default="", max_length=200)
    free_minutes: int | None = Field(default=None, ge=0, le=720, strict=True)

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, value):
        if not isinstance(value, dict) or value.get("type") not in ACTION_PARAMETERS:
            return value
        allowed = ACTION_PARAMETERS[value["type"]][1]
        value = {key:item for key,item in value.items() if key in {"type", "quote"} or item not in (None, "", [])}
        if "date" in value and "date" not in allowed and "dates" in allowed:
            value["dates"] = list(dict.fromkeys([*value.get("dates", []), value["date"]]))
        if "date" in value and "date" not in allowed:
            value.pop("date")
        if isinstance(value.get("dates"), list) and len(value["dates"]) == 1 and "dates" not in allowed and "date" in allowed:
            value["date"] = value.get("date", value["dates"][0])
            if value["date"] == value["dates"][0]:
                value.pop("dates")
        return value

    @model_validator(mode="after")
    def action_parameters(self):
        required, allowed = ACTION_PARAMETERS[self.type]
        supplied = {key for key in self.model_fields_set - {"type", "quote"} if getattr(self,key) is not None and getattr(self,key) != []}
        if supplied - allowed or any(getattr(self, key) is None or getattr(self, key) == [] for key in required):
            raise ValueError("动作参数不完整或不适用于" + self.type)
        if self.type == "keep_short" and not (self.date or self.dates):
            raise ValueError("keep_short需要日期")
        if self.type == "set_time_window":
            global_window = self.field is not None and self.value is not None and self.stop_id is None and self.at is None and not self.dates
            anchor = self.stop_id is not None and self.at is not None and self.field is None and self.value is None
            release = self.unlock and bool(self.stop_id or self.resource_id) and self.at is None and self.field is None and self.value is None
            resource = self.resource_id is not None and isinstance(self.value,str) and self.field is None and self.stop_id is None and self.at is None and not self.unlock
            if sum(map(bool,(global_window and not self.unlock and not self.resource_id,anchor and not self.unlock and not self.resource_id,release,resource))) != 1:
                raise ValueError("时间窗须指定field/value或stop_id/at/dates")
            if global_window and (self.field == "max_transport_hours" and (isinstance(self.value,bool) or not isinstance(self.value,(int,float)) or not 0 < self.value <= 72)
                                  or self.field != "max_transport_hours" and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(self.value))):
                raise ValueError("时间窗数值无效")
        if len(set(self.dates)) != len(self.dates) or len(set(self.stop_ids)) != len(self.stop_ids) or len(set(self.meals)) != len(self.meals):
            raise ValueError("动作范围不可重复")
        if self.type == "grant_repeat" and (self.role == "dining") != bool(self.meals):
            raise ValueError("餐饮再访须指定餐次；游览再访不指定餐次")
        return self


class DecisionImpact(CoordinatorModel):
    minutes: Annotated[int, Field(ge=-43200, le=43200)] | None = None
    amount: ActionNumber | None = None
    meters: Annotated[int, Field(ge=-1000000, le=1000000)] | None = None
    dates: list[Date] = Field(default_factory=list, max_length=30)
    removed_stop_ids: list[ActionId] = Field(default_factory=list, max_length=80)
    added_stop_ids: list[ActionId] = Field(default_factory=list, max_length=80)
    moved_stops: list[dict] = Field(default_factory=list, max_length=80)
    known_before: ActionNumber | None = None
    known_after: ActionNumber | None = None
    budget_limit: ActionNumber | None = None
    travel_minutes: int | None = None
    estimated_transport: bool = False


class DecisionOption(CoordinatorModel):
    id: ActionId
    label: Annotated[str, Field(min_length=1, max_length=200)]
    description: str = Field(default="", max_length=2000)
    actions: list[PlanAction] = Field(min_length=1, max_length=24)
    impact: DecisionImpact = Field(default_factory=DecisionImpact)
    recommended: bool = False


class DecisionQuestion(CoordinatorModel):
    id: ActionId
    title: Annotated[str, Field(min_length=1, max_length=200)]
    question: ActionText
    why_needed: ActionText
    unmet_refs: list[UnmetReference] = Field(default_factory=list, max_length=24)
    affected_dates: list[Date] = Field(default_factory=list, max_length=30)
    affected_stop_ids: list[ActionId] = Field(default_factory=list, max_length=80)
    options: list[DecisionOption] = Field(min_length=2, max_length=4)
    recommended_option_id: ActionId

    @model_validator(mode="after")
    def option_references(self):
        ids = [item.id for item in self.options]
        recommended = [item.id for item in self.options if item.recommended]
        if len(set(ids)) != len(ids) or self.recommended_option_id not in ids or recommended and recommended != [self.recommended_option_id]:
            raise ValueError("选项ID或推荐引用不一致")
        return self


class DecisionBatch(CoordinatorModel):
    batch_id: ActionId
    draft_revision: int = Field(ge=0, le=1000000000)
    stage_scope: Literal["early_identity", "draft_tradeoff"]
    summary: ActionText
    questions: list[DecisionQuestion] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def question_ids(self):
        if len({item.id for item in self.questions}) != len(self.questions):
            raise ValueError("问题ID不可重复")
        return self


class ActionQuote(CoordinatorModel):
    action_index: int = Field(ge=0, le=23)
    quote: Annotated[str, Field(min_length=1, max_length=200)]


class DecisionInterpretation(CoordinatorModel):
    interpretation_id: ActionId
    summary: ActionText
    actions: list[PlanAction] = Field(default_factory=list, max_length=24)
    quotes: list[ActionQuote] = Field(default_factory=list, max_length=24)
    matched_option_ids: list[ActionId] = Field(default_factory=list, max_length=24)
    ambiguities: list[ActionText] = Field(default_factory=list, max_length=24)
    changes: list[ActionText] = Field(default_factory=list, max_length=40)
    relaxed: list[ActionText] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def quote_references(self):
        indices = [item.action_index for item in self.quotes]
        if len(set(indices)) != len(indices) or any(index >= len(self.actions) for index in indices):
            raise ValueError("引文须引用本次不同动作的索引")
        return self


class AssumptionAlternative(CoordinatorModel):
    id: ActionId
    label: Annotated[str, Field(min_length=1, max_length=200)]
    description: str = Field(default="", max_length=2000)
    actions: list[PlanAction] = Field(min_length=1, max_length=24)


class SystemAssumption(CoordinatorModel):
    id: ActionId
    title: Annotated[str, Field(min_length=1, max_length=200)]
    dates: list[Date] = Field(default_factory=list, max_length=30)
    stop_ids: list[ActionId] = Field(default_factory=list, max_length=80)
    current_action: PlanAction
    reason: ActionText
    revision: int = Field(default=0, ge=0, le=1000000000)
    status: Literal["active", "replaced", "withdrawn"] = "active"
    alternatives: list[AssumptionAlternative] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def alternative_ids(self):
        if len({item.id for item in self.alternatives}) != len(self.alternatives):
            raise ValueError("替代方案ID不可重复")
        return self


class DecisionOptionCopy(CoordinatorModel):
    id: ActionId
    description: str = Field(default="", max_length=2000)


class DecisionQuestionCopy(CoordinatorModel):
    id: ActionId
    title: Annotated[str, Field(min_length=1, max_length=200)]
    question: ActionText
    why_needed: ActionText
    options: list[DecisionOptionCopy] = Field(min_length=2, max_length=4)


class DecisionWording(CoordinatorModel):
    questions: list[DecisionQuestionCopy] = Field(min_length=1, max_length=6)


class CoordinatorAnswer(CoordinatorModel):
    round_id: ActionId | None = None
    batch_id: ActionId | None = None
    draft_revision: int = Field(ge=0, le=1000000000)
    selected_options: dict[ActionId, ActionId] = Field(default_factory=dict, max_length=6)
    answers: dict[ActionId, Annotated[str, Field(max_length=8000)]] = Field(default_factory=dict, max_length=6)
    note: str = Field(default="", max_length=8000)
    interpretation_id: ActionId | None = None
    confirm: bool = False

    @model_validator(mode="after")
    def round_reference(self):
        if not (self.round_id or self.batch_id) or self.round_id and self.batch_id and self.round_id != self.batch_id:
            raise ValueError("回答须引用同一个有效批次")
        return self


class CoordinatorConfirmation(CoordinatorModel):
    batch_id: ActionId
    draft_revision: int = Field(ge=0, le=1000000000)
    interpretation: DecisionInterpretation
    original_answers: dict[ActionId, Annotated[str, Field(max_length=8000)]] = Field(default_factory=dict, max_length=6)
    original_note: str = Field(default="", max_length=8000)
    state_hash: ActionId
    batch_hash: ActionId


class CoordinatorOutcome(CoordinatorModel):
    state_updates: dict = Field(default_factory=dict)
    itinerary: dict
    batch: DecisionBatch | None = None
    confirmation: CoordinatorConfirmation | None = None
    messages: list[dict] = Field(default_factory=list)
    remaining_blockers: list[UnmetItem] = Field(default_factory=list)
