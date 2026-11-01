"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { view: "create", detailId: null, activeJob: null };
const progressMap = {};
const esMap = {};
const DRAFT_KEY = "av_create_draft";
let appDefaults = { mode: "auto", image_backend: "openai" };

const STATUS_LABEL = {
  running: "运行中",
  queued: "排队中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已停止",
  unknown: "未知",
};

function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  el.className = "toast" + (isErr ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 3200);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function badge(status) {
  return `<span class="badge ${status}">${STATUS_LABEL[status] || status}</span>`;
}

function loadDraft() {
  try {
    return JSON.parse(sessionStorage.getItem(DRAFT_KEY) || "{}");
  } catch (_) {
    return {};
  }
}

function saveDraft(partial) {
  const draft = { ...loadDraft(), ...partial };
  sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
  return draft;
}

function navigate(view, detailId) {
  let hash = "#/" + view;
  if (detailId) hash += "/" + detailId;
  if (location.hash === hash) {
    renderRoute();
  } else {
    location.hash = hash;
  }
}

function parseHash() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  return { view: parts[0] || "create", detailId: parts[1] || null };
}

function renderRoute() {
  const known = ["create", "tasks", "detail", "archive", "check", "settings"];
  const { view, detailId } = parseHash();
  const targetView = known.includes(view) ? view : "create";
  state.view = targetView;
  state.detailId = (targetView === "detail" ? detailId : null) || null;
  document.querySelectorAll(".nav-item").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === targetView);
  });
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $("#view-" + targetView).classList.add("active");
  render(targetView);
}

function render(view) {
  const map = {
    create: renderCreate,
    tasks: renderTasks,
    detail: renderDetail,
    archive: renderArchive,
    check: renderCheck,
    settings: renderSettings,
  };
  (map[view] || renderCreate)();
}

function renderCreate() {
  const draft = loadDraft();
  const mode = draft.mode || appDefaults.mode;
  const imageBackend = draft.image_backend || appDefaults.image_backend;
  const el = $("#view-create");
  const active = state.activeJob;
  const live = active && progressMap[active] &&
    (progressMap[active].status === "running" || progressMap[active].status === "queued");

  el.innerHTML = `
    <h1>新建任务</h1>
    <p class="sub">输入一个主题或一段文案，AI 会拆成 5 页讲解并生成配音视频。</p>
    <div class="card pad-lg">
      <div class="field">
        <label>主题 / 文案</label>
        <textarea id="source-text" placeholder="例如：Transformer 模型如何理解一句话">${esc(draft.source_text || "")}</textarea>
        <div class="hint">也可以上传 .txt / .md 文件，内容会读入输入框。</div>
      </div>
      <div class="row">
        <div class="field">
          <label>运行模式</label>
          <select id="mode">
            <option value="auto" ${mode === "auto" ? "selected" : ""}>auto</option>
            <option value="api" ${mode === "api" ? "selected" : ""}>api</option>
            <option value="demo" ${mode === "demo" ? "selected" : ""}>demo</option>
          </select>
        </div>
        <div class="field">
          <label>生图后端</label>
          <select id="image-backend">
            <option value="demo" ${imageBackend === "demo" ? "selected" : ""}>demo</option>
            <option value="openai" ${imageBackend === "openai" ? "selected" : ""}>openai（云端）</option>
            <option value="comfyui" ${imageBackend === "comfyui" ? "selected" : ""}>comfyui（本地）</option>
          </select>
        </div>
        <div class="field">
          <label>上传文本文件</label>
          <input type="file" id="file-input" accept=".txt,.md,.text" />
        </div>
      </div>
      <div class="actions">
        <button class="btn" id="run-btn">开始生成</button>
        <button class="btn ghost" id="cancel-btn" ${live ? "" : "disabled"}>停止</button>
      </div>
      <div id="progress-area" ${live ? "" : "hidden"}>
        <div class="hint">下面是最近一次任务的进度，不影响你继续新建。</div>
        <div class="progress-wrap">
          <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
          <div class="progress-label" id="progress-label">准备中…</div>
        </div>
        <div class="logbox" id="run-log"></div>
      </div>
    </div>`;

  $("#source-text").addEventListener("input", (e) => saveDraft({ source_text: e.target.value }));
  $("#mode").addEventListener("change", (e) => saveDraft({ mode: e.target.value }));
  $("#image-backend").addEventListener("change", (e) => saveDraft({ image_backend: e.target.value }));
  $("#file-input").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const text = await file.text();
    $("#source-text").value = text;
    saveDraft({ source_text: text });
    toast("已读取文件内容到输入框");
  });
  $("#run-btn").addEventListener("click", startRun);
  $("#cancel-btn").addEventListener("click", cancelRun);

  if (live) {
    $("#cancel-btn").disabled = false;
    $("#progress-area").hidden = false;
    paintProgress(active);
    ensureSubscribed(active);
  }
}

