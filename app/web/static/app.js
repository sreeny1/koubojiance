/* 口播违禁词检测 - 前端逻辑 */
"use strict";

/* ========================= 前端日志上报（汇入 logs/app.log） ========================= */
function clientLog(level, message, extra) {
  try {
    const payload = JSON.stringify({
      level: level || "info",
      message: String(message || "").slice(0, 2000),
      extra: String(extra || "").slice(0, 4000),
    });
    if (navigator.sendBeacon) {
      navigator.sendBeacon("/api/log", new Blob([payload], { type: "application/json" }));
    } else {
      fetch("/api/log", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: payload, keepalive: true,
      }).catch(() => {});
    }
  } catch (_) {}
}
// 全局未捕获错误 / Promise 拒绝：自动上报到服务端日志，便于定位前端问题
window.addEventListener("error", (e) => {
  clientLog("error",
    "[window.onerror] " + e.message + " @ " + (e.filename || "?") + ":" + (e.lineno || 0) + ":" + (e.colno || 0),
    (e.error && e.error.stack) || "");
});
window.addEventListener("unhandledrejection", (e) => {
  const r = e.reason;
  clientLog("error",
    "[unhandledrejection] " + ((r && (r.stack || r.message)) || String(r)),
    (r && r.stack) || "");
});

/* ========================= 工具函数 ========================= */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtTime(ms) {
  if (ms == null) return "—";
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = String(m).padStart(2, "0"), ss = String(sec).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}
function fmtTimeFine(ms) {
  return `${fmtTime(ms)}.${Math.floor((ms % 1000) / 100)}`;
}
function fmtDur(ms) {
  if (!ms) return "—";
  const s = Math.round(ms / 1000);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h > 0 ? `${h}小时${m}分` : m > 0 ? `${m}分${sec}秒` : `${sec}秒`;
}

function fmtBytes(n) {
  if (n == null || !isFinite(n)) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let v = n, i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (v >= 100 ? Math.round(v) : v >= 10 ? v.toFixed(1) : v.toFixed(2)) + " " + u[i];
}

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(url, opt);
  } catch (e) {
    clientLog("error", `api ${method} ${url} 网络异常: ${e.message}`, e.stack || "");
    throw e;
  }
  if (!r.ok) {
    let msg = `请求失败 (${r.status})`;
    try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
    clientLog("warn", `api ${method} ${url} → ${r.status}: ${msg}`);
    throw new Error(msg);
  }
  return r.json();
}

let toastTimer = null;
function toast(msg, isError) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("error", !!isError);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 5000 : 2600);
}

function busy(on, text) {
  $("#busy").hidden = !on;
  if (text) $("#busyText").textContent = text;
}

/* ========================= 悬浮提示（JS 全局 fixed，避免被裁剪） ========================= */
function initTooltip() {
  const gtip = $("#gtip");
  function showTip(el) {
    const text = el.getAttribute("data-tip");
    if (!text) { gtip.hidden = true; return; }
    gtip.textContent = text;
    gtip.hidden = false;
    const r = el.getBoundingClientRect();
    const tw = gtip.offsetWidth, th = gtip.offsetHeight;
    let x = Math.round(r.left + r.width / 2 - tw / 2);
    let y = Math.round(r.top - th - 8);
    if (y < 6) y = Math.round(r.bottom + 8);          // 上方放不下，放下方
    if (x < 6) x = 6;
    if (x + tw > window.innerWidth - 6) x = window.innerWidth - tw - 6;
    gtip.style.left = x + "px";
    gtip.style.top = y + "px";
  }
  document.addEventListener("mouseover", (e) => {
    const el = e.target.closest("[data-tip]");
    if (el) showTip(el);
    else if (!e.target.closest("#gtip")) gtip.hidden = true;
  });
  document.addEventListener("mouseout", (e) => {
    const el = e.target.closest("[data-tip]");
    if (el && !el.contains(e.relatedTarget)) gtip.hidden = true;
  });
}

/* ========================= 主题（浅色/深色/跟随系统） ========================= */
const THEME_KEY = "theme";
function resolveTheme(t) {
  if (t === "light" || t === "dark") return t;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function applyTheme(t) {
  const resolved = resolveTheme(t);
  document.documentElement.dataset.theme = resolved;
  try { localStorage.setItem(THEME_KEY, t); } catch (_) {}
  // 同步给原生拖放覆盖层（WinForms），让"松开鼠标开始检测"页面跟随主题
  try {
    if (window.chrome?.webview) window.chrome.webview.postMessage(resolved === "dark" ? "theme-dark" : "theme-light");
  } catch (_) {}
}
function initTheme() {
  let saved = "system";
  try { saved = localStorage.getItem(THEME_KEY) || "system"; } catch (_) {}
  applyTheme(saved);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    try { if ((localStorage.getItem(THEME_KEY) || "system") === "system") applyTheme("system"); } catch (_) {}
  });
}

/* 在句子里高亮命中的原文片段 */
function markSentence(sentence, matched) {
  const s = esc(sentence);
  if (!matched) return s;
  const i = (sentence ?? "").indexOf(matched);
  if (i < 0) return s;
  return esc(sentence.slice(0, i)) +
    `<mark>${esc(matched)}</mark>` + esc(sentence.slice(i + matched.length));
}

/* ========================= 全局状态 ========================= */
const state = {
  results: null,
  lastResultsJson: "",
  words: null,
  player: null,   // {videoId, videoName, hits, hitIdx, segments, segByMs}
  cutSelection: new Set(),  // 去词勾选的命中 id
  activeCut: null,          // 进行中的去词任务 {videoId, jobId}
  modelReady: null,         // 识别模型是否就绪（首启下载拦截依据）
  availableModels: [],      // 本地已下载的模型名（设置页标注"已下载"）
};

