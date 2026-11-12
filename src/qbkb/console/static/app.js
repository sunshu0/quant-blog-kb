// qbkb 控制台前端：纯原生 JS，不从任何 CDN 加载东西。
// 除了服务端渲染好的文章 HTML（render.py 已做安全处理），页面上的文字一律用 textContent 写入。
"use strict";

const TOKEN = document.querySelector('meta[name="qbkb-token"]').content;
const $ = (id) => document.getElementById(id);
const state = { tab: "browse", url: null, view: "zh", logNext: 0, jobId: null, poll: null };

async function api(path, body) {
  const opts = body === undefined
    ? {}
    : {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-QBKB-Token": TOKEN },
        body: JSON.stringify(body),
      };
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (e) { /* 非 JSON */ }
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

function toast(msg, bad) {
  const t = $("toast");
  t.textContent = msg;
  t.className = bad ? "toast bad" : "toast";
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, bad ? 8000 : 4000);
}

function el(tag, props, ...children) {
  const e = document.createElement(tag);
  Object.assign(e, props || {});
  for (const c of children) e.append(c);
  return e;
}

// 时间一律显示成本机时间（状态库里的 translated_at 是 UTC，控制台自己记的是本机时间）
function fmtTime(s) {
  if (!s) return "—";
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString("zh-CN", { hour12: false });
}

// ---- 标签页 ----

function showTab(name) {
  state.tab = name;
  for (const b of document.querySelectorAll(".tabs button")) {
    b.classList.toggle("active", b.dataset.tab === name);
  }
  for (const s of document.querySelectorAll(".tab")) s.hidden = s.id !== `tab-${name}`;
  if (name === "browse") loadToc();
  if (name === "sections") loadSections();
  if (name === "review") loadReview();
  if (name === "settings" || name === "run") loadState();
}

// ---- 顶部状态 ----

async function loadState() {
  const s = await api("/api/state");
  const badge = $("mode-badge");
  badge.textContent = s.mode === "demo" ? "演示模式" : "真实模式";
  badge.className = `badge ${s.mode}`;
  $("demo-banner").hidden = s.mode !== "demo";
  const n = (s.counts.needs_review || 0) + (s.counts.failed || 0);
  $("review-count").textContent = n ? String(n) : "";
  for (const which of ["deepl", "llm"]) {
    const k = s.keys[which];
    const st = $(`key-${which}-state`);
    st.textContent = k.configured ? `已配置（末 4 位 ${k.last4 || "****"}）` : "未配置";
    st.className = k.configured ? "keystate ok" : "keystate";
  }
  for (const r of document.querySelectorAll('input[name="mode"]')) r.checked = r.value === s.mode_setting;
  $("data-dir").textContent = `当前数据目录：${s.data_dir}`;
  $("sched-enabled").checked = s.schedule.enabled;
  $("sched-time").value = s.schedule.time;
  $("sched-limit").value = s.schedule.limit;
  let task = "";
  if (!s.schedule.supported) task = "这台电脑不是 Windows，自动更新不可用。";
  else if (s.task) {
    task = s.task.exists
      ? (s.task.ours ? "任务计划里有控制台建的任务（qbkb-daily-update）。" : "任务计划里有同名任务，但不是控制台建的。")
      : "任务计划里没有自动更新任务。";
  }
  $("sched-state").textContent = task;
  const a = s.last_auto;
  $("sched-last").textContent = a
    ? `上次自动更新：${fmtTime(a.time)}，${a.result}（新增 ${a.new ?? 0} 篇，${a.mode === "demo" ? "演示" : "真实"}模式）`
    : "上次自动更新：还没有运行过。";
  showQuota(s.mode, s.quota);
  if (s.job && s.job.running && !state.poll) watchJob();
  else if (s.job && !state.jobId) renderJob(s.job);
  return s;
}