function paintProgress(taskId) {
  const entry = progressMap[taskId];
  if (!entry) return;
  const pct = entry.total ? Math.round((entry.step / entry.total) * 100) : 0;
  const fill = $("#progress-fill");
  const label = $("#progress-label");
  if (fill) fill.style.width = pct + "%";
  if (label) label.textContent = `[${entry.step}/${entry.total}] ${entry.message || ""}`;
}

async function startRun() {
  const sourceText = $("#source-text").value.trim();
  if (!sourceText) { toast("请先输入主题或文案", true); return; }
  const body = {
    source_text: sourceText,
    mode: $("#mode").value,
    image_backend: $("#image-backend").value,
  };
  saveDraft({ mode: body.mode, image_backend: body.image_backend });
  $("#cancel-btn").disabled = false;
  $("#progress-area").hidden = false;
  $("#progress-fill").style.width = "0%";
  $("#progress-label").textContent = "排队中…";
  $("#run-log").textContent = "";
  try {
    const data = await api("/api/tasks", { method: "POST", body: JSON.stringify(body) });
    saveDraft({ source_text: "" });
    $("#source-text").value = "";
    state.activeJob = data.task_id;
    progressMap[data.task_id] = progressMap[data.task_id] ||
      { status: "queued", step: 0, total: 10, message: "排队中" };
    subscribeEvents(data.task_id);
    refreshRunningSummary();
  } catch (err) {
    toast("创建失败：" + err.message, true);
    $("#progress-area").hidden = true;
    $("#cancel-btn").disabled = true;
  }
}

function ensureSubscribed(taskId) {
  if (!taskId || esMap[taskId]) return;
  const entry = progressMap[taskId];
  if (entry && (entry.status === "completed" || entry.status === "failed" || entry.status === "cancelled")) {
    return;
  }
  subscribeEvents(taskId);
}

function subscribeEvents(taskId) {
  if (esMap[taskId]) return;
  const entry = progressMap[taskId] ||
    { status: "queued", step: 0, total: 10, message: "排队中" };
  progressMap[taskId] = entry;
  const es = new EventSource(`/api/tasks/${taskId}/events`);
  esMap[taskId] = es;
  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.type === "progress" || msg.type === "status") {
      entry.status = msg.status || "running";
      entry.step = msg.step || 0;
      entry.total = msg.total || 10;
      entry.message = msg.message || "";
      onProgressUpdate(taskId);
    } else if (msg.type === "error") {
      entry.message = msg.message || entry.message;
      onProgressUpdate(taskId);
    } else if (msg.type === "done") {
      entry.status = msg.status;
      entry.message = STATUS_LABEL[msg.status] || msg.status;
      onProgressUpdate(taskId);
      es.close();
      delete esMap[taskId];
      if (taskId === state.activeJob) {
        finishRun(msg.status, taskId);
      } else if (state.view === "detail" && state.detailId === taskId) {
        renderDetail();
      } else if (state.view === "tasks") {
        renderTasks();
      } else if (state.view === "archive") {
        renderArchive();
      }
    }
  };
  es.onerror = () => {
    es.close();
    delete esMap[taskId];
  };
}

function onProgressUpdate(taskId) {
  const entry = progressMap[taskId];
  if (!entry) return;
  if (state.view === "create" && taskId === state.activeJob) {
    paintProgress(taskId);
  }
  if (state.view === "detail" && state.detailId === taskId) {
    paintDetailProgress(taskId);
  }
  updateTaskCardProgress(taskId);
  refreshRunningSummary();
}

function paintDetailProgress(taskId) {
  const entry = progressMap[taskId];
  const fill = $("#detail-progress-fill");
  const label = $("#detail-progress-label");
  const pct = entry && entry.total ? Math.round((entry.step / entry.total) * 100) : 0;
  if (fill) fill.style.width = pct + "%";
  if (label) label.textContent = entry ? `[${entry.step}/${entry.total}] ${entry.message || ""}` : "";
}

