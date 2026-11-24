"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

// Tiny, safe markdown: escape first, then bold/italic/code/lists/paragraphs.
function md(text) {
  const lines = esc(text).split(/\r?\n/);
  let html = "", list = null;
  const inline = (s) => s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*(?!\s)(.+?)\*/g, "$1<em>$2</em>").replace(/`(.+?)`/g, "<code>$1</code>");
  const close = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const raw of lines) {
    const l = raw.trim();
    let m;
    if ((m = l.match(/^[-*•]\s+(.*)/))) { if (list !== "ul") { close(); html += "<ul>"; list = "ul"; } html += `<li>${inline(m[1])}</li>`; }
    else if ((m = l.match(/^\d+[.)]\s+(.*)/))) { if (list !== "ol") { close(); html += "<ol>"; list = "ol"; } html += `<li>${inline(m[1])}</li>`; }
    else if ((m = l.match(/^#{1,4}\s+(.*)/))) { close(); html += `<p><strong>${inline(m[1])}</strong></p>`; }
    else if (!l) { close(); }
    else { close(); html += `<p>${inline(l)}</p>`; }
  }
  close();
  return `<div class="md">${html}</div>`;
}

const fmtTime = (iso) => {
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString("sv-SE", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
};

// ---------------------------------------------------------------- status
async function loadStatus() {
  try {
    const s = await api("/api/status");
    $("#ver").textContent = "v" + s.version;
    const sensorsState = s.lhm ? "on" : s.nvidia_smi ? "part" : "off";
    const sensorsText = s.lhm ? "LibreHardwareMonitor" : s.nvidia_smi ? "nvidia-smi (CPU-temp saknas)" : "Begränsade sensorer";
    $("#pills").innerHTML = `
      <span class="pill" title="Lokal AI-motor"><span class="dot ${s.ollama ? "on" : "off"}"></span>Ollama ${s.ollama ? esc(s.ollama) : "körs inte"}</span>
      <span class="pill"><span class="dot ${s.model ? "on" : "off"}"></span>${s.model ? esc(s.model) : "Ingen modell"}</span>
      <span class="pill" title="Sensorkälla"><span class="dot ${sensorsState}"></span>${sensorsText}</span>
      ${s.recording ? `<span class="pill"><span class="dot off"></span>Spelar in…</span>` : ""}
      ${s.lhm ? "" : `<a class="pill" href="https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases" target="_blank" rel="noopener"
        title="Installera LibreHardwareMonitor, kör som administratör och slå på Options → Remote Web Server (port 8085)">+ CPU-temperaturer?</a>`}`;
    return s;
  } catch (e) {
    $("#pills").innerHTML = `<span class="pill"><span class="dot off"></span>Servern svarar inte</span>`;
  }
}

async function loadTokens() {
  const t = await api("/api/tokens").catch(() => null);
  if (t) $("#tokens").textContent = `Lokalt processade tokens: ${t.total_tokens.toLocaleString("sv-SE")} (${t.calls} AI-anrop)`;
}

// ---------------------------------------------------------------- hardware
async function loadHardware(refresh = false) {
  $("#hw").classList.add("loading");
  const hw = await api("/api/hardware" + (refresh ? "?refresh=1" : ""));
  const g = hw.primary_gpu || {};
  const mem = hw.memory || {};
  const st = mem.sticks || [];
  const s0 = st[0] || {};
  const speedBadge = s0.rated_speed_guess && s0.configured_speed && s0.rated_speed_guess > s0.configured_speed + 100
    ? `<span class="badge warn">märkt ${s0.rated_speed_guess}</span>` : "";
  const disks = (hw.disks || []).map((d) => `${esc(d.name)} <span class="badge">${esc(d.bus)} · ${d.size_gb} GB</span>`).join("<br>");
  $("#hw").classList.remove("loading");
  $("#hw").innerHTML = `<dl class="kv">
    <dt>Processor</dt><dd>${esc(hw.cpu.name)} <span class="badge">${hw.cpu.cores} kärnor / ${hw.cpu.threads} trådar</span></dd>
    <dt>Grafikkort</dt><dd>${esc(g.name || "–")} ${g.vram_gb ? `<span class="badge">${g.vram_gb} GB VRAM</span>` : ""}
      <div class="hint">Drivrutin ${esc(g.driver_version || "?")} ${g.driver_date ? "· " + esc(g.driver_date) : ""} · VRAM via ${esc(g.vram_source || "?")}</div></dd>
    <dt>Minne</dt><dd>${mem.total_gb} GB ${esc(s0.type || "")} · ${st.length} st · ${s0.configured_speed || "?"} MT/s ${speedBadge}
      <div class="hint">${esc(s0.manufacturer || "")} ${esc(s0.part_number || "")}</div></dd>
    <dt>Moderkort</dt><dd>${esc(hw.board.manufacturer || "")} ${esc(hw.board.model || "")}
      <div class="hint">BIOS ${esc(hw.bios.version || "?")} · ${esc(hw.bios.date || "")}</div></dd>
    <dt>Lagring</dt><dd>${disks || "–"}</dd>
    <dt>System</dt><dd>${esc(hw.os.name || "")} (build ${esc(hw.os.build || "?")}) · ${esc(hw.power_scheme.name || "")}</dd>
  </dl>`;
}

// ---------------------------------------------------------------- models
let pullTimer = null;
async function loadModels() {
  const m = await api("/api/models").catch((e) => ({ error: e.message }));
  const el = $("#model");
  el.classList.remove("loading");
  if (m.error) { el.textContent = m.error; return; }
  const bench = m.benchmark && m.benchmark.model === m.selected ? m.benchmark : null;
  const options = [`<option value="">Automatiskt (${esc(m.auto_choice || "ingen")})</option>`]
    .concat(m.installed.map((i) => `<option value="${esc(i.name)}" ${m.manual === i.name ? "selected" : ""}>${esc(i.name)}${i.fits ? "" : " (för stor)"}</option>`));
  el.innerHTML = `
    <div class="model-hero">
      <div>
        <div class="sub">Upptäckt: ${esc(m.basis)}</div>
        <div class="big">${esc(m.selected || "Ingen modell installerad")}</div>
        <div class="sub">Nivå: ${esc(m.tier.label)} · ${m.usable_gb} GB användbart för AI
          ${bench ? ` · <strong>${bench.tokens_per_second} tokens/s</strong> uppmätt` : ""}</div>
      </div>
    </div>
    ${!m.ollama ? `<div class="notice"><strong>Ollama hittades inte.</strong> AI-delen behöver den lokala AI-motorn Ollama
      (gratis). Installera från <a href="https://ollama.com/download" target="_blank" rel="noopener">ollama.com/download</a>,
      starta den och ladda om sidan. Hårdvarukollen och diagnosen fungerar redan utan AI.</div>` : ""}
    ${m.ollama && m.suggested_download ? `<p>Rekommenderad modell för din dator: <strong>${esc(m.suggested_download)}</strong> (~${m.tier.approx_size_gb} GB).</p>
      <button class="primary" id="btn-pull" data-name="${esc(m.suggested_download)}">⬇ Ladda ner ${esc(m.suggested_download)}</button>` : ""}
    <div id="pull-progress"></div>
    ${m.installed.length ? `<ul class="model-list">${m.installed.map((i) => `<li><span>${esc(i.name)}</span><span>${i.size_gb} GB
      <span class="badge ${i.fits ? "ok" : "warn"}">${i.fits ? "passar" : "för stor"}</span></span></li>`).join("")}</ul>` : ""}
    <div class="row">
      <label>Använd: <select id="model-select">${options.join("")}</select></label>
      <button id="btn-bench" ${m.selected ? "" : "disabled"}>⏱ Mät hastighet</button>
    </div>`;
  $("#model-select").onchange = async (e) => { await api("/api/models/select", { method: "POST", body: { name: e.target.value || null } }); loadModels(); loadStatus(); };
  $("#btn-bench").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Mäter…";
    try { await api("/api/models/benchmark", { method: "POST" }); } catch (err) { alert(err.message); }
    loadModels(); loadTokens();
  };
  const pb = $("#btn-pull");
  if (pb) pb.onclick = async () => { await api("/api/models/pull", { method: "POST", body: { name: pb.dataset.name } }); watchPull(); };
  if (m.pull && !m.pull.done) watchPull();
}

function watchPull() {
  clearInterval(pullTimer);
  pullTimer = setInterval(async () => {
    const m = await api("/api/models");
    const p = m.pull;
    const pct = p.total ? Math.round((100 * p.completed) / p.total) : 0;
    const el = $("#pull-progress");
    if (el) el.innerHTML = `<div class="hint">${esc(p.status || "")} ${pct ? pct + " %" : ""} ${p.error ? "– fel: " + esc(p.error) : ""}</div><div class="progress"><div style="width:${pct}%"></div></div>`;
    if (p.done) { clearInterval(pullTimer); loadModels(); loadStatus(); }
  }, 1000);
}

// ---------------------------------------------------------------- findings
const SEV = { kritisk: "Kritisk", varning: "Varning", info: "Info", ok: "Bra" };
function renderFindings(list) {
  if (!list.length) return `<p class="empty-ok">✓ Inga problem hittades. Gör en inspelning under last för en djupare analys.</p>`;
  return list.map((f) => `<div class="finding ${f.severity}">
      <div class="bar"></div>
      <div class="title"><span class="sev">${SEV[f.severity] || f.severity}</span>${esc(f.title)}</div>
      <div class="detail">${esc(f.detail)}</div>
      ${f.fix ? `<div class="fix">${esc(f.fix)}</div>` : ""}
    </div>`).join("");
}

async function loadFindings() {
  $("#findings").classList.add("loading");
  const list = await api("/api/diagnose");
  $("#findings").classList.remove("loading");
  $("#findings").innerHTML = renderFindings(list);
}

$("#btn-diag-refresh").onclick = loadFindings;
$("#btn-explain").onclick = async (e) => {
  const box = $("#ai-summary");
  box.classList.remove("hidden");
  box.innerHTML = `<span class="loading">Flaskhalsen tänker… (lokal AI, kan ta några sekunder)</span>`;
  e.target.disabled = true;
  try {
    const r = await api("/api/diagnose/explain", { method: "POST" });
    box.innerHTML = md(r.answer) + `<div class="meta">${esc(r.model)} · ${r.usage.prompt_tokens + r.usage.eval_tokens} tokens · ${r.usage.tokens_per_second ?? "?"} tokens/s</div>`;
  } catch (err) {
    box.innerHTML = `<span style="color:var(--crit)">${esc(err.message)}</span>`;
  }
  e.target.disabled = false;
  loadTokens();
};

// ---------------------------------------------------------------- recording + charts
const charts = {};
const COLORS = ["#38bdf8", "#f59e0b", "#f43f5e", "#a78bfa", "#22c55e", "#f472b6"];
function lineChart(id, series) {
  const ctx = document.getElementById(id);
  if (charts[id]) charts[id].destroy();
  if (typeof Chart === "undefined") { ctx.replaceWith(Object.assign(document.createElement("p"), { className: "hint", textContent: "Diagram kräver internet för Chart.js." })); return; }
  charts[id] = new Chart(ctx, {
    type: "line",
    data: { datasets: series.map((s, i) => ({ label: s.label, data: s.data, borderColor: COLORS[i % COLORS.length], backgroundColor: COLORS[i % COLORS.length], borderWidth: 1.6, pointRadius: 0, tension: .25, yAxisID: s.axis || "y" })) },
    options: {
      animation: false, maintainAspectRatio: false, parsing: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { color: "#c7d2e6", boxWidth: 10, font: { size: 11 } } } },
      scales: {
        x: { type: "linear", ticks: { color: "#8b9ab8", callback: (v) => v + " s" }, grid: { color: "#1b2742" } },
        y: { ticks: { color: "#8b9ab8" }, grid: { color: "#1b2742" } },
        ...(series.some((s) => s.axis === "y2") ? { y2: { position: "right", ticks: { color: "#8b9ab8" }, grid: { display: false } } } : {}),
      },
    },
  });
}

