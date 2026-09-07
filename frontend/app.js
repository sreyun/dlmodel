const TOKEN_KEY = "dlmodel_token";
const ROUTES = ["download", "tasks", "library", "services", "settings"];
const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const PAGE_TITLE = {
  download: "下载模型",
  tasks: "下载任务",
  library: "模型库",
  services: "推理服务",
  settings: "设置",
};

const STATUS_LABEL = {
  queued: "排队中",
  running: "下载中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

let pollTimer = null;
let tasksInFlight = false;
let tasksRefreshGen = null;
let routeGen = 0;
let ignoreHashChange = false;
let lastTaskFingerprint = "";
let settingsDirty = false;
let taskFilter = "all"; // all | active | done | issue
let taskFlashUntil = 0;
let cachedTasks = [];

const DOWNLOAD_DRAFT_KEY = "dlmodel_download_draft";
const TASK_FILTER_KEY = "dlmodel_task_filter";

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

function networkMessage(err) {
  const raw = String(err && err.message ? err.message : err || "");
  if (!raw || raw === "Failed to fetch" || raw.includes("NetworkError") || raw.includes("fetch")) {
    return "网络异常，请检查服务是否已启动。";
  }
  if (raw === "unauthorized") return "unauthorized";
  return raw;
}

function flash(kind, message) {
  return message
    ? `<div class="flash ${kind}" role="alert">${esc(message)}</div>`
    : "";
}

function showFlash(targetId, kind, message) {
  if (!message) return;
  const el = targetId ? $(targetId) : null;
  if (el) {
    setHtml(el, flash(kind, message));
    if (targetId === "task-error" && kind === "ok") {
      taskFlashUntil = Date.now() + 3500;
    }
    return;
  }
  const host = $("toast-host");
  if (!host) return;
  const node = document.createElement("div");
  node.innerHTML = flash(kind, message);
  const flashEl = node.firstElementChild;
  if (!flashEl) return;
  host.appendChild(flashEl);
  setTimeout(() => flashEl.remove(), 3200);
}

function clearFlash(targetId) {
  const el = $(targetId);
  if (el) el.innerHTML = "";
}

function pageHead(title, desc, actionsHtml = "") {
  return `
    <div class="page-head">
      <div>
        <div class="eyebrow">MODEL HUB</div>
        <h1>${esc(title)}</h1>
        <p>${esc(desc)}</p>
      </div>
      ${actionsHtml ? `<div class="actions">${actionsHtml}</div>` : ""}
    </div>`;
}

function btnLink(href, label, primary = false) {
  return `<a class="btn${primary ? " primary" : ""}" href="${esc(href)}">${esc(label)}</a>`;
}

async function api(path, opts = {}) {
  const token = localStorage.getItem(TOKEN_KEY) || "";
  const headers = Object.assign({}, opts.headers || {}, {
    Authorization: `Bearer ${token}`,
  });
  if (opts.body != null && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) throw new Error("unauthorized");
  return res;
}

async function apiJson(path, opts = {}) {
  const res = await api(path, opts);
  const text = await res.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: text };
    }
  }
  if (!res.ok) throw new Error(errorMessage(data, res.statusText));
  return data;
}

function errorMessage(data, fallback) {
  const detail = data && data.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
  }
  if (detail != null) return JSON.stringify(detail);
  return fallback || "请求失败";
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) =>
    ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[ch],
  );
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

function fmtEta(progressBytes, totalBytes, speedBps) {
  if (!totalBytes || !speedBps || speedBps <= 0) return null;
  const remain = Math.max(totalBytes - (progressBytes || 0), 0);
  if (remain <= 0) return "即将完成";
  const seconds = Math.ceil(remain / speedBps);
  if (seconds < 60) return `约 ${seconds} 秒`;
  if (seconds < 3600) return `约 ${Math.ceil(seconds / 60)} 分钟`;
  return `约 ${(seconds / 3600).toFixed(1)} 小时`;
}

function progressPct(task) {
  if (!task.total_bytes || !Number(task.total_bytes)) return null;
  return Math.min(100, (100 * Number(task.progress_bytes || 0)) / Number(task.total_bytes));
}

function progressLabel(task) {
  const pct = progressPct(task);
  if (pct == null) {
    if (task.status === "running" || task.status === "queued") {
      return `已下载 ${fmtBytes(task.progress_bytes)}`;
    }
    return fmtBytes(task.progress_bytes);
  }
  return `${pct.toFixed(1)}% · ${fmtBytes(task.progress_bytes)} / ${fmtBytes(task.total_bytes)}`;
}

function friendlyMessage(message, status) {
  const raw = String(message || "").trim();
  if (!raw) {
    if (status === "queued") return "等待调度开始…";
    if (status === "running") return "正在准备下载…";
    if (status === "completed") return "下载已完成，可在模型库中查看。";
    if (status === "cancelled") return "任务已取消。";
    return "";
  }
  if (status === "cancelled" && (raw === "已取消" || raw === "队列已停止" || raw.includes("服务关闭"))) {
    if (raw === "队列已停止" || raw.includes("服务关闭")) {
      return "服务已关闭，任务已保留，重启后会继续。";
    }
    return "任务已取消。";
  }
  const lower = raw.toLowerCase();
  if (lower.includes("illegal header value") && lower.includes("bearer")) {
    return "HF Token 为空或格式无效。请到「设置」填写有效 Token，或清除无效配置后重试。";
  }
  if (lower.includes("401") || lower.includes("unauthorized")) {
    return "鉴权失败，请检查 HF / ModelScope Token。";
  }
  if (
    lower.includes("unexpected_eof") ||
    lower.includes("eof occurred") ||
    lower.includes("ssl:") ||
    lower.includes("ssl error") ||
    lower.includes("connection reset") ||
    lower.includes("remote protocol")
  ) {
    return "下载链路中断（常见于国内镜像 TLS 抖动）。可点「重试」；系统会自动断点续传并重试瞬时错误。";
  }
  if (lower.includes("not found") || raw.includes("未找到模型")) {
    return "未找到该模型，请确认名称与下载源是否正确。";
  }
  if (raw.includes("服务重启后重新排队") || raw.includes("下次启动后继续")) {
    return raw;
  }
  // "ModelScope 已下载 2722303781 字节…" → human size
  return raw.replace(/(\d+)\s*字节/g, (_, n) => fmtBytes(Number(n)));
}

function taskSummary(tasks) {
  const counts = { queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0 };
  tasks.forEach((t) => {
    if (counts[t.status] != null) counts[t.status] += 1;
  });
  return counts;
}

function statusLabel(status) {
  return STATUS_LABEL[status] || status;
}

function sourceLabel(source) {
  return (
    {
      auto: "自动",
      modelscope: "ModelScope 魔搭",
      huggingface: "Hugging Face",
      ollama: "Ollama",
    }[source] || source
  );
}

function targetLabel(target) {
  return ({ vllm: "vLLM", ollama: "Ollama" })[target] || target;
}