function updateTaskCardProgress(taskId) {
  const entry = progressMap[taskId];
  const fill = $("#prog-fill-" + taskId);
  if (!fill) return;
  const pct = entry && entry.total ? Math.round((entry.step / entry.total) * 100) : 0;
  fill.style.width = pct + "%";
  const label = $("#prog-label-" + taskId);
  if (label && entry) {
    label.textContent = `[${entry.step}/${entry.total}] ${entry.message || ""}`;
  }
}

function refreshRunningSummary() {
  const el = $("#running-summary");
  if (!el) return;
  const running = Object.entries(progressMap).filter(
    ([, e]) => e.status === "running" || e.status === "queued"
  );
  if (running.length === 0) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  const [, entry] = running[0];
  const pct = entry.total ? Math.round((entry.step / entry.total) * 100) : 0;
  el.innerHTML = `
    <div class="rs-label">运行中 ${running.length} 个</div>
    <div class="mini-bar"><div class="mini-fill" style="width:${pct}%"></div></div>
    <div class="rs-msg">${esc(entry.message || "")}</div>`;
}

function finishRun(status, taskId) {
  if (state.activeJob === taskId) state.activeJob = null;
  refreshRunningSummary();
  const runBtn = $("#run-btn");
  const cancelBtn = $("#cancel-btn");
  if (runBtn) runBtn.disabled = false;
  if (cancelBtn) cancelBtn.disabled = true;

  if (status === "completed") {
    toast("生成完成，可在任务列表查看");
    if (state.view === "create") {
      const label = $("#progress-label");
      if (label) label.textContent = "已完成";
    } else if (state.view === "detail" && state.detailId === taskId) renderDetail();
    else if (state.view === "tasks") renderTasks();
    else if (state.view === "archive") renderArchive();
  } else if (status === "failed") {
    toast("任务失败，可查看详情", true);
    if (state.view === "tasks") renderTasks();
    else if (state.view === "detail" && state.detailId === taskId) renderDetail();
  } else if (status === "cancelled") {
    toast("任务已停止，可稍后续跑");
    if (state.view === "tasks") renderTasks();
  }
}

async function cancelRun() {
  if (!state.activeJob) return;
  const taskId = state.activeJob;
  try {
    await api(`/api/tasks/${taskId}/cancel`, { method: "POST" });
    toast("已请求停止");
  } catch (err) {
    toast("停止失败：" + err.message, true);
  }
}

function taskCard(t) {
  const live = t.status === "running" || t.status === "queued";
  const isArchived = t.archived;
  const buttons = isArchived
    ? `<button class="btn danger sm" data-act="delete" data-id="${esc(t.task_id)}">删除</button>`
    : live
      ? `<button class="btn ghost sm" data-act="cancel" data-id="${esc(t.task_id)}">停止</button>`
      : `<button class="btn ghost sm" data-act="archive" data-id="${esc(t.task_id)}">归档</button>`;
  const progress = live
    ? `<div class="mini-progress">
        <div class="mini-bar"><div class="mini-fill" id="prog-fill-${esc(t.task_id)}"></div></div>
        <div class="mini-label" id="prog-label-${esc(t.task_id)}">${esc(STATUS_LABEL[t.status] || t.status)}</div>
      </div>`
    : "";
  return `
    <div class="task-card" data-id="${esc(t.task_id)}">
      <div class="top">
        <span class="topic">${esc(t.source_text || t.task_id)}</span>
        ${badge(t.status)}
        ${buttons}
      </div>
      ${progress}
      <div class="meta">
        ${esc(t.task_id)} · ${esc(fmtTime(t.created_at))}
        ${t.total_duration_seconds ? " · " + t.total_duration_seconds + "s" : ""}
        ${t.has_video ? " · 有视频" : ""}
      </div>
    </div>`;
}

