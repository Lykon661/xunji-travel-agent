"""LangGraph 共享状态定义与节点到前端阶段的映射。"""
from typing import TypedDict


# 前端进度条按阶段展示；key 为图节点名。
NODE_STAGES = {"coordinate":"coordinating", "requirements_gate":"coordinating", "guides_gate":"parsing",
               "prepare_guides":"parsing", "guide_analysis":"parsing", "interview":"interviewing", "interview_gate":"interviewing",
               "planning_failed":"interviewing", "decision_gate":"decision", "discover":"candidates", "assess":"assessing",
               "selection_gate":"reviewing", "review_gate":"reviewing", "review_result":"reviewing", "revise_candidate":"revising_candidate",
               "transport":"transport", "hotels":"hotels", "activities":"activities", "tickets":"tickets", "check":"checking",
               "confirm_route":"saving", "save":"saving"}


class TripState(TypedDict, total=False):
    trip_id: str
    run_id: str
    generation: str
    base_version: int
    requirements: dict
    message: str
    change: dict
    mode: str
    warnings: list[str]
    candidates: list[dict]
    selected_route: dict
    selected_source_ids: list[str]
    source_selection_changed: bool
    interview: dict
    route_edit: dict
    itinerary: dict
    old_itinerary: dict
    old_requirements: dict
    stops: dict
    weather: dict
    resource_retry_done: bool
    repair_round: int
    locks: list[str]
    previews: dict
    preview_ready: bool
    assessment_dates: list[str]
    assessment_base: str
    revision_rejected: bool
    assessment: bool
    stop_constraints: dict
    guide_brief: dict
    decision: dict
    decision_resume: str
    decision_reopened: bool
    decision_history: list[dict]
    place_selection: dict
    # Code-built options of the pending decision: {question_id, options: {label: action}}.
    decision_options: dict
    draft_revision: int
    route_confirmed: bool
    factual_refresh_completed: bool
    assessment_refresh: bool
    keep_current_plan: bool
    revision_request: str
    revision_reply: str
    revision_selected_ids: list[str]
    review_feedback: list[dict]
    round_backup: dict
    rollback: dict
    repair_only: bool