/* ========================= 页签切换 ========================= */
$("#tabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (!btn) return;
  $$(".tab").forEach((t) => t.classList.toggle("active", t === btn));
  $$(".tabpane").forEach((p) => p.classList.toggle("active", p.id === `tab-${btn.dataset.tab}`));
  if (btn.dataset.tab === "words") loadWords();
  if (btn.dataset.tab === "settings") { loadSettings(); refreshStatus(); }
});

/* ========================= 检测页：提交 ========================= */
const dz = $("#dropzone");

// WebView2 桌面模式：文件拖入窗口任意位置 → 通知原生层弹出全窗覆盖层接管拖放
// （真实路径直达 /api/scan，零复制）；平时无任何额外 UI。
const isDesktopApp = !!window.chrome?.webview;

if (isDesktopApp) {
  // 每次拖入都通知原生层弹覆盖层（C# 侧幂等，重复通知无副作用）。
  // 不用 enter/leave 计数：覆盖层出现后 Chromium 收不到配对的 dragleave，计数会失步。
  document.addEventListener("dragenter", () => {
    // 模型未就绪时不弹拖放覆盖层（由"首启下载界面 + 后端拦截"引导用户等待）
    if (state.modelReady === false) return;
    window.chrome.webview.postMessage("native-drag-enter");
  });
  document.addEventListener("dragover", (e) => e.preventDefault());
  // 兜底：防止竞态时 Chromium 直接打开 file://
  document.addEventListener("drop", (e) => e.preventDefault());
}

// 常规浏览器下的拖拽上传兜底（桌面模式拖放由原生覆盖层接管，不经这里）
dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("dragover"); });
dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
dz.addEventListener("drop", async (e) => {
  e.preventDefault();
  dz.classList.remove("dragover");
  if (isDesktopApp) return; // 桌面模式由原生层处理
  const files = Array.from(e.dataTransfer.files || []);
  if (!files.length) return;
  busy(true, `上传 ${files.length} 个文件…`);
  let ok = 0, fail = 0;
  for (const f of files) {
    try {
      const fd = new FormData();
      fd.append("file", f);
      const r = await fetch("/api/upload", { method: "POST", body: fd });
      if (r.ok) ok++; else fail++;
    } catch (_) { fail++; }
  }
  busy(false);
  toast(`已提交 ${ok} 个文件${fail ? `，${fail} 个失败` : ""}`, fail > 0);
  refreshAll(true);
});

// WebView2 原生层扫描完成通知（C# PostWebMessageAsJson({"type":"scan-submitted",...})）
window.chrome?.webview?.addEventListener("message", (ev) => {
  const msg = ev.data;
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "scan-submitted") {
    toast(`已提交 ${msg.count} 项（文件 / 文件夹）开始检测`);
    refreshAll(true);
  }
});

$("#btnPickFiles").addEventListener("click", () => pickAndScan("files"));
$("#btnPickFolder").addEventListener("click", () => pickAndScan("folder"));

async function pickAndScan(mode) {
  busy(true, "等待选择" + (mode === "files" ? "文件" : "文件夹") + "…");
  let paths;
  try {
    const r = await api("POST", "/api/pick", { mode });
    paths = r.paths;
  } catch (e) {
    busy(false);
    toast(e.message, true);
    return;
  }
  busy(false);
  if (!paths.length) return;
  scanPaths(paths);
}

$("#btnSubmitPaths").addEventListener("click", () => {
  const lines = $("#manualPaths").value.split("\n").map((s) => s.trim()).filter(Boolean);
  if (!lines.length) { toast("请先填写路径", true); return; }
  scanPaths(lines);
});

async function scanPaths(paths) {
  busy(true, "提交检测…");
  try {
    const r = await api("POST", "/api/scan", { paths });
    toast(`已提交 ${r.tasks} 个检测任务`);
    $("#manualPaths").value = "";
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy(false);
    refreshAll(true);
  }
}

/* ========================= 检测页：渲染 ========================= */
async function refreshResults(force) {
  const r = await api("GET", "/api/results");
  const j = JSON.stringify(r);
  if (!force && j === state.lastResultsJson) return;
  state.lastResultsJson = j;
  state.results = r;
  renderStats(r.stats);
  renderVideoList(r.videos);
  renderTaskStrip();
}

function renderStats(stats) {
  $("#statsText").innerHTML =
    `共 <b>${stats.videos}</b> 个媒体 · 已转写 <b>${stats.transcribed}</b> · ` +
    `命中违禁词 <span class="hitnum"><b>${stats.hits}</b></span> 处`;
}

function renderTaskStrip() {
  const strip = $("#taskStrip");
  const active = (state.results?.videos || []).filter(
    (v) => v.task_status === "queued" || v.task_status === "running"
  );
  if (!active.length) { strip.hidden = true; strip.innerHTML = ""; return; }
  strip.hidden = false;
  strip.innerHTML = active.map((v) => {
    const running = v.task_status === "running";
    return `<div class="trow">
      <span class="badge ${v.task_status}">${running ? "转写中" : "排队中"}</span>
      <span class="name" title="${esc(v.path)}">${esc(v.filename)}</span>
      <div class="progressbar"><div style="width:${Math.round((v.progress || 0) * 100)}%"></div></div>
    </div>`;
  }).join("");
}