async function renderTasks() {
  const el = $("#view-tasks");
  el.innerHTML = `<h1>任务列表</h1><p class="sub">点击任务卡片查看详情。</p><div id="tasks-body">加载中…</div>`;
  let data;
  try {
    data = await api("/api/tasks");
  } catch (err) {
    el.innerHTML = `<h1>任务列表</h1><p class="sub err">${esc(err.message)}</p>`;
    return;
  }
  const activeHtml = data.active.length
    ? data.active.map(taskCard).join("")
    : `<div class="empty">暂无任务</div>`;
  el.innerHTML = `
    <h1>任务列表</h1>
    <p class="sub">输出目录：<span class="mono">${esc(data.output_dir)}</span></p>
    <div class="section-title-row">
      <h2>活跃任务</h2>
      <button class="btn ghost sm" id="archive-all">全部归档</button>
    </div>
    <div class="list">${activeHtml}</div>`;

  $("#archive-all").addEventListener("click", async () => {
    if (!confirm("确定把所有活跃任务都移到归档区？")) return;
    try {
      const r = await api("/api/tasks/archive-all", { method: "POST" });
      toast(`已归档 ${r.archived_count} 个任务`);
      renderTasks();
    } catch (err) { toast(err.message, true); }
  });
  bindTaskCards(el);
  data.active.forEach((t) => {
    if (t.status === "running" || t.status === "queued") {
      ensureSubscribed(t.task_id);
      updateTaskCardProgress(t.task_id);
    }
  });
  refreshRunningSummary();
}

function bindTaskCards(el) {
  el.querySelectorAll(".task-card").forEach((card) => {
    card.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      const id = card.dataset.id;
      if (btn) {
        e.stopPropagation();
        const act = btn.dataset.act;
        if (act === "archive") archiveTask(id);
        if (act === "cancel") cancelTask(id);
        if (act === "delete") deleteArchived(id);
        return;
      }
      navigate("detail", id);
    });
  });
}

async function archiveTask(id) {
  try {
    await api(`/api/tasks/${id}/archive`, { method: "POST" });
    toast("已归档");
    renderTasks();
  } catch (err) { toast(err.message, true); }
}

async function cancelTask(id) {
  try {
    await api(`/api/tasks/${id}/cancel`, { method: "POST" });
    toast("已请求停止");
  } catch (err) { toast(err.message, true); }
}

async function deleteArchived(id) {
  if (!confirm("确定永久删除该归档任务？此操作不可恢复。")) return;
  try {
    await api(`/api/archive/${id}/delete`, { method: "POST" });
    toast("已删除");
    renderArchive();
  } catch (err) { toast(err.message, true); }
}

async function renderArchive() {
  const el = $("#view-archive");
  el.innerHTML = `<h1>归档区</h1><p class="sub">这里可以永久删除任务。</p><div id="archive-body">加载中…</div>`;
  let data;
  try {
    data = await api("/api/tasks");
  } catch (err) {
    el.innerHTML = `<h1>归档区</h1><p class="sub err">${esc(err.message)}</p>`;
    return;
  }
  const html = data.archived.length
    ? data.archived.map(taskCard).join("")
    : `<div class="empty">归档区为空</div>`;
  el.innerHTML = `
    <h1>归档区</h1>
    <p class="sub">共 ${data.archived.length} 个归档任务。</p>
    <div class="actions" style="margin-bottom:16px">
      <button class="btn danger" id="delete-all">删除全部</button>
    </div>
    <div class="list">${html}</div>`;
  $("#delete-all").addEventListener("click", async () => {
    if (!confirm("确定永久删除归档区里的全部任务？此操作不可恢复。")) return;
    try {
      const r = await api("/api/archive/delete-all", { method: "POST" });
      toast(`已删除 ${r.deleted_count} 个任务`);
      renderArchive();
    } catch (err) { toast(err.message, true); }
  });
  bindTaskCards(el);
}

