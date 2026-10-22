# Agent Core Implementation Reference

Historical API and implementation notes. Use the root README for current setup
and the stock-system integration boundary.

Independent Flask/SQLite execution service extracted from the existing SaaS LangGraph loop. CAD and Chat SaaS call this service; Core never imports their databases, user models, or provider pools. OCR and RAG keep independent HTTP interfaces and data ownership.

## Start

Use Python 3.11 or newer, a virtual environment, and `pip install -r requirements.txt`. Copy `config.example.json` to a private configuration file and replace the placeholder model, endpoint, caller token digests, and project IDs. Caller tokens and model keys resolve from server-side credential references. Each caller must use a distinct token.

```powershell
F:/my_projects/agent-core/.venv/Scripts/python.exe -m agent_core --config F:/my_projects/agent-core/.local/core.json
```

Default bind is `127.0.0.1:18200`. Use an explicit configured `host` or `--host 0.0.0.0` when a Docker caller needs the host service, together with the intended firewall rules. Service endpoints, including `/health`, require `Authorization: Bearer …`. The public `/admin` page uses account/password login: the initial account is `admin / admin123`, and the first login must change the password before accessing any management API. New passwords require 10–128 characters. The sidebar provides password change and logout. Credentials are stored as scrypt hashes in the Core SQLite database; sessions use revocable HttpOnly, SameSite=Strict cookies with an eight-hour expiry (Secure on HTTPS). Five failed logins per source IP within five minutes temporarily block login. Administrator Bearer tokens, including legacy `adminTokenSha256`, are no longer accepted. Preserve the Core database volume to preserve credentials.

Only one process may own a database; the command-line entry point holds an OS file lock. Do not launch this app with multiple Gunicorn workers. Two execution threads and a caller's `maxActiveRuns` provide single-process limits, not cluster scheduling. No Redis, SaaS database, or vector database is required.

## Chat contract

`POST /v1/chat/runs` returns HTTP 202 and `{id,status,context,content,eventsUrl,expiresAt}`.

```json
{
  "request_id": "00000000-0000-4000-8000-000000000001",
  "model_profile": "primary",
  "messages": [{"role":"user","content":"查询当前知识库的钢材标准"}],
  "reasoning_enabled": false,
  "context": {"conversation_id":"conversation-id","knowledge_base_id":"kb-id","actor_id":"user-id","tenant_id":"default"}
}
```

The caller's server authenticates its users and supplies the delegated actor and conversation. Core binds every run and callback to the authenticated caller and its configured tenant. Caller-provided model endpoints, keys, callback URLs, or unknown fields are rejected. The external conversation ID is the stable thread association; business conversation editing and history remain in SaaS. `knowledge_base_id` may be null, and ordinary chat does not require a space.

The caller's fixed `ragCallback.url` receives `POST {operation,arguments,context,request_id}`; operations are `list_documents` and `recall`. Context includes Core-bound `caller_id`, `service_id`, `tenant_id` and the original delegated conversation/actor/knowledge-base fields. RAG owns resource authorization. Callback results retain original retrieval fields; Core adds monotonically increasing `label` values across recalls in one run. A `retrieval` SSE event has `{payload: <retrieval response>}`.

Message image content accepts only inlined `data:image/png|jpeg|webp;base64,…` URLs. Images are removed from generic model messages and exposed by index to an approved OCR tool. Core never fetches caller-provided image URLs. No approved OCR tool means image requests are rejected.

## CAD compatibility and evidence

The existing `/api/service/v1` contract supports capabilities, tools/execute, spaces, documents, search, package, promote, runs, events and cancel. A space is bound to an allowed `{project_id,drawing_id,revision}`. Run context must match its space. Temporary lexical retrieval preserves the exact chunk source metadata; promotion copies to a persistent read-only space without publishing into RAG.

CAD requests support `images`, `design_data`, and `source_refs` in addition to `requestId`, `question`, `spaceId`, `context`, `tools`, and `reasoningEnabled`. Runtime exposes session list/recall/load/unload tools and only the selected, config-approved HTTP tools. Effect `write` is never exposed to the model; `read`, `analyze`, `compute`, and `propose` remain separate declarations. Explicit service tool execution still requires the same allowlist and project authorization.

HTTP tools return the existing `{contractVersion:"1",status:"succeeded",data,…}` envelope. Image artifacts use `{type:"image",mediaType:"image/png",data:"base64",source_ref:{…}}`. Core stores these in the run's runtime image list and replaces bytes in tool events/model context with `imageIndex`. Tool-returned `data.design_data` is associated with those images; selecting candidate images therefore selects their original engineering context, rather than a model-generated reconstruction.

The OCR adapter uses only the independent jobs API. CAD selects `cad.analysis`; chat selects `image.describe`. Before submitting a paid job it validates the actual OCR v1 limits: 1–6 images, 5 MiB per image, 20 MiB total, 16 million pixels per image, and 64 KiB design data. Oversized engineering context is rejected with `VISION_DESIGN_SIZE_LIMIT`, never silently trimmed. The model should request a candidate's evidence context before OCR when a whole drawing exceeds that budget.

When global and candidate images carry different versions of the same drawing context, Core does not concatenate full copies. The `core-shared-design/1` encoding shares identical native sections through exact `$core_ref` pointers and binds every `image_id` to its original context. If needed, repeated object field names are stored once in `object_fields`; `$core_object:[field-set,values]` retains all original field values in order. The OCR request explains this lossless encoding and how to cite physical JSON pointers into its stored scalar values. Every engineering value, unit, constraint, source handle and coordinate survives round-trip expansion. Unique data that still exceeds 64 KiB fails before an OCR job is submitted.

