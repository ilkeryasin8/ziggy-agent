"use strict";
// Ziggy panel. Every text goes through text nodes (h(), MD), never innerHTML: ticket
// text, plans and agent output are untrusted.

// The panel was called bmad-pipeline: its saved choices lived under the old key names. Copy each
// one to its new name once (unless the new one is already set), then drop the old key.
(() => {
  const rename = (key) => key.startsWith("bmad-pipeline-choice:") ? `ziggy.choice:${key.slice(21)}`
    : key === "bmad-lang" ? "ziggy.ticketlang" : key.startsWith("bmad.") ? `ziggy.${key.slice(5)}` : null;
  for (const area of [localStorage, sessionStorage]) {
    try {
      for (const key of Object.keys(area)) {
        const next = rename(key);
        if (!next) continue;
        if (area.getItem(next) === null) area.setItem(next, area.getItem(key));
        area.removeItem(key);
      }
    } catch { /* storage unavailable: nothing to carry over */ }
  }
})();

const $ = (id) => document.getElementById(id);
const EN = I18N.lang !== "tr";
const LOCALE = EN ? "en-GB" : "tr-TR";
const pctText = (n) => (EN ? `${n}%` : `%${n}`);  // "18%" / "%18"
const STATUS_TR = {
  "new": t("yeni"), "queued": t("kuyrukta"), "running": t("çalışıyor"), "needs-user": t("soru var"), "awaiting-approval": t("onay bekliyor"),
  "done": t("bitti"), "failed": t("başarısız"), "error": t("hata"), "waiting": t("bekliyor"), "split": t("dilimlere bölündü"), "quota": t("kota bekliyor"), "budget": t("bütçe bekliyor"),
  "host": t("bellek bekliyor"),
  // v1 tickets keep their old status; shown as is
  "planning": t("planlanıyor"), "building": t("yazılıyor"), "ready-for-dev": t("spec hazır"), "blocked": t("takıldı"), "draft": t("taslak"),
  "in-progress": t("yazılıyor"), "in-review": t("review'da"),
};
const TYPE_TR = { feat: t("özellik"), fix: t("hata"), refactor: "refactor", chore: t("bakım"), docs: "docs", project: "PROJECT.md" };
let snapshot = null;
let lastOkAt = 0;
let drawer = null; // {target, id, tab, phase, follow, all, offset, timer, detail, tx, files}

// --- small helpers ------------------------------------------------------------------------------

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;  // CSSOM, which the CSP allows (an inline style attribute it would not)
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const ICONS = {
  activity: "M22 12h-4l-3 9L9 3l-3 9H2",
  spec: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z|M14 2v6h6|M16 13H8|M16 17H8|M10 9H8",
  ticket: "M22 12h-6l-2 3h-4l-2-3H2|M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z",
  layers: "M12 2 2 7l10 5 10-5-10-5z|M2 17l10 5 10-5|M2 12l10 5 10-5",
  terminal: "M4 17l6-6-6-6|M12 19h8",
  branch: "M6 3v12|M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6z|M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6z|M18 9a9 9 0 0 1-9 9",
  pr: "M18 15a3 3 0 1 0 0 6 3 3 0 0 0 0-6z|M6 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6z|M13 6h3a2 2 0 0 1 2 2v7|M6 9v12",
  play: "M6 4l13 8-13 8V4z",
  stop: "M7 7h10v10H7z",
  retry: "M21 12a9 9 0 1 1-3-6.7L21 8|M21 3v5h-5",
  message: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
  check: "M20 6 9 17l-5-5",
  alert: "M12 9v4|M12 17h.01|M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z",
  x: "M18 6 6 18|M6 6l12 12",
  plus: "M12 5v14|M5 12h14",
  external: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6|M15 3h6v6|M10 14 21 3",
  clock: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z|M12 6v6l4 2",
  dollar: "M12 1v22|M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6",
  box: "M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z",
  shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  user: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2|M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
  zap: "M13 2 3 14h9l-1 8 10-12h-9l1-8z",
  bell: "M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9|M13.73 21a2 2 0 0 1-3.46 0",
  hammer: "M15 12l-8.5 8.5a2.12 2.12 0 0 1-3-3L12 9|M17.64 15 22 10.64|M20.91 11.7 18.3 9.1a2 2 0 0 1-.6-1.42V6.3L13.7 2.3a1 1 0 0 0-1.4 0L11 3.6l4 4v1.6a2 2 0 0 0 .58 1.41l2.6 2.6",
  pin: "M12 22s-8-6.5-8-13a8 8 0 0 1 16 0c0 6.5-8 13-8 13z|M12 11a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
  chevron: "M6 9l6 6 6-6",
  eye: "M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z|M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  upload: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4|M17 8l-5-5-5 5|M12 3v12",
  folder: "M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z",
  diff: "M12 3v14|M5 10h14|M5 21h14",
};

function icon(name, cls) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon" + (cls ? " " + cls : ""));
  svg.setAttribute("aria-hidden", "true");
  for (const d of (ICONS[name] || "").split("|")) {
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return svg;
}

// A count with its noun: t("{0} oturum") in English holds both forms, "{0} session|{0} sessions" (Turkish has one).
function plural(text, n, ...args) {
  const forms = String(text).split("|");
  return (n !== 1 && forms[1] !== undefined ? forms[1] : forms[0]).replace(/\{(\d+)\}/g, (_, i) => (i === "0" ? n : args[i - 1] ?? ""));
}

function ago(iso) {
  if (!iso) return "-";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return t("az önce");
  if (s < 3600) return t("{0} dk önce", Math.floor(s / 60));
  if (s < 86400) return t("{0} sa önce", Math.floor(s / 3600));
  return t("{0} gün önce", Math.floor(s / 86400));
}

function duration(sec) {
  sec = Math.max(0, sec);
  const days = Math.floor(sec / 86400), hrs = Math.floor((sec % 86400) / 3600), min = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  if (days) return t("{0} g {1} sa", days, hrs);
  if (hrs) return t("{0} sa {1} dk", hrs, String(min).padStart(2, "0"));
  if (min) return t("{0} dk {1} sn", min, String(s).padStart(2, "0"));
  return t("{0} sn", s);
}

const elapsed = (iso) => (iso ? duration((Date.now() - new Date(iso).getTime()) / 1000) : "");
// A span the 1 s clock keeps current, so a card does not have to be rebuilt to tick.
const since = (iso, cls) => h("span", { class: "since " + (cls || ""), "data-since": iso }, elapsed(iso));
const money = (v) => (typeof v === "number" ? (v > 0 && v < 0.005 ? "<$0.01" : `$${v.toFixed(2)}`) : "-");
const when = (iso) => (iso ? new Date(iso).toLocaleString(LOCALE, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "-");
const badge = (status) => h("span", { class: `badge ${status}` }, h("span", { class: "badge-dot" }), STATUS_TR[status] || status);
const chip = (iconName, text, cls) => h("span", { class: "meta" + (cls ? " " + cls : ""), title: typeof text === "string" ? text : null },
  icon(iconName), h("span", { class: "meta-text" }, text));
const link = (href, ...kids) => h("a", { href, target: "_blank", rel: "noopener noreferrer" }, ...kids);
const empty = (text, iconName) => h("div", { class: "empty" }, icon(iconName || "check"), h("span", {}, text));
const mdNode = (text) => h("div", { class: "md" }, MD.blocks(text || ""));

function toast(text, bad) {
  const tk = $("toast");
  tk.replaceChildren(icon(bad ? "alert" : "check"), h("span", {}, text));
  tk.className = "toast" + (bad ? " bad" : "");
  tk.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { tk.hidden = true; }, bad ? 9000 : 4000);
}

function confirmBox(text) {
  return new Promise((resolve) => {
    $("modal-text").textContent = text;
    $("modal").hidden = false;
    $("modal-yes").focus();
    const done = (v) => { $("modal").hidden = true; $("modal-yes").onclick = $("modal-no").onclick = null; resolve(v); };
    $("modal-yes").onclick = () => done(true);
    $("modal-no").onclick = () => done(false);
  });
}

async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

const store = {
  get(area, key) { try { return area.getItem(key) || ""; } catch { return ""; } },
  set(area, key, value) { try { if (value) area.setItem(key, value); else area.removeItem(key); } catch { /* this view only */ } },
};

// What review.js, sessions.js and chat.js get from the panel.
const ctx = {
  api, h, toast,
  navigate: (hash) => { location.hash = hash; },
  md: mdNode,
  confirm: confirmBox,
  publish: (body) => withGuard(body),  // a publish request, with the guard's findings asked about (app.js withGuard)
  // Switch the open ticket to a tab; "chat" brings the chat beside the tabs into view (chat.js: "Sor" seeds it first).
  openTab: (name) => { if (drawer) switchTab(name); },
  // Open a ticket's Aşamalar tab on one stage session (review.js: commit and evidence links).
  openStage: (target, id, stageDir) => {
    const row = rowOf(target, id);
    if (row) openDrawer(row, "stages", { highlight: stageDir });
  },
};

// --- actions ------------------------------------------------------------------------------------

const ACTION_CONFIRM = {
  run: (tk) => tk.status === "host"
    ? t("{0} şimdi başlatılsın mı?\nDikkat: makine bellekte zorlanıyor ({1}). Şimdi başlarsa komutları çok yavaş koşabilir ve zaman aşımına uğrayabilir.", tk.id, snapshot?.host?.text || t("bellek dolu"))
    : tk.status === "quota"
    ? t("{0} şimdi başlatılsın mı?\nDikkat: {1} kullanım sınırında; {2} sıfırlanıyor. Şimdi başlarsa oturum hemen yine reddedilebilir.", tk.id, tk.quota?.label || t("Claude hesabı"), clockAt(tk.quota?.resume_at))
    : tk.status === "budget"
    ? t("{0} şimdi başlatılsın mı?\nDikkat: {1} bugünkü bütçesini ({2}) doldurdu; şimdi başlarsa sınırın üstünde harcar.", tk.id, tk.target, money(tk.budget?.cap))
    : tk.status === "failed"
    ? t("{0} kaldığı aşamadan ({1}) yeniden koşsun mu?\nAşama baştan, yeni bir oturumla çalışır; birkaç dolar tutabilir.", tk.id, tk.stage || "?")
    : t("{0} başlatılsın mı?\n{1} hattı aşama aşama koşar; birkaç dolar ve 10–30 dakika tutabilir.", tk.id, tk.pipeline || pipelineFor(tk.type)),
  resume: (tk) => t("{0}: yarıda kalan oturum kaldığı yerden sürdürülsün mü?\nAynı Claude oturumu devam eder (claude --resume); bitmiş adımlar tekrarlanmaz.", tk.id),
  stop: (tk) => tk.queued_at ? t("{0} kuyruktan çıkarılsın mı?\nHenüz başlamadı; ticket olduğu yerde kalır.", tk.id)
    : t("{0} durdurulsun mu?\nYarım kalan değişiklikler stash'e alınır, ticket 'başarısız' olur. Sonra 'Tekrar' ile aşamayı yeniden koşturabilir ya da yön değiştiren bir not yazabilirsin.", tk.id),
  approve: (tk) => t("{0}: plan onaylansın ve koşu devam etsin mi?\n{1} hemen başlar.", tk.id, tk.gate?.stage ? t("{0} aşamasından sonraki adım", tk.gate.stage) : t("Sıradaki aşama")),
  publish: (tk) => tk.pull ? t("{0}: düzeltme commit'leri PR #{1}'in kendi dalına ({2}) push edilsin mi?\n{3}", tk.id, tk.pull.number, tk.pull.head, tk.pr)
      + (tk.pull_foreign ? t("\n\nDikkat: bu PR {0} kullanıcısının; commit'ler onun dalına gider.", tk.pull.author) : "")
    : tk.pr ? t("{0}: {1} dalındaki yeni commit'ler push edilip PR güncellensin mi?\n{2}", tk.id, tk.branch, tk.pr)
    : t("{0}: {1} dalı {2} reposuna push edilip draft PR açılsın mı?\nReady yapmak ve merge sende kalır.", tk.id, tk.branch, tk.repo || "GitHub")
      + (tk.stack_base ? t("\n\nYığının dilimi: PR'ın tabanı {0}. Alttaki dilimlerin PR'ı yoksa önce onlar (draft olarak) açılır.", tk.base_branch) : "")
      + protectedWarning(tk),
  protected_apply: (tk) => t("{0}: korumalı dosya değişikliği ({1}) {2} dalına uygulanıp commit edilsin mi?\n", tk.id, (tk.needs_you?.patch?.files || []).join(", "), tk.branch)
    + (tk.needs_you?.patch?.replaces?.length
      ? t("Bu yama {0}: dosyanın içeriği bu yamanın hazırlandığı hâline (önceki uygulanan değişiklik olmadan) bu yama eklenerek yazılır.", replacesText(tk.needs_you.patch))
      : t("Yama dalın şimdiki HEAD'inde yeniden denenir; temiz uygulanmazsa hiçbir şey değişmez."))
    + (tk.pr ? t("\n\nPR açık: commit sonrası 'PR'ı güncelle' ile gönderirsin.") : ""),
  review: (tk) => t("{0} dalı Codex'e review ettirilsin mi?\n{1}'e göre fark incelenir (1–3 dk, ChatGPT hesabından). Dala dokunulmaz.", tk.id, tk.base_branch),
  split: (tk) => t("{0}: plan {1} dilime bölünsün mü?\nHer dilim kendi ticket'ı, dalı ve draft PR'ı olur; her dal bir alttakinin üstüne yığılır. ", tk.id, tk.split_offer?.length || "?")
    + t("İlk dilim hemen başlar (onay verilmiş sayılır), diğerleri bir önceki bitince sırayla. Bu ticket dilimlerin kabı olur, kendisi kod yazmaz."),
  restack: (tk) => t("{0}: {1} dalı {2} üstüne yeniden düzenlensin mi (git rebase --onto)?\n", tk.id, tk.branch, tk.restack?.onto || t("yeni tabanı"))
    + t("Çakışma olursa dal olduğu gibi kalır. Sonuç gösterilir; PR'a push ayrıca senin onayınla yapılır."),
  restack_push: (tk) => t("{0}: yeniden düzenlenen {1} dalı PR'ına force-with-lease ile push edilsin mi?\n{2}\n", tk.id, tk.branch, tk.pr || "")
    + t("Yalnızca bu ticket'ın kendi dalı; son push'tan beri dala başkası push ettiyse push reddedilir."),
};
const ACTION_DONE = {
  resume: t("oturum kaldığı yerden sürüyor"), review: t("Codex review başladı"), fix: t("bulgular plana eklendi, düzeltme koşusu başladı"),
  dismiss: t("kapatıldı; aynı olay bir daha gösterilmez"),
  post_review: t("review PR'a yorum olarak eklendi"), answer: t("cevap kaydedildi, koşu devam ediyor"), approve: t("onaylandı, koşu devam ediyor"),
  run: t("başladı"), publish: t("push ve PR başladı"), cleanup: t("worktree temizleniyor; dal kalıyor"),
  split: t("dilimlere bölündü; ilk dilim başladı"), restack: t("yeniden düzenleniyor"), restack_push: t("yeniden düzenlenen dal push ediliyor"),
  protected_apply: t("korumalı dosya değişikliği uygulandı ve commit edildi"), protected_reject: t("korumalı dosya değişikliği reddedildi"),
};
// Publishing never waits for a protected file change; the confirm (and the review page's PR form) only says one waits.
const protectedWarning = (tk) => (tk.protected_pending?.length
  ? t("\n\nOnay bekleyen korumalı dosya değişikliği var ({0}); PR onsuz açılır.", [...new Set(tk.protected_pending.flat())].join(", ")) : "");
const NUDGE_DONE = {
  interrupt: t("aşama durduruluyor; kendi oturumunda mesajınla devam edecek"),
  queued: t("mesaj kaydedildi; aşama başlayınca önce onu okuyacak"),
};

const pipelineFor = (type) => ({ feat: "feat", fix: "fix", project: "project" }[type] || "chore");

// A publish guard.py stopped (files that run in CI or steer agents, a credential, invisible characters): show what
// it found and, if you agree, send the same request again approving exactly those findings.
const GUARD_MARKER = "publish needs approval:";
async function withGuard(body) {
  try {
    return await api("/api/action", body);
  } catch (e) {
    if (!String(e.message).startsWith(GUARD_MARKER)) throw e;
    const shown = e.message.slice(GUARD_MARKER.length).trim();
    if (!(await confirmBox(t("Yayından önce bakman gereken bulgular var:\n\n{0}\n\nDiff'e baktıysan ve bunlar beklenen değişikliklerse onaylayıp yayınla.", shown)))) {
      throw new Error(t("yayın iptal edildi"));
    }
    return api("/api/action", { ...body, approve_findings: true });
  }
}

async function act(tk, action, text, extra) {
  if (ACTION_CONFIRM[action] && !(await confirmBox(ACTION_CONFIRM[action]({ ...tk, repo: targetRepo(tk.target) })))) return false;
  try {
    if (action === "publish" && tk.pull_foreign) text = "foreign-ok";  // the confirm above named the owner
    const body = { target: tk.target, id: tk.id, action, text, ...(extra || {}) };
    const out = action === "publish" ? await withGuard(body) : await api("/api/action", body);
    if (out.job?.queued) { toast(t("{0}: kuyruğa alındı; projedeki koşu slotları dolu, sırası gelince başlar", tk.id)); refresh(); return true; }
    const label = action === "stop" ? (tk.queued_at ? t("kuyruktan çıkarıldı") : t("durdurma istendi"))
      : action === "nudge" ? NUDGE_DONE[out.nudged?.delivery] || out.nudged?.message : ACTION_DONE[action];
    toast(`${tk.id}: ${label || action + t(" başladı")}`);
    refresh();
    return true;
  } catch (e) {
    toast(`${tk.id}: ${e.message}`, true);
    return false;
  }
}

const targetRepo = (name) => (snapshot?.targets.find((x) => x.name === name) || {}).github_repo;
const rowOf = (target, id) => snapshot?.tickets.find((x) => x.target === target && x.id === id);

// A waiting ticket's line: what it waits for ("Bekliyor: local-3 bitince").
const waitText = (tk) => (tk.waiting_for?.length ? t("Bekliyor: {0} bitince", tk.waiting_for.map((d) => d.id).join(", ")) : t("Bekliyor"));

// What a running row says: waiting in line for a run slot, waiting out a retry backoff, or the stage under way.
function runLabel(tk) {
  if (tk.queued_at) return t("kuyrukta · {0} beri", ago(tk.queued_at));
  const stage = tk.stage || tk.phase || tk.last_job?.command || t("hazırlanıyor");
  if (tk.retry_at) return t("{0} · {1} sonra yeniden denenecek", stage, duration(Math.max(0, (new Date(tk.retry_at).getTime() - Date.now()) / 1000)));
  return `${stage}${tk.visit > 1 ? ` #${tk.visit}` : ""}${tk.model ? " · " + tk.model : ""}`;
}

function actionButtons(tk, compact) {
  const cls = compact ? "small" : "";
  const b = (iconName, label, action, extra) =>
    h("button", { class: `${cls} ${extra || ""}`, onclick: (e) => { e.stopPropagation(); act(tk, action); } }, icon(iconName), label);
  const open = (iconName, label, tab, extra) =>
    h("button", { class: `${cls} ${extra || ""}`, onclick: (e) => { e.stopPropagation(); openDrawer(tk, tab); } }, icon(iconName), label);
  if (tk.running && tk.last_job?.command?.startsWith("review")) return [h("button", { class: cls, disabled: true }, icon("eye"), t("Codex inceliyor…"))];
  if (tk.running && tk.last_job?.state === "running" && tk.last_job.command.startsWith("cleanup")) return [h("button", { class: cls, disabled: true }, icon("x"), t("Temizleniyor…"))];
  if (tk.running) {
    return [
      tk.legacy ? "" : h("button", { class: cls, title: t("Ajana mesaj gönder: çalışmayı bırakmadan yön verir"),
        onclick: (e) => { e.stopPropagation(); nudgeDialog(tk); } }, icon("message"), t("Yönlendir")),
      b("stop", tk.queued_at ? t("Kuyruktan çıkar") : t("Durdur"), "stop", "danger"),
    ];
  }
  if (tk.archived || tk.status === "error") return [];  // worktree removed: nothing to run, review or push
  const out = [];
  if (tk.legacy) {  // made by the BMad version: publish, review and clean up still work, runs do not
    if (tk.status === "done" && !tk.pr) out.push(b("pr", t("Draft PR aç"), "publish", "primary"));
    if (tk.status === "done" && tk.needs_you?.kind === "push" && !tk.pull?.fork) out.push(b("upload", t("PR'ı güncelle"), "publish", "primary"));
    return out;
  }
  if (tk.status === "new") out.push(b("play", t("Başlat"), "run"));
  if (["quota", "host", "budget"].includes(tk.status)) {  // held for the login's limit, the machine's memory or the day's budget: it goes on by itself; this starts it now (with a warning)
    out.push(h("button", { class: cls, onclick: (e) => { e.stopPropagation(); act(tk, "run", "", { now: true }); } }, icon("play"), t("Şimdi başlat")));
  }
  if (tk.status === "needs-user") out.push(open("message", t("Cevapla"), "questions", "primary"));
  if (tk.status === "awaiting-approval") {
    out.push(b("check", t("Onayla"), "approve", "primary"));
    if (tk.split_offer) out.push(b("layers", t("Dilimlere böl"), "split"));
    out.push(open("spec", "Plan", "plan"));
  }
  if (tk.restack) {
    out.push(tk.restack.rebased ? b("upload", t("Düzenlenen dalı push et"), "restack_push", "primary") : b("retry", t("Yeniden düzenle"), "restack", "primary"));
  }
  if (tk.status === "failed") {
    if (tk.resumable) out.push(b("play", t("Devam"), "resume", "primary"));
    if (tk.recheckable) out.push(b("check", t("Yalnız check'ler"), "recheck"));
    out.push(b("retry", t("Tekrar"), "run"));
  }
  if (tk.status === "done" && !tk.pr) out.push(b("pr", t("Draft PR aç"), "publish", "primary"));
  if (tk.status === "done" && tk.needs_you?.kind === "push" && !tk.pull?.fork) out.push(b("upload", tk.pull ? t("PR'a gönder") : t("PR'ı güncelle"), "publish", "primary"));
  if (tk.drafts) out.push(h("a", { class: `button ${cls}`, href: ReviewPage.hash(tk.target, tk.id), onclick: (e) => e.stopPropagation() }, icon("diff"), t("Yorumlar ({0})", tk.drafts)));
  if (tk.status === "done" && !["MERGED", "CLOSED"].includes(tk.pr_state)) out.push(...reviewStep(tk, cls));
  if (!compact && tk.worktree && ["done", "failed"].includes(tk.status)) {
    out.push(h("button", { class: "ghost", title: t("Worktree'yi sil (önce teardown); dal kalır"),
      onclick: (e) => { e.stopPropagation(); cleanupDialog(tk); } }, icon("x"), t("Temizle")));
  }
  return out;
}

// What the latest Codex review leaves to do: nothing but the PR when it is clean and still covers
// the branch, the findings when it has some, and a new review when there is none or the branch moved on.
function reviewStep(tk, cls) {
  const r = tk.review;
  const again = (label) => h("button", { class: cls, onclick: (e) => { e.stopPropagation(); act(tk, "review"); } }, icon("eye"), label);
  if (!r || r.state !== "ok") return [again("Codex review")];
  if (r.stale) return [again(t("Yeniden review"))];
  if (r.findings && !tk.legacy) return [h("button", { class: `${cls}`, onclick: (e) => { e.stopPropagation(); openDrawer(tk, "review"); } }, icon("hammer"), t("Bulguları düzelt"))];
  if (tk.pr && tk.needs_you?.kind !== "push") return [h("a", { class: `button ${cls}`, href: tk.pr, target: "_blank", rel: "noopener noreferrer", onclick: (e) => e.stopPropagation() }, icon("pr"), t("PR'ı aç"), icon("external"))];
  return [];
}

// --- pipeline bar -------------------------------------------------------------------------------
// One step per stage of the ticket's pipeline, from the engine's stages_view. The running step
// takes the ticket's state (running or queued); while the ticket waits for a question's answer or
// an approval, the stage that stopped (st.stage) shows as waiting.

const STEP_TR = { done: t("bitti"), active: t("çalışıyor"), waiting: t("seni bekliyor"), queued: t("kuyrukta"), failed: t("başarısız"), stopped: t("durdu"), pending: t("başlamadı"), skipped: t("atlandı") };

function stepState(tk, s) {
  if (s.state === "current") return tk.queued_at || ["quota", "host"].includes(tk.status) ? "queued" : tk.running ? "active" : "stopped";
  if (s.id === tk.stage && ["needs-user", "awaiting-approval"].includes(tk.status)) return "waiting";
  return s.state;
}

function pipelineBar(tk, compact) {
  const stages = tk.stages || [];
  if (!stages.length || tk.status === "split") return "";  // a split ticket runs nothing more; its slices do
  const states = stages.map((s) => stepState(tk, s));
  const label = t("Hat: {0}", stages.map((s, i) => `${t(s.title || s.id)} ${STEP_TR[states[i]] || states[i]}${s.visits > 1 ? ` ×${s.visits}` : ""}`).join(", "));
  return h("ol", { class: "pbar" + (compact ? " compact" : ""), role: "img", "aria-label": label, title: label },
    stages.map((s, i) => h("li", { class: `pstep pstep-${states[i]}` },
      h("span", { class: "pstep-bar" }),
      compact ? "" : h("span", { class: "pstep-name" }, t(s.title || s.id), s.visits > 1 ? ` ×${s.visits}` : ""))));
}

const typeTag = (tk) => h("span", { class: "type" + (tk.type === "project" ? " project" : "") }, tk.type || "");
const bmadTag = (tk) => tk.pipeline === "bmad" ? h("span", { class: "tag", title: t("BMad build-auto planlar ve kurar; kontroller, review ve PR bu pipeline'ın") }, t("BMad hattı")) : "";

// BMad in a project (snapshot targets[].bmad): installed on the base branch, and this machine's per-developer files.
function bmadText(b) {
  if (!b?.installed) return t("BMad yok");
  return b.missing_local?.length ? t("BMad kurulu · yerel dosya eksik") : t("BMad kurulu{0}", b.version ? ` (${b.version})` : "");
}
function bmadPill(tk) {
  const b = tk.bmad;
  if (!b) return "";
  const cls = !b.installed ? "mute" : b.missing_local?.length ? "warn" : "ok";
  const title = !b.installed ? t(b.reason || "") || t("Taban dalda BMad yok")
    : b.missing_local?.length ? t("Ana checkout'ta eksik (BMad kurulumunu orada çalıştır):\n{0}", b.missing_local.join("\n")) : t(b.reason || "");
  return h("span", { class: `pill ${cls}`, title }, bmadText(b));
}
const bmadReady = (b) => !!(b?.installed && !b.missing_local?.length);

// Controlled projects (mode = "controlled"): every plan waits for approval, dependants ask before they start, no PR replies.
const CONTROLLED_TITLE = t("Kontrollü mod: her ticket plan onayında durur, bağımlı ticket'lar sormadan başlamaz, PR yorumlarına yanıt yazılmaz");
const controlledPill = (x) => x.mode === "controlled" ? h("span", { class: "pill info", title: CONTROLLED_TITLE }, t("Kontrollü")) : "";
const controlledTag = (x) => x.mode === "controlled" ? h("span", { class: "tag", title: CONTROLLED_TITLE }, t("Kontrollü")) : "";

// A time as the cards say it ("15:05", "17.10 09:00" when not today) and with the Turkish locative ("15:05'te"),
// read by its last word: 05 "beş" → 'te, 30 "otuz" → 'da, 15:00 "on beş" → 'te.
function clockText(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "?";
  const time = d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? time : `${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")} ${time}`;
}
const LOC_ONES = ["", "de", "de", "te", "te", "te", "da", "de", "de", "da"];
const LOC_TENS = { 0: "da", 10: "da", 20: "de", 30: "da", 40: "ta", 50: "de" };
function clockAt(iso) {
  const text = clockText(iso);
  if (EN) return text;  // English has no locative suffix
  const [hh, mm] = text.split(" ").pop().split(":").map(Number);
  const n = mm || hh || 0;
  return `${text}'${LOC_ONES[n % 10] || LOC_TENS[n - (n % 10)] || "de"}`;
}
// A ticket held for its Claude login's usage limit (status quota): rejected mid-stage it continues its session, held
// before a stage it starts it.
const quotaText = (tk) => (tk.quota?.reason === "rejected" ? t("Kota doldu · {0} devam", clockAt(tk.quota?.resume_at)) : t("Kota doldu · {0} başlar", clockAt(tk.quota?.resume_at)));
const quotaDetail = (tk) => t("{0} kullanım sınırına ulaştı. ", tk.quota?.label || t("Claude hesabı"))
  + (tk.quota?.reason === "rejected" ? t("{0} oturumu {1} kaldığı yerden devam eder.", tk.quota?.stage || t("Aşama"), clockAt(tk.quota?.resume_at))
    : t("{0} {1} başlar.", tk.quota?.stage || t("Sıradaki aşama"), clockAt(tk.quota?.resume_at)));
// A ticket held while the machine is short of memory (status host): it starts after two calm panel ticks.
const HOST_TEXT = t("Makine bellekte zorlanıyor · bekliyor");
const hostDetail = (tk) => t("{0} başlamadı: {1}. ", tk.host?.stage || t("Sıradaki aşama"), snapshot?.host?.text || t("bellek dolu"))
  + t("Makine iki dakika üst üste rahat olunca kendiliğinden başlar (her dakika bir ticket).");

// --- health bar ---------------------------------------------------------------------------------
// One line: "Her şey yolunda" or "N sorun", the quiet facts inline, problems as full chips below.

function healthItems(s) {
  const items = [];
  const connOld = Date.now() - lastOkAt > 45000;
  items.push({ key: "panel", tone: connOld ? "bad" : "ok", compact: t("güncellendi {0}", new Date(lastOkAt).toLocaleTimeString(LOCALE)),
    title: t("Panel"), text: connOld ? t("45 sn'dir güncellenmiyor") : t("çalışıyor · {0}", duration(s.uptime_s)) });
  items.push({ key: "claude", tone: s.claude_cli ? "ok" : "bad", compact: s.claude_cli ? null : t("claude CLI yok"),
    title: "Claude CLI", text: s.claude_cli ? t("bulundu") : t("bulunamadı: koşular başlayamaz (ZIGGY_CLAUDE ya da PATH)") });
  for (const tk of s.targets) {
    if (tk.error) { items.push({ key: `t-${tk.name}`, tone: "bad", compact: t("{0}: config hatası", tk.name), title: tk.name, text: t(tk.error) }); continue; }
    const used = (tk.active || []).length;
    items.push({ key: `t-${tk.name}`, tone: "ok", compact: null, title: tk.name, text: t("{0}/{1} koşu slotu dolu", used, tk.max_active) });
    (tk.warnings || []).forEach((w, i) => items.push({ key: `w-${tk.name}-${i}`, tone: "warn", compact: null, title: `${tk.name} config`, text: t(w) }));
  }
  const sync = s.pr_sync || {};
  items.push({ key: "sync", tone: sync.error ? "warn" : sync.last ? "ok" : "muted", compact: sync.error ? t("PR takibi hata veriyor") : null,
    title: t("PR takibi"), text: t(sync.error || "") || (sync.last ? t("son kontrol {0}", ago(sync.last)) : t("henüz kontrol edilmedi")) });
  // One chip per Claude login: its windows ("5 sa %18 · 7 gün %11"), warn from 80 %, bad while it is over its limit.
  for (const q of s.quota || []) {
    items.push({ key: `q-${q.key}`, tone: q.tone, compact: null, title: q.label,
      text: [q.blocked ? t("Kota doldu · {0} sıfırlanır", clockAt(q.blocked_until)) : "", quotaWindows(q) || t("henüz kota bilgisi yok")].filter(Boolean).join(" · ") });
  }
  // The machine's memory: a chip only while pressure is warn/critical or swap is 75 % full (bad while it holds stages).
  const m = s.host;
  if (m) {
    items.push({ key: "host", tone: m.show ? m.tone : "ok", compact: m.show ? t(m.text) : null, title: t("Makine"),
      text: [t(m.text), m.load1 != null ? t("yük {0}", m.load1.toFixed(1)) : "", m.busy ? t("yeni aşamalar bekliyor") : m.enabled === false ? t("bekletme kapalı") : ""].filter(Boolean).join(" · ") });
  }
  return items;
}

const quotaWindows = (q) => q.windows.map((w) => `${t(w.label)} ${pctText(w.percent)}`).join(" · ");

// The sidebar's quota: per Claude login, each window's utilization in its colour, and when a reached limit resets.
function renderQuota(s) {
  const rows = s.quota || [];
  const sig = JSON.stringify(rows.map((q) => [q.key, q.label, q.tone, q.windows, q.blocked_until]));
  const box = $("side-quota");
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  $("side-quota-label").hidden = !rows.length;
  box.replaceChildren(...rows.map((q) => h("div", { class: `quota-row ${q.tone}`, title: `${q.label} · ${q.targets.join(", ")}` },
    h("span", { class: "quota-name" }, q.label),
    h("span", { class: "quota-wins" }, q.blocked ? h("span", { class: "quota-win bad" }, t("Kota doldu · {0}", clockText(q.blocked_until))) : "",
      ...(q.windows.length ? q.windows.map((w) => h("span", { class: `quota-win ${w.tone}`, title: w.resets_at ? t("{0} sıfırlanır", clockAt(w.resets_at)) : "" }, `${t(w.label)} ${pctText(w.percent)}`))
        : [h("span", { class: "quota-win muted" }, t("henüz bilgi yok"))])))));
}