function showQuota(mode, q) {
  const box = $("run-quota");
  if (mode === "demo") { box.textContent = "演示模式不用 DeepL，不涉及额度。"; return; }
  box.textContent = q
    ? `DeepL 剩余额度：${q.remaining.toLocaleString()} 字符（已用 ${q.used.toLocaleString()} / 上限 ${q.limit.toLocaleString()}，${fmtTime(q.at)} 查询）`
    : "DeepL 剩余额度：还没查过（运行一次翻译，或在设置页点「测试连接」）。";
}

// ---- 浏览 ----

async function loadToc() {
  const data = await api("/api/articles");
  const toc = $("toc");
  toc.replaceChildren();
  toc.append(el("div", { className: "hint", textContent: `共 ${data.total} 篇已收录` }));
  if (!data.total) {
    toc.append(el("p", { className: "empty", textContent: "还没有文章：到「运行」页点一次「运行」。" }));
  }
  for (const g of data.groups) {
    toc.append(el("h3", { textContent: `${g.category}（${g.articles.length}）` }));
    for (const a of g.articles) {
      const bits = [a.date || "无日期"];
      const link = el("a", { href: "#", title: a.title_en || "" },
        a.title, el("span", { className: "sub", textContent: bits.join(" · ") }));
      link.dataset.url = a.url;
      link.classList.toggle("active", a.url === state.url);
      link.addEventListener("click", (ev) => { ev.preventDefault(); openArticle(a.url); });
      toc.append(link);
    }
  }
}

async function openArticle(url, view) {
  state.url = url;
  state.view = view || state.view;
  for (const a of document.querySelectorAll(".toc a")) a.classList.toggle("active", a.dataset.url === url);
  for (const b of document.querySelectorAll(".views button")) b.classList.toggle("active", b.dataset.view === state.view);
  const body = $("reader-body");
  try {
    const d = await api(`/api/article?url=${encodeURIComponent(url)}&view=${state.view}`);
    $("reader-meta").hidden = false;
    const src = $("meta-source");
    if (d.source_url) { src.href = d.source_url; src.hidden = false; } else src.hidden = true;
    $("meta-section").textContent = `栏目 ${d.section || "未记录"}`;
    $("meta-translated").textContent = `翻译时间 ${fmtTime(d.translated_at)}`;
    body.innerHTML = d.html; // 服务端已渲染并做过安全处理（render.py）
    body.parentElement.scrollTop = 0;
  } catch (e) {
    body.replaceChildren(el("p", { className: "empty", textContent: e.message }));
  }
}

// ---- 运行 ----

async function startRun() {
  const limit = parseInt($("run-limit").value, 10);
  try {
    const d = await api("/api/run", { limit });
    beginJob(d.job);
  } catch (e) { toast(e.message, true); }
}

function beginJob(job) {
  state.jobId = job.id;
  state.logNext = 0;
  $("run-log").textContent = "";
  renderJob(job);
  watchJob();
}

function watchJob() {
  if (state.poll) return;
  const tick = async () => {
    let d;
    try { d = await api(`/api/job?since=${state.logNext}`); } catch (e) { state.poll = null; return; }
    if (!d.job) { state.poll = null; return; }
    if (d.job.id !== state.jobId) { state.jobId = d.job.id; state.logNext = 0; $("run-log").textContent = ""; }
    renderJob(d.job);
    if (d.job.running) state.poll = setTimeout(tick, 1000);
    else {
      state.poll = null;
      loadState();
      if (state.tab === "review") loadReview();
      toast(`任务结束：${d.job.result}`, d.job.exit_code !== 0 && d.job.exit_code !== 5);
    }
  };
  state.poll = setTimeout(tick, 200);
}

