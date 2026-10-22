"""消融实验批量运行与汇总。

在 backend 目录下运行（需先停止本机服务，避免与评测争用模型和高德配额）：

    .venv\\Scripts\\python.exe -m eval.run_ablation --mode record --variants full --repeats 1
    .venv\\Scripts\\python.exe -m eval.run_ablation --mode auto --repeats 3
    .venv\\Scripts\\python.exe -m eval.run_ablation --report eval/runs/<目录>

每次运行使用独立的临时数据库和内存检查点，不读写 backend/data/trips.sqlite；
模型、高德、飞猪配置沿用 backend/data 中现有设置。结果逐行追加到 results.jsonl，中断后用同一 --out 重跑会跳过已完成的组合。
"""
import argparse
import re
import asyncio
import csv
import difflib
import json
import logging
import shutil
import statistics
import sys
import traceback
from datetime import datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver

from app import ablation
from app.llm import worker as model_worker
from app.planner import Planner
from app.store import Store, now

from . import replay
from .metrics import VIOLATIONS, evaluate, visit_duplicates

HERE = Path(__file__).resolve().parent
VARIANTS = {"full": "全部开启", "no_reviewer": "关掉检验 Agent", "no_dedupe": "关掉统一地点身份与全程去重",
            "no_code_checks": "关掉代码硬约束校验"}
UNAVAILABLE = re.compile(r"额度|重新登录|模型权限")
TRANSIENT = {"model_network", "model_format", "amap_network", "amap_rate_limit", "flyai_timeout"}
RUN_LEVEL = {"decisions", "code_option_decisions", "model_calls", "elapsed_seconds"}
OUTCOMES = {"selection": "得到候选", "decision_limit": "追问超过上限", "failed": "运行失败",
            "no_candidate": "未形成候选", "timeout": "超时", "invalid_case": "用例无效"}


def auto_answer(question: dict) -> str:
    """Deterministic stand-in for the user, identical across variants."""
    options = [option for option in question.get("options", []) if option.strip()]
    if question.get("id", "").startswith(("place-", "hub-")) and options:
        return options[0]
    recommendation = question.get("recommendation", "").strip()
    match = next((option for option in options if option in recommendation), None)
    if options:
        return match or max(options, key=lambda option: difflib.SequenceMatcher(None, option, recommendation).ratio())
    return recommendation or "你决定，按推荐处理"


async def wait_run(planner, run_id, timeout):
    task = planner.tasks.get(run_id)
    if task is None:
        return False
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
        return False
    except TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True


