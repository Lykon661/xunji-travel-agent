import copy
import logging

from ..adapters import ToolError
from ..models import ReviewReport, ReviewReferenceCorrections
from ..reviewing import canonical_review_path, correct_transfer_arithmetic, review_field_index, review_reference_errors, review_semantic_errors
from ..store import dump
from .route import ANSWERABLE_QUESTIONS
from .runtime import AgentRuntime, digest


class ReviewerAgent(AgentRuntime):
    def __init__(self, models, store):
        super().__init__("检验 Agent",models,store)

    async def review(self, state, payload):
        instruction = (
            "独立重审整份旅行候选，先对照原始证据和需求，不沿用行程 Agent 的自评。"
            "完整检查places/dining/sources/constraints/timing/lodging/budget/facts/feasibility/scope十项。"
            "source_kind=user且wish=true的餐饮地点是硬性用餐安排，须在早餐/午餐/晚餐中恰好出现一次，不要求把它列为游览景点。"
            "用户指定咖啡甜品饮品属于必去visit，默认45分钟，用户填写时长优先；它们不属于正餐wish。正餐尽量顺路2公里内，只有同城所有可行日期均远才保留单独前往说明，指定日期不擅自移动。"
            "餐饮wish无定位或距离远不代表可以删掉：保留地址待核实和单独前往说明；指定日期与城市不符则报告具体冲突。"
            "必去景点不等于时间锁定；普通旧计划的start不是预约证据。对appointment_time冲突核对用户明确要求，错误继承的预约字段反馈route自动更正，"
            "真实预约则反馈route联动调整当天前序活动、午餐和顺序；仅在必须改变真实用户硬约束时提出取舍，不让用户批准常规排程修复。"
            "重点全程扫描同实际地点不同攻略ID、跨日重复，以及同餐厅门店跨午晚餐和跨日重复；"
            "不同分店、景区内独立活动和酒店车站接驳不能误报为重复。明确repeat_exceptions只按授权范围生效。"
            "同城的北京路与北京路步行街这类道路/步行街简称，若位置相符仍是同一游览地点，不能因POI ID不同放过跨日重复。"
            "possible_duplicate_ids只是同名同址近邻的疑点，不证明相同；对照原文与查询证据，不能凭距离合并不同POI门店。"
            "按完整时间线检查班次/接驳/进站缓冲/预约/返程和住宿覆盖、步行、预算口径。"
            "requirements.constraints是用户明确提出的条件约束，只在其scope内生效（scope为空表示全程）；违反某条约束时，finding的evidence_ids引用对应的constraint:<id>。"
            "superseded_by非空或revoke=true的条件已失效，不能据此阻断。"
            "status=needs_decision时的question同样遵守："+ANSWERABLE_QUESTIONS+
            "requirements.self_arranged_round_trip=true表示出发地与目的地之间的去程、返程由用户自行安排：不检查、不查询、不追问去程返程班次、到离时刻或车站机场接驳，只规划目的地内的行程及多个目的地之间的交通。"
            "景点间衔接以review_scope.activities.transfer_timing的代码计算为准：available_minutes已扣除紧邻的午餐、休息或等待时段，"
            "route_minutes是已采用高德路线总耗时，不能重复累加其中的步行接驳和换乘；available_minutes>=route_minutes表示移动时间足够。"
            "例如预留63分钟、路线43分钟，仍剩20分钟，不能无依据判为衔接或缓冲不足。额外缓冲必须引用明确用户要求或真实班次/检票规则，"
            "城际班次衔接以review_scope.days.transport_connections为准：slack_minutes已扣除station_buffer_minutes（进站/值机）或exit_buffer_minutes（出站），"
            "within_station_buffer或within_exit_buffer为true即衔接成立，不能再次扣除进站时间后判为不足。"
            "普通实时交通不确定性只提示warning，不凭空添加最低缓冲要求。预留很长不等于冲突，未选具体船班也不能凭空设置登船截止时刻。"
            "完整游览日应有60分钟午餐预留：activities中meal=lunch的rest是独立午餐时段，"
            "visit.meal_break是全天园区内部用餐，已计入该活动总时长，不能当作活动重叠或重复地点，也不能再次加60分钟。"
            "午餐预留不代表已选择餐厅，不要求补餐厅或增加餐饮费用；固定时段没有午餐空间时反馈timing问题修复，返程只覆盖上午则不强制午餐。"
            "酒店仅在selected_by_user=true时参与住宿覆盖、起终点、接驳与费用审查；"
            "未选定酒店只是推荐，不能暂用、不能要求补选或阻止通过；没有已选酒店时lodging为not_applicable。"
            "review_scope逐日明确适用范围和程序计算的星期。hotel_location_confirmed=false时不检查住宿至首站、末站返回住宿或虚构的每日返程，"
            "start_place/end_place为空是正常状态，不能要求用户补住宿、出发地址或豁免返程；只检查景点之间的真实衔接。"
            "first_leg_required/last_leg_required仅在对应日有已采用酒店或已选城际班次时为true，实际已选班次的到离时刻与车站接驳仍检查。"
            "requirements.return_to_origin=true本身不代表已选返程班次；last_leg_required=false时，返程班次、车站或时刻未提供只能提示待确认，"
            "不能另起RETURN_UNSPECIFIED或MISSING_RETURN_FEASIBILITY等硬问题绕过该范围，更不能要求用户补班次才能形成路线。"
            "星期以review_scope为准，不靠心算或模型记忆。闭馆规则须有适用于对应日期和场馆的证据；不能把周五说成周二后据此删点。"
            "开放冲突必须列出活动日期、起止时刻和对应证据窗口，明确何时早于开放或晚于关闭；完全在窗口内的安排不能判冲突。"
            "最晚入场只比较活动start，闭馆只比较end；例如13:13-16:13、最晚入场16:00、闭馆17:00完全满足两项，"
            "不能把16:00当作离场期限，也不能凭空增加入馆缓冲后阻断。review_scope.activities给出上述时间比较与对应原文。"
            "摘要、检查项和findings必须一致；没有具体开放冲突发现就不能在摘要宣称存在开放冲突。"
            "例如16:40-17:40落在10:00-18:00内，不能写成TIMING_REVIEW_REQUIRED阻断；若没有其他已证实的冲突，只保留普通营业/预约核实提示。"
            "审查报告的状态、格式或结论自相矛盾由你在报告内纠正，不是路线问题，不能为了保留revise或满足结构而新增假blocker、要求重排或重新核验已成立的时间不等式。"
            "金额算术与已知/估算/未知以代码budget为准，不能把未知当零，不能覆盖硬规则。"
            "budget.status=incomplete和unknown表示费用未纳入当前小计，不是按免费或0元计费；estimated_tickets/estimated_local_transport为0仅表示没有可用估算。"
            "只要代码没有over_budget且费用未知已明示，不得因不能证明总预算达标而阻断路线；用pass_with_warnings说明预算尚不能完整核算，不宣称预算已满足。"
            "普通门票缺价、参考报价未结算、预约尚未完成只提示待核实，不要求先购买/预约才能形成路线。"
            "用户明确要求未返回门票价格时默认免费：ticket_quote.assumed_free=true、price=0表示按该用户规则估算，"
            "不是供应商确认免费，不得误判为隐瞒未知费用、漏计票价或预算阻断；保留来源说明即可。reported_free=true表示供应商标记基础参观免费。"
            "确有证据证明闭馆、已售罄、必要预约不可取得、日期票种不适用，或用户明确要求完成预约才安排时，才按具体事实和约束阻断。"
            "地点坐标、地图导航和路段起终点统一以高德map_place及其查询证据为准；飞猪只负责票务查询，供应商坐标不参与路线定位或坐标一致性审查。"
            "飞猪与高德坐标不同不能认定定位错误、缺少衔接或要求用户选择场馆，也不能因此清空或替换已有高德POI。"
            "票务匹配只核对名称、完整地址、展区与明确别名；同址且原文明确新旧名称对应时按同一场馆处理，不因供应商坐标偏差否定匹配。"
            "ticket_candidates_only=true表示门票还在place_confirmation候选阶段，未采用或计价；其名称或地址匹配疑点只记录warning，"
            "不得以未采用门票的匹配疑点阻断已有明确地图定位的行程。核对完整地址、展区与别名，不能仅凭跨平台坐标不同就认定不同场馆。"
            "路线继续使用原高德POI与交通依据；只有名称、地址、展区等证据证实已采用商品/门票确属另一场馆，或高德实际路线定位错误时才阻断并修复。"
            "博物馆、动物园等场馆即使攻略没写票价，也检查ticket_quote查询状态；缺价遵循上述用户默认免费规则，预约状态另作提示，不编造已预约或确认免费事实。"
            "recovery_note表示路线可能回退，不代表每条交通查询失败；refresh_failed表示对应路段补查失败。"
            "逐路段按实际evidence_id和queried_at判断新旧，回滚后补回的成功交通依据可按其查询时间引用，不能因整体回退就判成缺失；旧依据不可冒充新查询。"
            "来源中参考开放时间不证明当日营业，天气只匹配预报日期，查询报价不保证库存和结算。"
            "图片证据只是已有提取引用，不声称本次亲自验证图像。缺普通最新营业说明、远期天气或非关键价格"
            "可pass_with_warnings明确保留；地点/关键路段/必要交通缺失、已采用住宿的硬冲突、确定重复不能通过。"
            "findings必须引用evidence_index中真实ID，field_path是plan中存在的JSON指针。"
            '支持材料中的单独{"$ref":"#/..."}表示本次输入内完全相同的材料，按JSON Pointer读取被引用内容；不是新证据或网页链接。'
            "evidence_catalog列出真实证据ID及请求，不得根据ID数字猜测不存在的查询。"
            "field_path相对于plan，从/days、/transport、/hotels、/budget等实际字段开始，不能加/plan前缀；"
            "数组下标从0开始，不能用活动ID、日期或景点名代替下标。field_path_index提供本次真实对象位置。"
            "报告缺失字段时，field_path应指向现有活动、日期或资源父对象，在message中说明缺什么，不引用不存在的子字段。"
            "owner只能是guide、resource、route、reviewer：攻略提取归guide，报价/查询归resource，排程/午餐/接驳归route，检验自身归reviewer。"
            "局部问题的dates留空，或包含field_path所指日期；跨日问题可以同时包含其他受影响日期。"
            "给出影响日期与建议，不生成新地点、价格或直接修改计划。"
            "action=repair用于可在授权范围内调整的问题；evidence用于需查询的事实；decision仅用于用户取舍。"
            "午餐、顺序、可选项删减、开放窗口和返程衔接能在现有授权内修复时直接反馈repair，由责任Agent自动修改后重审，不能要求用户点击修复或许可重新规划。"
            "即使另有真实用户取舍，也应逐项区分仍可独立修复的问题；不要把整份报告所有问题都变成decision。"
            "已回答或已明确豁免的条件不能重新追问。needs_decision才返回一道question；不让用户代查事实。"
            "没有发现返回pass，有非阻断缺口返回pass_with_warnings；未审完不能通过。")
        request = {**payload,"field_path_index":review_field_index(payload["plan"])}
        for semantic_attempt in range(2):
            result = await self.ask(state,ReviewReport,instruction,request,cache=False)
            original = result
            result = correct_transfer_arithmetic(result,payload)
            if result != original:
                self.store.save_agent_result(state["trip_id"],"review_arithmetic_corrected:"+digest([payload,state["run_id"],semantic_attempt]),
                    {"original":original.model_dump(mode="json"),"result":result.model_dump(mode="json")})
            semantic_errors = review_semantic_errors(result,payload)
            if not semantic_errors:
                break
            self.store.save_agent_result(state["trip_id"],"review_semantic_invalid:"+digest([payload,state["run_id"],semantic_attempt]),
                {"errors":semantic_errors,"result":result.model_dump(mode="json")})
            if semantic_attempt:
                raise ToolError("review_semantics","检验结论纠正一次后仍与审查范围或日期事实矛盾，未采用："+"；".join(semantic_errors[:3]))
            request = {**request,"previous_report":result.model_dump(mode="json"),"semantic_errors":semantic_errors,
                       "correction_instruction":"根据相同原始证据重新审查并返回完整报告，纠正上述误判；保留其他有效阻断，不得为通过而删除真实问题。"}
        previous = None
        for attempt in range(2):
            if attempt:
                patch = await self.ask(state,ReviewReferenceCorrections,
                    "仅修正既有检验发现的引用，不重新审查。corrections恰好包含invalid_findings中的每个finding_index一次。"
                    "field_path只能选field_path_index中的现有对象位置；缺失字段指向父对象。"
                    "evidence_ids只能从evidence_catalog选与原问题和查询请求相符的真实ID，不能编造、猜测或替换审查结论；"
                    "dates只能选plan_dates，并包含field_path所指日期；跨日问题保留其他有效日期。"
                    "保留原本正确的引用字段；不返回状态、严重性、检查项或新的报告。",
                    request,cache=False)
                expected_indices = {item["finding_index"] for item in request["invalid_findings"]}
                if (not isinstance(patch,ReviewReferenceCorrections) or
                        len(patch.corrections) != len(expected_indices) or
                        {item.finding_index for item in patch.corrections} != expected_indices):
                    raise ToolError("review_correction","检验引用补丁未逐项对应原问题，审查未采用")
                corrected = copy.deepcopy(previous)
                for item in patch.corrections:
                    fields = item.model_dump(mode="json")
                    fields.pop("finding_index")
                    invalid_fields = {error["field"] for error in request["validation_errors"]
                                      if error["finding_index"] == item.finding_index}
                    finding = corrected["findings"][item.finding_index]
                    if "evidence_ids" in invalid_fields:
                        valid = [identity for identity in finding["evidence_ids"] if identity in payload["evidence_index"]]
                        fields["evidence_ids"] = list(dict.fromkeys([*valid,*fields["evidence_ids"]]))
                        if len(fields["evidence_ids"]) > 20:
                            raise ToolError("review_correction","检验引用补丁超出依据数量，审查未采用")
                    if "dates" in invalid_fields:
                        plan_dates = {day["date"] for day in payload["plan"].get("days",[])}
                        fields["dates"] = list(dict.fromkeys([day for day in finding["dates"] if day in plan_dates]+fields["dates"]))
                    finding.update({key:value for key,value in fields.items() if key in invalid_fields})
                result = ReviewReport.model_validate(corrected)
            result = result.model_copy(deep=True)
            for finding in result.findings:
                finding.field_path = canonical_review_path(payload["plan"],finding.field_path)
            errors = review_reference_errors(result,payload)
            actual = result.model_dump(mode="json")
            if not errors:
                break
            self.store.save_agent_result(state["trip_id"],"review_invalid:"+digest([payload,state["generation"],state["run_id"],attempt]),
                {"run_id":state["run_id"],"attempt":attempt+1,"errors":errors,"result":actual})
            logging.getLogger(__name__).warning("Review references rejected: run=%s attempt=%s errors=%s",
                                               state["run_id"],attempt+1,dump(errors))
            if attempt:
                detail = "；".join(f"findings[{item['finding_index']}].{item['field']}={item['value']}"
                    if "finding_index" in item else item["message"] for item in errors[:3])
                raise ToolError(errors[0]["code"],"检验 Agent 的引用自动纠正一次后仍未通过，审查未采用："+detail)
            previous = actual
            indices = sorted({item["finding_index"] for item in errors})
            request = {"invalid_findings":[{"finding_index":index,**previous["findings"][index]} for index in indices],
                       "validation_errors":errors,"field_path_index":review_field_index(payload["plan"]),
                       "plan_dates":[day["date"] for day in payload["plan"].get("days",[])],
                       "evidence_catalog":payload.get("evidence_catalog") or
                           [{"evidence_id":identity} for identity in payload["evidence_index"]]}
        result = correct_transfer_arithmetic(result,payload)
        remaining = review_semantic_errors(result,payload)
        if remaining:
            raise ToolError("review_semantics","检验引用修正后的结论仍与审查范围或日期事实矛盾，未采用："+"；".join(remaining[:3]))
        self.store.save_agent_result(state["trip_id"],"review:"+digest([payload,state["generation"]]),
                                     {"result":result.model_dump(mode="json")})
        return result
