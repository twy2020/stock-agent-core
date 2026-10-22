# Stock Agent Core

股票分析系统使用的独立 Agent 执行服务。负责模型调用、Tools/MCP/Skills 调度与授权、SSE 事件和持久化运行记录，以及供应商、模型和应用管理。技术栈为 Python、Flask、SQLite 和 Waitress。

这是股票项目的 Core 副本。历史通用 CAD/OCR 适配器仍在代码中，但只有经过应用授权的能力才会暴露，股票部署不会自动开放其他业务接口。

## 与主项目的关系

- [stock-analysis](https://github.com/twy2020/stock-analysis)：对话 SaaS、股票数据引擎、图表与部署编排。
- [stock-agent-core](https://github.com/twy2020/stock-agent-core)：本仓库，由主项目作为 `agent-core/` 子模块引用。

两个仓库均为私有。完整股票系统推荐按主项目 README 部署；其初始化脚本会生成股票专用调用方、工具、Skills 和权限，本服务在该部署中使用 `18210` 端口。

## 架构边界

AI 根据需求生成查询 schema 和研究方案，通过工具获得摘要后解读。行情查询、指标、数学统计和回测属于外部数据引擎，不能让语言模型凭空计算，也不把整库行情送进上下文。

Core 不导入 SaaS 用户数据库，不拥有行情库，也不替代 RAG、搜索、OCR 或业务授权。调用方认证用户并提供可信的租户、会话和用户上下文，Core 按服务身份再次检查模型和工具权限。

## 独立启动

需要 Python 3.12，以下为仓库根目录下的 PowerShell 示例：

```powershell
py -3.12 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
New-Item -ItemType Directory -Force .local
Copy-Item config.example.json .local/core.json
```

修改 `.local/core.json` 中的模型、API Base URL、调用方及工具授权。示例中的模型 Key 使用 `AGENT_CORE_MODEL_KEY` 环境变量引用，在启动进程的环境中安全配置它；调用方令牌应使用独立随机值，并将其 SHA-256 摘要配置到 `tokenSha256`。也可以使用服务器端凭据文件引用。不要提交真实配置。

```powershell
./.venv/Scripts/python.exe -m agent_core --config .local/core.json
```

示例默认绑定 `127.0.0.1:18200`，可通过配置或 `--host`、`--port` 调整。相对路径须按实际启动目录核实；容器使用容器内路径。示例含通用 CAD/OCR 调用方，独立使用时应删除或重新配置不需要的项。

## 管理控制台

默认入口为 `http://127.0.0.1:18200/admin`。全新数据库初始账号 `admin / admin123`，首次登录强制修改为 10–128 字符密码。凭据以哈希存入 SQLite，使用可撤销的 HttpOnly 会话；旧管理员 Bearer 令牌不能代替账密登录。

按供应商 → 账号 → Key 配置凭据，再绑定模型；填写真正的 API 前缀，不要填网站首页。保存后分别运行文本和工具调用测试，再给应用授权模型及选择默认模型。供应商可单独设置 HTTP(S) 代理；Docker 访问主机代理使用 `host.docker.internal`。

## 主要接口

| 接口 | 用途 |
| --- | --- |
| `GET /health` | 服务健康，需要服务 Bearer 认证 |
| `POST /v1/chat/runs` | 创建持久化执行，返回运行 ID 与事件地址 |
| `GET /v1/runs/{id}` | 读取执行状态 |
| `GET /v1/runs/{id}/events` | SSE 事件，支持事件 ID 续接 |
| `POST /v1/runs/{id}/cancel` | 取消执行 |
| `GET /v1/runs/{id}/results/{operationId}` | 按权限读取完整工具结果 |
| `/v1/models`、`/v1/chat/completions` | 受控 OpenAI-compatible 模型网关 |
| `/admin` | 账密管理控制台 |

服务请求使用 `Authorization: Bearer ...`，不是管理员 Cookie；服务配置分别控制模型、Tools、MCP、Skills、插件、并发和额度，聊天请求不能自行指定任意接口或密钥。

详细 API 语义见 [实现参考](docs/implementation-reference.md)，控制台与配置见 [Core 控制台和网关](docs/core-console-and-gateway.md)。历史资料中的旧图片路径和只读管理页说明，以当前实现及该控制台文档为准。

## 执行与恢复

- 同一 SQLite 数据库只允许一个 Core 进程，命令入口使用文件锁；不要用多个独立 Gunicorn worker 共享该库。
- 内部提供有限执行线程及调用方并发配额，不是分布式调度器。
- 相同调用方、租户和 UUID 的相同请求保持幂等，SSE 断线不重新执行模型。
- 工具结果以受限摘要进入模型，完整结果和图像通过受控引用读取；付费和有副作用工具不盲目重放。
- HTML 响应、不完整 SSE 和半截工具调用明确失败；仅特定连接建立前错误有限恢复，不自动更换模型。
- 重启后的 RUNNING 任务标为 `WORKER_RESTARTED`，不自动重放付费执行；排队任务可继续。取消不能撤销已产生的外部调用和费用。
- RAG、搜索和 OCR 需实际接入并授权；股票系统默认不启用 RAG。

## 验证与安全

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -v
```

常规测试使用临时数据库和本地模拟模型，不调用付费供应商；部分可选外部契约检查可能依赖独立项目，不属于生产运行依赖。

仓库不含 `.local/`、真实配置、API Key、服务令牌、数据库、日志和原有 Git 历史。`config.example.json` 仅为占位模板。保留数据库和凭据文件才能保留管理员密码、运行记录及服务授权；公网部署需自行配置 HTTPS、访问边界和受保护备份。私有仓库同样不能存放生产密钥。
