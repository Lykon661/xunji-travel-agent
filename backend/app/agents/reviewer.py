from ..models import REVIEW_DIMENSIONS, ReviewReport, SemanticReviewReport
from ..reviewing import canonical_review_path, review_field_index
from .runtime import AgentRuntime, agent_payload, digest
from ..assumptions import ASSUMPTION_NOTE


class ReviewerAgent(AgentRuntime):
    def __init__(self, models, store):
        super().__init__("检验 Agent",models,store)

    async def review(self, state, payload):
        from ..views import reviewer_view
        from ..unmet import valid_refs
        from ..reviewing import pointer_exists
        payload = reviewer_view(state,agent_payload(state,payload))
        if payload.get("review_mode") == "changed_requirements" and not set(payload.get("review_dimensions",[])) & {"places","dining","sources"}:
            return ReviewReport(status="pass",summary="本次仅改变代码负责的条件，沿用原有语义提示。",
                checks=[{"dimension":dimension,"result":"not_applicable","note":"本次无语义变化"} for dimension in sorted(REVIEW_DIMENSIONS)])
        instruction = (
            ASSUMPTION_NOTE+"只审查三个语义维度：places（用户偏好与天气适配）、sources（所选攻略主题）、dining（餐饮推荐合理性）。"
            "禁止审查时刻、开放/停止入场、交通、预算、住宿与其他硬性条件，这些由代码负责。"
            "checks只返回places/dining/sources各一次；findings必须标明对应dimension，severity只能warning。"
            "places还要用suspected_duplicates指出疑似重复，最多6组，每组两个current_places中的完整景点ID及reason。"
            "简称/全称、上级区域与内部景点、同地不同攻略ID、跨日重复都要指出；不同分店、景区内独立子园区（如长隆野生动物世界与长隆欢乐世界）不算。"
            "你只指出疑似，不决定保留或删除，不把疑似重复写入findings或用户提示；代码负责核对和处理。"
            "只给提示，不触发修复或要求用户查询。只引用当前对象ID、日期、路径与evidence_index已有ID。"
            "review_mode=changed_dates时只审改动日期，其他日期沿用旧提示。"
            "review_mode=changed_requirements时只审review_dimensions中的语义变化。"
            "普通攻略餐厅为可选推荐，source_kind=user的餐饮反映用户偏好。")
        request = {**payload,"field_path_index":review_field_index(payload["plan"])}
        result = await self.ask(state,SemanticReviewReport,instruction,request,cache=False)
        report = result.model_dump(mode="json")
        current_ids = {item["id"] for item in payload.get("current_places",[])}
        reviewed_ids = {item["id"] for day in payload["plan"].get("days",[]) for item in day.get("activities",[]) if item.get("kind") == "visit"}
        report["suspected_duplicates"] = [item for item in report.get("suspected_duplicates",[])
            if len(set(item["stop_ids"])) == 2 and set(item["stop_ids"]) <= current_ids and set(item["stop_ids"]) & reviewed_ids][:6]
        semantic = {item["dimension"]:item for item in report["checks"]}
        report["checks"] = [semantic.get(dimension,{"dimension":dimension,"result":"not_applicable","note":"由代码负责，不属于语义审查"})
                            for dimension in sorted(REVIEW_DIMENSIONS)]
        report["unmet"] = []
        valid = []
        dates = {day["date"] for day in payload["plan"].get("days",[])}
        req = state.get("requirements",{})
        ids = set(state.get("stops",{})) | set(req) | dates | {item["id"] for item in req.get("constraints",[]) if item.get("id") and not item.get("revoke") and not item.get("superseded_by")} | {entry["id"] for group in ("transport","hotels") for entry in payload["plan"].get(group,[])}
        for finding in report["findings"]:
            finding["field_path"] = canonical_review_path(payload["plan"],finding.get("field_path",""))
            if (not pointer_exists(payload["plan"],finding["field_path"]) or
                    finding.get("target") and finding["target"] not in ids or not set(finding.get("dates",[])) <= dates or
                    any(identity not in payload["evidence_index"] for identity in finding.get("evidence_ids",[]))):
                continue
            finding["severity"] = "info" if finding["severity"] == "info" else "warning"
            valid.append(finding)
        report["findings"] = valid
        report["unmet"] = [{**item,"blocking":False} for item in report.get("unmet",[]) if
                           valid_refs(state,payload["plan"],item.get("refs",[])) and set(item.get("dates",[])) <= dates]
        for check in report["checks"]:
            if check["result"] == "blocker":
                check["result"] = "warning"
        report["status"] = "pass_with_warnings" if valid or report["unmet"] or any(item["result"] in {"warning","unknown"} for item in report["checks"]) else "pass"
        self.store.save_agent_result(state["trip_id"],"review:"+digest([payload,state["generation"]]),{"result":report})
        return ReviewReport.model_validate(report)