function renderSession(samples) {
  if (!samples.length) return;
  const t0 = samples[0].ts;
  const pts = (k) => samples.filter((s) => s[k] != null).map((s) => ({ x: Math.round(s.ts - t0), y: s[k] }));
  const have = (k) => samples.some((s) => s[k] != null);
  lineChart("ch-temp", [["gpu.temp", "GPU"], ["gpu.hotspot", "GPU hotspot"], ["cpu.temp", "CPU"], ["storage.temp_max", "SSD"]]
    .filter(([k]) => have(k)).map(([k, l]) => ({ label: l, data: pts(k) })));
  lineChart("ch-load", [["gpu.load", "GPU"], ["cpu.load", "CPU snitt"], ["cpu.core_load_max", "CPU hetaste kärna"], ["ram.used_pct", "RAM"]]
    .filter(([k]) => have(k)).map(([k, l]) => ({ label: l, data: pts(k) })));
  lineChart("ch-clock", [{ label: "GPU MHz", data: pts("gpu.clock") }, { label: "GPU W", data: pts("gpu.power"), axis: "y2" }]
    .concat(have("gpu.thr_thermal") ? [{ label: "Termisk strypning (×100)", data: pts("gpu.thr_thermal").map((p) => ({ x: p.x, y: p.y * 100 })), axis: "y2" }] : []));
}

