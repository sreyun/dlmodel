const TOKEN_KEY = "dlmodel_token";
const ROUTES = ["download", "tasks", "library", "services", "settings"];
const TERMINAL = new Set(["completed", "failed", "cancelled"]);

const STATUS_LABEL = {
  queued: "排队中",
  running: "下载中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

let pollTimer = null;
let tasksInFlight = false;

async function api(path, opts = {}) {
  const token = localStorage.getItem(TOKEN_KEY) || "";
  const headers = Object.assign({"Content-Type": "application/json"}, opts.headers || {}, {
    Authorization: `Bearer ${token}`,
  });
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) throw new Error("unauthorized");
  return res;
}

async function apiJson(path, opts = {}) {
  const res = await api(path, opts);
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = { detail: text }; }
  }
  if (!res.ok) throw new Error(errorMessage(data, res.statusText));
  return data;
}

function errorMessage(data, fallback) {
  const detail = data && data.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
  if (detail != null) return JSON.stringify(detail);
  return fallback || "请求失败";
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function $(id) {
  return document.getElementById(id);
}

function flash(kind, message) {
  return message ? `<div class="flash ${kind}">${esc(message)}</div>` : "";
}

function fmtBytes(n) {
  if (n == null || n === "") return "—";
  let value = Number(n);
  if (!Number.isFinite(value)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtSpeed(bps) {
  if (bps == null || bps === "") return "—";
  return `${fmtBytes(bps)}/s`;
}

function progressLabel(task) {
  if (!task.total_bytes) return `${fmtBytes(task.progress_bytes)}`;
  const pct = Math.min(100, (100 * Number(task.progress_bytes || 0)) / Number(task.total_bytes));
  return `${pct.toFixed(1)}% (${fmtBytes(task.progress_bytes)} / ${fmtBytes(task.total_bytes)})`;
}

function statusLabel(status) {
  return STATUS_LABEL[status] || status;
}

function sourceLabel(source) {
  return ({
    auto: "自动",
    modelscope: "ModelScope 魔搭",
    huggingface: "Hugging Face",
    ollama: "Ollama",
  })[source] || source;
}

function targetLabel(target) {
  return ({ vllm: "vLLM", ollama: "Ollama" })[target] || target;
}

function destHint(name, source, target) {
  const model = (name || "").trim();
  if (!model) return "输入模型名称后可预览保存路径。";
  if (target === "ollama" || source === "ollama") return `Ollama 模型库：${model}`;
  return `保存路径预览：hf/${model}`;
}

function currentPage() {
  const raw = (location.hash || "#/download").replace(/^#\/?/, "");
  return ROUTES.includes(raw) ? raw : "download";
}

function stopPoll() {
  if (pollTimer != null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function setNavVisible(visible) {
  const nav = $("nav");
  if (nav) nav.hidden = !visible;
}

function markActiveNav(page) {
  document.querySelectorAll("nav a").forEach((link) => {
    const href = link.getAttribute("href") || "";
    link.classList.toggle("active", href === `#/${page}`);
  });
}

function showLogin(message) {
  stopPoll();
  setNavVisible(false);
  $("app").innerHTML = `
    <div class="panel" style="max-width:420px;margin:48px auto;">
      <h1>登录</h1>
      <p class="muted">请输入管理员令牌（ADMIN_TOKEN）。令牌会保存在本浏览器的 <span class="mono">dlmodel_token</span> 中。</p>
      ${flash("error", message)}
      <form id="login-form">
        <label><span>管理员令牌</span><input id="token" type="password" autocomplete="current-password" required></label>
        <div class="actions"><button class="primary" type="submit">进入系统</button></div>
      </form>
    </div>`;
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    localStorage.setItem(TOKEN_KEY, $("token").value.trim());
    try {
      await apiJson("/api/auth/verify", { method: "POST" });
      await showApp();
    } catch (err) {
      localStorage.removeItem(TOKEN_KEY);
      showLogin(err.message === "unauthorized" ? "令牌无效" : err.message);
    }
  });
}

async function verifySession() {
  const token = localStorage.getItem(TOKEN_KEY) || "";
  if (!token) return false;
  try {
    await apiJson("/api/auth/verify", { method: "POST" });
    return true;
  } catch {
    localStorage.removeItem(TOKEN_KEY);
    return false;
  }
}

async function guarded(fn) {
  try {
    await fn();
  } catch (err) {
    if (err.message === "unauthorized") {
      showLogin("登录已过期，请重新登录。");
      return;
    }
    $("app").insertAdjacentHTML("afterbegin", flash("error", err.message));
  }
}

function renderDownload() {
  $("app").innerHTML = `
    <h1>下载模型</h1>
    <div class="panel">
      <form id="dl-form">
        <label><span>模型名称</span><input id="name" required placeholder="Qwen/Qwen2.5-7B-Instruct 或 llama3.2"></label>
        <label><span>下载源</span>
          <select id="source">
            <option value="auto">自动（优先魔搭，回落 HF）</option>
            <option value="modelscope">ModelScope 魔搭</option>
            <option value="huggingface">Hugging Face</option>
            <option value="ollama">Ollama</option>
          </select>
        </label>
        <label><span>目标用途</span>
          <select id="target">
            <option value="vllm">vLLM（HF 目录布局）</option>
            <option value="ollama">Ollama</option>
          </select>
        </label>
        <label><span>版本 / Revision（可选）</span><input id="revision" placeholder="main"></label>
        <p class="hint" id="dest-hint">${esc(destHint("", "auto", "vllm"))}</p>
        <div class="actions"><button class="primary" type="submit">开始下载</button></div>
      </form>
    </div>`;

  const updateHint = () => {
    $("dest-hint").textContent = destHint($("name").value, $("source").value, $("target").value);
  };
  ["name", "source", "target"].forEach((id) => $(id).addEventListener("input", updateHint));
  $("source").addEventListener("change", updateHint);
  $("target").addEventListener("change", updateHint);

  $("dl-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {
      name: $("name").value.trim(),
      source: $("source").value,
      target: $("target").value,
    };
    const revision = $("revision").value.trim();
    if (revision) body.revision = revision;
    await guarded(async () => {
      const created = await apiJson("/api/downloads", { method: "POST", body: JSON.stringify(body) });
      location.hash = "#/tasks";
      $("app").insertAdjacentHTML("afterbegin", flash("ok", `已加入队列：${created.id}`));
    });
  });
}

function taskRow(task) {
  const canCancel = !TERMINAL.has(task.status);
  const canRetry = task.status === "failed" || task.status === "cancelled";
  return `<tr>
    <td class="mono" title="${esc(task.id)}">${esc(String(task.id).slice(0, 8))}</td>
    <td>${esc(task.name)}<div class="muted">${esc(sourceLabel(task.source))} → ${esc(targetLabel(task.target))}</div></td>
    <td class="status ${esc(task.status)}">${esc(statusLabel(task.status))}</td>
    <td>${esc(progressLabel(task))}<div class="muted">${esc(fmtSpeed(task.speed_bps))}</div></td>
    <td>${esc(task.message || "")}</td>
    <td>
      <button data-act="cancel" data-id="${esc(task.id)}" ${canCancel ? "" : "disabled"}>取消</button>
      <button data-act="retry" data-id="${esc(task.id)}" ${canRetry ? "" : "disabled"}>重试</button>
    </td>
  </tr>`;
}

async function refreshTasks() {
  if (tasksInFlight || currentPage() !== "tasks") return;
  tasksInFlight = true;
  try {
    const tasks = await apiJson("/api/downloads");
    const body = $("task-body");
    if (!body) return;
    body.innerHTML = tasks.length
      ? tasks.map(taskRow).join("")
      : `<tr><td colspan="6" class="empty">暂无下载任务。</td></tr>`;
  } catch (err) {
    if (err.message === "unauthorized") {
      showLogin("登录已过期，请重新登录。");
      return;
    }
    const box = $("task-error");
    if (box) box.innerHTML = flash("error", err.message);
  } finally {
    tasksInFlight = false;
  }
}

function renderTasks() {
  $("app").innerHTML = `
    <h1>下载任务</h1>
    <div id="task-error"></div>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>模型</th>
            <th>状态</th>
            <th>进度</th>
            <th>消息</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody id="task-body">
          <tr><td colspan="6" class="empty">加载中…</td></tr>
        </tbody>
      </table>
    </div>`;

  $("task-body").addEventListener("click", async (event) => {
    const btn = event.target.closest("button[data-act]");
    if (!btn || btn.disabled) return;
    const id = btn.getAttribute("data-id");
    const act = btn.getAttribute("data-act");
    await guarded(async () => {
      await apiJson(`/api/downloads/${encodeURIComponent(id)}/${act}`, { method: "POST" });
      await refreshTasks();
    });
  });

  refreshTasks();
  stopPoll();
  pollTimer = setInterval(refreshTasks, 1000);
}

function renderLibrary() {
  $("app").innerHTML = `
    <h1>模型库</h1>
    <div id="lib-msg"></div>
    <div class="panel">
      <table>
        <thead>
          <tr><th>名称</th><th>路径</th><th>大小</th><th>用途</th><th>操作</th></tr>
        </thead>
        <tbody id="lib-body">
          <tr><td colspan="5" class="empty">加载中…</td></tr>
        </tbody>
      </table>
    </div>`;

  const load = async () => {
    await guarded(async () => {
      const models = await apiJson("/api/models");
      $("lib-body").innerHTML = models.length
        ? models.map((m) => `<tr>
            <td>${esc(m.name)}<div class="muted mono">${esc(m.id)}</div></td>
            <td class="mono">${esc(m.path)}</td>
            <td>${esc(fmtBytes(m.size_bytes))}</td>
            <td>${esc(targetLabel(m.target))}</td>
            <td><button class="danger" data-del="${esc(m.id)}">删除</button></td>
          </tr>`).join("")
        : `<tr><td colspan="5" class="empty">磁盘上还没有已下载模型。</td></tr>`;
    });
  };

  $("lib-body").addEventListener("click", async (event) => {
    const btn = event.target.closest("button[data-del]");
    if (!btn) return;
    const id = btn.getAttribute("data-del");
    if (!confirm(`确认删除 ${id}？此操作不可恢复。`)) return;
    await guarded(async () => {
      await apiJson(`/api/models/${encodeURIComponent(id)}`, { method: "DELETE" });
      $("lib-msg").innerHTML = flash("ok", `已删除 ${id}`);
      await load();
    });
  });

  load();
}

function healthBadge(health) {
  if (!health) return `<span class="muted">未知</span>`;
  return health.ok
    ? `<span class="status completed">已连接</span> <span class="muted">${esc(health.detail || "")}</span>`
    : `<span class="status failed">不可达</span> <span class="muted">${esc(health.detail || "")}</span>`;
}

function renderServices() {
  $("app").innerHTML = `
    <h1>推理服务</h1>
    <div class="row">
      <div class="panel">
        <h2>Ollama</h2>
        <p id="ollama-health" class="muted">检查中…</p>
        <form id="ollama-pull" class="actions">
          <input id="ollama-name" placeholder="llama3.2" required style="max-width:240px">
          <button class="primary" type="submit">拉取</button>
          <button type="button" id="ollama-refresh">刷新</button>
        </form>
        <div id="ollama-msg"></div>
        <table>
          <thead><tr><th>名称</th><th>大小</th></tr></thead>
          <tbody id="ollama-models"><tr><td colspan="2" class="empty">加载中…</td></tr></tbody>
        </table>
      </div>
      <div class="panel">
        <h2>vLLM</h2>
        <p id="vllm-health" class="muted">检查中…</p>
        <form id="vllm-form">
          <label><span>模型路径</span><input id="vllm-model" required placeholder="/models/hf/Qwen/Qwen2.5-7B-Instruct"></label>
          <label><span>端口</span><input id="vllm-port" type="number" value="8000" min="1"></label>
          <div class="actions">
            <button class="primary" type="submit">生成启动命令</button>
            <button type="button" id="copy-cmd" disabled>复制</button>
            <button type="button" id="vllm-refresh">刷新状态</button>
          </div>
        </form>
        <p id="vllm-cmd" class="mono cmd hint"></p>
      </div>
    </div>`;

  const loadOllama = async () => {
    await guarded(async () => {
      const health = await apiJson("/api/services/ollama/health");
      $("ollama-health").innerHTML = healthBadge(health);
      try {
        const models = await apiJson("/api/services/ollama/models");
        $("ollama-models").innerHTML = models.length
          ? models.map((m) => `<tr><td>${esc(m.name || m.model || "")}</td><td>${esc(fmtBytes(m.size))}</td></tr>`).join("")
          : `<tr><td colspan="2" class="empty">暂无 Ollama 模型。</td></tr>`;
      } catch (err) {
        $("ollama-models").innerHTML = `<tr><td colspan="2" class="empty">${esc(err.message)}</td></tr>`;
      }
    });
  };

  const loadVllm = async () => {
    await guarded(async () => {
      $("vllm-health").innerHTML = healthBadge(await apiJson("/api/services/vllm/health"));
    });
  };

  $("ollama-refresh").addEventListener("click", loadOllama);
  $("vllm-refresh").addEventListener("click", loadVllm);
  $("ollama-pull").addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = $("ollama-name").value.trim();
    $("ollama-msg").innerHTML = flash("", `正在拉取 ${name}…`);
    await guarded(async () => {
      await apiJson("/api/services/ollama/pull", { method: "POST", body: JSON.stringify({ name }) });
      $("ollama-msg").innerHTML = flash("ok", `已拉取 ${name}`);
      await loadOllama();
    });
  });
  $("vllm-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const model = $("vllm-model").value.trim();
    const port = $("vllm-port").value || "8000";
    await guarded(async () => {
      const qs = new URLSearchParams({ model, port });
      const data = await apiJson(`/api/services/vllm/launch-command?${qs}`);
      $("vllm-cmd").textContent = data.command;
      $("copy-cmd").disabled = !data.command;
      $("copy-cmd").dataset.cmd = data.command;
    });
  });
  $("copy-cmd").addEventListener("click", async () => {
    const cmd = $("copy-cmd").dataset.cmd || "";
    if (!cmd) return;
    try {
      await navigator.clipboard.writeText(cmd);
    } catch {
      const box = document.createElement("textarea");
      box.value = cmd;
      document.body.appendChild(box);
      box.select();
      document.execCommand("copy");
      box.remove();
    }
    $("vllm-cmd").insertAdjacentHTML("beforebegin", flash("ok", "启动命令已复制"));
  });

  loadOllama();
  loadVllm();
}