function destHint(name, source, target) {
  const model = (name || "").trim();
  if (!model) return "输入模型名称后可预览保存路径";
  if (target === "ollama") return `Ollama 模型库 · ${model}`;
  return `hf/${model}`;
}

function pairWarning(source, target) {
  if (target === "ollama" && source !== "auto" && source !== "ollama") {
    return "Ollama 目标仅支持「自动」或「Ollama」源。";
  }
  if (target === "vllm" && source === "ollama") {
    return "vLLM 目标不支持 Ollama 源，请改用自动 / HF / 魔搭。";
  }
  return "";
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
  document.querySelectorAll("nav a[data-nav]").forEach((link) => {
    const active = link.getAttribute("data-nav") === page;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  document.title = `${PAGE_TITLE[page] || "控制台"} · 模型下载管理`;
}

function setBusy(btn, busy, busyText) {
  if (!btn) return;
  if (busy) {
    btn.dataset.label = btn.dataset.label || btn.textContent;
    btn.disabled = true;
    if (busyText) btn.textContent = busyText;
  } else {
    btn.disabled = false;
    if (btn.dataset.label) btn.textContent = btn.dataset.label;
  }
}

async function guarded(fn, opts = {}) {
  try {
    await fn();
  } catch (err) {
    const msg = networkMessage(err);
    if (msg === "unauthorized") {
      showLogin("登录已过期，请重新登录。");
      return;
    }
    if (opts.gen != null && opts.page && !stillOn(opts.page, opts.gen)) return;
    showFlash(opts.flashId || null, "error", msg);
  }
}

function showLogin(message) {
  stopPoll();
  routeGen += 1;
  setNavVisible(false);
  document.title = "登录 · 模型下载管理";
  const root = appRoot();
  if (!root) return;
  setHtml(
    root,
    `
    <div class="login-shell">
      <div class="login-card">
        <section class="login-visual">
          <div class="eyebrow" style="background:rgba(255,255,255,.12);border-color:rgba(255,255,255,.18);color:#fff;">LOCAL MODEL OPS</div>
          <h1>本机模型中心</h1>
          <p>一站完成模型下载、目录管理，并挂接 vLLM / Ollama。针对国内网络做了镜像与加速默认配置。</p>
          <ul>
            <li>只填模型名，自动选择魔搭 / HF 镜像</li>
            <li>任务进度、速度、ETA 一眼可见</li>
            <li>管理端与推理服务可分离部署</li>
          </ul>
        </section>
        <section class="login-form-pane">
          <h2>管理员登录</h2>
          <p class="lead">使用环境变量 <span class="mono">ADMIN_TOKEN</span>。令牌仅保存在本浏览器。</p>
          ${flash("error", message)}
          <form id="login-form">
            <label class="field">
              <span>管理员令牌</span>
              <input id="login-token" name="token" type="password" autocomplete="off" required placeholder="请输入 ADMIN_TOKEN">
            </label>
            <div class="actions mt">
              <button class="primary" id="login-submit" type="submit">进入控制台</button>
              <button type="button" id="toggle-token" class="ghost">显示</button>
            </div>
          </form>
        </section>
      </div>
    </div>`,
  );
  const tokenInput = $("login-token");
  if (tokenInput) tokenInput.focus();
  const toggle = $("toggle-token");
  if (toggle && tokenInput) {
    toggle.addEventListener("click", () => {
      const show = tokenInput.type === "password";
      tokenInput.type = show ? "text" : "password";
      toggle.textContent = show ? "隐藏" : "显示";
    });
  }
  const form = $("login-form");
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("login-token");
    const submit = $("login-submit");
    if (!input) return;
    localStorage.setItem(TOKEN_KEY, input.value.trim());
    setBusy(submit, true, "验证中…");
    try {
      await apiJson("/api/auth/verify", { method: "POST" });
      await showApp();
    } catch (err) {
      localStorage.removeItem(TOKEN_KEY);
      showLogin(err.message === "unauthorized" ? "令牌无效，请重试" : networkMessage(err));
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

function renderDownload(gen) {
  const root = appRoot();
  if (!root) return;
  setHtml(
    root,
    `
    ${pageHead(
      "下载模型",
      "填写模型名即可；推荐「自动」源（优先魔搭，失败回落 HF 镜像）。",
      btnLink("#/tasks", "查看任务"),
    )}
    <div id="dl-msg" aria-live="polite"></div>
    <div class="panel">
      <div class="panel-title">
        <div>
          <h2>新建下载任务</h2>
          <p>支持 Hugging Face、ModelScope、Ollama；目标可落盘到 vLLM 或 Ollama。</p>
        </div>
      </div>
      <form id="dl-form">
        <div class="form-grid">
          <label class="field full">
            <span>模型名称</span>
            <input id="name" required aria-describedby="dest-hint" placeholder="例如 Qwen/Qwen2.5-7B-Instruct 或 llama3.2">
          </label>
          <div class="full">
            <fieldset class="choice-set">
              <legend>下载源</legend>
              <div class="choice-grid" id="source-choices">
                <label class="choice"><input type="radio" name="source" value="auto" checked><strong>自动</strong><span>优先魔搭，回落 HF 镜像</span></label>
                <label class="choice"><input type="radio" name="source" value="modelscope"><strong>ModelScope</strong><span>魔搭社区直连</span></label>
                <label class="choice"><input type="radio" name="source" value="huggingface"><strong>Hugging Face</strong><span>走国内镜像加速</span></label>
                <label class="choice"><input type="radio" name="source" value="ollama"><strong>Ollama</strong><span>官方库 pull</span></label>
              </div>
            </fieldset>
          </div>
          <div class="full">
            <fieldset class="choice-set">
              <legend>目标用途</legend>
              <div class="choice-grid" id="target-choices">
                <label class="choice"><input type="radio" name="target" value="vllm" checked><strong>vLLM</strong><span>保存为 HF 目录布局</span></label>
                <label class="choice"><input type="radio" name="target" value="ollama"><strong>Ollama</strong><span>写入 Ollama 模型目录</span></label>
              </div>
            </fieldset>
            <div id="pair-warn" class="pair-warn" hidden></div>
          </div>
          <label class="field">
            <span>版本 / Revision（可选）</span>
            <input id="revision" placeholder="main">
          </label>
          <div class="field">
            <span class="muted" style="display:block;margin-bottom:6px;font-size:.84rem;font-weight:560;">保存路径预览</span>
            <div class="path-preview" id="dest-hint">${esc(destHint("", "auto", "vllm"))}</div>
          </div>
        </div>
        <div class="actions mt">
          <button class="primary" id="dl-submit" type="submit">开始下载</button>
          <button type="button" id="dl-reset">清空</button>
        </div>
      </form>
    </div>`,
  );

  const selected = (name) => {
    const el = document.querySelector(`input[name="${name}"]:checked`);
    return el ? el.value : name === "source" ? "auto" : "vllm";
  };

  const readDraft = () => {
    try {
      return JSON.parse(localStorage.getItem(DOWNLOAD_DRAFT_KEY) || "null");
    } catch {
      return null;
    }
  };

  const writeDraft = () => {
    const nameEl = $("name");
    const revisionEl = $("revision");
    const draft = {
      name: nameEl ? nameEl.value : "",
      source: selected("source"),
      target: selected("target"),
      revision: revisionEl ? revisionEl.value : "",
    };
    localStorage.setItem(DOWNLOAD_DRAFT_KEY, JSON.stringify(draft));
  };

  const applyDraft = (draft) => {
    if (!draft || typeof draft !== "object") return;
    const nameEl = $("name");
    const revisionEl = $("revision");
    if (nameEl && draft.name != null) nameEl.value = String(draft.name);
    if (revisionEl && draft.revision != null) revisionEl.value = String(draft.revision);
    const allowedSource = new Set(["auto", "modelscope", "huggingface", "ollama"]);
    const allowedTarget = new Set(["vllm", "ollama"]);
    if (allowedSource.has(draft.source)) {
      const src = document.querySelector(`input[name="source"][value="${draft.source}"]`);
      if (src) src.checked = true;
    }
    if (allowedTarget.has(draft.target)) {
      const tgt = document.querySelector(`input[name="target"][value="${draft.target}"]`);
      if (tgt) tgt.checked = true;
    }
  };

  const updateHint = () => {
    const hint = $("dest-hint");
    const name = $("name");
    const warn = $("pair-warn");
    const submit = $("dl-submit");
    if (!hint || !name) return;
    const source = selected("source");
    const target = selected("target");
    hint.textContent = destHint(name.value, source, target);
    const warning = pairWarning(source, target);
    if (warn) {
      warn.hidden = !warning;
      warn.textContent = warning;
    }
    if (submit) submit.disabled = Boolean(warning);
    writeDraft();
  };

  applyDraft(readDraft());

  ["name", "source-choices", "target-choices", "revision"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("input", updateHint);
    el.addEventListener("change", updateHint);
  });

  const resetBtn = $("dl-reset");
  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      const form = $("dl-form");
      if (form) form.reset();
      localStorage.removeItem(DOWNLOAD_DRAFT_KEY);
      updateHint();
    });
  }

  const form = $("dl-form");
  if (!form) return;
  updateHint();
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!stillOn("download", gen)) return;
    const nameEl = $("name");
    const revisionEl = $("revision");
    const submit = $("dl-submit");
    if (!nameEl || (submit && submit.disabled)) return;
    const body = {
      name: nameEl.value.trim(),
      source: selected("source"),
      target: selected("target"),
    };
    const revision = revisionEl ? revisionEl.value.trim() : "";
    if (revision) body.revision = revision;
    setBusy(submit, true, "提交中…");
    await guarded(
      async () => {
        const created = await apiJson("/api/downloads", {
          method: "POST",
          body: JSON.stringify(body),
        });
        localStorage.removeItem(DOWNLOAD_DRAFT_KEY);
        const shortId = created && created.id ? `${String(created.id).slice(0, 8)}…` : "";
        sessionStorage.setItem("dlmodel_flash", shortId ? `已加入队列：${shortId}` : "已加入队列");
        location.hash = "#/tasks";
      },
      { gen, page: "download", flashId: "dl-msg" },
    );
    if (stillOn("download", gen)) setBusy(submit, false);
  });
}

