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
  if (!r.ok) {
    const err = new Error(data.error || `HTTP ${r.status}`);
    err.status = r.status;
    throw err;
  }
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
  if (name === "browse" && state.tab === "browse") closeReader();
  state.tab = name;
  for (const b of document.querySelectorAll(".tabs button")) {
    b.classList.toggle("active", b.dataset.tab === name);
  }
  for (const s of document.querySelectorAll(".tab")) s.hidden = s.id !== `tab-${name}`;
  updateSelectBar();
  if (name === "browse") loadToc().catch((e) => toast(e.message, true));
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

const MAX_PICK = 50; // 一次最多翻 50 篇（后端同样限制）
const browse = {
  data: null,
  site: "all", // "all" 或站点 id
  query: "",
  open: new Set(), // 展开的栏目：`站点/栏目id`
  picked: new Set(), // 勾选的文章 url，切站点、搜索、刷新列表都保留
  legacy: false,
  busy: false,
  scrollY: 0,
  onlyPicked: false, // 「只看已选的」
  byUrl: new Map(), // url → 文章，给确认框列标题、只看已选用
  visible: new Set(), // 当前列表（站点 + 搜索）里的 url
};

const BATCH = 50; // 大栏目分批显示，每批 50 张卡片
const BACKENDS = { deepl: "DeepL", azure: "Azure 翻译", llm: "大模型翻译", fake: "演示翻译" };
// 这个 flag 几乎每篇 PDF 都有，卡片上已有 PDF 标签，只在阅读区提示一次
const isPdfNoise = (f) => /PDF/.test(f) && /不完整/.test(f);

async function loadToc() {
  const data = await api("/api/articles");
  browse.data = data;
  browse.legacy = !Array.isArray(data.sites);
  if (browse.legacy) {
    browse.picked.clear();
    renderLegacyToc(data);
    updateSelectBar();
    return;
  }
  browse.byUrl = new Map();
  for (const s of data.sites) for (const sec of s.sections) for (const a of sec.articles) browse.byUrl.set(a.url, a);
  for (const u of [...browse.picked]) if (!browse.byUrl.has(u)) browse.picked.delete(u);
  if (browse.site !== "all" && !data.sites.some((s) => s.site === browse.site)) browse.site = "all";
  renderToc();
}

// 后端还没给 sites 时的旧渲染（按类别分组的纯列表，不能勾选）
function renderLegacyToc(data) {
  $("toc-search").hidden = true;
  $("toc-sites").hidden = true;
  $("toc-hint").textContent = `共 ${data.total} 篇已收录`;
  const list = $("toc-list");
  list.replaceChildren();
  if (!data.total) {
    list.append(el("p", { className: "empty", textContent: "还没有文章：到「运行」页点一次「运行」。" }));
  }
  for (const g of data.groups) {
    list.append(el("h3", { textContent: `${g.category}（${g.articles.length}）` }));
    for (const a of g.articles) {
      const bits = [a.date || "无日期"];
      const link = el("a", { href: "#", title: a.title_en || "" },
        a.title, el("span", { className: "sub", textContent: bits.join(" · ") }));
      link.dataset.url = a.url;
      link.classList.toggle("active", a.url === state.url);
      link.addEventListener("click", (ev) => { ev.preventDefault(); openArticle(a.url); });
      list.append(link);
    }
  }
}

function matches(a, q) {
  return [a.title, a.title_en, a.summary].some((s) => s && s.toLowerCase().includes(q));
}

function renderSiteChips(sites) {
  const box = $("toc-sites");
  box.replaceChildren();
  box.hidden = sites.length < 2;
  const total = sites.reduce((n, s) => n + s.total, 0);
  const chip = (id, label) => {
    const b = el("button", { type: "button", className: "chip", textContent: label });
    b.classList.toggle("active", browse.site === id);
    b.addEventListener("click", () => { browse.site = id; renderToc(); });
    return b;
  };
  box.append(chip("all", `全部 ${total}`));
  for (const s of sites) box.append(chip(s.site, `${s.name} ${s.total}`));
}