function renderSettings() {
  $("app").innerHTML = `
    <h1>设置</h1>
    <div id="set-msg"></div>
    <div class="panel">
      <form id="set-form">
        <label><span>Hugging Face 镜像地址</span><input id="hf_endpoint"></label>
        <label><span>HF Token（留空表示不修改）</span><input id="hf_token" type="password" autocomplete="off"></label>
        <label><span>ModelScope Token（留空表示不修改）</span><input id="modelscope_api_token" type="password" autocomplete="off"></label>
        <label><span>下载并发任务数</span><input id="download_concurrency" type="number" min="1"></label>
        <label><span>aria2 单文件连接数</span><input id="aria2_connections" type="number" min="1"></label>
        <label><span>Ollama 服务地址</span><input id="ollama_base_url"></label>
        <label><span>vLLM 服务地址</span><input id="vllm_base_url"></label>
        <div class="actions"><button class="primary" type="submit">保存设置</button></div>
      </form>
    </div>`;

  const fields = [
    "hf_endpoint",
    "hf_token",
    "modelscope_api_token",
    "download_concurrency",
    "aria2_connections",
    "ollama_base_url",
    "vllm_base_url",
  ];

  guarded(async () => {
    const data = await apiJson("/api/settings");
    fields.forEach((key) => {
      if ($(key) && data[key] != null) $(key).value = data[key];
    });
  });

  $("set-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {
      hf_endpoint: $("hf_endpoint").value.trim(),
      download_concurrency: Number($("download_concurrency").value),
      aria2_connections: Number($("aria2_connections").value),
      ollama_base_url: $("ollama_base_url").value.trim(),
      vllm_base_url: $("vllm_base_url").value.trim(),
    };
    const hfToken = $("hf_token").value.trim();
    if (hfToken) body.hf_token = hfToken;
    const msToken = $("modelscope_api_token").value.trim();
    if (msToken) body.modelscope_api_token = msToken;
    await guarded(async () => {
      await apiJson("/api/settings", { method: "PUT", body: JSON.stringify(body) });
      $("set-msg").innerHTML = flash("ok", "设置已保存");
    });
  });
}

const VIEWS = {
  download: renderDownload,
  tasks: renderTasks,
  library: renderLibrary,
  services: renderServices,
  settings: renderSettings,
};

function route() {
  stopPoll();
  const page = currentPage();
  if (location.hash !== `#/${page}`) location.hash = `#/${page}`;
  markActiveNav(page);
  VIEWS[page]();
}

async function showApp() {
  setNavVisible(true);
  route();
}

async function boot() {
  $("signout").addEventListener("click", () => {
    localStorage.removeItem(TOKEN_KEY);
    showLogin();
  });
  window.addEventListener("hashchange", () => {
    if ($("nav").hidden) return;
    route();
  });
  if (await verifySession()) await showApp();
  else showLogin();
}

boot();