function filterTasks(tasks, filter) {
  if (filter === "active") {
    return tasks.filter((t) => t.status === "running" || t.status === "queued");
  }
  if (filter === "done") {
    return tasks.filter((t) => t.status === "completed");
  }
  if (filter === "issue") {
    return tasks.filter((t) => t.status === "failed" || t.status === "cancelled");
  }
  return tasks;
}

function taskCard(task) {
  const canCancel = !TERMINAL.has(task.status);
  const canRetry = task.status === "failed" || task.status === "cancelled";
  const canDelete = task.status === "completed" || task.status === "failed" || task.status === "cancelled";
  const pct = progressPct(task);
  const showSpeed = task.status === "running" || task.status === "queued";
  const eta = showSpeed
    ? fmtEta(task.progress_bytes, task.total_bytes, task.speed_bps)
    : null;
  const msg = friendlyMessage(task.message, task.status);
  const fillClass = [
    "progress-fill",
    task.status === "completed" ? "done" : "",
    task.status === "failed" ? "failed" : "",
    task.status === "cancelled" ? "cancelled" : "",
    (task.status === "running" || task.status === "queued") && pct == null
      ? "indeterminate"
      : "",
  ]
    .filter(Boolean)
    .join(" ");
  let width = 0;
  if (task.status === "cancelled") width = 100;
  else if (pct == null) width = task.status === "failed" || task.status === "completed" ? 100 : 35;
  else width = pct;
  const rightMeta = showSpeed
    ? `${esc(fmtSpeed(task.speed_bps))}${eta ? ` · ETA ${eta}` : ""}`
    : "";
  const showMsg =
    msg &&
    (task.status === "running" ||
      task.status === "queued" ||
      task.status === "failed" ||
      (task.status === "cancelled" && msg !== "任务已取消。"));
  return `<article class="task-card status-${esc(task.status)} ${task.status === "running" ? "is-running" : ""}" data-id="${esc(task.id)}">
    <div class="task-top">
      <div class="task-main">
        <div class="task-title-row">
          <div class="task-title">${esc(task.name)}</div>
          <span class="chip status-${esc(task.status)}">${esc(statusLabel(task.status))}</span>
        </div>
        <div class="task-meta">
          <span class="chip">${esc(sourceLabel(task.source))} → ${esc(targetLabel(task.target))}</span>
          <span class="chip mono" title="${esc(task.id)}">#${esc(String(task.id).slice(0, 8))}</span>
          ${rightMeta ? `<span class="chip muted-chip">${rightMeta}</span>` : ""}
        </div>
      </div>
      <div class="task-actions">
        ${canCancel ? `<button type="button" data-act="cancel" data-id="${esc(task.id)}" title="取消下载">取消</button>` : ""}
        ${canRetry ? `<button type="button" class="primary" data-act="retry" data-id="${esc(task.id)}" title="重新加入队列">重试</button>` : ""}
        ${canDelete ? `<button type="button" class="danger" data-act="delete" data-id="${esc(task.id)}" data-name="${esc(task.name)}" title="删除任务记录（不删模型文件）">删除</button>` : ""}
      </div>
    </div>
    <div class="progress-block">
      <div class="progress-row">
        <span>${esc(progressLabel(task))}</span>
      </div>
      <div class="progress-track"><div class="${fillClass}" style="width:${width}%"></div></div>
    </div>
    ${showMsg ? `<div class="task-message ${task.status === "failed" ? "error" : ""}">${esc(msg)}</div>` : ""}
  </article>`;
}

function tasksFingerprint(tasks, filter) {
  return (
    filter +
    "|" +
    tasks
      .map(
        (t) =>
          `${t.id}:${t.status}:${t.progress_bytes}:${t.total_bytes}:${t.speed_bps}:${t.message}`,
      )
      .join("|")
  );
}

