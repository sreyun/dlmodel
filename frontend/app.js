const TOKEN_KEY = "dlmodel_token";
const ROUTES = ["download", "tasks", "library", "services", "settings"];
const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const SECRET_FIELDS = new Set(["hf_token", "modelscope_api_token"]);

const STATUS_LABEL = {
  queued: "排队中",
  running: "下载中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

let pollTimer = null;
let tasksInFlight = false;
let routeGen = 0;
let ignoreHashChange = false;

function $(id) {
  return document.getElementById(id);
}

function appRoot() {
  return $("app");
}

function setHtml(el, html) {
  if (!el) return false;
  el.innerHTML = html;
  return true;
}

function showFlash(targetId, kind, message) {
  const el = $(targetId) || appRoot();
  if (!el || !message) return;
  const html = flash(kind, message);
  if (targetId && $(targetId)) {
    setHtml($(targetId), html);
    return;
  }
  el.insertAdjacentHTML("afterbegin", html);
}

function flash(kind, message) {
  return message ? `<div class="flash ${kind}">${esc(message)}</div>` : "";
}

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

function stillOn(page, gen) {
  return gen === routeGen && currentPage() === page && !!appRoot();
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
  routeGen += 1;
  setNavVisible(false);
  const root = appRoot();
  if (!root) return;
  setHtml(root, `
    <div class="panel" style="max-width:420px;margin:48px auto;">
      <h1>登录</h1>
      <p class="muted">请输入管理员令牌（ADMIN_TOKEN）。令牌会保存在本浏览器的 <span class="mono">dlmodel_token</span> 中。</p>
      ${flash("error", message)}
      <form id="login-form">
        <label><span>管理员令牌</span><input id="login-token" type="password" autocomplete="current-password" required></label>
        <div class="actions"><button class="primary" type="submit">进入系统</button></div>
      </form>
    </div>`);
  const form = $("login-form");
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("login-token");
    if (!input) return;
    localStorage.setItem(TOKEN_KEY, input.value.trim());
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

async function guarded(fn, opts = {}) {
  try {
    await fn();
  } catch (err) {
    if (err.message === "unauthorized") {
      showLogin("登录已过期，请重新登录。");
      return;
    }
    if (opts.gen != null && opts.page && !stillOn(opts.page, opts.gen)) return;
    showFlash(opts.flashId || null, "error", err.message);
  }
}

function renderDownload(gen) {
  const root = appRoot();
  if (!root) return;
  setHtml(root, `
    <h1>下载模型</h1>
    <div id="dl-msg"></div>
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
    </div>`);

  const updateHint = () => {
    const hint = $("dest-hint");
    const name = $("name");
    const source = $("source");
    const target = $("target");
    if (!hint || !name || !source || !target) return;
    hint.textContent = destHint(name.value, source.value, target.value);
  };
  ["name", "source", "target"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("input", updateHint);
    if (el) el.addEventListener("change", updateHint);
  });

  const form = $("dl-form");
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!stillOn("download", gen)) return;
    const nameEl = $("name");
    const sourceEl = $("source");
    const targetEl = $("target");
    const revisionEl = $("revision");
    if (!nameEl || !sourceEl || !targetEl) return;
    const body = {
      name: nameEl.value.trim(),
      source: sourceEl.value,
      target: targetEl.value,
    };
    const revision = revisionEl ? revisionEl.value.trim() : "";
    if (revision) body.revision = revision;
    await guarded(async () => {
      const created = await apiJson("/api/downloads", { method: "POST", body: JSON.stringify(body) });
      sessionStorage.setItem("dlmodel_flash", `已加入队列：${created.id}`);
      location.hash = "#/tasks";
    }, { gen, page: "download", flashId: "dl-msg" });
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

async function refreshTasks(gen) {
  if (tasksInFlight || !stillOn("tasks", gen ?? routeGen)) return;
  tasksInFlight = true;
  const myGen = gen ?? routeGen;
  try {
    const tasks = await apiJson("/api/downloads");
    if (!stillOn("tasks", myGen)) return;
    const body = $("task-body");
    if (!body) return;
    setHtml(
      body,
      tasks.length
        ? tasks.map(taskRow).join("")
        : `<tr><td colspan="6" class="empty">暂无下载任务。</td></tr>`,
    );
  } catch (err) {
    if (err.message === "unauthorized") {
      showLogin("登录已过期，请重新登录。");
      return;
    }
    if (!stillOn("tasks", myGen)) return;
    showFlash("task-error", "error", err.message);
  } finally {
    tasksInFlight = false;
  }
}

function renderTasks(gen) {
  const root = appRoot();
  if (!root) return;
  const pending = sessionStorage.getItem("dlmodel_flash");
  if (pending) sessionStorage.removeItem("dlmodel_flash");
  setHtml(root, `
    <h1>下载任务</h1>
    <div id="task-error">${pending ? flash("ok", pending) : ""}</div>
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
    </div>`);

  const body = $("task-body");
  if (body) {
    body.addEventListener("click", async (event) => {
      const btn = event.target.closest("button[data-act]");
      if (!btn || btn.disabled || !stillOn("tasks", gen)) return;
      const id = btn.getAttribute("data-id");
      const act = btn.getAttribute("data-act");
      await guarded(async () => {
        await apiJson(`/api/downloads/${encodeURIComponent(id)}/${act}`, { method: "POST" });
        await refreshTasks(gen);
      }, { gen, page: "tasks", flashId: "task-error" });
    });
  }

  refreshTasks(gen);
  stopPoll();
  pollTimer = setInterval(() => refreshTasks(gen), 1000);
}

function renderLibrary(gen) {
  const root = appRoot();
  if (!root) return;
  setHtml(root, `
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
    </div>`);

  const load = async () => {
    await guarded(async () => {
      const models = await apiJson("/api/models");
      if (!stillOn("library", gen)) return;
      const body = $("lib-body");
      if (!body) return;
      setHtml(
        body,
        models.length
          ? models.map((m) => `<tr>
              <td>${esc(m.name)}<div class="muted mono">${esc(m.id)}</div></td>
              <td class="mono">${esc(m.path)}</td>
              <td>${esc(fmtBytes(m.size_bytes))}</td>
              <td>${esc(targetLabel(m.target))}</td>
              <td><button class="danger" data-del="${esc(m.id)}">删除</button></td>
            </tr>`).join("")
          : `<tr><td colspan="5" class="empty">磁盘上还没有已下载模型。</td></tr>`,
      );
    }, { gen, page: "library", flashId: "lib-msg" });
  };

  const body = $("lib-body");
  if (body) {
    body.addEventListener("click", async (event) => {
      const btn = event.target.closest("button[data-del]");
      if (!btn || !stillOn("library", gen)) return;
      const id = btn.getAttribute("data-del");
      if (!confirm(`确认删除 ${id}？此操作不可恢复。`)) return;
      await guarded(async () => {
        await apiJson(`/api/models/${encodeURIComponent(id)}`, { method: "DELETE" });
        if (!stillOn("library", gen)) return;
        showFlash("lib-msg", "ok", `已删除 ${id}`);
        await load();
      }, { gen, page: "library", flashId: "lib-msg" });
    });
  }

  load();
}

function healthBadge(health) {
  if (!health) return `<span class="muted">未知</span>`;
  return health.ok
    ? `<span class="status completed">已连接</span> <span class="muted">${esc(health.detail || "")}</span>`
    : `<span class="status failed">不可达</span> <span class="muted">${esc(health.detail || "")}</span>`;
}

function renderServices(gen) {
  const root = appRoot();
  if (!root) return;
  setHtml(root, `
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
        <div id="vllm-msg"></div>
      </div>
    </div>`);

  const loadOllama = async () => {
    await guarded(async () => {
      const health = await apiJson("/api/services/ollama/health");
      if (!stillOn("services", gen)) return;
      setHtml($("ollama-health"), healthBadge(health));
      try {
        const models = await apiJson("/api/services/ollama/models");
        if (!stillOn("services", gen)) return;
        setHtml(
          $("ollama-models"),
          models.length
            ? models.map((m) => `<tr><td>${esc(m.name || m.model || "")}</td><td>${esc(fmtBytes(m.size))}</td></tr>`).join("")
            : `<tr><td colspan="2" class="empty">暂无 Ollama 模型。</td></tr>`,
        );
      } catch (err) {
        if (!stillOn("services", gen)) return;
        setHtml($("ollama-models"), `<tr><td colspan="2" class="empty">${esc(err.message)}</td></tr>`);
      }
    }, { gen, page: "services", flashId: "ollama-msg" });
  };

  const loadVllm = async () => {
    await guarded(async () => {
      const health = await apiJson("/api/services/vllm/health");
      if (!stillOn("services", gen)) return;
      setHtml($("vllm-health"), healthBadge(health));
    }, { gen, page: "services", flashId: "vllm-msg" });
  };

  const ollamaRefresh = $("ollama-refresh");
  const vllmRefresh = $("vllm-refresh");
  if (ollamaRefresh) ollamaRefresh.addEventListener("click", () => loadOllama());
  if (vllmRefresh) vllmRefresh.addEventListener("click", () => loadVllm());

  const pullForm = $("ollama-pull");
  if (pullForm) {
    pullForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!stillOn("services", gen)) return;
      const nameEl = $("ollama-name");
      if (!nameEl) return;
      const name = nameEl.value.trim();
      showFlash("ollama-msg", "", `正在拉取 ${name}…`);
      await guarded(async () => {
        await apiJson("/api/services/ollama/pull", { method: "POST", body: JSON.stringify({ name }) });
        if (!stillOn("services", gen)) return;
        showFlash("ollama-msg", "ok", `已拉取 ${name}`);
        await loadOllama();
      }, { gen, page: "services", flashId: "ollama-msg" });
    });
  }

  const vllmForm = $("vllm-form");
  if (vllmForm) {
    vllmForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!stillOn("services", gen)) return;
      const modelEl = $("vllm-model");
      const portEl = $("vllm-port");
      if (!modelEl) return;
      const model = modelEl.value.trim();
      const port = (portEl && portEl.value) || "8000";
      await guarded(async () => {
        const qs = new URLSearchParams({ model, port });
        const data = await apiJson(`/api/services/vllm/launch-command?${qs}`);
        if (!stillOn("services", gen)) return;
        const cmdEl = $("vllm-cmd");
        const copyBtn = $("copy-cmd");
        if (cmdEl) cmdEl.textContent = data.command || "";
        if (copyBtn) {
          copyBtn.disabled = !data.command;
          copyBtn.dataset.cmd = data.command || "";
        }
      }, { gen, page: "services", flashId: "vllm-msg" });
    });
  }

  const copyBtn = $("copy-cmd");
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      const cmd = copyBtn.dataset.cmd || "";
      if (!cmd || !stillOn("services", gen)) return;
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
      showFlash("vllm-msg", "ok", "启动命令已复制");
    });
  }

  loadOllama();
  loadVllm();
}