function renderToc() {
  const data = browse.data;
  const toc = $("toc");
  const scroll = toc.scrollTop;
  const list = $("toc-list");
  list.replaceChildren();
  $("toc-search").hidden = false;
  const q = browse.query.trim().toLowerCase();
  $("toc-hint").textContent = `共 ${data.total} 篇已收录，其中已翻全文 ${countTranslated(data.sites)} 篇`;
  if (!data.total) {
    $("toc-sites").hidden = true;
    list.append(el("p", { className: "empty", textContent: "还没有文章：到「运行」页点一次「运行」。" }));
    updateSelectBar();
    return;
  }
  renderSiteChips(data.sites);
  browse.visible = new Set();
  if (browse.onlyPicked && !browse.picked.size) browse.onlyPicked = false;
  if (browse.onlyPicked) {
    renderPickedList(list, q);
    toc.scrollTop = scroll;
    updateSelectBar();
    return;
  }
  let shown = 0;
  for (const site of data.sites) {
    if (browse.site !== "all" && browse.site !== site.site) continue;
    const blocks = [];
    for (const sec of site.sections) {
      const arts = q ? sec.articles.filter((a) => matches(a, q)) : sec.articles;
      for (const a of arts) browse.visible.add(a.url);
      if (q && !arts.length) continue;
      shown += arts.length;
      blocks.push(sectionBlock(site, sec, arts, q));
    }
    if (!blocks.length) continue;
    if (browse.site === "all" && data.sites.length > 1) {
      list.append(el("h3", { className: "site-title", textContent: `${site.name}（${site.total} 篇）` }));
    }
    list.append(...blocks);
  }
  if (q && !shown) list.append(el("p", { className: "empty", textContent: "没有匹配的文章。" }));
  else if (q) list.prepend(el("div", { className: "hint search-hit", textContent: `找到 ${shown} 篇` }));
  toc.scrollTop = scroll;
  updateSelectBar();
}

// 「只看已选的」：把勾选的文章（不管在哪个站点、搜不搜得到）平铺出来，可逐个取消
function renderPickedList(list, q) {
  $("toc-sites").hidden = true;
  const back = el("button", { type: "button", className: "ghost small", textContent: "← 回到全部文章" });
  back.addEventListener("click", () => { browse.onlyPicked = false; renderToc(); });
  list.append(el("div", { className: "picked-head" },
    el("span", { className: "hint", id: "picked-head-text" }), back));
  const box = el("div", { className: "sec-fold picked-list" });
  for (const u of browse.picked) {
    const a = browse.byUrl.get(u);
    if (!a || (q && !matches(a, q))) continue;
    browse.visible.add(u);
    box.append(articleCard(a));
  }
  list.append(box);
}

function countTranslated(sites) {
  let n = 0;
  for (const s of sites) for (const sec of s.sections) for (const a of sec.articles) if (a.translated) n += 1;
  return n;
}

function sectionBlock(site, sec, arts, q) {
  const key = `${site.site}/${sec.id}`;
  const done = sec.articles.filter((a) => a.translated).length;
  const count = q ? `${arts.length}/${sec.total}` : String(sec.total);
  const det = el("details", { className: "sec-fold" });
  det.append(el("summary", {},
    el("span", { className: "fold-name", textContent: sec.name }),
    el("span", { className: "fold-count", textContent: `${count} 篇` }),
    el("span", { className: "fold-done", textContent: `已翻全文 ${done}` })));
  const body = el("div", { className: "fold-body" });
  det.append(body);
  const fill = () => {
    if (body.dataset.filled) return;
    body.dataset.filled = "1";
    body.append(sectionTools(arts));
    let done = 0;
    const more = el("button", { type: "button", className: "ghost more-btn" });
    const batch = () => {
      const frag = document.createDocumentFragment();
      for (const a of arts.slice(done, done + BATCH)) frag.append(articleCard(a));
      done = Math.min(done + BATCH, arts.length);
      more.before(frag);
      const left = arts.length - done;
      more.hidden = left <= 0;
      more.textContent = `再显示 ${Math.min(BATCH, left)} 篇（还剩 ${left} 篇）`;
    };
    more.addEventListener("click", batch);
    body.append(more);
    batch();
  };
  det.addEventListener("toggle", () => {
    if (det.open) fill();
    if (!q) { if (det.open) browse.open.add(key); else browse.open.delete(key); }
  });
  if (q || browse.open.has(key)) { det.open = true; fill(); }
  return det;
}

// 栏目里的快捷操作：把这个栏目还没翻全文的文章一次勾上
function sectionTools(arts) {
  const todo = arts.filter((a) => !a.translated);
  const label = todo.length > MAX_PICK ? `勾选前 ${MAX_PICK} 篇（未翻 ${todo.length}）` : `勾选未翻的 ${todo.length} 篇`;
  const all = el("button", { type: "button", className: "ghost small", textContent: label });
  all.disabled = !todo.length;
  all.addEventListener("click", () => {
    let skipped = 0;
    for (const a of todo) {
      if (browse.picked.has(a.url)) continue;
      if (browse.picked.size >= MAX_PICK) { skipped += 1; continue; }
      browse.picked.add(a.url);
    }
    syncChecks();
    updateSelectBar();
    if (skipped) toast(`一次最多选 ${MAX_PICK} 篇，还有 ${skipped} 篇没勾上；先翻这一批，再选下一批。`, true);
  });
  return el("div", { className: "fold-tools" }, all);
}