async function renderDetail() {
  const el = $("#view-detail");
  const id = state.detailId;
  if (!id) { el.innerHTML = `<h1>任务详情</h1><p class="sub">请先选择任务。</p>`; return; }
  el.innerHTML = `<h1>任务详情</h1><p class="sub">加载中…</p>`;
  let data;
  try {
    data = await api(`/api/tasks/${id}`);
  } catch (err) {
    el.innerHTML = `<h1>任务详情</h1><p class="sub err">${esc(err.message)}</p>`;
    return;
  }
  const m = data.manifest || {};
  const sb = data.storyboard || {};
  const pages = sb.pages || [];
  const videoRel = (m.artifacts && m.artifacts.video) || "video.mp4";
  const videoSrc = `/api/tasks/${id}/files/${videoRel}`;
  const videoDownload = videoSrc + "?download=1";
  const live = m.status === "running" || m.status === "queued";
  const qcByPage = {};
  (data.quality_checks || []).forEach((item) => { qcByPage[item.page] = item; });
  const pagesHtml = pages.map((p) => {
    const qc = qcByPage[p.index];
    let qcHtml = "";
    if (qc) {
      if (qc.status === "qc_error") {
        qcHtml = `<span class="qc-badge muted">质检未完成</span><div class="qc-reason">${esc(qc.reason || "")}</div>`;
      } else if (typeof qc.score === "number") {
        const cls = qc.pass ? "pass" : "fail";
        qcHtml = `<span class="qc-badge ${cls}">质检 ${qc.score}/10</span>${qc.redraws ? ` <span class="qc-badge muted">重画 ${qc.redraws} 次</span>` : ""}<div class="qc-reason">${esc(qc.reason || "")}</div>`;
      }
    }
    return `
      <div class="card" data-page="${p.index}">
        ${p.image_file ? `<img src="/api/tasks/${id}/files/${esc(p.image_file)}" alt="" />` : ""}
        <div class="page-head">
          <span class="page-index">第 ${p.index} 页</span>
          ${qcHtml ? `<span>${qcHtml}</span>` : ""}
        </div>
        <input class="edit-input" data-field="title" value="${esc(p.title || "")}" placeholder="标题" />
        <textarea class="edit-textarea" data-field="narration" placeholder="解说词">${esc(p.narration || "")}</textarea>
        <input class="edit-input" data-field="key_points" value="${esc((p.key_points || []).join("；"))}" placeholder="要点（用；分隔）" />
        <details class="muted" style="margin-top:8px">
          <summary>提示词</summary>
          <input class="edit-input" data-field="visual_prompt" value="${esc(p.visual_prompt || "")}" placeholder="原始提示词" />
          <input class="edit-input" data-field="image_prompt" value="${esc(p.image_prompt || "")}" placeholder="扩充提示词" />
        </details>
        ${p.audio_file ? `<audio controls src="/api/tasks/${id}/files/${esc(p.audio_file)}"></audio>` : ""}
        <div class="page-actions">
          <button class="btn ghost sm" data-act="regenerate" data-page="${p.index}">重做此页</button>
        </div>
      </div>`;
  }).join("");

  const providers = Object.entries(m.providers || {}).map(([k, v]) => `<div class="muted">${esc(k)}: <span class="mono">${esc(v)}</span></div>`).join("");
  const cost = m.cost_estimate || {};
  const errHtml = m.error
    ? `<div class="card"><h2>错误信息</h2><p class="err">${esc(m.error.message || m.error)}</p></div>`
    : "";
  const liveHtml = live
    ? `<div class="card">
        <h2>实时进度</h2>
        <div class="progress-bar"><div class="progress-fill" id="detail-progress-fill"></div></div>
        <div class="progress-label" id="detail-progress-label">排队中…</div>
      </div>`
    : "";

  el.innerHTML = `
    <div class="section-title-row">
      <h1>${esc(m.source_text || id)}</h1>
      <div class="actions">
        <button class="btn" id="resume-btn">续跑</button>
        ${data.archived ? "" : `<button class="btn ghost" id="archive-btn">归档</button>`}
      </div>
    </div>
    <p class="sub">
      ${badge(m.status || "unknown")}
      <span class="mono">${esc(id)}</span>
      · ${esc(fmtTime(m.created_at))}
      ${m.elapsed_seconds ? " · 用时 " + m.elapsed_seconds + "s" : ""}
    </p>

    ${liveHtml}

    ${m.status === "completed" ? `
      <div class="card">
        <h2>最终视频</h2>
        <video controls src="${videoSrc}"></video>
        <div class="actions" style="margin-top:12px">
          <a class="btn ghost" href="${videoDownload}">下载视频</a>
          ${(m.artifacts && m.artifacts.subtitles) ? `<a class="btn ghost" href="/api/tasks/${id}/files/${esc(m.artifacts.subtitles)}?download=1">下载字幕</a>` : ""}
        </div>
      </div>` : ""}

    ${errHtml}

    <div class="card">
      <div class="section-title-row">
        <h2>5 页分镜</h2>
        <button class="btn sm" id="save-storyboard">保存修改</button>
      </div>
      <div class="grid">${pagesHtml || "<div class='empty'>暂无分镜</div>"}</div>
    </div>

    <div class="card">
      <h2>运行信息</h2>
      ${providers || "<div class='muted'>暂无</div>"}
      ${cost.total !== undefined ? `<div class="muted">预估费用：¥${esc(cost.total)}（${esc(cost.kind || "")}）</div>` : ""}
      ${cost.qc_calls !== undefined ? `<div class="muted">生图 ${esc(cost.images ?? 0)} 张 · 质检 ${esc(cost.qc_calls)} 次 · 重画 ${esc(cost.redraws ?? 0)} 次</div>` : ""}
      <div class="muted">时长：${m.total_duration_seconds ? m.total_duration_seconds + "s" : "—"}</div>
    </div>

    <div class="card">
      <details>
        <summary class="muted" style="cursor:pointer">运行日志（最近 80 行）</summary>
        <pre class="logbox" id="task-log" style="max-height:320px;overflow:auto;margin-top:12px">加载中…</pre>
      </details>
    </div>`;

  fetch(`/api/tasks/${id}/files/pipeline.log`)
    .then((r) => (r.ok ? r.text() : "无法读取日志"))
    .then((txt) => {
      const pre = $("#task-log");
      if (pre) pre.textContent = txt.split("\n").slice(-80).join("\n");
    })
    .catch(() => {
      const pre = $("#task-log");
      if (pre) pre.textContent = "无法读取日志";
    });

  if (live) {
    ensureSubscribed(id);
    paintDetailProgress(id);
  }
  $("#resume-btn").addEventListener("click", async () => {
    try {
      const r = await api(`/api/tasks/${id}/resume`, { method: "POST" });
      toast("已开始续跑");
      state.activeJob = r.task_id;
      progressMap[r.task_id] = progressMap[r.task_id] || { status: "queued", step: 0, total: 10, message: "排队中" };
      subscribeEvents(r.task_id);
      refreshRunningSummary();
      navigate("create");
    } catch (err) { toast(err.message, true); }
  });
  const archiveBtn = $("#archive-btn");
  if (archiveBtn) archiveBtn.addEventListener("click", async () => {
    await archiveTask(id);
    navigate("tasks");
  });

  const saveBtn = $("#save-storyboard");
  if (saveBtn) saveBtn.addEventListener("click", async () => {
    const pagesPayload = [];
    el.querySelectorAll(".card[data-page]").forEach((card) => {
      const index = parseInt(card.dataset.page, 10);
      const get = (field) => (card.querySelector(`[data-field="${field}"]`) || {}).value || "";
      const points = get("key_points").split(/[；;]/).map((s) => s.trim()).filter(Boolean);
      pagesPayload.push({
        index,
        title: get("title"),
        narration: get("narration"),
        key_points: points,
        visual_prompt: get("visual_prompt"),
        image_prompt: get("image_prompt"),
      });
    });
    try {
      await api(`/api/tasks/${id}/storyboard`, {
        method: "PUT",
        body: JSON.stringify({ pages: pagesPayload }),
      });
      toast("分镜已保存，可点“重做此页”让改动生效到画面/配音");
    } catch (err) { toast("保存失败：" + err.message, true); }
  });

  el.querySelectorAll('[data-act="regenerate"]').forEach((btn) => {
    btn.addEventListener("click", async () => {
      const page = btn.dataset.page;
      try {
        const r = await api(`/api/tasks/${id}/pages/${page}/regenerate`, {
          method: "POST",
          body: JSON.stringify({ redraw_image: true, redo_audio: true }),
        });
        progressMap[r.task_id] = {
          status: "queued",
          step: 0,
          total: 10,
          message: `排队中：重做第 ${page} 页`,
        };
        subscribeEvents(r.task_id);
        toast(`已提交重做第 ${page} 页`);
      } catch (err) { toast("重做失败：" + err.message, true); }
    });
  });
}