function renderVideoList(videos) {
  const box = $("#videoList");
  if (!videos.length) {
    box.innerHTML = `<div class="panel empty-hint">还没有检测记录——把视频拖到上方虚线框开始</div>`;
    return;
  }
  box.innerHTML = videos.map((v) => {
    const catCount = {};
    v.hits.forEach((h) => { catCount[h.category] = catCount[h.category] || { n: 0, color: h.color }; catCount[h.category].n++; });
    const pills = Object.entries(catCount).map(([name, o]) =>
      `<span class="hitpill" style="background:${esc(o.color)}1a;color:${esc(o.color)}">
        <span class="dot" style="background:${esc(o.color)}"></span>${esc(name)} ${o.n}</span>`).join("");
    const status = v.task_status || "—";
    const statusText = {
      running: "转写中", queued: "排队中", done: v.seg_count ? "已完成" : "无字幕",
      error: "失败", canceled: "已取消",
    }[status] || status;

    let body = "";
    if (status === "running" || status === "queued") {
      body = `<div class="empty-hint muted">转写完成后自动显示违禁词检测结果…</div>`;
    } else if (status === "error") {
      body = `<div class="empty-hint err-text">转写失败，请点击右上角「重测」重试</div>`;
    } else if (!v.seg_count) {
      body = `<div class="empty-hint muted">未识别到语音内容（可能是纯音乐/无人声）</div>`;
    } else if (!v.hits.length) {
      // 无命中：展示完整字幕（弱化展示），便于人工二次复核
      body = `<div class="subs-nohit">
        <div class="subs-head">
          <span class="badge done">✓ 未发现违禁词</span>
          <span class="muted small">${v.seg_count} 句字幕 · 点击任意句在播放器中复核</span>
        </div>
        <div class="subs-slot" data-subs-for="${v.id}"><div class="subs-loading muted small">加载字幕…</div></div>
      </div>`;
    } else {
      body = `<table class="hit-table">
        <thead><tr><th style="width:36px"><input type="checkbox" class="hit-check-all" data-vid="${v.id}" title="全选/取消全选"></th><th style="width:110px">时间点</th><th style="width:130px">违禁词</th><th>口播内容</th></tr></thead>
        <tbody>${v.hits.map((h, i) => `
          <tr class="clickable" data-vid="${v.id}" data-hidx="${i}">
            <td><input type="checkbox" class="hit-check" data-vid="${v.id}" data-hid="${h.id}" ${state.cutSelection.has(h.id) ? "checked" : ""}></td>
            <td><span class="time-link">▶ ${fmtTimeFine(h.start_ms)}</span></td>
            <td><span class="word-tag" style="background:${esc(h.color)}">${esc(h.word_text)}</span>
                ${h.matched_text !== h.word_text ? `<br><span class="muted small">原文:${esc(h.matched_text)}</span>` : ""}</td>
            <td class="sentence-cell">${markSentence(h.sentence, h.matched_text)}</td>
          </tr>`).join("")}</tbody></table>`;
    }

    return `<div class="vcard" data-vid="${v.id}">
      <div class="vcard-head">
        <div class="vname">${esc(v.filename)}
          <span class="sub">${fmtDur(v.duration_ms)} · ${v.seg_count || 0} 句字幕${v.transcribed_at ? ` · 检测于 ${esc(v.transcribed_at)}` : ""}</span>
        </div>
        ${pills}
        <span class="badge ${status}">${statusText}</span>
        <button class="btn btn-xs" data-act="retest" data-tip="重新转写并检测该视频">重测</button>
        ${v.seg_count ? `<button class="btn btn-xs" data-act="srt" data-tip="下载该视频的 SRT 字幕文件">SRT</button>` : ""}
        ${v.hits.length && (v.path || "").toLowerCase().endsWith(".mp4") ? `<button class="btn btn-xs btn-primary" data-act="cut" data-tip="去除勾选的违禁词片段：原文件自动备份到同目录，成品文件名不变（仅 mp4）">去除所选</button>` : ""}
        <button class="btn btn-xs btn-danger" data-act="del" data-tip="删除该视频的检测记录（不删除磁盘上的视频文件）">删除</button>
        <button class="btn btn-xs btn-danger" data-act="delfile" data-tip="把该视频文件移入回收站（可恢复），并移除本记录">删除文件</button>
      </div>
      <div class="vcard-body">${body}</div>
    </div>`;
  }).join("");
  // 无命中卡片异步补齐字幕（已缓存则直接渲染，避免重复请求）
  loadSubsForNoHit(videos);
}

/* 为“无命中违禁词”的卡片加载完整字幕（供人工复核） */
const subsCache = {};  // { videoId: [segments] }
async function loadSubsForNoHit(videos) {
  const targets = videos.filter(
    (v) => v.task_status === "done" && v.seg_count > 0 && !v.hits.length
  );
  if (!targets.length) return;
  await Promise.allSettled(targets.map((v) => ensureSubs(v.id)));
}
async function ensureSubs(vid) {
  if (subsCache[vid]) {
    fillSubsSlot(vid, subsCache[vid]);
    return;
  }
  try {
    const data = await api("GET", `/api/videos/${vid}/subtitles`);
    subsCache[vid] = data.segments;
    fillSubsSlot(vid, data.segments);
  } catch (_) { /* 忽略：下次刷新重试 */ }
}
function fillSubsSlot(vid, segments) {
  const slot = document.querySelector(`.subs-slot[data-subs-for="${vid}"]`);
  if (!slot || !segments.length) return;
  slot.outerHTML = segments.map((s) => `
    <div class="subs-row" data-vid="${vid}" data-ms="${s.start_ms}">
      <span class="t">${fmtTime(s.start_ms)}</span>
      <span>${esc(s.text)}</span>
    </div>`).join("");
}