async function loadSessions(selectId) {
  const list = await api("/api/sessions");
  const sel = $("#session-select");
  sel.innerHTML = list.length ? list.map((s) => `<option value="${s.id}">#${s.id} ${esc(s.label)} – ${new Date(s.started * 1000).toLocaleString("sv-SE")} (${s.n} s)</option>`).join("")
    : `<option value="">Inga inspelningar än</option>`;
  const id = selectId || (list[0] && list[0].id);
  if (id) { sel.value = id; await showSession(id); }
}

async function showSession(id) {
  const r = await api(`/api/sessions/${id}`);
  renderSession(r.samples);
  $("#session-findings").innerHTML = r.findings.length ? `<h3 style="margin:16px 0 4px;font-size:14px;color:var(--muted)">Fynd i denna inspelning</h3>` + renderFindings(r.findings) : "";
}
$("#session-select").onchange = (e) => e.target.value && showSession(e.target.value);

let recTimer = null, liveSamples = [];
$("#btn-rec").onclick = async () => {
  const st = await api("/api/status");
  if (st.recording) { await api("/api/record/stop", { method: "POST" }); return; }
  const body = { seconds: +$("#rec-seconds").value, label: $("#rec-label").value || "Inspelning" };
  try { await api("/api/record/start", { method: "POST", body }); } catch (e) { alert(e.message); return; }
  watchRecording();
};