async def run_one(case, variant, repeat, out, cassette, args):
    work = out / "work" / f"{case['id']}-{variant}-{repeat}-{uuid4().hex[:6]}"
    store = Store(work)
    row = {"case": case["id"], "variant": variant, "repeat": repeat, "started_at": now(), "outcome": "failed",
           "error": "", "decisions": 0, "resumes": 0, "model_calls": 0, "model_seconds": 0.0}
    started = perf_counter()
    before = dict(cassette.stats)
    planner = None
    undo = None
    run_ids = []
    # Warnings from the app (rejected model output, fallbacks) are kept only if the run fails.
    captured = CapturedLogs()
    logging.getLogger("app").addHandler(captured)
    with ablation.use(variant):
        try:
            planner = Planner(store, InMemorySaver())
            undo = replay.install(planner, cassette)
            ask = planner.models.ask

            async def counted(*call_args, **call_kwargs):
                begun = perf_counter()
                try:
                    return await ask(*call_args, **call_kwargs)
                finally:
                    row["model_calls"] += 1
                    row["model_seconds"] += perf_counter() - begun
            planner.models.ask = counted

            trip = store.create_trip(case["title"], case["requirements"])
            source_ids = []
            for index, guide in enumerate(case["guides"]):
                doc = {"id": uuid4().hex, "identity": f"{case['id']}:{index}", "title": guide["title"], "url": "",
                       "text": guide["text"], "images": [], "city_hint": guide.get("city_hint", ""), "likes": 0, "favorites": 0,
                       "published_at": None, "collected_at": now(), "kind": "manual", "imported": True,
                       "extraction": None, "error": None}
                source_ids.append(store.add_source(trip["id"], doc)["id"])
            deadline = perf_counter() + args.timeout
            run_id = await planner.submit(trip["id"], trip["version"], selected_source_ids=source_ids)
            run_ids.append(run_id)
            while True:
                if await wait_run(planner, run_id, max(1, deadline - perf_counter())):
                    row["outcome"] = "timeout"
                    break
                run = store.run(run_id)
                current = store.trip(trip["id"])
                doc = current["document"]
                if run["status"] == "failed":
                    details = run.get("document") or {}
                    row.update(outcome="failed", error=f"{details.get('code', '')}: {details.get('message', '')}"[:300])
                    # 模型或地图网络抖动：像用户点「继续本次任务」一样从检查点恢复，不重新规划。
                    if details.get("code") in TRANSIENT and row["resumes"] < args.max_resumes:
                        row["resumes"] += 1
                        print(f"    … {details.get('code')}，第 {row['resumes']} 次继续本次任务", flush=True)
                        try:
                            run_id = await planner.submit(trip["id"], current["version"], selected_source_ids=source_ids, resume=True)
                            run_ids.append(run_id)
                            continue
                        except Exception as exc:
                            row["error"] += f"；继续任务失败：{exc}"[:200]
                    break
                pending = doc.get("pending") or {}
                kind = pending.get("kind")
                if kind == "selection":
                    row["outcome"] = "selection"
                    break
                if kind == "sources":
                    row.update(outcome="no_candidate", error="；".join(doc.get("warnings") or [])[:300])
                    break
                if kind in {"decision", "interview"} and pending.get("questions"):
                    if row["decisions"] >= args.max_decisions:
                        row["outcome"] = "decision_limit"
                        break
                    question = pending["questions"][0]
                    response = {"round_id": pending["round_id"], "answers": {question["id"]: auto_answer(question)},
                                "note": "", "confirm": False, "draft_revision": pending.get("draft_revision")}
                    row["decisions"] += 1
                elif kind == "interview_confirmation":
                    response = {"round_id": pending["round_id"], "answers": {}, "note": "", "confirm": True}
                else:
                    row.update(outcome="invalid_case", error=f"意外的等待类型：{kind or run['status']}")
                    break
                run_id = await planner.submit(trip["id"], current["version"], selected_source_ids=source_ids,
                                              interview_response=response)
                run_ids.append(run_id)
            doc = store.trip(trip["id"])["document"]
            candidate = (doc.get("candidates") or [None])[0]
            pool = {stop["id"] for source in store.sources(trip["id"]) for stop in (source.get("extraction") or {}).get("stops", [])}
            # 先保存方案再判分：判分出错时也能事后核查，并用 --report 之外的方式重新判分。
            plans = out / "plans"
            plans.mkdir(parents=True, exist_ok=True)
            (plans / f"{case['id']}-{variant}-{repeat}.json").write_text(json.dumps(
                {"requirements": doc["requirements"], "candidate": candidate, "decision_history": doc.get("decision_history", []),
                 "warnings": doc.get("warnings", [])}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            # 由代码给出选项、回答后直接执行的追问数（其余追问由模型生成和解析）。
            row["code_option_decisions"] = sum(bool(entry.get("code_option")) for entry in doc.get("decision_history", []))
            try:
                row.update(evaluate(doc["requirements"], candidate, pool, case.get("aliases", []), case.get("expected_constraints")))
            except Exception as exc:
                # 判分脚本的问题不是规划结果：保留规划结局，单独标记，续跑时会重新执行。
                row.update(outcome="score_error", error=f"判分失败 {type(exc).__name__}: {exc}"[:300])
                logging.getLogger(__name__).error("scoring %s/%s/%s failed\n%s", case["id"], variant, repeat, traceback.format_exc())
        except Exception as exc:
            row.update(outcome="failed", error=f"{type(exc).__name__}: {exc}"[:300])
            logging.getLogger(__name__).error("run %s/%s/%s crashed\n%s", case["id"], variant, repeat, traceback.format_exc())
        finally:
            logging.getLogger("app").removeHandler(captured)
            if planner is not None:
                await planner.close()
            if undo:
                undo()
            cassette.flush()
    if row["outcome"] in {"failed", "no_candidate"} and UNAVAILABLE.search(row["error"]):
        row["outcome"] = "model_unavailable"
    if row["outcome"] in FAILED_OUTCOMES:
        save_failure(out / "failures" / f"{case['id']}-{variant}-{repeat}.json", store, run_ids, row, captured.records)
    row["elapsed_seconds"] = round(perf_counter() - started, 1)
    row["model_seconds"] = round(row["model_seconds"], 1)
    row["replay"] = {key: value - before.get(key, 0) for key, value in cassette.stats.items()}
    if not args.keep_work:
        shutil.rmtree(work, ignore_errors=True)
        if work.parent.is_dir() and not any(work.parent.iterdir()):
            work.parent.rmdir()
    return row


FAILED_OUTCOMES = {"failed", "no_candidate", "timeout", "model_unavailable", "score_error"}


class CapturedLogs(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append({"time": datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
                             "level": record.levelname, "logger": record.name, "message": record.getMessage()[:4000],
                             "traceback": logging.Formatter().formatException(record.exc_info)[:8000] if record.exc_info else ""})


def save_failure(path: Path, store, run_ids, row, logs):
    """Keep what a failed run leaves before its work folder is deleted: run states, progress events and app warnings."""
    runs = []
    for run_id in dict.fromkeys(run_ids):
        try:
            run = store.run(run_id)
        except Exception as exc:
            runs.append({"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"})
            continue
        events, after = [], 0
        while batch := store.events(run_id, after):
            events += batch
            after = batch[-1]["id"]
        runs.append({"run_id": run_id, "status": run.get("status"), "document": run.get("document"), "events": events})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"case": row["case"], "variant": row["variant"], "repeat": row["repeat"], "outcome": row["outcome"],
                                "error": row["error"], "runs": runs, "logs": logs}, ensure_ascii=False, indent=1, default=str),
                    encoding="utf-8")