/* 卡片事件委托：播放定位 / 歌词行跳转 / 去词勾选 / 重测 / SRT / 删除 */
$("#videoList").addEventListener("click", async (e) => {
  const cb = e.target.closest(".hit-check, .hit-check-all");
  if (cb) { handleCutCheckbox(cb); return; }

  const sub = e.target.closest(".subs-row");
  if (sub) {
    openPlayerAt(Number(sub.dataset.vid), Number(sub.dataset.ms));
    return;
  }
  const row = e.target.closest("tr.clickable");
  if (row) {
    openPlayer(Number(row.dataset.vid), Number(row.dataset.hidx));
    return;
  }
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const card = btn.closest(".vcard");
  const vid = Number(card.dataset.vid);
  const v = state.results.videos.find((x) => x.id === vid);
  if (!v) return;

  if (btn.dataset.act === "retest") {
    try { await api("POST", "/api/scan", { paths: [v.path] }); toast("已重新加入队列"); }
    catch (err) { toast(err.message, true); }
    refreshAll(true);
  } else if (btn.dataset.act === "cut") {
    await cutSelected(vid);
  } else if (btn.dataset.act === "srt") {
    window.open(`/api/videos/${vid}/srt`, "_blank");
  } else if (btn.dataset.act === "del") {
    if (!confirm(`删除「${v.filename}」的检测记录？（不会删除磁盘上的视频文件）`)) return;
    try { await api("DELETE", `/api/videos/${vid}`); toast("已删除"); }
    catch (err) { toast(err.message, true); }
    refreshAll(true);
  } else if (btn.dataset.act === "delfile") {
    if (!confirm(
      `将「${v.filename}」的磁盘文件移入回收站（可恢复），并移除本检测记录。\n\n` +
      `文件路径：${v.path}\n\n` +
      `注意：此操作会删除本地原始文件（可在回收站找回），确认？`
    )) return;
    try {
      const r = await api("DELETE", `/api/videos/${vid}?remove_file=1`);
      toast(r.removed_file ? "已删除文件到回收站，记录已移除" : "记录已移除（文件已不在磁盘）");
    } catch (err) { toast(err.message, true); }
    refreshAll(true);
  }
});

/* ========================= 去词（自定义去除违禁词） ========================= */
function handleCutCheckbox(cb) {
  if (cb.classList.contains("hit-check-all")) {
    const vid = Number(cb.dataset.vid);
    const v = state.results.videos.find((x) => x.id === vid);
    if (!v) return;
    v.hits.forEach((h) => cb.checked ? state.cutSelection.add(h.id) : state.cutSelection.delete(h.id));
    document.querySelectorAll(`.hit-check[data-vid="${vid}"]`).forEach((c) => { c.checked = cb.checked; });
  } else {
    const hid = Number(cb.dataset.hid);
    const vid = Number(cb.dataset.vid);
    if (cb.checked) state.cutSelection.add(hid); else state.cutSelection.delete(hid);
    const v = state.results.videos.find((x) => x.id === vid);
    if (v) {
      const all = v.hits.every((h) => state.cutSelection.has(h.id));
      const ac = document.querySelector(`.hit-check-all[data-vid="${vid}"]`);
      if (ac) ac.checked = all;
    }
  }
}

async function cutSelected(vid) {
  const v = state.results.videos.find((x) => x.id === vid);
  if (!v) return;
  const hids = v.hits.map((h) => h.id).filter((id) => state.cutSelection.has(id));
  if (!hids.length) { toast("请先勾选要去除的违禁词", true); return; }
  const ok = confirm(
    `将去除 ${hids.length} 处命中对应的画面和声音片段（命中前后各 0.3 秒）。\n\n` +
    `· 原视频会先备份到视频所在文件夹\n` +
    `· 成品文件名保持不变\n` +
    `· 去词后会自动重新检测一次\n\n确认开始？`
  );
  if (!ok) return;
  try {
    const r = await api("POST", `/api/videos/${vid}/cut`, { hit_ids: hids, pad: 0.3 });
    state.activeCut = { videoId: vid, jobId: r.job_id };
    state.cutSelection.clear();
    updateCutBar({ status: "queued", progress: 0 });
    toast("去词任务已开始");
    scheduleCutPoll();
    refreshAll(true);
  } catch (err) { toast(err.message, true); }
}

function updateCutBar(job) {
  const pct = Math.max(0, Math.min(100, Math.round((job.progress || 0) * 100)));
  $("#cutBar").hidden = false;
  $("#cutBarText").textContent =
    (job.status === "queued" ? "去词排队中…" : "去词处理中…") + ` ${pct}%`;
  $("#cutBarFill").style.width = pct + "%";
}
function hideCutBar() { $("#cutBar").hidden = true; }

async function pollCutJob() {
  if (!state.activeCut) { hideCutBar(); return; }
  const { videoId } = state.activeCut;
  try {
    const r = await api("GET", `/api/videos/${videoId}/cut-jobs`);
    const job = r.jobs.find((j) => j.id === state.activeCut.jobId) || r.jobs[0];
    if (!job) return;
    if (job.status === "queued" || job.status === "running") {
      updateCutBar(job);
    } else {
      hideCutBar();
      state.activeCut = null;
      if (job.status === "done") {
        toast("去词完成，已自动重新检测" + (job.backup_path ? "（原文件已备份）" : ""));
      } else if (job.status === "error") {
        toast("去词失败：" + job.error, true);
      } else {
        toast("去词已取消");
      }
      refreshAll(true);
      return;
    }
  } catch (_) { /* 忽略：下次轮询重试 */ }
}
function scheduleCutPoll() {
  if (!state.activeCut) return;
  setTimeout(async () => { await pollCutJob(); scheduleCutPoll(); }, 1500);
}

/* ========================= 播放器弹窗 ========================= */
async function openPlayer(videoId, hitIdx = null) {
  let data;
  try {
    data = await api("GET", `/api/videos/${videoId}/subtitles`);
  } catch (e) { toast(e.message, true); return; }

  const hits = [];
  data.segments.forEach((s) => s.hits.forEach((h) => hits.push({ ...h, segment: s })));
  state.player = { videoId, videoName: data.video.filename, hits, hitIdx, segments: data.segments };

  $("#pmTitle").textContent = data.video.filename;
  $("#pmVideo").src = `/api/videos/${videoId}/stream`;
  $("#playerModal").hidden = false;
  document.body.style.overflow = "hidden";

  renderPlayerHits();
  renderPlayerSubs();
  if (hitIdx != null) seekToHit(hitIdx);
}

/* 打开播放器并跳转到指定毫秒（无命中字幕行复核用） */
function openPlayerAt(videoId, ms) {
  openPlayer(videoId).then?.(() => {
    const v = $("#pmVideo");
    const seek = () => { v.currentTime = ms / 1000; v.play().catch(() => {}); };
    if (v.readyState >= 1) seek(); else v.addEventListener("loadedmetadata", seek, { once: true });
  });
}

