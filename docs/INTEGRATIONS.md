# 外部服务接入

所有 Key 都可以在页面右上角对应的配置面板保存（存到 `backend/data/*.json`，立即生效，优先于 `.env`）。旅行任务运行中不允许修改配置。读取配置的接口不会返回密钥。

## 模型

页面右上角「模型配置」：先添加服务商（接口根地址 + API Key，或订阅 OAuth 登录），再添加模型并「选择用于旅行规划」。图片攻略可以另选一个支持图片的模型，不选则复用规划模型。

调用经由 `model-runtime/` 中的 Node 子进程（`@earendil-works/pi-ai` 1.0.4），后端通过 stdin/stdout 与常驻 worker 通信（[backend/app/llm.py](../backend/app/llm.py)、[model-runtime/bridge.mjs](../model-runtime/bridge.mjs)）。模型只能返回经 Pydantic 校验的 JSON；格式错误会带着具体字段错误重试一次，仍失败则报错，不采用无法校验的输出。

兼容旧的环境变量 `TEXT_BASE_URL / TEXT_MODEL / TEXT_API_KEY` 和 `VISION_*`：没有网页保存的配置时，从 `.env` 导入。

## 高德 MCP

在高德开放平台创建「Web 服务」类型的 Key，填到「高德配置」，或在 `backend/.env` 中设置 `AMAP_API_KEY`（也兼容 `AMAP_MAPS_API_KEY`）。

- 使用官方 Streamable HTTP MCP 地址，启动会话后读取工具 schema，再按 schema 调用地点搜索、详情、天气、路线工具。
- 一个规划任务复用一个 MCP 会话；调用串行，单任务上限 300 次。遇到限流（HTTP 429、`ACCESS_TOO_FREQUENT`、`CUQPS_HAS_EXCEEDED_THE_LIMIT`）按退避重试。
- 缓存：路线 5 分钟，天气 15 分钟，其他最多 30 分钟，内存中最多 512 项。
- 多个 POI 候选无法唯一匹配时保留歧义交给用户选择，不默认取第一个。
- 未配置时，路线耗时标为「估算」（市内 45 分钟、车站接驳 60 分钟），候选不能通过审查。

## 飞猪 CLI

`scripts/setup.ps1` 会在 `model-runtime/` 中安装官方 `@fly-ai/flyai-cli`，也可以使用全局安装的版本。在「飞猪配置」中填写 API Key（或 `.env` 的 `FLYAI_API_KEY`）。

用到的命令：`search-train`、`search-flight`、`search-hotel`、`search-poi`。成功结果缓存 10 分钟，「更新报价」会绕过缓存。

Windows 下 CLI 退出时可能触发 libuv 的 `UV_HANDLE_CLOSING` 断言，所以直接调用 Node 入口并预加载 [model-runtime/flyai-exit.cjs](../model-runtime/flyai-exit.cjs)，让输出和句柄清理完再退出。用户输入不经 shell 拼接。

费用口径（[backend/app/rules.py](../backend/app/rules.py) `resource_cost`）：

- 车票/机票 `ticketPrice` 按每人每段计；
- 酒店 `pricePerNight` 按每间每晚计，多晚报价不假设每晚相同；
- 只有 `price` 没有口径、或只有 `minPrice` 起价的，不计入已知费用；
- 门票缺价时按用户设置可默认 0 元，但标记为 `assumed_free`，和供应商确认免费（`reported_free`）区分；
- 未知费用单独列出，不当作 0 元，也不声称预算已满足。

## 小红书采集（可选）

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -WithCrawler
```

把 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) 固定在提交 `d6f7c5bb906b6dac40ddf343ef9e26438a3de092`，安装到 `external/MediaCrawler`，使用独立的 Python 3.11 虚拟环境。安装时用 [backend/crawler_profile.py](../backend/crawler_profile.py) 打补丁移除不需要的依赖（OpenCV、Pandas、词云等）。

之后在页面「小红书采集器」面板安装、扫码登录、启动。主要限制：

- 只能从前端手动搜索触发，规划时不自动采集；
- 每轮一个关键词，最多读三篇详情，请求间隔至少 6 秒，两轮之间至少 1 分钟；
- 遇到 401/403/429/461/471 或验证码，立即停止并写入保护锁，需要用户确认账号恢复后手动解除。

不用采集器时，也可以在「参考攻略」中直接粘贴正文、上传图片。

MediaCrawler 是非商业学习许可；代码许可不等于获得了小红书或攻略作者的内容授权。