const isProblem = (i) => i.tone === "bad" || i.tone === "warn";

function renderHealth(s) {
  const items = healthItems(s);
  const open = store.get(localStorage, "ziggy.system.open") === "1";
  const sig = JSON.stringify([items.map((i) => [i.key, i.tone, i.compact, i.text]), open]);
  const box = $("health");
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  const problems = items.filter(isProblem);
  const tone = problems.some((i) => i.tone === "bad") ? "bad" : "warn";
  const chipOf = (i) => h("div", { class: `hchip ${i.tone}` }, h("span", { class: `dot ${i.tone}` }), h("strong", {}, i.title), h("span", { class: "muted" }, i.text));
  box.replaceChildren(
    h("div", { class: "health-row" },
      problems.length ? h("span", { class: `health-sum ${tone}` }, h("span", { class: `dot ${tone}` }), plural(t("{0} sorun"), problems.length))
        : h("span", { class: "health-sum ok" }, h("span", { class: "dot ok" }), t("Her şey yolunda")),
      ...items.filter((i) => i.compact && !isProblem(i)).map((i) => h("span", { class: "sys-item" }, h("span", { class: `dot ${i.tone}` }), i.compact)),
      h("span", { class: "grow" }),
      h("button", { class: "small ghost", onclick: () => { store.set(localStorage, "ziggy.system.open", open ? "0" : "1"); renderHealth(snapshot); } }, open ? t("Ayrıntıları gizle") : t("Ayrıntılar"))),
    (problems.length || open) ? h("div", { class: "health-chips" }, (open ? items : problems).map(chipOf)) : "");
}

// --- board ----------------------------------------------------------------------------------------
// One board for the home page and #needs: what waits on you, branches ready for review, agents at
// work and PRs on their way. The server groups and sorts (attention.board_from); each card leads
// with the next step (data-primary), and j/k/Enter walk the cards.

const NEED_TITLE = {
  question: t("Ajanın soruları var"), approval: t("Plan onayını bekliyor"), publish: t("Bitti, PR açılmadı"),
  failed: t("Koşu başarısız oldu"), "job-error": t("Komut hata verdi"), error: t("Ticket state'i okunamıyor"),
  push: t("Dal PR'dan sonra değişti"), "merged-kept": t("PR kapandı ama worktree'de iş kaldı"),
  quiet: t("Oturum bir süredir sessiz"), stall: t("Komut uzun sürüyor"), lost: t("Koşunun süreci ölmüş"), memory: t("Hafıza"),
  blocked: t("Beklediği ticket takıldı"), restack: t("Yeniden düzenleme gerekiyor"), start: t("Başlatılmayı bekliyor"), quota: t("Kota doldu"),
  lead: t("İş önerileri"), protected: t("Korumalı dosya değişikliği"),
};
const NEED_ICON = { "merged-kept": "folder", quiet: "clock", stall: "clock", lost: "alert", question: "message", approval: "shield", publish: "pr", failed: "alert", "job-error": "terminal", error: "alert", push: "upload", "pr-feedback": "pr", memory: "spec",
  blocked: "clock", restack: "branch", start: "play", quota: "clock", lead: "zap", protected: "shield" };
const RISK_PILL = { low: "ok", medium: "warn", high: "bad" };
// Triage at a glance: the plan's risk when it is not low, and a large size. Nothing for a small, low-risk ticket.
function riskChip(tk) {
  const parts = [tk.risk && tk.risk !== "low" ? t("risk {0}", tk.risk) : "", tk.size === "large" ? t("büyük") : ""].filter(Boolean);
  if (!parts.length) return "";
  return h("span", { class: `pill ${RISK_PILL[tk.risk] || "mute"}`, title: t("plan: risk {0} · boyut {1}", tk.risk || "?", tk.size || "?") }, parts.join(" · "));
}
const BOARD_GROUPS = [
  ["waiting_on_you", t("Seni bekliyor"), t("sorular, onaylar, hatalar, PR yorumları")],
  ["ready_for_review", t("Review'a hazır"), t("PR'ı açılmamış biten dallar")],
  ["working", t("Çalışıyor"), t("şu an koşan ajanlar")],
  ["waiting_on_others", t("Sırada"), t("başka ticket'ı bekleyenler, dilimlere bölünenler")],
  ["landing", t("Teslimde"), t("açık PR'lar")],
];

// The card's next step: the button Enter presses (or the answer box it focuses).
const primary = (el) => { el.setAttribute("data-primary", ""); return el; };

function renderBoard(s) {
  const board = s.board || {};
  const waiting = (board.waiting_on_you || []).length;
  const urgent = (board.waiting_on_you || []).some((e) => e.severity === "high");
  $("needs-count").textContent = waiting ? String(waiting) : "";
  $("needs-count").classList.toggle("bad", urgent);
  document.title = s.unread ? `(${s.unread}) Ziggy` : "Ziggy";
  // The heading counts what the filter shows; the sidebar count stays the whole machine's.
  const scope = homeTarget();
  const mine = (board.waiting_on_you || []).filter((e) => inHome(e.target)).length;
  const working = (board.working || []).filter((e) => inHome(e.target)).length;
  const sig = JSON.stringify([scope, mine, working]);
  if ($("komuta-title").dataset.sig !== sig) {
    $("komuta-title").dataset.sig = sig;
    const where = scope ? `${scope}: ` : "";
    const [lead, tail] = t("Sende {0} iş var", "\0").split("\0");
    $("komuta-title").replaceChildren(...(mine ? [where + lead, h("span", { class: "volt-num" }, String(mine)), tail]
      : [where + (working ? t("Ajanlar çalışıyor; sende iş yok") : t("Sende bekleyen iş yok"))]));
  }
  renderSlots(s);
  renderQuota(s);
  renderHomeFilter(s);
  boardInto($("home-board"), s);
}

// The sidebar's run slots: per project, used / max (a used slot is a run under way), and the queue.
function renderSlots(s) {
  const live = s.targets.filter((tk) => !tk.error);
  const queued = s.tickets.filter((tk) => tk.running && tk.queued_at).length;
  const sig = JSON.stringify([live.map((tk) => [tk.name, (tk.active || []).length, tk.max_active]), queued]);
  const box = $("side-slots");
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.replaceChildren(...live.map((tk) => {
    const used = (tk.active || []).length;
    return h("div", { class: "slot-row" + (used ? " busy" : "") }, h("span", { class: "slot-name", title: tk.name }, tk.name), h("span", { class: "slot-n" }, `${used}/${tk.max_active}`));
  }), queued ? h("div", { class: "slot-row queued" }, h("span", {}, t("kuyrukta")), h("span", { class: "slot-n" }, String(queued))) : "");
}

function boardInto(box, s) {
  const groups = BOARD_GROUPS.map(([key, title, hint]) => ({ key, title, hint,
    items: ((s.board || {})[key] || []).filter((e) => inHome(e.target)).map((e) => ({ e, t: rowOf(e.target, e.id) })).filter((x) => x.t) }));
  // Re-render only on change, and never under a cursor: an answer may be half typed.
  const sig = JSON.stringify([s.targets.length > 1, homeTarget(), groups.map((g) => [g.key, g.items.map(({ e, t: tk }) => [e, tk.status, tk.running, tk.resumable, tk.needs_you,
    tk.stage, tk.phase, tk.model, tk.visit, tk.activity, tk.last_text, tk.cost_usd, tk.queued_at, tk.retry_at, tk.stages, tk.pr, tk.pr_state, tk.drafts, tk.dismissed, tk.review, tk.run_started_at,
    tk.waiting_for, tk.children, tk.restack, tk.split_offer, tk.quota, tk.mode, tk.recheckable, tk.risk, tk.size])])]);
  if (sig === box.dataset.sig || (box.contains(document.activeElement) && document.activeElement.tagName === "TEXTAREA")) return;
  box.dataset.sig = sig;
  const focused = box.contains(document.activeElement) ? document.activeElement.closest("[data-board-card]")?.dataset.key : null;
  if (!groups.some((g) => g.items.length)) {
    const free = s.targets.filter((tk) => !tk.error && inHome(tk.name)).reduce((n, tk) => n + tk.max_active - (tk.active || []).length, 0);
    box.replaceChildren(h("div", { class: "idle" }, icon("check"),
      h("span", { class: "muted" }, homeTarget() ? t("{0} projesinde seni bekleyen bir şey yok, çalışan ajan da yok. {1} slot boş: yukarıdaki komut çubuğundan bir iş ver ya da listeden başlat.", homeTarget(), free)
        : t("Seni bekleyen bir şey yok, çalışan ajan da yok. {0} slot boş: yukarıdaki komut çubuğundan bir iş ver ya da listeden başlat.", free))));
    placeNext(box);
    return;
  }
  // "Seni bekliyor" needs no heading: the page title counts it. Its first card is the hero (the next move).
  box.replaceChildren(...groups.filter((g) => g.items.length).map((g) => h("section", { class: `board-group ${g.key}` },
    g.key === "waiting_on_you" ? "" : h("h2", { class: "board-group-h" }, g.key === "working" ? h("span", { class: "dot live" }) : "", g.key === "working" ? t("Canlı") : g.title,
      h("span", { class: "group-n" }, String(g.items.length)), h("span", { class: "muted small" }, g.hint)),
    h("div", { class: g.key === "waiting_on_you" ? "att-grid" : g.key === "working" ? "lane-grid" : "live-grid" },
      g.items.map(({ e, t: tk }, i) => boardCard(g.key, e, tk, s, i))))));
  placeNext(box);
  if (focused) box.querySelector(`[data-board-card][data-key="${CSS.escape(focused)}"] .live-title`)?.focus();
}

function boardCard(group, e, tk, s, index) {
  const card = group === "waiting_on_you" && tk.needs_you ? needCard(tk)
    : group === "working" ? liveCard(tk, s)
    : group === "ready_for_review" ? reviewCard(tk)
    : group === "waiting_on_others" ? (tk.status === "split" ? splitCard(tk) : tk.status === "quota" ? quotaCard(tk) : tk.status === "host" ? hostCard(tk) : tk.status === "budget" ? budgetCard(tk) : waitingCard(tk))
    : tk.status === "split" ? splitCard(tk) : landingCard(tk);
  card.setAttribute("data-board-card", "");
  card.dataset.key = `${tk.target}/${tk.id}`;
  if (e.unread) card.classList.add("unread");
  if (group === "waiting_on_you" && index === 0) card.classList.add("hero");
  return card;
}

// The card's title: the j/k target, which opens the drawer.
function cardTitle(tk) {
  return h("button", { class: "link-btn live-title", title: tk.title, onclick: () => openDrawer(tk) }, tk.title);
}

function cardHead(tk, extra) {
  return h("div", { class: "run-head" },
    tk.needs_you && !tk.needs_you.seen_at ? h("span", { class: "dot unread-dot", title: t("okunmadı") }) : "",
    h("span", { class: "id" }, tk.id), typeTag(tk), bmadTag(tk), controlledTag(tk), riskChip(tk),
    snapshot?.targets.length > 1 ? h("span", { class: "muted small" }, tk.target) : "",
    h("span", { class: "grow" }), extra || "");
}

function needCard(tk) {
  const n = tk.needs_you;
  const head = h("div", { class: "need-head" },
    h("div", { class: "need-icon" }, icon(NEED_ICON[n.kind] || "bell")),
    h("div", { class: "grow" },
      h("div", { class: "need-kind" }, !n.seen_at ? h("span", { class: "dot unread-dot", title: t("okunmadı") }) : "", n.kind === "stall" ? t(n.title) : NEED_TITLE[n.kind] || t(n.title),
        n.kind === "quiet" && n.minutes ? t(" · {0} dk", n.minutes) : ""),
      h("div", { class: "need-title" }, h("span", { class: "id" }, tk.id), " ", cardTitle(tk), n.kind === "approval" ? "" : riskChip(tk)),  // the gate card shows its own
      h("div", { class: "muted small" }, tk.target, tk.stage ? ` · ${tk.stage}` : "", n.created_at ? ` · ${ago(n.created_at)}` : "")));
  const body = [head];
  const controls = [];
  if (n.kind === "question" && n.round) {
    const form = answerForm(tk, n.round);
    const first = form.querySelector("textarea");
    if (first) primary(first);
    body.push(form);
  } else if (n.kind === "approval") {
    body.push(approvalBox(tk, n.gate || tk.gate || {}));
  } else if (n.kind === "protected" && n.patch) {
    body.push(protectedBox(n.patch));
    controls.push(...protectedButtons(tk, n.patch));
  } else {
    if (n.details) body.push(h("pre", { class: "need-details" }, tLines(n.details)));
    if (["failed", "lost"].includes(n.kind) && !tk.legacy) {
      if (n.kind === "lost") body.push(h("div", { class: "muted small" }, t("Süreç sonuç yazmadan bitmiş (öldürülmüş ya da çökmüş).")));
      if (tk.resumable) controls.push(primary(h("button", { class: "primary", onclick: () => act(tk, "resume") }, icon("play"), t("Kaldığı yerden devam"))));
      // The agent's work passed as READY and only an engine check failed (e.g. its environment, fixed since): the
      // checks alone run again, no session and no cost; a pass continues the pipeline.
      const recheck = tk.recheckable && !tk.resumable ? h("button", { class: "primary", title: t("Ajan koşmaz; motor check'leri dalın şu anki hali üzerinde yeniden koşar, geçerse devam eder"),
        onclick: () => act(tk, "recheck") }, icon("check"), t("Yalnız check'leri tekrar koştur")) : null;
      if (recheck) controls.push(primary(recheck));
      const again = h("button", { class: tk.resumable || recheck ? "" : "primary", onclick: () => act(tk, "run") }, icon("retry"), t("Aşamayı tekrar koştur"));
      controls.push(tk.resumable || recheck ? again : primary(again));
      if (n.kind === "failed") controls.push(h("button", { onclick: () => openDrawer(tk, "questions") }, icon("message"), t("Yön değiştir")));
    } else if (["quiet", "stall"].includes(n.kind)) {
      body.push(h("div", { class: "muted small" }, n.kind === "stall" ? t("Ziggy komutu durdurmaz. Makine yavaşsa beklemek yeter; takıldıysa ajana yön ver ya da durdur.")
        : t("Uzun bir test koşusu da sessiz görünür; son satırlara bak. Takıldıysa ajana yön ver ya da durdur.")));
      controls.push(primary(h("button", { class: "primary", onclick: () => nudgeDialog(tk) }, icon("message"), t("Yönlendir"))));
      controls.push(h("button", { class: "danger", onclick: () => act(tk, "stop") }, icon("stop"), t("Durdur")));
    } else if (n.kind === "pr-feedback") {
      // The title counts them ("PR'da 2 yeni yorum", "PR'da CI kırmızı"); the review page lists them to pick.
      controls.push(primary(h("a", { class: "button primary", href: ReviewPage.hash(tk.target, tk.id, null, null, "github") }, icon("diff"), t("Geri bildirime bak"))));
      if (tk.pr) controls.push(h("a", { class: "button", href: tk.pr, target: "_blank", rel: "noopener noreferrer" }, icon("pr"), t("PR'ı aç"), icon("external")));
    } else if (n.kind === "blocked") {
      for (const d of n.waiting_for || []) {
        const dep = rowOf(tk.target, d.id);
        if (dep) controls.push(h("button", { class: controls.length ? "" : "primary", onclick: () => openDrawer(dep) }, icon("ticket"), t("{0} aç", d.id)));
      }
      if (controls.length) primary(controls[0]);
    } else if (n.kind === "start") {
      // A controlled project's dependant: its dependencies are done, and it starts only when you say so.
      controls.push(primary(h("button", { class: "primary", onclick: () => act(tk, "run") }, icon("play"), t("Başlat"))));
    } else if (n.kind === "restack") {
      const r = tk.restack || n.restack || {};
      if (r.result?.ok && r.result.commits?.length) {
        body.push(h("div", { class: "muted small" }, t("Yeniden düzenlendi ({0} → {1}); PR hâlâ eski commit'leri gösteriyor:", (r.result.from || "").slice(0, 7), (r.result.to || "").slice(0, 7))),
          h("pre", { class: "need-details" }, r.result.commits.join("\n")));
      }
      controls.push(primary(r.rebased
        ? h("button", { class: "primary", onclick: () => act(tk, "restack_push") }, icon("upload"), t("Push et (force-with-lease)"))
        : h("button", { class: "primary", onclick: () => act(tk, "restack") }, icon("retry"), t("Yeniden düzenle"))));
      if (r.rebased) controls.push(h("a", { class: "button", href: ReviewPage.hash(tk.target, tk.id) }, icon("diff"), t("Değişiklikler")));
    }
  }
  const details = h("button", { class: "ghost", onclick: () => openDrawer(tk) }, icon("activity"), t("Ayrıntı"));
  controls.push(controls.length || ["question", "approval"].includes(n.kind) ? details : primary(details));
  if (n.dismissable) controls.push(dismissButton(tk, n, ""));
  return h("div", { class: `need ${n.kind} sev-${n.severity}` + (["question", "approval", "protected"].includes(n.kind) ? " wide" : "") }, body, h("div", { class: "row" }, controls));
}

// A protected file's patch (AGENTS.md, …): the agents may not write these files, so the stage left a diff; it lands on
// the branch only when you apply it (the wrapper checks it on the current HEAD, applies and commits it).
function protectedBox(p) {
  const lines = (p.diff || "").replace(/\n$/, "").split("\n").map((line) => {
    const cls = /^(diff --git|index |--- |\+\+\+ |new file|deleted file|similarity|rename )/.test(line) ? "pd-meta"
      : line.startsWith("@@") ? "pd-hunk" : line.startsWith("+") ? "pd-add" : line.startsWith("-") ? "pd-del" : "";
    return h("span", { class: `pd-line ${cls}` }, line || " ");
  });
  return h("div", { class: "protected-box" },
    p.summary ? h("div", { class: "small" }, p.summary) : "",
    h("div", { class: "muted small" }, t("Ajanlar bu dosyalara yazamaz; {0} değişikliği yama olarak bıraktı (#{1}). ", p.stage || t("aşama"), p.n)
      + t("Uygularsan dala commit olarak eklenir.") + (p.replaces?.length ? t(" Bu yama {0}.", replacesText(p)) : "")),
    h("pre", { class: "patch-diff", "aria-label": t("Yama #{0}", p.n) }, lines));
}

// "önceki #1'in yerini alır": the applied patches of the same files this one was prepared without (protected.replaces).
// The genitive suffix follows how the last number is read (1 bir'in, 2 iki'nin, 3 üç'ün, 10 on'un, 40 kırk'ın, …).
function replacesText(p) {
  const ns = p.replaces || [];
  const last = ns[ns.length - 1];
  const ones = ["", "'in", "'nin", "'ün", "'ün", "'in", "'nın", "'nin", "'in", "'un"];  // i18n-ok: Turkish grammar, used only in Turkish
  const tens = ["", "'un", "'nin", "'un", "'ın", "'nin", "'ın", "'in", "'in", "'ın"];  // i18n-ok: Turkish grammar
  const suffix = EN ? "" : last % 10 ? ones[last % 10] : Math.floor(last / 10) % 10 ? tens[Math.floor(last / 10) % 10]
    : Math.floor(last / 100) % 10 ? "'ün" : "'in";  // i18n-ok: Turkish grammar
  const list = ns.length > 1 ? t("{0} ve #{1}", ns.slice(0, -1).map((n) => `#${n}`).join(", "), last) : `#${last}`;
  return t("önceki {0}{1} yerini alır", list, suffix);
}

function protectedButtons(tk, p) {
  const reject = async () => {
    const r = await askBox({ title: t("{0}: korumalı dosya değişikliği reddedilsin mi?", tk.id), text: t("{0} değişmeden kalır. Neden (isteğe bağlı):", p.files.join(", ")),
      placeholder: t("ör. AGENTS.md'yi ben güncelleyeceğim"), max: 1000, yes: t("Reddet"), optional: true });
    if (r) act(tk, "protected_reject", r.text, { n: p.n });
  };
  return [primary(h("button", { class: "primary", onclick: () => act(tk, "protected_apply", "", { n: p.n }) }, icon("check"), t("Uygula"))),
    h("button", { class: "danger", onclick: reject }, icon("x"), t("Reddet"))];
}

// The last few activity lines per running ticket, kept across polls (the snapshot carries only the latest).
const laneLog = new Map();

function laneLines(tk) {
  const key = `${tk.target}/${tk.id}`;
  const lines = laneLog.get(key) || [];
  const line = tk.activity ? shortLine(tk.activity, tk.worktree) : "";
  if (line && lines[lines.length - 1] !== line) {
    lines.push(line);
    if (lines.length > 4) lines.shift();
  }
  laneLog.set(key, lines);
  return lines;
}

// A running ticket as a live lane: its stages, the clock, and the agent's last moves as they happen.
function liveCard(tk, s) {
  const quiet = ["quiet", "stall"].includes(tk.needs_you?.kind);
  const started = tk.run_started_at || tk.phase_started_at || tk.queued_at;
  const watch = primary(h("button", { class: "small", onclick: () => openDrawer(tk, tk.round ? "questions" : "activity") }, icon(tk.round ? "message" : "activity"), tk.round ? t("Cevapla") : t("İzle")));
  const lines = laneLines(tk);
  return h("div", { class: "lane" + (quiet ? " quiet" : tk.queued_at ? " queued" : "") },
    h("div", { class: "lane-top" },
      cardHead(tk, [tk.queued_at ? h("span", { class: "pill mute" }, t("kuyrukta")) : "", h("span", { class: `dot ${tk.queued_at ? "" : quiet ? "warn" : "live"}` }),
        started ? since(started, "lane-clock nowrap") : ""]),
      cardTitle(tk),
      pipelineBar(tk, false),
      h("div", { class: "run-phase" }, runLabel(tk))),
    h("div", { class: "lane-log" },
      lines.length ? lines.map((l, i) => h("div", { class: "lane-line" + (i === lines.length - 1 ? " now" : ""), title: l }, l))
        : h("div", { class: "lane-line muted" }, tk.queued_at ? t("Projenin bütün koşu slotları dolu; önündekiler bitince kendiliğinden başlar.") : t("hazırlanıyor…"))),
    tk.last_text ? h("div", { class: "live-text", title: tk.last_text }, tk.last_text) : "",
    quiet ? h("div", { class: "warn-text small lane-warn" }, tk.needs_you.kind === "stall" ? tk.needs_you.title : t("{0} dakikadır olay yok", tk.needs_you.minutes ?? "?")) : "",
    h("div", { class: "lane-foot" },
      h("span", { class: "lane-cost" }, tk.cost_usd ? money(tk.cost_usd) : t("henüz maliyet yok")),
      h("span", { class: "grow" }),
      watch,
      ...actionButtons(tk, true)));
}

function reviewCard(tk) {
  const n = tk.needs_you || {};
  const controls = [];
  if (n.kind === "push" && !tk.pull?.fork) {
    controls.push(primary(h("button", { class: "small primary", onclick: () => act(tk, "publish") }, icon("upload"), tk.pull ? t("PR'a gönder") : t("PR'ı güncelle"))));
    controls.push(h("a", { class: "button small", href: ReviewPage.hash(tk.target, tk.id) }, icon("diff"), t("Değişiklikler")));
  } else {
    controls.push(primary(h("a", { class: "button small primary", href: ReviewPage.hash(tk.target, tk.id) }, icon("diff"), t("Değişiklikleri incele"))));
    if (tk.status === "done" && !tk.pr) controls.push(h("button", { class: "small", onclick: () => act(tk, "publish") }, icon("pr"), t("Draft PR aç")));
  }
  controls.push(h("button", { class: "small ghost", onclick: () => openDrawer(tk) }, t("Ayrıntı")));
  if (n.dismissable) controls.push(dismissButton(tk, n, "small"));
  return h("div", { class: `live-card review-ready ${n.kind || ""}`, title: n.details || "" },
    cardHead(tk, h("span", { class: "muted small nowrap" }, ago(n.created_at || tk.updated_at))),
    cardTitle(tk),
    h("div", { class: "muted small row tight" }, NEED_TITLE[n.kind] || t("Bitti"), tk.drafts ? h("span", { class: "pill info" }, plural(t("{0} taslak yorum"), tk.drafts)) : "",
      tk.dismissed ? h("a", { class: "pill-btn warn", href: ReviewPage.hash(tk.target, tk.id, null, null, "dismissed"),
        title: t("AI review'un \"boşver\" dediği bulgular; aç, istediğini düzelttir") }, plural(t("Review {0} bulguyu eledi"), tk.dismissed)) : "",
      reviewPill(tk)),
    h("div", { class: "row tight wrap" }, controls));
}

function landingCard(tk) {
  const open = tk.pr ? primary(h("a", { class: "button small primary", href: tk.pr, target: "_blank", rel: "noopener noreferrer" }, icon("pr"), t("PR'ı aç"), icon("external")))
    : primary(h("button", { class: "small", onclick: () => openDrawer(tk) }, t("Ayrıntı")));
  return h("div", { class: "live-card landing" },
    cardHead(tk, h("span", { class: "muted small nowrap" }, ago(tk.updated_at))),
    cardTitle(tk),
    h("div", { class: "row tight wrap" }, tk.pr ? h("span", { class: `pr-pill ${(tk.pr_state || "open").toLowerCase()}` }, icon("pr"), "#" + tk.pr.split("/").pop()) : "", reviewPill(tk)),
    h("div", { class: "row tight wrap" }, open, h("button", { class: "small ghost", onclick: () => openDrawer(tk) }, t("Ayrıntı"))));
}

// A ticket that waits for other tickets: what it waits for, each with its status; it starts on its own (a controlled
// project asks first).
function waitingCard(tk) {
  return h("div", { class: "live-card queued waiting" },
    cardHead(tk, [h("span", { class: "pill warn" }, t("bekliyor")), h("span", { class: "muted small nowrap" }, ago(tk.updated_at))]),
    cardTitle(tk),
    h("div", { class: "run-phase" }, waitText(tk)),
    h("div", { class: "row tight wrap" }, (tk.waiting_for || []).map((d) => {
      const dep = rowOf(tk.target, d.id);
      return h("button", { class: "pill-btn " + (d.state === "blocked" ? "bad" : ""), title: d.title || "", onclick: () => dep && openDrawer(dep) },
        d.id, " · ", STATUS_TR[d.status] || d.status || t("yok"));
    })),
    h("div", { class: "muted small" }, tk.mode === "controlled"
      ? t("{0} bitince başlatmak için sorulur (kontrollü proje).", tk.slice ? t("Dilim {0}: bir alttaki dilim", tk.slice) : t("Beklediği ticket'lar"))
      : tk.slice ? t("Dilim {0}: bir alttaki dilim bitince kendiliğinden başlar.", tk.slice) : t("Beklediği ticket'lar bitince kendiliğinden başlar.")),
    h("div", { class: "row tight wrap" }, primary(h("button", { class: "small ghost", onclick: () => openDrawer(tk) }, t("Ayrıntı")))));
}

// A ticket held for its Claude login's usage limit: when it goes on by itself; "Şimdi başlat" starts it anyway.
function quotaCard(tk) {
  return h("div", { class: "live-card queued waiting" },
    cardHead(tk, [h("span", { class: "pill warn" }, t("kota")), h("span", { class: "muted small nowrap" }, ago(tk.updated_at))]),
    cardTitle(tk),
    pipelineBar(tk, false),
    h("div", { class: "run-phase" }, quotaText(tk)),
    h("div", { class: "muted small" }, quotaDetail(tk)),
    h("div", { class: "row tight wrap" }, primary(h("button", { class: "small ghost", onclick: () => openDrawer(tk) }, t("Ayrıntı"))),
      h("button", { class: "small", onclick: () => act(tk, "run", "", { now: true }) }, icon("play"), t("Şimdi başlat"))));
}

// A ticket held for its target's daily spending cap (daily_budget_usd): it starts tomorrow; "Şimdi başlat" goes on now.
function budgetCard(tk) {
  const b = tk.budget || {};
  return h("div", { class: "live-card queued waiting" },
    cardHead(tk, [h("span", { class: "pill warn" }, t("bütçe")), h("span", { class: "muted small nowrap" }, ago(tk.updated_at))]),
    cardTitle(tk),
    pipelineBar(tk, false),
    h("div", { class: "run-phase" }, t("Günlük bütçe doldu · {0} devam eder", clockAt(b.resume_at))),
    h("div", { class: "muted small" }, t("{0} bugün {1} harcadı; sınır {2} (daily_budget_usd).", tk.target, money(b.spent), money(b.cap))),
    h("div", { class: "row tight wrap" }, primary(h("button", { class: "small ghost", onclick: () => openDrawer(tk) }, t("Ayrıntı"))),
      h("button", { class: "small", onclick: () => act(tk, "run", "", { now: true }) }, icon("play"), t("Şimdi başlat"))));
}

// A ticket held while the machine is short of memory: it starts once the machine is calm; "Şimdi başlat" starts it anyway.
function hostCard(tk) {
  return h("div", { class: "live-card queued waiting" },
    cardHead(tk, [h("span", { class: "pill warn" }, t("bellek")), h("span", { class: "muted small nowrap" }, ago(tk.updated_at))]),
    cardTitle(tk),
    pipelineBar(tk, false),
    h("div", { class: "run-phase" }, HOST_TEXT),
    h("div", { class: "muted small" }, hostDetail(tk)),
    h("div", { class: "row tight wrap" }, primary(h("button", { class: "small ghost", onclick: () => openDrawer(tk) }, t("Ayrıntı"))),
      h("button", { class: "small", onclick: () => act(tk, "run", "", { now: true }) }, icon("play"), t("Şimdi başlat"))));
}

// A split ticket: a container for its stacked slices, each with its status and PR.
function splitCard(tk) {
  const kids = tk.children || [];
  const done = kids.filter((c) => c.status === "done").length;
  return h("div", { class: "live-card split" },
    cardHead(tk, [h("span", { class: "pill info" }, plural(t("{0} dilim"), kids.length)), h("span", { class: "muted small nowrap" }, ago(tk.updated_at))]),
    cardTitle(tk),
    h("div", { class: "muted small" }, t("Dilimlere bölündü · {0}/{1} bitti · her dilim bir öncekinin dalına yığılı", done, kids.length)),
    sliceList(tk, kids),
    h("div", { class: "row tight wrap" }, primary(h("button", { class: "small ghost", onclick: () => openDrawer(tk) }, t("Ayrıntı")))));
}

// The slices of a stack, lowest first: slice id, ticket, status (waiting: what for), PR link.
function sliceList(tk, kids) {
  return h("ol", { class: "slice-list" }, kids.map((c) => {
    const row = rowOf(tk.target, c.id);
    const status = c.running ? "running" : c.status;
    return h("li", { class: "slice" + (c.id === tk.id ? " current" : "") },
      h("span", { class: "slice-id" }, c.slice || "?"),
      h("button", { class: "link-btn slice-title", title: c.title || "", onclick: () => row && openDrawer(row) }, h("span", { class: "id" }, c.id), " ", (c.title || "").replace(/^.* — S\d+: /, "")),
      badge(status),
      c.status === "waiting" && c.waiting_for?.length ? h("span", { class: "muted small" }, t("{0} bitince", c.waiting_for.join(", "))) : "",
      c.restack ? h("span", { class: "pill warn" }, t("yeniden düzenle")) : "",
      c.pr ? link(c.pr, h("span", { class: `pr-pill ${(c.pr_state || "open").toLowerCase()}` }, icon("pr"), "#" + c.pr.split("/").pop())) : h("span", { class: "muted small" }, t("PR yok")));
  }));
}

// Closes this occurrence only (a new failure or a new run shows again); questions, approvals and
// quiet sessions need an action, so they have no close.
function dismissButton(tk, n, cls) {
  return h("button", { class: `${cls} ghost`, title: t("Bu kaydı kapat; aynı olay bir daha gösterilmez"),
    onclick: (e) => { e.stopPropagation(); act(tk, "dismiss", n.key); } }, icon("x"), t("Kapat"));
}

// j/k move between board cards, Enter takes the focused card's next step.
document.addEventListener("keydown", (e) => {
  const key = e.key?.length === 1 ? e.key.toLowerCase() : e.key;  // the hint says J/K: Shift or Caps Lock still moves
  if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented || !["j", "k", "Enter"].includes(key)) return;
  if (e.target.closest?.("input, textarea, select, [contenteditable='true'], dialog")) return;
  if (overlayOpen()) return;
  const box = currentView() === "home" ? $("home-board") : null;
  const titles = box ? [...box.querySelectorAll("[data-board-card] .live-title")] : [];
  if (!titles.length) return;
  const at = titles.indexOf(document.activeElement);
  if (key === "Enter") {
    if (at < 0) return;
    e.preventDefault();
    const next = titles[at].closest("[data-board-card]").querySelector("[data-primary]");
    if (next?.tagName === "TEXTAREA") next.focus();
    else next?.click();
    return;
  }
  e.preventDefault();
  const to = key === "j" ? Math.min(titles.length - 1, at + 1) : Math.max(0, at === -1 ? 0 : at - 1);
  titles[to].focus();
  titles[to].scrollIntoView({ block: "nearest" });
});