function renderPlayerHits() {
  const p = state.player;
  const box = $("#pmHits");
  if (!p.hits.length) {
    box.innerHTML = `<div class="empty-hint">无命中</div>`;
    return;
  }
  box.innerHTML = p.hits.map((h, i) => `
    <div class="pm-hit ${i === p.hitIdx ? "active" : ""}" data-hidx="${i}">
      <span class="time-link">▶ ${fmtTimeFine(h.start_ms)}</span>
      <span class="word-tag" style="background:${esc(h.color)}">${esc(h.word_text)}</span>
      <span class="grow">${markSentence(h.sentence, h.matched_text)}</span>
    </div>`).join("");
}

function renderPlayerSubs() {
  const p = state.player;
  $("#pmSubs").innerHTML = p.segments.map((s) => `
    <div class="pm-sub" data-seg="${s.idx}">
      <span class="t">${fmtTime(s.start_ms)}</span>${hitsMarkedSegment(s)}
    </div>`).join("");
}

function hitsMarkedSegment(seg) {
  let text = esc(seg.text);
  // 逐个命中替换（用占位符避免嵌套替换问题）
  seg.hits.forEach((h, i) => {
    const m = esc(h.matched_text);
    if (text.includes(m)) {
      text = text.replace(m, `\x00${i}\x01`);
    }
  });
  seg.hits.forEach((h, i) => {
    text = text.replaceAll(`\x00${i}\x01`,
      `<mark style="background:${esc(h.color)}26;border-bottom:2px solid ${esc(h.color)}">${esc(h.matched_text)}</mark>`);
  });
  return text;
}

function seekToHit(idx) {
  const p = state.player;
  if (!p || !p.hits.length) return;
  idx = Math.max(0, Math.min(p.hits.length - 1, idx));
  p.hitIdx = idx;
  const h = p.hits[idx];
  const v = $("#pmVideo");
  v.currentTime = Math.max(0, h.start_ms / 1000 - 0.4);
  v.play().catch(() => {});  // 浏览器可能限制自动播放，用户手动点播放即可
  renderPlayerHits();
  const el = $(`#pmHits .pm-hit[data-hidx="${idx}"]`);
  if (el) el.scrollIntoView({ block: "nearest" });
}

$("#pmHits").addEventListener("click", (e) => {
  const row = e.target.closest(".pm-hit");
  if (row) seekToHit(Number(row.dataset.hidx));
});
$("#pmSubs").addEventListener("click", (e) => {
  const row = e.target.closest(".pm-sub");
  if (!row) return;
  const seg = state.player.segments.find((s) => s.idx === Number(row.dataset.seg));
  if (seg) {
    $("#pmVideo").currentTime = seg.start_ms / 1000;
    $("#pmVideo").play().catch(() => {});
  }
});
$("#pmPrev").addEventListener("click", () => seekToHit((state.player?.hitIdx ?? 0) - 1));
$("#pmNext").addEventListener("click", () => seekToHit((state.player?.hitIdx ?? -1) + 1));

/* 播放时同步高亮当前字幕 */
$("#pmVideo").addEventListener("timeupdate", () => {
  const p = state.player;
  if (!p) return;
  const t = $("#pmVideo").currentTime * 1000;
  const cur = p.segments.find((s) => t >= s.start_ms && t < s.end_ms);
  $$("#pmSubs .pm-sub").forEach((el) => {
    const on = cur && Number(el.dataset.seg) === cur.idx;
    el.classList.toggle("active", on);
    if (on) el.scrollIntoView({ block: "nearest" });
  });
});
$("#pmVideo").addEventListener("error", () => {
  toast("浏览器无法直接播放该格式，可按时间点在本地播放器中定位", true);
});

