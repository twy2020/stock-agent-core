# Core 控制台、模型网关与公共能力

2026-10-12 更新。控制台入口 `/admin`，使用账号密码登录；默认 `admin / admin123`，首次登录必须改密才能访问管理功能。密码以 scrypt 哈希保存在 Core SQLite 中，登录 Cookie 为 HttpOnly、SameSite=Strict，有效期八小时；改密撤销所有旧会话。旧管理员令牌不再接受，服务调用方仍使用各自的 Bearer token。配置采用版本检查、活动任务/网关锁和原子落盘。生产使用单进程 SQLite；不启动多个进程共享一个数据库。

## 管理对象

供应商 `providers` → 供应商账号 `providerAccounts` → Key 引用 `providerKeys` → 模型别名 `models`。Key 保存 `apiKeyFile` 或 `apiKeyEnv`，控制台不读取或回显密钥内容。原来的内联 model profile 配置仍可加载。新模型通过 keyId 绑定层级；禁用任意上级会拒绝调用。发现模型仅预览，加入模型目录与授予应用是两个操作。

控制台提供各层级的新增、编辑、启停和删除入口，编辑在右侧属性栏完成。新增供应商可选择常用供应商预设；新增账号和 Key 自动关联当前上级。API Key 可直接粘贴录入或更换，留空保留旧值；高级选项仍可引用现有文件或环境变量。模型支持手动新增或“从供应商导入”，发现结果需确认能力、价格、别名后保存，导入默认停用。模型的价格全部清空后恢复未计价。

管理写接口均需已完成改密的管理员登录 Cookie，写请求携带 `X-Core-Admin-Request: 1`，并检查同源 Origin：

- `PATCH /admin/api/registry`：`{revision,kind,id,changes}`，保存允许编辑的配置字段。
- `POST /admin/api/provider-keys`：`{revision,id,accountId,name,enabled,secret}`，创建或更换密钥。密钥写入配置目录下独立的 `credentials/<随机名>.token`，配置和审计只保存引用/字段名，响应不回显内容。凭证文件应由主机权限保护，不是加密保险库。
- `DELETE /admin/api/registry/<kind>/<id>`：`{revision}`。仍被账号、Key、模型或调用方引用时返回 409，不做级联删除；调用方不允许从此接口删除。

以上操作返回最新 registry。并发版本冲突、文件被外部修改、Agent 或网关正在执行时拒绝修改。密钥保存失败会回收本次暂存文件；更换或删除 Key 不自动删除旧凭据文件，避免破坏可能共享的引用。调用方创建和凭证发放仍由部署配置管理，本轮控制台只编辑已有调用方。

当前 Agent 消费方只有 SaaS 与 CAD。OCR、RAG 作为公共能力提供方，不为它们建立额外的 Agent 消费身份。应用准用策略包含模型、HTTP 工具、MCP 服务、MCP 工具、Skills、插件、图片路径与输出/并发上限。

Skills 是管理员提供的有限指令，按调用方与能力域注入；不会自动授予工具。插件是能力分组及启停/授权单元，不运行任意 Python/JavaScript 安装脚本。MCP 支持 HTTP Streamable transport，不包含本机 stdio 子进程执行。

能力管理页支持名称/标识/说明搜索、启停筛选以及逐项新增、编辑、启停和删除。Tools 可配置地址、适配器、操作类型、超时、凭证引用与输入/输出 Schema；Skills 可维护指令和能力域；插件属性显示关联的 MCP、Tools 和 Skills，并可跳转编辑。将“所属插件”改为未选择可解除关联，删除仍由服务端检查全部引用。

MCP 工具使用逐项编辑器。先保存连接配置，再发现工具；勾选发现项并明确其操作类型后加入草稿，确认参数后保存。新发现的工具默认停用，既有工具更新参数时保留启停状态；发现不会修改应用授权，也不会信任远端 annotations 自动授予权限。HTTP 桥接可选择已有 Tool，自动填入它的操作类型和输入 Schema。

接入应用页管理既有 SaaS、CAD 的能力域、模型、图片路径、并发/输出限制、MCP 服务与具体工具、HTTP Tools、Skills、插件和 CAD 项目范围。授权依赖检查会提示缺少的服务/工具/插件授权、停用对象或域不匹配；这些是配置诊断，不代表服务连通性或模型质量。缺少依赖不会被自动补授权，运行时仍逐项校验。编辑中的版本固定在打开表单时，避免后台刷新掩盖并发修改；未保存时阻止直接切换页面或编辑其他资源，可保存或取消后继续。

## 图片路径

`service.visionPolicy`: `ocr`（默认兼容）、`direct`、`both`。`direct` 同时要求模型声明 `capabilities.vision:true`；只允许 OCR 时不能把图片经网关发往模型。只允许 direct 时 OCR 原始工具和 MCP 别名均不可用。