function articleCard(a) {
  const card = el("div", { className: "art" });
  card.dataset.url = a.url;
  card.classList.toggle("active", a.url === state.url);

  const box = el("input", { type: "checkbox", className: "art-pick", checked: browse.picked.has(a.url) });
  box.dataset.url = a.url;
  box.setAttribute("aria-label", `选择：${a.title}`);
  box.addEventListener("change", () => {
    if (box.checked && browse.picked.size >= MAX_PICK) {
      box.checked = false;
      toast(`一次最多选 ${MAX_PICK} 篇。`, true);
      return;
    }
    if (box.checked) browse.picked.add(a.url); else browse.picked.delete(a.url);
    updateSelectBar();
    if (browse.onlyPicked && !browse.picked.size) renderToc();
  });
  const check = el("label", { className: "art-check" }, box);

  const title = el("a", { href: "#", className: "art-title", textContent: a.title || a.title_en || "（无标题）" });
  title.addEventListener("click", (ev) => { ev.preventDefault(); openArticle(a.url); });

  const main = el("div", { className: "art-main" }, title);
  if (a.title_en && a.title_en !== a.title) main.append(el("div", { className: "art-en", textContent: a.title_en }));

  const meta = el("div", { className: "art-meta" },
    el("span", { className: "art-date", textContent: a.date || "无日期" }),
    el("span", { className: `tag ${a.translated ? "full" : "brief"}`, textContent: a.translated ? "已翻全文" : "只有摘要" }));
  if (a.has_pdf) meta.append(el("span", { className: "tag pdf", textContent: "PDF" }));
  for (const f of (a.flags || []).filter((x) => !isPdfNoise(x)).slice(0, 3)) meta.append(el("span", { className: "tag flag", textContent: f }));
  main.append(meta);

  if (a.summary) {
    const sum = el("div", { className: "art-sum", textContent: a.summary });
    main.append(sum);
    if (a.summary.length > 60) {
      const more = el("button", { type: "button", className: "link-btn", textContent: "展开摘要" });
      more.addEventListener("click", () => {
        const open = sum.classList.toggle("open");
        more.textContent = open ? "收起摘要" : "展开摘要";
      });
      main.append(more);
    }
  } else {
    main.append(el("div", { className: "art-sum none", textContent: "（还没有中文摘要）" }));
  }
  card.append(check, main);
  return card;
}

function syncChecks() {
  for (const c of document.querySelectorAll(".art-pick")) c.checked = browse.picked.has(c.dataset.url);
}

function updateSelectBar() {
  const n = browse.picked.size;
  const show = n > 0 && state.tab === "browse" && !browse.legacy;
  $("select-bar").hidden = !show;
  document.body.classList.toggle("has-bar", show);
  let inList = 0;
  for (const u of browse.picked) if (browse.visible.has(u)) inList += 1;
  $("select-count").textContent = `已选 ${n} 篇`;
  $("select-sub").textContent = browse.onlyPicked ? "正在只看已选的" : `当前列表里 ${inList} 篇`;
  $("select-sub").classList.toggle("warn-text", !browse.onlyPicked && inList < n);
  $("select-only").textContent = browse.onlyPicked ? "看全部" : "只看已选的";
  const head = $("picked-head-text");
  if (head) head.textContent = `已选的 ${n} 篇（取消勾选就不翻）`;
  $("select-go").textContent = `翻译选中的 ${n} 篇`;
  $("select-go").disabled = browse.busy;
}

function fmtNum(n) {
  return typeof n === "number" ? n.toLocaleString("zh-CN") : "未知";
}

