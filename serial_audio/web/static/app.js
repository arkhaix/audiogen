"use strict";

// Helpers

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const pad = (n) => String(n).padStart(2, "0");
const plural = (n, word, many) => `${n} ${n === 1 ? word : many || word + "s"}`;
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : "");
const baseName = (p) => String(p || "").split(/[\\/]/).pop();
const DL_ICON = `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M8 2v8m-3.5-3.5L8 10l3.5-3.5M3 13h10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const downloadUrl = (slug, n) => `/download/${encodeURIComponent(slug)}/${n}`;
const STATUSES = ["fetched", "characters", "scripted", "rendered"];
const FINISHED = new Set(["done", "failed", "cancelled", "stopped", "skipped", "lost"]);
const IMPORTANCE = ["major", "recurring", "minor", "one_off"];
const NEXT = {
  fetched: { label: "Characters", kind: "character-pass" },
  characters: { label: "Script", kind: "screenplay" },
  scripted: { label: "Render", kind: "render" },
};
const PLAY_SVG = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2.5v11l9.5-5.5z"/></svg>';
const PAUSE_SVG = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 2.5h3v11h-3zM9.5 2.5h3v11h-3z"/></svg>';

function clock(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

function span(sec) {
  if (sec == null || !isFinite(sec)) return "";
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec}s`;
  const m = Math.round(sec / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${pad(m % 60)}m`;
}

function hours(sec) {
  if (!sec) return "0 min";
  return sec >= 3600 ? `${(sec / 3600).toFixed(1)} h` : `${Math.round(sec / 60)} min`;
}

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  const now = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return time;
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return `yesterday ${time}`;
  const opts = { month: "short", day: "numeric" };
  if (d.getFullYear() !== now.getFullYear()) opts.year = "numeric";
  return `${d.toLocaleDateString([], opts)} ${time}`;
}

function elapsed(fromIso, toIso) {
  const a = Date.parse(fromIso), b = toIso ? Date.parse(toIso) : Date.now();
  return isNaN(a) || isNaN(b) ? null : (b - a) / 1000;
}

const gb = (n) => (n ? `${(n / 2 ** 30).toFixed(1)} GB` : "?");

function hue(id) {
  let h = 7;
  for (const c of String(id)) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return h % 360;
}

function chip(id, label) {
  if (id === "narrator") return `<span class="chip narrator">${esc(label || "Narrator")}</span>`;
  return `<span class="chip" style="--h:${hue(id)}" title="${esc(id)}">${esc(label || id)}</span>`;
}

function rangeText(nums) {
  const out = [];
  let start = null, prev = null;
  for (const n of nums) {
    if (start === null) start = prev = n;
    else if (n === prev + 1) prev = n;
    else { out.push(start === prev ? `${start}` : `${start}-${prev}`); start = prev = n; }
  }
  if (start !== null) out.push(start === prev ? `${start}` : `${start}-${prev}`);
  return out.join(", ");
}

function options(list, selected) {
  return list.map((o) => {
    const [value, label] = Array.isArray(o) ? o : [o, o];
    return `<option value="${esc(value)}"${String(value) === String(selected) ? " selected" : ""}>${esc(label)}</option>`;
  }).join("");
}

const store = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw == null ? fallback : JSON.parse(raw);
    } catch (e) {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (e) {
      return;
    }
  },
};

// Server

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let data = {};
  try {
    data = await res.json();
  } catch (e) {
    data = {};
  }
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

const get = (path) => api(path);
const post = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json", "X-Serial-Audio": "1" }, body: JSON.stringify(body || {}),
});

async function upload(slug, file) {
  const q = new URLSearchParams({ name: file.name });
  if (slug) q.set("slug", slug);
  const res = await fetch(`/api/upload?${q}`, { method: "POST", body: file, headers: { "X-Serial-Audio": "1" } });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `upload failed (${res.status})`);
  return data;
}

async function queue(jobs, stopOnFailure = true) {
  const res = await post("/api/jobs", { jobs, stop_on_failure: stopOnFailure });
  toast(res.jobs.length === 1 ? `Queued: ${res.jobs[0].title}` : `Queued ${res.jobs.length} jobs`);
  await poll();
  return res.jobs;
}

function toast(message, bad = false) {
  const el = document.createElement("div");
  el.className = "toast" + (bad ? " bad" : "");
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), bad ? 9000 : 3500);
}

// State and routing

const S = {
  route: { view: "library" },
  state: null,
  data: {},
  sel: {},
  usel: {},
  filter: {},
  imports: {},
  chTab: store.get("chTab", "script"),
  spk: null,
  bibleQ: "",
  bibleImp: "all",
  pronEdit: null,
  pronDraft: {},
  sig: "",
  allHistory: false,
};

const selFor = (slug) => (S.sel[slug] ||= new Set());
const uselFor = (slug) => (S.usel[slug] ||= new Set());

function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "jobs") return { view: "jobs" };
  if (parts[0] === "s" && parts[1]) {
    if (parts[2] === "c" && parts[3]) return { view: "chapter", slug: parts[1], n: Number(parts[3]) };
    return { view: "serial", slug: parts[1], tab: ["cast", "bible"].includes(parts[2]) ? parts[2] : "chapters" };
  }
  return { view: "library" };
}

function routeKey(r) {
  if (r.view === "chapter") return `ch:${r.slug}:${r.n}`;
  if (r.view === "serial") return `s:${r.slug}:${r.tab}`;
  return r.view;
}

async function fetchFor(r) {
  if (r.view === "library") return get("/api/serials");
  if (r.view === "jobs") return {};
  if (r.view === "chapter") return get(`/api/serials/${r.slug}/chapters/${r.n}`);
  const base = `/api/serials/${r.slug}`;
  const extra = r.tab === "cast" ? get(`${base}/voices`) : r.tab === "bible" ? get(`${base}/bible`) : null;
  const [serial, more] = await Promise.all([get(base), extra]);
  return { serial, extra: more };
}

let showToken = 0;

async function show() {
  const r = parseRoute();
  const key = routeKey(r);
  if (key !== routeKey(S.route)) S.spk = null;
  S.route = r;
  const token = ++showToken;
  $$("[data-nav]").forEach((a) => a.classList.toggle("on", a.dataset.nav === (r.view === "jobs" ? "jobs" : "library")));
  if (S.data[key]) paint();
  else $("#view").innerHTML = `<div class="empty">Loading…</div>`;
  try {
    const data = await fetchFor(r);
    if (token !== showToken) return;
    S.data[key] = data;
    paint();
  } catch (e) {
    if (token === showToken && !S.data[key]) $("#view").innerHTML = `<div class="notice bad">${esc(e.message)}</div>`;
  }
}

function paint() {
  const r = S.route;
  const data = S.data[routeKey(r)];
  if (!data) return;
  const view = $("#view");
  const opened = new Set($$("details[data-key]", view).filter((d) => d.open).map((d) => d.dataset.key));
  const closed = new Set($$("details[data-key]", view).filter((d) => !d.open).map((d) => d.dataset.key));
  const focus = document.activeElement && view.contains(document.activeElement) ? document.activeElement : null;
  const focusId = focus && focus.id;
  const caret = focus && "selectionStart" in focus ? focus.selectionStart : null;
  const typed = $$("input[id], select[id], textarea[id]", view).filter((el) => el.type !== "file")
    .map((el) => [el.id, el.type === "checkbox" ? el.checked : el.value]);
  view.innerHTML = VIEWS[r.view](data, r);
  for (const [id, value] of typed) {
    const el = document.getElementById(id);
    if (!el || !view.contains(el)) continue;
    if (el.type === "checkbox") el.checked = value;
    else el.value = value;
  }
  $$("details[data-key]", view).forEach((d) => {
    if (opened.has(d.dataset.key)) d.open = true;
    else if (closed.has(d.dataset.key)) d.open = false;
  });
  if (focusId) {
    const el = document.getElementById(focusId);
    if (el) {
      el.focus();
      if (caret != null) {
        try { el.setSelectionRange(caret, caret); } catch (e) { /* not a text field */ }
      }
    }
  }
  applyFilters();
  updateLive();
  markPlaying();
}

// Live state

async function poll() {
  try {
    S.state = await get(`/api/state${S.allHistory ? "?all=1" : ""}`);
    S.stateError = null;
  } catch (e) {
    S.stateError = e.message;
  }
  if (S.state) {
    const sig = S.state.jobs.filter((j) => j.status !== "queued").map((j) => j.id + j.status).join()
      + "|" + S.state.external.map((e) => `${e.pid}`).join();
    const first = !S.sig;
    const changed = S.sig && sig !== S.sig;
    S.sig = sig;
    if (first && S.route.view !== "jobs") paint();
    if (changed) {
      const current = routeKey(S.route);
      for (const key of Object.keys(S.data)) if (key !== current) delete S.data[key];
      if (S.route.view !== "jobs") show();
    }
  }
  if (S.route.view === "jobs") paint();
  else updateLive();
}

let pollTimer = null;
function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    await poll();
    schedulePoll();
  }, document.hidden ? 15000 : 3000);
}

function activeJobs(slug, n, kind) {
  if (!S.state) return [];
  return S.state.jobs.filter((j) => (j.status === "running" || j.status === "queued") && j.slug === slug
    && (n == null || (j.meta?.chapters || []).includes(n)) && (!kind || j.kind === kind));
}

function externalFor(slug, n) {
  return S.state ? S.state.external.find((e) => e.slug === slug && e.chapter === n) : null;
}

function chapterBusy(slug, n, kind) {
  const ext = externalFor(slug, n);
  return activeJobs(slug, n, kind).length > 0 || (!!ext && (!kind || ext.kind === kind));
}

function progressBits(p) {
  const pct = p && p.fraction != null ? Math.round(p.fraction * 100) : null;
  return { pct, eta: p && p.eta ? `${span(p.eta)} left` : "" };
}

function liveHTML(slug, n) {
  if (!S.state) return "";
  const ext = externalFor(slug, n);
  const run = activeJobs(slug, n).find((j) => j.status === "running");
  const queued = activeJobs(slug, n).filter((j) => j.status === "queued");
  let html = "";
  const active = run || ext;
  if (active) {
    const label = run ? run.title.split(" · ")[0] : `${cap(ext.kind)}, outside the app`;
    const { pct, eta } = progressBits(active.progress);
    html += `<small>${esc(label)}${pct != null ? ` ${pct}%` : ""}${eta ? ` · ${eta}` : ""}</small>`
      + `<div class="bar${pct == null ? " indet" : ""}"><i style="width:${pct ?? 0}%"></i></div>`;
  }
  if (queued.length) {
    html += `<small class="muted">queued: ${esc(queued.map((j) => j.title.split(" · ")[0].toLowerCase()).join(", "))}</small>`;
  }
  return html;
}

function updateLive() {
  $$("[data-live]").forEach((el) => {
    const [slug, n] = el.dataset.live.split(":");
    el.innerHTML = liveHTML(slug, Number(n));
  });
  renderHeader();
}

function renderHeader() {
  const a = $("#activity");
  const st = S.state;
  if (!st) {
    a.innerHTML = `<span class="dot down"></span><span class="txt">${S.stateError ? "web app not reachable" : "connecting…"}</span>`;
    return;
  }
  const run = st.jobs.filter((j) => j.status === "running");
  const ext = st.external.filter((e) => e.kind !== "script");
  const queued = st.jobs.filter((j) => j.status === "queued").length;
  const qc = $("#queue-count");
  qc.hidden = !queued;
  qc.textContent = queued;
  let text, p = null;
  if (run.length) {
    text = run[0].title;
    p = run[0].progress;
  } else if (ext.length) {
    text = `${cap(ext[0].kind)}${ext[0].chapter ? ` ch ${ext[0].chapter}` : ""}, outside the app`;
    p = ext[0].progress;
  } else if (st.external.length) {
    text = "A pipeline script is running";
  } else {
    text = queued ? `${queued} queued${st.paused ? ", on hold" : ""}` : "Idle";
  }
  const { pct, eta } = progressBits(p);
  if (pct != null) text += ` · ${pct}%`;
  if (eta) text += ` · ${eta}`;
  if (S.stateError) text = "web app not reachable";
  const c = st.comfy || {};
  const cluster = st.executor === "nomad";
  const ready = cluster ? (st.workers || []).filter((w) => w.ready) : [];
  const up = cluster ? Boolean(st.nomad?.up && ready.length) : c.up;
  const dot = S.stateError ? "down" : run.length || ext.length ? "busy" : up ? "up" : "down";
  a.title = cluster
    ? (st.nomad?.up ? `Workers ready: ${ready.map((w) => w.name).join(", ") || "none"}` : "Nomad is not reachable")
    : c.up ? `ComfyUI ${c.version || ""} is up. RAM free ${gb(c.ram_free)}.` : "ComfyUI is not reachable";
  a.innerHTML = `<span class="dot ${dot}"></span><span class="txt">${esc(text)}</span>`
    + (pct != null ? `<div class="bar hide-sm"><i style="width:${pct}%"></i></div>` : "");
}

// Player

const P = { audio: null, url: null, resume: false, lastSave: 0 };

function initPlayer() {
  const a = (P.audio = $("#pl-audio"));
  const rate = store.get("rate", 1);
  $("#pl-rate").value = String(rate);
  $("#pl-rate").addEventListener("change", (e) => {
    a.playbackRate = Number(e.target.value);
    store.set("rate", Number(e.target.value));
  });
  a.addEventListener("loadedmetadata", () => { a.playbackRate = Number($("#pl-rate").value); });
  a.addEventListener("timeupdate", () => savePos(false));
  a.addEventListener("pause", () => { savePos(true); markPlaying(); });
  a.addEventListener("play", markPlaying);
  a.addEventListener("ended", () => {
    if (P.resume) store.set(`pos:${P.url}`, 0);
    markPlaying();
  });
  if ("mediaSession" in navigator) {
    navigator.mediaSession.setActionHandler("seekbackward", () => { a.currentTime = Math.max(0, a.currentTime - 15); });
    navigator.mediaSession.setActionHandler("seekforward", () => { a.currentTime += 30; });
  }
}

function savePos(force) {
  if (!P.resume || !P.url) return;
  const now = Date.now();
  if (!force && now - P.lastSave < 4000) return;
  P.lastSave = now;
  store.set(`pos:${P.url}`, Math.floor(P.audio.currentTime));
}

function play(url, title, resume, artist) {
  const a = P.audio;
  if (P.url === url) {
    if (a.paused) a.play().catch((e) => toast(`Could not play: ${e.message}`, true));
    else a.pause();
    return;
  }
  savePos(true);
  P.url = url;
  P.resume = resume;
  $("#pl-title").textContent = title;
  $("#player").hidden = false;
  document.body.classList.add("playing");
  a.src = url;
  const pos = resume ? store.get(`pos:${url}`, 0) : 0;
  if (pos > 5) {
    a.addEventListener("loadedmetadata", () => {
      if (!a.duration || pos < a.duration - 10) a.currentTime = pos;
    }, { once: true });
  }
  a.play().catch((e) => toast(`Could not play: ${e.message}`, true));
  if ("mediaSession" in navigator && window.MediaMetadata) {
    navigator.mediaSession.metadata = new MediaMetadata({ title, artist: artist || "Serial Audio" });
  }
  markPlaying();
}

function markPlaying() {
  $$("[data-act=play]").forEach((b) => {
    const on = b.dataset.url === P.url && P.audio && !P.audio.paused;
    b.classList.toggle("on", on);
    if (b.classList.contains("play") || b.classList.contains("bigplay")) b.innerHTML = on ? PAUSE_SVG : PLAY_SVG;
  });
}

function playBtn(url, title, resume = false, big = false, artist = "") {
  return `<button type="button" class="${big ? "bigplay" : "play"}" data-act="play" data-url="${esc(url)}" data-title="${esc(title)}"`
    + ` data-artist="${esc(artist)}" data-resume="${resume ? 1 : 0}" aria-label="Play ${esc(title)}">${PLAY_SVG}</button>`;
}

// Dialogs

function openDialog({ title, body, submit, onSubmit, onInput, wide }) {
  const dlg = $("#dlg");
  dlg.classList.toggle("wide", !!wide);
  $("#dlg-title").textContent = title;
  $("#dlg-body").innerHTML = body;
  $("#dlg-foot").innerHTML = `<span class="grow" id="dlg-note"></span>`
    + `<button type="button" data-act="dlg-close">Cancel</button>`
    + `<button type="submit" class="primary" id="dlg-submit">${esc(submit)}</button>`;
  const form = $("#dlg-form");
  form.onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("#dlg-submit");
    btn.disabled = true;
    try {
      if ((await onSubmit(form)) !== false) dlg.close();
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  };
  form.oninput = onInput ? () => onInput(form) : null;
  dlg.showModal();
  if (onInput) onInput(form);
}

const field = (form, name) => form.elements[name];
const checked = (form, name) => !!(field(form, name) && field(form, name).checked);
const num = (form, name) => Number(field(form, name).value);

function llmFields() {
  const saved = store.get("llm", { backend: "auto", model: "opus", on_limit: "api", effort: "" });
  return `<details class="more"><summary>LLM settings</summary><div class="fields">
    <label class="field"><span>Backend</span><select name="backend">${options(["auto", "subscription", "api"], saved.backend)}</select></label>
    <label class="field"><span>Model</span><select name="model">${options(["opus", "sonnet", "haiku"], saved.model)}</select></label>
    <label class="field"><span>At the subscription limit</span><select name="on_limit">${options([["api", "fall back to the API"], ["wait", "wait for the reset"], ["fail", "fail"]], saved.on_limit)}</select></label>
    <label class="field"><span>Effort</span><select name="effort">${options([["", "default"], "low", "medium", "high", "xhigh", "max"], saved.effort)}</select></label>
  </div></details>`;
}

function llmParams(form, remember = false) {
  const p = {
    backend: field(form, "backend").value, model: field(form, "model").value,
    on_limit: field(form, "on_limit").value, effort: field(form, "effort").value || undefined,
  };
  if (remember) store.set("llm", { ...p, effort: p.effort || "" });
  return p;
}

function storedLLM() {
  const saved = store.get("llm", { backend: "auto", model: "opus", on_limit: "api", effort: "" });
  return { backend: saved.backend, model: saved.model, on_limit: saved.on_limit, effort: saved.effort || undefined };
}

// Log viewer

const L = { id: null, offset: 0, timer: null };

async function openLog(id, title) {
  clearTimeout(L.timer);
  L.id = id;
  L.offset = 0;
  $("#log-title").textContent = title;
  $("#log-text").textContent = "";
  $("#logdlg").showModal();
  await pullLog(true);
}

async function pullLog(first = false) {
  if (!L.id) return;
  const id = L.id;
  try {
    const r = await get(`/api/jobs/${id}/log?offset=${first ? -200000 : L.offset}`);
    if (id !== L.id) return;
    const pre = $("#log-text");
    pre.textContent += r.text;
    L.offset = r.offset;
    if ($("#log-follow").checked) pre.scrollTop = pre.scrollHeight;
    if (!r.finished || r.text) L.timer = setTimeout(() => pullLog(false), 2000);
  } catch (e) {
    toast(e.message, true);
  }
}

function closeLog() {
  L.id = null;
  clearTimeout(L.timer);
  $("#logdlg").close();
}

// Library

const VIEWS = {};

VIEWS.library = (d) => {
  document.title = "Library · Serial Audio";
  const cards = d.serials.map((s) => {
    const total = Math.max(1, s.on_disk || 0);
    const seg = STATUSES.map((st) => (s.status[st]
      ? `<i class="${st}" style="width:${(s.status[st] / total) * 100}%" title="${st}: ${s.status[st]}"></i>` : "")).join("");
    const legend = STATUSES.filter((st) => s.status[st]).map((st) => `<span><i class="${st}"></i>${s.status[st]} ${st}</span>`).join("");
    return `<a class="panel card" href="#/s/${esc(s.slug)}">
      <h2>${esc(s.title)}</h2>
      <div class="sub small">${esc([s.subtitle, s.author].filter(Boolean).join(" · "))}</div>
      <div class="segbar">${seg}</div>
      <div class="legend">${legend}</div>
      <div class="stats" style="margin-top:14px">
        <div class="stat"><b>${s.on_disk}</b><span>chapters on disk${s.known ? ` of ${s.known}` : ""}</span></div>
        <div class="stat"><b>${hours(s.audio_seconds)}</b><span>of audio</span></div>
        ${s.voices_pending ? `<div class="stat"><b>${s.voices_pending}</b><span>voices to review</span></div>` : ""}
      </div>
    </a>`;
  }).join("");
  return `<div class="page-head"><div><h1>Library</h1>
      <div class="sub">Serials, their chapters on the way to audio, and the voices that read them.</div></div></div>
    <div class="cards">${cards || `<div class="panel empty">No serials yet. Add one below.</div>`}</div>
    <div class="section-head"><h2>Add a serial</h2></div>
    <div class="panel pad stack">
      <div class="fields">
        <label class="field grow"><span>Royal Road fiction URL or id</span><input type="text" id="rr-url" placeholder="https://www.royalroad.com/fiction/12345/..."></label>
        <label class="field"><span>Slug (optional)</span><input type="text" id="rr-slug" placeholder="from the title"></label>
        <label class="field"><span>Fetch now</span><select id="rr-chapters">${options([["none", "the index only"], ["1", "chapter 1"], ["all", "every chapter"]], "1")}</select></label>
        <button class="primary" data-act="add-rr">Add</button>
      </div>
      <div class="fields">
        <label class="field grow"><span>EPUB file${S.epubFile ? `: ${esc(S.epubFile.name)}` : ""}</span><input type="file" id="epub-file" accept=".epub" data-change="epub-pick"></label>
        <label class="field"><span>Slug (optional)</span><input type="text" id="epub-slug" placeholder="from the title"></label>
        <button data-act="add-epub">Upload and add</button>
      </div>
    </div>`;
};

// Serial

VIEWS.serial = (d, r) => {
  const sd = d.serial;
  document.title = `${sd.serial.title} · Serial Audio`;
  let body;
  if (r.tab === "cast") body = castTab(sd, d.extra, r.slug);
  else if (r.tab === "bible") body = bibleTab(d.extra);
  else body = chaptersTab(sd, r.slug);
  return serialHead(sd, r.tab) + body;
};

function serialHead(sd, tab) {
  const s = sd.serial;
  const src = s.source || {};
  const rendered = sd.chapters.filter((c) => c.status === "rendered");
  const secs = rendered.reduce((t, c) => t + (c.audio?.seconds || 0), 0);
  const nar = s.narration?.point_of_view ? s.narration.point_of_view.replace("_", " ") : "";
  const sub = [s.subtitle, s.author, nar && `${nar} narration`].filter(Boolean).map(esc).join(" · ");
  const tabLink = (name, label, extra = "") => `<a href="#/s/${esc(s.slug)}${name === "chapters" ? "" : "/" + name}" class="${tab === name ? "on" : ""}">${label}${extra}</a>`;
  return `<div class="crumbs"><a href="#/">Library</a></div>
    <div class="page-head">
      <div><h1>${esc(s.title)}</h1><div class="sub">${sub}${src.url ? ` · <a href="${esc(src.url)}" target="_blank" rel="noopener">source</a>` : ""}</div></div>
      <div class="stats">
        <div class="stat"><b>${sd.chapters.length}</b><span>on disk${s.chapters ? ` of ${s.chapters}` : ""}</span></div>
        <div class="stat"><b>${rendered.length}</b><span>rendered</span></div>
        <div class="stat"><b>${hours(secs)}</b><span>of audio</span></div>
      </div>
    </div>
    <nav class="tabs">
      ${tabLink("chapters", "Chapters")}
      ${tabLink("cast", "Cast", sd.voices_pending ? ` <span class="count" title="voices to review">${sd.voices_pending}</span>` : "")}
      ${tabLink("bible", "Bible", ` <span class="muted small">${sd.characters}</span>`)}
    </nav>`;
}

function chaptersTab(sd, slug) {
  const sel = selFor(slug);
  const f = S.filter[slug] || "all";
  const counts = {};
  for (const c of sd.chapters) counts[c.status] = (counts[c.status] || 0) + 1;
  const rows = sd.chapters.filter((c) => f === "all" || c.status === f);
  let lastPart = null;
  let html = "";
  for (const c of rows) {
    if (c.part_title && c.part_title !== lastPart) {
      lastPart = c.part_title;
      html += `<tr class="part"><td colspan="7">${esc(c.part_title)}</td></tr>`;
    }
    html += chapterRow(c, slug, sel, sd.serial.title);
  }
  const allOn = rows.length > 0 && rows.every((c) => sel.has(c.number));
  const filters = ["all", ...STATUSES].map((st) => `<button class="${f === st ? "on" : ""}" data-act="filter" data-status="${st}">${st}${st === "all" ? "" : ` ${counts[st] || 0}`}</button>`).join("");
  return `<div class="panel">
      <div class="toolbar">
        <label class="inline"><input type="checkbox" data-change="sel-all" ${allOn ? "checked" : ""} aria-label="Select all shown"> <span class="small">${sel.size ? `${sel.size} selected` : "Select"}</span></label>
        <button class="sm ghost" data-act="sel-next" data-count="5">Next 5 to render</button>
        ${sel.size ? `<button class="sm ghost" data-act="sel-none">Clear</button>` : ""}
        <span class="grow"></span>
        <div class="filters">${filters}</div>
        <button class="primary" data-act="process" ${sel.size ? "" : "disabled"}>Process…</button>
      </div>
      <div class="table-wrap"><table class="list">
        <thead><tr><th></th><th class="hide-sm">#</th><th>Chapter</th><th class="hide-sm right">Words</th><th class="hide-sm">Status</th><th class="hide-sm right">Lines</th><th>Audio</th></tr></thead>
        <tbody>${html || `<tr><td colspan="7" class="empty">No chapters here.</td></tr>`}</tbody>
      </table></div>
    </div>
    ${addChapters(sd, slug)}`;
}

function chapterRow(c, slug, sel, serialTitle) {
  const hints = [];
  if (c.will_cast.length) hints.push(`<div class="hint warn">needs a voice: ${esc(c.will_cast.join(", "))}</div>`);
  if (c.unapproved.length) hints.push(`<div class="hint">not approved yet: ${esc(c.unapproved.join(", "))}</div>`);
  let audio = "";
  if (c.audio) {
    const pos = store.get(`pos:${c.audio.url}`, 0);
    const title = `${c.label}: ${c.title}`;
    audio = `<div class="audio-cell">${playBtn(c.audio.url, title, true, false, serialTitle)}`
      + `<span class="small nowrap">${clock(c.audio.seconds)}</span>`
      + (pos > 5 && pos < (c.audio.seconds || 0) - 10 ? `<span class="resume nowrap">at ${clock(pos)}</span>` : "")
      + `<a class="btn sm dl" href="${esc(downloadUrl(slug, c.number))}" download title="Download for offline listening" aria-label="Download ${esc(title)}">${DL_ICON}</a></div>`;
  } else {
    const next = chapterBusy(slug, c.number) ? null : NEXT[c.status];
    const est = c.estimate_minutes ? `<span class="muted small nowrap hide-sm">~${span(c.estimate_minutes * 60)}</span>` : "";
    audio = `<div class="audio-cell">${next ? `<button class="sm" data-act="next-step" data-n="${c.number}">${next.label}</button>` : ""}${est}</div>`;
  }
  return `<tr class="${sel.has(c.number) ? "sel" : ""}">
    <td><input type="checkbox" data-change="sel" data-n="${c.number}" ${sel.has(c.number) ? "checked" : ""} aria-label="Select chapter ${c.number}"></td>
    <td class="num hide-sm">${c.number}</td>
    <td class="ch-title"><a href="#/s/${esc(slug)}/c/${c.number}"><span class="show-sm muted">${c.number}. </span>${esc(c.title || c.label)}</a>${hints.join("")}
      <div class="show-sm"><span class="pill ${esc(c.status)}">${esc(c.status)}</span><span class="live" data-live="${esc(slug)}:${c.number}"></span></div></td>
    <td class="num right hide-sm">${(c.words || 0).toLocaleString()}</td>
    <td class="hide-sm"><span class="pill ${esc(c.status)}">${esc(c.status)}</span><span class="live" data-live="${esc(slug)}:${c.number}"></span></td>
    <td class="num right hide-sm">${c.lines ?? ""}</td>
    <td>${audio}</td>
  </tr>`;
}

function addChapters(sd, slug) {
  if ((sd.serial.source || {}).type !== "royalroad") return "";
  const imports = S.imports[slug] || [];
  const next = sd.remote.slice(0, 5);
  const rows = imports.map((it, i) => `<tr>
      <td class="small">${esc(it.file.name)}</td>
      <td><input type="number" min="1" value="${it.n ?? ""}" data-change="import-n" data-i="${i}" data-slug="${esc(slug)}" aria-label="Chapter number"></td>
      <td class="small muted">${sd.chapters.some((c) => c.number === it.n) ? "already on disk" : ""}</td>
    </tr>`).join("");
  return `<div class="section-head"><h2>Add chapters</h2>
      <span class="muted small">${plural(sd.remote.length, "more chapter")} in the saved index${sd.index_checked ? `, last checked ${when(sd.index_checked)}` : ""}</span></div>
    <div class="panel pad stack">
      <div>
        <h3>Import saved pages</h3>
        <p class="muted small">Open a chapter on Royal Road in your browser, save the page, then pick the saved files here. The chapter number comes from the file name.</p>
        <input type="file" multiple accept=".htm,.html" data-change="import-pick" data-slug="${esc(slug)}">
        ${imports.length ? `<div class="table-wrap" style="margin-top:10px"><table class="list"><thead><tr><th>File</th><th>Chapter</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
          <div class="row" style="margin-top:10px">
            <label class="inline small"><input type="checkbox" id="import-force"> replace chapter text already on disk</label>
            <span class="grow"></span>
            <button class="ghost" data-act="import-clear" data-slug="${esc(slug)}">Clear</button>
            <button class="primary" data-act="import-go" data-slug="${esc(slug)}">Import ${plural(imports.length, "page")}</button>
          </div>` : ""}
      </div>
      ${next.length ? `<div><h3>Next on the site</h3><ul class="small" style="margin:6px 0 0;padding-left:18px">${next.map((c) => `<li><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.title)}</a> <span class="muted">${esc(when(c.date))}</span></li>`).join("")}</ul></div>` : ""}
      <div>
        <h3>Fetch from the site</h3>
        <div class="row" style="margin-top:6px">
          <label class="inline small">the next <input type="number" id="fetch-limit" value="5" min="1" max="100"> new chapters</label>
          <button data-act="fetch-new" data-slug="${esc(slug)}">Fetch</button>
        </div>
        <p class="muted small">If the site refuses the request with HTTP 403, import saved pages instead.</p>
      </div>
    </div>`;
}

async function detectChapter(file) {
  const m = file.name.match(/chapter[\s_-]*(\d+)/i);
  if (m) return Number(m[1]);
  try {
    const head = await file.slice(0, 200000).text();
    const t = head.match(/<title>[^<]*?chapter\s*(\d+)/i);
    if (t) return Number(t[1]);
  } catch (e) {
    return null;
  }
  return null;
}

// Process dialog

async function voicesFor(slug) {
  const cached = S.data[`s:${slug}:cast`];
  if (cached && cached.extra) return cached.extra;
  return get(`/api/serials/${slug}/voices`);
}

async function openProcess(slug) {
  const sd = S.data[routeKey(S.route)].serial;
  const sel = [...selFor(slug)].sort((a, b) => a - b);
  const rows = sd.chapters.filter((c) => sel.includes(c.number));
  let nextSeed = 1000;
  try {
    nextSeed = (await voicesFor(slug)).next_seed;
  } catch (e) {
    nextSeed = 1000;
  }
  const needChars = rows.filter((c) => c.status === "fetched");
  const needScript = rows.filter((c) => c.status === "fetched" || c.status === "characters");
  const needRender = rows.filter((c) => c.status !== "rendered");
  const castLikely = needChars.length > 0 || rows.some((c) => c.will_cast.length);
  const stage = (name, label, on, why, opts = "") => `<div class="stage" data-stage="${name}">
      <label class="inline"><input type="checkbox" name="do_${name}" ${on ? "checked" : ""}> ${label}</label>
      <div class="why">${why}</div>${opts ? `<div class="opts">${opts}</div>` : ""}</div>`;
  const body = `
    <p class="small muted">Chapters ${esc(rangeText(sel))}. ${S.state?.executor === "nomad"
      ? "Each chapter's jobs run in this order. A job waits only for the jobs it needs, so a chapter renders once its screenplay is written. If a job fails, only the jobs that need it are skipped."
      : "The jobs run one at a time, in this order. If one fails, the rest of the batch is skipped."}</p>
    ${stage("characters", "Character pass", needChars.length, `${plural(needChars.length, "chapter")} still need it. It updates the bible with who appears.`,
      `<label class="inline"><input type="checkbox" name="redo_characters"> redo chapters that have one</label>`)}
    ${stage("cast", "Cast new voices", castLikely, "Designs a voice for each major or recurring character who has spoken and has none. Runs on a GPU node.",
      `<label class="inline">candidates <input type="number" name="candidates" value="3" min="1" max="12"></label>
       <label class="inline">first seed <input type="number" name="cast_seed" value="${nextSeed}" min="0"></label>`)}
    ${stage("script", "Screenplay", needScript.length, `${plural(needScript.length, "chapter")} still need one.`,
      `<label class="inline"><input type="checkbox" name="redo_script"> redo chapters that have one</label>`)}
    ${stage("render", "Render", needRender.length, `${plural(needRender.length, "chapter")} have no audio yet. Runs ${engineLabel(sd.render_engine, sd.render_runner)}.`,
      `<label class="inline"><input type="checkbox" name="rerender"> re-render chapters that have audio</label>
       <label class="inline">seed <input type="number" name="render_seed" value="1" min="0"></label>`)}
    <div id="proc-warn" class="stack"></div>
    ${llmFields()}`;
  openDialog({
    title: `Process ${plural(sel.length, "chapter")}`,
    body,
    submit: "Queue",
    onInput: (form) => {
      const jobs = buildProcess(form, rows, slug);
      for (const name of ["characters", "cast", "script", "render"]) {
        $(`[data-stage=${name}]`).classList.toggle("off", !checked(form, `do_${name}`));
      }
      const renders = jobs.filter((j) => j.kind === "render").map((j) => rows.find((c) => c.number === j.params.chapter));
      const mpl = sd.minutes_per_line || 0.5;
      const minutes = renders.reduce((t, c) => t + (c.lines || Math.round((c.words || 0) / 16)) * mpl, 0);
      $("#dlg-note").textContent = `${plural(jobs.length, "job")}${minutes ? `, rendering about ${span(minutes * 60)}` : ""}`;
      $("#dlg-submit").disabled = !jobs.length;
      const warn = [];
      const gaps = sd.chapters.filter((c) => c.status === "fetched" && c.number < sel[sel.length - 1] && !sel.includes(c.number)).map((c) => c.number);
      if (checked(form, "do_characters") && gaps.length) {
        warn.push(`Chapter ${esc(rangeText(gaps))} ${gaps.length === 1 ? "has" : "have"} no character pass and ${gaps.length === 1 ? "is" : "are"} not selected. The bible for later chapters will miss ${gaps.length === 1 ? "its" : "their"} characters.`);
      }
      if (checked(form, "do_characters") && checked(form, "redo_characters") && rows.some((c) => c.status !== "fetched")) {
        warn.push("Redoing the character pass sets those chapters back to characters and adds their facts to the bible a second time.");
      }
      if (checked(form, "do_script") && checked(form, "redo_script") && rows.some((c) => c.status === "rendered")) {
        warn.push("Redoing the screenplay of a rendered chapter marks it as scripted until it is rendered again.");
      }
      const unapproved = [...new Set(renders.flatMap((c) => c.unapproved))];
      if (unapproved.length) warn.push(`These voices are not approved yet: ${esc(unapproved.join(", "))}. Listen on the Cast tab first if you care.`);
      $("#proc-warn").innerHTML = warn.map((w) => `<div class="notice warn">${w}</div>`).join("");
    },
    onSubmit: async (form) => {
      llmParams(form, true);
      await queue(buildProcess(form, rows, slug));
      selFor(slug).clear();
      paint();
    },
  });
}

function buildProcess(form, rows, slug) {
  const llm = llmParams(form);
  const jobs = [];
  if (checked(form, "do_characters")) {
    for (const c of rows) {
      if (c.status === "fetched" || checked(form, "redo_characters")) {
        jobs.push({ kind: "character-pass", params: { slug, chapters: String(c.number), redo: c.status !== "fetched", ...llm } });
      }
    }
  }
  if (checked(form, "do_cast")) {
    jobs.push({ kind: "cast", params: { slug, candidates: num(form, "candidates"), seed: num(form, "cast_seed"), ...llm } });
  }
  if (checked(form, "do_script")) {
    for (const c of rows) {
      const done = c.status === "scripted" || c.status === "rendered";
      if (!done || checked(form, "redo_script")) {
        jobs.push({ kind: "screenplay", params: { slug, chapters: String(c.number), redo: done, ...llm } });
      }
    }
  }
  if (checked(form, "do_render")) {
    for (const c of rows) {
      if ((c.status !== "rendered" || checked(form, "rerender")) && !chapterBusy(slug, c.number, "render")) {
        jobs.push({ kind: "render", params: { slug, chapter: c.number, seed: num(form, "render_seed") } });
      }
    }
  }
  return jobs;
}

async function nextStep(slug, n) {
  const sd = S.data[routeKey(S.route)].serial;
  const c = sd.chapters.find((x) => x.number === n);
  const step = NEXT[c.status];
  if (!step) return;
  if (chapterBusy(slug, n)) {
    toast(`Chapter ${n} already has a job queued or running`);
    return;
  }
  if (step.kind === "render") {
    const notes = [];
    if (c.will_cast.length) notes.push(`${c.will_cast.join(", ")} ${c.will_cast.length === 1 ? "has" : "have"} no voice yet, so the narrator would read those lines.`);
    if (c.unapproved.length) notes.push(`Not approved yet: ${c.unapproved.join(", ")}.`);
    if (notes.length && !confirm(`${notes.join("\n")}\n\nRender chapter ${n} anyway?`)) return;
    await queue([{ kind: "render", params: { slug, chapter: n } }]);
  } else {
    await queue([{ kind: step.kind, params: { slug, chapters: String(n), ...storedLLM() } }]);
  }
}

// Chapter

VIEWS.chapter = (d, r) => {
  const m = d.meta;
  const slug = r.slug;
  document.title = `${m.label} · ${d.title} · Serial Audio`;
  const tabs = [
    ["script", "Script", d.script], ["text", "Text", true], ["cast", "Cast", d.script],
    ["notes", "Author notes", d.author_notes], ["details", "Details", true], ["log", "Render log", d.render_log],
  ].filter((t) => t[2]);
  const tab = tabs.find((t) => t[0] === S.chTab) ? S.chTab : tabs[0][0];
  const sc = m.script || {};
  const metaBits = [
    m.word_count && `${m.word_count.toLocaleString()} words`, sc.lines && `${sc.lines} lines`,
    sc.coverage && `coverage ${sc.coverage}`, m.published && `published ${when(m.published)}`,
  ].filter(Boolean).map(esc);
  if (m.source_url) metaBits.push(`<a href="${esc(m.source_url)}" target="_blank" rel="noopener">on the site</a>`);
  const nav = `<div class="row tight">
      ${d.prev ? `<a class="btn sm" href="#/s/${esc(slug)}/c/${d.prev}" title="Previous chapter">‹ ${d.prev}</a>` : ""}
      ${d.next ? `<a class="btn sm" href="#/s/${esc(slug)}/c/${d.next}" title="Next chapter">${d.next} ›</a>` : ""}
    </div>`;
  let body = "";
  if (tab === "script") body = scriptTab(d);
  else if (tab === "text") body = `<div class="panel pad prose">${prose(d.text)}</div>`;
  else if (tab === "cast") body = chapterCast(d, slug);
  else if (tab === "notes") body = `<div class="panel pad prose">${prose(d.author_notes)}</div><p class="muted small">Author notes are kept out of the narration.</p>`;
  else if (tab === "details") body = chapterDetails(d);
  else if (tab === "log") body = `<pre class="log">${esc(d.render_log)}</pre>`;
  return `<div class="crumbs"><a href="#/">Library</a> / <a href="#/s/${esc(slug)}">${esc(d.title)}</a></div>
    <div class="page-head">
      <div><h1>${esc(m.label)}: ${esc(m.title)}</h1>
        <div class="sub row tight"><span class="pill ${esc(m.status)}">${esc(m.status)}</span>${metaBits.join(" · ")}</div>
        <span class="live" data-live="${esc(slug)}:${m.number}"></span>
      </div>
      ${nav}
    </div>
    ${m.summary ? `<p class="summary">${esc(m.summary)}</p>` : ""}
    <div class="panel pad stack" style="margin-top:14px">
      ${chapterAudio(d, slug)}
      ${takeCheck(d)}
      <div class="row">${chapterActions(d, slug)}</div>
    </div>
    <nav class="tabs" style="margin-top:20px">${tabs.map((t) => `<button type="button" class="${t[0] === tab ? "on" : ""}" data-act="ch-tab" data-tab="${t[0]}">${t[1]}</button>`).join("")}</nav>
    ${body}`;
};

function engineLabel(engine, runner) {
  if (engine !== "breeze") return "Drama 3 in ComfyUI";
  return runner === "comfyui" ? "Breeze TTS 2 in ComfyUI" : "Breeze TTS 2 through breeze-server";
}

function chapterAudio(d, slug) {
  const m = d.meta;
  if (!d.audio) {
    const est = m.script?.lines ? ` Rendering would take about ${span(m.script.lines * (S.state?.minutes_per_line || 0.5) * 60)}.` : "";
    return `<div class="muted">No audio yet.${est}</div>`;
  }
  const r = m.render || {};
  const pos = store.get(`pos:${d.audio.url}`, 0);
  const title = `${m.label}: ${m.title}`;
  return `<div class="audio-card">
      ${playBtn(d.audio.url, title, true, true, d.title)}
      <div class="grow">
        <div><b>${clock(d.audio.seconds)}</b>${pos > 5 && pos < d.audio.seconds - 10 ? ` <span class="resume">resume at ${clock(pos)}</span>` : ""}</div>
        <div class="small muted">rendered ${esc(when(r.at))}${r.seed != null ? ` · seed ${r.seed}` : ""}${r.slots ? ` · ${plural(Object.keys(r.slots).length, "voice")}` : ""}${r.fx ? ` · effects on ${r.fx.treated_lines} lines` : ""}</div>
      </div>
      <a class="btn sm dl" href="${esc(downloadUrl(slug, m.number))}" download title="Download for offline listening">${DL_ICON}<span>Download</span></a>
    </div>`;
}

function takeCheck(d) {
  const r = d.meta.render;
  if (!r?.takes_file) return "";
  const c = r.check;
  const items = d.take_check || [];
  const listen = items.filter((t) => t.verdict === "flagged" || t.verdict === "out_of_range");
  const rest = items.filter((t) => t.verdict !== "flagged" && t.verdict !== "out_of_range");
  let head;
  if (!c) head = "The takes have not been checked against the script.";
  else if (c.skipped) head = `Take check skipped ${esc(when(c.at))}: ${esc(c.skipped)}`;
  else head = `<b>Take check.</b> ` + [`${c.checked} takes checked ${esc(when(c.at))}`, `${(c.flagged || []).length} flagged`,
    c.suspect?.length ? `${c.suspect.length} suspect` : "", c.unreadable?.length ? `${c.unreadable.length} unreadable` : ""].filter(Boolean).join(" · ");
  const why = (t) => t.verdict === "out_of_range" ? `${clock(t.seconds || 0)} for ${t.said.split(/\s+/).length} words, kept after retakes`
    : `${t.ins} inserted, ${t.del} dropped, ${t.sub} changed`;
  const row = (t) => `<div class="stack"><div class="row tight"><span class="pill ${listen.includes(t) ? "failed" : "auto"}">${esc(t.verdict.replace("_", " "))}</span><b>line ${t.line + 1}</b>${chip(t.speaker, t.name || t.speaker)}${t.direction ? `<span class="dir small">${esc(t.direction)}</span>` : ""}<span class="small muted">${why(t)}</span></div>
      <div class="small"><span class="muted">script:</span> ${esc(t.said)}</div>
      ${t.heard != null ? `<div class="small"><span class="muted">heard:</span> ${esc(t.heard || "(nothing)")}</div>` : ""}
      ${t.media ? `<audio controls preload="none" src="${esc(t.media.url)}"></audio>` : ""}</div>`;
  return `<div class="stack">
      <div class="small${listen.length ? "" : " muted"}">${head}${listen.length ? ` · ${plural(listen.length, "take")} to listen to` : ""}</div>
      ${listen.map(row).join("")}
      ${rest.length ? `<details><summary class="small muted">${rest.length} suspect or unreadable ${rest.length === 1 ? "take" : "takes"}, usually a name the recognizer spelled its own way</summary>${rest.map(row).join("")}</details>` : ""}
    </div>`;
}

function jobButton(label, kind, params, { confirmText = "", cls = "" } = {}) {
  return `<button class="sm ${cls}" data-act="job" data-kind="${kind}" data-params="${esc(JSON.stringify(params))}"`
    + (confirmText ? ` data-confirm="${esc(confirmText)}"` : "") + `>${label}</button>`;
}

function chapterActions(d, slug) {
  const m = d.meta;
  const n = m.number;
  const st = m.status;
  const out = [];
  const llm = storedLLM();
  if (st === "fetched") out.push(jobButton("Run character pass", "character-pass", { slug, chapters: String(n), ...llm }, { cls: "primary" }));
  else out.push(jobButton("Redo character pass", "character-pass", { slug, chapters: String(n), redo: true, ...llm },
    { confirmText: "Redo the character pass? The chapter goes back to characters and its facts are added to the bible a second time." }));
  if (st === "fetched" || st === "characters") out.push(jobButton("Write screenplay", "screenplay", { slug, chapters: String(n), ...llm }, { cls: st === "characters" ? "primary" : "" }));
  else out.push(jobButton("Redo screenplay", "screenplay", { slug, chapters: String(n), redo: true, ...llm },
    { confirmText: `Write a new screenplay for chapter ${n}? It replaces script.json${st === "rendered" ? ", and the chapter shows as scripted until it is rendered again" : ""}.` }));
  if (d.script) {
    if (st === "rendered") out.push(jobButton("Re-render", "render", { slug, chapter: n }, { confirmText: `Render chapter ${n} again? The new audio replaces the current file.` }));
    else out.push(jobButton("Render", "render", { slug, chapter: n }, { cls: st === "scripted" ? "primary" : "" }));
  }
  if (m.render?.codes_file) out.push(jobButton("Decode again", "render", { slug, chapter: n, decode_only: true }, { confirmText: "Decode the saved codes again? This replaces the audio file and takes a couple of minutes." }));
  if (m.render?.takes_file) out.push(jobButton("Join takes again", "render", { slug, chapter: n, decode_only: true }, { confirmText: "Join the saved Breeze takes again? This replaces the audio file and takes about a minute." }));
  if (m.render?.takes_file) {
    const checked = !!(m.render.check && !m.render.check.skipped);
    out.push(jobButton(checked ? "Check takes again" : "Check takes", "check-takes", { slug, chapter: n, redo: checked }));
  }
  return out.join("");
}

function scriptTab(d) {
  const names = Object.fromEntries(d.cast.map((c) => [c.id, c.name]));
  const chips = d.cast.map((c) => `<button type="button" class="chip${c.id === "narrator" ? " narrator" : ""}" style="--h:${hue(c.id)}" data-act="spk" data-id="${esc(c.id)}">${esc(c.name)} <span class="small">${c.lines}</span></button>`).join("");
  const lines = d.script.lines.map((l, i) => `<div class="line${l.speaker === "narrator" ? " narr" : ""}" data-speaker="${esc(l.speaker)}">
      <span class="idx">${i + 1}</span><span class="who">${chip(l.speaker, names[l.speaker] || l.speaker)}</span>
      <span class="txt">${l.direction ? `<span class="dir">${esc(l.direction)}</span>` : ""}${esc(l.text)}</span></div>`).join("");
  return `<div class="row tight" style="margin-bottom:12px">${chips}<button type="button" class="sm ghost" data-act="spk" data-id="">show everyone</button></div>
    <div class="panel pad script">${lines}</div>
    ${d.script.notes ? `<p class="muted small">Notes from the screenplay pass: ${esc(d.script.notes)}</p>` : ""}`;
}

function chapterCast(d, slug) {
  const rows = d.cast.map((c) => {
    let voice;
    if (c.voice) voice = `<span class="mono small">${esc(c.voice)}</span> <span class="pill ${esc(c.voice_status || "")}">${esc(c.voice_status || "?")}</span>`;
    else voice = `<span class="muted small">narrator reads</span>`;
    return `<tr><td>${chip(c.id, c.name)}</td><td class="hide-sm small muted">${esc(c.importance || "")}</td><td class="num right">${c.lines}</td><td class="num right">${c.slot ?? ""}</td><td>${voice}</td></tr>`;
  }).join("");
  return `<div class="panel table-wrap"><table class="list">
      <thead><tr><th>Speaker</th><th class="hide-sm">Importance</th><th class="right">Lines</th><th class="right">Slot</th><th>Voice</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    ${d.plan_notes.length ? `<div class="notice" style="margin-top:12px"><ul>${d.plan_notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>` : ""}
    <p class="muted small">Slots are how the render maps speakers to voices. Slot 0 is the narrator. <a href="#/s/${esc(slug)}/cast">Manage voices</a></p>`;
}

function chapterDetails(d) {
  const m = d.meta;
  const passes = Object.entries(m.passes || {}).map(([name, p]) => `<dt>${esc(name)} pass</dt><dd>${esc(p.backend)} / ${esc(p.model)}, ${esc(when(p.at))}${p.cost_estimate_usd != null ? `, about $${Number(p.cost_estimate_usd).toFixed(2)}` : ""}</dd>`).join("");
  const r = m.render;
  const made = r?.engine === "breeze" ? `Breeze TTS 2, ${esc(r.model)}, takes <span class="mono">${esc(r.takes_file)}</span>`
    : `Drama 3, codes <span class="mono">${esc(r?.codes_file)}</span>`;
  const render = r ? `<dt>Render</dt><dd>${esc(when(r.at))}, seed ${esc(r.seed)}, ${made}</dd>
      <dt>Voice slots</dt><dd>${Object.entries(r.slots || {}).map(([s, v]) => `${esc(s)}: ${esc(v)}`).join(", ")}</dd>
      ${r.notes?.length ? `<dt>Render notes</dt><dd>${r.notes.map(esc).join("<br>")}</dd>` : ""}
      ${r.effects ? `<dt>Effects</dt><dd>${Object.entries(r.effects).map(([v, e]) => `${esc(v)}: ${esc(e.effect)}`).join(", ")}${r.fx ? `, ${r.fx.raw_seconds} s raw to ${r.fx.seconds} s` : ""}</dd>` : ""}` : "";
  const present = (m.characters_present || []).map((id) => chip(id)).join(" ");
  const fresh = d.character_pass?.new?.length ? d.character_pass.new.map((c) => `${chip(c.id, c.name)} <span class="small muted">${esc(c.importance)}: ${esc(c.role)}</span>`).join("<br>") : "";
  return `<div class="panel pad"><dl class="kv">
      <dt>Chapter</dt><dd>${m.number}${m.part_title ? `, ${esc(m.part_title)}` : ""}</dd>
      <dt>Ingested</dt><dd>${esc(when(m.ingested_at))}</dd>
      ${passes}
      ${m.script ? `<dt>Script</dt><dd>${m.script.lines} lines, ${m.script.script_words} words for ${m.script.source_words} in the text (coverage ${m.script.coverage})</dd>` : ""}
      ${render}
      ${present ? `<dt>Present</dt><dd>${present}</dd>` : ""}
      ${fresh ? `<dt>New here</dt><dd>${fresh}</dd>` : ""}
      ${d.character_pass?.pronunciations?.length ? `<dt>Pronunciations</dt><dd>${d.character_pass.pronunciations.map((p) => `${esc(p.term)} (${esc(p.respelling)})`).join(", ")}</dd>` : ""}
    </dl></div>`;
}

function inline(text) {
  return esc(text).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\*(.+?)\*/g, "<i>$1</i>");
}

function prose(md) {
  return String(md || "").split(/\n{2,}/).map((block) => {
    const b = block.trim();
    if (!b) return "";
    if (/^#{1,3} /.test(b)) return `<h2>${inline(b.replace(/^#{1,3} /, ""))}</h2>`;
    if (b.startsWith(">")) return `<blockquote>${b.split("\n").map((l) => inline(l.replace(/^>\s?/, ""))).join("<br>")}</blockquote>`;
    if (b === "---") return "<hr>";
    return `<p>${b.split("\n").map(inline).join("<br>")}</p>`;
  }).join("");
}