function renderTaskFilterBar(counts) {
  const items = [
    ["all", "全部", counts.queued + counts.running + counts.completed + counts.failed + counts.cancelled],
    ["active", "进行中", counts.queued + counts.running],
    ["done", "已完成", counts.completed],
    ["issue", "失败/取消", counts.failed + counts.cancelled],
  ];
  return items
    .map(
      ([key, label, n]) =>
        `<button type="button" class="filter-chip ${taskFilter === key ? "active" : ""}" data-filter="${key}">${label}<span class="count">${n}</span></button>`,
    )
    .join("");
}

function paintTaskList(tasks) {
  const body = $("task-list");
  if (!body) return;
  const filtered = filterTasks(tasks, taskFilter);
  setHtml(
    body,
    filtered.length
      ? filtered.map(taskCard).join("")
      : `<div class="empty"><strong>没有符合条件的任务</strong>${
          taskFilter === "all"
            ? "去「下载」页提交第一个模型。"
            : "切换筛选，或新建下载任务。"
        }</div>`,
  );
}

async function refreshTasks(gen) {
  const myGen = gen ?? routeGen;
  if (!stillOn("tasks", myGen)) return;
  if (tasksInFlight) {
    tasksRefreshGen = myGen;
    return;
  }
  tasksInFlight = true;
  try {
    const tasks = await apiJson("/api/downloads");
    if (!stillOn("tasks", myGen)) return;
    cachedTasks = tasks;
    const box = $("task-error");
    if (box) {
      const hasOk = box.querySelector(".flash.ok");
      const hasErr = box.querySelector(".flash.error");
      if (hasErr) clearFlash("task-error");
      else if (hasOk && Date.now() > taskFlashUntil) clearFlash("task-error");
    }
    const counts = taskSummary(tasks);
    const stats = $("task-stats");
    if (stats) {
      setHtml(
        stats,
        `
        <button type="button" class="stat clickable ${taskFilter === "active" ? "selected" : ""}" data-filter="active"><div class="label">进行中</div><div class="value accent">${counts.running}</div></button>
        <button type="button" class="stat clickable" data-filter="active"><div class="label">排队中</div><div class="value">${counts.queued}</div></button>
        <button type="button" class="stat clickable ${taskFilter === "done" ? "selected" : ""}" data-filter="done"><div class="label">已完成</div><div class="value ok">${counts.completed}</div></button>
        <button type="button" class="stat clickable ${taskFilter === "issue" ? "selected" : ""}" data-filter="issue"><div class="label">失败 / 取消</div><div class="value danger">${counts.failed + counts.cancelled}</div></button>
      `,
      );
    }
    const filters = $("task-filters");
    if (filters) setHtml(filters, renderTaskFilterBar(counts));
    const clearBtn = $("clear-completed");
    if (clearBtn) {
      clearBtn.disabled = counts.completed < 1;
      clearBtn.textContent = counts.completed
        ? `清除已完成 (${counts.completed})`
        : "清除已完成";
    }
    const stamp = $("task-updated");
    if (stamp) {
      const now = new Date();
      stamp.textContent = `更新于 ${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}:${String(now.getSeconds()).padStart(2, "0")}`;
    }
    const nextFp = tasksFingerprint(tasks, taskFilter);
    const active = document.activeElement;
    const focusAct = active && active.getAttribute ? active.getAttribute("data-act") : null;
    const focusId = active && active.getAttribute ? active.getAttribute("data-id") : null;
    if (nextFp !== lastTaskFingerprint) {
      lastTaskFingerprint = nextFp;
      paintTaskList(tasks);
      const body = $("task-list");
      if (body && focusAct && focusId) {
        const btn = body.querySelector(`button[data-act="${focusAct}"][data-id="${focusId}"]`);
        if (btn) btn.focus();
      }
    }
    const activeCount = counts.running + counts.queued;
    stopPoll();
    pollTimer = setInterval(
      () => refreshTasks(myGen),
      activeCount > 0 ? 1000 : 4000,
    );
  } catch (err) {
    const msg = networkMessage(err);
    if (msg === "unauthorized") {
      showLogin("登录已过期，请重新登录。");
      return;
    }
    if (!stillOn("tasks", myGen)) return;
    showFlash("task-error", "error", msg);
  } finally {
    tasksInFlight = false;
    const pendingGen = tasksRefreshGen;
    tasksRefreshGen = null;
    if (pendingGen != null && stillOn("tasks", pendingGen)) {
      queueMicrotask(() => refreshTasks(pendingGen));
    }
  }
}