// --- inbox ----------------------------------------------------------------------------------------
// The header's unread count and list (the attention ledger); opening a ticket marks its items read.

function renderInbox(s) {
  $("inbox-count").textContent = s.unread ? String(s.unread) : "";
  $("inbox-btn").title = s.unread ? t("Gelen kutusu · {0} okunmamış", s.unread) : t("Gelen kutusu");
  if (!$("inbox-pop").hidden && s.unread !== loadInbox.unread) loadInbox();
}

async function loadInbox() {
  const pop = $("inbox-pop");
  loadInbox.unread = snapshot?.unread;
  let items;
  try {
    items = (await api("/api/attention")).items;
  } catch (e) {
    pop.replaceChildren(h("p", { class: "error-banner small" }, e.message));
    return;
  }
  const markAll = h("button", { class: "small ghost", disabled: !items.some((i) => !i.seen_at), onclick: async () => {
    try { await api("/api/attention/seen", { all: true }); } catch (e) { return toast(e.message, true); }
    await refresh();
    loadInbox();
  } }, icon("check"), t("Tümünü okundu say"));
  pop.replaceChildren(
    h("div", { class: "row" }, h("strong", { class: "grow" }, t("Gelen kutusu")), markAll),
    items.length ? h("ul", { class: "inbox-list" }, items.map((i) => h("li", {},
      h("button", { class: "inbox-item" + (i.seen_at ? "" : " unread"), onclick: () => {
        closeInbox();
        if (i.kind === "memory") { location.hash = "#memory"; return; }  // a project's memory, not a ticket
        if (i.kind === "lead") { openNext(i.target); return; }  // a project's lead proposals: Komuta's Sıradaki işler
        if (i.kind === "quota") {  // a Claude login's limit, not a ticket: the sidebar shows when it resets
          api("/api/attention/seen", { target: i.target, quota: true }).then(refresh, (e) => toast(e.message, true));
          return;
        }
        const row = rowOf(i.target, i.ticket);
        if (row) openDrawer(row);
      } },
        h("span", { class: "dot " + (i.seen_at ? "" : "unread-dot") }),
        // pr-feedback has no fixed kicker (cards show its counted title); the inbox row still names the kind.
        h("span", { class: "grow" }, h("span", { class: "small muted" }, NEED_TITLE[i.kind] || (i.kind === "pr-feedback" ? t("PR geri bildirimi") : i.kind)), h("br"),
          ["memory", "quota", "lead"].includes(i.kind) ? "" : h("span", { class: "id" }, i.ticket), ["memory", "lead"].includes(i.kind) ? h("span", { class: "id" }, i.target) : "", " ", t(i.title)),
        h("span", { class: "muted small nowrap" }, ago(i.created_at))))))
      : h("p", { class: "muted small" }, t("Yeni bir şey yok. Açtığın işler bitene kadar panoda kalır.")));
}

function closeInbox() {
  $("inbox-pop").hidden = true;
  $("inbox-btn").setAttribute("aria-expanded", "false");
}

$("inbox-icon").replaceWith(icon("bell"));
$("inbox-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  if (!$("inbox-pop").hidden) return closeInbox();
  $("inbox-pop").hidden = false;
  $("inbox-btn").setAttribute("aria-expanded", "true");
  $("inbox-pop").replaceChildren(h("p", { class: "muted small" }, t("yükleniyor…")));
  loadInbox();
});
document.addEventListener("click", (e) => { if (!$("inbox-pop").hidden && !e.target.closest(".inbox")) closeInbox(); });

function markSeen(tk) {
  if (!tk?.needs_you || tk.needs_you.seen_at) return;
  api("/api/attention/seen", { target: tk.target, ticket: tk.id }).then(() => refresh()).catch(() => { /* the next poll shows it unread again */ });
}

// --- dialogs: nudge, cleanup --------------------------------------------------------------------------

// A dialog with an optional text box and an optional checkbox; resolves {text, checked} or null. `optional`: the
// text may stay empty.
function askBox({ title, text, placeholder, max, yes, input = true, check, optional = false }) {
  return new Promise((resolve) => {
    const ta = $("ask-input");
    $("ask-title").textContent = title;
    $("ask-text").textContent = text || "";
    ta.style.display = input ? "" : "none";
    ta.value = "";
    ta.placeholder = placeholder || "";
    ta.maxLength = max || 8000;
    $("ask-check-row").style.display = check ? "" : "none";
    $("ask-check").checked = false;
    $("ask-check-label").textContent = check || "";
    $("ask-yes").textContent = yes || t("Gönder");
    $("ask-modal").hidden = false;
    (input ? ta : $("ask-yes")).focus();
    const done = (v) => { $("ask-modal").hidden = true; $("ask-form").onsubmit = $("ask-no").onclick = null; resolve(v); };
    $("ask-form").onsubmit = (e) => {
      e.preventDefault();
      if (input && !optional && !ta.value.trim()) return ta.focus();
      done({ text: ta.value.trim(), checked: $("ask-check").checked });
    };
    $("ask-no").onclick = () => done(null);
  });
}

// Steer the agent without starting over: a running stage is interrupted and resumes its own session
// with the message; one that has not started gets it in its prompt.
async function nudgeDialog(tk) {
  const live = tk.running && !tk.queued_at && !tk.retry_at;
  const stage = tk.stage || t("sıradaki aşama");
  const r = await askBox({
    title: t("Ajana mesaj gönder: çalışmayı bırakmadan yön verir"),
    text: live ? t("{0}: {1} durdurulur ve kendi oturumunda, bağlamını koruyarak önce mesajını okuyup devam eder. Süren bir araç çağrısı iptal olur.", tk.id, stage)
      : t("{0}: mesajın {1} başlayınca prompt'una eklenir.", tk.id, stage),
    placeholder: t("Ne farklı yapsın? (en çok 4000 karakter)"), max: 4000, yes: live ? t("Durdur ve yönlendir") : t("Mesajı kaydet"),
  });
  if (r) act(tk, "nudge", r.text);
}

async function cleanupDialog(tk) {
  const r = await askBox({
    title: t("{0}: worktree temizlensin mi?", tk.id),
    text: t("Önce teardown komutları çalışır, sonra worktree silinir. Dal ({0}) kalır; yeni bir koşu worktree'yi daldan yeniden kurar.", tk.branch),
    input: false, check: t("Commit edilmemiş dosyalar olsa da sil (kaybolurlar)"), yes: t("Temizle"),
  });
  if (r) act(tk, "cleanup", r.checked ? "force" : "");
}

// An intent gap: the stage found the change answers a different intent than the ticket and the plan. The
// attempt was saved as a patch and the branch reset to the commit before it, unless it was already published.
const GAP_KEPT = {
  published: t("Dal zaten yayında (PR ya da push): kod olduğu gibi duruyor, geri alınmadı; yalnızca soru açıldı."),
  "not in history": t("Denemeden önceki commit artık dalın geçmişinde değil; dal geri alınmadı."),
  "nothing built": t("Denemeden bu yana commit yok; geri alınacak bir şey yoktu."),
  stacked: t("Üstteki dilim bu dalın üstüne kuruldu: kod olduğu gibi duruyor, geri alınmadı; yalnızca soru açıldı."),
};
function intentGapBox(tk, gap) {
  const patch = `/api/attempts/${encodeURIComponent(tk.target)}/${encodeURIComponent(tk.id)}/attempt-${gap.attempt}.patch`;
  const note = gap.reset ? t("Dal {0} commit'ine geri alındı; deneme ({1} commit) yamada saklı.", (gap.reset_to || "").slice(0, 7), gap.commits)
    : GAP_KEPT[gap.kept] || t("Dal geri alınmadı.");
  return h("div", { class: "intent-gap" },
    h("div", { class: "need-kind" }, icon("alert"), t(" Niyet sapması"), h("span", { class: "muted" }, t(" · {0} buldu", gap.stage || "?"))),
    h("div", { class: "md" }, MD.blocks(gap.summary || "")),
    gap.evidence ? h("div", { class: "gap-evidence small" }, h("span", { class: "muted" }, t("Kanıt: ")), gap.evidence) : "",
    h("div", { class: "row small" },
      h("a", { class: "button small", href: patch, target: "_blank", rel: "noopener" }, icon("diff"), t("Deneme {0} yaması", gap.attempt)),
      h("span", { class: `pill ${gap.reset ? "info" : "warn"}` }, gap.reset ? t("dal geri alındı") : t("kod korundu")),
      h("span", { class: "muted grow" }, note)));
}

// The open question round: each question with its options (a click fills the answer), the
// recommendation, and an answer box per question. Drafts survive a reload (sessionStorage).
function answerForm(tk, round) {
  const key = (qid) => `ziggy.answer.${tk.target}/${tk.id}/${round.n}/${qid}`;
  const boxes = [];
  const items = (round.questions || []).map((q, i) => {
    const qid = q.id || `Q${i + 1}`;
    const ta = h("textarea", { rows: 2, class: "answer-box", placeholder: q.recommendation ? t("Önerilen: {0}", q.recommendation) : t("Cevabın"), "aria-label": t("{0} için cevap", qid),
      oninput: () => store.set(sessionStorage, key(qid), ta.value) });
    ta.value = store.get(sessionStorage, key(qid));
    boxes.push([qid, ta]);
    const fill = (text) => { ta.value = text; store.set(sessionStorage, key(qid), text); ta.focus(); };
    const options = [...new Set(q.options || [])];
    return h("li", { class: "q" },
      h("div", { class: "q-text" }, h("span", { class: "q-id" }, qid), h("div", { class: "md grow" }, MD.blocks(q.text || ""))),
      options.length ? h("div", { class: "decision-options" }, options.map((o) => h("button", { type: "button", class: "small option", onclick: () => fill(o) },
        o, o === q.recommendation ? h("span", { class: "pill ok" }, t("önerilen")) : ""))) : "",
      q.recommendation && !options.includes(q.recommendation)
        ? h("div", { class: "q-rec small" }, h("span", { class: "muted" }, t("Önerilen: ")), q.recommendation, " ", h("button", { type: "button", class: "small ghost", onclick: () => fill(q.recommendation) }, t("kullan"))) : "",
      ta);
  });
  const note = h("textarea", { rows: 2, class: "answer-box", placeholder: t("Genel not (isteğe bağlı): sorulmayan ama bilinmesi gereken"), "aria-label": t("Genel not"),
    oninput: () => store.set(sessionStorage, key("_note"), note.value) });
  note.value = store.get(sessionStorage, key("_note"));
  const send = h("button", { class: "primary", onclick: async (e) => {
    const btn = e.currentTarget;
    const parts = boxes.filter(([, ta]) => ta.value.trim()).map(([qid, ta]) => `${qid}: ${ta.value.trim()}`);
    const open = boxes.length - parts.length;
    if (note.value.trim()) parts.push(note.value.trim());
    if (!parts.length) return toast(t("Önce en az bir soruyu cevapla"), true);
    if (open && !(await confirmBox(t("{0} soru cevapsız kalacak; ajan onlar için kendi varsayımını yapar.\nGönderilsin mi?", open)))) return;
    btn.disabled = true;
    if (await act(tk, "answer", parts.join("\n\n"))) {
      for (const [qid] of boxes) store.set(sessionStorage, key(qid), "");
      store.set(sessionStorage, key("_note"), "");
    }
    btn.disabled = false;
  } }, icon("message"), t("Cevapla ve devam et"));
  return h("div", { class: "answer-form" },
    round.intent_gap ? intentGapBox(tk, round.intent_gap) : "",
    round.context && round.context !== round.intent_gap?.summary ? h("div", { class: "answer-context md" }, MD.blocks(round.context)) : "",
    h("ol", { class: "questions" }, items),
    note,
    h("div", { class: "row" }, send, h("span", { class: "muted small grow" }, t("tur {0}{1} · {2}", round.n, round.stage ? ` · ${round.stage}` : "", ago(round.asked_at)))),
    tk.auto_answer_at ? h("div", { class: "muted small" }, icon("clock"), t(" Cevap gelmezse {0} önerilenlerle devam eder.", clockAt(tk.auto_answer_at))) : "");
}

// The gate: risk, size and why it stopped; approve, or send notes that go into the plan.
function approvalBox(tk, gate) {
  const notes = h("textarea", { rows: 3, class: "answer-box", placeholder: t("Neyin değişmesini istiyorsun?"), "aria-label": t("Değişiklik notları") });
  const key = `ziggy.approval.${tk.target}/${tk.id}`;
  notes.value = store.get(sessionStorage, key);
  notes.addEventListener("input", () => store.set(sessionStorage, key, notes.value));
  const changeBox = h("div", { class: "stack", hidden: !notes.value },
    h("div", { class: "muted small" }, t("Not olarak plana eklenir, plan yeniden gözden geçirilir.")),
    notes,
    h("div", { class: "row end" }, h("button", { class: "primary", onclick: async (e) => {
      if (!notes.value.trim()) return toast(t("Önce notunu yaz"), true);
      const btn = e.currentTarget;
      btn.disabled = true;
      if (await act(tk, "answer", notes.value.trim())) store.set(sessionStorage, key, "");
      btn.disabled = false;
    } }, icon("message"), t("Notları gönder"))));
  return h("div", { class: "approval" },
    h("div", { class: "row tight wrap" },
      gate.stage ? h("span", { class: "pill info" }, t("{0} bitti", gate.stage)) : "",
      gate.risk ? h("span", { class: `pill ${RISK_PILL[gate.risk] || "mute"}` }, t("risk {0}", gate.risk)) : "",
      gate.size ? h("span", { class: "pill mute" }, t("boyut {0}", gate.size)) : "",
      tk.tasks?.total ? h("span", { class: "pill mute" }, plural(t("{0} görev"), tk.tasks.total)) : ""),
    gate.reason && !gateReasonIsRating(gate.reason) ? h("div", { class: "md" }, MD.blocks(t(gate.reason))) : "",
    tk.split_offer ? h("div", { class: "split-offer" },
      h("div", { class: "muted small" }, t("Plan {0} dilimli ve büyük/riskli: tek PR yerine her dilim ayrı bir ticket ve yığılı draft PR olabilir.", tk.split_offer.length)),
      h("ol", { class: "slice-list" }, tk.split_offer.map((s) => h("li", { class: "slice" }, h("span", { class: "slice-id" }, s.id), h("span", { class: "slice-title" }, s.title))))) : "",
    h("div", { class: "row" },
      primary(h("button", { class: "primary", onclick: () => act(tk, "approve") }, icon("check"), tk.split_offer ? t("Onayla → tek PR") : t("Onayla → devam"))),
      tk.split_offer ? h("button", { title: t("Her dilim bir öncekinin dalına yığılı ayrı bir ticket ve draft PR olur; ilk dilim hemen başlar"),
        onclick: () => act(tk, "split") }, icon("layers"), t("Dilimlere böl ({0} PR)", tk.split_offer.length)) : "",
      h("button", { onclick: () => { changeBox.hidden = !changeBox.hidden; if (!changeBox.hidden) notes.focus(); } }, icon("message"), t("Değişiklik iste")),
      h("button", { class: "ghost", onclick: () => openDrawer(tk, "plan") }, icon("spec"), t("Planı oku"))),
    changeBox);
}

// The gate's reason is "risk high · size large" when the rating stopped it: the pills already say that.
const gateReasonIsRating = (reason) => /^(risk|size) \S+( · (risk|size) \S+)*$/.test(reason.trim());

// --- composer -----------------------------------------------------------------------------------

function composerOpen() {
  $("composer-more").hidden = !($("new-title").value.trim() || $("new-body").value.trim() || composerOrigin);
}

// Where a composer draft came from: a deferred finding ("Ticket'a çevir"), a roadmap item ("Ticket aç") or a lead
// proposal ("Ticket olarak aç" / "Düzenle"). Sent as the new ticket's origin, so the finding shows "→ local-N", the
// item follows the ticket afterwards and the proposal is accepted. Dropped with ×, on a project switch, or once the
// ticket exists.
let composerOrigin = null;
const ORIGIN_CHIP = {
  deferred: (o) => t("Kaynak: {0} deferred {1}", o.ticket, o.entry),
  roadmap: (o) => t("Kaynak: yol haritası {0}{1}", o.phase ? `${o.phase} · ` : "", o.item),
  lead: (o) => t("Kaynak: iş önerisi {0}", o.proposal),
};

function setComposerOrigin(origin) {
  composerOrigin = origin;
  const box = $("composer-origin");
  box.hidden = !origin;
  box.replaceChildren(...(origin ? [
    h("span", { class: "chip" }, icon("pin"), (ORIGIN_CHIP[origin.kind] || ORIGIN_CHIP.deferred)(origin)),
    h("button", { class: "small ghost", type: "button", title: t("Bağlantıyı kaldır (metin kalır)"), onclick: () => setComposerOrigin(null) }, "×"),
  ] : []));
  composerOpen();
}

// "Ticket'a çevir": the composer, prefilled from a deferred entry; creating still takes Oluştur / Oluştur ve başlat.
function convertDeferred(target, ticketId, entry) {
  if (currentView() !== "home") location.hash = "#home";
  $("new-target").value = target;
  $("new-type").value = entry.type;
  $("new-title").value = entry.title;
  $("new-body").value = entry.body;
  setComposerOrigin({ kind: "deferred", target, ticket: ticketId, entry: entry.key });
  composerHint();
  setTimeout(() => { $("new-title").focus(); $("composer").scrollIntoView({ block: "center" }); }, 0);
}

// "Ticket aç" on a roadmap item: the composer, prefilled from the item; creating still takes Oluştur / Oluştur ve başlat.
function roadmapTicket(target, phase, item) {
  if (currentView() !== "home") location.hash = "#home";
  $("new-target").value = target;
  $("new-type").value = "feat";
  $("new-title").value = item.title;
  $("new-body").value = [item.title, t("Evre: {0} · {1}", phase.id, phase.title), item.detail || "", t("Kaynak: yol haritası {0}", item.id)]
    .filter(Boolean).join("\n\n");
  setComposerOrigin({ kind: "roadmap", target, item: item.id, phase: phase.id });
  composerHint();
  setTimeout(() => { $("new-title").focus(); $("composer").scrollIntoView({ block: "center" }); }, 0);
}

const composerTarget = () => snapshot?.targets.find((x) => x.name === $("new-target").value);
const pipelineKey = (name) => `ziggy.choice:${name}`;

// "Hat: Standart / BMad" shows when the chosen project has BMad and the type is not project; the choice is
// remembered per project. Without one, the project's default (default_pipeline: BMad in a controlled project with BMad
// ready). BMad stays off (and says why) while the main checkout lacks its per-developer files.
function composerPipeline() {
  const tk = composerTarget();
  const sel = $("new-pipeline");
  const show = !!tk?.bmad?.installed && $("new-type").value !== "project";
  sel.hidden = !show;
  const bmadOption = sel.querySelector('option[value="bmad"]');
  bmadOption.disabled = !bmadReady(tk?.bmad);
  bmadOption.title = bmadOption.disabled ? bmadPill(tk)?.title || "" : "";
  const choice = (tk ? store.get(localStorage, pipelineKey(tk.name)) : "") || tk?.default_pipeline || "standard";
  sel.value = choice === "bmad" && !bmadOption.disabled ? "bmad" : "standard";
}

const chosenPipeline = () => ($("new-pipeline").hidden ? "standard" : $("new-pipeline").value);

function composerHint() {
  const type = $("new-type").value;
  const tk = composerTarget();
  const target = $("new-target").value || "?";
  composerPipeline();
  composerAfter();
  const line = chosenPipeline() === "bmad" ? "bmad" : pipelineFor(type);
  const after = $("new-after").value;
  $("composer-hint").textContent = t("{0} için {1} ticket'ı · {2} hattı{3}. ", target, type, line, tk?.bmad ? ` · ${bmadText(tk.bmad)}` : "")
    + (after ? t("{0} bitene kadar bekler, sonra {1}.", after, tk?.mode === "controlled" ? t("başlatmak için sorulur") : t("kendiliğinden başlar")) : t("Başlatınca ilk aşama hemen koşar (ajanlar para harcar)."));
}

// "Sonra başla": the chosen project's open tickets (not done, not split, not archived); the choice survives refreshes.
function composerAfter() {
  const sel = $("new-after");
  const target = $("new-target").value || snapshot?.targets.find((x) => !x.error)?.name;
  const open = (snapshot?.tickets || []).filter((x) => x.target === target && !x.archived && !["done", "split", "error"].includes(x.status));
  const options = [["", t("Hemen başlayabilir")], ...open.map((x) => [x.id, t("{0} bitince · {1}", x.id, x.title.length > 40 ? x.title.slice(0, 39) + "…" : x.title)])];
  const current = sel.value;
  if (sel.options.length !== options.length || [...sel.options].some((o, i) => o.value !== options[i][0] || o.textContent !== options[i][1])) {
    sel.replaceChildren(...options.map(([v, label]) => h("option", { value: v }, label)));
    sel.value = options.some(([v]) => v === current) ? current : "";
  }
}

$("new-title").addEventListener("input", composerOpen);
$("new-body").addEventListener("input", composerOpen);
$("new-type").addEventListener("change", composerHint);
$("new-target").addEventListener("change", () => {
  if (composerOrigin && composerOrigin.target !== $("new-target").value) setComposerOrigin(null);
  composerHint();
});
$("new-after").addEventListener("change", composerHint);
$("new-pipeline").addEventListener("change", () => {
  const tk = composerTarget();
  if (tk) store.set(localStorage, pipelineKey(tk.name), $("new-pipeline").value === "bmad" ? "bmad" : "standard");
  composerHint();
});

$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.submitter;
  const start = btn?.value === "start";
  if (!$("new-title").value.trim()) return toast(t("Başlık boş"), true);
  if (btn) btn.disabled = true;
  try {
    const target = $("new-target").value;
    const origin = composerOrigin && composerOrigin.target === target
      ? Object.fromEntries(Object.entries(composerOrigin).filter(([k]) => k !== "target")) : null;
    const out = await api("/api/new", {
      target, title: $("new-title").value, type: $("new-type").value, body: $("new-body").value,
      run: start, plan_checkpoint: $("new-checkpoint").checked, language: $("new-lang").value, pipeline: chosenPipeline(), origin,
      after: $("new-after").value ? [$("new-after").value] : [],
    });
    toast(t("{0} oluşturuldu ({1}){2}", out.ticket.id, out.ticket.branch, out.job ? (out.job.queued ? t(", kuyruğa alındı (koşu slotları dolu)") : $("new-after").value ? t(", {0} bitince başlayacak", $("new-after").value) : t(", başlatıldı")) : ""));
    $("new-after").value = "";
    $("new-title").value = "";
    $("new-body").value = "";
    setComposerOrigin(null);
    if (origin?.kind === "lead") loadLead(true);
    await refresh();
    const row = rowOf(target, out.ticket.id);
    if (row && out.job) openDrawer(row, "activity");
  } catch (err) {
    toast(err.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
});

// The language of new tickets' text (not the panel's own language, which i18n.js keeps under "ziggy.lang").
$("new-lang").addEventListener("change", () => store.set(localStorage, "ziggy.ticketlang", $("new-lang").value || "-"));
{
  const saved = store.get(localStorage, "ziggy.ticketlang");
  if (saved) { $("new-lang").value = saved === "-" ? "" : saved; $("pj-lang").value = saved === "-" ? "" : saved; }
}

function renderTargets(s) {
  const live = s.targets.filter((tk) => !tk.error);
  const names = live.map((tk) => tk.name);
  for (const [sel, withAll] of [[$("new-target"), false], [$("filter"), true]]) {
    const current = sel.value;
    const options = (withAll ? [["", t("Tüm projeler")]] : []).concat(live.map((tk) => [tk.name, withAll || !tk.bmad ? tk.name : `${tk.name} · ${bmadText(tk.bmad)}`]));
    if (sel.options.length !== options.length || [...sel.options].some((o, i) => o.value !== options[i][0] || o.textContent !== options[i][1])) {
      sel.replaceChildren(...options.map(([v, label]) => h("option", { value: v }, label)));
      if ([...sel.options].some((o) => o.value === current)) sel.value = current;
    }
  }
  $("new-target").hidden = names.length < 2;
  $("composer").hidden = !names.length;
  composerHint();
}

// Agent commands often start with `cd <worktree>; …`: drop that, and show the worktree as ".".
function shortLine(line, worktree) {
  if (!worktree || !line) return line || "";
  return line.split(`cd ${worktree}; `).join("").split(`cd ${worktree} && `).join("").split(worktree).join(".");
}

function renderSessions(s) {
  const list = s.sessions || [];
  const hidden = s.sessions_hidden || 0;
  const open = list.filter((x) => x.status !== "ended").length;
  $("sessions-count").textContent = open ? String(open) : "";
  const box = $("sessions-page");
  const sig = JSON.stringify([hidden, list.map((x) => [x.config, x.id, x.status, x.title, x.last_activity, x.last_prompt, x.branch])]);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  SessionsView.renderPage(box, list, ctx, hidden);
}

// Komuta's top right: what today and the week cost so far.
function renderSpend(s) {
  const st = loadStrip.data;
  const sig = JSON.stringify([s.cost_today_usd, st?.totals?.week]);
  if ($("home-spend").dataset.sig === sig) return;
  $("home-spend").dataset.sig = sig;
  $("home-spend").replaceChildren(
    h("a", { class: "spend-item", href: "#stats" }, h("span", { class: "spend-v" }, money(s.cost_today_usd)), t(" bugün")),
    st ? h("a", { class: "spend-item", href: "#stats" }, h("span", { class: "spend-v" }, money(st.totals.week)), t(" 7 gün")) : "");
}

async function loadStrip() {
  if (Date.now() - (loadStrip.at || 0) < 60000) return;
  loadStrip.at = Date.now();
  try { loadStrip.data = await api("/api/stats"); } catch { /* the spend shows what the snapshot has */ }
}

// --- home filter ----------------------------------------------------------------------------------
// One project's board and proposals, or all of them. The choice is this browser's (localStorage); a project
// that is gone falls back to all. Each chip counts what waits on you there.

const HOME_FILTER_KEY = "ziggy.home.target";
const homeTarget = () => {
  const name = store.get(localStorage, HOME_FILTER_KEY) || "";
  return snapshot?.targets.some((tk) => tk.name === name) ? name : "";
};
const inHome = (name) => !homeTarget() || name === homeTarget();

function setHomeTarget(name) {
  store.set(localStorage, HOME_FILTER_KEY, name);
  if (name && [...$("new-target").options].some((o) => o.value === name)) { $("new-target").value = name; composerHint(); }
  if (!snapshot) return;
  renderHomeFilter(snapshot);
  renderBoard(snapshot);
  renderNext();
}

function renderHomeFilter(s) {
  const box = $("home-filter");
  const current = homeTarget();
  const waiting = (s.board?.waiting_on_you || []).map((e) => e.target);
  const live = s.targets.filter((tk) => !tk.error);
  const sig = JSON.stringify([current, live.map((tk) => tk.name), waiting]);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.hidden = live.length < 2;
  const chip = (name, label, n) => h("button", { class: "vtab" + (name === current ? " on" : ""), "aria-pressed": String(name === current),
    onclick: () => setHomeTarget(name) }, label, n ? h("span", { class: "vcount volt", title: t("seni bekleyen") }, String(n)) : "");
  box.replaceChildren(chip("", t("Tümü"), waiting.length), ...live.map((tk) => chip(tk.name, tk.name, waiting.filter((n) => n === tk.name).length)));
}

// --- lead: Sıradaki işler ---------------------------------------------------------------------------
// Every morning the lead (lead.py) turns each project's signals (roadmap, red CI, issues, deferred findings) into
// ticket proposals. Sıradaki işler sits below what waits on you, folded to one line until opened, with
// "Başlat" in a personal project and only "Ticket olarak aç" (the composer) in a controlled one.

const SIGNAL_TR = { ci: "CI", roadmap: t("yol haritası"), issue: "issue", deferred: t("ertelenen bulgu") };
const SIZE_TR = { trivial: t("çok küçük"), small: t("küçük@@size"), medium: t("orta"), large: t("büyük") };
const NEXT_BOX = $("home-next");  // moved into the board, right below "Seni bekliyor", on every board render
let nextOpen = false;  // folded on every load: the proposals are there when you look for them

function placeNext(box) {
  const waiting = box.querySelector(":scope > .board-group.waiting_on_you");
  if (waiting) waiting.after(NEXT_BOX);
  else box.prepend(NEXT_BOX);
}

async function loadLead(force) {
  if (!force && Date.now() - (loadLead.at || 0) < 20000) return;
  loadLead.at = Date.now();
  try { loadLead.data = await api("/api/lead"); } catch { return; }
  renderNext();
}

// Inbox "N yeni iş önerisi": Komuta, Sıradaki işler opened (on that project when the filter hides it); the row is read.
function openNext(target) {
  if (currentView() !== "home") location.hash = "#home";
  nextOpen = true;
  if (!inHome(target)) setHomeTarget(target);
  api("/api/attention/seen", { target, lead: true }).then(refresh, () => { /* stays unread */ });
  loadLead(true).then(() => { renderNext(); setTimeout(() => NEXT_BOX.scrollIntoView({ block: "start" }), 0); });
}

function leadRunText(d) {
  if (!d) return "";
  if (d.targets.some((tk) => tk.running)) return t("Lead şimdi çalışıyor…");
  const last = d.targets.map((tk) => tk.last_run).filter((r) => r?.started_at).sort((a, b) => b.started_at.localeCompare(a.started_at))[0];
  const plan = d.enabled ? t("her sabah {0}", d.at) : t("otomatik çalışma kapalı");
  return last ? t("son: {0} · {1}", when(last.started_at), plan) : t("henüz çalışmadı · {0}", plan);
}

function renderNext() {
  const d = loadLead.data;
  if (!d) return;
  const targets = d.targets.filter((tk) => inHome(tk.target));
  const sig = JSON.stringify([nextOpen, targets.map((tk) => [tk.target, tk.mode, tk.running, tk.last_run, tk.pending.map((p) => [p.id, p.title, p.why, p.signals])])]);
  if (NEXT_BOX.dataset.sig === sig) return;
  NEXT_BOX.dataset.sig = sig;
  NEXT_BOX.hidden = !targets.length;
  NEXT_BOX.classList.toggle("open", nextOpen);
  const total = targets.reduce((n, tk) => n + tk.pending.length, 0);
  NEXT_BOX.replaceChildren(
    h("button", { type: "button", class: "next-toggle", "aria-expanded": String(nextOpen),
      onclick: () => { nextOpen = !nextOpen; renderNext(); } },
      icon("chevron", "chev"), icon("zap"), t("Sıradaki işler"), h("span", { class: "group-n" }, String(total)),
      h("span", { class: "muted small" }, nextOpen ? t("lead'in önerileri: yol haritası, CI, issue'lar, ertelenen bulgular") : leadRunText(d))),
    ...(nextOpen ? targets.map(nextTarget) : []));
}

function nextTarget(tk) {
  const last = tk.last_run;
  const refreshBtn = h("button", { class: "small ghost", disabled: tk.running, title: t("Sinyalleri şimdi topla ve öner (kota varsa kısa bir oturum)"), onclick: async (e) => {
    e.currentTarget.disabled = true;
    try { await api(`/api/lead/${enc(tk.target)}/run`, {}); toast(t("{0}: lead başladı", tk.target)); } catch (err) { toast(err.message, true); }
    loadLead(true);
  } }, icon("retry"), tk.running ? t("Çalışıyor…") : t("Yenile"));
  const status = tk.running ? t("şimdi çalışıyor")
    : !last?.started_at ? t("henüz çalışmadı")
    : [t("son {0}", when(last.started_at)), last.state === "failed" ? t("hata: {0}", last.error || "?") : "", last.state === "skipped" ? t("yeni sinyal yoktu") : "",
      last.agentless ? t("ajansız ({0})", last.why || t("kota yok")) : ""].filter(Boolean).join(" · ");
  return h("section", { class: "next-target" },
    h("div", { class: "next-head" }, snapshot?.targets.length > 1 || tk.mode === "controlled" ? h("strong", {}, tk.target) : "", controlledPill(tk),
      h("span", { class: "muted small grow" }, status), refreshBtn),
    (last?.warnings || []).length ? h("div", { class: "small warn-text" }, last.warnings.map((w) => t(w)).join(" · ")) : "",
    tk.pending.length ? h("div", { class: "live-grid" }, tk.pending.map((p) => proposalCard(tk, p)))
      : h("p", { class: "muted small" }, t("Bekleyen öneri yok. Lead her sabah sinyallere bakar; Yenile şimdi bakar.")));
}