// Cast

function castTab(sd, v, slug) {
  if (!v) return `<div class="empty">Loading…</div>`;
  const pending = v.voices.filter((x) => x.status !== "approved");
  const approved = v.voices.filter((x) => x.status === "approved");
  const usel = uselFor(slug);
  const uncastRows = v.uncast.map((u) => `<tr class="${usel.has(u.id) ? "sel" : ""}">
      <td><input type="checkbox" data-change="usel" data-id="${esc(u.id)}" ${usel.has(u.id) ? "checked" : ""} aria-label="Select ${esc(u.name)}"></td>
      <td><b>${esc(u.name)}</b> <span class="mono small muted">${esc(u.id)}</span>${u.role ? `<div class="small muted">${esc(u.role)}</div>` : ""}</td>
      <td><span class="tag">${esc(u.importance)}</span>${u.speaks ? "" : ` <span class="tag">silent so far</span>`}${u.policy ? ` <span class="pill auto">will be cast</span>` : ""}</td>
      <td class="num right hide-sm">${u.chapters}</td>
      <td class="small muted hide-sm">${u.brief ? esc(u.brief.instruct) : "no brief yet"}</td>
    </tr>`).join("");
  return `
    ${pending.length ? `<div class="notice info">${plural(pending.length, "voice is", "voices are")} waiting for you to listen. Approve the take you like, file another candidate, or recast with a new description.</div>` : ""}
    ${pending.length ? `<div class="section-head"><h2>To review</h2></div><div class="voices">${pending.map((x) => voiceCard(x, slug)).join("")}</div>` : ""}
    <div class="section-head"><h2>Approved</h2><span class="muted small">${approved.length ? "" : "none yet"}</span></div>
    <div class="voices">${approved.map((x) => voiceCard(x, slug)).join("")}</div>
    <div class="section-head"><h2>No voice yet</h2>
      <div class="row tight">
        <button class="sm" data-act="cast-open" data-mode="sel" ${usel.size ? "" : "disabled"}>Cast selected…</button>
        <button class="sm primary" data-act="cast-open" data-mode="all">Cast everyone who needs a voice…</button>
      </div></div>
    <div class="panel table-wrap"><table class="list">
      <thead><tr><th></th><th>Character</th><th>Importance</th><th class="hide-sm right">Chapters</th><th class="hide-sm">Cached brief</th></tr></thead>
      <tbody>${uncastRows || `<tr><td colspan="5" class="empty">Everyone in the bible has a voice.</td></tr>`}</tbody>
    </table></div>
    <p class="muted small">Casting everyone covers major and recurring characters who have spoken. The narrator reads the lines of anyone without a voice. The next free seed range starts at ${v.next_seed}.</p>`;
}

