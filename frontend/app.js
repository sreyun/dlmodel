const TOKEN_KEY = "dlmodel_token";
const ROUTES = ["download", "tasks", "library", "services", "settings"];
const TERMINAL = new Set(["completed", "failed", "cancelled"]);

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
  return fallback || "Request failed";
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

function destHint(name, source, target) {
  const model = (name || "").trim();
  if (!model) return "Enter a model name to preview the destination.";
  if (target === "ollama" || source === "ollama") return `Ollama library: ${model}`;
  return `hf/${model}`;
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
      <h1>Sign in</h1>
      <p class="muted">Enter the admin token. It is stored in this browser as <span class="mono">dlmodel_token</span>.</p>
      ${flash("error", message)}
      <form id="login-form">
        <label><span>Admin token</span><input id="token" type="password" autocomplete="current-password" required></label>
        <div class="actions"><button class="primary" type="submit">Continue</button></div>
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
      showLogin(err.message === "unauthorized" ? "Invalid token" : err.message);
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
      showLogin("Session expired. Sign in again.");
      return;
    }
    $("app").insertAdjacentHTML("afterbegin", flash("error", err.message));
  }
}

function renderDownload() {
  $("app").innerHTML = `
    <h1>Download</h1>
    <div class="panel">
      <form id="dl-form">
        <label><span>Model name</span><input id="name" required placeholder="Qwen/Qwen2.5-7B-Instruct"></label>
        <label><span>Source</span>
          <select id="source">
            <option value="auto">auto</option>
            <option value="modelscope">modelscope</option>
            <option value="huggingface">huggingface</option>
            <option value="ollama">ollama</option>
          </select>
        </label>
        <label><span>Target</span>
          <select id="target">
            <option value="vllm">vllm</option>
            <option value="ollama">ollama</option>
          </select>
        </label>
        <label><span>Revision (optional)</span><input id="revision" placeholder="main"></label>
        <p class="hint" id="dest-hint">${esc(destHint("", "auto", "vllm"))}</p>
        <div class="actions"><button class="primary" type="submit">Start download</button></div>
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
      $("app").insertAdjacentHTML("afterbegin", flash("ok", `Queued ${created.id}`));
    });
  });
}

function taskRow(task) {
  const canCancel = !TERMINAL.has(task.status);
  const canRetry = task.status === "failed" || task.status === "cancelled";
  return `<tr>
    <td class="mono" title="${esc(task.id)}">${esc(String(task.id).slice(0, 8))}</td>
    <td>${esc(task.name)}<div class="muted">${esc(task.source)} → ${esc(task.target)}</div></td>
    <td class="status ${esc(task.status)}">${esc(task.status)}</td>
    <td>${esc(progressLabel(task))}<div class="muted">${esc(fmtSpeed(task.speed_bps))}</div></td>
    <td>${esc(task.message || "")}</td>
    <td>
      <button data-act="cancel" data-id="${esc(task.id)}" ${canCancel ? "" : "disabled"}>Cancel</button>
      <button data-act="retry" data-id="${esc(task.id)}" ${canRetry ? "" : "disabled"}>Retry</button>
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
      : `<tr><td colspan="6" class="empty">No download tasks yet.</td></tr>`;
  } catch (err) {
    if (err.message === "unauthorized") {
      showLogin("Session expired. Sign in again.");
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
    <h1>Tasks</h1>
    <div id="task-error"></div>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Model</th>
            <th>Status</th>
            <th>Progress</th>
            <th>Message</th>
            <th></th>
          </tr>
        </thead>
        <tbody id="task-body">
          <tr><td colspan="6" class="empty">Loading…</td></tr>
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
    <h1>Library</h1>
    <div id="lib-msg"></div>
    <div class="panel">
      <table>
        <thead>
          <tr><th>Name</th><th>Path</th><th>Size</th><th>Target</th><th></th></tr>
        </thead>
        <tbody id="lib-body">
          <tr><td colspan="5" class="empty">Loading…</td></tr>
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
            <td>${esc(m.target)}</td>
            <td><button class="danger" data-del="${esc(m.id)}">Delete</button></td>
          </tr>`).join("")
        : `<tr><td colspan="5" class="empty">No models on disk.</td></tr>`;
    });
  };

  $("lib-body").addEventListener("click", async (event) => {
    const btn = event.target.closest("button[data-del]");
    if (!btn) return;
    const id = btn.getAttribute("data-del");
    if (!confirm(`Delete ${id}?`)) return;
    await guarded(async () => {
      await apiJson(`/api/models/${encodeURIComponent(id)}`, { method: "DELETE" });
      $("lib-msg").innerHTML = flash("ok", `Deleted ${id}`);
      await load();
    });
  });

  load();
}

function healthBadge(health) {
  if (!health) return `<span class="muted">unknown</span>`;
  return health.ok
    ? `<span class="status completed">connected</span> <span class="muted">${esc(health.detail || "")}</span>`
    : `<span class="status failed">down</span> <span class="muted">${esc(health.detail || "")}</span>`;
}

function renderServices() {
  $("app").innerHTML = `
    <h1>Services</h1>
    <div class="row">
      <div class="panel">
        <h2>Ollama</h2>
        <p id="ollama-health" class="muted">Checking…</p>
        <form id="ollama-pull" class="actions">
          <input id="ollama-name" placeholder="llama3.2" required style="max-width:240px">
          <button class="primary" type="submit">Pull</button>
          <button type="button" id="ollama-refresh">Refresh</button>
        </form>
        <div id="ollama-msg"></div>
        <table>
          <thead><tr><th>Name</th><th>Size</th></tr></thead>
          <tbody id="ollama-models"><tr><td colspan="2" class="empty">Loading…</td></tr></tbody>
        </table>
      </div>
      <div class="panel">
        <h2>vLLM</h2>
        <p id="vllm-health" class="muted">Checking…</p>
        <form id="vllm-form">
          <label><span>Model path</span><input id="vllm-model" required placeholder="/models/hf/Qwen/Qwen2.5-7B-Instruct"></label>
          <label><span>Port</span><input id="vllm-port" type="number" value="8000" min="1"></label>
          <div class="actions">
            <button class="primary" type="submit">Build command</button>
            <button type="button" id="copy-cmd" disabled>Copy</button>
            <button type="button" id="vllm-refresh">Refresh health</button>
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
          : `<tr><td colspan="2" class="empty">No Ollama models.</td></tr>`;
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
    $("ollama-msg").innerHTML = flash("", `Pulling ${name}…`);
    await guarded(async () => {
      await apiJson("/api/services/ollama/pull", { method: "POST", body: JSON.stringify({ name }) });
      $("ollama-msg").innerHTML = flash("ok", `Pulled ${name}`);
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
    $("vllm-cmd").insertAdjacentHTML("beforebegin", flash("ok", "Copied launch command"));
  });

  loadOllama();
  loadVllm();
}

function renderSettings() {
  $("app").innerHTML = `
    <h1>Settings</h1>
    <div id="set-msg"></div>
    <div class="panel">
      <form id="set-form">
        <label><span>HF endpoint</span><input id="hf_endpoint"></label>
        <label><span>HF token</span><input id="hf_token" type="password" autocomplete="off"></label>
        <label><span>ModelScope API token</span><input id="modelscope_api_token" type="password" autocomplete="off"></label>
        <label><span>Download concurrency</span><input id="download_concurrency" type="number" min="1"></label>
        <label><span>aria2 connections</span><input id="aria2_connections" type="number" min="1"></label>
        <label><span>Ollama base URL</span><input id="ollama_base_url"></label>
        <label><span>vLLM base URL</span><input id="vllm_base_url"></label>
        <div class="actions"><button class="primary" type="submit">Save</button></div>
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
      $("set-msg").innerHTML = flash("ok", "Settings saved");
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
