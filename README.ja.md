# dlmodel

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-%E2%89%A53.11-blue)](backend/pyproject.toml)
[![Release](https://img.shields.io/badge/release-v0.4.2-green)](https://github.com/sreyun/dlmodel/tags)
[![CI](https://github.com/sreyun/dlmodel/actions/workflows/docker-swr.yml/badge.svg)](https://github.com/sreyun/dlmodel/actions/workflows/docker-swr.yml)
[![Image](https://img.shields.io/badge/image-Huawei%20SWR-orange)](#デプロイとイメージ)

[简体中文](README.md) · [English](README.en.md) · **日本語** · [Русский](README.ru.md)

**dlmodel はセルフホスト型の LLM モデル重みダウンロードマネージャです**：ネットワークが制限された環境でも、マルチソースフォールバック + aria2 並列接続により ModelScope / Hugging Face のモデルを確実に取得し、vLLM レイアウトでディスクに保存します。Web UI でキュー・続き取り・一時停止／再開を一元管理し、ダウンロード完了後は vLLM / Ollama 推論へ直行できます。

<!-- TODO: UI スクリーンショット/GIF（タスク一覧、進捗、一時停止/再開）を挿入。推奨パス docs/screenshot.png -->

## 特徴

- 🔄 **マルチソース自動フォールバック** — `auto` ソースは ModelScope を先に試し、HF ミラーへフォールバック。各ソース内では **aria2 並列 → 単一ストリーム HTTP 続き取り → ネイティブ SDK** の順に段階的に降格し、プロキシなしで hf-mirror.com / ModelScope に直接接続できます
- ⏸️ **一時停止 / 再開 / 続き取り** — `paused` は第一級の状態です。取消ではなくネットワークストリームの安全な停止であり、タスク記録・進捗・ダウンロード済みファイル・ブレークポイント・ログを保持。再開すれば続きから取得します（aria2 `continue` / HTTP `Range` / SDK `.temp` / Ollama サーバー側キャッシュ）
- 📊 **ダウンロードキュー** — 複数タスク同時実行（1–8）、1 ファイル複数接続、進捗 / 速度 / ETA、失敗時のリトライ、一時的な TLS/ネットワークエラーに対する指数バックオフ・リトライ
- 🛡️ **グレースフル再起動** — 停止時に `queued` / `running` タスクを SQLite に退避し、次回起動時に自動で再キュー・続き取り。アップグレードや再起動でタスクを失いません
- 🔑 **Bearer トークン認証** — 定数時間比較 + 起動ゲート（短すぎる / 弱いパスワードのトークンは起動を拒否）+ 認証失敗のレート制限（429）
- 🤖 **推論サービス統合** — Compose プロファイルで Ollama / vLLM をワンコマースタート。UI はダウンロード済みモデルパスから vLLM 起動コマンドを生成（パス + ポート、シェル安全なクォート）。モデルライブラリスキャン、Ollama ローカルモデル一覧 / プルに対応
- 📱 **チャットボット通知** — DingTalk / Feishu（Lark）/ WeCom グループボット。イベント別の ON/OFF、送信先ホストの許可リスト検証、トークンのマスク表示
- 🗃️ **外部依存ゼロ** — タスクと設定は SQLite（WAL）に永続化。`docker compose up -d` だけで完全デプロイ（DB・メッセージキュー・Node ビルドチェーン不要）

**技術スタック**：FastAPI + uvicorn + httpx（async）· aria2 JSON-RPC · aiosqlite · huggingface_hub / modelscope SDK フォールバック · ビルド不要のネイティブ JS SPA（外部静的アセットなし、CSP 完全ロック）

## クイックスタート

Linux / macOS / Windows 対応。

```bash
# 1. クローン
git clone https://github.com/sreyun/dlmodel.git
cd dlmodel

# 2. 設定（重要：強い ADMIN_TOKEN を設定）
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # 強いトークンを生成して .env に記載

# 3. 起動（Huawei SWR 加速イメージを既定で使用。SWR 組織が非公開の場合は先に
#    docker login swr.cn-east-3.myhuaweicloud.com を実行）
docker compose up -d

# 4. ブラウザで ADMIN_TOKEN を貼り付けてログイン
#    http://<サーバーIP>:8080
```

ログイン後、ダウンロードタスクを作成（ソース `auto`、ターゲット `vLLM`）。完了するとタスクカードに保存パスが表示され（モデルライブラリページでも確認可）、vLLM / Ollama でそのままロードできます。

> ⚠️ **セキュリティ**：既定では `0.0.0.0:8080` にバインドされます。公開前に必ず `.env` で十分に長いランダムな `ADMIN_TOKEN` を設定してください（12 文字未満または弱いパスワードリストに該当するトークンは**起動を拒否**します）。リバースプロキシ / HTTPS 配下に置くことを推奨します。`ARIA2_RPC_SECRET` も一意な値に変更してください。

## アーキテクチャ

```
ブラウザ SPA (frontend/, ビルド不要ネイティブ JS, 短周期ポーリング)
   │  Bearer トークン
   ▼
FastAPI ルート ── /api/downloads* /api/settings /api/models /api/services/* /api/health
   │
   ├─ DownloadQueue：asyncio ワーカープール（同時数 1–8）、ステートマシン
   │     queued → running → completed | failed | cancelled
   │        ▲ 一時停止（ストリームを安全に停止、進捗/ブレークポイントを保持）
   │        └────── paused ◄─────┘──► 再開 → queued に戻り続き取り
   │
   ├─ ダウンローダ：source auto → ModelScope ⇁ HF ミラー；各ソース aria2 → HTTP Range → SDK
   ├─ 制御面：cancel / pause は協調停止（on_progress / on_log でシグナル検査、
   │           リトライやフォールバックに飲み込まれない）
   ├─ aria2 コンテナ（JSON-RPC、1 ファイル 16 接続、.aria2 制御ファイルで続き取り）
   └─ SQLite (WAL)：tasks + settings；停止時に queued/running を退避し起動時に再キュー
ボリューム：./data/models（重み、vLLM/Ollama と共有）· ./data/app（DB）· ./data/aria2
```

状態の整合性は CAS 更新（`UPDATE … WHERE status IN (…)`）で収束します：queued と running の間での一時停止 / 再開 / 取消のあらゆる競合でも、成立するのは一方のみ。ワーカーは報告前に所有権を再確認するため、取消済みタスクが状態を上書きすることはありません。終了状態と一時停止ではメモリ上の管理情報（取消/一時停止フラグ、aria2 gid マップ、通知重複排除セット）を統一解放し、長期稼働でもリークしません。

## 設定リファレンス

すべての設定はリポジトリ直下の `.env` で提供します（括弧内は既定値）：

| 変数 | 既定値 | 説明 |
|------|--------|------|
| `ADMIN_TOKEN` | `changeme` | UI / API の Bearer トークン。**必ず変更を**。12 文字未満 or 弱いパスワードは起動拒否 |
| `MODEL_ROOT` | `/models` | モデル重みのルート（compose: `./data/models`） |
| `DATA_DIR` | `/data` | SQLite `app.db` と設定（compose: `./data/app`） |
| `HF_ENDPOINT` | `https://hf-mirror.com` | HF ミラーのエンドポイント |
| `HF_TOKEN` | 空 | gated モデル用（ミラー側でトークン透過が必要） |
| `MODELSCOPE_API_TOKEN` | 空 | ModelScope アクセストークン |
| `ARIA2_RPC_URL` | `http://aria2:6800/jsonrpc` | aria2 制御面アドレス |
| `ARIA2_RPC_SECRET` | `dlmodel` | aria2 サービスと一致させること。本番では必須変更 |
| `DOWNLOAD_CONCURRENCY` | `2` | 同時ダウンロードタスク数（API 経由で 1–8 にホット調整可） |
| `ARIA2_CONNECTIONS` | `16` | 1 ファイルあたりの接続数 |
| `DOWNLOAD_RETRIES` | `3` | 一時的 TLS/ネットワークエラーの追加リトライ回数（指数バックオフ、0 = 無効） |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama アドレス（プロファイル or リモート） |
| `VLLM_BASE_URL` | `http://vllm:8000` | vLLM アドレス（プロファイル or リモート） |
| `VLLM_MODEL` | 空 | `--profile vllm` が使う**ダウンロード済みの具体リポジトリパス** |
| `NOTIFY_DINGTALK_WEBHOOK` / `NOTIFY_FEISHU_WEBHOOK` / `NOTIFY_WECOM_WEBHOOK` | 空 | グループボット Webhook（UI 設定が環境変数より優先） |
| `NOTIFY_ON_COMPLETED` / `NOTIFY_ON_FAILED` | `1` | 完了 / 失敗の通知スイッチ |
| `NOTIFY_ON_STARTED` / `NOTIFY_ON_CANCELLED` | `0` | 開始 / 取消の通知スイッチ |
| `DLMODEL_IMAGE` | SWR `:latest` | 本番イメージ。ロールバック時は `:vX.Y.Z` に固定 |
| `ARIA2_IMAGE` | SWR 加速版 `p3terx/aria2-pro` | aria2 イメージ（v0.4.1 から SWR 加速アドレスが既定、プロキシなしで取得可） |
| `WEB_BIND` / `WEB_PORT` | `0.0.0.0` / `8080` | 本番 Web のバインドとポート |
| `PUID` / `PGID` | `0` / `0` | aria2 の実行アイデンティティ。モデルボリュームの書き込み権限と一致させる必要あり |

設定ページ（SQLite に保存）で指定した `HF_TOKEN` / `MODELSCOPE_API_TOKEN` は同名の環境変数より優先され、Web からのホット変更が可能です。

<details>
<summary>データとディレクトリ構成</summary>

| ホスト側パス | コンテナ側パス | 用途 |
|--------------|----------------|------|
| `./data/models` | `/models`（`MODEL_ROOT`） | モデル重み（vLLM / Ollama と共有） |
| `./data/app` | `/data`（`DATA_DIR`） | SQLite `app.db`、設定 |
| `./data/aria2` | `/config` | aria2 の設定・セッション・トラッカーログ |

ディスク配置：vLLM / HF は `{MODEL_ROOT}/hf/<org>/<repo>/`、Ollama は `{MODEL_ROOT}/ollama`（Ollama コンテナの `/root/.ollama` にマップ）。タスク記録は UI から削除してください（終了状態のクリーンは記録のみ削除し、ダウンロード済みファイルは消しません）。上記ディレクトリの直マネージメントも可能です。

</details>

## API 概要

`GET /api/health` を除く全エンドポイントに `Authorization: Bearer <ADMIN_TOKEN>` が必要です。

| メソッドとパス | 説明 |
|----------------|------|
| `POST /api/downloads` | タスク作成。ボディ `{name, source: auto\|modelscope\|huggingface\|ollama, target: vllm\|ollama, revision?}`。同名・同ターゲットにアクティブなタスク（`paused` 含む）があると `400` |
| `GET /api/downloads` | タスク一覧（`progress_bytes` / `total_bytes` / `speed_bps` 含む） |
| `GET /api/downloads/{id}` | 単一タスク |
| `POST /api/downloads/{id}/pause` | `queued` / `running` タスクを一時停止（`400` = 不正な状態） |
| `POST /api/downloads/{id}/resume` | `paused` タスクをキューへ復帰 |
| `POST /api/downloads/{id}/cancel` | 取消（アクティブ状態および `paused` から可） |
| `POST /api/downloads/{id}/retry` | `failed` / `cancelled` タスクの再試行。キュー復帰後ブレークポイントから継続 |
| `DELETE /api/downloads/{id}` | タスク記録の削除（終了状態と `paused`。記録のみでディスクファイルは保持） |
| `POST /api/downloads/cleanup?status=…` | 終了状態の一括クリーン（既定 `completed,cancelled,failed`） |
| `GET /api/downloads/{id}/events` | SSE 進捗ストリーム（Bearer ヘッダーのみ、[FAQ](#よくある質問--トラブルシューティング) 4 項参照） |
| `GET` / `PUT /api/settings` | 設定の読み書き（トークン / Webhook はマスク表示、平文は返さない；PUT は非 `None` フィールドのみ適用） |
| `POST /api/settings/notify-test` | テスト通知の送信 |
| `GET /api/models` · `DELETE /api/models/{id}` | ライブラリスキャン（パス / サイズ）と削除（**ディスク上のモデルディレクトリを削除**；タスク記録削除はファイルを触らない） |
| `GET /api/services/ollama/health` · `…/models` · `POST …/pull` | Ollama ヘルス、ローカルモデル一覧、プル（tag 上書き可） |
| `GET /api/services/vllm/health` · `…/launch-command` | vLLM ヘルス、起動コマンド生成 |
| `POST /api/auth/verify` | トークン検証 |
| `GET /api/health` | ライブネスプローブ（認証不要、コンテナ healthcheck 用） |

キュー同時数のホット調整：`PUT /api/settings` の `download_concurrency` 変更でキューが `resize`（1–8）即時反映されます。

## デプロイとイメージ

- **本番**：`docker compose up -d` —— 既定イメージ `swr.cn-east-3.myhuaweicloud.com/sreyun/dlmodel:latest`（aria2 イメージも SWR 加速）。`pull_policy: always` により毎回 `up` 時に取得。バージョン固定 / ロールバックは `.env` に `DLMODEL_IMAGE=…:vX.Y.Z`
- **リリース CI**：`v*` タグの push で GitHub Actions「Build and push Huawei SWR」が起動し `:<tag>` と `:latest` を同時更新。Secrets `HW_ACCESS_KEY` / `HW_SECRET_KEY` が必要（任意 Variable `HW_SWR_NAMESPACE`、既定 `sreyun`）。事前に [SWR コンソール](https://console.huaweicloud.com/swr)（cn-east-3）で組織を作成してください。手動再実行：Actions → Run workflow

### 任意の推論サービス

管理とダウンロードは推論サービスに依存しません。必要に応じて Compose プロファイルで有効化（既定では起動しません）：

```bash
docker compose --profile ollama up -d      # ローカル API: http://127.0.0.1:11434
docker compose --profile vllm up -d        # OpenAI 互換: http://127.0.0.1:8000、NVIDIA Container Toolkit 必要
```

- `VLLM_MODEL` には**ダウンロード済みの具体リポジトリパス**（例 `/models/hf/Qwen/Qwen3-0.6B`）を指定。`/models/hf` ルートは不可。ダウンロード完了後に `.env` へ記述し `vllm` を recreate
- vLLM は既定でイメージ `vllm/vllm-openai:latest` + `runtime: nvidia`。`HUGGING_FACE_HUB_TOKEN` は `HF_TOKEN` から透過
- Ollama でプルしたモデルは `{MODEL_ROOT}/ollama`（コンテナ `/root/.ollama`）に保存され、ダウンロードページの Ollama ソースと共通

## よくある質問 / トラブルシューティング

**1. aria2 が利用できない / 単一ストリームしか出ない？**
RPC 失敗時は自動的に単一ストリームの HTTP 続き取りへ降格します（タスクメッセージに「aria2 不可用」表示。タスクは失敗せずダウンロード続行）。多重接続が効かない原因はほぼ **ネットワーク** か **権限不足** のみ。既定の `PUID=0` / `PGID=0` は共有ボリュームの書き込み権限問題回避のためのもの。ホスト側ディレクトリが厳しすぎると aria2 は `.aria2` 制御ファイルやモデルボリュームへ書けません。コンテナユーザーに `./data/models` と `./data/aria2` の書き込み権限を与えてください。修復後はダウンロードを再実行し、「aria2 不可用」が消えれば復旧。`docker compose ps` / `docker compose logs aria2` でコンテナ状態も確認できます。

**2. HF モデルが 401 / 403（gated）？**
設定ページか `.env` に `HF_TOKEN` を指定（HF 公式サイトで利用規約同意後に生成）。利用中のミラーがトークン透過に対応するか確認（hf-mirror.com は対応）。ModelScope 独占モデルは `auto` または明示的 `modelscope` ソースを推奨。

**3. 「継続」を押すと「このターゲットのダウンロードはまだ実行中」と出る？**
ModelScope の既知の制限です：`snapshot_download` はブロッキングスレッドで動作し即中断できないため、一時停止は「追跡を止めて `paused` に退避」する方式です。バックグラウンドスレッドが終了してからターゲットディレクトリを解放します。そのため**同一ターゲットの即時再開は並行二重書き込み防止のため拒否されます（409）**——少し待ってから再度「継続」を押してください。それ以外のソース（HF / aria2 / HTTP / Ollama）は一時停止後即座に再開・続き取りできます。

**4. SSE エンドポイントがあるのにフロントエンドはポーリング？**
`GET /api/downloads/{id}/events`はリクエストヘッダー形式の Bearer トークンのみ受け付けますが、ブラウザのネイティブ `EventSource` はカスタムヘッダーを設定できないため、フロントエンドは 1 秒（アクティブ時）/ 4 秒（アイドル時）の短周期ポーリングを採用しています。リアルタイム配信が必要なら `fetch` + ReadableStream を自前で統合してください。

**5. ポートが使用中？**
`.env` の `WEB_PORT`（`WEB_BIND` でバインドアドレスも調整可）を変更し、`docker compose up -d` を再実行。

**6. ディスク容量不足（No space left on device）？**
原因はタスクメッセージと通知にそのまま表示されます。注意：**ディスク満杯と権限拒否は自動リトライしません**（リトライ無意味）。空きを作った後に失敗タスクの「再試行」を押せばブレークポイントから継続。大容量（数十 GB 級）ダウンロード前の `df -h` 確認を推奨。

**7. コンテナ再起動後に running タスクはどうなる？**
起動時にキューはすべての `queued` / `running` タスクを統一再キューして再スケジュールします（メッセージ「サービス再起動後に再キュー…」、グレースフル停止も `docker compose down` 等のハード停止も対象）。aria2 は進行中転送を復元し、HTTP / SDK はディスク上のブレークポイントから続き取りします。`paused` タスクは再起動でも停止状態を保ち、「継続」を待つだけです。

**8. タスクが繰り返し失敗する、原因を知りたい？**
各タスクの `message` に失敗原因または現在段階（ソース解決、フォールバック、リトライ回数）が表示され、`GET /api/downloads/{id}` で直接確認できます。サーバーログ（`docker compose logs web`、JSON 形式）にはタスク ID 付きの完全なタイムラインがあり、トークン等の機密はマスク済みです。

## ロードマップ

検討中の方向性（約束ではありません）：SSE リアルタイム配信のフロント統合（`fetch` ストリーム方式）、帯域制限、バルクマニフェストインポート、定期クリーンポリシー、タスクのタグとグループ化。

## コントリビューション

Issue と PR を歓迎します：テストカバレッジを添付のうえ、挙動変更時は本書および全言語版（`README.md` / `README.en.md` / `README.ru.md`）の同期更新をお願いします。コミットメッセージは Conventional Commits（`feat:` / `fix:` / `chore:`…）に準拠。

## ライセンス

[MIT License](LICENSE) © 2026 sreyun