function voiceCard(x, slug) {
  const chosen = x.candidates[x.chosen] || {};
  const facts = [
    x.importance && `<span class="tag">${esc(x.importance)}</span>`,
    `seed ${esc(x.seed)}`,
    chosen.score != null && `match ${Number(chosen.score).toFixed(2)}`,
    x.chapters && `in ${plural(x.chapters, "chapter")}`,
    x.rendered_in.length && `rendered in ${esc(rangeText(x.rendered_in))}`,
    x.effect && `<span class="tag">${esc(x.effect.effect)} effect</span>`,
  ].filter(Boolean).join(" · ");
  const bio = [x.role, x.description].filter(Boolean).join(" ");
  const rounds = x.history.slice().reverse().map((h) => `<div class="round">
      <div class="small muted">Earlier round${h.replaced_at ? `, replaced ${esc(when(h.replaced_at))}` : ""}${h.why_replaced ? `: ${esc(h.why_replaced)}` : ""}</div>
      <div class="instruct small">${esc(h.instruct)}</div>
      ${candidates(x, h.candidates, null)}</div>`).join("");
  const count = x.candidates.length + x.history.reduce((t, h) => t + (h.candidates || []).length, 0);
  return `<div class="panel voice${x.status !== "approved" ? " pending" : ""}">
      <div class="who">${x.media ? playBtn(x.media.url, `${x.name}, filed voice`) : ""}
        <div class="grow"><h3>${esc(x.name)}</h3><div class="mono small muted">${esc(x.id)}</div></div>
        <span class="pill ${esc(x.status)}">${esc(x.status)}</span></div>
      <div class="small muted">${facts}</div>
      ${bio ? `<div class="bio" data-act="expand" title="Click to expand">${esc(bio)}</div>` : ""}
      <div class="instruct" data-act="expand" title="Click to expand">${esc(x.instruct)}</div>
      <div class="sample">“${esc(x.sample_text)}”</div>
      <details class="cands" data-key="cands:${esc(x.id)}"><summary>${plural(count, "candidate")}${x.history.length ? ` across ${x.history.length + 1} rounds` : ""}</summary>
        ${candidates(x, x.candidates, x.seed)}${rounds}
      </details>
      <div class="actions">
        ${x.status !== "approved" ? `<button class="sm primary" data-act="pick" data-id="${esc(x.id)}" data-seed="${esc(x.seed)}" data-approve="1">Approve</button>` : ""}
        <button class="sm" data-act="recast" data-id="${esc(x.id)}">Recast…</button>
      </div>
    </div>`;
}