function renderJob(job) {
  if (job.lines && job.lines.length) {
    const log = $("run-log");
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    log.append(job.lines.join("\n") + "\n");
    if (atBottom) log.scrollTop = log.scrollHeight;
  }
  if (job.next !== undefined) state.logNext = job.next;
  $("run-start").disabled = job.running;
  $("run-stop").disabled = !job.running;
  const p = job.progress;
  $("run-bar").style.width = job.running
    ? (p ? `${Math.max(4, (100 * (p.done - 0.5)) / p.total)}%` : "50%")
    : "100%";
  $("run-status").textContent = job.running
    ? `正在运行：${job.title}${job.stage_name ? `，当前阶段：${job.stage_name}` : ""}`
    : `${job.title}：${fmtTime(job.started)} 开始，${fmtTime(job.finished)} 结束`;
  const res = $("run-result");
  res.hidden = job.running;
  if (!job.running) {
    const cls = job.exit_code === 0 ? "ok" : job.exit_code === 5 ? "attention" : "bad";
    res.className = `result ${cls}`;
    const extra = job.kind === "run" ? ` 这次新发布 ${job.published} 篇。` : "";
    res.textContent = `结果（退出码 ${job.exit_code}）：${job.result}${extra}`;
  }
  if (job.quota) showQuota(job.mode, { ...job.quota, at: job.finished || job.started });
}

async function stopRun() {
  try { await api("/api/stop", {}); toast("已请求停止，稍等片刻。"); } catch (e) { toast(e.message, true); }
}

// ---- 待复查 ----

async function loadReview() {
  const d = await api("/api/review");
  const list = $("review-list");
  list.replaceChildren();
  if (!d.items.length) {
    list.append(el("p", { className: "empty", textContent: "没有需要处理的文章。" }));
    return;
  }
  for (const it of d.items) {
    const label = it.status === "needs_review" ? "需复查" : "失败";
    const what = el("div", { className: "what" },
      el("div", {}, el("span", { className: `tag ${it.status}`, textContent: label }), it.title || ""),
      el("div", { className: "url", textContent: it.url }),
      el("div", { className: "reason", textContent: `原因：${it.reason || "（没有记录）"}${it.retries ? `（已重试 ${it.retries} 次）` : ""}` }));
    const row = el("div", { className: "review-item" }, what);
    if (it.can_retranslate) {
      const b = el("button", { type: "button", textContent: "重翻" });
      b.addEventListener("click", async () => {
        try {
          const r = await api("/api/retranslate", { url: it.url });
          showTab("run");
          beginJob(r.job);
        } catch (e) { toast(e.message, true); }
      });
      row.append(b);
    }
    list.append(row);
  }
}

// ---- 栏目抽样 ----

let sectionsSite = null;
const expandedSections = new Set();

async function loadSections() {
  try {
    const d = await api(`/api/sections${sectionsSite ? `?site=${encodeURIComponent(sectionsSite)}` : ""}`);
    renderSections(d);
  } catch (e) { toast(e.message, true); }
}

function renderSections(d) {
  sectionsSite = d.site;
  const select = $("sections-site");
  select.replaceChildren();
  for (const s of d.sites) select.append(el("option", { value: s, textContent: s }));
  if (d.site) select.value = d.site;
  $("sections-generate").hidden = d.has_csv;
  $("sections-msg").textContent = "";
  if (!d.sites.length) {
    $("sections-msg").textContent = "没有配了栏目的站点（站点配置里要有 sections 节）。";
    $("sections-generate").hidden = true;
  }

  const sum = $("sections-summary");
  sum.replaceChildren();
  if (d.summary.length) for (const row of d.summary) sum.append(summaryCard(row));
  else if (d.has_csv) sum.append(el("p", { className: "empty", textContent: "抽样表是空的。" }));

  const list = $("sections-list");
  list.replaceChildren();
  if (d.sections.length) {
    for (const sec of d.sections) list.append(sectionCard(sec, d.site));
  } else {
    list.append(el("p", { className: "empty", textContent: "还没有抽样的文章：先到「运行」页抓取并抽取，再生成抽样表。" }));
  }
}