// 简单的确认框：返回 true / false。showOk=false 时只有「知道了」。
function askConfirm(title, lines, okLabel, showOk, okDisabled) {
  return new Promise((resolve) => {
    $("modal-title").textContent = title;
    const body = $("modal-body");
    body.replaceChildren();
    for (const l of lines) {
      if (l.list) {
        const ul = el("ul", { className: "modal-list" });
        for (const t of l.list) ul.append(el("li", { textContent: t }));
        body.append(ul);
      } else {
        body.append(el("p", { className: l.bad ? "bad-text" : l.soft ? "soft-text" : "", textContent: l.text }));
      }
    }
    $("modal-ok").hidden = !showOk;
    $("modal-ok").disabled = Boolean(okDisabled);
    $("modal-ok").textContent = okLabel;
    $("modal-cancel").textContent = showOk ? "取消" : "知道了";
    $("modal").hidden = false;
    (showOk ? $("modal-ok") : $("modal-cancel")).focus({ preventScroll: true });
    document.querySelector(".modal-box").scrollTop = 0;
    const done = (v) => {
      $("modal").hidden = true;
      $("modal-ok").onclick = $("modal-cancel").onclick = $("modal").onclick = null;
      document.removeEventListener("keydown", onKey);
      resolve(v);
    };
    const onKey = (ev) => { if (ev.key === "Escape") done(false); };
    document.addEventListener("keydown", onKey);
    $("modal-ok").onclick = () => done(true);
    $("modal-cancel").onclick = () => done(false);
    $("modal").onclick = (ev) => { if (ev.target === $("modal")) done(false); };
  });
}

// 翻选中文章的全文：先 dry_run 让用户确认篇数 / 字符 / 额度，再正式开始并跳到任务日志。
async function translateSelected(urls) {
  if (browse.busy || !urls.length) return;
  browse.busy = true;
  updateSelectBar();
  $("meta-translate").disabled = true;
  try {
    const d = await api("/api/translate", { urls, dry_run: true });
    const n = d.count || 0;
    const already = d.already || 0;
    if (!n) {
      await askConfirm("不用翻译", [{ text: `选中的 ${urls.length} 篇都已经翻过全文了。` }], "", false);
      return;
    }
    // chars_base：基础估算；chars：最坏上限。没有 chars_base 的老后端按 chars_upper_bound 判断。
    const hasBase = typeof d.chars_base === "number";
    const charsText = hasBase
      ? `约 ${fmtNum(d.chars_base)} 字符（最多 ${fmtNum(d.chars)}）`
      : d.chars_upper_bound === true
        ? `最多约 ${fmtNum(d.chars)} 字符（已翻过的段落不重复收费，实际会更少）`
        : `约 ${fmtNum(d.chars)} 字符`;
    const lines = [{ text: `将翻 ${n} 篇${already ? `（另有 ${already} 篇已翻过，会跳过）` : ""}，${charsText}。` }];
    // 要翻的标题（已翻全文的会被后端跳过，这里也不列）
    const titles = urls.map((u) => browse.byUrl.get(u)).filter((a) => a && !a.translated).map((a) => a.title || a.title_en || a.url);
    if (titles.length) {
      const shownTitles = titles.slice(0, 10);
      if (titles.length > 10) shownTitles.push(`…共 ${n} 篇`);
      lines.push({ list: shownTitles });
    }
    const hasQuota = typeof d.quota_left === "number";
    if (d.backend) {
      lines.push({ text: `翻译服务：${BACKENDS[d.backend] || d.backend}；剩余额度：${hasQuota ? `${fmtNum(d.quota_left)} 字符` : "未知"}。` });
    } else {
      lines.push({ text: "还没配置翻译服务，开始后会失败。请先到「设置」里填好翻译服务的 key。", bad: true });
    }
    // 红字只给「基础估算就超额度」；只有上限超额度时用温和提示
    const base = hasBase ? d.chars_base : d.chars_upper_bound === true ? null : d.chars;
    const BAD = "注意：预计字符数超过了剩余额度，可能翻不完；额度不够会自动停下，已翻的会保存。";
    const SOFT = "最多的情况会超过剩余额度。这是保守估计，实际通常更少；额度不够会自动停下，已翻的会保存。";
    if (hasQuota && typeof base === "number" && base > d.quota_left) {
      lines.push({ text: BAD, bad: true });
    } else if (hasQuota && typeof d.chars === "number" && d.chars > d.quota_left) {
      lines.push({ text: SOFT, soft: true });
    }
    if (!(await askConfirm("确认翻译全文", lines, `开始翻译 ${n} 篇`, true, !d.backend))) return;
    const r = await api("/api/translate", { urls, dry_run: false });
    for (const u of urls) browse.picked.delete(u);
    syncChecks();
    updateSelectBar();
    toast(`已开始翻译 ${r.count ?? n} 篇，进度看下面的日志。`);
    const j = await api("/api/job?since=0");
    if (j.job) beginJob(j.job);
    showTab("run");
  } catch (e) {
    toast(e.status === 409 ? "已有翻译或运行任务在跑，等它完成再点。勾选都还留着。" : e.message, true);
  } finally {
    browse.busy = false;
    updateSelectBar();
    $("meta-translate").disabled = false;
  }
}