function candidates(x, list, current) {
  return (list || []).map((c) => {
    const isCurrent = c.seed === current;
    const btns = [];
    if (!isCurrent) btns.push(`<button class="sm" data-act="pick" data-id="${esc(x.id)}" data-seed="${c.seed}" title="File this take and keep the status">File</button>`);
    if (!isCurrent || x.status !== "approved") btns.push(`<button class="sm" data-act="pick" data-id="${esc(x.id)}" data-seed="${c.seed}" data-approve="1">${isCurrent ? "Approve" : "File and approve"}</button>`);
    return `<div class="cand${isCurrent ? " chosen" : ""}">
      ${c.media ? playBtn(c.media.url, `${x.name}, seed ${c.seed}`) : `<span class="small muted">no clip</span>`}
      <div><div>seed ${esc(c.seed)} · match ${Number(c.score).toFixed(2)}${c.clipped ? " · clipped" : ""}${isCurrent ? " · filed" : ""}</div>
        ${Number(c.score) < 0.999 ? `<div class="tr">heard: ${esc(c.transcript)}</div>` : ""}</div>
      <div class="btns">${btns.join("")}</div></div>`;
  }).join("");
}

function openRecast(slug, x, nextSeed) {
  openDialog({
    title: `Recast ${x.name}`,
    body: `<p class="small muted">Designs new candidates and files the best transcript match. The current voice moves to an earlier round, where you can still pick it.</p>
      <label class="field"><span>Voice description</span><textarea name="instruct">${esc(x.instruct)}</textarea></label>
      <label class="inline small"><input type="checkbox" name="redo_brief"> ask the casting pass for a fresh description instead</label>
      <div class="fields">
        <label class="field"><span>Candidates</span><input type="number" name="candidates" value="4" min="1" max="12"></label>
        <label class="field"><span>First seed</span><input type="number" name="seed" value="${nextSeed}" min="0"></label>
      </div>
      <label class="field"><span>Why replace it (kept with the old voice)</span><input type="text" name="note" placeholder="too young, too nasal, ..."></label>
      ${llmFields()}`,
    submit: "Queue recast",
    onInput: (form) => {
      field(form, "instruct").disabled = checked(form, "redo_brief");
      $("#dlg-note").textContent = `Makes ${field(form, "candidates").value} candidates from seed ${field(form, "seed").value}.`;
    },
    onSubmit: async (form) => {
      const redo = checked(form, "redo_brief");
      const instruct = field(form, "instruct").value.trim();
      if (!redo && !instruct) throw new Error("Write a voice description or ask for a fresh one");
      await queue([{ kind: "cast", params: {
        slug, characters: [x.id], recast: true, candidates: num(form, "candidates"), seed: num(form, "seed"),
        note: field(form, "note").value.trim() || undefined, instruct: redo ? undefined : instruct, redo_brief: redo,
        ...llmParams(form, true),
      } }]);
    },
  });
}