function summaryCard(row) {
  const conclusion = row["结论"] || "";
  const cls = conclusion.includes("超过七成") ? "ok"
    : conclusion.includes("不到七成") ? "attention" : "";
  const collect = row["是否采集"] === "是" ? "已选采集" : row["是否采集"] === "否" ? "未选" : "—";
  return el("div", { className: "sec-summary" },
    el("div", { className: "sec-summary-name", textContent: `${row["栏目"]}（${collect}）` }),
    el("div", { className: "sec-summary-nums", textContent: `已判定 ${row["已判定篇数"]} 篇 · 抽样 ${row["抽样"]} · 已标 ${row["已标"]}（要 ${row["要"]} / 不要 ${row["不要"]}）` }),
    el("div", { className: `sec-summary-ratio ${cls}`, textContent: `占比 ${row["占比"] || "—"}${conclusion ? ` — ${conclusion}` : ""}` })
  );
}

function sectionCard(sec, site) {
  const toggle = el("button", { type: "button", className: "ghost sec-toggle", textContent: "展开" });
  const head = el("div", { className: "sec-head" },
    el("div", { className: "sec-title", textContent: `${sec.name}（${sec.collect === "是" ? "已选采集" : "未选"}）` }),
    el("span", { className: "hint", textContent: `${sec.articles.length} 篇` }),
    el("button", { type: "button", className: "primary sec-translate", textContent: sec.estimate_chars ? `翻译这一栏（约 ${sec.estimate_chars.toLocaleString()} 字符）` : "翻译这一栏" }),
    toggle
  );
  const body = el("div", { className: "sec-body", hidden: true });
  for (const a of sec.articles) body.append(articleRow(a, site));
  toggle.addEventListener("click", () => {
    const hide = !body.hidden;
    body.hidden = hide;
    toggle.textContent = hide ? "展开" : "收起";
    if (hide) expandedSections.delete(sec.id); else expandedSections.add(sec.id);
  });
  head.querySelector(".sec-translate").addEventListener("click", async () => {
    try {
      const d = await api("/api/sections/translate", { site, section: sec.id });
      renderSections(d);
      toast("已翻译这一栏的标题和摘要（命中的不重复计费）。");
    } catch (e) { toast(e.message, true); }
  });
  if (expandedSections.has(sec.id)) { body.hidden = false; toggle.textContent = "收起"; }
  return el("div", { className: "card sec-card" }, head, body);
}

function articleRow(a, site) {
  const link = /^https?:\/\//.test(a.url)
    ? el("a", { href: a.url, target: "_blank", rel: "noopener noreferrer", textContent: "原文链接" })
    : el("span", { className: "hint", textContent: "—" });
  const makeBtn = (label, value) => {
    const b = el("button", { type: "button", textContent: label });
    if (a.mark === value) b.classList.add("active");
    b.addEventListener("click", () => markArticle(site, a.url, value, undefined));
    return b;
  };
  const note = el("input", { type: "text", value: a.note || "", placeholder: "备注（可选）" });
  note.addEventListener("change", () => markArticle(site, a.url, undefined, note.value));
  const bits = [el("strong", { textContent: a.title_en || "（无标题）" })];
  if (a.title_zh) bits.push(el("div", { className: "sec-art-zh", textContent: `中文标题：${a.title_zh}` }));
  const head = el("div", { className: "sec-art-head" },
    el("div", { className: "sec-art-title" }, bits),
    el("div", { className: "hint sec-art-meta" }, `${a.date || "无日期"} · `, link)
  );
  const body = [];
  if (a.summary) body.push(el("div", { className: "sec-art-summary", textContent: a.summary }));
  if (a.summary_zh) body.push(el("div", { className: "sec-art-summary-zh", textContent: a.summary_zh }));
  return el("div", { className: "sec-art" }, head, ...body,
    el("div", { className: "sec-art-actions" }, makeBtn("要", "要"), makeBtn("不要", "不要"), makeBtn("清除", ""), note));
}

async function markArticle(site, url, mark, note) {
  const body = { site, url };
  if (mark !== undefined) body.mark = mark;
  if (note !== undefined) body.note = note;
  try {
    const d = await api("/api/sections/mark", body);
    renderSections(d);
    toast(note !== undefined ? "备注已保存。" : (mark ? `已标「${mark}」。` : "已清除标记。"));
  } catch (e) { toast(e.message, true); }
}