function proposalCard(tk, p) {
  const actions = tk.mode === "controlled"
    ? [h("button", { class: "small primary", title: t("Kontrollü proje: öneri yalnız öneri; ticket'ı composer'dan sen oluşturursun"), onclick: () => proposalToComposer(tk, p) }, icon("ticket"), t("Ticket olarak aç"))]
    : [h("button", { class: "small primary", onclick: (e) => startProposal(e.currentTarget, tk, p) }, icon("play"), t("Başlat")),
      h("button", { class: "small", onclick: () => proposalToComposer(tk, p) }, icon("spec"), t("Düzenle"))];
  actions.push(h("button", { class: "small ghost", onclick: (e) => rejectProposal(e.currentTarget, tk, p) }, icon("x"), t("Reddet")));
  return h("article", { class: "live-card proposal-card" + (p.agentless ? " agentless" : "") },
    h("div", { class: "run-head" }, h("span", { class: "id" }, p.id), h("span", { class: "type" }, p.type),
      p.size ? h("span", { class: "pill mute" }, SIZE_TR[p.size] || p.size) : "",
      p.agentless ? h("span", { class: "pill warn", title: t("Kota ya da oturum yoktu: sinyalden doğrudan yazıldı") }, t("ajansız")) : "",
      h("span", { class: "grow" }), h("span", { class: "muted small nowrap" }, ago(p.created_at))),
    h("div", { class: "proposal-title" }, p.title),
    p.why ? h("div", { class: "small proposal-why" }, p.why) : "",
    h("ul", { class: "proposal-sources" }, (p.signals || []).map((s) => h("li", {},
      h("span", { class: "tag" }, SIGNAL_TR[s.kind] || s.kind), " ", s.url ? link(s.url, s.title) : s.title))),
    h("div", { class: "row tight wrap" }, actions));
}

async function startProposal(btn, tk, p) {
  if (!(await confirmBox(t("{0}\n\n{1} projesinde bu ticket açılıp hemen başlatılsın mı? Ajanlar para harcar.", p.title, tk.target)))) return;
  btn.disabled = true;
  try {
    const out = await api(`/api/lead/${enc(tk.target)}/proposals/${enc(p.id)}`, { action: "start", language: $("new-lang").value });
    toast(t("{0} oluşturuldu, {1}", out.ticket.id, out.job?.queued ? t("kuyruğa alındı (koşu slotları dolu)") : t("başlatıldı")));
    await refresh();
    const row = rowOf(tk.target, out.ticket.id);
    if (row) openDrawer(row, "activity");
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
  }
  loadLead(true);
}

async function rejectProposal(btn, tk, p) {
  const got = await askBox({ title: t("{0} reddedilsin mi?", p.id), text: t("{0}\nBu önerinin kaynakları bir daha önerilmez.", p.title),
    placeholder: t("Neden (isteğe bağlı)"), max: 500, yes: t("Reddet"), optional: true });
  if (!got) return;
  btn.disabled = true;
  try {
    await api(`/api/lead/${enc(tk.target)}/proposals/${enc(p.id)}`, { action: "reject", ...(got.text ? { reason: got.text } : {}) });
    toast(t("{0}: {1} reddedildi", tk.target, p.id));
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
  }
  loadLead(true);
  refresh();
}

// "Düzenle" (personal) and "Ticket olarak aç" (controlled): the composer, prefilled; creating it accepts the proposal.
function proposalToComposer(tk, p) {
  if (currentView() !== "home") location.hash = "#home";
  $("new-target").value = tk.target;
  $("new-type").value = p.type;
  $("new-title").value = p.title;
  $("new-body").value = p.ticket_body;
  setComposerOrigin({ kind: "lead", target: tk.target, proposal: p.id });
  composerHint();
  setTimeout(() => { $("new-title").focus(); $("composer").scrollIntoView({ block: "center" }); }, 0);
}

function renderStatus(s) {
  const running = s.tickets.filter((tk) => tk.running);
  $("status-line").replaceChildren(
    h("span", { class: `dot ${running.length ? "live" : ""}` }), running.length ? plural(t("{0} koşu"), running.length) : t("boşta"),
    h("span", {}, "·"), t("{0} bugün", money(s.cost_today_usd)));
}

// --- ticket lists with view tabs ------------------------------------------------------------------

const VIEWS_T = [
  ["all", t("Hepsi"), () => true],
  ["needs", t("Seni bekliyor"), (tk) => ["needs-user", "awaiting-approval"].includes(tk.status) && !tk.running],
  ["running", t("Sürüyor"), (tk) => tk.running],
  ["new", t("Başlamadı"), (tk) => tk.status === "new" && !tk.running],
  ["failed", t("Başarısız"), (tk) => tk.status === "failed" && !tk.running],
  ["done", t("Bitti"), (tk) => tk.status === "done" && !tk.running],
  ["archived", t("Arşiv"), () => true],
];
const WEEK = 7 * 86400000;
const isOld = (tk) => tk.status === "done" && Date.now() - new Date(tk.updated_at || 0).getTime() > WEEK;
const viewRows = (rows, view) => {
  const [, , test] = VIEWS_T.find(([k]) => k === view) || VIEWS_T[0];
  return rows.filter((tk) => (view === "archived" ? tk.archived : !tk.archived) && test(tk));
};
const PRIORITY = (tk) => (tk.running ? 1 : ["needs-user", "awaiting-approval"].includes(tk.status) ? 0 : 2);

function viewTabs(box, rows, view, onPick) {
  const sig = JSON.stringify([view, VIEWS_T.map(([k]) => viewRows(rows, k).length)]);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.replaceChildren(...VIEWS_T.map(([key, label]) => {
    const n = viewRows(rows, key).length;
    if (key !== "all" && key !== view && !n) return "";
    return h("button", { class: "vtab" + (key === view ? " on" : ""), role: "tab", "aria-selected": String(key === view), onclick: () => onPick(key) },
      label, h("span", { class: "vcount" + (["needs", "failed"].includes(key) && n ? " warn" : "") }, String(n)));
  }));
}

function ticketsView() {
  const v = hashParams().get("view") || "all";
  return VIEWS_T.some(([k]) => k === v) ? v : "all";
}

function renderTickets(s) {
  const view = ticketsView();
  const filter = $("filter").value;
  const words = $("search").value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const scoped = s.tickets.filter((tk) => !filter || tk.target === filter);
  viewTabs($("ticket-tabs"), scoped, view, (key) => {
    history.replaceState(null, "", key === "all" ? "#tickets" : `#tickets?view=${key}`);
    renderTickets(snapshot);
  });
  let rows = viewRows(scoped, view).filter((tk) => !words.length
    || words.every((w) => [tk.id, tk.title, tk.branch, tk.target, tk.status, tk.type, tk.stage].some((v) => (v || "").toLowerCase().includes(w))));
  const old = view === "all" ? rows.filter(isOld) : [];
  $("old-toggle").hidden = !old.length;
  $("old-label").textContent = t("7 günden eski {0} biten ticket'ı da göster", old.length);
  if (!$("show-old").checked) rows = rows.filter((tk) => !old.includes(tk));
  rows.sort((a, b) => PRIORITY(a) - PRIORITY(b) || String(b.updated_at).localeCompare(String(a.updated_at)));
  const live = rows.filter((tk) => tk.running).length;
  $("ticket-count").textContent = live ? t("{0} · {1} hareket halinde", rows.length, live) : String(rows.length);
  ticketList($("ticket-list"), rows, { key: [view, filter, words, $("show-old").checked], showTarget: !filter,
    empty: !s.tickets.length ? t("Henüz ticket yok. Ana sayfadan bir başlık yazıp başla.") : words.length ? t("Filtreye uyan ticket yok.") : t("\"{0}\" içinde ticket yok.", (VIEWS_T.find(([k]) => k === view) || VIEWS_T[0])[1]) });
}

function ticketList(container, rows, opts) {
  // Rebuilding the rows replaces their buttons; do it only when something shown changed (the
  // "updated" column moves once a minute), so a click is never lost to a refresh.
  const signature = JSON.stringify([opts.key, Math.floor(Date.now() / 60000), rows]);
  if (signature === container.dataset.sig) return;
  container.dataset.sig = signature;
  if (!rows.length) {
    container.replaceChildren(empty(opts.empty, "ticket"));
    return;
  }
  const head = h("div", { class: "trow head", "aria-hidden": "true" },
    h("div", {}, "Ticket"), h("div", {}, t("Başlık")), h("div", {}, t("İlerleme")), h("div", {}, "PR"), h("div", { class: "num t-cost" }, t("Maliyet")), h("div", { class: "t-time" }, t("Güncellendi")), h("div", {}));
  container.replaceChildren(head, ...rows.map((tk) => h("div", {
    class: "trow item" + (tk.running ? " running" : "") + (["quiet", "stall"].includes(tk.needs_you?.kind) ? " quiet" : "") + (tk.status === "failed" ? " failed" : "")
      + (!tk.running && ["needs-user", "awaiting-approval"].includes(tk.status) ? " needs" : ""),
    role: "listitem", tabindex: "0",
    onclick: (e) => { if (!e.target.closest("a,button")) openDrawer(tk); },
    onkeydown: (e) => { if (e.key === "Enter" && e.target === e.currentTarget) openDrawer(tk); },
  },
    h("div", { class: "t-idcell" }, h("span", { class: "id" }, tk.id), typeTag(tk), bmadTag(tk), controlledTag(tk)),
    h("div", { class: "t-main" },
      h("div", { class: "t-title", title: tk.title }, tk.title),
      h("div", { class: "t-sub" }, riskChip(tk), opts.showTarget ? h("span", {}, tk.target) : "", originLink(tk), h("span", { class: "branch", title: tk.branch }, icon("branch"), tk.branch || "-"))),
    h("div", { class: "t-status" },
      pipelineBar(tk, true),
      h("div", { class: "row tight" }, badge(tk.status), tk.stage && !["done", "new"].includes(tk.status) ? h("span", { class: "muted small mono" }, tk.stage) : "",
        tk.tasks?.total ? h("span", { class: "muted small", title: t("biten / toplam görev") }, `${tk.tasks.done}/${tk.tasks.total}`) : ""),
      tk.running ? h("span", { class: "live-line" }, runLabel(tk)) : tk.status === "waiting" ? h("span", { class: "live-line" }, waitText(tk))
        : tk.status === "quota" ? h("span", { class: "live-line warn-text" }, quotaText(tk))
        : tk.status === "host" ? h("span", { class: "live-line warn-text" }, HOST_TEXT)
        : tk.status === "split" ? h("span", { class: "muted small" }, t("{0} dilim: {1}", (tk.children || []).length, (tk.children || []).map((c) => c.id).join(", "))) : "",
      tk.archived ? h("span", { class: "pill mute" }, t("arşivde")) : reviewPill(tk)),
    h("div", { class: "t-pr" }, tk.pr ? link(tk.pr, h("span", { class: `pr-pill ${(tk.pr_state || "open").toLowerCase()}` }, icon("pr"), "#" + tk.pr.split("/").pop(),
      tk.pr_state && tk.pr_state !== "OPEN" ? (tk.pr_state === "MERGED" ? " merged" : t(" kapalı")) : "")) : h("span", { class: "muted" }, "—")),
    h("div", { class: "num t-cost" }, money(tk.cost_usd)),
    h("div", { class: "muted t-time nowrap" }, ago(tk.updated_at)),
    h("div", { class: "t-actions" }, actionButtons(tk, true)),
  )));
}

// An issue the ticket came from (YouTrack and the like), as a link to the tracker; a deferred finding,
// as a button to the ticket whose review deferred it; a roadmap item it works on, as a link to its project's roadmap.
function originLink(tk) {
  const r = tk.roadmap;
  if (r) {
    return h("a", { class: "origin", href: `#project/${encodeURIComponent(tk.target)}`, title: r.title ? t("Yol haritası maddesi: {0}", r.title) : t("Yol haritası maddesi"),
      onclick: (e) => e.stopPropagation() }, t("Yol haritası: {0}{1}", r.phase ? `${r.phase} · ` : "", r.item));
  }
  const o = tk.origin;
  if (!o || o.kind === "roadmap") return "";
  if (o.kind === "lead") {
    return h("button", { class: "origin link", type: "button", title: t("Lead'in iş önerisi {0}", o.proposal),
      onclick: (e) => { e.stopPropagation(); location.hash = "#home"; } }, t("← öneri {0}", o.proposal));
  }
  if (o.kind === "deferred") {
    return h("button", { class: "origin link", type: "button", title: t("{0} ticket'ının ertelenen bulgusu {1}", o.ticket, o.entry),
      onclick: (e) => { e.stopPropagation(); const row = rowOf(tk.target, o.ticket); if (row) openDrawer(row, "plan"); } }, `← ${o.ticket} deferred`);
  }
  const label = h("span", { class: "origin" }, o.ref, o.url ? " ↗" : "");
  return o.url ? link(o.url, label) : label;
}

function reviewPill(tk) {
  const r = tk.review;
  if (!r || r.state === "running") return "";
  const text = r.state === "error" ? t("review hata") : r.stale ? t("review · eski") : r.findings ? plural(t("review · {0} bulgu"), r.findings) : t("review · temiz");
  return h("button", {
    class: `pill-btn ${r.state === "error" ? "bad" : r.stale ? "" : r.findings ? "warn" : "ok"}`,
    title: r.stale ? t("Dal bu review'dan sonra değişti; yeni hali incelenmedi") : null,
    onclick: (e) => { e.stopPropagation(); openDrawer(tk, "review"); },
  }, icon("eye"), text, r.posted ? t(" · PR'da") : "");
}

// Projects as cards: what is open, what it cost, its run slots (a used slot slides), and the way in.
function projectList(box, s) {
  const costs = Object.fromEntries((loadStrip.data?.by_target || []).map((g) => [g.name, g]));
  const waiting = (s.board?.waiting_on_you || []).map((e) => e.target);
  const signature = JSON.stringify([s.targets, s.tickets.map((tk) => [tk.target, tk.archived, tk.status, tk.running, tk.id]), costs, waiting]);
  if (signature === box.dataset.sig) return;
  box.dataset.sig = signature;
  if (!s.targets.length) {
    box.replaceChildren(empty(t("Henüz proje yok. 'Mevcut repoyu ekle' ya da 'Sıfırdan yeni proje' ile başla."), "box"));
    return;
  }
  const stat = (value, label) => h("div", { class: "pstat" }, h("div", { class: "pstat-v" }, value), h("div", { class: "pstat-l" }, label));
  box.replaceChildren(...s.targets.map((tk) => {
    const rows = s.tickets.filter((x) => x.target === tk.name && !x.archived);
    const open = rows.filter((x) => !["done", "split"].includes(x.status)).length;
    const live = rows.filter((x) => x.running && !x.queued_at);
    const mine = waiting.filter((n) => n === tk.name).length;
    const g = costs[tk.name];
    const href = `#project/${encodeURIComponent(tk.name)}`;
    const state = tk.error ? h("span", { class: "pill bad" }, t("config hatası"))
      : live.length ? h("span", { class: "pill info" }, h("span", { class: "dot live" }), t("{0} canlı", live.length))
      : mine ? h("span", { class: "pill volt" }, h("span", { class: "dot volt-dot" }), t("{0} sende", mine))
      : h("span", { class: "pill mute" }, t("boşta"));
    const slots = Array.from({ length: tk.max_active || 0 }, (_, i) => h("span", { class: "slot-seg" + (i < (tk.active || []).length ? " used" : "") }));
    return h("article", { class: "pcard" + (rows.length ? "" : " fresh"), role: "listitem" },
      h("div", { class: "pcard-top" },
        h("div", { class: "grow" }, tk.error ? h("span", { class: "pcard-name" }, tk.name) : h("a", { class: "pcard-name", href }, tk.name),
          h("div", { class: "pcard-where mono" }, [tk.github_repo, homeShort(tk.path)].filter(Boolean).join(" · "))),
        state),
      tk.error ? h("p", { class: "error-banner small" }, t(tk.error))
        : rows.length ? h("div", { class: "pstats" }, stat(String(open), t("açık ticket")), stat(g ? money(g.cost) : "—", t("toplam maliyet")), stat(g ? String(g.sessions) : "0", t("oturum")))
        : h("div", { class: "pcard-empty" }, h("strong", {}, t("Henüz ticket yok")), h("span", { class: "muted small" }, t("Proje hazır. İlk işi ver; plan çıkınca seni çağırır."))),
      tk.error ? "" : h("div", { class: "pcard-slot" }, h("span", { class: "side-label" }, "Slot"), h("span", { class: "slot-segs" }, slots),
        h("span", { class: "muted small grow" }, live.length ? live.map((x) => `${x.id} · ${x.stage || t("hazırlanıyor")}`).join(", ") : t("boş"))),
      h("div", { class: "row pcard-foot" }, bmadPill(tk), controlledPill(tk),
        h("span", { class: "grow" }),
        tk.error ? "" : h("button", { class: "small", onclick: () => { focusComposer(); $("new-target").value = tk.name; composerHint(); } }, rows.length ? t("Ticket ver") : t("İlk ticket'ı ver")),
        rows.length ? h("button", { class: "small ghost", onclick: () => { $("filter").value = tk.name; location.hash = "#tickets"; } }, t(rows.length === 1 ? "{0} ticket@@one" : "{0} ticket@@many", rows.length)) : "",
        tk.error ? "" : h("a", { class: "button small ghost", href }, t("Aç →")),
        h("button", { class: "small ghost", title: t("Projeyi Ziggy'den çıkar (repo ve ticket geçmişi yerinde kalır)"), disabled: live.length > 0,
          onclick: (e) => removeTarget(e.currentTarget, tk.name) }, icon("x"), t("Çıkar"))));
  }));
}

// What removed projects left in the state dir: mostly their worktrees, which "Worktree'leri sil" removes (their
// branches stay in the repository, the tickets and memory in the state dir).
const bytes = (n) => (n >= 1 << 30 ? `${(n / (1 << 30)).toFixed(1)} GB` : `${Math.round(n / 1048576)} MB`);

async function loadLeftovers() {
  let found;
  try { found = (await api("/api/leftovers")).leftovers; } catch { return; }
  const box = $("leftovers");
  box.hidden = !found.length;
  box.replaceChildren(h("h2", { class: "board-group-h" }, t("Çıkarılmış projelerin artıkları"),
    h("span", { class: "muted small" }, t("Ticket geçmişi ve hafıza kalır; yalnız worktree'ler silinir, dalları repoda durur."))),
  ...found.map((x) => h("div", { class: "row leftover" },
    h("strong", {}, x.name), h("span", { class: "muted small grow" }, `${bytes(x.bytes)} · ${x.worktrees.length} worktree (${bytes(x.worktree_bytes)})`
      + (x.worktrees.some((tk) => tk.dirty) ? t(" · kaydedilmemiş değişiklik var") : "")),
    x.worktrees.length ? h("button", { class: "small", onclick: (e) => purgeLeftover(e.currentTarget, x) }, icon("x"), t("Worktree'leri sil ({0})", bytes(x.worktree_bytes))) : "")));
}

async function purgeLeftover(btn, x) {
  if (!(await confirmBox(t("{0} projesinin {1} worktree'si silinsin mi? ({2})\n\nDalları repoda, ticket geçmişi ve hafıza Ziggy'de kalır.", x.name, x.worktrees.length, bytes(x.worktree_bytes))))) return;
  btn.disabled = true;
  const send = (force) => api(`/api/leftovers/${enc(x.name)}/purge`, { force });
  try {
    let out;
    try { out = await send(false); } catch (e) {
      if (!/uncommitted/.test(e.message) || !(await confirmBox(t("{0}\n\nYine de silinsin mi?", e.message)))) throw e;
      out = await send(true);
    }
    toast(t("{0}: {1} worktree silindi, {2} açıldı", x.name, out.removed.length, bytes(out.freed_bytes)));
  } catch (e) {
    toast(e.message, true);
  }
  loadLeftovers();
}

// "Çıkar": the target's config table goes (after a backup); its repository, worktrees, tickets and memory stay,
// so adding the same repository again brings them back. The server refuses while one of its tickets runs.
async function removeTarget(btn, name) {
  if (!(await confirmBox(t("{0} Ziggy'den çıkarılsın mı?\n\nConfig'teki kaydı silinir (önce yedeklenir). Repo, worktree'ler, ticket geçmişi ve hafıza yerinde kalır; aynı repoyu yeniden eklersen geri gelir.", name)))) return;
  btn.disabled = true;
  try {
    await api(`/api/targets/${enc(name)}/remove`, {});
    toast(t("{0} Ziggy'den çıkarıldı", name));
    await refresh();
    loadLeftovers();
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
  }
}

async function refresh() {
  if (document.hidden) return;
  try {
    snapshot = await api("/api/status");
    lastOkAt = Date.now();
    $("conn").hidden = true;
  } catch (e) {
    $("conn").hidden = false;
    if (snapshot) renderHealth(snapshot);
    return;
  }
  const s = snapshot;
  renderStatus(s);
  renderTargets(s);
  renderHealth(s);
  renderBoard(s);
  renderInbox(s);
  renderSessions(s);
  renderTickets(s);
  $("tickets-count").textContent = String(s.tickets.filter((tk) => !tk.archived && !["done", "split"].includes(tk.status)).length || "");
  $("projects-count").textContent = String(s.targets.length || "");
  projectList($("targets"), s);
  if (currentView() === "project") renderProject(s);
  if (currentView() === "home") {
    loadStrip().then(() => renderSpend(snapshot));
    loadLead();
  }
  if (currentView() === "talk") openTalk();  // the first snapshot opens it; later ones keep the list fresh
  renderSpend(s);
  renderMemoryCount(s);
  if (currentView() === "memory" && s.memory_pending !== loadMemory.pending && !memoryEditing()) loadMemory();
  if (currentView() === "stats" && Date.now() - (loadStats.at || 0) > 30000) loadStats();
  if (drawer) refreshDrawerHead();
}

// --- ticket page ------------------------------------------------------------------------------
// A ticket is a page of its own (#ticket/<target>/<id>?tab=…), not a window over the list. `drawer` keeps
// its old name: it is the open ticket's state. The tab bar groups the run's record (Etkinlik, Aşamalar,
// Job'lar) under Kayıt; the chat sits beside the tabs; Değişiklikler is the full review page.

const TABS = [["plan", "Plan", "spec"], ["questions", t("Sorular"), "message"], ["changes", t("Değişiklikler"), "diff"], ["review", "Review", "eye"],
  ["log", t("Kayıt"), "activity"], ["ticket", t("Ticket metni"), "ticket"]];
const LOG_TABS = [["activity", t("Etkinlik")], ["stages", t("Aşamalar")], ["jobs", t("Job'lar")]];
const isLogTab = (key) => LOG_TABS.some(([k]) => k === key);
const enc = encodeURIComponent;

function defaultTab(tk) {
  if (tk.round) return "questions";
  if (tk.running) return "activity";
  if (tk.status === "awaiting-approval") return "plan";
  if (tk.status === "new") return "ticket";
  return "plan";
}

const ticketHash = (target, id, tab) => `#ticket/${enc(target)}/${enc(id)}${tab ? `?tab=${enc(tab)}` : ""}`;

function parseTicketHash(text) {
  const m = /^#ticket\/([^/?]+)\/([^/?]+)(?:\?(.*))?$/.exec(text || "");
  if (!m) return null;
  const q = new URLSearchParams(m[3] || "");
  return { target: decodeURIComponent(m[1]), id: decodeURIComponent(m[2]), tab: q.get("tab") };
}

// What the page should open with next: the stage session to scroll to (commit and evidence links).
let pendingHighlight = null;

// Open a ticket: its page, on a tab. The same ticket already open only switches the tab.
function openDrawer(tk, tab, opts) {
  closeInbox();
  pendingHighlight = opts?.highlight || null;
  const want = tab === "chat" ? null : tab;
  if (drawer && drawer.target === tk.target && drawer.id === tk.id && currentView() === "ticket") {
    if (tab === "chat") return renderChatSide(true);
    if (want) switchTab(want);
    if (pendingHighlight) { drawer.highlight = pendingHighlight; pendingHighlight = null; renderTabs(); }
    return;
  }
  ticketReturn = currentView() === "ticket" ? ticketReturn : location.hash || "#home";
  location.hash = ticketHash(tk.target, tk.id, want || defaultTab(tk));
  if (tab === "chat") chatSeedFocus = true;
}

// Where Esc and "geri" go: the view the ticket was opened from.
let ticketReturn = "#tickets";
let chatSeedFocus = false;

function leaveTicket() {
  location.hash = ticketReturn && !ticketReturn.startsWith("#ticket/") ? ticketReturn : "#tickets";
}

// The route opened a ticket: set up its state, paint the head from the snapshot at once, then load the detail.
function showTicket(params) {
  const tk = rowOf(params.target, params.id);
  const tab = params.tab || (tk ? defaultTab(tk) : "plan");
  if (drawer && drawer.target === params.target && drawer.id === params.id) {
    if (tab !== drawer.tab && !(tab === "changes" && drawer.tab === "changes")) switchTab(tab);
    return;
  }
  closeDrawer();
  drawer = { target: params.target, id: params.id, tab, phase: null, follow: true, all: false, offset: 0, detail: null, files: {},
    highlight: pendingHighlight, reviewCleanup: null };
  pendingHighlight = null;
  $("drawer").hidden = false;
  $("d-crumb").textContent = params.id;
  $("d-back").textContent = ticketReturn.startsWith("#home") ? t("Komuta") : ticketReturn.startsWith("#project") ? t("Proje") : t("Ticket'lar");
  $("d-back").href = ticketReturn.startsWith("#ticket/") ? "#tickets" : ticketReturn;
  $("d-title").textContent = tk ? tk.title : params.id;
  $("d-body").replaceChildren(h("div", { class: "skeleton" }, h("span"), h("span"), h("span")));
  $("d-tabs").replaceChildren();
  $("d-subtabs").hidden = true;
  if (tk) { markSeen(tk); refreshDrawerHead(); }
  renderChatSide(chatSeedFocus);
  chatSeedFocus = false;
  loadDetail();
}

function closeDrawer() {
  if (!drawer) return;
  clearTimeout(drawer.timer);
  closeTranscript();
  if (drawer.reviewCleanup) drawer.reviewCleanup();
  drawer = null;
  $("drawer").hidden = true;
  $("d-chat").replaceChildren();
  $("d-next").hidden = true;
}

// The chat beside the tabs: one per open ticket; "Sor" buttons seed it (chat.js) and bring it into view.
function renderChatSide(focus) {
  const d = drawer;
  if (!d || !window.ChatView) return;
  const row = rowOf(d.target, d.id) || d.detail;
  if (!row) return;
  ChatView.renderTab($("d-chat"), row, ctx);
  if (focus) $("d-chat").scrollIntoView({ block: "nearest" });
}

// A tab that must not be rebuilt under the user: a live log, a transcript, the review page's
// own state, or anything half typed.
function drawerBusy() {
  const d = drawer;
  return d.tab === "activity" || d.tab === "changes" || (d.tab === "stages" && d.tx)
    || [...$("d-body").querySelectorAll("textarea")].some((x) => x.value.trim())
    || $("d-body").querySelector(".fix-box:not(:empty)");
}

// The next step, when the ticket waits on you: what it waits for, and the buttons that answer it. Only the answer is
// promoted into the banner; cleanup, stop and the other buttons stay in the page head, as they were before Volt.
function renderNextStep(tk) {
  const n = tk.needs_you;
  const box = $("d-next");
  const all = actionButtons(tk, false).filter(Boolean);
  const lead = n ? nextStepLead(tk, n, all) : [];
  const leadLabels = new Set(lead.map((el) => el.textContent));
  $("d-actions").replaceChildren(...all.filter((el) => !lead.includes(el) && !leadLabels.has(el.textContent)));
  if (!n) {
    box.hidden = true;
    return;
  }
  if (n.dismissable) lead.push(dismissButton(tk, n, ""));
  box.hidden = false;
  box.replaceChildren(
    h("div", { class: "next-text" },
      h("span", { class: "next-kicker" }, h("span", { class: "dot" }), t("Sıradaki hamle senin")),
      h("div", { class: "next-title" }, n.kind === "stall" ? t(n.title) : NEED_TITLE[n.kind] || t(n.title) || t("Seni bekliyor")),
      n.details && !(n.kind === "approval" && gateReasonIsRating(n.details)) ? h("div", { class: "next-detail" }, t(n.details.split("\n")[0])) : ""),
    lead.length ? h("div", { class: "row next-actions" }, lead) : "");
}

// The banner's buttons per kind, the same next step the board card offers; otherwise the head's buttons minus the
// destructive or incidental ones (Temizle, Durdur: danger and ghost).
function nextStepLead(tk, n, all) {
  if (n.kind === "pr-feedback") {
    return [h("a", { class: "button primary", href: ReviewPage.hash(tk.target, tk.id, null, null, "github") }, icon("diff"), t("Geri bildirime bak")),
      tk.pr ? h("a", { class: "button", href: tk.pr, target: "_blank", rel: "noopener noreferrer" }, icon("pr"), t("PR'ı aç"), icon("external")) : ""].filter(Boolean);
  }
  if (["quiet", "stall"].includes(n.kind)) return [h("button", { class: "primary", onclick: () => nudgeDialog(tk) }, icon("message"), t("Yönlendir"))];
  if (n.kind === "blocked") {
    return (n.waiting_for || []).map((d) => rowOf(tk.target, d.id)).filter(Boolean)
      .map((dep, i) => h("button", { class: i ? "" : "primary", onclick: () => openDrawer(dep) }, icon("ticket"), t("{0} aç", dep.id)));
  }
  if (n.kind === "start") return [h("button", { class: "primary", onclick: () => act(tk, "run") }, icon("play"), t("Başlat"))];
  if (n.kind === "protected" && n.patch) return protectedButtons(tk, n.patch);
  return all.filter((el) => !el.classList.contains("danger") && !el.classList.contains("ghost"));
}

function refreshDrawerHead() {
  const tk = rowOf(drawer.target, drawer.id);
  if (!tk) return;
  const signature = JSON.stringify(tk);
  if (signature === drawer.headSignature) return;
  drawer.headSignature = signature;
  if (drawer.detail && !drawerBusy()) loadDetail();  // e.g. a stage just finished
  $("d-sub").replaceChildren(h("span", { class: "id" }, tk.id), typeTag(tk), bmadTag(tk), controlledTag(tk), badge(tk.status),
    tk.pipeline ? h("span", { class: "muted small" }, t("{0} hattı", tk.pipeline)) : tk.legacy ? h("span", { class: "pill mute" }, "v1") : "",
    tk.running ? h("span", { class: "live-line" }, h("span", { class: "dot live" }), runLabel(tk))
      : tk.status === "waiting" ? h("span", { class: "live-line" }, h("span", { class: "dot warn" }), waitText(tk))
      : tk.status === "quota" ? h("span", { class: "live-line warn-text" }, h("span", { class: "dot warn" }), quotaText(tk))
      : tk.status === "host" ? h("span", { class: "live-line warn-text" }, h("span", { class: "dot warn" }), HOST_TEXT) : "");
  $("d-title").textContent = tk.title;
  $("d-meta").replaceChildren(
    chip("box", tk.target), originLink(tk), chip("branch", tk.branch, "mono"), chip("dollar", money(tk.cost_usd)), chip("clock", ago(tk.updated_at)),
    tk.risk ? h("span", { class: `pill ${RISK_PILL[tk.risk] || "mute"}` }, t("risk {0}", tk.risk)) : "", tk.size ? h("span", { class: "pill mute" }, tk.size) : "",
    tk.pr ? link(tk.pr, chip("pr", "PR #" + tk.pr.split("/").pop(), "linked")) : "",
    tk.slice && tk.parent ? h("span", { class: "pill info", title: t("{0}\nTaban: {1}", tk.parent.title, tk.base_branch) }, t("{0} · dilim {1}", tk.parent.id, tk.slice)) : "",
    tk.after?.length && tk.status !== "waiting" ? h("span", { class: "pill mute" }, t("sonra: {0}", tk.after.map((d) => d.id).join(", "))) : "");
  $("d-pipe").replaceChildren(pipelineBar(tk, false));
  renderNextStep(tk);
}

async function loadDetail() {
  if (!drawer) return;
  const d = drawer;
  const wasLive = d.detail?.chat?.live;
  try {
    d.detail = await api(`/api/ticket/${enc(d.target)}/${enc(d.id)}`);
  } catch (e) {
    if (drawer !== d) return;
    $("d-body").replaceChildren(h("p", { class: "error-banner" }, e.message));
    return;
  }
  if (drawer !== d) return;
  if (wasLive && !d.detail.chat?.live && !onScreen($("d-chat"))) toast(t("{0}: sohbette cevap hazır", d.id));
  if (!$("d-chat").firstChild) renderChatSide(false);
  if (snapshot) refreshDrawerHead();
  renderTabs();
}