function openCast(slug, ids, v) {
  const one = ids.length === 1 ? v.uncast.find((u) => u.id === ids[0]) : null;
  const who = ids.length ? ids.map((id) => esc(v.uncast.find((u) => u.id === id)?.name || id)).join(", ") : "every major or recurring character who has spoken and has no voice";
  openDialog({
    title: ids.length ? `Cast ${plural(ids.length, "character")}` : "Cast everyone who needs a voice",
    body: `<p class="small">${who}</p>
      <p class="small muted">A casting pass writes a voice description and a sample line. Breeze then designs the candidates, unless the serial names another engine, and each is transcribed and the best match filed with status auto.</p>
      ${one ? `<label class="field"><span>Voice description (leave empty to let the casting pass write it)</span><textarea name="instruct">${esc(one.brief?.instruct || "")}</textarea></label>` : ""}
      <div class="fields">
        <label class="field"><span>Candidates</span><input type="number" name="candidates" value="3" min="1" max="12"></label>
        <label class="field"><span>First seed</span><input type="number" name="seed" value="${v.next_seed}" min="0"></label>
      </div>
      <label class="inline small"><input type="checkbox" name="dry_run"> write the briefs only, make no audio</label>
      <label class="inline small"><input type="checkbox" name="redo_brief"> ignore cached briefs and ask the casting pass again</label>
      ${llmFields()}`,
    submit: "Queue",
    onSubmit: async (form) => {
      const params = { slug, characters: ids, candidates: num(form, "candidates"), seed: num(form, "seed"),
        dry_run: checked(form, "dry_run"), redo_brief: checked(form, "redo_brief"), ...llmParams(form, true) };
      if (one) {
        const text = field(form, "instruct").value.trim();
        if (text && text !== (one.brief?.instruct || "").trim()) params.instruct = text;
      }
      await queue([{ kind: "cast", params }]);
      uselFor(slug).clear();
    },
  });
}