function renderTasks(gen) {
  const root = appRoot();
  if (!root) return;
  lastTaskFingerprint = "";
  cachedTasks = [];
  tasksRefreshGen = null;
  try {
    const savedFilter = sessionStorage.getItem(TASK_FILTER_KEY);
    if (savedFilter && ["all", "active", "done", "issue"].includes(savedFilter)) {
      taskFilter = savedFilter;
    }
  } catch {
    /* ignore */
  }
  const pending = sessionStorage.getItem("dlmodel_flash");
  if (pending) {
    sessionStorage.removeItem("dlmodel_flash");
    taskFlashUntil = Date.now() + 3500;
  }
  setHtml(
    root,
    `
    ${pageHead(
      "下载任务",
      "查看进度；已完成 / 失败 / 已取消均可删除记录。任务与配置保存在本地 SQLite，重启后继续。",
      `${btnLink("#/download", "新建下载", true)}`,
    )}
    <div id="task-error" aria-live="polite">${pending ? flash("ok", pending) : ""}</div>
    <div class="stats" id="task-stats"></div>
    <div class="task-toolbar">
      <div class="filter-bar" id="task-filters" role="tablist" aria-label="任务筛选"></div>
      <div class="toolbar-right">
        <p class="task-updated" id="task-updated">正在同步…</p>
        <button type="button" id="clear-completed" class="ghost" disabled>清除已完成</button>
      </div>
    </div>
    <div id="task-list" class="task-list" aria-live="polite">
      <div class="empty"><div class="skeleton" style="width:40%;margin:0 auto 10px;"></div>加载中…</div>
    </div>`,
  );

  const applyFilter = (next) => {
    if (!next || next === taskFilter) {
      if (next === taskFilter) {
        lastTaskFingerprint = "";
        paintTaskList(cachedTasks);
      }
      return;
    }
    taskFilter = next;
    try {
      sessionStorage.setItem(TASK_FILTER_KEY, taskFilter);
    } catch {
      /* ignore */
    }
    lastTaskFingerprint = "";
    const counts = taskSummary(cachedTasks);
    const filters = $("task-filters");
    if (filters) setHtml(filters, renderTaskFilterBar(counts));
    paintTaskList(cachedTasks);
    refreshTasks(gen);
  };

  const stats = $("task-stats");
  if (stats) {
    stats.addEventListener("click", (event) => {
      const btn = event.target.closest("[data-filter]");
      if (!btn || !stillOn("tasks", gen)) return;
      applyFilter(btn.getAttribute("data-filter"));
    });
  }
  const filters = $("task-filters");
  if (filters) {
    filters.addEventListener("click", (event) => {
      const btn = event.target.closest("[data-filter]");
      if (!btn || !stillOn("tasks", gen)) return;
      applyFilter(btn.getAttribute("data-filter"));
    });
  }

  const clearBtn = $("clear-completed");
  if (clearBtn) {
    clearBtn.addEventListener("click", async () => {
      if (!stillOn("tasks", gen)) return;
      const n = taskSummary(cachedTasks).completed;
      if (!n) return;
      if (!confirm(`确认清除 ${n} 条已完成任务记录？\n不会删除模型文件。`)) return;
      setBusy(clearBtn, true, "清除中…");
      try {
        await guarded(
          async () => {
            const res = await apiJson("/api/downloads/cleanup", {
              method: "POST",
              body: JSON.stringify({ statuses: ["completed"] }),
            });
            lastTaskFingerprint = "";
            showFlash("task-error", "ok", `已清除 ${res.deleted || n} 条已完成任务`);
            await refreshTasks(gen);
          },
          { gen, page: "tasks", flashId: "task-error" },
        );
      } finally {
        if (stillOn("tasks", gen) && $("clear-completed")) {
          setBusy($("clear-completed"), false);
        }
      }
    });
  }

  const list = $("task-list");
  if (list) {
    list.addEventListener("click", async (event) => {
      const btn = event.target.closest("button[data-act]");
      if (!btn || btn.disabled || !stillOn("tasks", gen)) return;
      const id = btn.getAttribute("data-id");
      const act = btn.getAttribute("data-act");
      if (act === "cancel") {
        if (!confirm("确认取消该下载任务？进行中的传输将被中断。")) return;
      }
      if (act === "delete") {
        const name = btn.getAttribute("data-name") || id;
        if (!confirm(`确认删除任务「${name}」？\n仅删除任务记录，不会删除已下载的模型文件。`)) return;
        setBusy(btn, true, "删除中…");
        try {
          await guarded(
            async () => {
              await apiJson(`/api/downloads/${encodeURIComponent(id)}`, {
                method: "DELETE",
              });
              lastTaskFingerprint = "";
              showFlash("task-error", "ok", `已删除任务 ${name}`);
              await refreshTasks(gen);
            },
            { gen, page: "tasks", flashId: "task-error" },
          );
        } finally {
          if (stillOn("tasks", gen) && document.body.contains(btn)) {
            setBusy(btn, false);
          }
        }
        return;
      }
      setBusy(btn, true, act === "cancel" ? "取消中…" : "重试中…");
      try {
        await guarded(
          async () => {
            await apiJson(`/api/downloads/${encodeURIComponent(id)}/${act}`, {
              method: "POST",
            });
            lastTaskFingerprint = "";
            await refreshTasks(gen);
          },
          { gen, page: "tasks", flashId: "task-error" },
        );
      } finally {
        if (stillOn("tasks", gen) && document.body.contains(btn)) {
          setBusy(btn, false);
        }
      }
    });
  }

  refreshTasks(gen);
}

function modelCard(m) {
  const shortId = String(m.id || "").length > 24 ? `${String(m.id).slice(0, 18)}…` : m.id;
  return `<article class="model-card" data-id="${esc(m.id)}">
    <div class="model-top">
      <div>
        <div class="model-title">${esc(m.name)}</div>
        <div class="chip-row">
          <span class="chip">${esc(targetLabel(m.target))}</span>
          <span class="chip">${esc(fmtBytes(m.size_bytes))}</span>
          <span class="chip mono" title="${esc(m.id)}">${esc(shortId)}</span>
        </div>
        <div class="model-path">${esc(m.path)}</div>
      </div>
      <div class="model-actions">
        <button type="button" class="primary" data-use-vllm="${esc(m.path)}">用于 vLLM</button>
        <button type="button" data-copy="${esc(m.path)}">复制路径</button>
        <button class="danger" data-del="${esc(m.id)}" data-name="${esc(m.name)}">删除</button>
      </div>
    </div>
  </article>`;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const box = document.createElement("textarea");
      box.value = text;
      document.body.appendChild(box);
      box.select();
      const ok = document.execCommand("copy");
      box.remove();
      return ok;
    } catch {
      return false;
    }
  }
}

function renderLibrary(gen) {
  const root = appRoot();
  if (!root) return;
  setHtml(
    root,
    `
    ${pageHead(
      "模型库",
      "浏览已下载的 HF 布局权重，核对占用并按需清理。",
      `${btnLink("#/download", "去下载", true)}<button type="button" id="lib-refresh">刷新</button>`,
    )}
    <div id="lib-msg" aria-live="polite"></div>
    <div id="lib-stats" class="stats" style="grid-template-columns:repeat(2,minmax(0,1fr));">
      <div class="stat"><div class="label">模型数量</div><div class="value">—</div></div>
      <div class="stat"><div class="label">总占用</div><div class="value">—</div></div>
    </div>
    <div id="lib-body" class="model-list">
      <div class="empty"><div class="skeleton" style="width:40%;margin:0 auto 10px;"></div>加载中…</div>
    </div>`,
  );

  const load = async () => {
    await guarded(
      async () => {
        const models = await apiJson("/api/models");
        if (!stillOn("library", gen)) return;
        const total = models.reduce((sum, m) => sum + (Number(m.size_bytes) || 0), 0);
        const stats = $("lib-stats");
        if (stats) {
          setHtml(
            stats,
            `
          <div class="stat"><div class="label">模型数量</div><div class="value">${models.length}</div></div>
          <div class="stat"><div class="label">总占用</div><div class="value">${esc(fmtBytes(total))}</div></div>
        `,
          );
        }
        const body = $("lib-body");
        if (!body) return;
        setHtml(
          body,
          models.length
            ? models.map(modelCard).join("")
            : `<div class="empty"><strong>磁盘上还没有 HF 模型</strong>下载到 vLLM 目标后会出现在这里。Ollama 模型请到「服务」页查看。</div>`,
        );
      },
      { gen, page: "library", flashId: "lib-msg" },
    );
  };

  const refresh = $("lib-refresh");
  if (refresh) refresh.addEventListener("click", () => load());

  const body = $("lib-body");
  if (body) {
    body.addEventListener("click", async (event) => {
      const useBtn = event.target.closest("button[data-use-vllm]");
      if (useBtn && stillOn("library", gen)) {
        sessionStorage.setItem("dlmodel_vllm_model", useBtn.getAttribute("data-use-vllm") || "");
        location.hash = "#/services";
        return;
      }
      const copyBtn = event.target.closest("button[data-copy]");
      if (copyBtn && stillOn("library", gen)) {
        const ok = await copyText(copyBtn.getAttribute("data-copy") || "");
        showFlash("lib-msg", ok ? "ok" : "error", ok ? "路径已复制" : "复制失败");
        return;
      }
      const btn = event.target.closest("button[data-del]");
      if (!btn || !stillOn("library", gen)) return;
      const id = btn.getAttribute("data-del");
      const name = btn.getAttribute("data-name") || id;
      if (!confirm(`确认删除「${name}」？\n${id}\n此操作不可恢复。`)) return;
      setBusy(btn, true, "删除中…");
      await guarded(
        async () => {
          await apiJson(`/api/models/${encodeURIComponent(id)}`, { method: "DELETE" });
          if (!stillOn("library", gen)) return;
          showFlash("lib-msg", "ok", `已删除 ${name}`);
          await load();
        },
        { gen, page: "library", flashId: "lib-msg" },
      );
    });
  }

  load();
}