function watchRecording() {
  clearInterval(recTimer);
  liveSamples = [];
  $("#btn-rec").textContent = "■ Stoppa";
  recTimer = setInterval(async () => {
    const st = await api("/api/status");
    loadStatus();
    if (!st.recording) {
      clearInterval(recTimer);
      $("#btn-rec").textContent = "● Spela in";
      $("#rec-status").innerHTML = "";
      await loadSessions();
      loadFindings();
      return;
    }
    const live = await api("/api/live");
    liveSamples.push({ ts: live.ts, ...live.metrics });
    renderSession(liveSamples);
    $("#rec-status").innerHTML = `<p><span class="live">● Spelar in</span> ${esc(st.recording.label)} – ${st.recording.elapsed} s, ${st.recording.remaining} s kvar</p>`;
  }, 2000);
}

// ---------------------------------------------------------------- events
const CAT = { crash: "Avstängning", bsod: "Blåskärm", gpu_driver: "GPU-drivrutin", whea: "Hårdvarufel", disk: "Disk", app_crash: "Programkrasch", app_hang: "Hängning", low_memory: "Slut på minne" };
async function loadEvents() {
  const ev = await api("/api/events");
  const el = $("#events");
  el.classList.remove("loading");
  if (!ev.length) { el.innerHTML = `<p class="empty-ok">✓ Inga krascher eller hårdvarufel de senaste 30 dagarna.</p>`; return; }
  const counts = {};
  ev.forEach((e) => (counts[e.cat] = (counts[e.cat] || 0) + 1));
  el.innerHTML = `<div class="ev-summary">${Object.entries(counts).map(([c, n]) => `<span class="cat ${c}">${CAT[c] || c}: ${n}</span>`).join("")}</div>
    <div class="ev-scroll"><table class="events">${ev.slice(0, 120).map((e) => `<tr><td>${fmtTime(e.t)}</td><td><span class="cat ${e.cat}">${CAT[e.cat] || e.cat}</span></td><td>${esc(e.text)}</td></tr>`).join("")}</table></div>`;
}

// ---------------------------------------------------------------- chat
const history = [];
function addMsg(role, html, cls = "") {
  const div = document.createElement("div");
  div.className = `msg ${role} ${cls}`;
  div.innerHTML = html;
  $("#chat-log").appendChild(div);
  $("#chat-log").scrollTop = 1e9;
  return div;
}
async function ask(q) {
  if (!q.trim()) return;
  history.push({ role: "user", content: q });
  addMsg("user", esc(q));
  const wait = addMsg("assistant", "Tänker och läser din dators data…", "thinking");
  try {
    const r = await api("/api/chat", { method: "POST", body: { messages: history } });
    history.push({ role: "assistant", content: r.answer });
    const tools = r.tool_calls.length ? `kollade: ${[...new Set(r.tool_calls)].join(", ")} · ` : "";
    wait.className = "msg assistant";
    wait.innerHTML = md(r.answer) + `<div class="meta">${tools}${r.usage.prompt_tokens + r.usage.eval_tokens} tokens · ${r.usage.tokens_per_second ?? "?"} tokens/s</div>`;
  } catch (e) {
    wait.className = "msg assistant";
    wait.innerHTML = `<span style="color:var(--crit)">${esc(e.message)}</span>`;
  }
  loadTokens();
}
$("#chat-form").onsubmit = (e) => { e.preventDefault(); const q = $("#chat-input").value; $("#chat-input").value = ""; ask(q); };
document.querySelectorAll(".chip").forEach((c) => (c.onclick = () => ask(c.textContent)));

$("#btn-hw-refresh").onclick = () => loadHardware(true).then(loadModels);

// ---------------------------------------------------------------- boot
(async () => {
  const st = await loadStatus();
  loadTokens();
  await loadHardware().catch((e) => ($("#hw").textContent = e.message));
  loadModels();
  loadFindings().catch((e) => ($("#findings").textContent = e.message));
  loadEvents().catch((e) => ($("#events").textContent = e.message));
  loadSessions();
  if (st && st.recording) watchRecording();
})();