// Bible

function bibleTab(b) {
  if (!b) return `<div class="empty">Loading…</div>`;
  const imps = ["all", ...IMPORTANCE];
  const chars = b.characters.map((c) => {
    const ch = c.chapters_present || [];
    const search = [c.id, c.name, ...(c.aliases || []), c.role].join(" ").toLowerCase();
    const facts = (c.facts || []).map((f) => `<li><b>ch ${esc(f.chapter)}</b><span>${esc(f.text)}</span></li>`).join("");
    return `<details class="char" data-key="char:${esc(c.id)}" data-search="${esc(search)}" data-imp="${esc(c.importance)}">
      <summary><span class="name">${esc(c.name)}</span><span class="tag">${esc(c.importance)}</span>
        <span class="small muted">${esc(c.gender)}, ${esc(String(c.age_group || "").replace("_", " "))}</span>
        <span class="small muted">${ch.length ? `ch ${esc(rangeText(ch))}` : ""}</span>
        <span class="grow"></span>
        ${c.voice_id ? `<span class="pill ${esc(c.voice_status || "")}">${esc(c.voice_status || "voiced")}</span>` : c.speaks === false ? `<span class="tag">silent</span>` : `<span class="small muted">no voice</span>`}
      </summary>
      <div class="body">
        <div><span class="mono small muted">${esc(c.id)}</span>${c.aliases?.length ? ` · also ${esc(c.aliases.join(", "))}` : ""}</div>
        <div><b>Role.</b> ${esc(c.role)}</div>
        ${c.description ? `<div><b>Description.</b> ${esc(c.description)}</div>` : ""}
        <div><b>Voice brief.</b> ${esc(c.voice_brief)}</div>
        ${facts ? `<div><b>Facts</b><ul class="facts">${facts}</ul></div>` : ""}
      </div></details>`;
  }).join("");
  const prons = [...b.pronunciations].sort((x, y) => (y.say ? 1 : 0) - (x.say ? 1 : 0));
  const overrides = prons.filter((p) => p.say).length;
  const busy = new Set((S.state?.jobs || []).filter((j) => j.kind === "say-as" && j.slug === S.route.slug
    && (j.status === "queued" || j.status === "running")).map((j) => j.meta?.term));
  const pronRow = (p) => S.pronEdit === p.term ? pronEditRow(p) : `<tr class="${p.say ? "override" : ""}">
      <td><b>${esc(p.term)}</b></td>
      <td class="mono hide-sm">${esc(p.respelling || "")}</td>
      <td>${p.say ? `<b>${esc(p.say)}</b>` : `<span class="small muted">as written</span>`}</td>
      <td class="small muted hide-sm">${esc(p.note || "")}</td>
      <td class="num right hide-sm">${esc(p.chapter ?? "")}</td>
      <td class="right">${busy.has(p.term) ? `<span class="small muted nowrap">saving…</span>`
        : `<button class="sm ghost" data-act="pron-edit" data-term="${esc(p.term)}">${p.say ? "Edit" : "Override"}</button>`}</td></tr>`;
  const pron = (S.pronEdit === "" ? pronEditRow({ term: "" }) : "") + prons.map(pronRow).join("");
  return `<div class="row" style="margin-bottom:12px">
      <input type="search" id="bible-q" data-input="bible-q" placeholder="Search names, aliases, roles" value="${esc(S.bibleQ)}" class="grow" style="min-width:200px">
      <div class="filters">${imps.map((i) => `<button class="${S.bibleImp === i ? "on" : ""}" data-act="bible-imp" data-imp="${i}">${i.replace("_", " ")}</button>`).join("")}</div>
    </div>
    <div class="panel">${chars || `<div class="empty">The bible is empty. Run the character pass.</div>`}</div>
    <div class="section-head"><h2>Pronunciations</h2><span class="muted small grow">${prons.length}${overrides ? ` · ${plural(overrides, "override")}` : ""}</span>
      <button class="sm" data-act="pron-edit" data-term="">Add an override</button></div>
    <p class="small muted">An override replaces the term with its spoken form in every line a voice reads, as a whole word with the same capitals. The text and the script keep the original spelling. A change applies from the next render, which regenerates only the lines that name the term.</p>
    <div class="panel table-wrap"><table class="list pron"><thead><tr><th>Term</th><th class="hide-sm">Respelling</th><th>Say as</th><th class="hide-sm">Note</th><th class="hide-sm right">Ch</th><th></th></tr></thead>
      <tbody>${pron || `<tr><td colspan="6" class="empty">None yet.</td></tr>`}</tbody></table></div>`;
}