function closePlayer() {
  const v = $("#pmVideo");
  v.pause();
  v.removeAttribute("src");
  v.load();
  $("#playerModal").hidden = true;
  document.body.style.overflow = "";
  state.player = null;
}
$("#pmClose").addEventListener("click", closePlayer);
$("#playerModal").addEventListener("click", (e) => { if (e.target === e.currentTarget) closePlayer(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#playerModal").hidden) closePlayer();
});

/* ========================= 顶部动作 ========================= */
$("#btnExport").addEventListener("click", async () => {
  busy(true, "生成报告…");
  try {
    const r = await fetch("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    if (!r.ok) throw new Error(`导出失败 (${r.status})`);
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `违禁词检测报告_${new Date().toISOString().slice(0, 10)}.xlsx`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast("报告已导出");
  } catch (e) {
    toast(e.message, true);
  } finally { busy(false); }
});

$("#btnRedetect").addEventListener("click", async () => {
  busy(true, "重新检测中…");
  try {
    const r = await api("POST", "/api/redetect");
    toast(`已重新检测 ${r.videos} 个视频，命中 ${r.hits} 处`);
  } catch (e) { toast(e.message, true); }
  finally { busy(false); refreshAll(true); }
});

$("#btnClearList").addEventListener("click", async () => {
  const r = await api("GET", "/api/results");
  const n = r.stats.videos;
  if (n === 0) { toast("列表已是空的"); return; }
  const ok = confirm(
    `将删除全部 ${n} 个视频的检测记录、字幕和媒体副本。\n\n` +
    `✓ 词库和设置不会丢失\n` +
    `✓ 磁盘上的原始视频文件不会被删除\n\n` +
    `此操作不可恢复，确认清除？`
  );
  if (!ok) return;
  busy(true, "正在清除…");
  try {
    const res = await api("DELETE", "/api/data");
    toast(
      `已清除 ${res.videos} 个视频 / ${res.tasks} 任务 / ` +
      `${res.hits} 处命中 / ${res.srt_files} 个字幕文件`
    );
    state.lastResultsJson = "";  // 强制下次刷新
  } catch (e) { toast(e.message, true); }
  finally { busy(false); refreshAll(true); }
});

/* ========================= 词库页 ========================= */
async function loadWords() {
  const r = await api("GET", "/api/words");
  state.words = r;
  renderWords();
}

function renderWords() {
  const { categories, words } = state.words;
  const catById = Object.fromEntries(categories.map((c) => [c.id, c]));

  // 分类标签
  $("#catChips").innerHTML = categories.map((c) => {
    const n = words.filter((w) => w.category_id === c.id).length;
    return `<span class="cat-chip"><span class="dot" style="background:${esc(c.color)}"></span>
      ${esc(c.name)} <span class="cnt">(${n})</span></span>`;
  }).join("");

  // 下拉框
  const opts = categories.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  ["#filterCat", "#wordCat", "#bulkCat"].forEach((sel) => {
    const keep = $(sel).value;
    $(sel).innerHTML = `<option value="">${sel === "#filterCat" ? "全部分类" : "选择分类"}</option>` + opts;
    if (keep) $(sel).value = keep;
  });

  // 词列表（过滤 + 搜索）
  const fc = $("#filterCat").value;
  const kw = $("#searchWord").value.trim();
  const shown = words.filter((w) =>
    (!fc || w.category_id === Number(fc)) && (!kw || w.word.includes(kw)));
  $("#wordCount").textContent = `共 ${words.length} 个词，显示 ${shown.length} 个`;
  $("#wordList").innerHTML = shown.length ? shown.map((w) => `
    <div class="word-row" data-wid="${w.id}">
      <span class="w">${esc(w.word)}</span>
      <span class="hitpill" style="background:${esc(w.color)}1a;color:${esc(w.color)}">
        <span class="dot" style="background:${esc(w.color)}"></span>${esc(w.category)}</span>
      <span class="note">${esc(w.note || "")}</span>
      <label class="switch" title="启用/停用">
        <input type="checkbox" ${w.enabled ? "checked" : ""} data-act="toggle">
        <span class="track"></span>
      </label>
      <button class="iconbtn" data-act="del" title="删除">🗑</button>
    </div>`).join("") : `<div class="empty-hint">没有匹配的词</div>`;
}

$("#filterCat").addEventListener("change", renderWords);
$("#searchWord").addEventListener("input", renderWords);

$("#btnAddCat").addEventListener("click", async () => {
  const name = $("#newCatName").value.trim();
  if (!name) { toast("请填写分类名称", true); return; }
  try {
    await api("POST", "/api/categories", { name, color: $("#newCatColor").value });
    $("#newCatName").value = "";
    toast("分类已添加");
    await loadWords();
  } catch (e) { toast(e.message, true); }
});

$("#btnAddWord").addEventListener("click", () => addWord());
$("#newWord").addEventListener("keydown", (e) => { if (e.key === "Enter") addWord(); });

async function addWord() {
  const word = $("#newWord").value.trim();
  const category_id = Number($("#wordCat").value);
  if (!word) { toast("请填写违禁词", true); return; }
  if (!category_id) { toast("请选择分类", true); return; }
  try {
    await api("POST", "/api/words", { word, category_id, note: $("#newNote").value.trim() });
    $("#newWord").value = ""; $("#newNote").value = "";
    toast("已添加");
    await afterWordChange();
  } catch (e) { toast(e.message, true); }
}

$("#btnBulkAdd").addEventListener("click", async () => {
  const text = $("#bulkText").value.trim();
  const category_id = Number($("#bulkCat").value);
  if (!text) { toast("请填写要导入的词", true); return; }
  if (!category_id) { toast("请选择分类", true); return; }
  try {
    const r = await api("POST", "/api/words/bulk", { text, category_id });
    $("#bulkText").value = "";
    toast(`导入 ${r.added} 个，跳过重复 ${r.skipped} 个`);
    await afterWordChange();
  } catch (e) { toast(e.message, true); }
});

$("#wordList").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act='del']");
  if (!btn) return;
  const wid = btn.closest(".word-row").dataset.wid;
  try { await api("DELETE", `/api/words/${wid}`); await afterWordChange(); }
  catch (err) { toast(err.message, true); }
});
$("#wordList").addEventListener("change", async (e) => {
  const cb = e.target.closest("input[data-act='toggle']");
  if (!cb) return;
  const wid = cb.closest(".word-row").dataset.wid;
  try { await api("PUT", `/api/words/${wid}`, { enabled: cb.checked ? 1 : 0 }); await afterWordChange(); }
  catch (err) { toast(err.message, true); }
});

/* 词库变更后：刷新词库视图，并对已转写视频自动重新检索（很快，无需重转写） */
async function afterWordChange() {
  await loadWords();
  const stats = state.results?.stats;
  if (stats && stats.transcribed > 0) {
    try { await api("POST", "/api/redetect"); toast("词库已更新，检测结果已刷新"); }
    catch (_) { /* 静默：用户可手动点「重新检测」 */ }
  }
  refreshAll(true);
}

/* 模型手动下载引导 */
function renderModelGuide(guide) {
  if (!guide || !guide.sources || !guide.sources.length) return "";
  const links = guide.sources.map((s) =>
    `<a href="${esc(s.base)}" target="_blank" rel="noopener">${esc(s.label)}</a>`).join(" · ");
  return `<div class="model-guide">
    <div class="mg-title">手动下载引导（自动下载失败时可用）</div>
    <div>1. 从任一源下载全部文件：${links}</div>
    <div>2. 放入目录：<code>${esc(guide.target_dir)}</code></div>
    <div>3. 重启软件，程序会自动识别本地模型</div>
  </div>`;
}