Agent 请求可指定聊天 `vision_mode` 或 CAD `visionMode`。未指定时，获准且所选模型支持视觉则 direct，否则 OCR。不会默认运行两次推理。Core 仅接收内联 PNG/JPEG/WebP，不替调用方抓取任意图片 URL。初始图片及后续 CAD 裁切图片均可作为真实图像消息发给 VLM，保留图像索引；发送图片不等于识读正确，工程字段仍为待审。

`capabilities.tools:false` 的模型只做模型回答，不发送工具 schema；可以用 OpenAI 兼容接口独立做视觉分析。需要 Agent 工具循环应选支持工具的模型。没有校准实验时不能把模型自评当作准确率。

## OpenAI 兼容接口

- `GET /v1/models`：返回调用方获准且层级均启用的模型别名。
- `POST /v1/chat/completions`：支持文本、内联图片、流式/非流式、受限生成参数和 function tools。
- 凭证是 Core 调用方 Bearer token；不能使用管理员 token 代替。
- `model` 填 Core 别名，如 `primary` 或已批准的 `vision-primary`。
- 网关返回模型产生的 function calls，由消费方执行；它不自动执行 Core 注册工具。Core Agent 循环继续使用 `/v1/chat/runs`、`/api/service/v1/runs` 与 SSE。

支持范围是 Chat Completions 子集，不声称支持 Responses、音频、文件上传或所有 OpenAI 可选字段。未知字段返回错误，避免悄悄丢弃参数。未授权请求与上游错误采用兼容 error envelope；流式断开不自动重试收费请求。

## MCP 注册与授权

Core façade: `POST /mcp/ocr`、`POST /mcp/rag`。支持 initialize、notifications/initialized、ping、tools/list、tools/call。`tools/call` 的 `params._meta` 需要 `{domain,context}`，context 与既有 Agent 协议相同，身份由 Bearer token 绑定。源服务保持原有 API，可独立部署。

每个调用方需要 `mcpServers:[serverId]` 和 `mcpTools:[mcp__serverId__toolName]` 双重批准。能力关联插件时还需要插件批准。HTTP 桥接额外保留原工具 grant，不能借 MCP 绕过原权限。远端 MCP 动态发现不等于批准，schema 变化会拒绝执行并要求管理员复核。

OCR façade 通过已有异步视觉任务适配器调用 OCR。对外 OCR tools/call 使用内联 `images`；Core Agent 内部别名使用证据图片索引。RAG façade 使用调用方现有 `ragCallback`，传递绑定租户、会话、知识库；不能借另一调用方的回调跨租户。SaaS 设置 `ragViaMcp:true` 后，旧 RAG 内置工具不再旁路授权。

当前 CAD 尚未绑定 SaaS 的长期知识库，因此不授予该 RAG callback。CAD 现有项目/图纸即时空间与会话检索仍可用；需要长期 RAG 时必须配置其自己的知识范围，不能复用 SaaS 租户身份。

## 用量、价格和账单

每次网关请求、每轮 Agent 模型调用分别记录真实 usage（含失败时已报告用量）。没有 usage 时记录 unknown，不估算 Token。旧运行没有补记模型账单，统计从启用此版本开始。OCR 内部模型用量不属于 Core 直连账本，不能把 Core 估算当作所有基建的总成本。

模型 `pricing` 可配置 `inputPerMillion`、`outputPerMillion`、`currency`。结果是当时配置价格的估算快照；未知价格不等于零费用，不同币种不相加。缓存价、阶梯价和特殊图像计费尚未自动折算，最终以供应商账单为准。

价格参考通过 SiliconFlow / DeepSeek 官方公开页面取得，保留来源、获取时间、阶梯与场景条件；不携带推理密钥，不自动覆盖模型价格。失败明确显示 unavailable/stale。模型 `/models` 未报告的能力和上下文保持未知。

供应商账号可配置 `billing.adapter:'openai-organization'` 与**单独的组织管理 Key 引用**，查询 organization costs（可能包含 Core 以外业务）。没有通用供应商账单标准，目前其他账单适配明确未配置/不支持，不伪造余额。SiliconFlow 公开价格已适配，账号余额查询未实现。

管理查询：`GET /admin/api/usage?caller=...&profile=...&start=<unix>&end=<unix>`，开始包含、结束不包含；每次最多展示/导出最近 100 条，按模型/账号/币种分组统计最多 1000 组。控制台日期按浏览器本地时区转换。详细财务报表、预算扣款与发票不在本版本范围。

## 维护与验收

测试：`.venv/Scripts/python.exe -m unittest discover -s tests -v`；JS 用 `node --check`。自动测试使用本地/模拟上游，覆盖授权、跨租户、模型和 MCP 协议、配置并发、视觉路径与费用缺失。真实供应商模型推理需要另外做业务质量验收；协议通过不等于图纸准确率达标。

当前迁移保留主模型别名与默认值，视觉配置来自 OCR 已配置模型。原配置保存在 `.local/core.before-console.json`。回退前先停止 Core 并确认无活动任务，再恢复备份配置；备份包含凭证引用及鉴权摘要，不应提交仓库。