function renderTabs() {
  const d = drawer;
  const det = d.detail;
  if (!det) return;
  const counts = { log: det.stage_runs.length, review: det.reviews.length, questions: det.rounds.length, changes: det.drafts || 0 };
  const hidden = (key) => (key === "changes" && !det.worktree) || (key === "questions" && !det.rounds.length && d.tab !== "questions" && det.status !== "failed");
  const on = (key) => d.tab === key || (key === "log" && isLogTab(d.tab));
  $("d-tabs").replaceChildren(...TABS.filter(([key]) => !hidden(key)).map(([key, label, ic]) =>
    h("button", { class: on(key) ? "on" : "", role: "tab", "aria-selected": String(on(key)),
      onclick: () => switchTab(key === "log" ? (isLogTab(d.lastLog) ? d.lastLog : "activity") : key) },
      icon(ic), label, counts[key] ? h("span", { class: "count" + (key === "questions" && det.round ? " warn" : "") }, counts[key]) : "")));
  const sub = $("d-subtabs");
  sub.hidden = !isLogTab(d.tab);
  if (isLogTab(d.tab)) {
    d.lastLog = d.tab;
    const n = { stages: det.stage_runs.length, jobs: det.jobs.length };
    sub.replaceChildren(...LOG_TABS.map(([key, label]) => h("button", { class: "seg" + (d.tab === key ? " on" : ""), "aria-pressed": String(d.tab === key),
      onclick: () => switchTab(key) }, label, n[key] ? h("span", { class: "vcount" }, String(n[key])) : "")));
  }
  const body = $("d-body");
  $("drawer").classList.toggle("wide", d.tab === "changes");  // the diff takes the chat's column
  if (d.tab !== "changes" && d.reviewCleanup) { d.reviewCleanup(); d.reviewCleanup = null; }
  if (d.tab === "activity") return renderActivity();
  if (d.tab === "plan") body.replaceChildren(...renderPlan(det));
  if (d.tab === "ticket") body.replaceChildren(...renderTicket(det.ticket_md));
  if (d.tab === "stages") {
    body.replaceChildren(...renderStages(det));
    if (d.highlight) document.getElementById(`sr-${d.highlight}`)?.scrollIntoView({ block: "center" });
  }
  if (d.tab === "questions") body.replaceChildren(...renderQuestions(det));
  if (d.tab === "jobs") body.replaceChildren(...renderJobs(det.jobs));
  if (d.tab === "review") body.replaceChildren(...renderReviews(det));
  if (d.tab === "changes" && !d.reviewCleanup) {
    const box = h("div", { class: "changes-tab" });
    body.replaceChildren(box);
    const params = ReviewPage.parseHash(location.hash) || { target: d.target, id: d.id };
    const out = ReviewPage.renderPage(box, params, ctx, { embedded: true });
    d.reviewCleanup = typeof out === "function" ? out : () => {};
  }
}

function switchTab(key) {
  const d = drawer;
  if (!d) return;
  if (key === "log") key = isLogTab(d.lastLog) ? d.lastLog : "activity";
  if (key === "chat") return renderChatSide(true);
  d.tab = key;
  clearTimeout(d.timer);
  d.offset = 0;
  d.shownPhase = null;
  const want = ticketHash(d.target, d.id, key);
  if (parseTicketHash(location.hash)?.tab !== key) history.replaceState(null, "", want);
  renderTabs();
}

// Ticket: the wrapper's facts, the untrusted ticket text, and the user's decisions as cards.
function sectionCards(sections, variant) {
  return sections.map((s, i) => {
    const v = variant ? variant(s) : {};
    return h("details", { class: `section ${v.cls || ""}`, id: `sec-${i}`, open: v.closed ? false : true },
      h("summary", {}, h("span", { class: "section-title" }, v.icon ? icon(v.icon) : "", s.title),
        v.tag ? h("span", { class: "tag " + (v.tagCls || "") }, v.tag) : "",
        h("span", { class: "grow" }), icon("chevron", "chev")),
      h("div", { class: "section-body md" }, v.body ? v.body(s) : MD.blocks(s.body)));
  });
}

function renderTicket(text) {
  const parsed = MD.sections(text || "");
  return sectionCards(parsed.sections, (s) => {
    if (s.title === "Pipeline") return { cls: "trusted", icon: "shield", tag: t("pipeline yazdı · güvenilir"), body: (x) => MD.blocks(x.body.replace(/^Written by the pipeline wrapper; trusted\.\s*$/m, "")) };
    if (s.title.startsWith("Ticket")) return { cls: "untrusted", icon: "ticket", tag: t("güvenilmeyen girdi"), tagCls: "warn" };
    if (s.title.startsWith("Decisions")) return { cls: "decisions", icon: "user", tag: t("senin kararların") };
    return {};
  });
}

// Plan: inbox/plan.md as the agents wrote it, with the task progress, the wrapper's sections
// (your decisions, review rounds) marked, and the review's deferred findings below.
function renderPlan(det) {
  const tk = rowOf(drawer.target, drawer.id) || det;
  const head = h("div", { class: "plan-head" },
    h("span", { class: "mono small muted path-inline" }, det.plan_path || ""),
    det.tasks?.total ? h("span", { class: "tasks-meter", title: t("biten / toplam görev") },
      h("span", { class: "meter" }, h("span", { class: "meter-fill", style: `width:${Math.round((det.tasks.done / det.tasks.total) * 100)}%` })),
      t("görevler {0}/{1}", det.tasks.done, det.tasks.total)) : "",
    det.risk ? h("span", { class: `pill ${RISK_PILL[det.risk] || "mute"}` }, t("risk {0}", det.risk)) : "",
    det.size ? h("span", { class: "pill mute" }, det.size) : "",
    det.checks?.length ? h("span", { class: "row tight" }, det.checks.map(checkChip)) : "");
  const out = [head];
  if (det.status === "awaiting-approval") out.push(h("div", { class: "need approval-inline wide" }, approvalBox(tk, det.gate || {})));
  if (tk.children?.length) {
    out.push(h("section", { class: "section stack-box" },
      h("div", { class: "section-title" }, icon("layers"), tk.status === "split" ? t("Dilimler") : t("Yığın ({0})", tk.parent?.id || ""),
        h("span", { class: "muted small" }, tk.status === "split" ? t(" · bu ticket dilimlerin kabı; kendi koşusu yok") : t(" · bu ticket dilim {0}, tabanı {1}", tk.slice, tk.base_branch))),
      sliceList(tk, tk.children)));
  }
  if (tk.status === "waiting") out.push(h("div", { class: "need blocked" }, h("div", { class: "need-kind" }, icon("clock"), " ", waitText(tk)),
    h("div", { class: "muted small" }, tk.mode === "controlled"
      ? t("Kontrollü proje: beklediği ticket'lar bitince gelen kutusu başlatmak için sorar; kendiliğinden başlamaz.")
      : t("Beklediği ticket'lar bitince kendiliğinden başlar; panel her dakika ve bir ticket her bittiğinde yeniden bakar."))));
  if (tk.status === "quota") out.push(h("div", { class: "need blocked" }, h("div", { class: "need-kind" }, icon("clock"), " ", quotaText(tk)),
    h("div", { class: "muted small" }, t("{0} Panel her dakika bakar; yeniden başlarsa da kaybolmaz.", quotaDetail(tk)))));
  if (tk.status === "host") out.push(h("div", { class: "need blocked" }, h("div", { class: "need-kind" }, icon("clock"), " ", HOST_TEXT),
    h("div", { class: "muted small" }, t("{0} Panel her dakika bakar; yeniden başlarsa da kaybolmaz.", hostDetail(tk)))));
  if (!det.plan_text.trim()) {
    out.push(empty(det.legacy ? t("Bu ticket eski (v1) hatta koştu; planı hedef repodaki spec dosyasında.") : t("Henüz plan yok. Planlama aşaması bitince burada görünür."), "spec"));
  } else {
    const parsed = MD.sections(det.plan_text);
    if (parsed.intro.trim()) out.push(h("div", { class: "md plan-intro" }, MD.blocks(parsed.intro)));
    out.push(...sectionCards(parsed.sections, (s) => {
      if (/^Decisions \(user\)/.test(s.title)) return { cls: "decisions", icon: "user", tag: t("senin kararların") };
      if (/^Review \(user/.test(s.title)) return { cls: "decisions", icon: "message", tag: t("senin review'un") };
      if (/^Review \(codex/.test(s.title)) return { cls: "result", icon: "eye", tag: "Codex review" };
      return {};
    }));
  }
  if (det.deferred_text.trim()) {
    const entries = det.deferred || [];
    const open = entries.filter((e) => !e.ticket).length;
    out.push(h("details", { class: "section deferred-card" },
      h("summary", {}, h("span", { class: "section-title" }, icon("pin"), t("Ertelenenler (deferred.md)")), h("span", { class: "grow" }),
        entries.length ? h("span", { class: "muted small" }, t("{0} bulgu · {1} açık", entries.length, open)) : "", icon("chevron", "chev")),
      entries.length
        ? h("div", { class: "section-body" }, deferredEntries(det, entries))
        : h("div", { class: "section-body md" }, MD.blocks(det.deferred_text))));
  }
  return out;
}

// One row per deferred finding, grouped by review round: the finding, and either "Ticket'a çevir" or the
// ticket already made from it.
function deferredEntries(det, entries) {
  const out = [];
  let round;
  for (const e of entries) {
    if (e.round !== round) {
      round = e.round;
      out.push(h("h4", { class: "deferred-round" }, round == null ? t("Tur belirtilmemiş") : t("Tur {0}", round)));
    }
    const made = e.ticket ? rowOf(det.target, e.ticket) : null;
    out.push(h("div", { class: "deferred-entry" },
      h("div", { class: "md" }, MD.blocks(e.text)),
      h("div", { class: "deferred-act" }, e.ticket
        ? h("button", { class: "small ghost", type: "button", disabled: !made, onclick: () => made && openDrawer(made, "plan") }, `→ ${e.ticket}`)
        : h("button", { class: "small", type: "button", title: t("{0} ticket'ı taslağı; oluşturmadan önce düzenleyebilirsin", e.type),
            onclick: () => convertDeferred(det.target, det.id, e) }, t("Ticket'a çevir")))));
  }
  return out;
}

const PASS = /^(pass|passed|ok|success)$/i;
// A check that fails on the base branch too is pre-existing: amber, not sent back to the agent.
const checkChip = (c) => h("span", { class: `pill ${PASS.test(c.status) ? "ok" : c.preexisting ? "warn" : /^(fail|failed|error)$/i.test(c.status) ? "bad" : "mute"}`,
  title: `${c.source === "engine" ? t("pipeline çalıştırdı") : t("ajan bildirdi")} · ${c.preexisting ? t("base'de de kalıyor (önceden var)") : c.status}${c.detail ? `\n${c.detail}` : ""}` },
  c.preexisting ? t("{0} (önceden)", c.name) : c.name);
const VERDICT_PILL = { READY: "ok", NEEDS_USER: "warn", FAILED: "bad" };
const STATE_TR = { running: t("çalışıyor"), ok: t("tamam"), failed: t("başarısız"), killed: t("durduruldu"), timeout: t("zaman aşımı"), lost: t("kayıp") };

// What the project memory gave this ticket's sessions and which entries they said they relied on.
function memoryUse(det) {
  const sessions = (det.memory?.sessions || []).filter((s) => s.shown || s.used?.length);
  if (!sessions.length) return "";
  const used = det.memory.used || [];
  return h("details", { class: "section mem-use", open: true },
    h("summary", {}, h("span", { class: "section-title" }, icon("spec"), t("Hafıza")),
      h("span", { class: "muted small" }, used.length ? plural(t("{0} kayıt kullanıldı"), used.length) : t("kullanılan kayıt yok")),
      h("span", { class: "grow" }), icon("chevron", "chev")),
    h("ul", { class: "section-body mem-use-list" }, sessions.map((s) => h("li", {},
      h("span", { class: "tag" }, s.stage),
      s.used?.length ? s.used.map((e) => h("a", { class: "mem-use-entry", href: "#memory", onclick: () => closeDrawer() }, h("span", { class: "id" }, e.id), " ", e.title))
        : h("span", { class: "muted small" }, t("{0} kayıt verildi, hiçbiri kullanılmadı", s.shown))))));
}

// Stage runs, grouped per run (newest first): verdict, route, the result's summary, checks, cost.
function renderStages(det) {
  if (!det.stage_runs.length) return [empty(t("Henüz oturum yok."), "layers")];
  const groups = new Map();
  for (const s of det.stage_runs) {
    const key = s.run ?? "–";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }
  const out = [memoryUse(det)];
  for (const [n, runs] of [...groups.entries()].reverse()) {
    const cost = runs.reduce((a, s) => a + (s.cost_usd || 0), 0);
    out.push(h("div", { class: "run-group" },
      h("h3", { class: "run-group-h" }, n === "–" ? t("Oturumlar") : t("Koşu {0}", n), h("span", { class: "muted small" }, " · " + plural(t("{0} oturum"), runs.length) + " · " + money(cost))),
      h("ol", { class: "timeline" }, runs.map(stageCard))));
  }
  out.push(
    det.worktree ? h("div", { class: "path muted mono" }, icon("folder"), det.worktree) : "",
    det.prepare_log ? h("details", { class: "section" }, h("summary", {}, h("span", { class: "section-title" }, icon("terminal"), "prepare.log")), h("pre", { class: "log" }, det.prepare_log)) : "");
  return out;
}

const RETRY_CAUSE = { nudge: t("mesajınla yönlendirildi"), lost: t("süreci kayboldu"), quiet: t("sessiz kaldı"), timeout: t("süresi doldu") };
const COUNT_TR = [["important", t("önemli"), "bad"], ["nit", "nit", "mute"], ["pre_existing", t("önceden vardı"), "info"], ["unverified", t("doğrulanmadı"), "mute"]];

// How an interrupted session went on: "sessiz kaldı · yeniden denendi 1/2", or the user's nudge.
function retryPills(p) {
  const out = [];
  if (p.retry) {
    const cause = RETRY_CAUSE[p.retry.cause] || p.retry.cause;
    out.push(h("span", { class: "pill warn", title: p.retry.delay_s ? t("{0} sn bekleyip aynı oturumda sürdü", p.retry.delay_s) : t("aynı oturumda sürdü") },
      icon("retry"), p.retry.n ? t("{0} · yeniden denendi {1}/{2}", cause, p.retry.n, p.retry.max ?? "?") : cause));
  }
  if (p.retry_of) out.push(h("span", { class: "pill info", title: t("Yeni bir ziyaret değil: önceki oturum kaldığı yerden sürdü") }, t("↻ {0} sürdü", p.retry_of)));
  if (p.retry_exhausted) out.push(h("span", { class: "pill bad" }, t(p.retry_exhausted)));
  return out;
}

// Screenshots, recordings and logs the stage saved as evidence (served from inbox/artifacts only).
function evidence(list) {
  const shown = (list || []).filter((a) => a.url);
  if (!shown.length) return "";
  const kindOf = (a) => a.kind || (/\.(png|jpe?g|gif|webp|svg)$/i.test(a.path) ? "image" : /\.(mp4|webm|mov)$/i.test(a.path) ? "video" : "text");
  return h("div", { class: "evidence" }, shown.map((a) => {
    const caption = a.caption || a.path;
    const kind = kindOf(a);
    if (kind === "image") {
      return h("figure", { class: "ev" }, link(a.url, h("img", { src: a.url, loading: "lazy", alt: caption })), h("figcaption", { class: "muted small" }, caption));
    }
    if (kind === "video") return h("figure", { class: "ev" }, h("video", { src: a.url, controls: true, preload: "metadata" }), h("figcaption", { class: "muted small" }, caption));
    return link(a.url, h("span", { class: "ev-file small" }, icon("spec"), caption));
  }));
}

// "Sor": opens the ticket's chat with a question about this, ready to edit and send.
function askButton(tk, question, label) {
  if (!window.ChatView) return "";
  return h("button", { class: "small ghost", title: t("Sohbette sor (salt okunur bir oturum cevaplar)"),
    onclick: (e) => { e.stopPropagation(); ChatView.ask(rowOf(tk.target, tk.id) || tk, question, ctx); } }, icon("message"), label || t("Sor"));
}

function stageCard(p) {
  const steered = p.state === "killed" && /^nudge:/.test(p.kill_reason || p.interrupt || "");
  const tone = p.alive ? "live" : p.state === "ok" ? (p.verdict === "NEEDS_USER" ? "warn" : p.verdict === "FAILED" ? "bad" : "ok")
    : ["done", "ready-for-dev"].includes(p.state) ? "ok" : steered || p.retry ? "warn" : "bad";
  const open = drawer.tx?.phase === p.name;
  const toggle = () => { if (drawer.tx?.phase === p.name) closeTranscript(); else openTranscript(p.name); renderTabs(); };
  const files = h("div", { class: "stage-files" });
  for (const [kind] of Object.entries(drawer.files)) if (kind.startsWith(p.name + "/")) files.append(drawer.files[kind]);
  const fileBtn = (kind, label) => h("button", { class: "small ghost", onclick: (e) => { e.stopPropagation(); stageFile(p.name, kind, label, files); } }, label);
  const ended = p.result_error || ["failed", "lost", "timeout", "killed"].includes(p.state);
  const question = t("{0} #{1} (aşama oturumu {2}, {3}{4}): ", p.stage, p.visit ?? "?", p.name, STATE_TR[p.state] || p.state, p.verdict ? `, ${p.verdict}` : "")
    + (ended ? t("neden böyle bitti, ne düzeltir?") : t("ne yaptı, neye karar verdi ve neden?"));
  const counts = p.result?.counts || {};
  const nudge = p.nudged || p.nudge;
  return h("li", { class: `step ${tone}` + (drawer.highlight === p.name ? " row-highlight" : ""), id: `sr-${p.name}` },
    h("div", { class: "step-dot" }),
    h("div", { class: "step-body" },
      h("div", { class: "row tight wrap" },
        h("strong", {}, p.title || p.stage), p.visit > 1 || p.max_visits > 1 ? h("span", { class: "muted small", title: t("ziyaret / en çok") }, `#${p.visit ?? "?"}${p.max_visits ? `/${p.max_visits}` : ""}`) : "",
        h("span", { class: `pill ${tone === "live" ? "info" : tone}` }, steered ? t("yönlendirildi") : STATE_TR[p.state] || STATUS_TR[p.state] || p.state),
        p.verdict ? h("span", { class: `pill ${VERDICT_PILL[p.verdict] || "mute"}` }, p.verdict) : "",
        retryPills(p),
        p.route || p.next ? h("span", { class: "muted small", title: t("rota → sıradaki adım") }, [p.route ? t("rota {0}", p.route) : "", p.next ? `→ ${p.next}` : ""].filter(Boolean).join(" ")) : "",
        p.via?.from ? h("span", { class: "muted small" }, t("{0}{1} sonrası", p.via.from, p.via.route ? ` (${p.via.route})` : "")) : "",
        h("span", { class: "grow" }), h("span", { class: "type" }, p.model || "?"), p.effort ? h("span", { class: "type", title: t("efor") }, p.effort) : "",
        h("span", { class: "muted small" }, when(p.started_at))),
      nudge?.text ? h("div", { class: "nudge-note small" }, h("span", { class: "muted" }, `${p.nudged ? t("Yönlendirme") : t("Mesaj")} · ${nudge.by || "?"}: `), nudge.text) : "",
      COUNT_TR.some(([k]) => typeof counts[k] === "number") ? h("div", { class: "row tight wrap" },
        COUNT_TR.filter(([k]) => counts[k]).map(([k, label, cls]) => h("span", { class: `pill ${cls}` }, `${counts[k]} ${label}`)),
        COUNT_TR.every(([k]) => !counts[k]) ? h("span", { class: "pill ok" }, t("açık bulgu yok")) : "") : "",
      p.summary ? h("div", { class: "md stage-summary" }, MD.blocks(p.summary)) : "",
      evidence(p.artifacts),
      p.result_error ? h("pre", { class: "need-details" }, t(p.result_error)) : "",
      p.result_warnings.length ? h("div", { class: "row tight wrap" }, p.result_warnings.map((w) => h("span", { class: "pill warn" }, icon("alert"), t(w)))) : "",
      p.checks.length ? h("div", { class: "row tight wrap" }, h("span", { class: "muted small" }, t("kontroller")), p.checks.map(checkChip)) : "",
      h("div", { class: "row tight wrap" },
        chip("dollar", money(p.cost_usd)),
        chip("clock", p.duration_ms ? duration(p.duration_ms / 1000) : "-"),
        chip("message", plural(t("{0} tur"), p.turns ?? "-")),
        p.remote_unchanged === true ? chip("shield", t("remote'a dokunulmadı"), "ok") : p.remote_unchanged === false ? chip("alert", t("remote değişti: {0}", (p.remote_changed || []).join(", ") || "?"), "bad") : "",
        h("span", { class: "grow" }),
        askButton({ target: drawer.target, id: drawer.id }, question),
        h("button", { class: "small" + (open ? " on" : ""), "aria-expanded": String(open), onclick: toggle }, icon("activity"), "Transcript"),
        fileBtn("prompt", "Prompt"), fileBtn("result", t("Sonuç")), p.has_checks_log ? fileBtn("checks", "Checks log") : ""),
      files,
      open ? drawer.tx.el : ""));
}

async function stageFile(phase, kind, label, box) {
  const key = `${phase}/${kind}`;
  if (drawer.files[key]) { drawer.files[key].remove(); delete drawer.files[key]; return; }
  const d = drawer;
  try {
    const f = await api(`/api/stage/${d.target}/${d.id}/${phase}/${kind}`);
    if (drawer !== d) return;
    const el = h("details", { class: "section", open: true }, h("summary", {}, h("span", { class: "section-title" }, icon("spec"), `${label} · ${f.name}`)),
      kind === "prompt" ? h("div", { class: "section-body md" }, MD.blocks(f.text)) : h("pre", { class: "log small" }, f.text));
    d.files[key] = el;
    box.append(el);
  } catch (e) {
    toast(e.message, true);
  }
}

// Question rounds of every run: the open one with the answer form, answered ones with the answer.
function renderQuestions(det) {
  const tk = rowOf(drawer.target, drawer.id) || det;
  const out = [];
  if (det.status === "failed" && !det.running) {
    // After a stop or a failure the user can turn the work: the text becomes a decision and the
    // run continues where it stopped.
    const ta = h("textarea", { rows: 3, class: "answer-box", placeholder: t("Yön değiştiren not: ne farklı yapılmalı?"), "aria-label": t("Yön notu") });
    out.push(h("div", { class: "need failed wide" },
      h("div", { class: "need-kind" }, t("Koşu durdu")),
      det.error ? h("pre", { class: "need-details" }, det.error) : "",
      h("div", { class: "muted small" }, t("Not kararlarına eklenir ve koşu kaldığı aşamadan devam eder.")),
      ta,
      h("div", { class: "row" }, h("button", { class: "primary", onclick: async (e) => {
        if (!ta.value.trim()) return toast(t("Önce notunu yaz"), true);
        e.currentTarget.disabled = true;
        if (await act(tk, "answer", ta.value.trim())) ta.value = "";
      } }, icon("message"), t("Notla devam et")))));
  }
  if (!det.rounds.length) {
    if (!out.length) out.push(empty(t("Bu ticket için soru sorulmadı."), "message"));
    return out;
  }
  const openN = det.round?.n;
  for (const r of [...det.rounds].reverse()) {
    const isOpen = r.n === openN && !r.answered_at;
    const head = h("div", { class: "round-head" }, h("strong", {}, t("Tur {0}", r.n)),
      h("span", { class: "muted small" }, t("koşu {0} · {1} · {2}", r.run ?? "?", r.stage || "?", when(r.asked_at))),
      r.answered_at ? h("span", { class: "pill ok" }, t("cevaplandı")) : isOpen ? h("span", { class: "pill warn" }, t("seni bekliyor")) : h("span", { class: "pill mute" }, t("cevapsız")));
    if (isOpen) {
      out.push(h("article", { class: "round open" }, head, answerForm(tk, r),
        h("div", { class: "row end" }, askButton(tk, t("Soru turu {0} için cevap vermeme yardım et: her soru için ne önerirsin ve neden? Önce koda bak.", r.n), t("Yardım iste")))));
      continue;
    }
    out.push(h("article", { class: "round" }, head,
      r.intent_gap ? intentGapBox(tk, r.intent_gap) : "",
      r.context && r.context !== r.intent_gap?.summary ? h("div", { class: "answer-context md" }, MD.blocks(r.context)) : "",
      h("ol", { class: "questions" }, (r.questions || []).map((q, i) => h("li", { class: "q" },
        h("div", { class: "q-text" }, h("span", { class: "q-id" }, q.id || `Q${i + 1}`), h("div", { class: "md grow" }, MD.blocks(q.text || ""))),
        q.options?.length ? h("ul", { class: "q-options small" }, [...new Set(q.options)].map((o) => h("li", {}, o))) : "",
        q.recommendation ? h("div", { class: "q-rec small" }, h("span", { class: "muted" }, t("Önerilen: ")), q.recommendation) : ""))),
      r.answer !== null && r.answer !== undefined ? h("div", { class: "answer-block" },
        h("div", { class: "muted small" }, t("Cevap: {0}{1} · {2}", r.by || "?", r.via ? ` · ${r.via}` : "", when(r.answered_at))),
        h("pre", { class: "answer-text" }, r.answer)) : ""));
  }
  if (det.answers_md.trim()) out.push(h("details", { class: "section" }, h("summary", {}, h("span", { class: "section-title" }, icon("spec"), t("answers.md (ajanların gördüğü)"))),
    h("pre", { class: "log small" }, det.answers_md)));
  return out;
}

// --- session transcript (Aşamalar) ------------------------------------------------------------------
// One session at a time, read in pages from /api/transcript. The main agent and each subagent get
// their own tab; in the main agent's flow a subagent is one line that opens its tab. The element is
// kept in drawer.tx and re-attached when the stage list re-renders, so tabs, open boxes and
// scrolling survive a refresh; a live session gets new items appended every few seconds.

const TX_FILTERS = [["all", t("Hepsi")], ["text", t("Mesajlar")], ["tool", t("Komutlar")], ["error", t("Hatalar")]];

function openTranscript(phase) {
  closeTranscript();
  const tabs = h("div", { class: "tx-tabs", role: "tablist", "aria-label": t("Ajanlar") });
  const panes = h("div", { class: "tx-panes" });
  const foot = h("div", { class: "tx-foot" });
  const el = h("div", { class: "tx", "data-filter": "all" },
    tabs,
    h("div", { class: "tx-filters", role: "group", "aria-label": t("Transcript filtresi") },
      TX_FILTERS.map(([key, label]) => h("button", { class: "small" + (key === "all" ? " on" : ""), "data-key": key, onclick: (e) => {
        el.dataset.filter = key;
        for (const b of e.currentTarget.parentNode.children) b.classList.toggle("on", b === e.currentTarget);
      } }, label))),
    panes, foot);
  drawer.tx = { phase, el, tabs, panes, foot, pos: 0, tools: new Map(), agents: new Map(), active: "main", loading: false };
  addAgent(drawer.tx, "main", t("Ana ajan"), null);
  loadTranscript();
}

function closeTranscript() {
  clearTimeout(drawer?.tx?.timer);
  if (drawer) drawer.tx = null;
}

// A tab and its pane; `link` is the line left in the parent agent's flow.
function addAgent(tx, id, label, parentPane) {
  const pane = h("div", { class: "tx-list", role: "tabpanel", hidden: id !== tx.active });
  const status = h("span", { class: "tx-tab-status" });
  const tab = h("button", { class: "tx-tab" + (id === tx.active ? " on" : ""), role: "tab", "aria-selected": id === tx.active ? "true" : "false",
    title: label, onclick: () => showAgent(tx, id) }, id === "main" ? icon("user") : icon("layers"), h("span", { class: "tx-tab-label" }, label), status);
  const agent = { pane, tab, status, link: null };
  if (parentPane) {
    agent.link = h("button", { class: "tx-item tx-agent-link", onclick: () => showAgent(tx, id) },
      icon("layers"), h("span", { class: "grow" }, `↳ ${label}`), h("span", { class: "tx-link-status muted small" }, t("çalışıyor")), h("span", { class: "small" }, t("sekmede aç →")));
    parentPane.append(agent.link);
    setAgentStatus(agent, "live", t("çalışıyor"));
  }
  tx.agents.set(id, agent);
  tx.tabs.append(tab);
  tx.panes.append(pane);
  return agent;
}

function setAgentStatus(agent, state, text) {
  agent.status.className = `tx-tab-status ${state}`;
  agent.status.title = text;
  agent.link?.querySelector(".tx-link-status")?.replaceChildren(text);
}

function showAgent(tx, id) {
  tx.active = id;
  for (const [key, a] of tx.agents) {
    a.pane.hidden = key !== id;
    a.tab.classList.toggle("on", key === id);
    a.tab.setAttribute("aria-selected", key === id ? "true" : "false");
  }
  tx.agents.get(id)?.tab.scrollIntoView({ block: "nearest", inline: "nearest" });
  tx.el.scrollIntoView({ block: "start" });
}

async function loadTranscript() {
  const d = drawer, tx = d?.tx;
  if (!tx || tx.loading) return;
  tx.loading = true;
  let r;
  try {
    r = await api(`/api/transcript/${d.target}/${d.id}/${tx.phase}?pos=${tx.pos}`);
  } catch (e) {
    tx.loading = false;
    tx.foot.replaceChildren(h("span", { class: "muted small" }, e.message));
    return;
  }
  tx.loading = false;
  if (drawer !== d || d.tx !== tx) return;  // closed or another session opened meanwhile
  const body = $("d-body");
  const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
  for (const item of r.items) addTxItem(tx, item);
  tx.pos = r.pos;
  const main = tx.agents.get("main").pane;
  if (!main.children.length) main.append(empty(r.alive ? t("Oturum başlıyor…") : t("Bu oturumda kayıt yok."), "clock"));
  if (r.legacy && r.items.length) main.prepend(h("div", { class: "muted small" }, t("Bu oturum canlı olay kaydından önce koştu; yalnız son mesajı var.")));
  tx.tabs.hidden = tx.agents.size < 2;  // no subagents: no tab bar
  tx.foot.replaceChildren(
    r.more ? h("button", { class: "small", onclick: () => loadTranscript() }, t("Devamını yükle")) : "",
    r.alive && !r.more ? h("span", { class: "live-line" }, h("span", { class: "dot live" }), t("oturum sürüyor")) : "");
  if (r.alive && r.items.length && atBottom && d.tab === "stages") body.scrollTop = body.scrollHeight;
  if (r.alive && !r.more) tx.timer = setTimeout(loadTranscript, 3000);
}

function addTxItem(tx, item) {
  const main = tx.agents.get("main").pane;
  const where = (item.parent && tx.agents.get(item.parent)?.pane) || main;
  main.querySelector(":scope > .empty")?.remove();
  if (item.kind === "session") {
    where.append(h("div", { class: "tx-item tx-session" }, icon("play"), t("oturum başladı · {0}", item.model || "?")));
  } else if (item.kind === "text") {
    where.append(h("div", { class: "tx-item tx-text" }, h("div", { class: "md" }, MD.blocks(item.text))));
  } else if (item.kind === "prompt") {
    where.append(h("details", { class: "tx-item tx-prompt" }, h("summary", {}, icon("message"), t("subagent'a verilen görev")),
      h("pre", { class: "log small" }, item.text)));
  } else if (item.kind === "tool" && ["Agent", "Task"].includes(item.name)) {
    addAgent(tx, item.id, item.description || item.input?.subagent_type || "subagent", where);
    tx.tabs.hidden = false;
  } else if (item.kind === "tool" && item.name === "SubagentHandback" && typeof item.input?.message === "string") {
    // The subagent hands its report back through this call; its tool result only says so.
    const agent = item.parent && tx.agents.get(item.parent);
    if (agent) agent.handback = true;
    where.append(h("div", { class: "tx-item tx-report" }, h("div", { class: "muted small" }, t("subagent raporu")), h("div", { class: "md" }, MD.blocks(item.input.message))));
  } else if (item.kind === "tool") {
    const box = h("details", { class: "tx-item tx-tool" },
      h("summary", {}, h("span", { class: "mono" }, item.summary || item.name)),
      toolInput(item));
    tx.tools.set(item.id, box);
    where.append(box);
  } else if (item.kind === "result") {
    const agent = tx.agents.get(item.tool_id);
    if (agent) {  // a subagent's final report closes its tab
      setAgentStatus(agent, item.is_error ? "bad" : "ok", item.is_error ? t("hata") : t("bitti"));
      if (agent.handback && !item.is_error) return;  // the report is already in its tab
      agent.pane.append(h("div", { class: "tx-item tx-report" + (item.is_error ? " bad" : "") }, h("div", { class: "muted small" }, t("subagent raporu")), h("div", { class: "md" }, MD.blocks(item.content))));
      if (item.is_error) agent.link?.classList.add("error");
      return;
    }
    const owner = tx.tools.get(item.tool_id);
    const out = h("div", { class: "tx-result" + (item.is_error ? " bad" : "") },
      h("div", { class: "muted small" }, item.is_error ? t("hata") : t("sonuç"), item.truncated ? t(" · 16 KB'de kesildi") : ""),
      h("pre", { class: "log small" }, item.content || t("(boş)")));
    if (owner) {
      owner.append(out);
      if (item.is_error) owner.classList.add("error");
    } else where.append(out);
  } else if (item.kind === "task") {
    where.append(h("div", { class: "tx-item tx-session" }, icon("terminal"), t("arka plan {0}: {1}", item.status, item.summary)));
  } else if (item.kind === "end") {
    where.append(h("div", { class: "tx-item tx-end" + (item.is_error ? " bad" : "") },
      h("div", { class: "row tight" }, icon(item.is_error ? "alert" : "check"), h("strong", {}, t("oturum bitti")), chip("dollar", money(item.cost)), chip("message", plural(t("{0} tur"), item.turns ?? "-"))),
      item.text ? h("div", { class: "md" }, MD.blocks(item.text)) : ""));
  }
}

function toolInput(item) {
  const i = item.input || {};
  if (item.name === "Edit" || item.name === "MultiEdit") {
    const edits = i.edits || [{ old_string: i.old_string, new_string: i.new_string }];
    return h("div", { class: "tx-input" }, h("div", { class: "muted small mono" }, i.file_path || ""),
      edits.map((e) => h("div", { class: "tx-diff" }, h("pre", { class: "log small del" }, e.old_string ?? ""), h("pre", { class: "log small add" }, e.new_string ?? ""))));
  }
  if (item.name === "Write") return h("div", { class: "tx-input" }, h("div", { class: "muted small mono" }, i.file_path || ""), h("pre", { class: "log small add" }, i.content ?? ""));
  if (item.name === "Bash") return h("div", { class: "tx-input" }, i.description ? h("div", { class: "muted small" }, i.description) : "", h("pre", { class: "log small" }, i.command ?? ""));
  return h("div", { class: "tx-input" }, h("pre", { class: "log small" }, JSON.stringify(i, null, 2)));
}

function renderJobs(jobs) {
  if (!jobs.length) return [empty(t("Panelden başlatılmış job yok."), "terminal")];
  return [h("ol", { class: "timeline" }, jobs.slice().reverse().map((j) => {
    const state = j.state === "running" ? "live" : j.state === "ok" ? "ok" : "bad";
    return h("li", { class: `step ${state}` }, h("div", { class: "step-dot" }),
      h("div", { class: "step-body" },
        h("div", { class: "row tight" }, h("strong", { class: "mono" }, j.command),
          h("span", { class: `pill ${state === "ok" ? "ok" : state === "live" ? "info" : "bad"}` }, j.state + (j.exit !== null ? t(" · çıkış {0}", j.exit) : "")),
          h("span", { class: "grow" }), h("span", { class: "muted small" }, when(j.started_at))),
        j.tail ? h("pre", { class: "log small" }, j.tail) : ""));
  }))];
}

// --- Codex reviews --------------------------------------------------------------------------------

function renderReviews(det) {
  const tk = { target: drawer.target, id: drawer.id, pr: det.pr, branch: det.branch, base_branch: det.base_branch };
  const head = h("div", { class: "row" },
    det.status === "done" ? h("button", { class: "primary", onclick: () => act(tk, "review") }, icon("eye"), det.reviews.length ? t("Yeniden review") : t("Codex review başlat")) : "",
    h("span", { class: "muted small" }, t("Codex, dalı {0}'e göre inceler; dosyaları değiştirmez.", det.base_branch)));
  if (!det.reviews.length) return [head, empty(t("Henüz review yok."), "eye")];
  return [head, ...det.reviews.map((r, i) => {
    const pills = [
      h("span", { class: `pill ${r.state === "ok" ? "ok" : r.state === "running" ? "info" : "bad"}` }, r.state === "running" ? t("sürüyor") : r.state === "ok" ? t("bitti") : t("hata")),
      r.head ? chip("branch", r.head.slice(0, 7), "mono") : "",
      r.stale ? h("span", { class: "pill warn" }, t("eski: dal sonra değişti")) : "",
      r.posted_at ? h("span", { class: "pill ok" }, icon("pr"), t("PR'da")) : "",
      r.findings?.length ? h("span", { class: "pill warn" }, plural(t("{0} bulgu"), r.findings.length)) : "",
    ];
    const canPost = r.state === "ok" && det.pr && !r.posted_at && r.head && r.head === det.published_head;
    const actions = [];
    if (canPost) actions.push(h("button", { onclick: async () => {
      if (await confirmBox(t("Bu review {0} PR'ına yorum olarak eklensin mi?", det.pr))) act(tk, "post_review", r.id);
    } }, icon("pr"), t("PR'a yorum ekle")));
    const box = h("div", { class: "fix-box" });
    if (r.state === "ok" && det.status === "done" && !det.legacy && !r.stale && r.findings?.length) actions.push(h("button", { class: "primary", onclick: () => openFix(tk, r, box) }, icon("hammer"), t("Bulguları düzelttir")));
    return h("div", { class: "section review-card" + (i ? "" : " latest") },
      h("div", { class: "review-head" }, h("strong", {}, when(r.started_at)), ...pills, h("span", { class: "grow" }), ...actions),
      r.state === "error" ? h("pre", { class: "need-details" }, r.stderr_tail || t("codex hata verdi")) : "",
      r.text ? h("div", { class: "section-body md" }, MD.blocks(r.text)) : r.state === "running" ? empty(t("Codex inceliyor…"), "clock") : "",
      box);
  })];
}

// The findings to fix, each kept or dropped; the kept ones become fix tasks in the plan and a new
// run starts at the pipeline's implement stage.
function finding(f) {
  const prio = (f.priority || "").replace(/[[\]]/g, "");
  const head = `${prio ? `[${prio}] ` : ""}${f.title || ""}${f.location ? ` — ${f.location}` : ""}`.trim();
  const body = (f.body || "").trim().split("\n").map((l) => "  " + l).join("\n");
  return `- ${head || (f.text || "").trim()}${body.trim() ? "\n" + body : ""}`;
}

function openFix(tk, r, box) {
  const kept = new Map(r.findings.map((f) => [f, true]));
  box.replaceChildren(
    h("div", { class: "muted small" }, t("Düzeltilmesini istemediklerinin işaretini kaldır. Kalanlar plana görev olarak eklenir ve düzeltme koşusu başlar; bitince PR'ı güncellemen istenir.")),
    h("ul", { class: "checks" }, r.findings.map((f) => {
      const cb = h("input", { type: "checkbox", onchange: () => kept.set(f, cb.checked) });
      cb.checked = true;
      return h("li", { class: "ok" }, h("label", { class: "check finding" }, cb,
        f.priority ? h("span", { class: `pill ${/[01]/.test(f.priority) ? "bad" : /2/.test(f.priority) ? "warn" : "info"}` }, f.priority.replace(/[[\]]/g, "")) : "",
        h("span", {}, f.title || f.text), f.location ? h("span", { class: "muted small mono" }, f.location) : ""));
    })),
    h("div", { class: "row end" },
      h("button", { class: "ghost", onclick: () => box.replaceChildren() }, t("Vazgeç")),
      h("button", { class: "primary", onclick: async () => {
        const chosen = r.findings.filter((f) => kept.get(f));
        if (!chosen.length) return toast(t("Düzeltilecek bulgu kalmadı"), true);
        if (!(await confirmBox(t("{0}: {1} bulgu plana görev olarak eklensin ve düzeltme koşusu başlasın mı?\nBirkaç dolar tutabilir; sonra PR'ı güncellemen istenir.", tk.id, chosen.length)))) return;
        if (await act(tk, "fix", chosen.map(finding).join("\n"), { review: r.id })) box.replaceChildren();
      } }, icon("hammer"), t("Gönder ve koştur"))));
}

// --- activity -----------------------------------------------------------------------------------

function lineClass(line) {
  if (line.startsWith("==")) return "phase";
  if (line.trimStart().startsWith("! ") || line.includes("↳ ! ")) return "err";
  if (line.startsWith("» ")) return "say";
  if (line.startsWith("subagent")) return "agent";
  if (line.startsWith("  ↳")) return "sub";
  if (line.startsWith("$ ")) return "cmd";
  if (line.startsWith("edit ")) return "edit";
  if (line.startsWith("session")) return "sys";
  return "plain";
}

function logLine(line) {
  return h("div", { class: `ll ${lineClass(line)}` }, line);
}

function renderActivity() {
  const d = drawer;
  const select = h("select", { "aria-label": t("Oturum"), onchange: (e) => { d.phase = e.target.value || null; d.follow = !e.target.value; d.offset = 0; renderActivity(); } },
    h("option", { value: "" }, t("en yeni oturum (takip et)")));
  const allBox = h("input", { type: "checkbox", onchange: (e) => { d.all = e.target.checked; d.offset = 0; renderActivity(); } });
  allBox.checked = d.all;
  const log = h("div", { class: "log", id: "activity-log" });
  const legend = h("div", { class: "legend muted small" },
    h("span", { class: "ll cmd" }, t("$ komut")), h("span", { class: "ll edit" }, t("edit dosya")), h("span", { class: "ll agent" }, t("alt-ajan")),
    h("span", { class: "ll say" }, t("» ajan")), h("span", { class: "ll err" }, t("! hata")));
  $("d-body").replaceChildren(h("div", { class: "row" }, select, h("label", { class: "check" }, allBox, t(" her şey (okumalar, alt-ajan içi)")), h("span", { class: "grow" }), legend), log);
  clearTimeout(d.timer);
  // A poll still in flight from the previous view must not touch this one's offset or log.
  d.gen = (d.gen || 0) + 1;
  d.shownPhase = null;
  pollActivity(select, log, d.gen);
}

async function pollActivity(select, log, gen) {
  const d = drawer;
  const current = () => drawer === d && d.tab === "activity" && d.gen === gen;
  if (!d || !current()) return;
  const q = new URLSearchParams({ offset: d.offset, all: d.all ? "1" : "0" });
  if (d.phase) q.set("phase", d.phase);
  try {
    const a = await api(`/api/activity/${d.target}/${d.id}?${q}`);
    if (!current()) return;
    if (select.options.length - 1 !== a.phases.length) {
      const keep = d.phase || "";
      select.replaceChildren(h("option", { value: "" }, t("en yeni oturum (takip et)")), ...a.phases.map((p) => h("option", { value: p }, p)));
      select.value = keep;
    }
    if (d.follow && a.phase && d.shownPhase && d.shownPhase !== a.phase) {
      d.offset = 0; // the run moved on to the next stage: start the new session's lines
      log.replaceChildren();
    }
    if (a.reset || d.offset === 0) log.replaceChildren(a.header ? logLine(a.header) : "");
    d.shownPhase = a.phase;
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    for (const line of a.lines) log.append(logLine(line));
    if (a.footer && !log.querySelector(".end")) log.append(h("div", { class: "ll phase end" }, a.footer));
    if (!a.phases.length) log.replaceChildren(empty(t("Henüz oturum yok (koşu hazırlanıyor olabilir)."), "clock"));
    d.offset = a.offset;
    if (atBottom) log.scrollTop = log.scrollHeight;
  } catch (e) {
    if (!current()) return;
    log.append(h("div", { class: "ll err" }, `! ${e.message}`));
  }
  d.timer = setTimeout(() => pollActivity(select, log, gen), 1500);
}

// --- onboarding -----------------------------------------------------------------------------------

let candidates = [];

$("toggle-onboard").addEventListener("click", async () => {
  $("onboard").hidden = !$("onboard").hidden;
  if ($("onboard").hidden) return;
  $("detect-path").focus();
  $("repo-list").replaceChildren(h("p", { class: "muted small" }, t("repolar aranıyor…")));
  try {
    candidates = (await api("/api/targets/candidates")).candidates;
  } catch (e) {
    candidates = [];
  }
  renderCandidates();
});

// Repositories under ~/Developer to pick from; typing in the path box filters them.
function renderCandidates() {
  const q = $("detect-path").value.trim().toLowerCase();
  const shown = candidates.filter((c) => !q || c.name.toLowerCase().includes(q) || c.path.toLowerCase().includes(q));
  if (!candidates.length) { $("repo-list").replaceChildren(); return; }
  $("repo-list").replaceChildren(
    h("div", { class: "muted small" }, t("~/Developer altındaki repolar ({0}/{1}) · birini seç ya da yolu yaz", shown.length, candidates.length)),
    h("div", { class: "repos" }, shown.map((c) => h("button", {
      type: "button", class: "repo" + (c.target ? " added" : ""), disabled: !!c.target, title: c.path,
      onclick: () => { $("detect-path").value = c.path; detect(); },
    },
      icon(c.target ? "check" : "folder"), h("span", { class: "repo-name" }, c.name),
      c.target ? h("span", { class: "pill ok" }, t("eklendi")) : "",
      !c.target && c.project_doc ? h("span", { class: "pill info" }, "PROJECT.md") : "",
      !c.target && !c.remote ? h("span", { class: "pill warn" }, t("remote yok")) : "")),
    ),
  );
}

// The form's model: the detected spec fields, the proposal's lines (each {…, why, source, verify}) and the background
// job (agent and/or verification) whose result replaces the lines when it ends. Nothing is saved before "Config'e ekle".
const ob = { found: null, spec: null, proposal: null, request: null, view: null, timer: null, gen: 0, prUrl: null };
const OB_PHASE = { queued: t("sırada"), start: t("başlıyor"), agent: t("ajan inceliyor"), verify: t("deneniyor"), repair: t("ajan onarıyor"),
  "verify-again": t("yeniden deneniyor"), done: t("bitti") };

async function detect() {
  const out = $("onboard-result");
  clearTimeout(ob.timer);
  ob.gen += 1;
  ob.request = null;
  ob.view = null;
  ob.prUrl = null;
  out.replaceChildren(h("p", { class: "muted" }, t("inceleniyor…")));
  let found;
  try {
    found = await api("/api/targets/detect", { path: $("detect-path").value });
  } catch (err) {
    out.replaceChildren(h("p", { class: "error-banner" }, err.message));
    return;
  }
  ob.found = found;
  ob.spec = { ...found.spec };
  ob.proposal = found.proposal;
  renderOnboard();
  // Free and in a throwaway clone: try what was found at once; the badges fill in when it ends.
  if (ob.proposal.prepare.length || ob.proposal.checks.length) startOnboardJob(false);
}

$("detect-path").addEventListener("input", renderCandidates);
$("detect-form").addEventListener("submit", (e) => { e.preventDefault(); detect(); });

function verifyBadge(v) {
  if (v && v.status === "pass") return h("span", { class: "pill ok ob-badge", title: v.reason || "" }, icon("check"), v.seconds != null ? t("denendi · {0} sn", Math.round(v.seconds)) : t("denendi"));
  if (v && v.status === "fail") return h("span", { class: "pill bad ob-badge", title: v.reason }, icon("alert"), t("geçmedi: {0}", v.reason || t("hata")));
  return h("span", { class: "pill mute ob-badge", title: (v && v.reason) || "" }, t("denenmedi"));
}

// One line of the proposal: its inputs (editing one drops its badge: the line is no longer what was tried), the badge,
// where it came from, why, and what a failed or messy try printed.
function obLine(kind, item, index) {
  const v = item.verify;
  let badgeEl = verifyBadge(v);
  const edit = (field) => (e) => {
    item[field] = e.target.value;
    item.source = "user";
    if (item.verify) {
      item.verify = null;
      const fresh = verifyBadge(null);
      badgeEl.replaceWith(fresh);
      badgeEl = fresh;
    }
  };
  const inputs = kind === "prepare"
    ? [h("input", { class: "grow mono", value: item.run, "aria-label": t("prepare komutu"), oninput: edit("run") })]
    : kind === "env"
      ? [h("input", { class: "mono ob-key", value: item.key, "aria-label": t("değişken"), oninput: edit("key") }),
         h("input", { class: "grow mono", value: item.value, "aria-label": t("değer"), oninput: edit("value") })]
      : [h("input", { class: "mono ob-key", value: item.name, "aria-label": t("kontrol adı"), oninput: edit("name") }),
         h("input", { class: "grow mono", value: item.run, "aria-label": t("kontrol komutu"), oninput: edit("run") })];
  return h("div", { class: "ob-line" },
    h("div", { class: "row tight" }, ...inputs, badgeEl,
      h("button", { type: "button", class: "small ghost", title: t("satırı sil"), onclick: () => { ob.proposal[kind].splice(index, 1); renderOnboard(); } }, icon("x"))),
    h("div", { class: "ob-why muted small" },
      item.source === "agent" ? h("span", { class: "pill info" }, t("ajan")) : item.source === "user" ? h("span", { class: "pill" }, t("elle")) : "",
      item.suggested ? h("span", { class: "pill warn" }, t("öneri · sahte değer")) : "",
      item.why ? h("span", {}, item.why) : ""),
    v && v.left_files && v.left_files.length ? h("div", { class: "warn-text small" }, t("bıraktığı dosyalar silindi: {0}", v.left_files.join(", "))) : "",
    v && v.status === "fail" && v.tail ? h("details", { class: "ob-tail" }, h("summary", { class: "small muted" }, t("çıktı")), h("pre", {}, v.tail)) : "",
  );
}

function obSection(kind, title, blank) {
  return h("div", { class: "ob-section" },
    h("div", { class: "row" }, h("h3", { class: "grow" }, title),
      h("button", { type: "button", class: "small ghost", onclick: () => { ob.proposal[kind].push({ ...blank, why: "", source: "user" }); renderOnboard(); } }, icon("plus"), t("satır"))),
    ob.proposal[kind].length ? ob.proposal[kind].map((item, i) => obLine(kind, item, i)) : h("p", { class: "muted small" }, t("yok")));
}

function obJobStatus() {
  const v = ob.view;
  if (!ob.request && !v) return "";
  if (!v) return h("p", { class: "muted small" }, t("başlatılıyor…"));
  const parts = [];
  if (v.running || (v.phase && v.phase !== "done")) {
    parts.push(h("div", { class: "row tight" }, h("span", { class: "pill info" }, icon("clock"), OB_PHASE[v.phase] || v.phase), h("span", { class: "muted small" }, v.message || "")));
    for (const line of v.live_lines || []) parts.push(h("div", { class: "ll small mono" }, line));
  }
  if (v.lost) parts.push(h("p", { class: "error-banner small" }, t("iş yarıda kaldı: {0}", (v.job && v.job.tail) || t("çıktı yok"))));
  if (v.error) parts.push(h("p", { class: "error-banner small" }, t(v.error)));
  if (v.phase === "done" && v.summary) {
    const s = v.summary;
    parts.push(h("p", { class: s.ok ? "small" : "warn-text small" },
      s.error ? t("doğrulama yapılamadı: {0}", s.error) : s.ok ? t("hepsi denendi, geçti ({0} sn)", Math.round(s.seconds)) : t("{0} satır geçmedi ({1} sn)", s.failures.length, Math.round(s.seconds)),
      v.cost_usd ? t(" · ajan maliyeti {0}", money(v.cost_usd)) : ""));
  }
  return h("div", { class: "ob-job" }, parts);
}

function collectSpec() {
  const s = ob.spec;
  const p = ob.proposal;
  return {
    name: s.name.trim(), path: s.path, base_branch: (s.base_branch || "").trim(), remote: "origin",
    github_repo: (s.github_repo || "").trim() || undefined, gh_user: (s.gh_user || "").trim() || undefined, mode: s.mode || "personal",
    prepare: p.prepare.map((x) => x.run.trim()).filter(Boolean),
    env: Object.fromEntries(p.env.filter((x) => x.key.trim()).map((x) => [x.key.trim(), x.value])),
    checks: p.checks.filter((x) => x.name.trim() && x.run.trim()).map((x) => ({ name: x.name.trim(), run: x.run.trim(), ...(x.timeout ? { timeout: x.timeout } : {}) })),
    ...(p.check_sandbox?.length ? { check_sandbox: p.check_sandbox } : {}),
  };
}

async function startOnboardJob(agent) {
  const gen = ob.gen;
  clearTimeout(ob.timer);
  ob.view = null;
  ob.request = "starting";
  renderOnboard();
  try {
    const r = await api("/api/onboard", { path: ob.spec.path, proposal: ob.proposal, agent, name: ob.spec.name });
    if (gen !== ob.gen) return;
    ob.request = r.request;
    pollOnboard(gen);
  } catch (err) {
    ob.request = null;
    renderOnboard();
    toast(err.message, true);
  }
}

async function pollOnboard(gen) {
  if (gen !== ob.gen || !ob.request) return;
  try {
    ob.view = await api(`/api/onboard/${ob.request}`);
  } catch (err) {
    ob.view = { error: err.message, phase: "done" };
  }
  if (gen !== ob.gen) return;
  const v = ob.view;
  const finished = v.phase === "done" || v.lost;
  if (finished) {
    if (v.proposal) ob.proposal = v.proposal;
    ob.request = null;
  }
  renderOnboard();
  if (!finished) ob.timer = setTimeout(() => pollOnboard(gen), 1500);
}

async function openAgentsPr(text) {
  const s = ob.spec;
  const company = ob.found.company;
  const question = company
    ? t("DİKKAT: {0} bir ŞİRKET reposu.\n{1} reposunda, {2} hesabıyla, {3} üzerine AGENTS.md için draft PR açılsın mı?", s.path, s.github_repo, s.gh_user, s.base_branch)
    : t("{0} reposunda {1} üzerine AGENTS.md için draft PR açılsın mı? (geçici worktree'de docs/agents-md dalı)", s.github_repo, s.base_branch);
  if (!(await confirmBox(question))) return;
  try {
    const r = await api("/api/onboard/agents-md", { path: s.path, text, gh_user: s.gh_user, github_repo: s.github_repo, base_branch: s.base_branch,
      company_confirmed: company === true });
    ob.prUrl = r.url;
    toast(t("draft PR açıldı: {0}", r.url));
    renderOnboard();
  } catch (err) {
    toast(err.message, true);
  }
}

function renderOnboard() {
  const found = ob.found;
  const s = ob.spec;
  const busy = !!ob.request;
  const field = (key) => h("input", { id: `ob-${key}`, value: s[key] || "", oninput: (e) => { s[key] = e.target.value; } });
  const checks = h("ul", { class: "checks" }, ...found.checks.map((c) => h("li", { class: c.ok ? "ok" : c.blocking ? "bad" : "warn" },
    icon(c.ok ? "check" : "alert"), h("div", {}, h("div", {}, t(c.message)), c.fix ? h("div", { class: "fix" }, t(c.fix)) : ""))));
  const add = h("button", {
    class: "primary", onclick: async () => {
      const spec = collectSpec();
      if (!(await confirmBox(t("[targets.{0}] config dosyasının sonuna eklensin mi?\nÖnce yedek alınır.", spec.name)))) return;
      try {
        const r = await api("/api/targets", { spec });
        toast(t("{0} eklendi · yedek: {1}", r.added, r.backup));
        ob.gen += 1;
        clearTimeout(ob.timer);
        $("onboard-result").replaceChildren();
        $("onboard").hidden = true;
        refresh();
      } catch (err) {
        toast(err.message, true);
      }
    },
  }, icon("plus"), t("Config'e ekle"));
  if (!found.can_add) add.disabled = true;
  const agentBtn = h("button", { type: "button", disabled: busy, title: t("Salt-okur bir ajan oturumu (en çok $1): eksikleri doldurur, şüpheli satırları düzeltir, AGENTS.md taslağı yazar; ardından doğrulama ve gerekirse bir onarım turu"),
    onclick: () => startOnboardJob(true) }, icon("zap"), t("Ajanla doldur"));
  const verifyBtn = h("button", { type: "button", disabled: busy, title: t("Geçici bir klonda prepare'i ve kontrolleri motorun koşturduğu gibi dener"),
    onclick: () => startOnboardJob(false) }, icon("play"), t("Doğrula"));
  const notes = ob.proposal.notes || [];
  let agentsBox = "";
  if (!found.has_rules && ob.proposal.agents_md) {
    const area = h("textarea", { id: "ob-agents-md", rows: 14, oninput: (e) => { ob.proposal.agents_md = e.target.value; } });
    area.value = ob.proposal.agents_md;
    agentsBox = h("div", { class: "ob-section ob-agents" },
      h("div", { class: "row" }, h("h3", { class: "grow" }, t("AGENTS.md taslağı")), found.company ? h("span", { class: "pill warn" }, t("şirket reposu")) : ""),
      h("p", { class: "muted small" }, t("Repoda AGENTS.md yok; ajan bunu yazdı. Düzenleyebilirsin. 'Draft PR aç' geçici bir worktree'de docs/agents-md dalını açar, commit'ler, push'lar ve draft PR açar; senin checkout'una dokunmaz.")),
      area,
      // Once the PR is open the button goes: a second click would push the same branch again.
      h("div", { class: "row end" }, ob.prUrl ? link(ob.prUrl, icon("pr"), t("Draft PR açıldı"))
        : h("button", { type: "button", disabled: !s.github_repo || !s.gh_user, onclick: () => openAgentsPr(area.value) }, icon("pr"), t("Draft PR aç"))));
  }
  $("onboard-result").replaceChildren(
    checks,
    h("div", { class: "onboard-grid" },
      h("label", { for: "ob-name" }, t("ad")), field("name"),
      h("label", {}, t("yol")), h("div", { class: "mono" }, s.path),
      h("label", { for: "ob-base_branch" }, "base branch"), field("base_branch"),
      h("label", { for: "ob-github_repo" }, "github repo"), field("github_repo"),
      h("label", { for: "ob-gh_user" }, t("gh hesabı")), field("gh_user"),
      h("label", { for: "ob-mode" }, t("mod")), modeSelect(s, found),
    ),
    h("div", { class: "row ob-tools" }, agentBtn, verifyBtn, h("span", { class: "muted small grow" }, t("Kaydetmeden önce denenir; hiçbir şey 'Config'e ekle'ye kadar yazılmaz."))),
    obJobStatus(),
    obSection("prepare", t("prepare (koşudan önce, ağ açık)"), { run: "" }),
    obSection("env", t("test ortamı"), { key: "", value: "" }),
    obSection("checks", t("kontroller (implement sonrası, sandbox'ta)"), { name: "", run: "" }),
    ob.proposal.check_sandbox?.length ? h("p", { class: "muted small" }, t("Sandbox ön ayarı: {0} ", ob.proposal.check_sandbox.join(", "))
      + t("(xcode: SwiftPM önbelleğine yazma ve simülatörün test soketi; başka hiçbir şey açılmaz). Config'e check_sandbox olarak yazılır.")) : "",
    notes.length ? h("div", { class: "ob-section" }, h("h3", {}, t("notlar")), h("ul", { class: "small" }, notes.map((n) => h("li", {}, n)))) : "",
    agentsBox,
    h("p", { class: "muted small" }, t("Yer tutucular: {run_id}, {state}, {worktree}, {checkout} (ana checkout: git'in yok saydığı dosyaları prepare'de oradan kopyalamak için).")),
    h("div", { class: "row end" }, found.can_add ? "" : h("span", { class: "error-banner small" }, t("Kırmızı maddeyi çözmeden eklenemez.")), add),
  );
}

// The project's mode: controlled (every plan waits for approval, dependants ask before they start, no PR replies) is
// proposed for a company repository, personal elsewhere.
function modeSelect(s, found) {
  const sel = h("select", { id: "ob-mode", title: CONTROLLED_TITLE, onchange: (e) => { s.mode = e.target.value; } },
    h("option", { value: "controlled" }, t("Kontrollü")), h("option", { value: "personal" }, t("Kişisel")));
  sel.value = s.mode || (found.company ? "controlled" : "personal");
  s.mode = sel.value;
  return sel;
}

// --- stats ----------------------------------------------------------------------------------------

const tokens = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n || 0));
const OUTCOME_TR = { ready: t("hazır"), needs_user: t("soru"), failed: t("başarısız"), killed: t("durduruldu"), timeout: t("zaman aşımı"), lost: t("kayıp"), running: t("sürüyor"),
  done: t("bitti (v1)"), "ready-for-dev": t("spec hazır (v1)"), blocked: t("takıldı (v1)") };