function renderSettings(gen) {
  const root = appRoot();
  if (!root) return;
  setHtml(root, `
    <h1>设置</h1>
    <div id="set-msg"></div>
    <div class="panel">
      <form id="set-form">
        <p id="set-loading" class="muted">正在加载当前配置…</p>
        <label><span>Hugging Face 镜像地址</span><input id="hf_endpoint" autocomplete="off"></label>
        <label>
          <span>HF Token（留空表示不修改）</span>
          <input id="hf_token" type="password" autocomplete="new-password" placeholder="未修改">
          <span id="hf_token_hint" class="hint"></span>
        </label>
        <label>
          <span>ModelScope Token（留空表示不修改）</span>
          <input id="modelscope_api_token" type="password" autocomplete="new-password" placeholder="未修改">
          <span id="ms_token_hint" class="hint"></span>
        </label>
        <label><span>下载并发任务数</span><input id="download_concurrency" type="number" min="1" step="1"></label>
        <label><span>aria2 单文件连接数</span><input id="aria2_connections" type="number" min="1" step="1"></label>
        <label><span>Ollama 服务地址</span><input id="ollama_base_url" autocomplete="off"></label>
        <label><span>vLLM 服务地址</span><input id="vllm_base_url" autocomplete="off"></label>
        <div class="actions">
          <button class="primary" id="set-save" type="submit" disabled>保存设置</button>
        </div>
      </form>
    </div>`);

  const fields = [
    "hf_endpoint",
    "download_concurrency",
    "aria2_connections",
    "ollama_base_url",
    "vllm_base_url",
  ];

  guarded(async () => {
    const data = await apiJson("/api/settings");
    if (!stillOn("settings", gen)) return;

    fields.forEach((key) => {
      const el = $(key);
      if (!el || data[key] == null) return;
      el.value = String(data[key]);
    });

    const hfHint = $("hf_token_hint");
    const msHint = $("ms_token_hint");
    if (hfHint) {
      hfHint.textContent = data.hf_token
        ? "当前已配置 HF Token（输入新值才会覆盖）"
        : "当前未配置 HF Token";
    }
    if (msHint) {
      msHint.textContent = data.modelscope_api_token
        ? "当前已配置 ModelScope Token（输入新值才会覆盖）"
        : "当前未配置 ModelScope Token";
    }

    const loading = $("set-loading");
    if (loading) loading.remove();
    const saveBtn = $("set-save");
    if (saveBtn) saveBtn.disabled = false;
  }, { gen, page: "settings", flashId: "set-msg" });

  const form = $("set-form");
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!stillOn("settings", gen)) return;

    const required = {
      hf_endpoint: $("hf_endpoint"),
      download_concurrency: $("download_concurrency"),
      aria2_connections: $("aria2_connections"),
      ollama_base_url: $("ollama_base_url"),
      vllm_base_url: $("vllm_base_url"),
    };
    if (Object.values(required).some((el) => !el)) {
      showFlash("set-msg", "error", "设置表单未就绪，请刷新页面后重试。");
      return;
    }

    const concurrency = Number(required.download_concurrency.value);
    const connections = Number(required.aria2_connections.value);
    if (!Number.isInteger(concurrency) || concurrency < 1) {
      showFlash("set-msg", "error", "下载并发任务数必须是大于等于 1 的整数。");
      return;
    }
    if (!Number.isInteger(connections) || connections < 1) {
      showFlash("set-msg", "error", "aria2 连接数必须是大于等于 1 的整数。");
      return;
    }

    const body = {
      hf_endpoint: required.hf_endpoint.value.trim(),
      download_concurrency: concurrency,
      aria2_connections: connections,
      ollama_base_url: required.ollama_base_url.value.trim(),
      vllm_base_url: required.vllm_base_url.value.trim(),
    };
    const hfTokenEl = $("hf_token");
    const msTokenEl = $("modelscope_api_token");
    const hfToken = hfTokenEl ? hfTokenEl.value.trim() : "";
    const msToken = msTokenEl ? msTokenEl.value.trim() : "";
    if (hfToken) body.hf_token = hfToken;
    if (msToken) body.modelscope_api_token = msToken;

    const saveBtn = $("set-save");
    if (saveBtn) saveBtn.disabled = true;
    await guarded(async () => {
      await apiJson("/api/settings", { method: "PUT", body: JSON.stringify(body) });
      if (!stillOn("settings", gen)) return;
      if (hfTokenEl) hfTokenEl.value = "";
      if (msTokenEl) msTokenEl.value = "";
      showFlash("set-msg", "ok", "设置已保存");
      if (hfToken || msToken) {
        const hfHint = $("hf_token_hint");
        const msHint = $("ms_token_hint");
        if (hfToken && hfHint) hfHint.textContent = "当前已配置 HF Token（输入新值才会覆盖）";
        if (msToken && msHint) msHint.textContent = "当前已配置 ModelScope Token（输入新值才会覆盖）";
      }
    }, { gen, page: "settings", flashId: "set-msg" });
    if (stillOn("settings", gen) && saveBtn) saveBtn.disabled = false;
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
  const wanted = `#/${page}`;
  if (location.hash !== wanted) {
    ignoreHashChange = true;
    location.hash = wanted;
    ignoreHashChange = false;
  }
  const gen = ++routeGen;
  markActiveNav(page);
  const view = VIEWS[page];
  if (typeof view === "function") view(gen);
}

async function showApp() {
  setNavVisible(true);
  route();
}

async function boot() {
  const signout = $("signout");
  if (signout) {
    signout.addEventListener("click", () => {
      localStorage.removeItem(TOKEN_KEY);
      showLogin();
    });
  }
  window.addEventListener("hashchange", () => {
    if (ignoreHashChange) return;
    if ($("nav") && $("nav").hidden) return;
    route();
  });
  if (await verifySession()) await showApp();
  else showLogin();
}

boot();