/* 首启模型下载界面：展示文件清单/大小/进度，模型未就绪时禁用拖入区 */
function renderModelDownload(s) {
  const ready = !!s.model_ready;
  state.modelReady = ready;
  const panel = $("#modelDl");
  const dz = $("#dropzone");
  if (ready) {
    panel.hidden = true;
    dz.classList.remove("disabled");
    return;
  }
  panel.hidden = false;
  dz.classList.add("disabled");

  const dl = s.model_download || {};
  const files = dl.files || (s.model_guide ? s.model_guide.files : []);
  const overall = dl.overall != null ? dl.overall : (dl.frac || 0);
  const active = !!dl.active;
  const modelName = dl.name || (s.model_guide && s.model_guide.name) || "";

  // 总大小估计
  let totalBytes = 0, hasSizes = false;
  (files || []).forEach((f) => { if (f.size) { totalBytes += f.size; hasSizes = true; } });
  const totalText = hasSizes ? `约 ${fmtBytes(totalBytes)}` : "约 3 GB";

  // 区分"首次使用（本地无任何模型）"与"切换模型（已有其它模型）"
  const isFirst = !s.has_any_model;
  $("#mdlTitle").textContent = isFirst
    ? "首次使用 · 正在准备语音识别模型"
    : `切换模型 · 正在下载识别模型「${esc(modelName || "…")}」`;
  $("#mdlSub").innerHTML = isFirst
    ? `程序需要下载识别模型（${totalText}）才能开始转写，仅需一次。` +
      `下载完成后即可拖入视频检测。请保持网络畅通，<b>完成前请先不要拖入视频</b>。`
    : `检测到识别模型已切换为「${esc(modelName || "…")}」，需要下载（${totalText}）才能开始转写。` +
      `下载完成后即可恢复拖入检测，<b>完成前请先不要拖入视频</b>。`;

  $("#mdlSrc").textContent = dl.source ? `下载源：${esc(dl.source)}` : (active ? "正在连接下载源…" : "准备中…");

  // 文件清单
  $("#mdlFiles").innerHTML = (files || []).map((f) => {
    const done = f.status === "done" || f.exists;
    const downloading = f.status === "downloading";
    const pct = downloading ? Math.round((dl.file_progress || 0) * 100) : (done ? 100 : 0);
    return `<div class="mdl-file ${done ? "done" : "downloading"}">
      <span class="st">${done ? "✓" : (downloading ? "…" : "·")}</span>
      <span class="nm" title="${esc(f.name)}">${esc(f.name)}</span>
      <span class="bar"><div style="width:${pct}%"></div></span>
      <span class="sz">${done ? "完成" : (f.size ? fmtBytes(f.size) : "")}</span>
    </div>`;
  }).join("");

  // 总体进度条
  $("#mdlFill").style.width = Math.round(overall * 100) + "%";
  $("#mdlPct").textContent = Math.round(overall * 100) + "%";

  // 错误/手动引导
  const errBox = $("#mdlError");
  const dlErr = dl.error;
  const guide = s.model_guide;
  if (dlErr) {
    errBox.hidden = false;
    errBox.innerHTML = `<span class="err-text">自动下载失败：${esc(dlErr)}</span>` +
      (guide ? renderModelGuide(guide) : "");
  } else if (!active && !ready) {
    errBox.hidden = false;
    errBox.innerHTML = guide ? renderModelGuide(guide) :
      `<span class="muted small">模型尚未就绪，正在准备…（若长时间无进度，请检查网络或在设置页更换下载源）</span>`;
  } else {
    errBox.hidden = true;
  }
}

/* ========================= 设置页 ========================= */
async function loadSettings() {
  const s = await api("GET", "/api/settings");
  $("#setModel").value = s.model;
  $("#setDevice").value = s.device;
  $("#setCompute").value = s.compute_type;
  $("#setLanguage").value = s.language;
  $("#setWorkers").value = s.max_workers;
  $("#setTheme").value = s.theme || "system";
  $("#setHfEndpoint").value = s.hf_endpoint || "";
  $("#setLogDebug").checked = s.log_level === "debug";
  $("#logDirPath").textContent = s.log_dir || "";
  if (s.theme) applyTheme(s.theme);  // 以服务端设置为准（首次打开无 localStorage 时也正确）

  // 标注本地已下载的模型，避免用户以为切换模型都要重新下载
  const avail = state.availableModels || [];
  Array.from($("#setModel").options).forEach((opt) => {
    const base = opt.textContent.replace(/\s*（已下载）.*/, "").trim();
    const label = avail.includes(opt.value) ? `${base}（已下载）` : base;
    if (opt.textContent !== label) opt.textContent = label;
  });
}

$("#setTheme").addEventListener("change", (e) => applyTheme(e.target.value));

$("#btnSaveSettings").addEventListener("click", async () => {
  const payload = {
    model: $("#setModel").value,
    device: $("#setDevice").value,
    compute_type: $("#setCompute").value,
    language: $("#setLanguage").value,
    max_workers: Number($("#setWorkers").value) || 1,
    theme: $("#setTheme").value,
    hf_endpoint: $("#setHfEndpoint").value.trim(),
    log_level: $("#setLogDebug").checked ? "debug" : "info",
  };
  try {
    await api("POST", "/api/settings", payload);
    toast("设置已保存（转写相关设置从下次任务生效）");
    refreshStatus();
  } catch (e) { toast(e.message, true); }
});

$("#btnViewLogs").addEventListener("click", async () => {
  const view = $("#logView");
  try {
    const d = await api("GET", "/api/logs/tail?lines=400");
    view.textContent = (d.note ? d.note + "\n\n" : "") + d.lines.join("\n");
    view.hidden = false;
    view.scrollTop = view.scrollHeight;
    if (!d.lines.length) toast("暂无日志内容");
  } catch (e) { toast(e.message, true); }
});
$("#btnOpenLogDir").addEventListener("click", async () => {
  try {
    const d = await api("POST", "/api/logs/open");
    toast("已打开日志目录：" + (d.path || ""));
  } catch (e) { toast(e.message, true); }
});