async function generateSections() {
  if (!sectionsSite) { toast("先选一个站点。", true); return; }
  try {
    const d = await api("/api/sections/generate", { site: sectionsSite });
    renderSections(d);
    toast("抽样表已生成，可以开始标注了。");
  } catch (e) { toast(e.message, true); }
}

// ---- 设置 ----

async function saveKeys() {
  const body = { deepl: $("key-deepl").value.trim(), llm: $("key-llm").value.trim() };
  try {
    await api("/api/keys", body);
    $("key-deepl").value = "";
    $("key-llm").value = "";
    $("keys-msg").textContent = "已保存到本机 .env。";
    await loadState();
  } catch (e) { toast(e.message, true); }
}

async function testKey(which) {
  const typed = $(`key-${which}`).value.trim();
  const st = $(`key-${which}-state`);
  st.textContent = "测试中…";
  try {
    const d = await api("/api/keys/test", typed ? { which, key: typed } : { which });
    toast(d.message, !d.ok);
  } catch (e) { toast(e.message, true); }
  await loadState();
}

async function setMode(mode) {
  try { await api("/api/mode", { mode }); } catch (e) { toast(e.message, true); }
  state.url = null;
  $("reader-meta").hidden = true;
  $("reader-body").replaceChildren(el("p", { className: "empty", textContent: "从左边选一篇文章。" }));
  await loadState();
  toast("模式已切换。");
}

async function saveSchedule() {
  const body = {
    enabled: $("sched-enabled").checked,
    time: $("sched-time").value,
    limit: parseInt($("sched-limit").value, 10),
  };
  try {
    await api("/api/schedule", body);
    toast(body.enabled ? `已打开：每天 ${body.time} 自动更新，每次最多 ${body.limit} 篇。` : "已关闭自动更新。");
  } catch (e) { toast(e.message, true); }
  await loadState();
}

async function resetDemo() {
  if (!confirm("清空演示模式的全部数据（示例文章、日志以外的东西）？真实数据不受影响。")) return;
  try { await api("/api/demo/reset", {}); toast("演示数据已清空。"); } catch (e) { toast(e.message, true); }
  await loadState();
  if (state.tab === "browse") loadToc();
}

async function quit() {
  if (!confirm("停止控制台？正在运行的任务也会停下（进度已保存）。")) return;
  try { await api("/api/shutdown", {}); } catch (e) { /* 服务已经关了 */ }
  document.body.replaceChildren(el("p", { className: "empty", textContent: "控制台已停止，可以关闭这个页面。" }));
}

// ---- 绑定 ----

document.addEventListener("DOMContentLoaded", () => {
  for (const b of document.querySelectorAll(".tabs button")) b.addEventListener("click", () => showTab(b.dataset.tab));
  // 文章里的链接：外部的在新标签页打开（服务端已加 target），其他的（站内相对链接）不让它把控制台页面换掉
  $("reader-body").addEventListener("click", (ev) => {
    const a = ev.target.closest("a");
    if (!a) return;
    const href = a.getAttribute("href") || "";
    if (a.target !== "_blank" && !href.startsWith("#")) ev.preventDefault();
  });
  for (const b of document.querySelectorAll(".views button")) {
    b.addEventListener("click", () => { if (state.url) openArticle(state.url, b.dataset.view); });
  }
  $("run-start").addEventListener("click", startRun);
  $("run-stop").addEventListener("click", stopRun);
  $("sections-site").addEventListener("change", () => { sectionsSite = $("sections-site").value; loadSections(); });
  $("sections-generate").addEventListener("click", generateSections);
  $("keys-save").addEventListener("click", saveKeys);
  for (const b of document.querySelectorAll("[data-test]")) b.addEventListener("click", () => testKey(b.dataset.test));
  for (const r of document.querySelectorAll('input[name="mode"]')) r.addEventListener("change", () => setMode(r.value));
  $("sched-save").addEventListener("click", saveSchedule);
  $("demo-reset").addEventListener("click", resetDemo);
  $("quit").addEventListener("click", quit);
  loadState().then(() => showTab("browse")).catch((e) => toast(e.message, true));
});
