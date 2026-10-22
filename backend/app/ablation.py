"""消融实验开关：默认全部开启（即不消融），只供 backend/eval 的评测脚本关闭某项设计。

开关名表示「被关掉的设计」：
- reviewer：不调用检验 Agent，审查报告固定为 pass，只保留代码规则的否决。
- dedupe：不做跨攻略的统一地点身份（每条攻略引用各算一个地点），不做全程地点与餐饮去重。
- clustering：关掉代码空间分日，回到路线 Agent 自行分日和原定位预算。
- code_checks：不做代码硬约束校验（日期覆盖、城市顺序、时间线、预算、营业窗口、重复等），
  只靠行程 Agent 与检验 Agent 自身发现问题；为避免崩溃，仍会丢弃引用不存在的地点 ID。

评测脚本用 `use()` 在当前 asyncio 上下文中设置；也可用环境变量 TRAVEL_ABLATION=reviewer,dedupe 启动服务做人工对比。
"""
import os
from contextlib import contextmanager
from contextvars import ContextVar

SWITCHES = ("reviewer", "dedupe", "code_checks", "clustering")

_current: ContextVar[frozenset | None] = ContextVar("travel_ablation", default=None)


def parse(value: str | list[str] | None) -> frozenset:
    names = value.replace("+", ",").split(",") if isinstance(value, str) else (value or [])
    result = frozenset(name.strip().removeprefix("no_") for name in names if name and name.strip() and name.strip() != "full")
    unknown = sorted(result - set(SWITCHES))
    if unknown:
        raise ValueError("未知的消融开关：" + "、".join(unknown) + "；可用：" + "、".join(SWITCHES))
    return result


def active() -> frozenset:
    current = _current.get()
    return current if current is not None else parse(os.getenv("TRAVEL_ABLATION", ""))


def disabled(name: str) -> bool:
    return name in active()


@contextmanager
def use(names):
    token = _current.set(parse(names) if not isinstance(names, frozenset) else names)
    try:
        yield
    finally:
        _current.reset(token)