function healthChip(health, loading = false) {
  if (loading) return `<span class="chip">检查中…</span>`;
  if (!health) return `<span class="chip">状态未知</span>`;
  return health.ok
    ? `<span class="chip ok">已连接 · ${esc(health.detail || "正常")}</span>`
    : `<span class="chip down">不可达 · ${esc(health.detail || "请检查地址")}</span>`;
}

function renderServices(gen) {
  const root = appRoot();
  if (!root) return;
  setHtml(
    root,
    `
    ${pageHead("推理服务", "检查 Ollama / vLLM，拉取模型或复制启动命令。")}
    <div class="service-grid">
      <section class="service-card">
        <div class="service-top">
          <div>
            <h2>Ollama</h2>
            <p class="lead">适合本地对话与快速试跑。</p>
          </div>
          <div id="ollama-health">${healthChip(null, true)}</div>
        </div>
        <div class="service-body">
          <form id="ollama-pull" class="actions">
            <label class="field" style="flex:1;min-width:160px;margin:0;">
              <span>模型名称</span>
              <input id="ollama-name" placeholder="llama3.2" required>
            </label>
            <button class="primary" id="ollama-submit" type="submit">拉取</button>
            <button type="button" id="ollama-refresh">刷新</button>
          </form>
          <div id="ollama-msg" aria-live="polite"></div>
          <div id="ollama-models" class="model-list" style="margin-top:14px;">
            <div class="empty"><div class="skeleton" style="width:40%;margin:0 auto 10px;"></div>加载中…</div>
          </div>
        </div>
      </section>
      <section class="service-card">
        <div class="service-top">
          <div>
            <h2>vLLM</h2>
            <p class="lead">高性能推理；生成可复制的启动命令。</p>
          </div>
          <div id="vllm-health">${healthChip(null, true)}</div>
        </div>
        <div class="service-body">
          <form id="vllm-form" class="form-grid">
            <label class="field full">
              <span>模型路径</span>
              <input id="vllm-model" required list="lib-paths" placeholder="/models/hf/Qwen/Qwen2.5-7B-Instruct">
              <datalist id="lib-paths"></datalist>
            </label>
            <label class="field">
              <span>端口</span>
              <input id="vllm-port" type="number" value="8000" min="1" max="65535" required>
            </label>
            <div class="actions" style="align-items:end;">
              <button class="primary" id="vllm-submit" type="submit">生成命令</button>
              <button type="button" id="copy-cmd" disabled>复制</button>
              <button type="button" id="vllm-refresh">刷新状态</button>
            </div>
          </form>
          <div class="cmd-box" id="vllm-cmd" tabindex="0">生成后将显示启动命令</div>
          <div id="vllm-msg" aria-live="polite"></div>
        </div>
      </section>
    </div>`,
  );

  const hydrateLibraryPaths = async () => {
    try {
      const models = await apiJson("/api/models");
      const list = $("lib-paths");
      if (!list || !stillOn("services", gen)) return;
      list.innerHTML = models
        .map((m) => `<option value="${esc(m.path)}"></option>`)
        .join("");
    } catch {
      /* optional */
    }
  };

  const loadOllama = async () => {
    setHtml($("ollama-health"), healthChip(null, true));
    await guarded(
      async () => {
        const health = await apiJson("/api/services/ollama/health");
        if (!stillOn("services", gen)) return;
        setHtml($("ollama-health"), healthChip(health));
        try {
          const models = await apiJson("/api/services/ollama/models");
          if (!stillOn("services", gen)) return;
          setHtml(
            $("ollama-models"),
            models.length
              ? models
                  .map(
                    (m) =>
                      `<div class="model-card"><div class="model-title">${esc(m.name || m.model || "")}</div><div class="chip-row"><span class="chip">${esc(fmtBytes(m.size))}</span></div></div>`,
                  )
                  .join("")
              : `<div class="empty"><strong>暂无 Ollama 模型</strong>可在上方输入名称后拉取。</div>`,
          );
        } catch (err) {
          if (!stillOn("services", gen)) return;
          setHtml($("ollama-models"), `<div class="empty">${esc(networkMessage(err))}</div>`);
        }
      },
      { gen, page: "services", flashId: "ollama-msg" },
    );
  };

  const loadVllm = async () => {
    setHtml($("vllm-health"), healthChip(null, true));
    await guarded(
      async () => {
        const health = await apiJson("/api/services/vllm/health");
        if (!stillOn("services", gen)) return;
        setHtml($("vllm-health"), healthChip(health));
      },
      { gen, page: "services", flashId: "vllm-msg" },
    );
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
      const submit = $("ollama-submit");
      if (!nameEl) return;
      const name = nameEl.value.trim();
      setBusy(submit, true, "拉取中…");
      showFlash("ollama-msg", "info", `正在拉取 ${name}，大模型可能需要较长时间…`);
      await guarded(
        async () => {
          await apiJson("/api/services/ollama/pull", {
            method: "POST",
            body: JSON.stringify({ name }),
          });
          if (!stillOn("services", gen)) return;
          nameEl.value = "";
          showFlash("ollama-msg", "ok", `已拉取 ${name}`);
          await loadOllama();
        },
        { gen, page: "services", flashId: "ollama-msg" },
      );
      if (stillOn("services", gen)) setBusy(submit, false);
    });
  }

  const vllmForm = $("vllm-form");
  if (vllmForm) {
    vllmForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!stillOn("services", gen)) return;
      const modelEl = $("vllm-model");
      const portEl = $("vllm-port");
      const submit = $("vllm-submit");
      if (!modelEl) return;
      const model = modelEl.value.trim();
      const port = Number((portEl && portEl.value) || "8000");
      if (!Number.isInteger(port) || port < 1 || port > 65535) {
        showFlash("vllm-msg", "error", "端口需为 1–65535 的整数。");
        return;
      }
      setBusy(submit, true, "生成中…");
      await guarded(
        async () => {
          const qs = new URLSearchParams({ model, port: String(port) });
          const data = await apiJson(`/api/services/vllm/launch-command?${qs}`);
          if (!stillOn("services", gen)) return;
          const cmdEl = $("vllm-cmd");
          const copyBtn = $("copy-cmd");
          if (cmdEl) cmdEl.textContent = data.command || "未能生成命令";
          if (copyBtn) {
            copyBtn.disabled = !data.command;
            copyBtn.dataset.cmd = data.command || "";
            copyBtn.textContent = "复制";
          }
          showFlash("vllm-msg", "ok", "启动命令已生成");
        },
        { gen, page: "services", flashId: "vllm-msg" },
      );
      if (stillOn("services", gen)) setBusy(submit, false);
    });
  }

  const copyBtn = $("copy-cmd");
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      const cmd = copyBtn.dataset.cmd || "";
      if (!cmd || !stillOn("services", gen)) return;
      const ok = await copyText(cmd);
      copyBtn.textContent = ok ? "已复制" : "复制失败";
      showFlash("vllm-msg", ok ? "ok" : "error", ok ? "启动命令已复制到剪贴板" : "复制失败，请手动选择命令框内容");
      setTimeout(() => {
        if (stillOn("services", gen)) copyBtn.textContent = "复制";
      }, 2000);
    });
  }

  hydrateLibraryPaths();
  const preset = sessionStorage.getItem("dlmodel_vllm_model");
  if (preset) {
    sessionStorage.removeItem("dlmodel_vllm_model");
    const modelEl = $("vllm-model");
    if (modelEl) {
      modelEl.value = preset;
      modelEl.focus();
    }
    showFlash("vllm-msg", "ok", "已填入模型库路径，可生成启动命令");
  }
  loadOllama();
  loadVllm();
}

