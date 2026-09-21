# dlmodel

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-%E2%89%A53.11-blue)](backend/pyproject.toml)
[![Release](https://img.shields.io/badge/release-v0.4.2-green)](https://github.com/sreyun/dlmodel/tags)
[![CI](https://github.com/sreyun/dlmodel/actions/workflows/docker-swr.yml/badge.svg)](https://github.com/sreyun/dlmodel/actions/workflows/docker-swr.yml)
[![Image](https://img.shields.io/badge/image-Huawei%20SWR-orange)](#部署与镜像)

**简体中文** · [English](README.en.md) · [日本語](README.ja.md) · [Русский](README.ru.md)

**dlmodel 是一个自托管的 LLM 模型权重下载管理器**：在网络受限的环境下，用多源回退 + aria2 多连接把 ModelScope / Hugging Face 模型可靠地拉下来，按 vLLM 布局落盘，Web UI 全程管理队列、断点与暂停恢复，下载完成一键对接 vLLM / Ollama 推理。

<!-- TODO: 此处插入界面截图/GIF（任务列表、下载进度、暂停恢复），建议路径 docs/screenshot.png，有真实图后替换下行：
     ![dlmodel 界面截图](docs/screenshot.png)
-->

## 功能特性

- 🔄 **多源自动回退** — `auto` 源先探测 ModelScope、再回落 HF 镜像；每个源内部按 **aria2 多连接 → 单流 HTTP 断点续传 → 原生 SDK** 逐级降级，无需代理即可直连 hf-mirror.com / 魔搭
- ⏸️ **暂停 / 恢复 / 断点续传** — `paused` 是一等状态：安全停止网络流而非取消，保留任务、进度、已下载文件、断点与日志，恢复后续传（aria2 `continue` / HTTP `Range` / SDK `.temp` / Ollama 服务端缓存）
- 📊 **下载队列** — 多任务并发（1–8）、单文件多连接、进度 / 速率 / ETA、失败重试、瞬时 TLS / 网络错误指数退避重试
- 🛡️ **优雅重启** — 停机时 `queued` / `running` 任务统一停放落库，下次启动自动重新排队并续传，升级 / 重启不丢任务
- 🔑 **Bearer Token 鉴权** — 常量时间比较 + 启动门禁（过短 / 弱口令 Token 直接拒绝启动）+ 认证失败限速（频繁错误尝试返回 429）
- 🤖 **推理服务集成** — Compose profile 一键拉起 Ollama / vLLM；UI 内基于已下载模型路径生成 vLLM 启动命令（模型路径 + 端口，shell 安全引用），支持模型库扫描与 Ollama 本地模型列表 / 拉取
- 📱 **消息推送** — 钉钉 / 飞书 / 企业微信群机器人，按事件（开始 / 完成 / 失败 / 取消）开关，出站地址白名单校验，Token 脱敏回显
- 🗃️ **零外部依赖持久化** — 任务与设置存 SQLite（WAL），`docker compose up -d` 即完整部署，无数据库 / 消息队列 / Node 构建链

**技术栈**：FastAPI + uvicorn + httpx（async）· aria2 JSON-RPC · aiosqlite · hugging_face / modelscope SDK 兜底 · 免构建原生 JS SPA（无外部静态资源，CSP 全锁定）

## 快速开始

以下最短路径适用于 Linux / macOS / Windows（PowerShell 7；Windows PowerShell 5.1 中 `cp` 亦可用）。

```bash
# 1. 克隆仓库
git clone https://github.com/sreyun/dlmodel.git
cd dlmodel

# 2. 配置（关键一步：设置强 ADMIN_TOKEN）
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # 生成一个强 Token，写入 .env

# 3. 启动（默认使用华为云 SWR 加速镜像；若组织为私有仓库需先 docker login swr.cn-east-3.myhuaweicloud.com）
docker compose up -d

# 4. 打开浏览器，粘贴 ADMIN_TOKEN 登录
#    http://<服务器IP>:8080
```

登录后新建下载任务（源选 `auto`、目标选 `vLLM`），等待完成后在任务卡片查看落盘路径（模型库页也可查），即可交给 vLLM / Ollama 加载。

> ⚠️ **安全**：默认 Web 监听 `0.0.0.0:8080`。暴露到公网前必须在 `.env` 中设置足够长的随机 `ADMIN_TOKEN`（不足 12 位或命中弱口令表的 Token 会让服务**拒绝启动**），并建议置于反向代理 / HTTPS 之后。`ARIA2_RPC_SECRET` 同样须改为唯一值。

## 架构概览

```
浏览器 SPA (frontend/, 免构建原生 JS, 短轮询)
   │  Bearer Token
   ▼
FastAPI 路由层 ── /api/downloads* /api/settings /api/models /api/services/* /api/health
   │
   ├─ DownloadQueue：asyncio worker 池（并发 1–8）、状态机
   │     queued → running → completed | failed | cancelled
   │        ▲ 暂停（安全停流，保留断点/进度）    │
   │        └────────────── paused ◄─────────────┘──► 恢复回 queued 续传
   │
   ├─ 下载器：source auto → ModelScope ⇁ HF 镜像；每个源 aria2 → HTTP Range → SDK 回退
   ├─ 控制面：cancel / pause 协作式停止（on_progress / on_log 信号点），重试 / 回退不会吞掉
   ├─ aria2 容器（JSON-RPC，16 连接/文件，.aria2 控制文件续传）
   └─ SQLite (WAL)：tasks + settings；停机停放 queued/running，启动时统一重新排队续传
卷：./data/models（权重，与 vLLM/Ollama 共享）· ./data/app（DB）· ./data/aria2
```

状态一致性由 CAS 更新（`UPDATE … WHERE status IN (…)`）收敛：暂停 / 恢复 / 取消在 queued 与 running 之间任意交错都只会有一方生效；worker 上报前复核任务仍被自己持有，被取消 / 回收的任务不会再改写状态。终态与暂停统一释放内存跟踪（取消 / 暂停标志、aria2 gid 映射、通知去重集合），长期运行无泄漏。

## 配置参考

全部配置通过仓库根目录 `.env` 提供（可选项括号内为默认值）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ADMIN_TOKEN` | `changeme` | UI / API 的 Bearer Token，**务必修改**；不足 12 位或弱口令会拒绝启动 |
| `MODEL_ROOT` | `/models` | 模型权重根目录（compose: `./data/models`） |
| `DATA_DIR` | `/data` | SQLite `app.db` 与设置（compose: `./data/app`） |
| `HF_ENDPOINT` | `https://hf-mirror.com` | HF 镜像地址 |
| `HF_TOKEN` | 空 | 拉取 gated 模型（镜像端点须支持透传） |
| `MODELSCOPE_API_TOKEN` | 空 | 魔搭访问 Token |
| `ARIA2_RPC_URL` | `http://aria2:6800/jsonrpc` | aria2 控制面地址 |
| `ARIA2_RPC_SECRET` | `dlmodel` | 须与 aria2 服务一致，生产必改 |
| `DOWNLOAD_CONCURRENCY` | `2` | 同时下载任务数（运行中可在 API 热调 1–8） |
| `ARIA2_CONNECTIONS` | `16` | 单文件下载连接数 |
| `DOWNLOAD_RETRIES` | `3` | 瞬时 TLS / 网络错误额外重试次数（指数退避，0 = 不重试） |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama 地址（profile 或远程） |
| `VLLM_BASE_URL` | `http://vllm:8000` | vLLM 地址（profile 或远程） |
| `VLLM_MODEL` | 空 | `--profile vllm` 使用的**具体已下载仓库路径** |
| `NOTIFY_DINGTALK_WEBHOOK` / `NOTIFY_FEISHU_WEBHOOK` / `NOTIFY_WECOM_WEBHOOK` | 空 | 群机器人 Webhook（UI 设置优先于环境变量） |
| `NOTIFY_ON_COMPLETED` / `NOTIFY_ON_FAILED` | `1` | 完成 / 失败推送开关 |
| `NOTIFY_ON_STARTED` / `NOTIFY_ON_CANCELLED` | `0` | 开始 / 取消推送开关 |
| `DLMODEL_IMAGE` | SWR `:latest` | 生产镜像；回滚时钉住 `:vX.Y.Z` |
| `ARIA2_IMAGE` | SWR 加速的 `p3terx/aria2-pro` | aria2 镜像（v0.4.1 起默认走 SWR 加速地址，免代理拉取） |
| `WEB_BIND` / `WEB_PORT` | `0.0.0.0` / `8080` | 生产 Web 绑定与端口 |
| `PUID` / `PGID` | `0` / `0` | aria2 运行身份；需与模型卷可写权限匹配 |

设置页（写 SQLite）中的 `HF_TOKEN` / `MODELSCOPE_API_TOKEN` 优先于同名环境变量，便于网页端热改。

<details>
<summary>数据与目录布局</summary>

| 主机路径 | 容器路径 | 用途 |
|----------|----------|------|
| `./data/models` | `/models`（`MODEL_ROOT`） | 模型权重，与 vLLM / Ollama 共享 |
| `./data/app` | `/data`（`DATA_DIR`） | SQLite `app.db`、设置 |
| `./data/aria2` | `/config` | aria2 配置、会话与 tracker 日志 |

磁盘布局：vLLM / HF 落盘 `{MODEL_ROOT}/hf/<org>/<repo>/`；Ollama 落盘 `{MODEL_ROOT}/ollama`（映射到 Ollama 容器 `/root/.ollama`）。任务记录请在 UI 删除（清理终态仅删记录、不删已下载文件），或自行清理上述目录。

</details>

## API 概览

除 `GET /api/health` 外，所有端点均需 `Authorization: Bearer <ADMIN_TOKEN>`。

| 方法与路径 | 说明 |
|------------|------|
| `POST /api/downloads` | 新建任务。请求体 `{name, source: auto\|modelscope\|huggingface\|ollama, target: vllm\|ollama, revision?}`；同名同目标已有活跃任务（含 `paused`）时返回 `400` |
| `GET /api/downloads` | 任务列表（含 `progress_bytes` / `total_bytes` / `speed_bps`） |
| `GET /api/downloads/{id}` | 单个任务 |
| `POST /api/downloads/{id}/pause` | 暂停 `queued` / `running` 任务（`400` = 状态非法） |
| `POST /api/downloads/{id}/resume` | 恢复 `paused` 任务回队列 |
| `POST /api/downloads/{id}/cancel` | 取消（活跃态与 `paused` 均可） |
| `POST /api/downloads/{id}/retry` | 重试 `failed` / `cancelled` 任务，重入队列并从断点续传 |
| `DELETE /api/downloads/{id}` | 删除任务记录（终态与 `paused` 可删，仅删记录不动磁盘） |
| `POST /api/downloads/cleanup?status=…` | 批量清理终态记录（默认 `completed,cancelled,failed`） |
| `GET /api/downloads/{id}/events` | SSE 进度流（仅 Bearer 头，见[常见问题](#常见问题--故障排查)第 4 条） |
| `GET` / `PUT /api/settings` | 读写设置（Token / Webhook 脱敏回显，明文不回传；PUT 仅改非 `None` 字段） |
| `POST /api/settings/notify-test` | 推送测试消息 |
| `GET /api/models` · `DELETE /api/models/{id}` | 模型库扫描（路径 / 占用大小）与删除（**会删除磁盘上的模型目录**，任务记录删除则不动文件） |
| `GET /api/services/ollama/health` · `…/models` · `POST …/pull` | Ollama 健康、本地模型列表、拉取（tag 可覆盖） |
| `GET /api/services/vllm/health` · `…/launch-command` | vLLM 健康、启动命令生成 |
| `POST /api/auth/verify` | 校验 Token |
| `GET /api/health` | 存活探针（免鉴权，供容器 healthcheck） |

队列并发热调：`PUT /api/settings` 改 `download_concurrency` 后由队列 `resize`（1–8）即时生效。

## 部署与镜像

- **生产**：`docker compose up -d` —— 默认镜像 `swr.cn-east-3.myhuaweicloud.com/sreyun/dlmodel:latest`（含 aria2 镜像均已走 SWR 加速），`pull_policy: always` 每次 `up` 拉新；固定版本或回滚在 `.env` 钉 `DLMODEL_IMAGE=…:vX.Y.Z`
- **发布 CI**：推送 `v*` 标签触发 GitHub Actions「Build and push Huawei SWR」，同时更新 `:latest`；需配置 Secrets `HW_ACCESS_KEY` / `HW_SECRET_KEY`（可选 Variable `HW_SWR_NAMESPACE`，默认 `sreyun`），并预先在 [SWR 控制台](https://console.huaweicloud.com/swr)（华东-上海一）创建组织；手动重跑：Actions → Run workflow

### 可选推理服务

管理与下载不依赖推理服务，需要时再通过 Compose profile 启用（默认不启动）：

```bash
docker compose --profile ollama up -d      # 本机 API: http://127.0.0.1:11434
docker compose --profile vllm up -d        # OpenAI 兼容: http://127.0.0.1:8000，需 NVIDIA Container Toolkit
```

- `VLLM_MODEL` 必须填**已下载的具体仓库路径**（如 `/models/hf/Qwen/Qwen3-0.6B`），不能是 `/models/hf` 根目录；下载完成后写入 `.env` 并 recreate `vllm`
- vLLM 镜像默认 `vllm/vllm-openai:latest` + `runtime: nvidia`，`HUGGING_FACE_HUB_TOKEN` 由 `HF_TOKEN` 透传
- Ollama 拉取的模型保存在 `{MODEL_ROOT}/ollama`（映射到容器 `/root/.ollama`），与下载页的 Ollama 源共用

## 常见问题 / 故障排查

**1. aria2 不可用 / 下载只有单流？**
RPC 失败时自动降级为单流 HTTP 续传（日志提示「aria2 不可用」），任务不会失败但仍会下载——多流不可用通常只有两种原因：**网络问题**或**权限不足**。默认 `PUID=0` / `PGID=0` 正是为规避共享卷写权限问题；宿主机目录权限过严时 aria2 无法写 `.aria2` 控制文件与模型卷，请保证容器用户可写 `./data/models` 与 `./data/aria2`。修复后重新下载，任务消息不再出现「aria2 不可用」即恢复；也可用 `docker compose ps` / `docker compose logs aria2` 确认 aria2 容器状态。

**2. HF 模型 401 / 403（gated）？**
在设置页或 `.env` 配置 `HF_TOKEN`（接受模型协议后到 HF 官网生成），并确认所用镜像端点支持 Token 透传（hf-mirror.com 支持）；ModelScope 独有模型优先用 `auto` 或显式 `modelscope` 源。

**3. 点了「继续」提示「目标目录仍有未结束的下载」？**
这是 ModelScope 的已知限制：其 `snapshot_download` 跑在阻塞线程中无法即时中断，暂停实现为「停止跟踪并落 `paused`」，后台线程真正结束后才释放目标目录。此时**立即恢复同一目标目录会被拒绝（409）以防并发双写损坏**——稍候片刻再点继续即可。其它来源（HF / aria2 / HTTP / Ollama）暂停后可立即恢复续传。

**4. SSE 端点存在但前端为什么用轮询？**
`GET /api/downloads/{id}/events` 仅接受请求头形式的 Bearer Token，而浏览器原生 `EventSource` 无法自定义请求头，故前端改用 1s（活跃）/ 4s（空闲）短轮询；如需实时推送可自行集成 `fetch` + ReadableStream。

**5. 端口被占用？**
改 `.env` 的 `WEB_PORT`（配合 `WEB_BIND` 可调绑定地址）后重新 `docker compose up -d`。

**6. 磁盘空间不足（No space left on device）？**
失败原因会透传到任务消息与推送通知。注意：**磁盘满与权限拒绝不自动重试**（重试无意义），清理空间后对失败任务点「重试」即可从断点续传。下载前可用 `df -h` 预估（大模型动辄数十 GB）。

**7. 重启容器后 running 中的任务哪去了？**
启动时队列会把所有 `queued` / `running` 任务统一停放回队列并重新调度（消息「服务重启后重新排队…」，优雅停机与 `docker compose down` 等硬停止都覆盖）；aria2 按在途传输恢复、HTTP / SDK 靠磁盘断点续传。`paused` 任务保持暂停状态不受重启影响，等你手动点「继续」。

**8. 任务反复失败想查原因？**
每个任务的状态消息（`message`）会显示失败原因或当前阶段（来源解析、回退、第几次重试），`GET /api/downloads/{id}` 可直接查看；服务端日志（`docker compose logs web`，JSON 格式）含带任务 ID 的完整时间线，Token 等敏感信息已脱敏。

## 路线图

以下为考虑中的方向（非承诺）：SSE 实时推送前端集成（`fetch` 流方案）、下载带宽限制、批量清单导入、定时清理策略、任务标签与分组。

## 贡献

欢迎 Issue 与 PR：提交请附带测试覆盖，行为变更请同步更新本文档及各语言版本（`README.en.md` / `README.ja.md` / `README.ru.md`）；commit message 遵循 Conventional Commits（`feat:` / `fix:` / `chore:`…）。

## 许可证

[MIT License](LICENSE) © 2026 sreyun
