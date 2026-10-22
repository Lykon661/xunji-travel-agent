# 循迹：基于小红书攻略的多 Agent 旅行规划

[![CI](https://github.com/Lykon661/xunji-travel-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Lykon661/xunji-travel-agent/actions/workflows/ci.yml)

用户勾选几篇小红书攻略并填写需求（日期、城市、人数、预算等），系统生成一份**每个地点都有来源、交通和时间都经过地图查证**的逐日行程。单机单用户，前后端合并为一个本地服务。

技术栈：FastAPI · LangGraph（SQLite checkpointer）· Pydantic · Vue 3 + TypeScript · 高德 MCP · 飞猪 CLI · MediaCrawler（可选）

## 它解决什么问题

直接让大模型写行程，常见问题是：地点编造、同一景点换个名字出现两次、交通时间拍脑袋、闭馆日照样安排。这个项目的做法是：

- **只用用户选的攻略**：每个景点/餐厅必须能追溯到原文片段或图片序号，模型不能自己加地点。
- **事实交给工具，计算交给代码**：坐标、路线耗时、天气来自高德；车次、酒店、门票来自飞猪；时间线、预算、去重、日期覆盖由代码计算。模型只做"选择和排序"。
- **独立审查 + 门禁**：完整行程生成后由检验 Agent 在新上下文里重审，审查报告绑定行程哈希和依据哈希，任何变化都要重审才能保存。
- **必要时只问一题**：遇到必须由用户取舍的冲突（例如必去景点排不下），用 LangGraph `interrupt()` 暂停，原地问一道具体问题，回答后从检查点继续。只问回答能改变的事；同一问题回答两次仍无法解决就不再追问，带着阻断项交给用户处理。
- **用消融实验验证设计**：逐个关掉检验 Agent、全程去重、代码校验，在固定的外部数据上比较违反数，见下文[消融评测](#消融评测)。

## 架构

```text
需求 + 所选攻略
   │
   ▼
攻略 Agent ──► 统一地点身份 / 去重
   │
   ▼
行程 Agent（分日初案）──► 资源 Agent（交通、住宿）──► 行程 Agent（时间线、餐饮）──► 资源 Agent（门票）
                                                                                     │
                                                                                     ▼
                                                                    代码规则检查 ──► 检验 Agent 重审
                                                                                     │
                                       ┌───────────── 可自动修复（≤2 轮）◄─────────────┤
                                       │                                             ├─ 需要用户取舍 → interrupt 问一题
                                       ▼                                             │
                                  责任 Agent 局部重算                                  └─ 通过 → 候选 → 用户确认 → 原子保存
```

四个 Agent 共享同一个 `AgentRuntime`（[backend/app/agents/runtime.py](backend/app/agents/runtime.py)），区别在于指令、输入上下文、输出 schema 和可用工具。调度、状态合并、版本校验由 `Planner`（LangGraph 图）负责，它是代码而不是 Agent。

详细设计见：

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：图节点、状态、数据存储、各模块职责
- [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md)：为什么这样设计、Agent 与代码规则的边界、中断恢复的一致性保证，以及已知的不足

## 快速开始（Windows）

需要 Python 3.11+ 和 Node.js 22.19+。

```bat
start.bat
```

首次运行会自动创建虚拟环境、安装依赖、打包前端，然后在后台启动服务并打开 http://127.0.0.1:8000 。`stop.bat` 停止服务，`build.bat` 重新打包前端。日志在 `backend/data/server.log`。

不配置任何外部服务也能启动和浏览界面；要真正生成行程，需要在页面右上角配置：

| 配置 | 用途 | 必需 |
|---|---|---|
| 模型配置 | 攻略解析、排序、审查（任意 OpenAI 兼容接口或订阅登录，经 pi-ai 调用） | 是 |
| 高德 Key（Web 服务） | 地点定位、路线耗时、天气 | 是 |
| 飞猪 API Key | 车次、航班、酒店、门票 | 否，缺失时这些项显示为未知 |
| 小红书采集器 | 搜索攻略 | 否，可手动粘贴正文和图片 |

Key 只保存在本机 `backend/data/`，读取接口不回传密钥。更详细的接入说明见 [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md)。

需求表中可以勾选**往返交通自行安排**：出发地往返的车次、航班由用户自己定，系统只规划目的地内的行程和多个目的地之间的交通，不再查询、检查或追问往返班次。

开发模式（前端热更新）：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-backend.ps1   # :8000
powershell -ExecutionPolicy Bypass -File .\scripts\start-frontend.ps1  # :5173
```

## 测试

```bat
test.bat
```

依次运行后端单元测试、model-runtime 测试和前端类型检查。CI 在每次 push 时于 Windows + Python 3.11 + Node 22 上运行同样的检查并完整构建前端。

当前状态（以 CI 为准，见上方徽章）：

| 检查 | 规模 |
|---|---|
| 后端 `unittest` | 61 个测试文件，约 600 个用例 |
| model-runtime `node --test` | 8 个用例 |
| 前端 `vue-tsc --noEmit && vite build` | 类型检查与生产构建 |

**测试覆盖范围说明**：后端测试用 mock 替代了模型、高德、飞猪和采集器，覆盖的是规划流程、状态机、规则校验、审查门禁、中断恢复、版本冲突和存储。真实模型输出质量和外部接口的实际返回无法在 CI 中验证，对应的手工检查清单在 [docs/MANUAL_TESTING.md](docs/MANUAL_TESTING.md)。

## 消融评测

为了回答「为什么拆成四个 Agent、检验 Agent 值不值」，用 [backend/eval/](backend/eval/README.md) 在同一批需求、同一份外部数据上，逐个关掉一项设计做对比：

- **固定外部数据**：高德、飞猪的真实返回录制一次后回放，各组差异只来自代码和模型。
- **代码自动判分**：日期覆盖、城市顺序、重复地点与餐厅（含用例中列出的已知别名）、来源缺失、时间线冲突、营业/闭馆、午餐、步行上限、预算。判分时总是按完整规则检查，与运行时关掉了哪项无关。
- **测试需求**：5 条核心用例，其中 2 条是地点池紧张的压力用例（两篇攻略用不同名字写同一批地点，同一家餐厅靠近多天景点），3 条分别覆盖别名重复、周一闭馆、攻略天数多于行程。往返交通设为用户自行安排。

结果（2026-10，DeepSeek 与 GPT-5.6 Luna 两个模型 × 5 条需求 × 每组 1 次，共 40 次规划；所有违反都出现在系统判为「可确认」的方案里，即全部漏检）：

| 组别 | 关掉的设计 | 含违反的方案 | 违反处数 |
|---|---|---|---|
| 完整系统 | 无 | **0 / 10** | 0 |
| `no_reviewer` | 检验 Agent（报告固定为通过，代码规则仍可否决） | 3 / 10 | 4 |
| `no_dedupe` | 跨攻略的统一地点身份、全程地点与餐厅去重 | **8 / 10** | 12 |
| `no_code_checks` | 代码硬约束校验（日期、时间线、预算、营业窗口、重复等） | 1 / 10 | 1 |

- **全程去重是关键，且不能交给模型审查**：关掉后 10 份中 8 份出现重复，其中 9 处是同一家餐厅午晚餐连推，3 处是同一景点隔天再排（如广州塔与小蛮腰）。这一组检验 Agent 始终开着，12 处一处都没发现。
- **检验 Agent 补的是代码做不到的语义判断**：关掉后出现「陈家祠 / 陈氏书院」「上下九 / 上下九步行街」这类在高德是不同 POI、代码无法合并的别名重复；完整系统中为 0。代价是模型调用约翻倍（DeepSeek 12.4→23.6 次，Luna 14.6→31.2 次）。
- **代码硬约束校验在这批用例中只观察到 1 处差异**：每天的时间线本身由确定性代码生成，模型很少有机会排出时间或日期错误，它在这里主要是兜底。

样本量小（每个模型每组 1 次），上面只说明趋势。评测过程中还暴露并修复了几处真实缺陷：飞猪新版价格字段导致全部车次被丢弃、机场多航站楼无法定位、午餐排程规则遗漏、检验 Agent 重复扣除进站缓冲、无法通过回答解决的问题被反复追问。运行方法与判分细节见 [backend/eval/README.md](backend/eval/README.md)。

## 目录结构

```text
backend/
  app/
    planner.py          LangGraph 图定义、执行入口与人工确认 gate（协调器）
    workflow/           TripState 定义；提交 / 回退 / 重开决策 / 删除等用户入口
    nodes/              图节点实现：攻略、候选、资源、定位、日程、审查-修复循环
    agents/             四个 Agent：guide / resource / route / reviewer，共享 runtime
    reviewing.py        审查报告校验、哈希绑定、门禁判断
    rules.py            预算、时间线、日期覆盖、锁定、用户条件约束等确定性规则
    constraints.py      偏好条件约束：抽取校验、覆盖关系、按日期/地点查询
    conflicts.py        代码给出的取舍问题：当天装不下时的冲突项与可行选项，攻略地点用完时补查已导入攻略
    decisions.py        确认门禁、用户决策处理
    dining.py places.py routing.py   餐饮去重、地点身份、路线排序
    adapters.py         模型 / 高德 MCP / 飞猪 CLI 适配与缓存
    store.py            SQLite 存储，版本号与乐观锁
    main.py             FastAPI 路由与 SSE
    ablation.py         消融实验开关（默认全部不生效）
  eval/                 消融评测：测试需求、接口录制回放、自动判分、批量运行与汇总
  tests/                单元测试（unittest，不访问外部服务）
frontend/src/           Vue 3 单页应用
model-runtime/          Node 子进程：模型调用桥接、飞猪 CLI 包装
scripts/                安装、启动、停止、打包、测试脚本
```

## 数据

`backend/data/`（已在 `.gitignore` 中）：

- `trips.sqlite`：旅行、攻略、历史版本、运行事件、外部查询记录
- `checkpoints.sqlite`：LangGraph 检查点，用于中断后恢复
- `uploads/`：攻略图片

备份时先停止服务再复制整个目录。

## 已知限制

- 只在 Windows 上运行和测试过（服务锁使用 `msvcrt`，飞猪 CLI 以 `.cmd` 入口调用）。
- 报价、库存、营业时间都是查询时刻的参考，不代表预订保证；不支持下单。
- 审查依赖模型判断，可能误判或漏判；代码规则能拦住的只有数值、去重、日期、版本这类确定性问题。消融评测的样本量还很小，结论只说明趋势。
- MediaCrawler 为非商业许可，采集功能仅供个人学习使用。