def load_rows(file: Path):
    if not file.is_file():
        return []
    return [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines() if line.strip()]


async def run_all(args):
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))["cases"]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [case for case in cases if case["id"] in wanted]
    else:
        cases = [case for case in cases if args.suite in case.get("suites", ["all"])]
    variants = [name.strip() for name in args.variants.split(",") if name.strip()]
    for name in variants:
        ablation.parse(name)
    out = Path(args.out) if args.out else HERE / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    results = out / "results.jsonl"
    done = {(row["case"], row["variant"], row["repeat"]) for row in load_rows(results)}
    (out / "config.json").write_text(json.dumps({**vars(args), "cases": [case["id"] for case in cases], "variants": variants},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
    total = len(cases) * len(variants) * args.repeats
    finished = len(done)
    unavailable = 0
    try:
        # 先按重复轮次、再按用例交替各组，时间漂移（模型服务负载、未命中的实时查询）对各组影响相同。
        for repeat in range(1, args.repeats + 1):
            for case in cases:
                cassette = replay.Cassette(Path(args.cassettes) / f"{case['id']}.json", args.mode)
                for variant in variants:
                    if (case["id"], variant, repeat) in done:
                        continue
                    print(f"[{finished + 1}/{total}] {case['id']} · {variant} · 第{repeat}次 ...", flush=True)
                    row = await run_one(case, variant, repeat, out, cassette, args)
                    if row["outcome"] == "score_error":
                        print(f"    → {row['error']}（方案已保存，本次不记录）", flush=True)
                        continue
                    if row["outcome"] == "model_unavailable":
                        # 额度或登录问题不是规划结果：不写入，续跑时会重新执行这一组合。
                        unavailable += 1
                        print(f"    → 模型不可用（{row['error'][:80]}），本次不记录", flush=True)
                        if unavailable >= 2:
                            print("连续两次模型不可用，已停止本批。恢复额度或重新登录后，用同一个 --out 续跑。", flush=True)
                            break
                        continue
                    unavailable = 0
                    with results.open("a", encoding="utf-8") as output:
                        output.write(json.dumps(row, ensure_ascii=False) + "\n")
                    finished += 1
                    print(f"    → {OUTCOMES.get(row['outcome'], row['outcome'])}，违反 {row.get('total_violations', '-')}，"
                          f"可确认 {row.get('confirmable', '-')}，模型调用 {row['model_calls']} 次，用时 {row['elapsed_seconds']}s"
                          + (f"，错误：{row['error']}" if row["error"] else ""), flush=True)
                if unavailable >= 2:
                    break
            if unavailable >= 2:
                break
    finally:
        model_worker.close()
    report(out)


def rescore(out: Path, cases_file: str):
    """Recount visit duplicates from saved plans with the cases' known aliases; no model or API calls.

    Only dup_visit and the totals derived from it change. Other metrics need data that is not kept
    after a run (e.g. the source stop pool), so they keep their recorded values.
    """
    aliases = {case["id"]: case.get("aliases", []) for case in json.loads(Path(cases_file).read_text(encoding="utf-8"))["cases"]}
    file = out / "results.jsonl"
    rows = load_rows(file)
    backup = file.with_name("results.before-rescore.jsonl")
    if not backup.exists():
        # 只备份第一次重判前的原始结果，重复重判不覆盖它。
        shutil.copy(file, backup)
    changed = 0
    for row in rows:
        plan = out / "plans" / f"{row['case']}-{row['variant']}-{row['repeat']}.json"
        if not plan.is_file() or not row.get("has_plan"):
            continue
        candidate = json.loads(plan.read_text(encoding="utf-8")).get("candidate") or {}
        days = [day for day in (candidate.get("draft") or {}).get("days") or [] if isinstance(day, dict)]
        visits = [(day, item) for day in days for item in day.get("activities", []) if item.get("kind") == "visit"]
        with_aliases, strict = visit_duplicates(visits, aliases.get(row["case"], []))
        previous = row.get("dup_visit", 0)
        previous_strict = row.get("dup_visit_strict", previous)
        row.update(dup_visit=with_aliases, dup_visit_strict=strict)
        row["total_violations"] = row.get("total_violations", 0) - previous + with_aliases
        row["escaped_violations"] = row["total_violations"] if row.get("confirmable") else 0
        changed += with_aliases != previous
        if strict != previous_strict:
            print(f"注意：{row['case']}/{row['variant']}/{row['repeat']} 原口径重复数 {previous_strict} 与重算 {strict} 不一致")
    file.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(f"{out}：重新判分 {len(rows)} 行，{changed} 行的重复景点数因已知别名改变；原结果另存为 results.before-rescore.jsonl")
    report(out)


def mean(values):
    values = [value for value in values if isinstance(value, (int, float))]
    return statistics.fmean(values) if values else None


def fmt(value, digits=2):
    return "-" if value is None else f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def report(out: Path):
    rows = load_rows(out / "results.jsonl")
    if not rows:
        print("还没有结果")
        return
    variants = list(dict.fromkeys(row["variant"] for row in rows))
    metrics = [*VIOLATIONS, "dup_visit_strict", "total_violations", "escaped_violations", "confirmable", "reviewer_blockers",
               "oracle_code_blockers", "decisions", "code_option_decisions", "model_calls", "elapsed_seconds",
               "constraints_expected", "constraints_extracted", "constraints_matched", "constraints_active_form"]
    summary = {}
    for variant in variants:
        group = [row for row in rows if row["variant"] == variant]
        planned = [row for row in group if row.get("has_plan")]
        visits = sum(row.get("visits", 0) for row in planned)
        meals = sum(row.get("meal_options", 0) for row in planned)
        # 每轮重复各算一个总数，再取均值和标准差，体现模型随机性。
        per_repeat = {}
        for row in planned:
            per_repeat.setdefault(row["repeat"], []).append(row.get("total_violations", 0))
        totals = [sum(values) for values in per_repeat.values()]
        summary[variant] = {
            "runs": len(group), "plans": len(planned),
            "outcomes": {name: sum(row["outcome"] == name for row in group) for name in OUTCOMES if any(row["outcome"] == name for row in group)},
            "mean": {name: mean([row.get(name) for row in (group if name in RUN_LEVEL else planned)]) for name in metrics},
            "sum": {name: sum(row.get(name) or 0 for row in planned) for name in metrics},
            "dup_visit_rate": sum(row.get("dup_visit", 0) for row in planned) / visits if visits else None,
            "dup_meal_rate": sum(row.get("dup_meal", 0) for row in planned) / meals if meals else None,
            "plans_with_violation": sum(row.get("total_violations", 0) > 0 for row in planned),
            "confirmable_with_violation": sum(row.get("escaped_violations", 0) > 0 for row in planned),
            "violations_per_repeat": totals,
            "violations_per_repeat_mean": mean(totals),
            "violations_per_repeat_stdev": statistics.stdev(totals) if len(totals) > 1 else None,
        }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    with (out / "results.csv").open("w", encoding="utf-8-sig", newline="") as output:
        fields = list(dict.fromkeys(key for row in rows for key in row if key != "replay"))
        writer = csv.DictWriter(output, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    repeats = len({row["repeat"] for row in rows})
    cases = len({row["case"] for row in rows})
    lines = [f"# 消融实验结果", "", f"- 用例 {cases} 条 × 每组 {repeats} 次；结果目录 `{out}`",
             "- 违反均为代码自动判定（见 eval/metrics.py），判分始终按完整规则进行，与运行时关掉了哪项设计无关。",
             "- 「漏检」= 系统判为可确认（审查通过且无阻断）的方案里仍然存在的违反数。", "",
             "## 运行结果", "", "| 组别 | 运行数 | 得到方案 | " + " | ".join(OUTCOMES.values()) + " |",
             "|---|---|---|" + "---|" * len(OUTCOMES)]
    for variant, item in summary.items():
        lines.append(f"| {VARIANTS.get(variant, variant)} | {item['runs']} | {item['plans']} | "
                     + " | ".join(str(item["outcomes"].get(name, 0)) for name in OUTCOMES) + " |")
    labels = {"date_coverage_error": "日期覆盖错误", "city_order_error": "城市顺序错误", "dup_visit": "重复安排地点（含已知别名）", "dup_visit_strict": "其中仅按高德地点/名称识别",
              "dup_meal": "重复推荐餐厅", "sourceless_visit": "无来源地点", "time_conflict": "时间线冲突",
              "opening_violation": "营业/闭馆冲突", "lunch_missing": "未留午餐", "walking_over_limit": "步行超限",
              "over_budget": "超预算", "user_constraint_violation": "违反用户条件", "total_violations": "违反合计", "escaped_violations": "漏检违反",
              "confirmable": "可确认方案", "reviewer_blockers": "检验 Agent 额外阻断", "oracle_code_blockers": "完整代码规则阻断",
              "decisions": "追问次数", "code_option_decisions": "其中代码给出选项", "model_calls": "模型调用", "elapsed_seconds": "用时(秒)",
              "constraints_expected": "应抽取的偏好条件", "constraints_extracted": "实际抽取的偏好条件", "constraints_matched": "其中种类正确",
              "constraints_active_form": "仍生效的表单条件"}
    lines += ["", "## 指标合计（只统计得到方案的运行）", "", "| 指标 | " + " | ".join(VARIANTS.get(v, v) for v in variants) + " |",
              "|---|" + "---|" * len(variants)]
    for name in metrics:
        aggregate = "mean" if name in RUN_LEVEL else "sum"
        lines.append(f"| {labels[name]}{'（全部运行均值）' if aggregate == 'mean' else ''} | "
                     + " | ".join(fmt(summary[v][aggregate][name], 1 if aggregate == "mean" else 0) for v in variants) + " |")
    lines.append("| 重复安排率（重复数/景点数） | " + " | ".join(
        "-" if summary[v]["dup_visit_rate"] is None else f"{summary[v]['dup_visit_rate']:.1%}" for v in variants) + " |")
    lines.append("| 餐厅重复率（重复数/推荐数） | " + " | ".join(
        "-" if summary[v]["dup_meal_rate"] is None else f"{summary[v]['dup_meal_rate']:.1%}" for v in variants) + " |")
    lines.append("| 含违反的方案数 | " + " | ".join(str(summary[v]["plans_with_violation"]) for v in variants) + " |")
    lines.append("| 判为可确认却含违反 | " + " | ".join(str(summary[v]["confirmable_with_violation"]) for v in variants) + " |")
    lines.append("| 每轮违反合计 均值±标准差 | " + " | ".join(
        fmt(summary[v]["violations_per_repeat_mean"], 1) + ("" if summary[v]["violations_per_repeat_stdev"] is None else
        "±" + fmt(summary[v]["violations_per_repeat_stdev"], 1)) for v in variants) + " |")
    failures = [row for row in rows if row["outcome"] in {"failed", "timeout", "invalid_case", "no_candidate"}]
    if failures:
        lines += ["", "## 未完成的运行", "", "| 用例 | 组别 | 次 | 结果 | 错误 |", "|---|---|---|---|---|"]
        lines += [f"| {row['case']} | {row['variant']} | {row['repeat']} | {OUTCOMES.get(row['outcome'])} | {row['error'].replace('|', '/')} |"
                  for row in failures]
    misses = sum(row.get("replay", {}).get("live", 0) for row in rows)
    lines += ["", f"回放说明：累计 {misses} 次查询未命中录制而走了实时接口（auto 模式会补录）。"
              "若希望各组外部数据完全一致，请先 record 一轮，再用 replay 模式跑。"]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description="旅行规划四 Agent 消融实验")
    parser.add_argument("--cases", default=str(HERE / "cases.json"))
    parser.add_argument("--suite", choices=("core", "all", "constraints"), default="core",
                        help="core：5 条核心用例，all：全部用例，constraints：偏好条件约束用例；--only 优先")
    parser.add_argument("--only", default="", help="只跑这些用例 ID，逗号分隔")
    parser.add_argument("--variants", default=",".join(VARIANTS), help="组别，逗号分隔；可组合，如 no_reviewer+no_dedupe")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--mode", choices=replay.MODES, default="auto")
    parser.add_argument("--cassettes", default=str(HERE / "cassettes"))
    parser.add_argument("--out", default="", help="结果目录；用已有目录可断点续跑")
    parser.add_argument("--timeout", type=int, default=1800, help="单次运行（含自动回答追问）的超时秒数")
    parser.add_argument("--max-decisions", type=int, default=8, help="自动回答追问的上限")
    parser.add_argument("--max-resumes", type=int, default=2, help="遇到模型网络/格式错误或地图网络错误时自动继续任务的次数")
    parser.add_argument("--keep-work", action="store_true", help="保留每次运行的临时数据库")
    parser.add_argument("--report", default="", help="只根据已有结果目录重新生成汇总")
    parser.add_argument("--rescore", default="", help="用已保存的方案和用例中的已知别名离线重算重复景点，不调用模型")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(HERE / "eval.log", encoding="utf-8"), logging.StreamHandler(sys.stderr)])
    if args.rescore:
        rescore(Path(args.rescore), args.cases)
        return
    if args.report:
        report(Path(args.report))
        return
    asyncio.run(run_all(args))


if __name__ == "__main__":
    main()