function pronEditRow(p) {
  const draft = S.pronDraft[p.term] || {};
  const val = (k, fallback) => esc(draft[k] ?? fallback ?? "");
  return `<tr class="editing" data-pron-row data-term="${esc(p.term)}"><td colspan="6"><div class="pron-edit">
      ${p.term ? `<div><b>${esc(p.term)}</b>${p.respelling ? ` <span class="mono small muted">${esc(p.respelling)}</span>` : ""}</div>`
        : `<label>Term <input data-input="pron" data-pron="term" value="${val("term")}" placeholder="as the text spells it" maxlength="80"></label>`}
      <label>Say as <input data-input="pron" data-pron="say" value="${val("say", p.say)}" placeholder="the spelling the voice reads" maxlength="120"></label>
      <label>Note <input data-input="pron" data-pron="note" value="${val("note", p.note)}" maxlength="300"></label>
      <div class="row tight">
        <button class="sm primary" data-act="pron-save">Save</button>
        ${p.say ? `<button class="sm danger" data-act="pron-clear">Remove override</button>` : ""}
        <button class="sm ghost" data-act="pron-cancel">Cancel</button>
      </div></div></td></tr>`;
}

function applyFilters() {
  const q = S.bibleQ.toLowerCase().trim();
  $$("details.char").forEach((d) => {
    d.hidden = (q && !d.dataset.search.includes(q)) || (S.bibleImp !== "all" && d.dataset.imp !== S.bibleImp);
  });
  $$(".line[data-speaker]").forEach((l) => { l.hidden = !!S.spk && l.dataset.speaker !== S.spk; });
  $$("button.chip[data-act=spk]").forEach((b) => b.classList.toggle("off", !!S.spk && b.dataset.id !== S.spk));
}

// Jobs

VIEWS.jobs = () => {
  document.title = "Jobs · Serial Audio";
  const st = S.state;
  if (!st) return `<div class="empty">Loading…</div>`;
  const run = st.jobs.filter((j) => j.status === "running");
  const queued = st.jobs.filter((j) => j.status === "queued");
  const done = st.jobs.filter((j) => FINISHED.has(j.status));
  const c = st.comfy || {};
  const est = queued.reduce((t, j) => t + (j.estimate || 0), 0);
  return `<div class="page-head"><div><h1>Jobs</h1>
      <div class="sub">${st.executor === "nomad"
        ? "Renders, casting and the Claude passes run on the Nomad cluster, on whichever worker offers them. Jobs on different chapters or workers run side by side, and a worker runs one GPU job at a time. Quick library jobs run here."
        : "Heavy jobs run one at a time and wait for anything else using the pipeline, including runs started outside the app. Quick jobs, such as picking a voice or importing a page, run straight away."}</div></div></div>
    ${st.executor === "nomad" ? workersPanel(st) : `<div class="panel pad machine">
      <div class="stat"><b><span class="pill ${c.up ? "done" : "down"}">${c.up ? "up" : "down"}</span></b><span>ComfyUI${c.version ? ` ${esc(c.version)}` : ""}</span></div>
      <div class="stat"><b>${c.up ? `${c.running || 0} running, ${c.pending || 0} waiting` : "not reachable"}</b><span>ComfyUI prompts</span></div>
      <div class="stat"><b>${gb(c.ram_free)} of ${gb(c.ram_total)}</b><span>RAM free</span></div>
      <div class="stat"><b>${gb(c.vram_free)} of ${gb(c.vram_total)}</b><span>VRAM free</span></div>
      <div class="stat"><b>${st.minutes_per_line} min</b><span>render time per script line</span></div>
    </div>`}
    <div class="section-head"><h2>Now</h2></div>
    <div class="panel">${[...run.map(jobRow), ...st.external.map(externalRow)].join("") || `<div class="empty">Nothing is running.</div>`}</div>
    <div class="section-head"><h2>Queue <span class="muted small">${queued.length ? `${plural(queued.length, "job")}${est ? `, renders about ${span(est)}` : ""}` : ""}</span></h2>
      <button class="sm" data-act="queue-hold" data-paused="${st.paused ? 0 : 1}">${st.paused ? "Resume the queue" : "Hold the queue"}</button></div>
    ${st.paused ? `<div class="notice warn" style="margin-bottom:10px">The queue is on hold. Heavy jobs wait until you resume it. Quick jobs still run.</div>` : ""}
    <div class="panel">${queued.map(jobRow).join("") || `<div class="empty">The queue is empty.</div>`}</div>
    <div class="section-head"><h2>Finished</h2>
      <div class="row tight">
        ${st.finished_total > done.length ? `<button class="sm ghost" data-act="history-all">Show all ${st.finished_total}</button>` : ""}
        ${done.length ? `<button class="sm ghost" data-act="jobs-clear">Clear finished</button>` : ""}
      </div></div>
    <div class="panel">${done.map(jobRow).join("") || `<div class="empty">No finished jobs yet.</div>`}</div>`;
};

function progressRow(p) {
  const { pct, eta } = progressBits(p);
  return `<div class="prog"><div class="bar${pct == null ? " indet" : ""}"><i style="width:${pct ?? 0}%"></i></div>
    <div class="meta"><span>${esc(p.detail || p.phase || "")}</span><span>${pct != null ? `${pct}%` : ""}${eta ? ` · ${eta}` : ""}</span></div></div>`;
}

function workersPanel(st) {
  const n = st.nomad || {};
  const running = st.jobs.filter((j) => j.status === "running");
  const cells = (st.workers || []).map((w) => {
    const job = running.find((j) => j.nomad?.node === w.name);
    const state = !w.ready ? (w.status !== "ready" ? w.status : w.busy ? "busy" : "not taking jobs") : job ? "working" : "idle";
    const pill = !w.ready ? "down" : job ? "running" : "done";
    return `<div class="stat"><b><span class="pill ${pill}">${esc(state)}</span></b>`
      + `<span>${esc(w.name)} · ${esc(w.classes.join(", "))}${job ? ` · ${esc(job.title)}` : ""}</span></div>`;
  });
  if (n.up === false) cells.unshift(`<div class="stat"><b><span class="pill down">down</span></b><span>Nomad: ${esc(n.error || "not reachable")}</span></div>`);
  else if (!cells.length) cells.push(`<div class="stat"><b>none</b><span>No Nomad worker offers serial_audio jobs yet</span></div>`);
  cells.push(`<div class="stat"><b>${st.minutes_per_line} min</b><span>render time per script line</span></div>`);
  return `<div class="panel pad machine">${cells.join("")}</div>`;
}

function jobRow(j) {
  const meta = [];
  if (j.slug) meta.push(esc(j.slug));
  if (j.status === "queued") {
    meta.push(`queued ${esc(when(j.created))}`);
    if (j.estimate) meta.push(`about ${span(j.estimate)}`);
    if (j.wait) meta.push(`<span class="wait">${esc(j.wait)}</span>`);
  } else if (j.status === "running" && !j.started) {
    meta.push(j.nomad?.dispatched_at ? `sent to the cluster ${esc(when(j.nomad.dispatched_at))}` : "sending to the cluster");
  } else if (j.status === "running") {
    meta.push(`started ${esc(when(j.started))}, running ${span(elapsed(j.started))}`
      + (j.nomad?.node ? ` on ${esc(j.nomad.node)}` : ""));
  } else {
    meta.push(esc(when(j.finished || j.created)));
    if (j.started && j.finished) meta.push(`took ${span(elapsed(j.started, j.finished))}`);
    if (j.exit_code != null && j.exit_code !== 0) meta.push(`exit code ${esc(j.exit_code)}`);
    if (j.note) meta.push(esc(j.note));
  }
  const id = esc(j.id);
  const log = `<button class="sm" data-act="job-log" data-id="${id}" data-title="${esc(j.title)}">Log</button>`;
  let btns = "";
  if (j.status === "running") btns = log + `<button class="sm danger" data-act="job-stop" data-id="${id}" data-kind="${esc(j.kind)}" data-comfy="${j.comfy ? 1 : 0}" data-engine="${esc(j.meta?.engine || "")}">Stop</button>`;
  else if (j.status === "queued") {
    btns = `<button class="icon" data-act="job-move" data-dir="up" data-id="${id}" title="Move up" aria-label="Move up">▲</button>`
      + `<button class="icon" data-act="job-move" data-dir="down" data-id="${id}" title="Move down" aria-label="Move down">▼</button>`
      + `<button class="sm" data-act="job-cancel" data-id="${id}">Cancel</button>`;
  } else btns = (j.started ? log : "") + `<button class="sm" data-act="job-retry" data-id="${id}">Run again</button>`;
  return `<div class="job">
      <div><div class="row tight"><span class="pill ${esc(j.status)}">${esc(j.status)}</span><span class="title">${esc(j.title)}</span></div>
        <div class="meta">${meta.join(" · ")}</div></div>
      <div class="btns">${btns}</div>
      ${j.status === "running" && j.progress ? progressRow(j.progress) : ""}
    </div>`;
}

