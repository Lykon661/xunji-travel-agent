from datetime import date as Date
import re
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, SecretStr, model_validator
from pydantic_core import PydanticCustomError


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
    meals: list[Literal["lunch", "dinner"]] = Field(default_factory=list, max_length=2)
    authorization: str = Field(min_length=1, max_length=2000)


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
    repeat_exceptions: list[RepeatPermission] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def dates_valid(self):
        if set(self.required_stop_ids) & set(self.excluded_stop_ids):
            raise ValueError("必去景点不能同时设为排除")
        if any(city not in self.cities or len(area.strip()) > 150 for city,area in self.hotel_areas.items()):
            raise ValueError("住宿区域须对应目的地城市，且每个区域不超过150字")
        self.hotel_areas = {city:area.strip() for city,area in self.hotel_areas.items() if area.strip()}
        if self.people and self.children >= self.people:
            raise ValueError("人数包含儿童，至少需要一位成人")
        if any(age < 0 or age > 17 for age in self.children_ages) or (self.children_ages and len(self.children_ages) != self.children):
            raise ValueError("儿童年龄须为0–17岁，填写时应覆盖每位儿童")
        if re.search(r"(?:不|不要|不想|不能|禁止|拒绝)(?:乘坐|坐)?(?:打车|出租车|网约车)", self.preferences):
            self.allow_taxi = False
        if self.start_date and self.end_date:
            days = (self.end_date - self.start_date).days + 1
            if days < 1 or days > 30:
                raise ValueError("第一版支持 1–30 天旅行，请检查起止日期")
            if days < len(self.cities):
                raise ValueError("旅行天数不能少于目的地城市数")
            if any(day < self.start_date or day > self.end_date for permission in self.repeat_exceptions for day in permission.dates):
                raise ValueError("重复安排的授权日期必须在旅行日期内")
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


class InterviewQuestion(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=150)
    question: str = Field(min_length=1, max_length=1500)
    options: list[str] = Field(default_factory=list, max_length=10)
    recommendation: str = Field(min_length=1, max_length=1000)


class PlanningDecision(InterviewQuestion):
    why_needed: str = Field(min_length=1, max_length=1500)
    affected_dates: list[Date] = Field(default_factory=list, max_length=30)


class GuideGroup(BaseModel):
    city: str
    stop_ids: list[str] = Field(default_factory=list, description="建议区域内的现有景点 ID 数组；这是资料分组，不是每日行程，无每日 12 项限制")
    reason: str = Field(default="", max_length=1500)


class GuideBrief(BaseModel):
    status: Literal["ready", "needs_decision"]
    summary: str
    required_ids: list[str] = Field(default_factory=list)
    excluded_ids: list[str] = Field(default_factory=list)
    groups: list[GuideGroup] = Field(default_factory=list, max_length=30)
    question: PlanningDecision | None = None

    @model_validator(mode="after")
    def explicit_decision(self):
        if (self.status == "needs_decision") != (self.question is not None):
            raise ValueError("待决定状态必须有一道问题，完成状态不能遗留问题")
        if self.question is None and re.search(r"当前尚未明确的是|需要用户(?:决定|选择)|待用户(?:决定|选择)|尚未(?:确定|明确)(?:用户|你).{0,12}是否",self.summary):
            raise ValueError("总结中存在未决用户选择，必须返回needs_decision和question，不能直接结束")
        return self


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
    status: Literal["tools", "ready", "needs_decision"]
    tools: list[RouteToolRequest] = Field(default_factory=list, max_length=4)
    candidate: Candidate | None = None
    order: "OrderChoice | None" = None
    question: PlanningDecision | None = None

    @model_validator(mode="after")
    def valid_turn(self):
        if self.status == "needs_decision" and (self.question is None or self.tools):
            raise ValueError("需要用户决定时必须返回一道完整问题，不能同时请求工具")
        if self.status == "tools" and (not self.tools or self.question is not None):
            raise ValueError("工具轮次必须提供工具请求")
        if self.status == "ready" and (self.question is not None or self.tools or (self.candidate is None and self.order is None)):
            raise ValueError("完成轮次必须有路线或顺序，不能遗留问题或工具请求")
        text = (self.candidate.reason if self.candidate else "")+(self.order.explanation if self.order else "")
        if self.status == "ready" and re.search(r"当前尚未明确的是|需要用户(?:决定|选择)|待用户(?:决定|选择)|尚未(?:确定|明确)(?:用户|你).{0,12}是否",text):
            raise ValueError("路线说明仍有未决选择，应返回具体问题")
        return self