function markActive(url) {
  for (const a of document.querySelectorAll(".toc [data-url]")) {
    if (a.classList.contains("art-pick")) continue;
    a.classList.toggle("active", a.dataset.url === url);
  }
}

async function openArticle(url, view) {
  state.url = url;
  state.view = view || state.view;
  markActive(url);
  for (const b of document.querySelectorAll(".views button")) b.classList.toggle("active", b.dataset.view === state.view);
  const body = $("reader-body");
  // 手机上列表和阅读区轮流显示：记住列表滚到哪，返回时回到原位
  if (!$("tab-browse").classList.contains("reading")) browse.scrollY = window.scrollY;
  $("tab-browse").classList.add("reading");
  try {
    const d = await api(`/api/article?url=${encodeURIComponent(url)}&view=${state.view}`);
    $("reader-meta").hidden = false;
    const src = $("meta-source");
    if (d.source_url) { src.href = d.source_url; src.hidden = false; } else src.hidden = true;
    $("meta-section").textContent = `栏目 ${d.section || "未记录"}`;
    $("meta-translated").textContent = `翻译时间 ${fmtTime(d.translated_at)}`;
    const st = $("meta-state");
    const known = typeof d.translated === "boolean";
    st.hidden = !known;
    if (known) {
      st.textContent = d.translated ? "已翻全文" : "只有摘要";
      st.className = `tag ${d.translated ? "full" : "brief"}`;
    }
    $("meta-translate").hidden = !(known && !d.translated) || browse.legacy;
    // 只有摘要的文章：没有翻译时间，也没有中英对照可看
    const brief = known && !d.translated;
    // 只有摘要的文章没有中英对照，停在 bi 视图时切回中文
    if (brief && state.view !== "zh") return openArticle(url, "zh");
    $("meta-translated").hidden = brief;
    document.querySelector(".views").hidden = brief;
    const a = browse.byUrl.get(url);
    const note = a && (a.flags || []).find(isPdfNoise);
    $("meta-note").hidden = !note;
    $("meta-note").textContent = note ? `提示：${note}（PDF 转出来的正文，公式和表格请对照原文）` : "";
    body.innerHTML = d.html; // 服务端已渲染并做过安全处理（render.py）
    body.parentElement.scrollTop = 0;
    window.scrollTo(0, 0);
  } catch (e) {
    body.replaceChildren(el("p", { className: "empty", textContent: e.message }));
  }
}

function closeReader() {
  $("tab-browse").classList.remove("reading");
  window.scrollTo(0, browse.scrollY);
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
      if (state.tab === "browse") loadToc().catch(() => {});
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
let translatingSection = false;
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
    if (translatingSection) return;
    translatingSection = true;
    const btn = head.querySelector(".sec-translate");
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "翻译中…";
    try {
      const d = await api("/api/sections/translate", { site, section: sec.id });
      renderSections(d);
      const failed = d.failed || [];
      if (failed.length) {
        toast(`已翻译 ${d.translated || 0} 条；${failed.length} 条没翻成功（已记录，不会重复计费）。`, true);
      } else {
        toast("已翻译这一栏的标题和摘要（命中的不重复计费）。");
      }
    } catch (e) {
      toast(e.message, true);
      // 失败（409/429/502/400/网络错误）后按钮还留在 DOM 里，得恢复，否则会一直停在「翻译中…」。
      // 成功路径里 renderSections 重建列表、旧按钮已被摘掉，isConnected 为 false，这里不碰它。
      if (btn.isConnected) {
        btn.disabled = false;
        btn.textContent = label;
      }
    } finally { translatingSection = false; }
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
  browse.picked.clear();
  browse.open.clear();
  closeReader();
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
  let searchTimer = null;
  $("toc-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      browse.query = $("toc-search").value;
      if (browse.data && !browse.legacy) renderToc();
    }, 200);
  });
  $("select-go").addEventListener("click", () => translateSelected([...browse.picked]));
  $("select-clear").addEventListener("click", () => {
    browse.picked.clear();
    if (browse.onlyPicked) { browse.onlyPicked = false; renderToc(); }
    syncChecks();
    updateSelectBar();
  });
  $("select-only").addEventListener("click", () => {
    browse.onlyPicked = !browse.onlyPicked;
    closeReader();
    renderToc();
    window.scrollTo(0, 0);
    $("toc").scrollTop = 0;
  });
  $("reader-back").addEventListener("click", closeReader);
  $("meta-translate").addEventListener("click", () => { if (state.url) translateSelected([state.url]); });
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