function externalRow(e) {
  const title = e.kind === "script" ? `Script ${baseName(e.script)}` : `${cap(e.kind)}${e.chapter ? ` chapter ${e.chapter}` : ""}`;
  const meta = [e.slug, "started outside the app", `pid ${e.pid}`, e.started ? `since ${when(new Date(e.started * 1000).toISOString())}` : "",
    e.kind === "script" ? "it runs pipeline commands, so heavy jobs wait for it" : ""].filter(Boolean);
  const link = e.slug && e.chapter ? `<button class="sm" data-act="goto-log" data-slug="${esc(e.slug)}" data-n="${e.chapter}">Log</button>` : "";
  return `<div class="job">
      <div><div class="row tight"><span class="pill running">outside</span><span class="title">${esc(title)}</span></div>
        <div class="meta">${meta.map(esc).join(" · ")}</div></div>
      <div class="btns">${link}</div>
      ${e.progress ? progressRow(e.progress) : ""}
    </div>`;
}

// Actions

const ACT = {
  play: (el) => play(el.dataset.url, el.dataset.title, el.dataset.resume === "1", el.dataset.artist),
  "pl-skip": (el) => { P.audio.currentTime = Math.max(0, P.audio.currentTime + Number(el.dataset.by)); },
  "pl-close": () => {
    savePos(true);
    P.audio.pause();
    P.audio.removeAttribute("src");
    P.audio.load();
    P.url = null;
    $("#player").hidden = true;
    document.body.classList.remove("playing");
    markPlaying();
  },
  "dlg-close": () => $("#dlg").close(),
  "log-close": closeLog,
  "sel-none": () => { selFor(S.route.slug).clear(); paint(); },
  "sel-next": (el) => {
    const sd = S.data[routeKey(S.route)].serial;
    const sel = selFor(S.route.slug);
    sel.clear();
    sd.chapters.filter((c) => c.status !== "rendered" && !chapterBusy(S.route.slug, c.number, "render"))
      .slice(0, Number(el.dataset.count)).forEach((c) => sel.add(c.number));
    paint();
  },
  filter: (el) => { S.filter[S.route.slug] = el.dataset.status; paint(); },
  process: () => openProcess(S.route.slug),
  "next-step": (el) => nextStep(S.route.slug, Number(el.dataset.n)),
  job: async (el) => {
    if (el.dataset.confirm && !confirm(el.dataset.confirm)) return;
    await queue([{ kind: el.dataset.kind, params: JSON.parse(el.dataset.params) }]);
  },
  "ch-tab": (el) => { S.chTab = el.dataset.tab; store.set("chTab", S.chTab); paint(); },
  spk: (el) => { S.spk = el.dataset.id && S.spk !== el.dataset.id ? el.dataset.id : null; applyFilters(); },
  expand: (el) => el.classList.toggle("open"),
  pick:(el) => queue([{ kind: "pick-voice", params: {
    slug: S.route.slug, character: el.dataset.id, seed: Number(el.dataset.seed), approve: el.dataset.approve === "1",
  } }]),
  recast: (el) => {
    const v = S.data[routeKey(S.route)].extra;
    openRecast(S.route.slug, v.voices.find((x) => x.id === el.dataset.id), v.next_seed);
  },
  "cast-open": (el) => {
    const v = S.data[routeKey(S.route)].extra;
    openCast(S.route.slug, el.dataset.mode === "sel" ? [...uselFor(S.route.slug)] : [], v);
  },
  "bible-imp": (el) => { S.bibleImp = el.dataset.imp; paint(); },
  "pron-edit": (el) => {
    S.pronEdit = el.dataset.term;
    paint();
    $(`[data-pron-row] [data-pron=${S.pronEdit ? "say" : "term"}]`)?.focus();
  },
  "pron-cancel": () => { delete S.pronDraft[S.pronEdit]; S.pronEdit = null; paint(); },
  "pron-save": async (el) => {
    const row = el.closest("[data-pron-row]");
    const val = (k) => row.querySelector(`[data-pron=${k}]`)?.value.trim() ?? "";
    const term = row.dataset.term || val("term");
    const say = val("say");
    if (!term) throw new Error("Type the term as the text spells it");
    if (!say) throw new Error("Type the spelling the voice should read, or use Remove override");
    const note = val("note");
    const params = { slug: S.route.slug, term, say, ...(row.dataset.term || note ? { note } : {}) };
    await queue([{ kind: "say-as", params }]);
    delete S.pronDraft[row.dataset.term];
    S.pronEdit = null;
    paint();
  },
  "pron-clear": async (el) => {
    const term = el.closest("[data-pron-row]").dataset.term;
    if (!confirm(`Remove the override for ${term}? From the next render the voice reads it as written.`)) return;
    await queue([{ kind: "say-as", params: { slug: S.route.slug, term, clear: true } }]);
    delete S.pronDraft[term];
    S.pronEdit = null;
    paint();
  },
  "add-rr": async () => {
    const url = $("#rr-url").value.trim();
    if (!url) throw new Error("Paste a Royal Road fiction URL or id");
    await queue([{ kind: "add-royalroad", params: { url, slug: $("#rr-slug").value.trim() || undefined, chapters: $("#rr-chapters").value } }]);
  },
  "add-epub": async () => {
    const file = S.epubFile;
    if (!file) throw new Error("Pick an EPUB file");
    const up = await upload(null, file);
    await queue([{ kind: "add-epub", params: { file: up.name, slug: $("#epub-slug").value.trim() || undefined } }]);
    S.epubFile = null;
    paint();
  },
  "fetch-new": (el) => queue([{ kind: "fetch", params: { slug: el.dataset.slug, chapters: "new", limit: Number($("#fetch-limit").value) || 5 } }]),
  "import-clear": (el) => { S.imports[el.dataset.slug] = []; paint(); },
  "import-go": async (el) => {
    const slug = el.dataset.slug;
    const items = S.imports[slug] || [];
    const missing = items.filter((it) => !it.n);
    if (missing.length) throw new Error(`Give a chapter number for ${missing.map((it) => it.file.name).join(", ")}`);
    const force = $("#import-force")?.checked || false;
    el.disabled = true;
    const jobs = [];
    try {
      for (const it of items) {
        const up = await upload(slug, it.file);
        jobs.push({ kind: "fetch", params: { slug, chapter: it.n, html: up.name, force } });
      }
    } finally {
      el.disabled = false;
    }
    await queue(jobs, false);
    S.imports[slug] = [];
    paint();
  },
  "job-log": (el) => openLog(el.dataset.id, el.dataset.title),
  "job-stop": async (el) => {
    const d = el.dataset;
    const extra = [];
    if (d.comfy === "1") extra.push("Its ComfyUI prompt is cancelled too.");
    if (d.kind === "render") extra.push(d.engine === "breeze" ? "Finished takes are kept for the next render." : "The render loses the progress it has made.");
    if (!confirm(["Stop this job?", ...extra].join(" "))) return;
    await post(`/api/jobs/${el.dataset.id}/cancel`);
    await poll();
  },
  "job-cancel": async (el) => { await post(`/api/jobs/${el.dataset.id}/cancel`); await poll(); },
  "job-move": async (el) => { await post(`/api/jobs/${el.dataset.id}/move`, { direction: el.dataset.dir }); await poll(); },
  "job-retry": async (el) => {
    const res = await post(`/api/jobs/${el.dataset.id}/retry`);
    toast(`Queued: ${res.job.title}`);
    await poll();
  },
  "jobs-clear": async () => { await post("/api/jobs/clear"); await poll(); },
  "history-all": async () => { S.allHistory = true; await poll(); },
  "queue-hold": async (el) => { await post("/api/queue/pause", { paused: el.dataset.paused === "1" }); await poll(); },
  "goto-log": (el) => {
    S.chTab = "log";
    location.hash = `#/s/${el.dataset.slug}/c/${el.dataset.n}`;
  },
};

const CHANGE = {
  sel: (el) => {
    const sel = selFor(S.route.slug);
    const n = Number(el.dataset.n);
    if (el.checked) sel.add(n);
    else sel.delete(n);
    paint();
  },
  "sel-all": (el) => {
    const sd = S.data[routeKey(S.route)].serial;
    const f = S.filter[S.route.slug] || "all";
    const sel = selFor(S.route.slug);
    sd.chapters.filter((c) => f === "all" || c.status === f).forEach((c) => (el.checked ? sel.add(c.number) : sel.delete(c.number)));
    paint();
  },
  usel: (el) => {
    const sel = uselFor(S.route.slug);
    if (el.checked) sel.add(el.dataset.id);
    else sel.delete(el.dataset.id);
    paint();
  },
  "import-pick": async (el) => {
    const slug = el.dataset.slug;
    const files = [...el.files];
    const items = [];
    for (const file of files) items.push({ file, n: await detectChapter(file) });
    items.sort((a, b) => (a.n || 0) - (b.n || 0));
    S.imports[slug] = items;
    paint();
  },
  "epub-pick": (el) => { S.epubFile = el.files[0] || null; },
  "import-n": (el) => {
    const it = (S.imports[el.dataset.slug] || [])[Number(el.dataset.i)];
    if (it) it.n = Number(el.value) || null;
  },
};

document.addEventListener("click", async (e) => {
  const el = e.target.closest("[data-act]");
  if (!el || el.disabled) return;
  const fn = ACT[el.dataset.act];
  if (!fn) return;
  e.preventDefault();
  try {
    await fn(el, e);
  } catch (err) {
    toast(err.message, true);
  }
});

document.addEventListener("change", async (e) => {
  const el = e.target.closest("[data-change]");
  if (!el) return;
  try {
    await CHANGE[el.dataset.change](el, e);
  } catch (err) {
    toast(err.message, true);
  }
});

document.addEventListener("input", (e) => {
  if (e.target.dataset.input === "bible-q") {
    S.bibleQ = e.target.value;
    applyFilters();
  }
  if (e.target.dataset.input === "pron") {
    const key = e.target.closest("[data-pron-row]").dataset.term;
    (S.pronDraft[key] ||= {})[e.target.dataset.pron] = e.target.value;
  }
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) poll();
  schedulePoll();
});

$("#logdlg").addEventListener("close", () => { L.id = null; clearTimeout(L.timer); });

// Start

initPlayer();
window.addEventListener("hashchange", show);
show();
poll().then(schedulePoll);