async function renderCheck() {
  const el = $("#view-check");
  el.innerHTML = `<h1>环境自检</h1><p class="sub">检查依赖与配置，不生成视频。</p><div id="check-body">加载中…</div>`;
  let data;
  try {
    data = await api("/api/check");
  } catch (err) {
    el.innerHTML = `<h1>环境自检</h1><p class="sub err">${esc(err.message)}</p>`;
    return;
  }
  const rows = data.checks.map((c) => `
    <tr>
      <td>${esc(c.name)}</td>
      <td class="${c.passed ? "pass" : "fail"}">${c.passed ? "通过" : "未通过"}</td>
      <td class="muted mono">${esc(c.detail)}</td>
    </tr>`).join("");
  el.innerHTML = `
    <h1>环境自检</h1>
    <p class="sub">${data.passed ? '<span class="pass">全部必需项通过</span>' : '<span class="fail">存在未通过项</span>'}</p>
    <div class="card">
      <table>
        <thead><tr><th>检查项</th><th>状态</th><th>详情</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

const SETTING_GROUPS = [
  {
    title: "API 与模型",
    fields: [
      { key: "OPENAI_API_KEY", label: "API Key", type: "secret", hint: "留空表示不修改；保存后对下次任务生效。" },
      { key: "OPENAI_BASE_URL", label: "API 地址", type: "text", hint: "硅基流动默认 https://api.siliconflow.cn/v1" },
      { key: "PLANNER_MODEL", label: "文案/文档模型", type: "text", hint: "负责把主题拆成 5 页讲解。" },
      { key: "PROMPT_ENHANCE_MODEL", label: "提示词扩充模型", type: "text", hint: "留空则跟随文案模型。" },
      { key: "IMAGE_MODEL", label: "生图模型", type: "text", hint: "云端生图用的模型 ID。" },
      { key: "IMAGE_SIZE", label: "图片尺寸", type: "text", hint: "宽x高，例如 1344x768。" },
    ],
  },
  {
    title: "生图与质检",
    fields: [
      { key: "PROMPT_ENHANCE", label: "生图前扩充提示词", type: "bool", hint: "用大模型把画面描述扩写得更具体。" },
      { key: "IMAGE_TEXT", label: "允许图片内出现文字/图表", type: "bool", hint: "本地 SDXL 中文易乱码时可关闭。" },
      { key: "QC_ENABLED", label: "生图后自动质检重画", type: "bool", hint: "openai 与 comfyui 后端生效，demo 占位图不质检。" },
      { key: "QC_MODEL", label: "质检视觉模型", type: "text", hint: "需支持图片输入，如 Qwen/Qwen3-VL-8B-Instruct。" },
      { key: "QC_MIN_SCORE", label: "质检通过分数（0-10）", type: "text", hint: "低于该分数会重画。" },
      { key: "QC_MAX_REDRAWS", label: "每页最多重画次数", type: "text", hint: "控制质检重画成本上限。" },
    ],
  },
  {
    title: "语音与字幕",
    fields: [
      { key: "EDGE_TTS_VOICE", label: "语音音色", type: "text", hint: "例如 zh-CN-XiaoxiaoNeural。" },
      { key: "EDGE_TTS_RATE", label: "语速", type: "text", hint: "如 +0%、-10%、+20%。" },
      { key: "BURN_SUBTITLES", label: "烧录字幕", type: "bool", hint: "把解说词烧进画面底部。" },
      { key: "SLIDE_TRANSITIONS", label: "幻灯片切换动画", type: "bool", hint: "页间淡入淡出。" },
      { key: "AI_DISCLOSURE", label: "右下角 AI 标识", type: "bool", hint: "合规展示可开启。" },
    ],
  },
  {
    title: "本地 ComfyUI（可选）",
    fields: [
      { key: "COMFYUI_BASE_URL", label: "ComfyUI 地址", type: "text", hint: "本地服务默认 http://127.0.0.1:8188" },
      { key: "COMFYUI_WORKFLOW_FILE", label: "ComfyUI 工作流文件", type: "text", hint: "API 格式 JSON 的相对或绝对路径。" },
    ],
  },
  {
    title: "视频与输出",
    fields: [
      { key: "VIDEO_WIDTH", label: "视频宽", type: "text" },
      { key: "VIDEO_HEIGHT", label: "视频高", type: "text" },
      { key: "FPS", label: "帧率", type: "text" },
      { key: "PAGE_AUDIO_TAIL_SECONDS", label: "每页结尾停顿秒数", type: "text", hint: "页间留白，避免语音被截断。" },
      { key: "BACKGROUND_MUSIC", label: "背景音乐路径", type: "text", hint: "留空则不混入。" },
      { key: "BACKGROUND_MUSIC_VOLUME", label: "背景音乐音量", type: "text", hint: "0 到 1，例如 0.10。" },
      { key: "OUTPUT_DIR", label: "输出目录", type: "text", hint: "留空回退到项目内 outputs。" },
    ],
  },
  {
    title: "费用估算（可选）",
    fields: [
      { key: "EST_PLANNER_CNY_PER_CALL", label: "文案模型每次调用（元）", type: "text", hint: "填 0 表示该项不计费。" },
      { key: "EST_IMAGE_CNY_PER_IMAGE", label: "生图每张（元）", type: "text" },
      { key: "EST_TTS_CNY_PER_1K_CHARS", label: "配音每千字（元）", type: "text", hint: "Edge TTS 免费，保持 0。" },
      { key: "EST_QC_CNY_PER_CALL", label: "质检每次调用（元）", type: "text" },
    ],
  },
];

async function renderSettings() {
  const el = $("#view-settings");
  el.innerHTML = `<h1>设置</h1><p class="sub">加载中…</p>`;
  let data;
  try {
    data = await api("/api/settings");
  } catch (err) {
    el.innerHTML = `<h1>设置</h1><p class="sub err">${esc(err.message)}</p>`;
    return;
  }
  const values = data.values || {};
  const fieldHtml = (f) => {
    let input;
    if (f.type === "bool") {
      input = `<select id="set-${f.key}">
        <option value="true" ${String(values[f.key] ?? "").toLowerCase() === "true" ? "selected" : ""}>开</option>
        <option value="false" ${String(values[f.key] ?? "").toLowerCase() !== "true" ? "selected" : ""}>关</option>
      </select>`;
    } else if (f.type === "secret") {
      input = `<input type="password" id="set-${f.key}" placeholder="${data.api_key_set ? "已设置：" + esc(data.api_key_masked) : "未设置"}" />`;
    } else {
      input = `<input type="text" id="set-${f.key}" value="${esc(values[f.key] ?? "")}" />`;
    }
    return `<div class="field">
      <label>${f.label} <span class="mono muted">${f.key}</span></label>
      ${input}
      ${f.hint ? `<div class="hint">${f.hint}</div>` : ""}
    </div>`;
  };
  const groupsHtml = SETTING_GROUPS.map((g) => `
    <div class="card">
      <h2>${esc(g.title)}</h2>
      <div class="row">${g.fields.map(fieldHtml).join("")}</div>
    </div>`).join("");

  el.innerHTML = `
    <h1>设置</h1>
    <p class="sub">改动会写入项目根目录的 .env 文件，多数项在下次生成任务时生效。</p>
    ${groupsHtml}
    <div class="actions" style="margin-top:4px">
      <button class="btn" id="save-settings">保存设置</button>
    </div>`;

  $("#save-settings").addEventListener("click", async () => {
    const payload = {};
    SETTING_GROUPS.forEach((g) => {
      g.fields.forEach((f) => {
        const input = $("#set-" + f.key);
        if (f.type === "secret") {
          if (input.value.trim()) payload[f.key] = input.value.trim();
          return;
        }
        payload[f.key] = input.value;
      });
    });
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify({ values: payload }) });
      toast("设置已保存");
      renderSettings();
    } catch (err) { toast("保存失败：" + err.message, true); }
  });
}

document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => navigate(btn.dataset.view));
});

document.querySelector("#nav-back").addEventListener("click", () => history.back());
document.querySelector("#nav-forward").addEventListener("click", () => history.forward());
window.addEventListener("hashchange", renderRoute);

async function checkHealth() {
  try {
    await api("/api/health");
    $("#health-dot").classList.add("ok");
    $("#health-text").textContent = "服务正常";
  } catch (_) {
    $("#health-dot").classList.remove("ok");
    $("#health-text").textContent = "连接失败";
  }
}

checkHealth();
async function init() {
  try {
    const s = await api("/api/settings");
    const v = s.values || {};
    if (["auto", "api", "demo"].includes(v.PIPELINE_MODE)) appDefaults.mode = v.PIPELINE_MODE;
    if (["demo", "openai", "comfyui"].includes(v.IMAGE_BACKEND)) appDefaults.image_backend = v.IMAGE_BACKEND;
  } catch (_) {}
  if (!location.hash) {
    location.hash = "#/create";
  } else {
    renderRoute();
  }
}
init();