function renderSettings(gen) {
  const root = appRoot();
  if (!root) return;
  settingsDirty = false;
  setHtml(
    root,
    `
    ${pageHead("设置", "镜像、鉴权、并发、推理地址与机器人推送。空 Token/Webhook 不覆盖；可显式清除。")}
    <div id="set-msg" aria-live="polite"></div>
    <form id="set-form" class="settings-sections">
      <p id="set-loading" class="panel muted">正在加载当前配置…</p>
      <fieldset id="set-fields" disabled class="settings-sections" style="border:0;padding:0;margin:0;">
      <section class="panel section-card">
        <h3>镜像与鉴权</h3>
        <p class="lead">国内默认走 hf-mirror；Token 仅在填写新值时更新。</p>
        <div class="form-grid">
          <label class="field full"><span>Hugging Face 镜像地址</span><input id="hf_endpoint" autocomplete="off" required></label>
          <label class="field">
            <span>HF Token（留空表示不修改）</span>
            <input id="hf_token" type="password" autocomplete="new-password" placeholder="未修改">
            <span id="hf_token_hint" class="hint"></span>
            <label class="checkbox-row"><input type="checkbox" id="clear_hf_token"> 清除已保存的 HF Token</label>
          </label>
          <label class="field">
            <span>ModelScope Token（留空表示不修改）</span>
            <input id="modelscope_api_token" type="password" autocomplete="new-password" placeholder="未修改">
            <span id="ms_token_hint" class="hint"></span>
            <label class="checkbox-row"><input type="checkbox" id="clear_modelscope_api_token"> 清除已保存的 ModelScope Token</label>
          </label>
        </div>
      </section>
      <section class="panel section-card">
        <h3>下载加速</h3>
        <p class="lead">控制任务并发与 aria2 单文件连接数。并发变更会立即调整工作线程。</p>
        <div class="form-grid">
          <label class="field"><span>下载并发任务数</span><input id="download_concurrency" type="number" min="1" max="32" step="1" required></label>
          <label class="field"><span>aria2 单文件连接数</span><input id="aria2_connections" type="number" min="1" max="64" step="1" required></label>
        </div>
      </section>
      <section class="panel section-card">
        <h3>推理连接</h3>
        <p class="lead">可指向本机 compose profile，或远程已有服务。</p>
        <div class="form-grid">
          <label class="field"><span>Ollama 服务地址</span><input id="ollama_base_url" autocomplete="off" required></label>
          <label class="field"><span>vLLM 服务地址</span><input id="vllm_base_url" autocomplete="off" required></label>
        </div>
      </section>
      <section class="panel section-card">
        <h3>消息推送</h3>
        <p class="lead">配置钉钉 / 飞书 / 企业微信机器人 Webhook。状态变更时推送摘要（进度、速率、预计剩余），不会按百分比刷屏；「开始下载」在测速后发送。</p>
        <div class="form-grid">
          <label class="field full">
            <span>钉钉机器人 Webhook（留空表示不修改）</span>
            <input id="notify_dingtalk_webhook" type="password" autocomplete="new-password" placeholder="https://oapi.dingtalk.com/robot/send?access_token=…">
            <span id="dingtalk_webhook_hint" class="hint"></span>
            <label class="checkbox-row"><input type="checkbox" id="clear_notify_dingtalk_webhook"> 清除钉钉 Webhook</label>
          </label>
          <label class="field full">
            <span>飞书机器人 Webhook（留空表示不修改）</span>
            <input id="notify_feishu_webhook" type="password" autocomplete="new-password" placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…">
            <span id="feishu_webhook_hint" class="hint"></span>
            <label class="checkbox-row"><input type="checkbox" id="clear_notify_feishu_webhook"> 清除飞书 Webhook</label>
          </label>
          <label class="field full">
            <span>企业微信机器人 Webhook（留空表示不修改）</span>
            <input id="notify_wecom_webhook" type="password" autocomplete="new-password" placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…">
            <span id="wecom_webhook_hint" class="hint"></span>
            <label class="checkbox-row"><input type="checkbox" id="clear_notify_wecom_webhook"> 清除企业微信 Webhook</label>
          </label>
        </div>
        <div class="notify-events">
          <span class="field-label">推送时机</span>
          <label class="checkbox-row"><input type="checkbox" id="notify_on_completed"> 下载完成</label>
          <label class="checkbox-row"><input type="checkbox" id="notify_on_failed"> 下载失败</label>
          <label class="checkbox-row"><input type="checkbox" id="notify_on_started"> 开始下载</label>
          <label class="checkbox-row"><input type="checkbox" id="notify_on_cancelled"> 任务取消</label>
        </div>
        <div class="actions notify-test-actions">
          <button type="button" id="notify-test-all" class="btn">发送测试消息</button>
        </div>
      </section>
      </fieldset>
      <div class="actions">
        <button class="primary" id="set-save" type="submit" disabled>保存设置</button>
        <button type="button" id="set-reload" hidden>重新加载</button>
      </div>
    </form>`,
  );

  const fields = [
    "hf_endpoint",
    "download_concurrency",
    "aria2_connections",
    "ollama_base_url",
    "vllm_base_url",
  ];
  const boolFields = [
    "notify_on_completed",
    "notify_on_failed",
    "notify_on_started",
    "notify_on_cancelled",
  ];
  const webhookHints = [
    ["dingtalk_webhook_hint", "notify_dingtalk_webhook_set", "钉钉"],
    ["feishu_webhook_hint", "notify_feishu_webhook_set", "飞书"],
    ["wecom_webhook_hint", "notify_wecom_webhook_set", "企业微信"],
  ];

  const applyWebhookHints = (data) => {
    webhookHints.forEach(([hintId, setKey, label]) => {
      const hint = $(hintId);
      if (!hint) return;
      hint.textContent = data[setKey]
        ? `当前已配置${label} Webhook（输入新值才会覆盖）`
        : `当前未配置${label} Webhook`;
    });
  };

  const markDirty = () => {
    settingsDirty = true;
  };
  const form = $("set-form");
  if (form) form.addEventListener("input", markDirty);
  if (form) form.addEventListener("change", markDirty);

  const hydrate = async () => {
    const reloadBtn = $("set-reload");
    if (reloadBtn) reloadBtn.hidden = true;
    await guarded(
      async () => {
        const data = await apiJson("/api/settings");
        if (!stillOn("settings", gen)) return;
        fields.forEach((key) => {
          const el = $(key);
          if (!el || data[key] == null) return;
          el.value = String(data[key]);
        });
        boolFields.forEach((key) => {
          const el = $(key);
          if (el) el.checked = Boolean(data[key]);
        });
        const hfHint = $("hf_token_hint");
        const msHint = $("ms_token_hint");
        if (hfHint) {
          hfHint.textContent = data.hf_token_set
            ? "当前已配置 HF Token（输入新值才会覆盖）"
            : "当前未配置 HF Token";
        }
        if (msHint) {
          msHint.textContent = data.modelscope_api_token_set
            ? "当前已配置 ModelScope Token（输入新值才会覆盖）"
            : "当前未配置 ModelScope Token";
        }
        applyWebhookHints(data);
        const loading = $("set-loading");
        if (loading) loading.remove();
        const fieldset = $("set-fields");
        if (fieldset) fieldset.disabled = false;
        const saveBtn = $("set-save");
        if (saveBtn) saveBtn.disabled = false;
        settingsDirty = false;
      },
      { gen, page: "settings", flashId: "set-msg" },
    );
    if (!stillOn("settings", gen)) return;
    if ($("set-loading")) {
      showFlash("set-msg", "error", "设置加载失败，请重试。");
      const reloadBtn2 = $("set-reload");
      if (reloadBtn2) {
        reloadBtn2.hidden = false;
        reloadBtn2.onclick = () => hydrate();
      }
    }
  };

  hydrate();

  const testBtn = $("notify-test-all");
  if (testBtn) {
    testBtn.addEventListener("click", async () => {
      if (!stillOn("settings", gen)) return;
      setBusy(testBtn, true, "发送中…");
      const payload = { channel: "all" };
      const ding = $("notify_dingtalk_webhook");
      const feishu = $("notify_feishu_webhook");
      const wecom = $("notify_wecom_webhook");
      if (ding && ding.value.trim()) payload.notify_dingtalk_webhook = ding.value.trim();
      if (feishu && feishu.value.trim()) payload.notify_feishu_webhook = feishu.value.trim();
      if (wecom && wecom.value.trim()) payload.notify_wecom_webhook = wecom.value.trim();
      await guarded(
        async () => {
          const result = await apiJson("/api/settings/notify-test", {
            method: "POST",
            body: JSON.stringify(payload),
          });
          if (!stillOn("settings", gen)) return;
          const parts = Object.entries(result.results || {}).map(
            ([ch, status]) => `${ch}: ${status}`,
          );
          showFlash(
            "set-msg",
            "ok",
            parts.length
              ? `测试已发送（${parts.join("；")}）。若使用了表单中的新 Webhook，请记得点「保存设置」。`
              : "测试已发送",
          );
        },
        { gen, page: "settings", flashId: "set-msg" },
      );
      if (stillOn("settings", gen)) setBusy(testBtn, false);
    });
  }

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
      notify_on_completed: Boolean($("notify_on_completed")?.checked),
      notify_on_failed: Boolean($("notify_on_failed")?.checked),
      notify_on_started: Boolean($("notify_on_started")?.checked),
      notify_on_cancelled: Boolean($("notify_on_cancelled")?.checked),
    };
    const hfTokenEl = $("hf_token");
    const msTokenEl = $("modelscope_api_token");
    const hfToken = hfTokenEl ? hfTokenEl.value.trim() : "";
    const msToken = msTokenEl ? msTokenEl.value.trim() : "";
    if (hfToken) body.hf_token = hfToken;
    if (msToken) body.modelscope_api_token = msToken;
    const clearHf = $("clear_hf_token");
    const clearMs = $("clear_modelscope_api_token");
    if (clearHf && clearHf.checked) body.clear_hf_token = true;
    if (clearMs && clearMs.checked) body.clear_modelscope_api_token = true;

    const webhookInputs = [
      ["notify_dingtalk_webhook", "clear_notify_dingtalk_webhook"],
      ["notify_feishu_webhook", "clear_notify_feishu_webhook"],
      ["notify_wecom_webhook", "clear_notify_wecom_webhook"],
    ];
    webhookInputs.forEach(([inputId, clearId]) => {
      const el = $(inputId);
      const clearEl = $(clearId);
      const value = el ? el.value.trim() : "";
      if (value) body[inputId] = value;
      if (clearEl && clearEl.checked) body[clearId] = true;
    });

    const saveBtn = $("set-save");
    setBusy(saveBtn, true, "保存中…");
    await guarded(
      async () => {
        const saved = await apiJson("/api/settings", {
          method: "PUT",
          body: JSON.stringify(body),
        });
        if (!stillOn("settings", gen)) return;
        if (hfTokenEl) hfTokenEl.value = "";
        if (msTokenEl) msTokenEl.value = "";
        if (clearHf) clearHf.checked = false;
        if (clearMs) clearMs.checked = false;
        webhookInputs.forEach(([inputId, clearId]) => {
          const el = $(inputId);
          const clearEl = $(clearId);
          if (el) el.value = "";
          if (clearEl) clearEl.checked = false;
        });
        const hfHint = $("hf_token_hint");
        const msHint = $("ms_token_hint");
        if (hfHint) {
          hfHint.textContent = saved.hf_token_set
            ? "当前已配置 HF Token（输入新值才会覆盖）"
            : "当前未配置 HF Token";
        }
        if (msHint) {
          msHint.textContent = saved.modelscope_api_token_set
            ? "当前已配置 ModelScope Token（输入新值才会覆盖）"
            : "当前未配置 ModelScope Token";
        }
        applyWebhookHints(saved);
        boolFields.forEach((key) => {
          const el = $(key);
          if (el && saved[key] != null) el.checked = Boolean(saved[key]);
        });
        settingsDirty = false;
        showFlash("set-msg", "ok", "设置已保存");
      },
      { gen, page: "settings", flashId: "set-msg" },
    );
    if (stillOn("settings", gen)) setBusy(saveBtn, false);
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
  if (settingsDirty && currentPage() !== "settings") {
    if (!confirm("设置尚未保存，确定离开？")) {
      ignoreHashChange = true;
      location.hash = "#/settings";
      ignoreHashChange = false;
      return;
    }
    settingsDirty = false;
  }
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
  const root = appRoot();
  if (root) root.focus({ preventScroll: true });
}

async function showApp() {
  setNavVisible(true);
  route();
}

async function boot() {
  const signout = $("signout");
  if (signout) {
    signout.addEventListener("click", () => {
      if (!confirm("确认退出登录？")) return;
      localStorage.removeItem(TOKEN_KEY);
      showLogin();
    });
  }
  const brand = $("brand-home");
  if (brand) {
    brand.addEventListener("click", (event) => {
      const nav = $("nav");
      if (nav && nav.hidden) {
        event.preventDefault();
      }
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
