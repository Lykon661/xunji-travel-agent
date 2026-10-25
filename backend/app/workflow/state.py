"""LangGraph 共享状态定义与节点到前端阶段的映射。"""
from typing import TypedDict


# 前端进度条按阶段展示；key 为图节点名。
NODE_STAGES = {"coordinate":"coordinating", "requirements_gate":"coordinating", "guides_gate":"parsing",
               "prepare_guides":"parsing", "guide_analysis":"parsing",
               "planning_failed":"reviewing", "coordinator":"coordinating", "apply_assumption_change":"coordinating",
               "coordinator_gate":"interviewing", "decision_confirmation_gate":"interviewing", "interpret_answer":"coordinating", "blocked_plan":"reviewing", "discover":"candidates", "assess":"assessing",
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
    guide_suggestions: dict
    stop_constraint_sources: dict
    user_inputs: list[str]
    preferences_original: str
    temporary_locks: list[str]
    system_durations: dict
    system_assumptions: list[dict]
    system_adjustments: list[dict]
    unmet: list[dict]
    available_sources: list[dict]
    hub_candidates: list[dict]
    system_free_time: dict
    coordinator_messages: list[dict]
    coordinator_confirmation: dict
    coordinator_batch: dict | None
    coordinator_answer: dict
    assumption_change: dict | None
    coordinator_resume: str
    coordinator_early: bool
    coordinator_blocked: bool
    coordinator_previous_plan: dict | None
    coordinator_deferred_unmet: list[dict]
    dialogue_version: int
    agent_unmet: list[dict]
    review: dict
    review_previous: dict | None
    review_change: dict | None
    decision_history: list[dict]
    place_selection: dict
    draft_revision: int
    route_confirmed: bool
    factual_refresh_completed: bool
    assessment_refresh: bool
    keep_current_plan: bool
    revision_request: str
    revision_reply: str
    revision_selected_ids: list[str]
    round_backup: dict
    rollback: dict
    repair_only: bool
    duplicate_review_generation: str
