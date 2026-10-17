# 循迹：基于小红书攻略的多 Agent 旅行规划

[![CI](https://github.com/Lykon661/xunji-travel-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Lykon661/xunji-travel-agent/actions/workflows/ci.yml)

用户勾选几篇小红书攻略并填写需求（日期、城市、人数、预算等），系统生成一份**每个地点都有来源、交通和时间都经过地图查证**的逐日行程。单机单用户，前后端合并为一个本地服务。

技术栈：FastAPI · LangGraph（SQLite checkpointer）· Pydantic · Vue 3 + TypeScript · 高德 MCP · 飞猪 CLI · MediaCrawler（可选）

## 它解决什么问题

直接让大模型写行程，常见问题是：地点编造、同一景点换个名字出现两次、交通时间拍脑袋、闭馆日照样安排。这个项目的做法是：

- **只用用户选的攻略**：每个景点/餐厅必须能追溯到原文片段或图片序号，模型不能自己加地点。
- **事实交给工具，计算交给代码**：坐标、路线耗时、天气来自高德；车次、酒店、门票来自飞猪；时间线、预算、去重、日期覆盖由代码计算。模型只做"选择和排序"。
- **独立审查 + 门禁**：完整行程生成后由检验 Agent 在新上下文里重审，审查报告绑定行程哈希和依据哈希，任何变化都要重审才能保存。
- **必要时只问一题**：遇到必须由用户取舍的冲突（例如必去景点排不下），用 LangGraph `interrupt()` 暂停，原地问一道具体问题，回答后从检查点继续。

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

最近一次本地运行（2026-10-17，Windows 11，Python 3.14.4 / 3.11.9，Node 24.16）：

| 检查 | 结果 |
|---|---|
| 后端 `unittest`（57 个文件） | 573 个用例全部通过，约 27 秒 |
| model-runtime `node --test` | 8 个用例全部通过 |
| 前端 `vue-tsc --noEmit && vite build` | 通过 |
| 启动服务并调用 API（建旅行、查询、404/422 校验）+ 浏览器打开首页 | 正常，控制台无报错 |

**测试覆盖范围说明**：后端测试用 mock 替代了模型、高德、飞猪和采集器，覆盖的是规划流程、状态机、规则校验、审查门禁、中断恢复、版本冲突和存储。真实模型输出质量和外部接口的实际返回无法在 CI 中验证，对应的手工检查清单在 [docs/MANUAL_TESTING.md](docs/MANUAL_TESTING.md)。

## 目录结构

```text
backend/
  app/
    planner.py          LangGraph 图定义与各节点（协调器）
    agents/             四个 Agent：guide / resource / route / reviewer，共享 runtime
    reviewing.py        审查报告校验、哈希绑定、门禁判断
    rules.py            预算、时间线、日期覆盖、锁定等确定性规则
    decisions.py        确认门禁、用户决策处理
    dining.py places.py routing.py   餐饮去重、地点身份、路线排序
    adapters.py         模型 / 高德 MCP / 飞猪 CLI 适配与缓存
    store.py            SQLite 存储，版本号与乐观锁
    main.py             FastAPI 路由与 SSE
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
- 审查依赖模型判断，可能误判或漏判；代码规则能拦住的只有数值、去重、日期、版本这类确定性问题。
- `planner.py` 仍然偏大（约 3500 行），拆分计划见 [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md#已知不足)。
- MediaCrawler 为非商业许可，采集功能仅供个人学习使用。