async function refreshStatus() {
  try {
    const s = await api("GET", "/api/status");
    state.availableModels = s.available_models || [];
    // 版本号标注到顶栏（服务端下发，保持唯一数据源）
    if (s.version) $("#appVer").textContent = " v" + s.version;
    // 首启模型下载界面（展示文件/大小/进度，并用"禁用拖入区"+后端拦截避免过早拖入）
    renderModelDownload(s);
    // 模型正在自动下载（首次启动）优先提示
    const dl = s.model_download || {};
    if (dl.active && dl.name) {
      const prog = dl.overall != null ? dl.overall : (dl.frac || 0);
      const pct = Math.min(100, Math.floor((prog || 0) * 100));
      const sizeText = dl.files?.some((f) => f.size)
        ? `（约 ${fmtBytes(dl.files.reduce((sum, f) => sum + (f.size || 0), 0))}，断点续传）`
        : "（断点续传）";
      $("#engineBadge").textContent = `引擎：正在下载模型 ${dl.name} ${pct}%`;
      $("#engineInfo").innerHTML =
        `正在从<b>${esc(dl.source || "国内源")}</b>自动下载识别模型 <b>${esc(dl.name)}</b>${sizeText}… <b>${pct}%</b><br>` +
        `下载完成后即可开始转写，无需任何手动操作。` +
        (dl.error ? `<br><span class="err-text">下载失败：${esc(dl.error)}，程序会在下次使用时自动续传重试</span>` +
          renderModelGuide(s.model_guide) : "");
      return;
    }
    const eff = s.effective;
    const gpu = (s.system && s.system.gpu) || {};
    const gpuNote = gpu.vendor === "amd"
      ? `检测到 <b>AMD 显卡</b>（${esc(gpu.name || "AMD")}）——本地引擎暂不支持 AMD 加速，将使用 <b>CPU 模式</b>。`
      : gpu.vendor === "nvidia"
        ? `检测到 <b>NVIDIA 显卡</b>，将使用 <b>GPU 加速</b>。`
        : "";
    if (eff && eff.model) {
      const dev = eff.device === "cuda" ? "GPU" : "CPU";
      $("#engineBadge").textContent = `引擎：${eff.model} · ${dev} · ${eff.compute_type}`;
      $("#engineInfo").innerHTML =
        `当前生效：<b>${esc(eff.model)}</b> · <b>${eff.device === "cuda" ? "GPU 加速" : "CPU"}</b> · ` +
        `精度 <b>${esc(eff.compute_type)}</b><br>` +
        (gpuNote ? gpuNote + "<br>" : "") +
        `排队 ${s.task_counts.queued || 0} · 进行中 ${s.task_counts.running || 0} · ` +
        `已完成 ${s.task_counts.done || 0} · 失败 ${s.task_counts.error || 0}`;
    } else {
      $("#engineBadge").textContent = gpu.vendor === "amd" ? "引擎：CPU 模式" : "引擎：未加载";
      $("#engineInfo").innerHTML =
        (gpuNote ? gpuNote + "<br>" : "") +
        `尚未加载模型（首次转写时自动加载并下载到 data\\models）。<br>` +
        (s.model_guide ? renderModelGuide(s.model_guide) : "") +
        `排队 ${s.task_counts.queued || 0} · 进行中 ${s.task_counts.running || 0}`;
    }
  } catch (_) { /* 状态刷新失败不影响使用 */ }
}

/* ========================= 轮询 ========================= */
async function refreshAll(force) {
  try { await refreshResults(force); } catch (_) {}
  refreshStatus();
}

let pollTimer = null;
function hasBusyWork() {
  return !state.modelReady || (state.results?.videos || []).some(
    (v) => v.task_status === "queued" || v.task_status === "running");
}
function schedulePoll() {
  const busy = hasBusyWork();
  pollTimer = setTimeout(async () => {
    if (!document.hidden) await refreshAll(false);
    schedulePoll();
  }, busy ? 2000 : 10000);
}

/* 任务进度轮询：空闲时大幅放慢（30s），仅在有任务或模型下载时保持高频，
   避免后台无工作时持续占用网络/CPU */
let taskPollTimer = null;
function scheduleTaskPoll() {
  const busy = hasBusyWork();
  clearTimeout(taskPollTimer);
  taskPollTimer = setTimeout(async () => {
    if (!document.hidden) await pollTaskProgress();
    scheduleTaskPoll();
  }, busy ? 1500 : 30000);
}

/* 进度数据：任务接口提供实时 progress，合并进 results 视图 */
async function pollTaskProgress() {
  try {
    const t = await api("GET", "/api/tasks?limit=200");
    const byVid = {};
    t.tasks.forEach((task) => { if (!(task.video_id in byVid)) byVid[task.video_id] = task; });
    (state.results?.videos || []).forEach((v) => {
      const task = byVid[v.id];
      if (task && (task.status === "running" || task.status === "queued")) {
        v.task_status = task.status;
        v.progress = task.progress;
      }
    });
    renderTaskStrip();
    const active = t.counts.queued || t.counts.running;
    if (active) refreshResults(false);  // 有任务时刷新结果（卡片状态切换）
  } catch (_) {}
}

/* ========================= 首次使用引导 ========================= */
function maybeShowWelcome() {
  let seen = false;
  try { seen = localStorage.getItem("welcome_seen") === "1"; } catch (_) {}
  if (seen) return;
  if (state.modelReady === false) return;  // 模型未就绪时优先展示"下载引导"，先不弹欢迎
  const videos = (state.results?.stats?.videos) || 0;
  if (videos > 0) return;  // 已有记录说明不是首次
  $("#welcomeModal").hidden = false;
}
$("#btnWelcomeClose").addEventListener("click", () => {
  $("#welcomeModal").hidden = true;
  try { localStorage.setItem("welcome_seen", "1"); } catch (_) {}
});
$("#welcomeModal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) $("#btnWelcomeClose").click();
});

/* ========================= 启动 ========================= */
(async function init() {
  clientLog("info", "前端页面初始化开始");
  initTheme();
  initTooltip();
  await refreshAll(true);
  schedulePoll();
  scheduleTaskPoll();
  maybeShowWelcome();
  clientLog("info", "前端页面初始化完成");
})();