DECISION_REQUIREMENT_FIELDS = frozenset({
    "preferences","allow_taxi","local_transport","max_walk_per_day","budget","earliest_departure",
    "latest_arrival","latest_return","max_transport_hours","hotel_areas","repeat_exceptions",
})


class DecisionResolution(BaseModel):
    model_config = {"extra":"forbid", "json_schema_extra":{"minProperties":1}}

    requirements: dict = Field(default_factory=dict,
        description="只返回本次回答明确授权的变更；条件式取舍写入preferences，不新建业务字段或复制完整原需求",
        json_schema_extra={"propertyNames":{"enum":sorted(DECISION_REQUIREMENT_FIELDS)}})
    required_ids: list[str] = Field(default_factory=list, description="只填allowed_stop_ids中的完整ID，且用户本次明确要求必去；条件式保留不能直接设为必去")
    excluded_ids: list[str] = Field(default_factory=list, description="只填allowed_stop_ids中的完整ID，且用户本次明确要求排除；待查询后再决定的景点不能立即排除")
    explanation: str = Field(default="", validation_alias=AliasChoices("explanation","reason","summary"))
    walking_limit_action: Literal["unchanged", "relax", "restore"] = "unchanged"
    keep_current_plan: bool = False

    @model_validator(mode="before")
    @classmethod
    def actual_resolution(cls, value):
        if isinstance(value,dict) and not set(value).intersection(set(cls.model_fields) | {"reason","summary"}):
            raise ValueError("需要返回实际的需求解析结果，不能返回空对象、JSON Schema或不相关字段")
        return value


class PlanningReview(BaseModel):
    question: PlanningDecision


class ReviewRepairResolution(BaseModel):
    status: Literal["repair", "needs_decision", "blocked"]
    explanation: str = Field(min_length=1, max_length=2000)
    question: PlanningDecision | None = None

    @model_validator(mode="after")
    def decision_has_question(self):
        if (self.status == "needs_decision") != (self.question is not None):
            raise ValueError("只有真实用户取舍才返回问题；可自动修复或技术失败不能夹带问题")
        return self


REVIEW_DIMENSIONS = {"places", "dining", "sources", "constraints", "timing", "lodging", "budget", "facts", "feasibility", "scope"}


class ReviewCheck(BaseModel):
    dimension: Literal["places", "dining", "sources", "constraints", "timing", "lodging", "budget", "facts", "feasibility", "scope"]
    result: Literal["ok", "warning", "blocker", "unknown", "not_applicable"]
    note: str = Field(default="", max_length=1000)


class ReviewFinding(BaseModel):
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


class ReviewReport(BaseModel):
    status: Literal["pass", "pass_with_warnings", "revise", "needs_evidence", "needs_decision"]
    summary: str = Field(min_length=1, max_length=2000)
    checks: list[ReviewCheck] = Field(min_length=10, max_length=10)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=40)
    question: PlanningDecision | None = None

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
        if self.status == "needs_decision" and self.question is None:
            raise PydanticCustomError("review_question_missing","用户取舍状态必须有一道问题")
        if self.status != "needs_decision" and self.question is not None:
            raise PydanticCustomError("review_question_unexpected","非用户取舍状态的question必须为null")
        return self


class ReviewReferenceCorrection(BaseModel):
    model_config = {"extra":"forbid"}
    finding_index: int = Field(ge=0)
    field_path: str = Field(min_length=1, max_length=300, pattern=r"^/")
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    dates: list[Date] = Field(default_factory=list, max_length=30)


class ReviewReferenceCorrections(BaseModel):
    model_config = {"extra":"forbid"}
    corrections: list[ReviewReferenceCorrection] = Field(min_length=1, max_length=40)


class InterviewRound(BaseModel):
    summary: str = Field(min_length=1, max_length=4000)
    questions: list[InterviewQuestion] = Field(default_factory=list, max_length=1)

    @model_validator(mode="after")
    def unique_questions(self):
        if len({question.id for question in self.questions}) != len(self.questions):
            raise ValueError("追问问题 ID 不能重复")
        return self


class InterviewResponse(BaseModel):
    round_id: str = Field(min_length=1, max_length=80)
    answers: dict[str, str] = Field(default_factory=dict, max_length=1)
    note: str = Field(default="", max_length=4000)
    confirm: bool = False
    draft_revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def bounded_answers(self):
        if any(len(key) > 80 or len(value) > 2000 for key, value in self.answers.items()):
            raise ValueError("每个回答最多 2000 字")
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
    explanation: str = ""


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


RouteTurn.model_rebuild()


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