// Outcome colours: finished quiet, waiting on you orange, broken red; stage colours are their own categorical set,
// apart from the status colours and the accent.
const OUTCOME_COLOR = { ready: "var(--text)", done: "var(--text)", "ready-for-dev": "var(--text-2)", needs_user: "var(--accent)", blocked: "var(--warn)",
  running: "var(--info)", failed: "var(--bad)", killed: "var(--border-strong)", timeout: "var(--bad)", lost: "var(--bad)" };
const STAGE_COLORS = ["#8e9bff", "#3fc9b5", "#e8b04b", "#ec79a8", "#b794f6", "#7fd3f7", "#c9a27e", "#6b7180"];

const CHART_H = 240;  // px: the daily chart's tallest bar
const bar = (pct, color, delay) => h("div", { class: "hbar" }, h("div", { class: "hbar-fill", style: `width:${Math.max(0, Math.min(100, pct)).toFixed(1)}%;background:${color};animation-delay:${delay || 0}s` }));

async function loadStats() {
  loadStats.at = Date.now();
  let s;
  try {
    s = await api("/api/stats");
  } catch (e) {
    $("stats-body").replaceChildren(h("p", { class: "error-banner" }, e.message));
    return;
  }
  const kpi = (label, value, sub, cls) => h("div", { class: "kpi" + (cls ? " " + cls : "") }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), h("div", { class: "sub" }, sub || ""));
  const stageNames = s.by_stage.map((g) => g.name);
  const color = (name) => STAGE_COLORS[Math.max(0, stageNames.indexOf(name)) % STAGE_COLORS.length];
  const max = Math.max(0.01, ...s.daily.map((d) => d.cost));
  const today = s.daily.at(-1);
  const top = today && Object.entries(today.stages).sort((x, y) => y[1] - x[1])[0];
  const week = s.daily.slice(-7).reduce((n, d) => n + d.sessions, 0);
  const month = s.daily.reduce((n, d) => n + d.sessions, 0);
  const bars = h("div", { class: "chart" },
    h("div", { class: "chart-axis" }, h("span", {}, money(max)), h("span", {}, money(max / 2)), h("span", {}, "$0")),
    h("div", { class: "chart-bars" }, s.daily.map((d, i) => {
      const isToday = i === s.daily.length - 1;
      const title = `${d.day}: ${money(d.cost)} · ${Object.entries(d.stages).map(([k, v]) => `${k} ${money(v)}`).join(" · ") || t("maliyet yok")} · ${plural(t("{0} oturum"), d.sessions)}`;
      return h("div", { class: "chart-col" + (isToday ? " today" : ""), title },
        h("div", { class: "chart-stack", style: `animation-delay:${(i * 0.03).toFixed(2)}s` },
          d.cost ? stageNames.filter((n) => d.stages[n]).map((n) => h("div", { class: "seg", style: `height:${Math.max(2, (d.stages[n] / max) * CHART_H).toFixed(1)}px;background:${color(n)}` }))
            : h("div", { class: "seg empty" })),
        h("div", { class: "chart-label" }, String(Number(d.day.slice(8)))));
    })));
  const card = (title, sub, ...kids) => h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, title), sub ? h("span", { class: "muted small" }, sub) : ""), ...kids);
  const maxTarget = Math.max(0.01, ...s.by_target.map((g) => g.cost));
  const maxStage = Math.max(0.01, ...s.by_stage.map((g) => g.cost));
  const totalModels = Math.max(0.01, s.models.reduce((n, m) => n + m.cost, 0));
  const outcomes = Object.entries(s.outcomes).sort((x, y) => y[1] - x[1]);
  const totalOut = Math.max(1, outcomes.reduce((n, [, v]) => n + v, 0));
  const avg = (g, k) => (g.sessions ? g[k] / g.sessions : 0);
  const MODEL_SHADES = ["var(--text)", "var(--muted)", "var(--border-strong)"];
  // Delivery: merged PRs over the last 30 days. A subscription login pays no dollars: its sessions' cost is shown as
  // the quota it used (API prices), "kota".
  const dl = s.delivery;
  const units = new Set([...dl.by_target, dl.overall].filter((r) => r.merged).map((r) => r.unit));
  const perLabel = units.has("usd") && !units.has("kota") ? t("PR başına maliyet") : units.has("usd") ? t("PR başına kota / maliyet") : t("PR başına kota");
  const perPr = (r) => (r.cost_per_pr === null ? "–" : r.unit === "usd" ? money(r.cost_per_pr) : t("≈{0} kota", money(r.cost_per_pr)));
  const dash = (v, f) => (v === null || v === undefined ? "–" : f(v));
  const deliveryRows = [...dl.by_target, ...(dl.by_target.length > 1 ? [dl.overall] : [])];
  const delivery = card(t("Teslim"), t("birleşen PR'lar · son {0} gün", dl.days),
    h("div", { class: "delivery-table" },
      [t("proje"), t("birleşen PR"), t("istek olmadan"), t("PR başına tur"), perLabel].map((x, i) => h("span", { class: "th" + (i ? " num" : "") }, x)),
      deliveryRows.map((r) => [
        r === dl.overall ? h("strong", {}, t("hepsi")) : h("span", {}, r.name),
        h("span", { class: "num mono" }, String(r.merged)),
        h("span", { class: "num mono", title: r.merged ? t("{0}/{1} PR, PR açıldıktan sonra değişiklik istenmeden birleşti", r.clean, r.merged) : "" }, dash(r.clean_share, (v) => pctText(Math.round(v * 100)))),
        h("span", { class: "num mono" }, dash(r.rounds_per_pr, (v) => v.toFixed(2))),
        h("span", { class: "num mono" }, perPr(r))])),
    units.has("kota") ? h("p", { class: "muted small card-foot" }, t("Abonelikli hesapta para ödenmez: değer, oturumların API fiyatıyla harcadığı kotanın karşılığı.")) : "",
    !dl.overall.merged ? h("p", { class: "muted small" }, t("Son {0} günde birleşen PR yok.", dl.days)) : "");
  // The weekly report (Monday's file, or this week so far), folded: its numbers are mostly above, its lists are not.
  const report = h("div", {});
  api(`/api/report?lang=${I18N.lang}`).then((r) => report.replaceChildren(card(t("Haftalık rapor"), r.week || t("bu hafta, şimdiye kadar"),
    h("details", { class: "weekly-report" }, h("summary", { class: "small" }, t("Raporu aç")),
      h("div", { class: "md" }, MD.blocks(r.markdown.replace(/^# .*\n+/, ""))))))).catch(() => {});
  $("stats-body").replaceChildren(
    h("div", { class: "kpis" },
      kpi(t("Bugün"), money(s.totals.today), today ? plural(t("{0} oturum"), today.sessions) + (top ? t(" · en çok {0}", top[0]) : "") : "", "now"),
      kpi(t("Son 7 gün"), money(s.totals.week), plural(t("{0} oturum"), week)), kpi(t("Son 30 gün"), money(s.totals.month), plural(t("{0} oturum"), month)),
      kpi(t("Toplam"), money(s.totals.all), t("{0} oturum · {1} ticket", s.totals.sessions, s.totals.tickets))),
    delivery, report,
    h("div", { class: "stats-row" },
      card(t("Günlük maliyet"), t("aşamaya göre · son {0} gün", s.daily.length), bars,
        h("div", { class: "legend small" }, stageNames.map((n) => h("span", { class: "legend-item" }, h("span", { class: "sw", style: `background:${color(n)}` }), n)))),
      card(t("Projeye göre"), "", h("div", { class: "hbars" }, s.by_target.map((g, i) => h("div", { class: "hbar-row" },
        h("div", { class: "hbar-top" }, h("span", {}, g.name), h("span", { class: "mono" }, money(g.cost))),
        bar((g.cost / maxTarget) * 100, "var(--text)", i * 0.08),
        h("div", { class: "hbar-sub" }, t("{0} oturum · {1} tur · {2}", g.sessions, g.turns, duration(g.duration_ms / 1000)),
          s.budgets[g.name] ? t(" · oturum başı bütçe {0}", money(s.budgets[g.name])) : "")))),
        Object.values(s.budgets).some(Boolean) ? "" : h("p", { class: "muted small card-foot" }, t("Proje bütçesi tanımlı değil.")))),
    h("div", { class: "stats-row" },
      card(t("Aşamalar"), t("maliyet · oturum · ortalama süre"),
        h("div", { class: "stage-table" },
          h("span", { class: "th" }, t("aşama")), h("span", { class: "th" }, t("maliyet")), h("span", { class: "th num" }, t("toplam")), h("span", { class: "th num" }, t("oturum")),
          h("span", { class: "th num" }, t("ort. maliyet")), h("span", { class: "th num" }, t("ort. süre")),
          s.by_stage.map((g, i) => [
            h("span", { class: "stage-name" }, h("span", { class: "sw", style: `background:${color(g.name)}` }), g.name),
            bar((g.cost / maxStage) * 100, color(g.name), i * 0.05),
            h("span", { class: "num mono" }, money(g.cost)), h("span", { class: "num mono muted" }, g.sessions),
            h("span", { class: "num mono muted" }, money(avg(g, "cost"))), h("span", { class: "num mono muted" }, duration(avg(g, "duration_ms") / 1000))]))),
      h("div", { class: "stats-side" },
        card(t("Modeller"), "",
          h("div", { class: "split-bar" }, s.models.map((m, i) => h("div", { style: `width:${(m.cost / totalModels) * 100}%;background:${MODEL_SHADES[i % 3]}` }))),
          h("div", { class: "model-list" }, s.models.map((m, i) => h("div", { class: "model-row" },
            h("span", { class: "sw", style: `background:${MODEL_SHADES[i % 3]}` }),
            h("div", { class: "grow" }, h("div", { class: "mono" }, m.name),
              h("div", { class: "muted small mono" }, t("{0} çıktı · {1} girdi · {2} cache okuma · {3} yazma", tokens(m.output), tokens(m.input), tokens(m.cache_read), tokens(m.cache_write)))),
            h("span", { class: "mono" }, money(m.cost)))))),
        card(t("Oturum sonuçları"), String(totalOut),
          h("div", { class: "split-bar" }, outcomes.map(([k, v]) => h("div", { title: `${OUTCOME_TR[k] || k}: ${v}`, style: `width:${(v / totalOut) * 100}%;background:${OUTCOME_COLOR[k] || "var(--bad)"}` }))),
          h("div", { class: "outcome-list" }, outcomes.map(([k, v]) => h("span", { class: "outcome" },
            h("span", { class: "sw", style: `background:${OUTCOME_COLOR[k] || "var(--bad)"}` }), OUTCOME_TR[k] || k, h("span", { class: "mono" }, String(v)))))))),
    card(t("En pahalı ticket'lar"), "", h("div", { class: "top-tickets" }, s.top_tickets.map((tk) => h("button", { class: "top-ticket", type: "button",
      onclick: () => { const row = rowOf(tk.target, tk.id); if (row) openDrawer(row, "stages"); else toast(t("{0} artık listede değil", tk.id), true); } },
      h("span", { class: "top-cost" }, money(tk.cost)), h("span", { class: "mono small" }, `${tk.target} · ${tk.id}`), h("span", { class: "muted small" }, plural(t("{0} oturum"), tk.sessions)))))),
  );
}

// --- memory: what each project remembers -------------------------------------------------------------
// Per project (memory.py): proposals from question-round answers and from the retro after a ticket ends,
// each with the evidence it came from, wait here; nothing reaches the memory files until it is approved
// (as proposed or edited). Below them, the entries the stage sessions are given, with how often they were used.

const SOURCE_TR = {
  user_comment: t("senin review yorumun"), overruled_finding: t("review eledi, sen düzelttirdin"),
  recurring_finding: t("turlar boyunca tekrarlayan bulgu"), answer_against_recommendation: t("önerinin tersine cevabın"),
  plan_note: t("plana değişiklik notun"), github: t("PR'daki geri bildirim"),
  answer: t("soru turundaki cevabın"), check_fixed: t("sonra geçen kontrol"), run_recovered: t("sonra düzelen koşu"), intent_gap: t("niyet sapması"),
};
const kindName = (kind) => (kind === "yol" ? t("yol haritası") : t(kind));  // the entry kinds are data ("kural" …); their names are shown
const ON_REVIEW_PAGE = new Set(["user_comment", "overruled_finding", "recurring_finding"]);
const MEMORY_KINDS = [["kural", t("Kurallar")], ["karar", t("Kararlar")], ["tuzak", t("Tuzaklar")], ["olay", t("Olaylar")]];
const MEMORY_MAIN = { kural: "Kural", karar: "Karar", tuzak: "Ne yapılır", olay: "Sonuç", yol: "Başlık" };  // i18n-ok: file-format field names; the field "Düzenle ve onayla" edits
const ORIGIN_TR = { answer: t("soru turu"), retro: "retro", migration: t("eski kurallardan taşındı"), lead: t("lider") };
// Proposal groups on the Hafıza page: the entry kinds, then roadmap scope changes (approving one edits roadmap.md).
const PENDING_KINDS = [...MEMORY_KINDS.filter(([k]) => k !== "olay"), ["yol", t("Yol haritası değişiklikleri")]];

function renderMemoryCount(s) {
  $("memory-count").textContent = s.memory_pending ? String(s.memory_pending) : "";
}

const memoryEditing = () => !!$("memory-body").querySelector(".mem-edit:not([hidden]) textarea");

async function loadMemory() {
  let data;
  try {
    data = await api("/api/memory");
  } catch (e) {
    $("memory-body").replaceChildren(h("p", { class: "error-banner" }, e.message));
    return;
  }
  loadMemory.pending = data.targets.reduce((n, tk) => n + tk.pending.length, 0);
  renderMemory(data.targets);
  const unseen = data.targets.filter((tk) => tk.pending.length || tk.warnings.length);
  if (unseen.length) Promise.all(unseen.map((tk) => api("/api/attention/seen", { target: tk.target, memory: true }))).then(() => refresh()).catch(() => { /* stays unread */ });
}

function memoryWhere(tk) {
  const label = tk.source === "vault" ? t("kasa: {0}", tk.vault_dir) : tk.source === "override" ? t("özel klasör") : t("Ziggy durumu");
  return h("span", { class: "muted small mono", title: tk.dir }, `${label} · ${homeShort(tk.dir)}`);
}

function renderMemory(targets) {
  const blocks = [];
  for (const tk of targets) {
    const stored = MEMORY_KINDS.reduce((n, [k]) => n + tk.entries[k].length, 0);
    const pending = PENDING_KINDS.map(([k, label]) => [label, tk.pending.filter((c) => c.kind === k)]).filter(([, cs]) => cs.length);
    blocks.push(h("section", { class: "block mem-target" },
      h("div", { class: "block-head" },
        h("h2", {}, tk.target, h("span", { class: "count" }, tk.pending.length ? String(tk.pending.length) : "")),
        memoryWhere(tk)),
      tk.hint ? h("p", { class: "muted small mem-hint" }, t(tk.hint)) : "",
      tk.warnings.length ? h("div", { class: "notice mem-warnings" }, icon("alert"),
        h("div", { class: "grow" }, h("strong", {}, t("Hafıza dosyalarında okunamayan yerler var")),
          h("ul", { class: "mem-warning-list small" }, tk.warnings.map((w) => h("li", {}, t(w)))))) : "",
      pending.length ? h("div", { class: "small muted mem-label" }, t("Bekleyen öneriler")) : "",
      ...pending.map(([label, cs]) => h("div", { class: "mem-kind" },
        h("h3", { class: "mem-kind-h" }, label, h("span", { class: "muted small" }, " · " + plural(t("{0} öneri"), cs.length))),
        h("div", { class: "stack" }, cs.map((c) => memoryCard(tk, c))))),
      stored ? [h("div", { class: "small muted mem-label" }, t("Hafızada")), MEMORY_KINDS.map(([k, label]) => memoryEntries(tk, k, label))]
        : !tk.pending.length ? empty(t("Henüz hafıza kaydı yok. Soru turlarında verdiğin cevaplar karar önerisi olur; bir ticket bitince kısa bir retro oturumu review yorumlarından, düzelttirdiğin bulgulardan, sonradan geçen kontrollerden ve düzelen koşulardan kural, karar ve tuzak önerir. Sen onaylamadan hafızaya hiçbir şey yazılmaz."), "spec") : ""));
  }
  const decided = targets.flatMap((tk) => tk.decided.map((c) => ({ t: tk, c })))
    .sort((a, b) => String(b.c.decided_at).localeCompare(String(a.c.decided_at))).slice(0, 20);
  if (decided.length) {
    blocks.push(h("details", { class: "section mem-decided" },
      h("summary", {}, h("span", { class: "section-title" }, t("Son kararlar"), h("span", { class: "muted small" }, String(decided.length)))),
      h("div", { class: "section-body" }, h("ul", { class: "mem-log" }, decided.map(({ t: tk, c }) => h("li", {},
        h("span", { class: `pill ${c.status === "approved" ? "ok" : "mute"}` }, c.status === "approved" ? (c.edited ? t("düzenlenip onaylandı") : t("onaylandı")) : t("reddedildi")),
        h("span", { class: "muted small" }, `${tk.target} · ${kindName(c.kind)} · ${when(c.decided_at)}`),
        c.entry ? h("span", { class: "id" }, c.entry) : "",
        h("span", { class: "md small" }, MD.inline(c.approved_title || c.title)),
        c.replaced?.length ? h("span", { class: "muted small" }, t("{0} yerine", c.replaced.join(", "))) : ""))))));
  }
  const off = targets.filter((tk) => !tk.retro).map((tk) => tk.target);
  if (off.length) blocks.push(h("p", { class: "muted small" }, t("Retro kapalı: {0} (config'te retro = false); bu projelere yalnız soru turlarındaki cevaplardan öneri gelir.", off.join(", "))));
  if (!targets.length) blocks.push(empty(t("Proje yok."), "spec"));
  $("memory-body").replaceChildren(...blocks);
  $("memory-body").querySelector(".mem-card")?.classList.add("lead");  // the first proposal: your next move here
}

function scopeLine(scope) {
  const paths = scope?.paths || [];
  const topics = scope?.topics || [];
  if (!paths.length && !topics.length) return "";
  return h("div", { class: "muted small mem-scope" },
    paths.length ? [t("dosyalar "), ...paths.flatMap((p, i) => [i ? ", " : "", h("code", {}, p)])] : "",
    paths.length && topics.length ? " · " : "",
    topics.length ? t("konular {0}", topics.join(", ")) : "");
}

function memorySource(tk, s) {
  const open = () => {
    const row = rowOf(tk.target, s.ticket);
    if (row) openDrawer(row);
    else toast(t("{0} bulunamadı", s.ticket), true);
  };
  return h("li", { class: "mem-source" },
    h("div", { class: "row tight" },
      s.ticket ? h("span", { class: "id" }, s.ticket) : "", s.ref ? h("span", { class: "tag" }, s.ref) : "",
      h("span", { class: "muted small grow" }, SOURCE_TR[s.kind] || s.kind, s.where ? ` · ${s.where}` : ""),
      s.ticket ? h("button", { class: "small", onclick: open }, icon("ticket"), "Ticket") : "",
      s.ticket && ON_REVIEW_PAGE.has(s.kind) ? h("a", { class: "button small", href: ReviewPage.hash(tk.target, s.ticket) }, icon("diff"), t("İnceleme")) : "",
      s.url ? h("a", { class: "button small", href: s.url, target: "_blank", rel: "noopener noreferrer" }, icon("external"), "GitHub") : ""),
    s.summary ? h("div", { class: "small mem-quote" }, s.summary) : "");
}

function memoryCard(tk, c) {
  const main = MEMORY_MAIN[c.kind];
  const titleBox = h("input", { type: "text", "aria-label": t("Başlık"), maxlength: 120 });
  const box = h("textarea", { rows: 3, class: "answer-box", "aria-label": t(main), maxlength: 600 });
  const replace = h("input", { type: "checkbox" });
  const editor = h("div", { class: "mem-edit stack", hidden: true },
    h("label", { class: "mem-edit-field" }, h("span", { class: "muted small" }, t("Başlık")), titleBox),
    h("label", { class: "mem-edit-field" }, h("span", { class: "muted small" }, t(main)), box));
  const decide = async (btn, action, edited) => {
    btn.disabled = true;
    try {
      const out = await api(`/api/memory/${encodeURIComponent(tk.target)}/candidates/${encodeURIComponent(c.id)}`,
        { action, ...(action === "approve" ? { replace: replace.checked } : {}), ...(edited || {}) });
      const made = out.candidate;
      toast(action === "approve" ? (c.kind === "yol" ? t("{0}: yol haritasına işlendi ({1})", tk.target, made.entry)
        : t("{0}: {1} olarak eklendi{2}", tk.target, made.entry, made.replaced?.length ? t(" · {0} yerine", made.replaced.join(", ")) : ""))
        : t("{0}: öneri reddedildi; hafıza değişmedi", tk.target));
    } catch (e) {
      toast(e.message, true);
    }
    await loadMemory();
    refresh();
  };
  const approve = h("button", { class: "small primary", onclick: (e) => decide(e.currentTarget, "approve") }, icon("check"), t("Onayla"));
  const edit = h("button", { class: "small", onclick: () => {
    editor.hidden = false;
    titleBox.value = c.title;
    box.value = c.fields[main] || "";
    box.focus();
    actions.hidden = true;
  } }, icon("spec"), t("Düzenle ve onayla"));
  if (c.kind === "yol" && !["ekle", "evre ekle"].includes(c.fields?.["İşlem"])) edit.hidden = true;  // i18n-ok: roadmap file data; nothing to word in a removal or a move
  const reject = h("button", { class: "small ghost", onclick: (e) => decide(e.currentTarget, "reject") }, icon("x"), t("Reddet"));
  const actions = h("div", { class: "row" }, approve, edit, reject,
    h("span", { class: "muted small grow" }, [c.ticket, ORIGIN_TR[c.origin] || c.origin].filter(Boolean).join(" · "), ` · ${ago(c.created_at)}`));
  editor.append(h("div", { class: "row" },
    h("button", { class: "small primary", onclick: (e) => {
      if (!titleBox.value.trim()) return toast(t("Başlık boş olamaz"), true);
      if (!box.value.trim()) return toast(t("{0} boş olamaz", t(main)), true);
      decide(e.currentTarget, "approve", { title: titleBox.value, text: box.value });
    } }, icon("check"), t("Kaydet ve onayla")),
    h("button", { class: "small ghost", onclick: () => { editor.hidden = true; actions.hidden = false; } }, t("Vazgeç")),
    h("span", { class: "muted small" }, t("Tek satır; kapsam, kaynak ve tarih olduğu gibi kalır."))));
  return h("article", { class: "panel mem-card" },
    h("div", { class: "row tight" }, h("span", { class: "tag" }, kindName(c.kind)), h("strong", { class: "md mem-title" }, MD.inline(c.title))),
    h("ul", { class: "mem-fields" }, Object.entries(c.fields || {}).map(([name, value]) =>
      h("li", { class: "md" }, h("strong", {}, `${t(name)}: `), MD.inline(value)))),
    scopeLine(c.scope),
    c.similar?.length ? h("div", { class: "mem-similar small" },
      c.similar.map((s) => h("div", {}, h("span", { class: "muted" }, t("benzer: ")), h("span", { class: "id" }, s.id), " ", s.title)),
      h("label", { class: "check", title: t("Onaylanınca benzer kayıtlara \"yerini aldı\" yazılır; aşamalara artık verilmezler") }, replace, t("yerine geçsin"))) : "",
    c.sources?.length ? h("div", { class: "small muted mem-label" }, t("Kaynaklar")) : "",
    c.sources?.length ? h("ul", { class: "mem-sources" }, c.sources.map((s) => memorySource(tk, s))) : "",
    editor, actions);
}

function memoryEntries(tk, kind, label) {
  const all = tk.entries[kind];
  if (!all.length) return "";
  const live = all.filter((e) => e.status === "active");
  const old = all.filter((e) => e.status !== "active");
  const list = (es) => h("ul", { class: "mem-entries" + (kind === "olay" ? " compact" : "") }, es.map((e) => memoryEntry(tk, e)));
  return h("div", { class: "mem-kind" },
    h("h3", { class: "mem-kind-h" }, label, h("span", { class: "muted small" }, ` · ${live.length}`)),
    live.length ? list(live) : h("p", { class: "muted small" }, t("Etkin kayıt yok.")),
    old.length ? h("details", { class: "mem-old" }, h("summary", { class: "muted small" }, t("Yerini almış ya da unutulmuş: {0}", old.length)), list(old)) : "");
}

function memoryEntry(tk, e) {
  const forget = async (btn) => {
    if (!(await confirmBox(t("{0} unutulsun mu?\nAşamalara artık verilmez; metni dosyada \"Durum: unutuldu\" ile kalır.", e.id)))) return;
    btn.disabled = true;
    try {
      await api(`/api/memory/${encodeURIComponent(tk.target)}/entries/${encodeURIComponent(e.id)}`, { action: "forget" });
      toast(t("{0}: {1} unutuldu", tk.target, e.id));
    } catch (err) {
      toast(err.message, true);
    }
    await loadMemory();
  };
  const main = e.fields[MEMORY_MAIN[e.kind]];
  const status = e.status === "replaced" ? h("span", { class: "pill mute" }, t("yerini aldı {0}", e.replaced_by || "").trim())
    : e.status === "forgotten" ? h("span", { class: "pill mute" }, t("unutuldu")) : "";
  return h("li", { class: "mem-entry" + (e.status === "active" ? "" : " inactive"), id: `mem-${tk.target}-${e.id}` },
    h("div", { class: "row tight" },
      h("span", { class: "id" }, e.id), h("strong", { class: "md mem-title grow" }, MD.inline(e.title)), status,
      e.stale ? h("span", { class: "pill warn", title: t("{0} gündür kullanılmadı", tk.stale_days) }, t("soluk")) : "",
      e.stale && !e.prune ? h("span", { class: "muted small" }, t("Unutalım mı?")) : "",
      e.prune ? h("span", { class: "pill warn", title: t(e.prune) }, t("budanabilir")) : "",
      h("span", { class: "muted small nowrap" }, e.uses ? t("{0} kez kullanıldı · son {1}", e.uses, ago(e.last_used)) : t("henüz kullanılmadı")),
      e.status === "active" ? h("button", { class: "small ghost", onclick: (ev) => forget(ev.currentTarget) }, icon("x"), t("Unut")) : ""),
    main ? h("div", { class: "md small" }, MD.inline(main)) : "",
    e.kind === "olay" && e.fields.PR ? h("div", { class: "muted small" }, /^https?:\/\//.test(e.fields.PR) ? link(e.fields.PR, e.fields.PR) : e.fields.PR,
      e.fields.Birleşti ? t(" · birleşti {0}", e.fields.Birleşti) : "") : "",
    e.kind === "olay" ? "" : scopeLine(e.scope));
}

// --- project page: PROJECT.md, the router, open PRs, this project's tickets -----------------------
// The project document comes from the base branch (/api/targets/<n>/project); a request goes to the
// router, whose proposal (a ticket type) waits for a click before a ticket is made and started.

const TYPE_OPTIONS = [["feat", t("feat · yeni özellik")], ["fix", t("fix · hata düzeltme")], ["refactor", "refactor"], ["chore", t("chore · bakım")], ["docs", "docs"], ["project", t("project · PROJECT.md'yi yaz/güncelle")]];
let project = null; // {name, state, loading, at, pulls, roadmap, roadAt}

async function openProject(name) {
  if (!name) return;
  if (project?.name !== name) {
    project = { name, state: null, pulls: null, roadmap: null };
    $("pj-proposal").replaceChildren();
    $("pj-text").value = "";
    $("pj-roadmap").replaceChildren();
    $("pj-roadmap").dataset.sig = "";
  }
  if (snapshot) renderProject(snapshot);
  await Promise.all([loadProjectState(true), loadPulls(true), loadRoadmap()]);
}

async function loadPulls(fresh) {
  const p = project;
  if (!p || p.pullsLoading) return;
  p.pullsLoading = true;
  try {
    p.pulls = (await api(`/api/targets/${encodeURIComponent(p.name)}/pulls${fresh ? "?fresh=1" : ""}`)).pulls;
    p.pullsError = null;
  } catch (e) {
    p.pullsError = e.message;
  }
  p.pullsAt = Date.now();
  p.pullsLoading = false;
  if (project === p && snapshot) renderProject(snapshot, true);
}

async function loadProjectState(fresh) {
  const p = project;
  if (!p || p.loading) return;
  p.loading = true;
  try {
    p.state = await api(`/api/targets/${encodeURIComponent(p.name)}/project${fresh ? "?fresh=1" : ""}`);
    p.error = null;
    p.at = Date.now();
  } catch (e) {
    p.error = e.message;
  }
  p.loading = false;
  if (project === p && snapshot) renderProject(snapshot, true);
}

async function loadRoadmap() {
  const p = project;
  if (!p || p.roadLoading) return;
  p.roadLoading = true;
  try {
    p.roadmap = await api(`/api/targets/${encodeURIComponent(p.name)}/roadmap`);
    p.roadError = null;
  } catch (e) {
    p.roadError = e.message;
  }
  p.roadAt = Date.now();
  p.roadLoading = false;
  if (project === p && snapshot) renderProject(snapshot, true);
}

function renderProject(s, force) {
  const p = project;
  if (!p) return;
  const tk = s.targets.find((x) => x.name === p.name);
  if (!tk) {
    $("pj-head").replaceChildren(h("h1", {}, p.name), h("span", { class: "muted" }, t("Böyle bir proje yok.")));
    return;
  }
  // The document follows merged PRs: reload now and then.
  if (Date.now() - (p.at || 0) > 60000) loadProjectState(false);
  if (Date.now() - (p.pullsAt || 0) > 60000) loadPulls(false);
  const [cls, text] = tk.error ? ["bad", t("config hatası")] : tk.active?.length ? ["live", tk.active.length > 1 ? plural(t("{0} koşu"), tk.active.length) : t("koşu var")] : ["ok", t("hazır")];
  const headSig = JSON.stringify([tk.name, cls, text, tk.github_repo, tk.bmad, tk.mode]);
  if (headSig !== $("pj-head").dataset.sig) {
    $("pj-head").dataset.sig = headSig;
    $("pj-head").replaceChildren(h("h1", {}, tk.name), h("span", { class: "badge" }, h("span", { class: `dot ${cls}` }), text), bmadPill(tk), controlledPill(tk),
      tk.github_repo ? link(`https://github.com/${tk.github_repo}`, h("span", { class: "mono small" }, tk.github_repo)) : "");
  }
  const rows = s.tickets.filter((x) => x.target === p.name && !x.archived);
  ticketList($("pj-tickets"), rows, { key: ["project", p.name], showTarget: false, empty: t("Bu projede açık ticket yok.") });
  renderPulls(tk, rows);
  // Progress follows the tickets (the server writes it); a draft being written is asked after more often.
  if (Date.now() - (p.roadAt || 0) > (p.roadmap?.draft?.running ? 4000 : 30000)) loadRoadmap();
  renderRoadmap(tk);
  const docSig = JSON.stringify([p.state?.project_doc, p.state?.base, p.error, rows.filter((x) => x.type === "project").map((x) => [x.id, x.status, x.running])]);
  if (!force && docSig === $("pj-doc").dataset.sig) return;
  $("pj-doc").dataset.sig = docSig;
  $("pj-base").textContent = p.state ? t("{0} dalından", p.state.base) : "";
  if (!p.state) {
    $("pj-doc").replaceChildren(empty(p.error || t("Proje belgesi okunuyor…"), p.error ? "alert" : "clock"));
    return;
  }
  $("pj-doc").replaceChildren(docCard(tk, p.state.project_doc || {}, rows.filter((x) => x.type === "project")));
}

// --- the project's roadmap: phases with progress, items with their ticket and PR, "Ticket aç" on the next ones ---
// Source: the memory's roadmap.md (vault or Ziggy's state) or BMad's epics.md + sprint-status.yaml (read-only).
// Without one: "Yol haritası çıkar" (a read-only draft session), the draft in an editor, "Taslak olarak kaydet".

const ROAD_ITEM = {
  todo: [t("yapılacak"), "mute"], ticket: [t("ticket açıldı"), "info"], "in-progress": [t("sürüyor"), "info"], review: [t("incelemede"), "warn"],
  pr: [t("PR açık"), "info"], merged: [t("birleşti"), "ok"], done: [t("bitti"), "ok"],
};
const ROAD_PHASE = { todo: [t("sırada"), "mute"], active: [t("aktif"), "volt"], done: [t("bitti"), "ok"] };
const ROAD_WHERE = { vault: (r) => t("kasa: {0}", r.vault_dir), override: () => t("özel klasör"), state: () => t("Ziggy durumu"), bmad: () => "BMad" };
const roadEditing = () => { const ta = $("pj-roadmap").querySelector("textarea"); return !!ta && (document.activeElement === ta || ta.value !== ta.dataset.orig); };

function renderRoadmap(tk) {
  const p = project;
  const r = p.roadmap;
  const linked = (r?.phases || []).flatMap((ph) => ph.items.filter((it) => it.ticket).map((it) => {
    const row = rowOf(tk.name, it.ticket);
    return [it.ticket, row?.status, row?.running];
  }));
  const sig = JSON.stringify([r, p.roadError, !!tk.bmad?.installed, linked]);
  const box = $("pj-roadmap");
  if (sig === box.dataset.sig || roadEditing()) return;  // never under a cursor: the draft may be half edited
  box.dataset.sig = sig;
  $("pj-road-where").textContent = r?.source ? `${ROAD_WHERE[r.where]?.(r) || r.where} · ${homeShort(r.path)}` : "";
  $("pj-road-where").title = r?.path || "";
  if (!r) {
    box.replaceChildren(empty(p.roadError || t("Yol haritası okunuyor…"), p.roadError ? "alert" : "clock"));
    return;
  }
  const parts = [];
  if (r.shadowed) parts.push(h("p", { class: "muted small road-note" }, t("Hafızadaki {0} de var; BMad'in epics.md'si kullanılıyor (iki kaynak olunca BMad kazanır).", homeShort(r.shadowed))));
  if (r.warnings?.length) {
    parts.push(h("div", { class: "notice mem-warnings" }, icon("alert"),
      h("div", { class: "grow" }, h("strong", {}, t("Yol haritasında okunamayan yerler var")),
        h("ul", { class: "mem-warning-list small" }, r.warnings.map((w) => h("li", {}, t(w)))))));
  }
  if (!r.source) parts.push(...roadmapEmpty(tk, r));
  else if (!r.phases.length) parts.push(empty(t("Yol haritasında evre yok."), "spec"));
  else parts.push(h("div", { class: "road-phases" }, r.phases.map((ph) => roadPhase(tk, r, ph))));
  box.replaceChildren(...parts);
}

function roadPhase(tk, r, ph) {
  const [label, cls] = ROAD_PHASE[ph.status] || [ph.status, "mute"];
  const pct = ph.total ? (100 * ph.done) / ph.total : 0;
  return h("section", { class: `stage-card road-phase ${ph.id === r.current ? "current" : ""} ${ph.status}` },
    h("div", { class: "row tight wrap" }, h("span", { class: "tag" }, ph.id), h("strong", { class: "grow road-title" }, ph.title),
      h("span", { class: `pill ${cls}` }, label), h("span", { class: "muted small mono nowrap" }, `${ph.done}/${ph.total}`)),
    h("div", { class: "hbar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(ph.total), "aria-valuenow": String(ph.done),
      "aria-label": t("{0} ilerleme", ph.id) }, h("div", { class: "hbar-fill", style: `width:${pct.toFixed(1)}%;background:var(--ok)` })),
    ph.items.length ? h("ul", { class: "road-items" }, ph.items.map((it) => roadItem(tk, r, ph, it))) : h("p", { class: "muted small" }, t("Bu evrede madde yok.")));
}

function roadItem(tk, r, ph, it) {
  const [label, cls] = ROAD_ITEM[it.status] || [it.status, "mute"];
  const row = it.ticket ? rowOf(tk.name, it.ticket) : null;
  const next = r.next?.includes(it.id);
  return h("li", { class: `road-item ${it.status}` + (next ? " next" : "") },
    h("div", { class: "road-item-head" },
      h("span", { class: "id" }, it.id), h("span", { class: "road-item-title" }, it.title),
      h("span", { class: "road-item-side" },
        h("span", { class: `pill ${cls}` }, label),
        it.ticket ? (row ? h("button", { class: "small", onclick: () => openDrawer(row) }, icon("ticket"), it.ticket) : h("span", { class: "chip mono" }, it.ticket)) : "",
        it.pr ? link(it.pr, chip("pr", "PR #" + it.pr.split("/").pop(), "linked")) : "",
        it.status === "todo" && !it.ticket ? h("button", { class: `small ${next ? "primary" : ""}`, title: t("Composer'ı bu maddeyle doldurur; oluşturmak senin elinde"),
          onclick: () => roadmapTicket(tk.name, ph, it) }, icon("plus"), t("Ticket aç")) : "")),
    it.detail ? h("div", { class: "muted small road-detail" }, it.detail) : "");
}

function roadmapEmpty(tk, r) {
  if (tk.bmad?.installed) {
    return [empty(t("Bu projede BMad kurulu ama epics.md yok. BMad'in epic ve hikâye akışıyla (create-epics-and-stories) _bmad-output/planning-artifacts/epics.md'yi oluştur; Ziggy onu ve sprint-status.yaml'ı yol haritası olarak okur."), "spec")];
  }
  const d = r.draft;
  const start = (label, cls) => h("button", { class: cls || "", onclick: async (e) => {
    e.currentTarget.disabled = true;
    try {
      await api(`/api/targets/${encodeURIComponent(tk.name)}/roadmap/draft`, {});
      toast(t("{0}: yol haritası taslağı yazılıyor", tk.name));
    } catch (err) {
      toast(err.message, true);
    }
    await loadRoadmap();
  } }, icon("play"), label);
  const cost = t("{0} · en çok $1 · salt okunur: README, docs/PROJECT.md, specler ve hafızadaki bağlam ile kararlar", r.model);
  if (!d) {
    return [h("div", { class: "stage-card road-empty" },
      h("strong", {}, t("Bu projenin yol haritası yok")),
      h("p", { class: "muted small" }, t("Evreler ve maddeleri: her sabah sıradaki maddeler önerilir, her madde bir ticket olur. Taslağı bir ajan çıkarır; sen düzenleyip kaydedene kadar hiçbir şey yazılmaz.")),
      h("div", { class: "row tight wrap" }, start(t("Yol haritası çıkar"), "primary"), h("span", { class: "muted small" }, cost)))];
  }
  if (d.running) return [h("div", { class: "stage-card road-empty" }, h("div", { class: "row tight" }, h("span", { class: "dot live" }), h("strong", {}, t("Taslak yazılıyor…")),
    h("span", { class: "muted small" }, d.started_at ? ago(d.started_at) : "")), h("p", { class: "muted small" }, cost))];
  if (!d.draft) {
    return [h("div", { class: "stage-card road-empty" },
      h("div", { class: "row tight" }, h("span", { class: "pill bad" }, STATE_TR[d.state] || d.state || t("hata")), h("strong", {}, t("Taslak çıkmadı"))),
      d.error ? h("pre", { class: "small road-error" }, d.error) : "",
      h("div", { class: "row tight wrap" }, start(t("Yeniden dene"), "primary"), h("span", { class: "muted small" }, cost)))];
  }
  const ta = h("textarea", { class: "answer-box mono road-draft", rows: 18, spellcheck: "false", "aria-label": t("Yol haritası taslağı") });
  ta.value = d.draft;
  ta.dataset.orig = d.draft;
  const save = h("button", { class: "primary", onclick: async (e) => {
    if (!ta.value.trim()) return toast(t("Taslak boş"), true);
    e.currentTarget.disabled = true;
    try {
      project.roadmap = await api(`/api/targets/${encodeURIComponent(tk.name)}/roadmap/save`, { text: ta.value });
      ta.dataset.orig = ta.value;
      toast(t("{0}: yol haritası kaydedildi", tk.name));
    } catch (err) {
      toast(err.message, true);
      e.currentTarget.disabled = false;
      return;
    }
    ta.blur();
    $("pj-roadmap").dataset.sig = "";
    renderProject(snapshot, true);
  } }, icon("check"), t("Taslak olarak kaydet"));
  return [h("div", { class: "stage-card road-empty" },
    h("div", { class: "row tight wrap" }, h("span", { class: "pill volt" }, t("taslak")), h("strong", { class: "grow" }, d.summary || t("Yol haritası taslağı")),
      d.cost != null ? h("span", { class: "muted small mono" }, money(d.cost)) : ""),
    d.notes?.length ? h("ul", { class: "small muted road-notes" }, d.notes.map((n) => h("li", {}, n))) : "",
    h("p", { class: "muted small" }, t("Düzenle, sonra kaydet: {0} olur. \"## P1 · başlık\" bir evre, \"- [ ] R-1 · başlık\" bir madde, altındaki girintili satır ayrıntısı.", homeShort(r.memory_path))),
    ta,
    h("div", { class: "row tight wrap" }, save, start(t("Yeniden çıkar"), "ghost")))];
}

function docCard(target, doc, docTickets) {
  const active = docTickets.find((x) => x.status !== "done" || x.running);
  const actions = [];
  if (doc.exists) actions.push(h("button", { class: "small", onclick: () => openDoc("PROJECT.md", doc.path, doc.text || "") }, icon("spec"), t("Oku")));
  if (active) actions.push(h("button", { class: "small", onclick: () => openDrawer(active) }, icon("ticket"), active.id));
  else actions.push(h("button", { class: "small ghost", onclick: () => showProposal({ type: "project", title: doc.exists ? "Update PROJECT.md" : "Write PROJECT.md", reason: t("Proje belgesini sen seçtin."), text: $("pj-text").value.trim() }) },
    icon(doc.exists ? "retry" : "play"), doc.exists ? t("Güncelle") : t("Yaz")));
  return h("div", { class: `stage-card doc-card ${doc.exists ? "final" : ""}` },
    h("div", { class: "row tight" }, h("strong", {}, "PROJECT.md"),
      h("span", { class: `pill ${doc.exists ? "ok" : "mute"}` }, doc.exists ? t("base dalında var") : t("yok")),
      h("span", { class: "grow" }), active ? h("span", { class: "muted small" }, active.running ? t("çalışıyor") : STATUS_TR[active.status] || active.status) : ""),
    h("div", { class: "muted small mono" }, doc.path || "docs/PROJECT.md"),
    doc.exists ? h("div", { class: "muted small" }, doc.sha ? h("span", { class: "mono" }, doc.sha.slice(0, 7)) : "", doc.subject ? ` · ${doc.subject}` : "", doc.date ? ` · ${when(doc.date)}` : "", doc.lines ? " · " + plural(t("{0} satır"), doc.lines) : "")
      : h("div", { class: "muted small" }, t("Ürünün ne olduğu, kapsamı ve kararları burada durur; ajanlar her aşamada okur. 'project' tipinde bir ticket yazar ve PR açar.")),
    h("div", { class: "row tight wrap" }, actions));
}

// The project's open PRs. One that no ticket follows gets "Codex review", which adopts it (ticket
// pr-<n>, a worktree at its head) and starts the review; from then on its ticket's own buttons apply:
// fix the findings, push the fixes to the PR's own branch.
function renderPulls(target, rows) {
  const p = project;
  const box = $("pj-pulls");
  const sig = JSON.stringify([p.pulls, p.pullsError, rows.filter((x) => x.pull || x.pr).map((x) => [x.id, x.status, x.running, x.review, x.needs_you?.kind])]);
  if (sig === box.dataset.sig) return;
  box.dataset.sig = sig;
  if (p.pullsError) {
    box.replaceChildren(empty(p.pullsError, "alert"));
    return;
  }
  if (!p.pulls) {
    box.replaceChildren(empty(t("PR'lar okunuyor…"), "clock"));
    return;
  }
  if (!p.pulls.length) {
    box.replaceChildren(empty(t("Bu projede açık PR yok."), "pr"));
    return;
  }
  box.replaceChildren(...p.pulls.map((pr) => {
    const row = pr.ticket && rows.find((x) => x.id === pr.ticket);
    const who = pr.mine ? t("sen") : pr.author.replace(/^app\//, "");
    const meta = h("div", { class: "muted small row tight wrap" },
      h("span", {}, who), h("span", { class: "mono" }, `${pr.head} → ${pr.base}`), h("span", {}, ago(pr.updated_at)),
      pr.draft ? h("span", { class: "pill mute" }, "draft") : "", pr.fork ? h("span", { class: "pill warn" }, "fork") : "");
    const actions = [];
    if (row) {
      actions.push(h("button", { class: "small ghost", onclick: () => openDrawer(row, row.review ? "review" : undefined) }, icon("ticket"), row.id));
      const pill = reviewPill(row);
      if (pill) actions.push(pill);
      actions.push(...actionButtons(row, true));
    } else if (pr.ticket) {
      actions.push(h("span", { class: "muted small" }, pr.ticket));
    } else {
      actions.push(h("button", { class: "small", onclick: () => adoptPull(target, pr) }, icon("eye"), "Codex review"));
    }
    actions.push(h("a", { class: "button small ghost", href: pr.url, target: "_blank", rel: "noopener noreferrer", "aria-label": t("PR #{0} GitHub'da", pr.number), title: t("GitHub'da aç") }, icon("external")));
    return h("div", { class: "story pull" },
      h("span", { class: "decision-n" }, `#${pr.number}`),
      h("div", { class: "story-body" }, h("strong", {}, pr.title), meta),
      h("div", { class: "row tight wrap end pull-actions" }, actions));
  }));
}

async function adoptPull(target, pr) {
  const owner = pr.mine ? "" : t("\nPR {0} kullanıcısının; review dala dokunmaz, düzeltmeleri push etmeden önce ayrıca sorulur.", pr.author.replace(/^app\//, ""));
  if (!(await confirmBox(t("{0}: PR #{1} — \"{2}\" Codex'e review ettirilsin mi?\nPR, pr-{3} ticket'ı olur (kendi worktree'si); {4}'e göre fark incelenir (1–3 dk).{5}", target.name, pr.number, pr.title, pr.number, pr.base, owner)))) return;
  try {
    const out = await api(`/api/targets/${encodeURIComponent(target.name)}/pulls`, { number: pr.number, review: true });
    toast(t("{0}: Codex review başladı", out.ticket.id));
    await refresh();
    await loadPulls(false);
  } catch (err) {
    toast(err.message, true);
  }
}

function openDoc(title, path, text) {
  $("doc-title").textContent = title;
  $("doc-path").textContent = path || "";
  $("doc-body").replaceChildren(...[MD.blocks(MD.frontmatter(text).body)].flat(Infinity));
  $("doc-modal").hidden = false;
}

$("close-doc").addEventListener("click", () => { $("doc-modal").hidden = true; });
$("doc-modal").addEventListener("click", (e) => { if (e.target === e.currentTarget) $("doc-modal").hidden = true; });

$("pj-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("pj-text").value.trim();
  if (!text) return toast(t("Önce ne yapmak istediğini yaz"), true);
  const btn = $("pj-route");
  btn.disabled = true;
  $("pj-proposal").replaceChildren(h("div", { class: "proposal muted" }, h("span", { class: "dot live" }), t("Nasıl bir ticket gerektiğine bakılıyor…")));
  try {
    const r = await api(`/api/targets/${encodeURIComponent(project.name)}/route`, { text });
    if (r.state) { project.state = r.state; project.at = Date.now(); renderProject(snapshot, true); }
    showProposal({ type: r.type, title: r.title, reason: r.reason, text });
  } catch (err) {
    $("pj-proposal").replaceChildren(h("div", { class: "proposal bad" }, icon("alert"), err.message));
  }
  btn.disabled = false;
});

function showProposal(p) {
  const kind = h("select", { "aria-label": t("Tip") }, TYPE_OPTIONS.map(([v, label]) => h("option", { value: v, selected: v === p.type }, label)));
  const title = h("input", { class: "grow", value: p.title || "", placeholder: t("Ticket başlığı"), "aria-label": t("Ticket başlığı") });
  const body = h("textarea", { rows: 3, placeholder: t("Ayrıntı (isteğe bağlı): ajan bunu talep olarak okur"), "aria-label": t("Ayrıntı") });
  body.value = p.text || "";
  const start = h("button", { class: "primary", onclick: async () => {
    if (!title.value.trim()) return toast(t("Başlık boş"), true);
    const type = kind.value;
    if (!(await confirmBox(t("{0}: \"{1}\" ({2}) ticket'ı açılıp koşsun mu?\n{3} hattı aşama aşama koşar; birkaç dolar, 10–30 dk.", project.name, title.value.trim(), type, pipelineFor(type))))) return;
    start.disabled = true;
    try {
      const out = await api("/api/new", { target: project.name, type, title: title.value.trim(), body: body.value, run: true, language: $("pj-lang").value });
      toast(out.job?.queued ? t("{0} kuyruğa alındı; koşu slotları boşalınca başlar", out.ticket.id) : t("{0} başladı", out.ticket.id));
      $("pj-proposal").replaceChildren();
      $("pj-text").value = "";
      await refresh();
      await loadProjectState(true);
      const row = rowOf(project.name, out.ticket.id);
      if (row) openDrawer(row, "activity");
    } catch (err) {
      toast(err.message, true);
      start.disabled = false;
    }
  } }, icon("check"), t("Onayla ve başlat"));
  $("pj-proposal").replaceChildren(h("div", { class: "proposal" },
    h("div", { class: "row tight" }, icon("zap"), h("strong", {}, t("Öneri")), h("span", { class: "muted small grow" }, p.reason || "")),
    h("div", { class: "row" }, kind, title),
    body,
    h("div", { class: "row end" }, h("button", { class: "ghost", onclick: () => $("pj-proposal").replaceChildren() }, t("Vazgeç")), start)));
  title.focus();
}

$("pj-lang").addEventListener("change", () => store.set(localStorage, "ziggy.ticketlang", $("pj-lang").value || "-"));

// --- Sohbet ---------------------------------------------------------------------------------------
// Free chats about a project (talk.py): "okuyan" in the main checkout, "yazan" in a worktree of its own on
// chat/<id>. #talk/<target>, #talk/<target>/<id>, #talk/<target>/new?mode=write. The conversation itself is
// chat.js's, through a conv adapter; a new chat exists once its first message is sent.

const TALK_MODE = { read: t("okuyan"), write: t("yazan") };
const talkHash = (target, id, mode) => `#talk/${enc(target)}${id ? `/${id}` : mode ? `/new?mode=${mode}` : ""}`;
const talk = { key: null, list: null, listAt: 0, target: null, view: null, viewSig: null };

function talkRoute() {
  const [path, query] = location.hash.slice(1).split("?");
  const [, target, id] = path.split("/");
  const live = (snapshot?.targets || []).filter((tk) => !tk.error).map((tk) => tk.name);
  const name = decodeURIComponent(target || "");
  return {
    target: live.includes(name) ? name : (live.includes(homeTarget()) ? homeTarget() : live[0] || ""),
    id: id && id !== "new" ? id : null,
    mode: new URLSearchParams(query || "").get("mode") === "write" ? "write" : "read",
  };
}

async function loadTalkList(target, force) {
  if (!force && talk.target === target && Date.now() - talk.listAt < 5000) return;
  try { talk.list = await api(`/api/talk/${enc(target)}`); } catch (e) { toast(e.message, true); return; }
  talk.target = target;
  talk.listAt = Date.now();
}

async function openTalk() {
  if (!snapshot) return;  // the first refresh opens it
  const route = talkRoute();
  const sel = $("talk-target");
  const live = snapshot.targets.filter((tk) => !tk.error).map((tk) => tk.name);
  if ([...sel.options].map((o) => o.value).join() !== live.join()) sel.replaceChildren(...live.map((n) => h("option", { value: n }, n)));
  sel.value = route.target;
  $("talk-new-read").href = talkHash(route.target, null, "read");
  $("talk-new-write").href = talkHash(route.target, null, "write");
  if (!route.target) { $("talk-chat").replaceChildren(empty(t("Önce bir proje ekle."), "box")); return; }
  await loadTalkList(route.target, talk.key !== location.hash);
  renderTalkList(route);
  const key = location.hash;
  if (talk.key === key && $("talk-chat").firstChild?.isConnected) return;
  talk.key = key;
  talk.view = null;
  talk.viewSig = null;
  renderTalkHead(route);
  ChatView.renderTab($("talk-chat"), { target: route.target, id: route.id || "new" }, ctx, talkConv(route));
}

$("talk-target").addEventListener("change", () => { location.hash = talkHash($("talk-target").value); });

function renderTalkList(route) {
  const chats = talk.list?.chats || [];
  const sig = JSON.stringify([route.target, route.id, chats.map((c) => [c.id, c.title, c.live, c.turns, c.mode])]);
  const box = $("talk-list");
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.replaceChildren(...(chats.length ? chats.map((c) => h("li", {},
    h("a", { class: "talk-item" + (c.id === route.id ? " on" : ""), href: talkHash(route.target, c.id), "aria-current": c.id === route.id ? "page" : null },
      h("span", { class: "talk-item-title" }, c.live ? h("span", { class: "dot live" }) : "", c.title || t("(boş sohbet)")),
      h("span", { class: "muted small" }, `${TALK_MODE[c.mode]} · ${plural(t("{0} mesaj"), c.turns)} · ${ago(c.updated_at)}`))))
    : [h("li", { class: "muted small" }, t("Henüz sohbet yok."))]));
}

function renderTalkHead(route) {
  const v = talk.view;
  const c = v?.chat;
  const mode = c?.mode || route.mode;
  const changes = v?.changes;
  const where = mode === "write"
    ? (c?.worktree ? h("span", { class: "muted small mono" }, icon("branch"), c.branch, changes ? t(" · {0} commit · {1} değişmiş dosya", changes.ahead, changes.dirty.length) : "")
      : h("span", { class: "muted small" }, t("İlk mesajla kendi worktree'si açılır (chat/<id> dalı, prepare komutlarıyla).")))
    : h("span", { class: "muted small" }, t("Ana checkout'u okur; dosya değiştirmez, komut çalıştırmaz."));
  $("talk-head").replaceChildren(h("div", { class: "row tight wrap" },
    h("strong", { class: "grow talk-title" }, c?.title || (mode === "write" ? t("Yeni yazan sohbet") : t("Yeni okuyan sohbet"))),
    h("span", { class: `pill ${mode === "write" ? "warn" : "mute"}` }, TALK_MODE[mode]),
    route.id ? h("button", { class: "small", type: "button", title: t("Sohbetten bir ticket taslağı: composer'da düzenleyip oluşturursun"),
      onclick: () => talkToTicket(route) }, icon("ticket"), t("Ticket'a çevir")) : "",
    route.id ? h("button", { class: "small ghost", type: "button", onclick: (e) => deleteTalk(e.currentTarget, route) }, icon("x"), t("Sil")) : ""),
  where);
}

function talkConv(route) {
  const base = `/api/talk/${enc(route.target)}`;
  const known = () => talk.list || { models: [], model: "" };
  const mode = () => talk.view?.chat?.mode || route.mode;
  return {
    key: `talk/${route.target}/${route.id || "new"}`, draftKey: `ziggy.talk.${route.target}.${route.id || `new-${route.mode}`}`,
    threads: false, placeholder: t("{0} hakkında yaz…", route.target),
    // An existing chat's mode and title come with its first load, after the tab is built.
    get writes() { return mode() === "write"; },
    get title() { return talk.view?.chat?.title || t("Sohbet"); },
    empty: mode() === "write"
      ? t("{0} üzerinde kendi worktree'sinde çalışan bir Claude oturumu: dosya düzenler, komut çalıştırır, commit atar.", route.target)
      : t("{0} hakkında her şeyi sorabilirsin: kod nasıl çalışıyor, bir issue ne diyor, nereden başlanır.", route.target),
    suggestions: () => mode() === "write"
      ? [t("Bu repo nasıl kurulu, testler nasıl koşuyor?"), t("Bana atanmış açık issue'ları listele.")]
      : [t("Bu projenin mimarisini özetle."), t("Bana atanmış açık issue'ları listele."), t("Son değişiklikler neler?")],
    info: () => mode() === "write" ? t("Yanıtlar bu sohbetin worktree'sinde çalışan bir Claude oturumundan gelir; push edemez.")
      : t("Yanıtlar salt-okunur bir Claude oturumundan gelir; dosya değiştirmez."),
    load: async (since) => {
      if (!route.id) return { turns: [], live: false, live_turn: null, truncated: false, models: known().models, model: known().model, cwd: "main" };
      const data = await api(`${base}/${route.id}` + (since ? `?since=${enc(since)}` : ""));
      const sig = JSON.stringify([data.chat, data.changes]);
      if (sig !== talk.viewSig) { talk.viewSig = sig; talk.view = data; renderTalkHead(route); }
      return data;
    },
    send: async (text, _fresh, model) => {
      if (route.id) return api(`${base}/${route.id}`, { action: "send", text, ...(model ? { model } : {}) });
      const out = await api(base, { mode: route.mode, text, ...(model ? { model } : {}) });
      talk.listAt = 0;
      location.hash = talkHash(route.target, out.chat.id);
      return out;
    },
    stop: () => api(`${base}/${route.id}`, { action: "stop" }).then(() => toast(t("Yanıt durduruluyor"))),
  };
}

// "Ticket'a çevir": the chat's title and turns in the composer, to edit and create from.
async function talkToTicket(route) {
  let draft;
  try { draft = await api(`/api/talk/${enc(route.target)}/${route.id}/ticket`); } catch (e) { toast(e.message, true); return; }
  location.hash = "#home";
  $("new-target").value = route.target;
  $("new-type").value = "feat";
  $("new-title").value = draft.title;
  $("new-body").value = draft.body;
  composerOpen();
  composerHint();
  setTimeout(() => { $("new-title").focus(); $("composer").scrollIntoView({ block: "center" }); }, 0);
}

async function deleteTalk(btn, route) {
  const write = talk.view?.chat?.mode === "write";
  if (!(await confirmBox(t("Bu sohbet silinsin mi?{0}", write ? t("\n\nWorktree'si ve chat dalı da silinir.") : "")))) return;
  btn.disabled = true;
  const base = `/api/talk/${enc(route.target)}/${route.id}`;
  try {
    await api(base, { action: "delete" });
  } catch (e) {
    // A branch with work on it asks once more, naming what would go.
    if (!/still holds work/.test(e.message) || !(await confirmBox(t("{0}\n\nYine de silinsin mi?", e.message)))) { toast(e.message, true); btn.disabled = false; return; }
    try { await api(base, { action: "delete", force: true }); } catch (e2) { toast(e2.message, true); btn.disabled = false; return; }
  }
  toast(t("Sohbet silindi"));
  talk.listAt = 0;
  location.hash = talkHash(route.target);
}

// --- views --------------------------------------------------------------------------------------

const VIEWS = ["home", "talk", "tickets", "sessions", "stats", "memory", "projects", "project", "ticket"];
// Old addresses: the board is Komuta now, and a review is the ticket page's Değişiklikler tab.
const MOVED = { needs: "#home" };

function currentView() {
  const v = location.hash.slice(1).split(/[/?]/)[0];
  return VIEWS.includes(v) ? v : "home";
}

const hashParams = () => new URLSearchParams(location.hash.split("?")[1] || "");
const currentProject = () => (currentView() === "project" ? decodeURIComponent(location.hash.slice(1).split(/[?]/)[0].split("/")[1] || "") : null);

// #review/<target>/<id>?file=… → #ticket/<target>/<id>?tab=changes&file=…; #needs → #home.
function redirectOld() {
  const old = location.hash.slice(1).split(/[/?]/)[0];
  if (MOVED[old]) { history.replaceState(null, "", MOVED[old]); return; }
  if (old === "review") {
    const p = ReviewPage.parseHash(location.hash);
    history.replaceState(null, "", p ? ReviewPage.hash(p.target, p.id, p.file, p.range, p.show) : "#tickets");
  }
}

// Each page's scroll position by address, so coming back from a ticket lands where the list was left.
const scrollMemo = new Map();
window.addEventListener("scroll", () => scrollMemo.set(location.hash || "#home", scrollY), { passive: true });

function showView(restore) {
  redirectOld();
  const v = currentView();
  for (const el of document.querySelectorAll("main .view")) el.hidden = el.dataset.view !== v;
  const nav = v === "project" ? "projects" : v === "ticket" ? (ticketReturn.startsWith("#project") ? "projects" : ticketReturn.startsWith("#home") ? "home" : "tickets") : v;
  for (const a of document.querySelectorAll(".nav a")) {
    a.classList.toggle("on", a.dataset.view === nav);
    if (a.dataset.view === nav) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  if (v !== "ticket") closeDrawer();
  if (v === "ticket") {
    const params = parseTicketHash(location.hash);
    if (params) showTicket(params);
    return;
  }
  if (v === "stats") loadStats();
  if (v === "memory") loadMemory();
  if (v === "project") openProject(currentProject());
  if (v === "tickets" && snapshot) renderTickets(snapshot);
  if (v === "projects") { loadStrip().then(() => snapshot && projectList($("targets"), snapshot)); loadLeftovers(); }
  if (v === "home") loadLead();
  if (v === "talk") openTalk();
  window.scrollTo(0, 0);
  // The list renders from the snapshot at once; a second frame catches a list that grows with it.
  if (restore) requestAnimationFrame(() => requestAnimationFrame(() => window.scrollTo(0, restore)));
}

window.addEventListener("hashchange", (e) => {
  // Leaving a page for a ticket remembers the way back (Esc, the crumb).
  const from = new URL(e.oldURL).hash || "#home";
  if (currentView() === "ticket" && !from.startsWith("#ticket/")) ticketReturn = from;
  const wasTicket = from.startsWith("#ticket/");
  showView(wasTicket && currentView() !== "ticket" ? scrollMemo.get(location.hash || "#home") : 0);
  if (currentView() === "ticket" && wasTicket) return;  // a tab or a file inside the same page: no jump to the top
});

// The command bar: N, or ⌘K on a Mac and Ctrl+K elsewhere, goes to Komuta and puts the cursor in it. Not while a dialog
// is open, and Ctrl+K never inside a text box (it is the line editor's kill-to-end there).
const MAC = /mac|iphone|ipad/i.test(navigator.userAgentData?.platform || navigator.platform || "");
$("composer").querySelector(".cmd-kbd").textContent = MAC ? "⌘K" : "Ctrl K";
function focusComposer() {
  if (currentView() !== "home") location.hash = "#home";
  setTimeout(() => { $("new-title").focus(); $("composer").scrollIntoView({ block: "nearest" }); }, 0);
}
$("open-new").addEventListener("click", focusComposer);
document.addEventListener("keydown", (e) => {
  if (e.defaultPrevented || overlayOpen()) return;
  const typing = e.target.closest?.("input, textarea, select, [contenteditable='true']");
  if (e.key?.toLowerCase() === "k" && !e.altKey && !e.shiftKey && (MAC ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey && !typing)) {
    e.preventDefault();
    focusComposer();
    return;
  }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "n" || e.key === "N") { e.preventDefault(); focusComposer(); }
});
$("search").addEventListener("input", () => snapshot && renderTickets(snapshot));
$("filter").addEventListener("change", () => snapshot && renderTickets(snapshot));
$("show-old").addEventListener("change", () => snapshot && renderTickets(snapshot));

// --- new project ------------------------------------------------------------------------------

const homeShort = (p) => (p || "").replace(/^\/Users\/[^/]+/, "~");

async function projectDefaults() {
  const q = new URLSearchParams({ parent: $("np-parent").value || "", name: $("np-name").value.trim() || "x" });
  try {
    const d = await api(`/api/projects/defaults?${q}`);
    if (!$("np-parent").options.length) {
      $("np-parent").replaceChildren(...d.parents.map((p) => h("option", { value: p }, homeShort(p))));
      return projectDefaults();
    }
    // Owners: every gh account and its organizations; the value carries the acting account.
    if ($("np-owner").options.length !== (d.owners || []).length) {
      $("np-owner").replaceChildren(...(d.owners || []).map((o) => h("option", { value: `${o.owner}|${o.gh_user}` },
        o.kind === "org" ? t("{0}  (organizasyon · {1} ile)", o.owner, o.gh_user) : t("{0}  (kişisel hesap)", o.owner))));
    }
    if (!$("np-owner").dataset.touched) $("np-owner").value = `${d.gh_user}|${d.gh_user}`;
    const logins = d.claude_logins || [];
    if ($("np-claude").options.length !== logins.length) {
      $("np-claude").replaceChildren(...logins.map((l) => h("option", { value: l.dir || "" },
        `${l.dir ? homeShort(l.dir) : t("Varsayılan giriş (~/.claude)")} — ${l.account || t("giriş yok")}`)));
    }
    if (!$("np-claude").dataset.touched) $("np-claude").value = d.claude_config_dir || "";
    const accounts = logins.map((l) => l.account).filter(Boolean);
    const same = accounts.length > 1 && new Set(accounts).size === 1;
    $("np-info").replaceChildren(
      same ? h("span", { class: "pill warn" }, icon("alert"), t("Bütün Claude girişleri aynı hesapta")) : "",
      same ? h("span", {}, t("Kişisel hesap için terminalde: "), h("code", {}, "cd ~ && command claude /login")) : h("span", {}, t("Klasöre göre önerildi; değiştirebilirsin.")));
  } catch (e) {
    $("np-info").textContent = e.message;
  }
}

$("toggle-project").addEventListener("click", () => {
  $("project").hidden = !$("project").hidden;
  if (!$("project").hidden) { $("np-name").focus(); projectDefaults(); }
});
$("np-parent").addEventListener("change", projectDefaults);
$("np-name").addEventListener("change", projectDefaults);
$("np-owner").addEventListener("change", () => { $("np-owner").dataset.touched = "1"; });
$("np-claude").addEventListener("change", () => { $("np-claude").dataset.touched = "1"; });

$("project-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const [owner, ghUser] = $("np-owner").value.split("|");
  const body = { name: $("np-name").value.trim(), parent: $("np-parent").value, description: $("np-desc").value.trim(),
    owner, gh_user: ghUser, claude_config_dir: $("np-claude").value, private: $("np-private").checked };
  const where = `${homeShort(body.parent)}/${body.name}`;
  if (!(await confirmBox(t("{0} oluşturulsun mu?\n• README, AGENTS.md, docs/PROJECT.md taslağı ve .gitignore ile ilk commit main'e\n• GitHub'da {1}/{2} ({3}) açılıp push edilir; PR'lar {4} hesabıyla\n• Koşular {5} ile\n• Proje pipeline'a eklenir", where, body.owner, body.name, body.private ? "private" : "public", body.gh_user, $("np-claude").selectedOptions[0]?.textContent || t("varsayılan Claude girişi"))))) return;
  const btn = e.submitter;
  btn.disabled = true;
  const log = h("pre", { class: "need-details" }, t("başlatılıyor…"));
  $("project-result").replaceChildren(log);
  try {
    const { job } = await api("/api/projects", body);
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500));
      const j = await api(`/api/jobs/${job.id}`);
      log.textContent = j.tail || t("çalışıyor…");
      if (j.state === "running") continue;
      if (j.state === "ok") {
        toast(t("{0} oluşturuldu ve eklendi", body.name));
        $("project-form").reset();
        delete $("np-owner").dataset.touched;
        delete $("np-claude").dataset.touched;
        $("project").hidden = true;
        refresh();
      } else toast(t("{0}: oluşturulamadı", body.name), true);
      break;
    }
  } catch (err) {
    log.textContent = err.message;
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
});

// --- cleanup ------------------------------------------------------------------------------------

$("toggle-cleanup").addEventListener("click", async () => {
  const box = $("cleanup");
  box.hidden = !box.hidden;
  if (box.hidden) return;
  box.replaceChildren(h("p", { class: "muted small" }, t("PR durumları GitHub'dan okunuyor…")));
  let found;
  try {
    found = (await api("/api/cleanup")).candidates;
  } catch (e) {
    box.replaceChildren(h("p", { class: "error-banner" }, e.message));
    return;
  }
  if (!found.length) {
    box.replaceChildren(empty(t("Temizlenecek worktree yok: PR'ı olan ve worktree'si duran ticket bulunmadı.")));
    return;
  }
  const checks = found.map((c) => {
    const cb = h("input", { type: "checkbox", disabled: !c.cleanable });
    cb.checked = c.cleanable;
    return [c, cb];
  });
  const go = h("button", { class: "danger", onclick: async () => {
    const items = checks.filter(([, cb]) => cb.checked).map(([c]) => ({ target: c.target, id: c.id }));
    if (!items.length) return toast(t("Seçili ticket yok"), true);
    if (!(await confirmBox(t("{0} worktree silinsin mi?\nMerge edilmiş PR'ların yerel dalı da silinir; GitHub'daki dal, PR ve koşu kayıtları kalır.", items.length)))) return;
    try {
      const { results } = await api("/api/cleanup", { items });
      const bad = results.filter((r) => !r.ok);
      toast(bad.length ? t("{0} temizlendi, {1} atlandı: {2}", results.length - bad.length, bad.length, bad.map((r) => r.id + " (" + r.error + ")").join(", ")) : plural(t("{0} worktree temizlendi"), results.length), bad.length > 0);
      box.hidden = true;
      refresh();
    } catch (e) {
      toast(e.message, true);
    }
  } }, icon("x"), t("Seçilenleri temizle"));
  box.replaceChildren(h("div", { class: "cleanup" },
    h("div", { class: "muted small" }, t("PR'ı merge edilmiş ya da kapanmış ticket'ların worktree'leri. Değişikliği ya da PR'da olmayan commit'i olan atlanır.")),
    h("ul", { class: "checks" }, checks.map(([c, cb]) => h("li", { class: c.cleanable ? "ok" : "warn" },
      h("label", { class: "check" }, cb, h("span", { class: "id" }, c.id), " ", c.title),
      h("span", { class: `pill ${c.pr_state === "MERGED" ? "ok" : c.pr_state === "CLOSED" ? "mute" : "warn"}` }, (c.pr_state || "?").toLowerCase()),
      c.cleanable ? h("span", { class: "muted small" }, c.delete_branch ? t("worktree + yerel dal") : t("worktree (dal kalır)")) : h("span", { class: "muted small" }, c.reasons.join("; "))))),
    h("div", { class: "row end" }, h("button", { class: "ghost", onclick: () => { box.hidden = true; } }, t("Vazgeç")), go)));
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !$("modal").hidden) return;
  if (!$("ask-modal").hidden) return $("ask-no").click();
  if (!$("inbox-pop").hidden) return closeInbox();
  if (!$("doc-modal").hidden) { $("doc-modal").hidden = true; return; }
  if (overlayOpen()) return;  // another dialog (the PR draft form, the transcript) closes itself
  // Esc in a text box only leaves the box (the chat uses it to stop an answer); elsewhere it leaves the ticket.
  if (currentView() === "ticket" && !e.target.closest?.("input, textarea, select")) leaveTicket();
});

// Any dialog on screen: the page's own modals, review.js's PR form, the session transcript, a <dialog>.
function overlayOpen() {
  return [...document.querySelectorAll(".modal, .session-drawer-backdrop, dialog[open]")].some((el) => !el.hidden);
}

// Whether the user can see the element now: the tab is in front, it is laid out and part of it is in the viewport.
function onScreen(el) {
  if (document.hidden || !el?.getClientRects().length) return false;
  const r = el.getBoundingClientRect();
  return r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

// One clock for every elapsed time on the page.
setInterval(() => { for (const el of document.querySelectorAll("[data-since]")) el.textContent = elapsed(el.dataset.since); }, 1000);

// --- language -----------------------------------------------------------------------------------
// "EN · TR" at the foot of the sidebar: the panel's language. i18n.js keeps the choice (localStorage) and reloads.
function languageSwitch() {
  const choice = (lang, label, name) => {
    const on = I18N.lang === lang;
    return h("button", { type: "button", lang, title: name, "aria-pressed": String(on), onclick: () => { if (!on) setLanguage(lang); },
      style: `font:inherit;font-size:12.5px;letter-spacing:.04em;padding:2px 4px;border:0;background:none;cursor:${on ? "default" : "pointer"};`
        + `color:${on ? "var(--text)" : "var(--muted)"};font-weight:${on ? "700" : "400"};text-decoration:${on ? "underline" : "none"};text-underline-offset:3px` }, label);
  };
  return h("div", { class: "lang-switch", role: "group", "aria-label": t("Dil"), style: "display:flex;align-items:center;gap:2px;margin-top:6px" },
    choice("en", "EN", "English"), h("span", { "aria-hidden": "true", style: "color:var(--muted)" }, "·"), choice("tr", "TR", "Türkçe"));  // i18n-ok: a language is named in its own language
}
document.querySelector(".side-foot").append(languageSwitch());

showView();
refresh();
setInterval(refresh, 3000);