The generic model receives structured evidence projections, never a cut-off escaped JSON string. Candidate IDs, evidence IDs, record IDs and image indexes remain ordinary JSON fields. Large source sections have `resultRef` and JSON `pointer` references; the `session_read_result` tool reads exact fields or pages inside the same run, including pages of dictionary keys. Authenticated callers can also retrieve a complete original tool result with `GET /v1/runs/{runId}/results/{operationId}` (or the CAD alias). Full evidence stays in durable run events, while model projections are limited to 24 KiB. Identical read-only tool requests in one run reuse their original result and image indexes without another callback. This cache never applies to proposal/write operations or OCR jobs and is invalidated before non-read tools and session mutations. Cached and paged results retain their originating tool grants; result reads recheck those grants so revocation cannot be bypassed through the cache or results endpoint.

Candidate evidence results already carry the new `evidence_id` for proposals and `imageIndex` for OCR. Repeating `cad_get_evidence` or loading the entire bundle into a session is unnecessary. Read-only CAD answers remain valid without submitting proposals. Core rejects unexecuted DSML/tool-call text. Every successful CAD run's complete event includes a receipt-derived `outcome` with `proposalSubmitted`, `proposalReceipts` and a factual notice. CAD separately determines whether its requested business outcome is complete or partial; prose is never a submission receipt.

An HTTP-success OCR response is not visual verification. Empty results fail with `VISION_EMPTY_RESULT`; CAD results without nonblank evidence tied to a requested image report `VISION_IMAGE_UNVERIFIED`. Coverage records which images have observations, while `field_verification` remains `unverified` for CAD to reconcile. Tool failures remain in the completion outcome, making the result partial even when a later native-data proposal succeeds. The native proposal receipt is retained for human review.

OCR's `source_ref` and `context` wire fields allow only bounded string maps. Core sends stable image/source identifiers, retains full original CAD bounding boxes and affine transforms locally, and restores them in the OCR tool-result envelope's `sources:[{image_id,source_id,source_ref}]`; `data` remains the original OCR result. OCR returns image-normalized rectangles; CAD performs any coordinate conversion deterministically using the restored transforms. The visual model neither receives these transforms nor measures engineering dimensions from pixels. Engineering parameters and constraints remain intact in `design_data`. Run-level `source_refs` are preserved alongside the result, not sent as unsupported OCR top-level fields. Source metadata is provenance, not a claim of calibrated confidence. CAD owns deterministic verification and human approval.

## Run durability and cancellation

`GET /v1/runs/{id}`, `GET /v1/runs/{id}/events`, and `POST /v1/runs/{id}/cancel` also have CAD aliases. SSE frames contain persistent per-run integer IDs; reconnect with `Last-Event-ID` or `?after=N`. Events include model, progress, delta, reasoning, usage, retrieval, answer_reset, tool_result, complete and error. Model events contain `id`, `name`, `model`, and `reasoningEnabled`.

Idempotency is scoped by caller, tenant and request UUID. Identical submissions reuse the run; changed payloads return 409. SSE disconnection never restarts execution. Cancellation atomically seals a run, preventing late token/result adoption and subsequent tool calls. An in-flight provider may continue work/billing until its HTTP response/timeout; external actions already completed are not undone. OCR cancellation is attempted when its polling loop observes cancellation.

On process restart, previously RUNNING runs fail with `WORKER_RESTARTED` and are not replayed. Queued runs may start normally. There is no automatic model/account rotation or retry of tools whose side effects or cost may already have occurred. Model SSE streams must explicitly finish with `stop` or `tool_calls`, matching the received content; a partial stream ending at socket EOF or `[DONE]` without a finish reason fails and cannot execute tools. Length limits, filtering and unknown finish reasons also fail without retry. Completed run payloads are cleared; events and results last 24 hours, and temporary evidence follows its TTL. This is durable execution history, not a cross-service business archive.

## Verify

```powershell
F:/my_projects/agent-core/.venv/Scripts/python.exe -m unittest discover -s tests -v
```

The suite starts a separate temporary Core process and deterministic local HTTP model/RAG/OCR providers, never a paid model. It covers scoped authentication and model/tool allowlists, idempotency/replay, durable cancellation, restart behavior, CAD space revision checks, RAG citation numbering, image bytes kept out of text models, original evidence metadata restoration, and admin redaction. The optional OCR schema contract test imports only the external checkout's schema in the test process; production Core has no import dependency on that project.
# 控制台与模型网关更新（2026-10-04）

`/admin` 已提供供应商→账号→Key、模型聚合、应用准用策略、MCP/Tools/Skills/插件管理与用量费用页面。Core 支持直连 VLM 和 OCR 两条可授权路径。OpenAI-compatible 接口为 `/v1/models`、`/v1/chat/completions`；OCR/RAG 的 MCP façade 保留原独立 API。完整配置、兼容范围、费用口径和验收说明见 [Core 控制台与网关](core-console-and-gateway.md)。以下早期说明若写“图片仅经 OCR”或“管理页只读”，以此新文档为准。

# Stock Research Core

This checkout is the stock-analysis Core copy. Its dedicated configuration is
generated by `../scripts/bootstrap_stock.py`, with stock-data-engine tools and
stock research skills. Start `../scripts/start-stock.ps1`; console port 18210.
See the root README. The reusable Core implementation notes below include historical
CAD/OCR adapters, which are not registered in the stock deployment.

