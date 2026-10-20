"use strict";
// Ziggy panel. Every text goes through text nodes (h(), MD), never innerHTML: ticket
// text, plans and agent output are untrusted.

// The panel was called bmad-pipeline: its saved choices lived under the old key names. Copy each
// one to its new name once (unless the new one is already set), then drop the old key.
(() => {
  const rename = (key) => key.startsWith("bmad-pipeline-choice:") ? `ziggy.choice:${key.slice(21)}`
    : key === "bmad-lang" ? "ziggy.lang" : key.startsWith("bmad.") ? `ziggy.${key.slice(5)}` : null;
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
const STATUS_TR = {
  "new": "yeni", "queued": "kuyrukta", "running": "çalışıyor", "needs-user": "soru var", "awaiting-approval": "onay bekliyor",
  "done": "bitti", "failed": "başarısız", "error": "hata", "waiting": "bekliyor", "split": "dilimlere bölündü", "quota": "kota bekliyor",
  "host": "bellek bekliyor",
  // v1 tickets keep their old status; shown as is
  "planning": "planlanıyor", "building": "yazılıyor", "ready-for-dev": "spec hazır", "blocked": "takıldı", "draft": "taslak",
  "in-progress": "yazılıyor", "in-review": "review'da",
};
const TYPE_TR = { feat: "özellik", fix: "hata", refactor: "refactor", chore: "bakım", docs: "docs", project: "PROJECT.md" };
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

function ago(iso) {
  if (!iso) return "-";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "az önce";
  if (s < 3600) return `${Math.floor(s / 60)} dk önce`;
  if (s < 86400) return `${Math.floor(s / 3600)} sa önce`;
  return `${Math.floor(s / 86400)} gün önce`;
}

function duration(sec) {
  sec = Math.max(0, sec);
  const days = Math.floor(sec / 86400), hrs = Math.floor((sec % 86400) / 3600), min = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  if (days) return `${days} g ${hrs} sa`;
  if (hrs) return `${hrs} sa ${String(min).padStart(2, "0")} dk`;
  if (min) return `${min} dk ${String(s).padStart(2, "0")} sn`;
  return `${s} sn`;
}

const elapsed = (iso) => (iso ? duration((Date.now() - new Date(iso).getTime()) / 1000) : "");
// A span the 1 s clock keeps current, so a card does not have to be rebuilt to tick.
const since = (iso, cls) => h("span", { class: "since " + (cls || ""), "data-since": iso }, elapsed(iso));
const money = (v) => (typeof v === "number" ? (v > 0 && v < 0.005 ? "<$0.01" : `$${v.toFixed(2)}`) : "-");
const when = (iso) => (iso ? new Date(iso).toLocaleString("tr-TR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "-");
const badge = (status) => h("span", { class: `badge ${status}` }, h("span", { class: "badge-dot" }), STATUS_TR[status] || status);
const chip = (iconName, text, cls) => h("span", { class: "meta" + (cls ? " " + cls : ""), title: typeof text === "string" ? text : null },
  icon(iconName), h("span", { class: "meta-text" }, text));
const link = (href, ...kids) => h("a", { href, target: "_blank", rel: "noopener noreferrer" }, ...kids);
const empty = (text, iconName) => h("div", { class: "empty" }, icon(iconName || "check"), h("span", {}, text));
const mdNode = (text) => h("div", { class: "md" }, MD.blocks(text || ""));

function toast(text, bad) {
  const t = $("toast");
  t.replaceChildren(icon(bad ? "alert" : "check"), h("span", {}, text));
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, bad ? 9000 : 4000);
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
  run: (t) => t.status === "host"
    ? `${t.id} şimdi başlatılsın mı?\nDikkat: makine bellekte zorlanıyor (${snapshot?.host?.text || "bellek dolu"}). Şimdi başlarsa komutları çok yavaş koşabilir ve zaman aşımına uğrayabilir.`
    : t.status === "quota"
    ? `${t.id} şimdi başlatılsın mı?\nDikkat: ${t.quota?.label || "Claude hesabı"} kullanım sınırında; ${clockAt(t.quota?.resume_at)} sıfırlanıyor. Şimdi başlarsa oturum hemen yine reddedilebilir.`
    : t.status === "failed"
    ? `${t.id} kaldığı aşamadan (${t.stage || "?"}) yeniden koşsun mu?\nAşama baştan, yeni bir oturumla çalışır; birkaç dolar tutabilir.`
    : `${t.id} başlatılsın mı?\n${t.pipeline || pipelineFor(t.type)} hattı aşama aşama koşar; birkaç dolar ve 10–30 dakika tutabilir.`,
  resume: (t) => `${t.id}: yarıda kalan oturum kaldığı yerden sürdürülsün mü?\nAynı Claude oturumu devam eder (claude --resume); bitmiş adımlar tekrarlanmaz.`,
  stop: (t) => t.queued_at ? `${t.id} kuyruktan çıkarılsın mı?\nHenüz başlamadı; ticket olduğu yerde kalır.`
    : `${t.id} durdurulsun mu?\nYarım kalan değişiklikler stash'e alınır, ticket 'başarısız' olur. Sonra 'Tekrar' ile aşamayı yeniden koşturabilir ya da yön değiştiren bir not yazabilirsin.`,
  approve: (t) => `${t.id}: plan onaylansın ve koşu devam etsin mi?\n${t.gate?.stage ? `${t.gate.stage} aşamasından sonraki adım` : "Sıradaki aşama"} hemen başlar.`,
  publish: (t) => t.pull ? `${t.id}: düzeltme commit'leri PR #${t.pull.number}'in kendi dalına (${t.pull.head}) push edilsin mi?\n${t.pr}`
      + (t.pull_foreign ? `\n\nDikkat: bu PR ${t.pull.author} kullanıcısının; commit'ler onun dalına gider.` : "")
    : t.pr ? `${t.id}: ${t.branch} dalındaki yeni commit'ler push edilip PR güncellensin mi?\n${t.pr}`
    : `${t.id}: ${t.branch} dalı ${t.repo || "GitHub"} reposuna push edilip draft PR açılsın mı?\nReady yapmak ve merge sende kalır.`
      + (t.stack_base ? `\n\nYığının dilimi: PR'ın tabanı ${t.base_branch}. Alttaki dilimlerin PR'ı yoksa önce onlar (draft olarak) açılır.` : "")
      + protectedWarning(t),
  protected_apply: (t) => `${t.id}: korumalı dosya değişikliği (${(t.needs_you?.patch?.files || []).join(", ")}) ${t.branch} dalına uygulanıp commit edilsin mi?\n`
    + (t.needs_you?.patch?.replaces?.length
      ? `Bu yama ${replacesText(t.needs_you.patch)}: dosyanın içeriği bu yamanın hazırlandığı hâline (önceki uygulanan değişiklik olmadan) bu yama eklenerek yazılır.`
      : "Yama dalın şimdiki HEAD'inde yeniden denenir; temiz uygulanmazsa hiçbir şey değişmez.")
    + (t.pr ? "\n\nPR açık: commit sonrası 'PR'ı güncelle' ile gönderirsin." : ""),
  review: (t) => `${t.id} dalı Codex'e review ettirilsin mi?\n${t.base_branch}'e göre fark incelenir (1–3 dk, ChatGPT hesabından). Dala dokunulmaz.`,
  split: (t) => `${t.id}: plan ${t.split_offer?.length || "?"} dilime bölünsün mü?\nHer dilim kendi ticket'ı, dalı ve draft PR'ı olur; her dal bir alttakinin üstüne yığılır. `
    + "İlk dilim hemen başlar (onay verilmiş sayılır), diğerleri bir önceki bitince sırayla. Bu ticket dilimlerin kabı olur, kendisi kod yazmaz.",
  restack: (t) => `${t.id}: ${t.branch} dalı ${t.restack?.onto || "yeni tabanı"} üstüne yeniden düzenlensin mi (git rebase --onto)?\n`
    + "Çakışma olursa dal olduğu gibi kalır. Sonuç gösterilir; PR'a push ayrıca senin onayınla yapılır.",
  restack_push: (t) => `${t.id}: yeniden düzenlenen ${t.branch} dalı PR'ına force-with-lease ile push edilsin mi?\n${t.pr || ""}\n`
    + "Yalnızca bu ticket'ın kendi dalı; son push'tan beri dala başkası push ettiyse push reddedilir.",
};
const ACTION_DONE = {
  resume: "oturum kaldığı yerden sürüyor", review: "Codex review başladı", fix: "bulgular plana eklendi, düzeltme koşusu başladı",
  dismiss: "kapatıldı; aynı olay bir daha gösterilmez",
  post_review: "review PR'a yorum olarak eklendi", answer: "cevap kaydedildi, koşu devam ediyor", approve: "onaylandı, koşu devam ediyor",
  run: "başladı", publish: "push ve PR başladı", cleanup: "worktree temizleniyor; dal kalıyor",
  split: "dilimlere bölündü; ilk dilim başladı", restack: "yeniden düzenleniyor", restack_push: "yeniden düzenlenen dal push ediliyor",
  protected_apply: "korumalı dosya değişikliği uygulandı ve commit edildi", protected_reject: "korumalı dosya değişikliği reddedildi",
};
// Publishing never waits for a protected file change; the confirm (and the review page's PR form) only says one waits.
const protectedWarning = (t) => (t.protected_pending?.length
  ? `\n\nOnay bekleyen korumalı dosya değişikliği var (${[...new Set(t.protected_pending.flat())].join(", ")}); PR onsuz açılır.` : "");
const NUDGE_DONE = {
  interrupt: "aşama durduruluyor; kendi oturumunda mesajınla devam edecek",
  queued: "mesaj kaydedildi; aşama başlayınca önce onu okuyacak",
};

const pipelineFor = (type) => ({ feat: "feat", fix: "fix", project: "project" }[type] || "chore");

async function act(t, action, text, extra) {
  if (ACTION_CONFIRM[action] && !(await confirmBox(ACTION_CONFIRM[action]({ ...t, repo: targetRepo(t.target) })))) return false;
  try {
    if (action === "publish" && t.pull_foreign) text = "foreign-ok";  // the confirm above named the owner
    const out = await api("/api/action", { target: t.target, id: t.id, action, text, ...(extra || {}) });
    if (out.job?.queued) { toast(`${t.id}: kuyruğa alındı; projedeki koşu slotları dolu, sırası gelince başlar`); refresh(); return true; }
    const label = action === "stop" ? (t.queued_at ? "kuyruktan çıkarıldı" : "durdurma istendi")
      : action === "nudge" ? NUDGE_DONE[out.nudged?.delivery] || out.nudged?.message : ACTION_DONE[action];
    toast(`${t.id}: ${label || action + " başladı"}`);
    refresh();
    return true;
  } catch (e) {
    toast(`${t.id}: ${e.message}`, true);
    return false;
  }
}

const targetRepo = (name) => (snapshot?.targets.find((x) => x.name === name) || {}).github_repo;
const rowOf = (target, id) => snapshot?.tickets.find((x) => x.target === target && x.id === id);

// A waiting ticket's line: what it waits for ("Bekliyor: local-3 bitince").
const waitText = (t) => (t.waiting_for?.length ? `Bekliyor: ${t.waiting_for.map((d) => d.id).join(", ")} bitince` : "Bekliyor");

// What a running row says: waiting in line for a run slot, waiting out a retry backoff, or the stage under way.
function runLabel(t) {
  if (t.queued_at) return `kuyrukta · ${ago(t.queued_at)} beri`;
  const stage = t.stage || t.phase || t.last_job?.command || "hazırlanıyor";
  if (t.retry_at) return `${stage} · ${duration(Math.max(0, (new Date(t.retry_at).getTime() - Date.now()) / 1000))} sonra yeniden denenecek`;
  return `${stage}${t.visit > 1 ? ` #${t.visit}` : ""}${t.model ? " · " + t.model : ""}`;
}

function actionButtons(t, compact) {
  const cls = compact ? "small" : "";
  const b = (iconName, label, action, extra) =>
    h("button", { class: `${cls} ${extra || ""}`, onclick: (e) => { e.stopPropagation(); act(t, action); } }, icon(iconName), label);
  const open = (iconName, label, tab, extra) =>
    h("button", { class: `${cls} ${extra || ""}`, onclick: (e) => { e.stopPropagation(); openDrawer(t, tab); } }, icon(iconName), label);
  if (t.running && t.last_job?.command?.startsWith("review")) return [h("button", { class: cls, disabled: true }, icon("eye"), "Codex inceliyor…")];
  if (t.running && t.last_job?.state === "running" && t.last_job.command.startsWith("cleanup")) return [h("button", { class: cls, disabled: true }, icon("x"), "Temizleniyor…")];
  if (t.running) {
    return [
      t.legacy ? "" : h("button", { class: cls, title: "Ajana mesaj gönder: çalışmayı bırakmadan yön verir",
        onclick: (e) => { e.stopPropagation(); nudgeDialog(t); } }, icon("message"), "Yönlendir"),
      b("stop", t.queued_at ? "Kuyruktan çıkar" : "Durdur", "stop", "danger"),
    ];
  }
  if (t.archived || t.status === "error") return [];  // worktree removed: nothing to run, review or push
  const out = [];
  if (t.legacy) {  // made by the BMad version: publish, review and clean up still work, runs do not
    if (t.status === "done" && !t.pr) out.push(b("pr", "Draft PR aç", "publish", "primary"));
    if (t.status === "done" && t.needs_you?.kind === "push" && !t.pull?.fork) out.push(b("upload", "PR'ı güncelle", "publish", "primary"));
    return out;
  }
  if (t.status === "new") out.push(b("play", "Başlat", "run"));
  if (["quota", "host"].includes(t.status)) {  // held for the login's limit or the machine's memory: it goes on by itself; this starts it now (with a warning)
    out.push(h("button", { class: cls, onclick: (e) => { e.stopPropagation(); act(t, "run", "", { now: true }); } }, icon("play"), "Şimdi başlat"));
  }
  if (t.status === "needs-user") out.push(open("message", "Cevapla", "questions", "primary"));
  if (t.status === "awaiting-approval") {
    out.push(b("check", "Onayla", "approve", "primary"));
    if (t.split_offer) out.push(b("layers", "Dilimlere böl", "split"));
    out.push(open("spec", "Plan", "plan"));
  }
  if (t.restack) {
    out.push(t.restack.rebased ? b("upload", "Düzenlenen dalı push et", "restack_push", "primary") : b("retry", "Yeniden düzenle", "restack", "primary"));
  }
  if (t.status === "failed") {
    if (t.resumable) out.push(b("play", "Devam", "resume", "primary"));
    if (t.recheckable) out.push(b("check", "Yalnız check'ler", "recheck"));
    out.push(b("retry", "Tekrar", "run"));
  }
  if (t.status === "done" && !t.pr) out.push(b("pr", "Draft PR aç", "publish", "primary"));
  if (t.status === "done" && t.needs_you?.kind === "push" && !t.pull?.fork) out.push(b("upload", t.pull ? "PR'a gönder" : "PR'ı güncelle", "publish", "primary"));
  if (t.drafts) out.push(h("a", { class: `button ${cls}`, href: ReviewPage.hash(t.target, t.id), onclick: (e) => e.stopPropagation() }, icon("diff"), `Yorumlar (${t.drafts})`));
  if (t.status === "done" && !["MERGED", "CLOSED"].includes(t.pr_state)) out.push(...reviewStep(t, cls));
  if (!compact && t.worktree && ["done", "failed"].includes(t.status)) {
    out.push(h("button", { class: "ghost", title: "Worktree'yi sil (önce teardown); dal kalır",
      onclick: (e) => { e.stopPropagation(); cleanupDialog(t); } }, icon("x"), "Temizle"));
  }
  return out;
}

// What the latest Codex review leaves to do: nothing but the PR when it is clean and still covers
// the branch, the findings when it has some, and a new review when there is none or the branch moved on.
function reviewStep(t, cls) {
  const r = t.review;
  const again = (label) => h("button", { class: cls, onclick: (e) => { e.stopPropagation(); act(t, "review"); } }, icon("eye"), label);
  if (!r || r.state !== "ok") return [again("Codex review")];
  if (r.stale) return [again("Yeniden review")];
  if (r.findings && !t.legacy) return [h("button", { class: `${cls}`, onclick: (e) => { e.stopPropagation(); openDrawer(t, "review"); } }, icon("hammer"), "Bulguları düzelt")];
  if (t.pr && t.needs_you?.kind !== "push") return [h("a", { class: `button ${cls}`, href: t.pr, target: "_blank", rel: "noopener noreferrer", onclick: (e) => e.stopPropagation() }, icon("pr"), "PR'ı aç", icon("external"))];
  return [];
}

// --- pipeline bar -------------------------------------------------------------------------------
// One step per stage of the ticket's pipeline, from the engine's stages_view. The running step
// takes the ticket's state (running or queued); while the ticket waits for a question's answer or
// an approval, the stage that stopped (st.stage) shows as waiting.

const STEP_TR = { done: "bitti", active: "çalışıyor", waiting: "seni bekliyor", queued: "kuyrukta", failed: "başarısız", stopped: "durdu", pending: "başlamadı", skipped: "atlandı" };

function stepState(t, s) {
  if (s.state === "current") return t.queued_at || ["quota", "host"].includes(t.status) ? "queued" : t.running ? "active" : "stopped";
  if (s.id === t.stage && ["needs-user", "awaiting-approval"].includes(t.status)) return "waiting";
  return s.state;
}

function pipelineBar(t, compact) {
  const stages = t.stages || [];
  if (!stages.length || t.status === "split") return "";  // a split ticket runs nothing more; its slices do
  const states = stages.map((s) => stepState(t, s));
  const label = "Hat: " + stages.map((s, i) => `${s.title || s.id} ${STEP_TR[states[i]] || states[i]}${s.visits > 1 ? ` ×${s.visits}` : ""}`).join(", ");
  return h("ol", { class: "pbar" + (compact ? " compact" : ""), role: "img", "aria-label": label, title: label },
    stages.map((s, i) => h("li", { class: `pstep pstep-${states[i]}` },
      h("span", { class: "pstep-bar" }),
      compact ? "" : h("span", { class: "pstep-name" }, s.title || s.id, s.visits > 1 ? ` ×${s.visits}` : ""))));
}

const typeTag = (t) => h("span", { class: "type" + (t.type === "project" ? " project" : "") }, t.type || "");
const bmadTag = (t) => t.pipeline === "bmad" ? h("span", { class: "tag", title: "BMad build-auto planlar ve kurar; kontroller, review ve PR bu pipeline'ın" }, "BMad hattı") : "";

// BMad in a project (snapshot targets[].bmad): installed on the base branch, and this machine's per-developer files.
function bmadText(b) {
  if (!b?.installed) return "BMad yok";
  return b.missing_local?.length ? "BMad kurulu · yerel dosya eksik" : `BMad kurulu${b.version ? ` (${b.version})` : ""}`;
}
function bmadPill(t) {
  const b = t.bmad;
  if (!b) return "";
  const cls = !b.installed ? "mute" : b.missing_local?.length ? "warn" : "ok";
  const title = !b.installed ? b.reason || "Taban dalda BMad yok"
    : b.missing_local?.length ? `Ana checkout'ta eksik (BMad kurulumunu orada çalıştır):\n${b.missing_local.join("\n")}` : b.reason || "";
  return h("span", { class: `pill ${cls}`, title }, bmadText(b));
}
const bmadReady = (b) => !!(b?.installed && !b.missing_local?.length);

// Controlled projects (mode = "controlled"): every plan waits for approval, dependants ask before they start, no PR replies.
const CONTROLLED_TITLE = "Kontrollü mod: her ticket plan onayında durur, bağımlı ticket'lar sormadan başlamaz, PR yorumlarına yanıt yazılmaz";
const controlledPill = (x) => x.mode === "controlled" ? h("span", { class: "pill info", title: CONTROLLED_TITLE }, "Kontrollü") : "";
const controlledTag = (x) => x.mode === "controlled" ? h("span", { class: "tag", title: CONTROLLED_TITLE }, "Kontrollü") : "";

// A time as the cards say it ("15:05", "17.10 09:00" when not today) and with the Turkish locative ("15:05'te"),
// read by its last word: 05 "beş" → 'te, 30 "otuz" → 'da, 15:00 "on beş" → 'te.
function clockText(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "?";
  const time = d.toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? time : `${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")} ${time}`;
}
const LOC_ONES = ["", "de", "de", "te", "te", "te", "da", "de", "de", "da"];
const LOC_TENS = { 0: "da", 10: "da", 20: "de", 30: "da", 40: "ta", 50: "de" };
function clockAt(iso) {
  const text = clockText(iso);
  const [hh, mm] = text.split(" ").pop().split(":").map(Number);
  const n = mm || hh || 0;
  return `${text}'${LOC_ONES[n % 10] || LOC_TENS[n - (n % 10)] || "de"}`;
}
// A ticket held for its Claude login's usage limit (status quota): rejected mid-stage it continues its session, held
// before a stage it starts it.
const quotaText = (t) => `Kota doldu · ${clockAt(t.quota?.resume_at)} ${t.quota?.reason === "rejected" ? "devam" : "başlar"}`;
const quotaDetail = (t) => `${t.quota?.label || "Claude hesabı"} kullanım sınırına ulaştı. `
  + (t.quota?.reason === "rejected" ? `${t.quota?.stage || "Aşama"} oturumu ${clockAt(t.quota?.resume_at)} kaldığı yerden devam eder.`
    : `${t.quota?.stage || "Sıradaki aşama"} ${clockAt(t.quota?.resume_at)} başlar.`);
// A ticket held while the machine is short of memory (status host): it starts after two calm panel ticks.
const HOST_TEXT = "Makine bellekte zorlanıyor · bekliyor";
const hostDetail = (t) => `${t.host?.stage || "Sıradaki aşama"} başlamadı: ${snapshot?.host?.text || "bellek dolu"}. `
  + "Makine iki dakika üst üste rahat olunca kendiliğinden başlar (her dakika bir ticket).";

// --- health bar ---------------------------------------------------------------------------------
// One line: "Her şey yolunda" or "N sorun", the quiet facts inline, problems as full chips below.

function healthItems(s) {
  const items = [];
  const connOld = Date.now() - lastOkAt > 45000;
  items.push({ key: "panel", tone: connOld ? "bad" : "ok", compact: `güncellendi ${new Date(lastOkAt).toLocaleTimeString("tr-TR")}`,
    title: "Panel", text: connOld ? "45 sn'dir güncellenmiyor" : `çalışıyor · ${duration(s.uptime_s)}` });
  items.push({ key: "claude", tone: s.claude_cli ? "ok" : "bad", compact: s.claude_cli ? null : "claude CLI yok",
    title: "Claude CLI", text: s.claude_cli ? "bulundu" : "bulunamadı: koşular başlayamaz (ZIGGY_CLAUDE ya da PATH)" });
  for (const t of s.targets) {
    if (t.error) { items.push({ key: `t-${t.name}`, tone: "bad", compact: `${t.name}: config hatası`, title: t.name, text: t.error }); continue; }
    const used = (t.active || []).length;
    items.push({ key: `t-${t.name}`, tone: "ok", compact: null, title: t.name, text: `${used}/${t.max_active} koşu slotu dolu` });
    (t.warnings || []).forEach((w, i) => items.push({ key: `w-${t.name}-${i}`, tone: "warn", compact: null, title: `${t.name} config`, text: w }));
  }
  const sync = s.pr_sync || {};
  items.push({ key: "sync", tone: sync.error ? "warn" : sync.last ? "ok" : "muted", compact: sync.error ? "PR takibi hata veriyor" : null,
    title: "PR takibi", text: sync.error || (sync.last ? `son kontrol ${ago(sync.last)}` : "henüz kontrol edilmedi") });
  // One chip per Claude login: its windows ("5 sa %18 · 7 gün %11"), warn from 80 %, bad while it is over its limit.
  for (const q of s.quota || []) {
    items.push({ key: `q-${q.key}`, tone: q.tone, compact: null, title: q.label,
      text: [q.blocked ? `Kota doldu · ${clockAt(q.blocked_until)} sıfırlanır` : "", quotaWindows(q) || "henüz kota bilgisi yok"].filter(Boolean).join(" · ") });
  }
  // The machine's memory: a chip only while pressure is warn/critical or swap is 75 % full (bad while it holds stages).
  const m = s.host;
  if (m) {
    items.push({ key: "host", tone: m.show ? m.tone : "ok", compact: m.show ? m.text : null, title: "Makine",
      text: [m.text, m.load1 != null ? `yük ${m.load1.toFixed(1)}` : "", m.busy ? "yeni aşamalar bekliyor" : m.enabled === false ? "bekletme kapalı" : ""].filter(Boolean).join(" · ") });
  }
  return items;
}

const quotaWindows = (q) => q.windows.map((w) => `${w.label} %${w.percent}`).join(" · ");

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
    h("span", { class: "quota-wins" }, q.blocked ? h("span", { class: "quota-win bad" }, `Kota doldu · ${clockText(q.blocked_until)}`) : "",
      ...(q.windows.length ? q.windows.map((w) => h("span", { class: `quota-win ${w.tone}`, title: w.resets_at ? `${clockAt(w.resets_at)} sıfırlanır` : "" }, `${w.label} %${w.percent}`))
        : [h("span", { class: "quota-win muted" }, "henüz bilgi yok")])))));
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
      problems.length ? h("span", { class: `health-sum ${tone}` }, h("span", { class: `dot ${tone}` }), `${problems.length} sorun`)
        : h("span", { class: "health-sum ok" }, h("span", { class: "dot ok" }), "Her şey yolunda"),
      ...items.filter((i) => i.compact && !isProblem(i)).map((i) => h("span", { class: "sys-item" }, h("span", { class: `dot ${i.tone}` }), i.compact)),
      h("span", { class: "grow" }),
      h("button", { class: "small ghost", onclick: () => { store.set(localStorage, "ziggy.system.open", open ? "0" : "1"); renderHealth(snapshot); } }, open ? "Ayrıntıları gizle" : "Ayrıntılar")),
    (problems.length || open) ? h("div", { class: "health-chips" }, (open ? items : problems).map(chipOf)) : "");
}

// --- board ----------------------------------------------------------------------------------------
// One board for the home page and #needs: what waits on you, branches ready for review, agents at
// work and PRs on their way. The server groups and sorts (attention.board_from); each card leads
// with the next step (data-primary), and j/k/Enter walk the cards.

const NEED_TITLE = {
  question: "Ajanın soruları var", approval: "Plan onayını bekliyor", publish: "Bitti, PR açılmadı",
  failed: "Koşu başarısız oldu", "job-error": "Komut hata verdi", error: "Ticket state'i okunamıyor",
  push: "Dal PR'dan sonra değişti", "merged-kept": "PR kapandı ama worktree'de iş kaldı",
  quiet: "Oturum bir süredir sessiz", stall: "Komut uzun sürüyor", lost: "Koşunun süreci ölmüş", memory: "Hafıza",
  blocked: "Beklediği ticket takıldı", restack: "Yeniden düzenleme gerekiyor", start: "Başlatılmayı bekliyor", quota: "Kota doldu",
  lead: "İş önerileri", protected: "Korumalı dosya değişikliği",
};
const NEED_ICON = { "merged-kept": "folder", quiet: "clock", stall: "clock", lost: "alert", question: "message", approval: "shield", publish: "pr", failed: "alert", "job-error": "terminal", error: "alert", push: "upload", "pr-feedback": "pr", memory: "spec",
  blocked: "clock", restack: "branch", start: "play", quota: "clock", lead: "zap", protected: "shield" };
const RISK_PILL = { low: "ok", medium: "warn", high: "bad" };
const BOARD_GROUPS = [
  ["waiting_on_you", "Seni bekliyor", "sorular, onaylar, hatalar, PR yorumları"],
  ["ready_for_review", "Review'a hazır", "PR'ı açılmamış biten dallar"],
  ["working", "Çalışıyor", "şu an koşan ajanlar"],
  ["waiting_on_others", "Sırada", "başka ticket'ı bekleyenler, dilimlere bölünenler"],
  ["landing", "Teslimde", "açık PR'lar"],
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
    $("komuta-title").replaceChildren(...(mine ? [where + "Sende ", h("span", { class: "volt-num" }, String(mine)), " iş var"]
      : [where + (working ? "Ajanlar çalışıyor; sende iş yok" : "Sende bekleyen iş yok")]));
  }
  renderSlots(s);
  renderQuota(s);
  renderHomeFilter(s);
  boardInto($("home-board"), s);
}

// The sidebar's run slots: per project, used / max (a used slot is a run under way), and the queue.
function renderSlots(s) {
  const live = s.targets.filter((t) => !t.error);
  const queued = s.tickets.filter((t) => t.running && t.queued_at).length;
  const sig = JSON.stringify([live.map((t) => [t.name, (t.active || []).length, t.max_active]), queued]);
  const box = $("side-slots");
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.replaceChildren(...live.map((t) => {
    const used = (t.active || []).length;
    return h("div", { class: "slot-row" + (used ? " busy" : "") }, h("span", { class: "slot-name", title: t.name }, t.name), h("span", { class: "slot-n" }, `${used}/${t.max_active}`));
  }), queued ? h("div", { class: "slot-row queued" }, h("span", {}, "kuyrukta"), h("span", { class: "slot-n" }, String(queued))) : "");
}

function boardInto(box, s) {
  const groups = BOARD_GROUPS.map(([key, title, hint]) => ({ key, title, hint,
    items: ((s.board || {})[key] || []).filter((e) => inHome(e.target)).map((e) => ({ e, t: rowOf(e.target, e.id) })).filter((x) => x.t) }));
  // Re-render only on change, and never under a cursor: an answer may be half typed.
  const sig = JSON.stringify([s.targets.length > 1, homeTarget(), groups.map((g) => [g.key, g.items.map(({ e, t }) => [e, t.status, t.running, t.resumable, t.needs_you,
    t.stage, t.phase, t.model, t.visit, t.activity, t.last_text, t.cost_usd, t.queued_at, t.retry_at, t.stages, t.pr, t.pr_state, t.drafts, t.dismissed, t.review, t.run_started_at,
    t.waiting_for, t.children, t.restack, t.split_offer, t.quota, t.mode, t.recheckable])])]);
  if (sig === box.dataset.sig || (box.contains(document.activeElement) && document.activeElement.tagName === "TEXTAREA")) return;
  box.dataset.sig = sig;
  const focused = box.contains(document.activeElement) ? document.activeElement.closest("[data-board-card]")?.dataset.key : null;
  if (!groups.some((g) => g.items.length)) {
    const free = s.targets.filter((t) => !t.error && inHome(t.name)).reduce((n, t) => n + t.max_active - (t.active || []).length, 0);
    box.replaceChildren(h("div", { class: "idle" }, icon("check"),
      h("span", { class: "muted" }, `${homeTarget() ? `${homeTarget()} projesinde s` : "S"}eni bekleyen bir şey yok, çalışan ajan da yok. ${free} slot boş: yukarıdaki komut çubuğundan bir iş ver ya da listeden başlat.`)));
    placeNext(box);
    return;
  }
  // "Seni bekliyor" needs no heading: the page title counts it. Its first card is the hero (the next move).
  box.replaceChildren(...groups.filter((g) => g.items.length).map((g) => h("section", { class: `board-group ${g.key}` },
    g.key === "waiting_on_you" ? "" : h("h2", { class: "board-group-h" }, g.key === "working" ? h("span", { class: "dot live" }) : "", g.key === "working" ? "Canlı" : g.title,
      h("span", { class: "group-n" }, String(g.items.length)), h("span", { class: "muted small" }, g.hint)),
    h("div", { class: g.key === "waiting_on_you" ? "att-grid" : g.key === "working" ? "lane-grid" : "live-grid" },
      g.items.map(({ e, t }, i) => boardCard(g.key, e, t, s, i))))));
  placeNext(box);
  if (focused) box.querySelector(`[data-board-card][data-key="${CSS.escape(focused)}"] .live-title`)?.focus();
}

function boardCard(group, e, t, s, index) {
  const card = group === "waiting_on_you" && t.needs_you ? needCard(t)
    : group === "working" ? liveCard(t, s)
    : group === "ready_for_review" ? reviewCard(t)
    : group === "waiting_on_others" ? (t.status === "split" ? splitCard(t) : t.status === "quota" ? quotaCard(t) : t.status === "host" ? hostCard(t) : waitingCard(t))
    : t.status === "split" ? splitCard(t) : landingCard(t);
  card.setAttribute("data-board-card", "");
  card.dataset.key = `${t.target}/${t.id}`;
  if (e.unread) card.classList.add("unread");
  if (group === "waiting_on_you" && index === 0) card.classList.add("hero");
  return card;
}

// The card's title: the j/k target, which opens the drawer.
function cardTitle(t) {
  return h("button", { class: "link-btn live-title", title: t.title, onclick: () => openDrawer(t) }, t.title);
}

function cardHead(t, extra) {
  return h("div", { class: "run-head" },
    t.needs_you && !t.needs_you.seen_at ? h("span", { class: "dot unread-dot", title: "okunmadı" }) : "",
    h("span", { class: "id" }, t.id), typeTag(t), bmadTag(t), controlledTag(t),
    snapshot?.targets.length > 1 ? h("span", { class: "muted small" }, t.target) : "",
    h("span", { class: "grow" }), extra || "");
}

function needCard(t) {
  const n = t.needs_you;
  const head = h("div", { class: "need-head" },
    h("div", { class: "need-icon" }, icon(NEED_ICON[n.kind] || "bell")),
    h("div", { class: "grow" },
      h("div", { class: "need-kind" }, !n.seen_at ? h("span", { class: "dot unread-dot", title: "okunmadı" }) : "", n.kind === "stall" ? n.title : NEED_TITLE[n.kind] || n.title,
        n.kind === "quiet" && n.minutes ? ` · ${n.minutes} dk` : ""),
      h("div", { class: "need-title" }, h("span", { class: "id" }, t.id), " ", cardTitle(t)),
      h("div", { class: "muted small" }, t.target, t.stage ? ` · ${t.stage}` : "", n.created_at ? ` · ${ago(n.created_at)}` : "")));
  const body = [head];
  const controls = [];
  if (n.kind === "question" && n.round) {
    const form = answerForm(t, n.round);
    const first = form.querySelector("textarea");
    if (first) primary(first);
    body.push(form);
  } else if (n.kind === "approval") {
    body.push(approvalBox(t, n.gate || t.gate || {}));
  } else if (n.kind === "protected" && n.patch) {
    body.push(protectedBox(n.patch));
    controls.push(...protectedButtons(t, n.patch));
  } else {
    if (n.details) body.push(h("pre", { class: "need-details" }, n.details));
    if (["failed", "lost"].includes(n.kind) && !t.legacy) {
      if (n.kind === "lost") body.push(h("div", { class: "muted small" }, "Süreç sonuç yazmadan bitmiş (öldürülmüş ya da çökmüş)."));
      if (t.resumable) controls.push(primary(h("button", { class: "primary", onclick: () => act(t, "resume") }, icon("play"), "Kaldığı yerden devam")));
      // The agent's work passed as READY and only an engine check failed (e.g. its environment, fixed since): the
      // checks alone run again, no session and no cost; a pass continues the pipeline.
      const recheck = t.recheckable && !t.resumable ? h("button", { class: "primary", title: "Ajan koşmaz; motor check'leri dalın şu anki hali üzerinde yeniden koşar, geçerse devam eder",
        onclick: () => act(t, "recheck") }, icon("check"), "Yalnız check'leri tekrar koştur") : null;
      if (recheck) controls.push(primary(recheck));
      const again = h("button", { class: t.resumable || recheck ? "" : "primary", onclick: () => act(t, "run") }, icon("retry"), "Aşamayı tekrar koştur");
      controls.push(t.resumable || recheck ? again : primary(again));
      if (n.kind === "failed") controls.push(h("button", { onclick: () => openDrawer(t, "questions") }, icon("message"), "Yön değiştir"));
    } else if (["quiet", "stall"].includes(n.kind)) {
      body.push(h("div", { class: "muted small" }, n.kind === "stall" ? "Ziggy komutu durdurmaz. Makine yavaşsa beklemek yeter; takıldıysa ajana yön ver ya da durdur."
        : "Uzun bir test koşusu da sessiz görünür; son satırlara bak. Takıldıysa ajana yön ver ya da durdur."));
      controls.push(primary(h("button", { class: "primary", onclick: () => nudgeDialog(t) }, icon("message"), "Yönlendir")));
      controls.push(h("button", { class: "danger", onclick: () => act(t, "stop") }, icon("stop"), "Durdur"));
    } else if (n.kind === "pr-feedback") {
      // The title counts them ("PR'da 2 yeni yorum", "PR'da CI kırmızı"); the review page lists them to pick.
      controls.push(primary(h("a", { class: "button primary", href: ReviewPage.hash(t.target, t.id, null, null, "github") }, icon("diff"), "Geri bildirime bak")));
      if (t.pr) controls.push(h("a", { class: "button", href: t.pr, target: "_blank", rel: "noopener noreferrer" }, icon("pr"), "PR'ı aç", icon("external")));
    } else if (n.kind === "blocked") {
      for (const d of n.waiting_for || []) {
        const dep = rowOf(t.target, d.id);
        if (dep) controls.push(h("button", { class: controls.length ? "" : "primary", onclick: () => openDrawer(dep) }, icon("ticket"), `${d.id} aç`));
      }
      if (controls.length) primary(controls[0]);
    } else if (n.kind === "start") {
      // A controlled project's dependant: its dependencies are done, and it starts only when you say so.
      controls.push(primary(h("button", { class: "primary", onclick: () => act(t, "run") }, icon("play"), "Başlat")));
    } else if (n.kind === "restack") {
      const r = t.restack || n.restack || {};
      if (r.result?.ok && r.result.commits?.length) {
        body.push(h("div", { class: "muted small" }, `Yeniden düzenlendi (${(r.result.from || "").slice(0, 7)} → ${(r.result.to || "").slice(0, 7)}); PR hâlâ eski commit'leri gösteriyor:`),
          h("pre", { class: "need-details" }, r.result.commits.join("\n")));
      }
      controls.push(primary(r.rebased
        ? h("button", { class: "primary", onclick: () => act(t, "restack_push") }, icon("upload"), "Push et (force-with-lease)")
        : h("button", { class: "primary", onclick: () => act(t, "restack") }, icon("retry"), "Yeniden düzenle")));
      if (r.rebased) controls.push(h("a", { class: "button", href: ReviewPage.hash(t.target, t.id) }, icon("diff"), "Değişiklikler"));
    }
  }
  const details = h("button", { class: "ghost", onclick: () => openDrawer(t) }, icon("activity"), "Ayrıntı");
  controls.push(controls.length || ["question", "approval"].includes(n.kind) ? details : primary(details));
  if (n.dismissable) controls.push(dismissButton(t, n, ""));
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
    h("div", { class: "muted small" }, `Ajanlar bu dosyalara yazamaz; ${p.stage || "aşama"} değişikliği yama olarak bıraktı (#${p.n}). `
      + "Uygularsan dala commit olarak eklenir." + (p.replaces?.length ? ` Bu yama ${replacesText(p)}.` : "")),
    h("pre", { class: "patch-diff", "aria-label": `Yama #${p.n}` }, lines));
}

// "önceki #1'in yerini alır": the applied patches of the same files this one was prepared without (protected.replaces).
// The genitive suffix follows how the last number is read (1 bir'in, 2 iki'nin, 3 üç'ün, 10 on'un, 40 kırk'ın, …).
function replacesText(p) {
  const ns = p.replaces || [];
  const last = ns[ns.length - 1];
  const ones = ["", "'in", "'nin", "'ün", "'ün", "'in", "'nın", "'nin", "'in", "'un"];
  const tens = ["", "'un", "'nin", "'un", "'ın", "'nin", "'ın", "'in", "'in", "'ın"];
  const suffix = last % 10 ? ones[last % 10] : Math.floor(last / 10) % 10 ? tens[Math.floor(last / 10) % 10]
    : Math.floor(last / 100) % 10 ? "'ün" : "'in";
  const list = ns.length > 1 ? `${ns.slice(0, -1).map((n) => `#${n}`).join(", ")} ve #${last}` : `#${last}`;
  return `önceki ${list}${suffix} yerini alır`;
}

function protectedButtons(t, p) {
  const reject = async () => {
    const r = await askBox({ title: `${t.id}: korumalı dosya değişikliği reddedilsin mi?`, text: `${p.files.join(", ")} değişmeden kalır. Neden (isteğe bağlı):`,
      placeholder: "ör. AGENTS.md'yi ben güncelleyeceğim", max: 1000, yes: "Reddet", optional: true });
    if (r) act(t, "protected_reject", r.text, { n: p.n });
  };
  return [primary(h("button", { class: "primary", onclick: () => act(t, "protected_apply", "", { n: p.n }) }, icon("check"), "Uygula")),
    h("button", { class: "danger", onclick: reject }, icon("x"), "Reddet")];
}

// The last few activity lines per running ticket, kept across polls (the snapshot carries only the latest).
const laneLog = new Map();

function laneLines(t) {
  const key = `${t.target}/${t.id}`;
  const lines = laneLog.get(key) || [];
  const line = t.activity ? shortLine(t.activity, t.worktree) : "";
  if (line && lines[lines.length - 1] !== line) {
    lines.push(line);
    if (lines.length > 4) lines.shift();
  }
  laneLog.set(key, lines);
  return lines;
}

// A running ticket as a live lane: its stages, the clock, and the agent's last moves as they happen.
function liveCard(t, s) {
  const quiet = ["quiet", "stall"].includes(t.needs_you?.kind);
  const started = t.run_started_at || t.phase_started_at || t.queued_at;
  const watch = primary(h("button", { class: "small", onclick: () => openDrawer(t, t.round ? "questions" : "activity") }, icon(t.round ? "message" : "activity"), t.round ? "Cevapla" : "İzle"));
  const lines = laneLines(t);
  return h("div", { class: "lane" + (quiet ? " quiet" : t.queued_at ? " queued" : "") },
    h("div", { class: "lane-top" },
      cardHead(t, [t.queued_at ? h("span", { class: "pill mute" }, "kuyrukta") : "", h("span", { class: `dot ${t.queued_at ? "" : quiet ? "warn" : "live"}` }),
        started ? since(started, "lane-clock nowrap") : ""]),
      cardTitle(t),
      pipelineBar(t, false),
      h("div", { class: "run-phase" }, runLabel(t))),
    h("div", { class: "lane-log" },
      lines.length ? lines.map((l, i) => h("div", { class: "lane-line" + (i === lines.length - 1 ? " now" : ""), title: l }, l))
        : h("div", { class: "lane-line muted" }, t.queued_at ? "Projenin bütün koşu slotları dolu; önündekiler bitince kendiliğinden başlar." : "hazırlanıyor…")),
    t.last_text ? h("div", { class: "live-text", title: t.last_text }, t.last_text) : "",
    quiet ? h("div", { class: "warn-text small lane-warn" }, t.needs_you.kind === "stall" ? t.needs_you.title : `${t.needs_you.minutes ?? "?"} dakikadır olay yok`) : "",
    h("div", { class: "lane-foot" },
      h("span", { class: "lane-cost" }, t.cost_usd ? money(t.cost_usd) : "henüz maliyet yok"),
      h("span", { class: "grow" }),
      watch,
      ...actionButtons(t, true)));
}

function reviewCard(t) {
  const n = t.needs_you || {};
  const controls = [];
  if (n.kind === "push" && !t.pull?.fork) {
    controls.push(primary(h("button", { class: "small primary", onclick: () => act(t, "publish") }, icon("upload"), t.pull ? "PR'a gönder" : "PR'ı güncelle")));
    controls.push(h("a", { class: "button small", href: ReviewPage.hash(t.target, t.id) }, icon("diff"), "Değişiklikler"));
  } else {
    controls.push(primary(h("a", { class: "button small primary", href: ReviewPage.hash(t.target, t.id) }, icon("diff"), "Değişiklikleri incele")));
    if (t.status === "done" && !t.pr) controls.push(h("button", { class: "small", onclick: () => act(t, "publish") }, icon("pr"), "Draft PR aç"));
  }
  controls.push(h("button", { class: "small ghost", onclick: () => openDrawer(t) }, "Ayrıntı"));
  if (n.dismissable) controls.push(dismissButton(t, n, "small"));
  return h("div", { class: `live-card review-ready ${n.kind || ""}`, title: n.details || "" },
    cardHead(t, h("span", { class: "muted small nowrap" }, ago(n.created_at || t.updated_at))),
    cardTitle(t),
    h("div", { class: "muted small row tight" }, NEED_TITLE[n.kind] || "Bitti", t.drafts ? h("span", { class: "pill info" }, `${t.drafts} taslak yorum`) : "",
      t.dismissed ? h("a", { class: "pill-btn warn", href: ReviewPage.hash(t.target, t.id, null, null, "dismissed"),
        title: "AI review'un \"boşver\" dediği bulgular; aç, istediğini düzelttir" }, `Review ${t.dismissed} bulguyu eledi`) : "",
      reviewPill(t)),
    h("div", { class: "row tight wrap" }, controls));
}

function landingCard(t) {
  const open = t.pr ? primary(h("a", { class: "button small primary", href: t.pr, target: "_blank", rel: "noopener noreferrer" }, icon("pr"), "PR'ı aç", icon("external")))
    : primary(h("button", { class: "small", onclick: () => openDrawer(t) }, "Ayrıntı"));
  return h("div", { class: "live-card landing" },
    cardHead(t, h("span", { class: "muted small nowrap" }, ago(t.updated_at))),
    cardTitle(t),
    h("div", { class: "row tight wrap" }, t.pr ? h("span", { class: `pr-pill ${(t.pr_state || "open").toLowerCase()}` }, icon("pr"), "#" + t.pr.split("/").pop()) : "", reviewPill(t)),
    h("div", { class: "row tight wrap" }, open, h("button", { class: "small ghost", onclick: () => openDrawer(t) }, "Ayrıntı")));
}

// A ticket that waits for other tickets: what it waits for, each with its status; it starts on its own (a controlled
// project asks first).
function waitingCard(t) {
  return h("div", { class: "live-card queued waiting" },
    cardHead(t, [h("span", { class: "pill warn" }, "bekliyor"), h("span", { class: "muted small nowrap" }, ago(t.updated_at))]),
    cardTitle(t),
    h("div", { class: "run-phase" }, waitText(t)),
    h("div", { class: "row tight wrap" }, (t.waiting_for || []).map((d) => {
      const dep = rowOf(t.target, d.id);
      return h("button", { class: "pill-btn " + (d.state === "blocked" ? "bad" : ""), title: d.title || "", onclick: () => dep && openDrawer(dep) },
        d.id, " · ", STATUS_TR[d.status] || d.status || "yok");
    })),
    h("div", { class: "muted small" }, t.mode === "controlled"
      ? `${t.slice ? `Dilim ${t.slice}: bir alttaki dilim` : "Beklediği ticket'lar"} bitince başlatmak için sorulur (kontrollü proje).`
      : t.slice ? `Dilim ${t.slice}: bir alttaki dilim bitince kendiliğinden başlar.` : "Beklediği ticket'lar bitince kendiliğinden başlar."),
    h("div", { class: "row tight wrap" }, primary(h("button", { class: "small ghost", onclick: () => openDrawer(t) }, "Ayrıntı"))));
}

// A ticket held for its Claude login's usage limit: when it goes on by itself; "Şimdi başlat" starts it anyway.
function quotaCard(t) {
  return h("div", { class: "live-card queued waiting" },
    cardHead(t, [h("span", { class: "pill warn" }, "kota"), h("span", { class: "muted small nowrap" }, ago(t.updated_at))]),
    cardTitle(t),
    pipelineBar(t, false),
    h("div", { class: "run-phase" }, quotaText(t)),
    h("div", { class: "muted small" }, quotaDetail(t)),
    h("div", { class: "row tight wrap" }, primary(h("button", { class: "small ghost", onclick: () => openDrawer(t) }, "Ayrıntı")),
      h("button", { class: "small", onclick: () => act(t, "run", "", { now: true }) }, icon("play"), "Şimdi başlat")));
}

// A ticket held while the machine is short of memory: it starts once the machine is calm; "Şimdi başlat" starts it anyway.
function hostCard(t) {
  return h("div", { class: "live-card queued waiting" },
    cardHead(t, [h("span", { class: "pill warn" }, "bellek"), h("span", { class: "muted small nowrap" }, ago(t.updated_at))]),
    cardTitle(t),
    pipelineBar(t, false),
    h("div", { class: "run-phase" }, HOST_TEXT),
    h("div", { class: "muted small" }, hostDetail(t)),
    h("div", { class: "row tight wrap" }, primary(h("button", { class: "small ghost", onclick: () => openDrawer(t) }, "Ayrıntı")),
      h("button", { class: "small", onclick: () => act(t, "run", "", { now: true }) }, icon("play"), "Şimdi başlat")));
}

// A split ticket: a container for its stacked slices, each with its status and PR.
function splitCard(t) {
  const kids = t.children || [];
  const done = kids.filter((c) => c.status === "done").length;
  return h("div", { class: "live-card split" },
    cardHead(t, [h("span", { class: "pill info" }, `${kids.length} dilim`), h("span", { class: "muted small nowrap" }, ago(t.updated_at))]),
    cardTitle(t),
    h("div", { class: "muted small" }, `Dilimlere bölündü · ${done}/${kids.length} bitti · her dilim bir öncekinin dalına yığılı`),
    sliceList(t, kids),
    h("div", { class: "row tight wrap" }, primary(h("button", { class: "small ghost", onclick: () => openDrawer(t) }, "Ayrıntı"))));
}

// The slices of a stack, lowest first: slice id, ticket, status (waiting: what for), PR link.
function sliceList(t, kids) {
  return h("ol", { class: "slice-list" }, kids.map((c) => {
    const row = rowOf(t.target, c.id);
    const status = c.running ? "running" : c.status;
    return h("li", { class: "slice" + (c.id === t.id ? " current" : "") },
      h("span", { class: "slice-id" }, c.slice || "?"),
      h("button", { class: "link-btn slice-title", title: c.title || "", onclick: () => row && openDrawer(row) }, h("span", { class: "id" }, c.id), " ", (c.title || "").replace(/^.* — S\d+: /, "")),
      badge(status),
      c.status === "waiting" && c.waiting_for?.length ? h("span", { class: "muted small" }, `${c.waiting_for.join(", ")} bitince`) : "",
      c.restack ? h("span", { class: "pill warn" }, "yeniden düzenle") : "",
      c.pr ? link(c.pr, h("span", { class: `pr-pill ${(c.pr_state || "open").toLowerCase()}` }, icon("pr"), "#" + c.pr.split("/").pop())) : h("span", { class: "muted small" }, "PR yok"));
  }));
}

// Closes this occurrence only (a new failure or a new run shows again); questions, approvals and
// quiet sessions need an action, so they have no close.
function dismissButton(t, n, cls) {
  return h("button", { class: `${cls} ghost`, title: "Bu kaydı kapat; aynı olay bir daha gösterilmez",
    onclick: (e) => { e.stopPropagation(); act(t, "dismiss", n.key); } }, icon("x"), "Kapat");
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
  $("inbox-btn").title = s.unread ? `Gelen kutusu · ${s.unread} okunmamış` : "Gelen kutusu";
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
  } }, icon("check"), "Tümünü okundu say");
  pop.replaceChildren(
    h("div", { class: "row" }, h("strong", { class: "grow" }, "Gelen kutusu"), markAll),
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
        h("span", { class: "grow" }, h("span", { class: "small muted" }, NEED_TITLE[i.kind] || (i.kind === "pr-feedback" ? "PR geri bildirimi" : i.kind)), h("br"),
          ["memory", "quota", "lead"].includes(i.kind) ? "" : h("span", { class: "id" }, i.ticket), ["memory", "lead"].includes(i.kind) ? h("span", { class: "id" }, i.target) : "", " ", i.title),
        h("span", { class: "muted small nowrap" }, ago(i.created_at))))))
      : h("p", { class: "muted small" }, "Yeni bir şey yok. Açtığın işler bitene kadar panoda kalır."));
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
  $("inbox-pop").replaceChildren(h("p", { class: "muted small" }, "yükleniyor…"));
  loadInbox();
});
document.addEventListener("click", (e) => { if (!$("inbox-pop").hidden && !e.target.closest(".inbox")) closeInbox(); });

function markSeen(t) {
  if (!t?.needs_you || t.needs_you.seen_at) return;
  api("/api/attention/seen", { target: t.target, ticket: t.id }).then(() => refresh()).catch(() => { /* the next poll shows it unread again */ });
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
    $("ask-yes").textContent = yes || "Gönder";
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
async function nudgeDialog(t) {
  const live = t.running && !t.queued_at && !t.retry_at;
  const stage = t.stage || "sıradaki aşama";
  const r = await askBox({
    title: "Ajana mesaj gönder: çalışmayı bırakmadan yön verir",
    text: live ? `${t.id}: ${stage} durdurulur ve kendi oturumunda, bağlamını koruyarak önce mesajını okuyup devam eder. Süren bir araç çağrısı iptal olur.`
      : `${t.id}: mesajın ${stage} başlayınca prompt'una eklenir.`,
    placeholder: "Ne farklı yapsın? (en çok 4000 karakter)", max: 4000, yes: live ? "Durdur ve yönlendir" : "Mesajı kaydet",
  });
  if (r) act(t, "nudge", r.text);
}

async function cleanupDialog(t) {
  const r = await askBox({
    title: `${t.id}: worktree temizlensin mi?`,
    text: `Önce teardown komutları çalışır, sonra worktree silinir. Dal (${t.branch}) kalır; yeni bir koşu worktree'yi daldan yeniden kurar.`,
    input: false, check: "Commit edilmemiş dosyalar olsa da sil (kaybolurlar)", yes: "Temizle",
  });
  if (r) act(t, "cleanup", r.checked ? "force" : "");
}

// An intent gap: the stage found the change answers a different intent than the ticket and the plan. The
// attempt was saved as a patch and the branch reset to the commit before it, unless it was already published.
const GAP_KEPT = {
  published: "Dal zaten yayında (PR ya da push): kod olduğu gibi duruyor, geri alınmadı; yalnızca soru açıldı.",
  "not in history": "Denemeden önceki commit artık dalın geçmişinde değil; dal geri alınmadı.",
  "nothing built": "Denemeden bu yana commit yok; geri alınacak bir şey yoktu.",
  stacked: "Üstteki dilim bu dalın üstüne kuruldu: kod olduğu gibi duruyor, geri alınmadı; yalnızca soru açıldı.",
};
function intentGapBox(t, gap) {
  const patch = `/api/attempts/${encodeURIComponent(t.target)}/${encodeURIComponent(t.id)}/attempt-${gap.attempt}.patch`;
  const note = gap.reset ? `Dal ${(gap.reset_to || "").slice(0, 7)} commit'ine geri alındı; deneme (${gap.commits} commit) yamada saklı.`
    : GAP_KEPT[gap.kept] || "Dal geri alınmadı.";
  return h("div", { class: "intent-gap" },
    h("div", { class: "need-kind" }, icon("alert"), " Niyet sapması", h("span", { class: "muted" }, ` · ${gap.stage || "?"} buldu`)),
    h("div", { class: "md" }, MD.blocks(gap.summary || "")),
    gap.evidence ? h("div", { class: "gap-evidence small" }, h("span", { class: "muted" }, "Kanıt: "), gap.evidence) : "",
    h("div", { class: "row small" },
      h("a", { class: "button small", href: patch, target: "_blank", rel: "noopener" }, icon("diff"), `Deneme ${gap.attempt} yaması`),
      h("span", { class: `pill ${gap.reset ? "info" : "warn"}` }, gap.reset ? "dal geri alındı" : "kod korundu"),
      h("span", { class: "muted grow" }, note)));
}

// The open question round: each question with its options (a click fills the answer), the
// recommendation, and an answer box per question. Drafts survive a reload (sessionStorage).
function answerForm(t, round) {
  const key = (qid) => `ziggy.answer.${t.target}/${t.id}/${round.n}/${qid}`;
  const boxes = [];
  const items = (round.questions || []).map((q, i) => {
    const qid = q.id || `Q${i + 1}`;
    const ta = h("textarea", { rows: 2, class: "answer-box", placeholder: q.recommendation ? `Önerilen: ${q.recommendation}` : "Cevabın", "aria-label": `${qid} için cevap`,
      oninput: () => store.set(sessionStorage, key(qid), ta.value) });
    ta.value = store.get(sessionStorage, key(qid));
    boxes.push([qid, ta]);
    const fill = (text) => { ta.value = text; store.set(sessionStorage, key(qid), text); ta.focus(); };
    const options = [...new Set(q.options || [])];
    return h("li", { class: "q" },
      h("div", { class: "q-text" }, h("span", { class: "q-id" }, qid), h("div", { class: "md grow" }, MD.blocks(q.text || ""))),
      options.length ? h("div", { class: "decision-options" }, options.map((o) => h("button", { type: "button", class: "small option", onclick: () => fill(o) },
        o, o === q.recommendation ? h("span", { class: "pill ok" }, "önerilen") : ""))) : "",
      q.recommendation && !options.includes(q.recommendation)
        ? h("div", { class: "q-rec small" }, h("span", { class: "muted" }, "Önerilen: "), q.recommendation, " ", h("button", { type: "button", class: "small ghost", onclick: () => fill(q.recommendation) }, "kullan")) : "",
      ta);
  });
  const note = h("textarea", { rows: 2, class: "answer-box", placeholder: "Genel not (isteğe bağlı): sorulmayan ama bilinmesi gereken", "aria-label": "Genel not",
    oninput: () => store.set(sessionStorage, key("_note"), note.value) });
  note.value = store.get(sessionStorage, key("_note"));
  const send = h("button", { class: "primary", onclick: async (e) => {
    const btn = e.currentTarget;
    const parts = boxes.filter(([, ta]) => ta.value.trim()).map(([qid, ta]) => `${qid}: ${ta.value.trim()}`);
    const open = boxes.length - parts.length;
    if (note.value.trim()) parts.push(note.value.trim());
    if (!parts.length) return toast("Önce en az bir soruyu cevapla", true);
    if (open && !(await confirmBox(`${open} soru cevapsız kalacak; ajan onlar için kendi varsayımını yapar.\nGönderilsin mi?`))) return;
    btn.disabled = true;
    if (await act(t, "answer", parts.join("\n\n"))) {
      for (const [qid] of boxes) store.set(sessionStorage, key(qid), "");
      store.set(sessionStorage, key("_note"), "");
    }
    btn.disabled = false;
  } }, icon("message"), "Cevapla ve devam et");
  return h("div", { class: "answer-form" },
    round.intent_gap ? intentGapBox(t, round.intent_gap) : "",
    round.context && round.context !== round.intent_gap?.summary ? h("div", { class: "answer-context md" }, MD.blocks(round.context)) : "",
    h("ol", { class: "questions" }, items),
    note,
    h("div", { class: "row" }, send, h("span", { class: "muted small grow" }, `tur ${round.n}${round.stage ? ` · ${round.stage}` : ""} · ${ago(round.asked_at)}`)));
}

// The gate: risk, size and why it stopped; approve, or send notes that go into the plan.
function approvalBox(t, gate) {
  const notes = h("textarea", { rows: 3, class: "answer-box", placeholder: "Neyin değişmesini istiyorsun?", "aria-label": "Değişiklik notları" });
  const key = `ziggy.approval.${t.target}/${t.id}`;
  notes.value = store.get(sessionStorage, key);
  notes.addEventListener("input", () => store.set(sessionStorage, key, notes.value));
  const changeBox = h("div", { class: "stack", hidden: !notes.value },
    h("div", { class: "muted small" }, "Not olarak plana eklenir, plan yeniden gözden geçirilir."),
    notes,
    h("div", { class: "row end" }, h("button", { class: "primary", onclick: async (e) => {
      if (!notes.value.trim()) return toast("Önce notunu yaz", true);
      const btn = e.currentTarget;
      btn.disabled = true;
      if (await act(t, "answer", notes.value.trim())) store.set(sessionStorage, key, "");
      btn.disabled = false;
    } }, icon("message"), "Notları gönder")));
  return h("div", { class: "approval" },
    h("div", { class: "row tight wrap" },
      gate.stage ? h("span", { class: "pill info" }, `${gate.stage} bitti`) : "",
      gate.risk ? h("span", { class: `pill ${RISK_PILL[gate.risk] || "mute"}` }, `risk ${gate.risk}`) : "",
      gate.size ? h("span", { class: "pill mute" }, `boyut ${gate.size}`) : "",
      t.tasks?.total ? h("span", { class: "pill mute" }, `${t.tasks.total} görev`) : ""),
    gate.reason && !gateReasonIsRating(gate.reason) ? h("div", { class: "md" }, MD.blocks(gate.reason)) : "",
    t.split_offer ? h("div", { class: "split-offer" },
      h("div", { class: "muted small" }, `Plan ${t.split_offer.length} dilimli ve büyük/riskli: tek PR yerine her dilim ayrı bir ticket ve yığılı draft PR olabilir.`),
      h("ol", { class: "slice-list" }, t.split_offer.map((s) => h("li", { class: "slice" }, h("span", { class: "slice-id" }, s.id), h("span", { class: "slice-title" }, s.title))))) : "",
    h("div", { class: "row" },
      primary(h("button", { class: "primary", onclick: () => act(t, "approve") }, icon("check"), t.split_offer ? "Onayla → tek PR" : "Onayla → devam")),
      t.split_offer ? h("button", { title: "Her dilim bir öncekinin dalına yığılı ayrı bir ticket ve draft PR olur; ilk dilim hemen başlar",
        onclick: () => act(t, "split") }, icon("layers"), `Dilimlere böl (${t.split_offer.length} PR)`) : "",
      h("button", { onclick: () => { changeBox.hidden = !changeBox.hidden; if (!changeBox.hidden) notes.focus(); } }, icon("message"), "Değişiklik iste"),
      h("button", { class: "ghost", onclick: () => openDrawer(t, "plan") }, icon("spec"), "Planı oku")),
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
  deferred: (o) => `Kaynak: ${o.ticket} deferred ${o.entry}`,
  roadmap: (o) => `Kaynak: yol haritası ${o.phase ? `${o.phase} · ` : ""}${o.item}`,
  lead: (o) => `Kaynak: iş önerisi ${o.proposal}`,
};

function setComposerOrigin(origin) {
  composerOrigin = origin;
  const box = $("composer-origin");
  box.hidden = !origin;
  box.replaceChildren(...(origin ? [
    h("span", { class: "chip" }, icon("pin"), (ORIGIN_CHIP[origin.kind] || ORIGIN_CHIP.deferred)(origin)),
    h("button", { class: "small ghost", type: "button", title: "Bağlantıyı kaldır (metin kalır)", onclick: () => setComposerOrigin(null) }, "×"),
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
  $("new-body").value = [item.title, `Evre: ${phase.id} · ${phase.title}`, item.detail || "", `Kaynak: yol haritası ${item.id}`]
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
  const t = composerTarget();
  const sel = $("new-pipeline");
  const show = !!t?.bmad?.installed && $("new-type").value !== "project";
  sel.hidden = !show;
  const bmadOption = sel.querySelector('option[value="bmad"]');
  bmadOption.disabled = !bmadReady(t?.bmad);
  bmadOption.title = bmadOption.disabled ? bmadPill(t)?.title || "" : "";
  const choice = (t ? store.get(localStorage, pipelineKey(t.name)) : "") || t?.default_pipeline || "standard";
  sel.value = choice === "bmad" && !bmadOption.disabled ? "bmad" : "standard";
}

const chosenPipeline = () => ($("new-pipeline").hidden ? "standard" : $("new-pipeline").value);

function composerHint() {
  const type = $("new-type").value;
  const t = composerTarget();
  const target = $("new-target").value || "?";
  composerPipeline();
  composerAfter();
  const line = chosenPipeline() === "bmad" ? "bmad" : pipelineFor(type);
  const after = $("new-after").value;
  $("composer-hint").textContent = `${target} için ${type} ticket'ı · ${line} hattı${t?.bmad ? ` · ${bmadText(t.bmad)}` : ""}. `
    + (after ? `${after} bitene kadar bekler, sonra ${t?.mode === "controlled" ? "başlatmak için sorulur" : "kendiliğinden başlar"}.` : "Başlatınca ilk aşama hemen koşar (ajanlar para harcar).");
}

// "Sonra başla": the chosen project's open tickets (not done, not split, not archived); the choice survives refreshes.
function composerAfter() {
  const sel = $("new-after");
  const target = $("new-target").value || snapshot?.targets.find((x) => !x.error)?.name;
  const open = (snapshot?.tickets || []).filter((x) => x.target === target && !x.archived && !["done", "split", "error"].includes(x.status));
  const options = [["", "Hemen başlayabilir"], ...open.map((x) => [x.id, `${x.id} bitince · ${x.title.length > 40 ? x.title.slice(0, 39) + "…" : x.title}`])];
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
  const t = composerTarget();
  if (t) store.set(localStorage, pipelineKey(t.name), $("new-pipeline").value === "bmad" ? "bmad" : "standard");
  composerHint();
});

$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.submitter;
  const start = btn?.value === "start";
  if (!$("new-title").value.trim()) return toast("Başlık boş", true);
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
    toast(`${out.ticket.id} oluşturuldu (${out.ticket.branch})${out.job ? (out.job.queued ? ", kuyruğa alındı (koşu slotları dolu)" : $("new-after").value ? `, ${$("new-after").value} bitince başlayacak` : ", başlatıldı") : ""}`);
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

$("new-lang").addEventListener("change", () => store.set(localStorage, "ziggy.lang", $("new-lang").value || "-"));
{
  const saved = store.get(localStorage, "ziggy.lang");
  if (saved) { $("new-lang").value = saved === "-" ? "" : saved; $("pj-lang").value = saved === "-" ? "" : saved; }
}

function renderTargets(s) {
  const live = s.targets.filter((t) => !t.error);
  const names = live.map((t) => t.name);
  for (const [sel, withAll] of [[$("new-target"), false], [$("filter"), true]]) {
    const current = sel.value;
    const options = (withAll ? [["", "Tüm projeler"]] : []).concat(live.map((t) => [t.name, withAll || !t.bmad ? t.name : `${t.name} · ${bmadText(t.bmad)}`]));
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
    h("a", { class: "spend-item", href: "#stats" }, h("span", { class: "spend-v" }, money(s.cost_today_usd)), " bugün"),
    st ? h("a", { class: "spend-item", href: "#stats" }, h("span", { class: "spend-v" }, money(st.totals.week)), " 7 gün") : "");
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
  return snapshot?.targets.some((t) => t.name === name) ? name : "";
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
  const live = s.targets.filter((t) => !t.error);
  const sig = JSON.stringify([current, live.map((t) => t.name), waiting]);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.hidden = live.length < 2;
  const chip = (name, label, n) => h("button", { class: "vtab" + (name === current ? " on" : ""), "aria-pressed": String(name === current),
    onclick: () => setHomeTarget(name) }, label, n ? h("span", { class: "vcount volt", title: "seni bekleyen" }, String(n)) : "");
  box.replaceChildren(chip("", "Tümü", waiting.length), ...live.map((t) => chip(t.name, t.name, waiting.filter((n) => n === t.name).length)));
}

// --- lead: Sıradaki işler ---------------------------------------------------------------------------
// Every morning the lead (lead.py) turns each project's signals (roadmap, red CI, issues, deferred findings) into
// ticket proposals. Sıradaki işler sits below what waits on you, folded to one line until opened, with
// "Başlat" in a personal project and only "Ticket olarak aç" (the composer) in a controlled one.

const SIGNAL_TR = { ci: "CI", roadmap: "yol haritası", issue: "issue", deferred: "ertelenen bulgu" };
const SIZE_TR = { trivial: "çok küçük", small: "küçük", medium: "orta", large: "büyük" };
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
  if (d.targets.some((t) => t.running)) return "Lead şimdi çalışıyor…";
  const last = d.targets.map((t) => t.last_run).filter((r) => r?.started_at).sort((a, b) => b.started_at.localeCompare(a.started_at))[0];
  const plan = d.enabled ? `her sabah ${d.at}` : "otomatik çalışma kapalı";
  return last ? `son: ${when(last.started_at)} · ${plan}` : `henüz çalışmadı · ${plan}`;
}

function renderNext() {
  const d = loadLead.data;
  if (!d) return;
  const targets = d.targets.filter((t) => inHome(t.target));
  const sig = JSON.stringify([nextOpen, targets.map((t) => [t.target, t.mode, t.running, t.last_run, t.pending.map((p) => [p.id, p.title, p.why, p.signals])])]);
  if (NEXT_BOX.dataset.sig === sig) return;
  NEXT_BOX.dataset.sig = sig;
  NEXT_BOX.hidden = !targets.length;
  NEXT_BOX.classList.toggle("open", nextOpen);
  const total = targets.reduce((n, t) => n + t.pending.length, 0);
  NEXT_BOX.replaceChildren(
    h("button", { type: "button", class: "next-toggle", "aria-expanded": String(nextOpen),
      onclick: () => { nextOpen = !nextOpen; renderNext(); } },
      icon("chevron", "chev"), icon("zap"), "Sıradaki işler", h("span", { class: "group-n" }, String(total)),
      h("span", { class: "muted small" }, nextOpen ? "lead'in önerileri: yol haritası, CI, issue'lar, ertelenen bulgular" : leadRunText(d))),
    ...(nextOpen ? targets.map(nextTarget) : []));
}

function nextTarget(t) {
  const last = t.last_run;
  const refreshBtn = h("button", { class: "small ghost", disabled: t.running, title: "Sinyalleri şimdi topla ve öner (kota varsa kısa bir oturum)", onclick: async (e) => {
    e.currentTarget.disabled = true;
    try { await api(`/api/lead/${enc(t.target)}/run`, {}); toast(`${t.target}: lead başladı`); } catch (err) { toast(err.message, true); }
    loadLead(true);
  } }, icon("retry"), t.running ? "Çalışıyor…" : "Yenile");
  const status = t.running ? "şimdi çalışıyor"
    : !last?.started_at ? "henüz çalışmadı"
    : [`son ${when(last.started_at)}`, last.state === "failed" ? `hata: ${last.error || "?"}` : "", last.state === "skipped" ? "yeni sinyal yoktu" : "",
      last.agentless ? `ajansız (${last.why || "kota yok"})` : ""].filter(Boolean).join(" · ");
  return h("section", { class: "next-target" },
    h("div", { class: "next-head" }, snapshot?.targets.length > 1 || t.mode === "controlled" ? h("strong", {}, t.target) : "", controlledPill(t),
      h("span", { class: "muted small grow" }, status), refreshBtn),
    (last?.warnings || []).length ? h("div", { class: "small warn-text" }, last.warnings.join(" · ")) : "",
    t.pending.length ? h("div", { class: "live-grid" }, t.pending.map((p) => proposalCard(t, p)))
      : h("p", { class: "muted small" }, "Bekleyen öneri yok. Lead her sabah sinyallere bakar; Yenile şimdi bakar."));
}

function proposalCard(t, p) {
  const actions = t.mode === "controlled"
    ? [h("button", { class: "small primary", title: "Kontrollü proje: öneri yalnız öneri; ticket'ı composer'dan sen oluşturursun", onclick: () => proposalToComposer(t, p) }, icon("ticket"), "Ticket olarak aç")]
    : [h("button", { class: "small primary", onclick: (e) => startProposal(e.currentTarget, t, p) }, icon("play"), "Başlat"),
      h("button", { class: "small", onclick: () => proposalToComposer(t, p) }, icon("spec"), "Düzenle")];
  actions.push(h("button", { class: "small ghost", onclick: (e) => rejectProposal(e.currentTarget, t, p) }, icon("x"), "Reddet"));
  return h("article", { class: "live-card proposal-card" + (p.agentless ? " agentless" : "") },
    h("div", { class: "run-head" }, h("span", { class: "id" }, p.id), h("span", { class: "type" }, p.type),
      p.size ? h("span", { class: "pill mute" }, SIZE_TR[p.size] || p.size) : "",
      p.agentless ? h("span", { class: "pill warn", title: "Kota ya da oturum yoktu: sinyalden doğrudan yazıldı" }, "ajansız") : "",
      h("span", { class: "grow" }), h("span", { class: "muted small nowrap" }, ago(p.created_at))),
    h("div", { class: "proposal-title" }, p.title),
    p.why ? h("div", { class: "small proposal-why" }, p.why) : "",
    h("ul", { class: "proposal-sources" }, (p.signals || []).map((s) => h("li", {},
      h("span", { class: "tag" }, SIGNAL_TR[s.kind] || s.kind), " ", s.url ? link(s.url, s.title) : s.title))),
    h("div", { class: "row tight wrap" }, actions));
}

async function startProposal(btn, t, p) {
  if (!(await confirmBox(`${p.title}\n\n${t.target} projesinde bu ticket açılıp hemen başlatılsın mı? Ajanlar para harcar.`))) return;
  btn.disabled = true;
  try {
    const out = await api(`/api/lead/${enc(t.target)}/proposals/${enc(p.id)}`, { action: "start", language: $("new-lang").value });
    toast(`${out.ticket.id} oluşturuldu, ${out.job?.queued ? "kuyruğa alındı (koşu slotları dolu)" : "başlatıldı"}`);
    await refresh();
    const row = rowOf(t.target, out.ticket.id);
    if (row) openDrawer(row, "activity");
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
  }
  loadLead(true);
}

async function rejectProposal(btn, t, p) {
  const got = await askBox({ title: `${p.id} reddedilsin mi?`, text: `${p.title}\nBu önerinin kaynakları bir daha önerilmez.`,
    placeholder: "Neden (isteğe bağlı)", max: 500, yes: "Reddet", optional: true });
  if (!got) return;
  btn.disabled = true;
  try {
    await api(`/api/lead/${enc(t.target)}/proposals/${enc(p.id)}`, { action: "reject", ...(got.text ? { reason: got.text } : {}) });
    toast(`${t.target}: ${p.id} reddedildi`);
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
  }
  loadLead(true);
  refresh();
}

// "Düzenle" (personal) and "Ticket olarak aç" (controlled): the composer, prefilled; creating it accepts the proposal.
function proposalToComposer(t, p) {
  if (currentView() !== "home") location.hash = "#home";
  $("new-target").value = t.target;
  $("new-type").value = p.type;
  $("new-title").value = p.title;
  $("new-body").value = p.ticket_body;
  setComposerOrigin({ kind: "lead", target: t.target, proposal: p.id });
  composerHint();
  setTimeout(() => { $("new-title").focus(); $("composer").scrollIntoView({ block: "center" }); }, 0);
}

function renderStatus(s) {
  const running = s.tickets.filter((t) => t.running);
  $("status-line").replaceChildren(
    h("span", { class: `dot ${running.length ? "live" : ""}` }), running.length ? `${running.length} koşu` : "boşta",
    h("span", {}, "·"), `${money(s.cost_today_usd)} bugün`);
}

// --- ticket lists with view tabs ------------------------------------------------------------------

const VIEWS_T = [
  ["all", "Hepsi", () => true],
  ["needs", "Seni bekliyor", (t) => ["needs-user", "awaiting-approval"].includes(t.status) && !t.running],
  ["running", "Sürüyor", (t) => t.running],
  ["new", "Başlamadı", (t) => t.status === "new" && !t.running],
  ["failed", "Başarısız", (t) => t.status === "failed" && !t.running],
  ["done", "Bitti", (t) => t.status === "done" && !t.running],
  ["archived", "Arşiv", () => true],
];
const WEEK = 7 * 86400000;
const isOld = (t) => t.status === "done" && Date.now() - new Date(t.updated_at || 0).getTime() > WEEK;
const viewRows = (rows, view) => {
  const [, , test] = VIEWS_T.find(([k]) => k === view) || VIEWS_T[0];
  return rows.filter((t) => (view === "archived" ? t.archived : !t.archived) && test(t));
};
const PRIORITY = (t) => (t.running ? 1 : ["needs-user", "awaiting-approval"].includes(t.status) ? 0 : 2);

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
  const scoped = s.tickets.filter((t) => !filter || t.target === filter);
  viewTabs($("ticket-tabs"), scoped, view, (key) => {
    history.replaceState(null, "", key === "all" ? "#tickets" : `#tickets?view=${key}`);
    renderTickets(snapshot);
  });
  let rows = viewRows(scoped, view).filter((t) => !words.length
    || words.every((w) => [t.id, t.title, t.branch, t.target, t.status, t.type, t.stage].some((v) => (v || "").toLowerCase().includes(w))));
  const old = view === "all" ? rows.filter(isOld) : [];
  $("old-toggle").hidden = !old.length;
  $("old-label").textContent = `7 günden eski ${old.length} biten ticket'ı da göster`;
  if (!$("show-old").checked) rows = rows.filter((t) => !old.includes(t));
  rows.sort((a, b) => PRIORITY(a) - PRIORITY(b) || String(b.updated_at).localeCompare(String(a.updated_at)));
  const live = rows.filter((t) => t.running).length;
  $("ticket-count").textContent = live ? `${rows.length} · ${live} hareket halinde` : String(rows.length);
  ticketList($("ticket-list"), rows, { key: [view, filter, words, $("show-old").checked], showTarget: !filter,
    empty: !s.tickets.length ? "Henüz ticket yok. Ana sayfadan bir başlık yazıp başla." : words.length ? "Filtreye uyan ticket yok." : `"${(VIEWS_T.find(([k]) => k === view) || VIEWS_T[0])[1]}" içinde ticket yok.` });
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
    h("div", {}, "Ticket"), h("div", {}, "Başlık"), h("div", {}, "İlerleme"), h("div", {}, "PR"), h("div", { class: "num t-cost" }, "Maliyet"), h("div", { class: "t-time" }, "Güncellendi"), h("div", {}));
  container.replaceChildren(head, ...rows.map((t) => h("div", {
    class: "trow item" + (t.running ? " running" : "") + (["quiet", "stall"].includes(t.needs_you?.kind) ? " quiet" : "") + (t.status === "failed" ? " failed" : "")
      + (!t.running && ["needs-user", "awaiting-approval"].includes(t.status) ? " needs" : ""),
    role: "listitem", tabindex: "0",
    onclick: (e) => { if (!e.target.closest("a,button")) openDrawer(t); },
    onkeydown: (e) => { if (e.key === "Enter" && e.target === e.currentTarget) openDrawer(t); },
  },
    h("div", { class: "t-idcell" }, h("span", { class: "id" }, t.id), typeTag(t), bmadTag(t), controlledTag(t)),
    h("div", { class: "t-main" },
      h("div", { class: "t-title", title: t.title }, t.title),
      h("div", { class: "t-sub" }, opts.showTarget ? h("span", {}, t.target) : "", originLink(t), h("span", { class: "branch", title: t.branch }, icon("branch"), t.branch || "-"))),
    h("div", { class: "t-status" },
      pipelineBar(t, true),
      h("div", { class: "row tight" }, badge(t.status), t.stage && !["done", "new"].includes(t.status) ? h("span", { class: "muted small mono" }, t.stage) : "",
        t.tasks?.total ? h("span", { class: "muted small", title: "biten / toplam görev" }, `${t.tasks.done}/${t.tasks.total}`) : ""),
      t.running ? h("span", { class: "live-line" }, runLabel(t)) : t.status === "waiting" ? h("span", { class: "live-line" }, waitText(t))
        : t.status === "quota" ? h("span", { class: "live-line warn-text" }, quotaText(t))
        : t.status === "host" ? h("span", { class: "live-line warn-text" }, HOST_TEXT)
        : t.status === "split" ? h("span", { class: "muted small" }, `${(t.children || []).length} dilim: ${(t.children || []).map((c) => c.id).join(", ")}`) : "",
      t.archived ? h("span", { class: "pill mute" }, "arşivde") : reviewPill(t)),
    h("div", { class: "t-pr" }, t.pr ? link(t.pr, h("span", { class: `pr-pill ${(t.pr_state || "open").toLowerCase()}` }, icon("pr"), "#" + t.pr.split("/").pop(),
      t.pr_state && t.pr_state !== "OPEN" ? (t.pr_state === "MERGED" ? " merged" : " kapalı") : "")) : h("span", { class: "muted" }, "—")),
    h("div", { class: "num t-cost" }, money(t.cost_usd)),
    h("div", { class: "muted t-time nowrap" }, ago(t.updated_at)),
    h("div", { class: "t-actions" }, actionButtons(t, true)),
  )));
}

// An issue the ticket came from (YouTrack and the like), as a link to the tracker; a deferred finding,
// as a button to the ticket whose review deferred it; a roadmap item it works on, as a link to its project's roadmap.
function originLink(t) {
  const r = t.roadmap;
  if (r) {
    return h("a", { class: "origin", href: `#project/${encodeURIComponent(t.target)}`, title: r.title ? `Yol haritası maddesi: ${r.title}` : "Yol haritası maddesi",
      onclick: (e) => e.stopPropagation() }, `Yol haritası: ${r.phase ? `${r.phase} · ` : ""}${r.item}`);
  }
  const o = t.origin;
  if (!o || o.kind === "roadmap") return "";
  if (o.kind === "lead") {
    return h("button", { class: "origin link", type: "button", title: `Lead'in iş önerisi ${o.proposal}`,
      onclick: (e) => { e.stopPropagation(); location.hash = "#home"; } }, `← öneri ${o.proposal}`);
  }
  if (o.kind === "deferred") {
    return h("button", { class: "origin link", type: "button", title: `${o.ticket} ticket'ının ertelenen bulgusu ${o.entry}`,
      onclick: (e) => { e.stopPropagation(); const row = rowOf(t.target, o.ticket); if (row) openDrawer(row, "plan"); } }, `← ${o.ticket} deferred`);
  }
  const label = h("span", { class: "origin" }, o.ref, o.url ? " ↗" : "");
  return o.url ? link(o.url, label) : label;
}

function reviewPill(t) {
  const r = t.review;
  if (!r || r.state === "running") return "";
  const text = r.state === "error" ? "review hata" : r.stale ? "review · eski" : r.findings ? `review · ${r.findings} bulgu` : "review · temiz";
  return h("button", {
    class: `pill-btn ${r.state === "error" ? "bad" : r.stale ? "" : r.findings ? "warn" : "ok"}`,
    title: r.stale ? "Dal bu review'dan sonra değişti; yeni hali incelenmedi" : null,
    onclick: (e) => { e.stopPropagation(); openDrawer(t, "review"); },
  }, icon("eye"), text, r.posted ? " · PR'da" : "");
}

// Projects as cards: what is open, what it cost, its run slots (a used slot slides), and the way in.
function projectList(box, s) {
  const costs = Object.fromEntries((loadStrip.data?.by_target || []).map((g) => [g.name, g]));
  const waiting = (s.board?.waiting_on_you || []).map((e) => e.target);
  const signature = JSON.stringify([s.targets, s.tickets.map((t) => [t.target, t.archived, t.status, t.running, t.id]), costs, waiting]);
  if (signature === box.dataset.sig) return;
  box.dataset.sig = signature;
  if (!s.targets.length) {
    box.replaceChildren(empty("Henüz proje yok. 'Mevcut repoyu ekle' ya da 'Sıfırdan yeni proje' ile başla.", "box"));
    return;
  }
  const stat = (value, label) => h("div", { class: "pstat" }, h("div", { class: "pstat-v" }, value), h("div", { class: "pstat-l" }, label));
  box.replaceChildren(...s.targets.map((t) => {
    const rows = s.tickets.filter((x) => x.target === t.name && !x.archived);
    const open = rows.filter((x) => !["done", "split"].includes(x.status)).length;
    const live = rows.filter((x) => x.running && !x.queued_at);
    const mine = waiting.filter((n) => n === t.name).length;
    const g = costs[t.name];
    const href = `#project/${encodeURIComponent(t.name)}`;
    const state = t.error ? h("span", { class: "pill bad" }, "config hatası")
      : live.length ? h("span", { class: "pill info" }, h("span", { class: "dot live" }), `${live.length} canlı`)
      : mine ? h("span", { class: "pill volt" }, h("span", { class: "dot volt-dot" }), `${mine} sende`)
      : h("span", { class: "pill mute" }, "boşta");
    const slots = Array.from({ length: t.max_active || 0 }, (_, i) => h("span", { class: "slot-seg" + (i < (t.active || []).length ? " used" : "") }));
    return h("article", { class: "pcard" + (rows.length ? "" : " fresh"), role: "listitem" },
      h("div", { class: "pcard-top" },
        h("div", { class: "grow" }, t.error ? h("span", { class: "pcard-name" }, t.name) : h("a", { class: "pcard-name", href }, t.name),
          h("div", { class: "pcard-where mono" }, [t.github_repo, homeShort(t.path)].filter(Boolean).join(" · "))),
        state),
      t.error ? h("p", { class: "error-banner small" }, t.error)
        : rows.length ? h("div", { class: "pstats" }, stat(String(open), "açık ticket"), stat(g ? money(g.cost) : "—", "toplam maliyet"), stat(g ? String(g.sessions) : "0", "oturum"))
        : h("div", { class: "pcard-empty" }, h("strong", {}, "Henüz ticket yok"), h("span", { class: "muted small" }, "Proje hazır. İlk işi ver; plan çıkınca seni çağırır.")),
      t.error ? "" : h("div", { class: "pcard-slot" }, h("span", { class: "side-label" }, "Slot"), h("span", { class: "slot-segs" }, slots),
        h("span", { class: "muted small grow" }, live.length ? live.map((x) => `${x.id} · ${x.stage || "hazırlanıyor"}`).join(", ") : "boş")),
      h("div", { class: "row pcard-foot" }, bmadPill(t), controlledPill(t),
        h("span", { class: "grow" }),
        t.error ? "" : h("button", { class: "small", onclick: () => { focusComposer(); $("new-target").value = t.name; composerHint(); } }, rows.length ? "Ticket ver" : "İlk ticket'ı ver"),
        rows.length ? h("button", { class: "small ghost", onclick: () => { $("filter").value = t.name; location.hash = "#tickets"; } }, `${rows.length} ticket`) : "",
        t.error ? "" : h("a", { class: "button small ghost", href }, "Aç →"),
        h("button", { class: "small ghost", title: "Projeyi Ziggy'den çıkar (repo ve ticket geçmişi yerinde kalır)", disabled: live.length > 0,
          onclick: (e) => removeTarget(e.currentTarget, t.name) }, icon("x"), "Çıkar")));
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
  box.replaceChildren(h("h2", { class: "board-group-h" }, "Çıkarılmış projelerin artıkları",
    h("span", { class: "muted small" }, "Ticket geçmişi ve hafıza kalır; yalnız worktree'ler silinir, dalları repoda durur.")),
  ...found.map((x) => h("div", { class: "row leftover" },
    h("strong", {}, x.name), h("span", { class: "muted small grow" }, `${bytes(x.bytes)} · ${x.worktrees.length} worktree (${bytes(x.worktree_bytes)})`
      + (x.worktrees.some((t) => t.dirty) ? " · kaydedilmemiş değişiklik var" : "")),
    x.worktrees.length ? h("button", { class: "small", onclick: (e) => purgeLeftover(e.currentTarget, x) }, icon("x"), `Worktree'leri sil (${bytes(x.worktree_bytes)})`) : "")));
}

async function purgeLeftover(btn, x) {
  if (!(await confirmBox(`${x.name} projesinin ${x.worktrees.length} worktree'si silinsin mi? (${bytes(x.worktree_bytes)})\n\nDalları repoda, ticket geçmişi ve hafıza Ziggy'de kalır.`))) return;
  btn.disabled = true;
  const send = (force) => api(`/api/leftovers/${enc(x.name)}/purge`, { force });
  try {
    let out;
    try { out = await send(false); } catch (e) {
      if (!/uncommitted/.test(e.message) || !(await confirmBox(`${e.message}\n\nYine de silinsin mi?`))) throw e;
      out = await send(true);
    }
    toast(`${x.name}: ${out.removed.length} worktree silindi, ${bytes(out.freed_bytes)} açıldı`);
  } catch (e) {
    toast(e.message, true);
  }
  loadLeftovers();
}

// "Çıkar": the target's config table goes (after a backup); its repository, worktrees, tickets and memory stay,
// so adding the same repository again brings them back. The server refuses while one of its tickets runs.
async function removeTarget(btn, name) {
  if (!(await confirmBox(`${name} Ziggy'den çıkarılsın mı?\n\nConfig'teki kaydı silinir (önce yedeklenir). Repo, worktree'ler, ticket geçmişi ve hafıza yerinde kalır; aynı repoyu yeniden eklersen geri gelir.`))) return;
  btn.disabled = true;
  try {
    await api(`/api/targets/${enc(name)}/remove`, {});
    toast(`${name} Ziggy'den çıkarıldı`);
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
  $("tickets-count").textContent = String(s.tickets.filter((t) => !t.archived && !["done", "split"].includes(t.status)).length || "");
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

const TABS = [["plan", "Plan", "spec"], ["questions", "Sorular", "message"], ["changes", "Değişiklikler", "diff"], ["review", "Review", "eye"],
  ["log", "Kayıt", "activity"], ["ticket", "Ticket metni", "ticket"]];
const LOG_TABS = [["activity", "Etkinlik"], ["stages", "Aşamalar"], ["jobs", "Job'lar"]];
const isLogTab = (key) => LOG_TABS.some(([k]) => k === key);
const enc = encodeURIComponent;

function defaultTab(t) {
  if (t.round) return "questions";
  if (t.running) return "activity";
  if (t.status === "awaiting-approval") return "plan";
  if (t.status === "new") return "ticket";
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
function openDrawer(t, tab, opts) {
  closeInbox();
  pendingHighlight = opts?.highlight || null;
  const want = tab === "chat" ? null : tab;
  if (drawer && drawer.target === t.target && drawer.id === t.id && currentView() === "ticket") {
    if (tab === "chat") return renderChatSide(true);
    if (want) switchTab(want);
    if (pendingHighlight) { drawer.highlight = pendingHighlight; pendingHighlight = null; renderTabs(); }
    return;
  }
  ticketReturn = currentView() === "ticket" ? ticketReturn : location.hash || "#home";
  location.hash = ticketHash(t.target, t.id, want || defaultTab(t));
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
  const t = rowOf(params.target, params.id);
  const tab = params.tab || (t ? defaultTab(t) : "plan");
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
  $("d-back").textContent = ticketReturn.startsWith("#home") ? "Komuta" : ticketReturn.startsWith("#project") ? "Proje" : "Ticket'lar";
  $("d-back").href = ticketReturn.startsWith("#ticket/") ? "#tickets" : ticketReturn;
  $("d-title").textContent = t ? t.title : params.id;
  $("d-body").replaceChildren(h("div", { class: "skeleton" }, h("span"), h("span"), h("span")));
  $("d-tabs").replaceChildren();
  $("d-subtabs").hidden = true;
  if (t) { markSeen(t); refreshDrawerHead(); }
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
function renderNextStep(t) {
  const n = t.needs_you;
  const box = $("d-next");
  const all = actionButtons(t, false).filter(Boolean);
  const lead = n ? nextStepLead(t, n, all) : [];
  const leadLabels = new Set(lead.map((el) => el.textContent));
  $("d-actions").replaceChildren(...all.filter((el) => !lead.includes(el) && !leadLabels.has(el.textContent)));
  if (!n) {
    box.hidden = true;
    return;
  }
  if (n.dismissable) lead.push(dismissButton(t, n, ""));
  box.hidden = false;
  box.replaceChildren(
    h("div", { class: "next-text" },
      h("span", { class: "next-kicker" }, h("span", { class: "dot" }), "Sıradaki hamle senin"),
      h("div", { class: "next-title" }, n.kind === "stall" ? n.title : NEED_TITLE[n.kind] || n.title || "Seni bekliyor"),
      n.details && !(n.kind === "approval" && gateReasonIsRating(n.details)) ? h("div", { class: "next-detail" }, n.details.split("\n")[0]) : ""),
    lead.length ? h("div", { class: "row next-actions" }, lead) : "");
}

// The banner's buttons per kind, the same next step the board card offers; otherwise the head's buttons minus the
// destructive or incidental ones (Temizle, Durdur: danger and ghost).
function nextStepLead(t, n, all) {
  if (n.kind === "pr-feedback") {
    return [h("a", { class: "button primary", href: ReviewPage.hash(t.target, t.id, null, null, "github") }, icon("diff"), "Geri bildirime bak"),
      t.pr ? h("a", { class: "button", href: t.pr, target: "_blank", rel: "noopener noreferrer" }, icon("pr"), "PR'ı aç", icon("external")) : ""].filter(Boolean);
  }
  if (["quiet", "stall"].includes(n.kind)) return [h("button", { class: "primary", onclick: () => nudgeDialog(t) }, icon("message"), "Yönlendir")];
  if (n.kind === "blocked") {
    return (n.waiting_for || []).map((d) => rowOf(t.target, d.id)).filter(Boolean)
      .map((dep, i) => h("button", { class: i ? "" : "primary", onclick: () => openDrawer(dep) }, icon("ticket"), `${dep.id} aç`));
  }
  if (n.kind === "start") return [h("button", { class: "primary", onclick: () => act(t, "run") }, icon("play"), "Başlat")];
  if (n.kind === "protected" && n.patch) return protectedButtons(t, n.patch);
  return all.filter((el) => !el.classList.contains("danger") && !el.classList.contains("ghost"));
}

function refreshDrawerHead() {
  const t = rowOf(drawer.target, drawer.id);
  if (!t) return;
  const signature = JSON.stringify(t);
  if (signature === drawer.headSignature) return;
  drawer.headSignature = signature;
  if (drawer.detail && !drawerBusy()) loadDetail();  // e.g. a stage just finished
  $("d-sub").replaceChildren(h("span", { class: "id" }, t.id), typeTag(t), bmadTag(t), controlledTag(t), badge(t.status),
    t.pipeline ? h("span", { class: "muted small" }, `${t.pipeline} hattı`) : t.legacy ? h("span", { class: "pill mute" }, "v1") : "",
    t.running ? h("span", { class: "live-line" }, h("span", { class: "dot live" }), runLabel(t))
      : t.status === "waiting" ? h("span", { class: "live-line" }, h("span", { class: "dot warn" }), waitText(t))
      : t.status === "quota" ? h("span", { class: "live-line warn-text" }, h("span", { class: "dot warn" }), quotaText(t))
      : t.status === "host" ? h("span", { class: "live-line warn-text" }, h("span", { class: "dot warn" }), HOST_TEXT) : "");
  $("d-title").textContent = t.title;
  $("d-meta").replaceChildren(
    chip("box", t.target), originLink(t), chip("branch", t.branch, "mono"), chip("dollar", money(t.cost_usd)), chip("clock", ago(t.updated_at)),
    t.risk ? h("span", { class: `pill ${RISK_PILL[t.risk] || "mute"}` }, `risk ${t.risk}`) : "", t.size ? h("span", { class: "pill mute" }, t.size) : "",
    t.pr ? link(t.pr, chip("pr", "PR #" + t.pr.split("/").pop(), "linked")) : "",
    t.slice && t.parent ? h("span", { class: "pill info", title: `${t.parent.title}\nTaban: ${t.base_branch}` }, `${t.parent.id} · dilim ${t.slice}`) : "",
    t.after?.length && t.status !== "waiting" ? h("span", { class: "pill mute" }, `sonra: ${t.after.map((d) => d.id).join(", ")}`) : "");
  $("d-pipe").replaceChildren(pipelineBar(t, false));
  renderNextStep(t);
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
  if (wasLive && !d.detail.chat?.live && !onScreen($("d-chat"))) toast(`${d.id}: sohbette cevap hazır`);
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
    if (s.title === "Pipeline") return { cls: "trusted", icon: "shield", tag: "pipeline yazdı · güvenilir", body: (x) => MD.blocks(x.body.replace(/^Written by the pipeline wrapper; trusted\.\s*$/m, "")) };
    if (s.title.startsWith("Ticket")) return { cls: "untrusted", icon: "ticket", tag: "güvenilmeyen girdi", tagCls: "warn" };
    if (s.title.startsWith("Decisions")) return { cls: "decisions", icon: "user", tag: "senin kararların" };
    return {};
  });
}

// Plan: inbox/plan.md as the agents wrote it, with the task progress, the wrapper's sections
// (your decisions, review rounds) marked, and the review's deferred findings below.
function renderPlan(det) {
  const t = rowOf(drawer.target, drawer.id) || det;
  const head = h("div", { class: "plan-head" },
    h("span", { class: "mono small muted path-inline" }, det.plan_path || ""),
    det.tasks?.total ? h("span", { class: "tasks-meter", title: "biten / toplam görev" },
      h("span", { class: "meter" }, h("span", { class: "meter-fill", style: `width:${Math.round((det.tasks.done / det.tasks.total) * 100)}%` })),
      `görevler ${det.tasks.done}/${det.tasks.total}`) : "",
    det.risk ? h("span", { class: `pill ${RISK_PILL[det.risk] || "mute"}` }, `risk ${det.risk}`) : "",
    det.size ? h("span", { class: "pill mute" }, det.size) : "",
    det.checks?.length ? h("span", { class: "row tight" }, det.checks.map(checkChip)) : "");
  const out = [head];
  if (det.status === "awaiting-approval") out.push(h("div", { class: "need approval-inline wide" }, approvalBox(t, det.gate || {})));
  if (t.children?.length) {
    out.push(h("section", { class: "section stack-box" },
      h("div", { class: "section-title" }, icon("layers"), t.status === "split" ? "Dilimler" : `Yığın (${t.parent?.id || ""})`,
        h("span", { class: "muted small" }, t.status === "split" ? " · bu ticket dilimlerin kabı; kendi koşusu yok" : ` · bu ticket dilim ${t.slice}, tabanı ${t.base_branch}`)),
      sliceList(t, t.children)));
  }
  if (t.status === "waiting") out.push(h("div", { class: "need blocked" }, h("div", { class: "need-kind" }, icon("clock"), " ", waitText(t)),
    h("div", { class: "muted small" }, t.mode === "controlled"
      ? "Kontrollü proje: beklediği ticket'lar bitince gelen kutusu başlatmak için sorar; kendiliğinden başlamaz."
      : "Beklediği ticket'lar bitince kendiliğinden başlar; panel her dakika ve bir ticket her bittiğinde yeniden bakar.")));
  if (t.status === "quota") out.push(h("div", { class: "need blocked" }, h("div", { class: "need-kind" }, icon("clock"), " ", quotaText(t)),
    h("div", { class: "muted small" }, `${quotaDetail(t)} Panel her dakika bakar; yeniden başlarsa da kaybolmaz.`)));
  if (t.status === "host") out.push(h("div", { class: "need blocked" }, h("div", { class: "need-kind" }, icon("clock"), " ", HOST_TEXT),
    h("div", { class: "muted small" }, `${hostDetail(t)} Panel her dakika bakar; yeniden başlarsa da kaybolmaz.`)));
  if (!det.plan_text.trim()) {
    out.push(empty(det.legacy ? "Bu ticket eski (v1) hatta koştu; planı hedef repodaki spec dosyasında." : "Henüz plan yok. Planlama aşaması bitince burada görünür.", "spec"));
  } else {
    const parsed = MD.sections(det.plan_text);
    if (parsed.intro.trim()) out.push(h("div", { class: "md plan-intro" }, MD.blocks(parsed.intro)));
    out.push(...sectionCards(parsed.sections, (s) => {
      if (/^Decisions \(user\)/.test(s.title)) return { cls: "decisions", icon: "user", tag: "senin kararların" };
      if (/^Review \(user/.test(s.title)) return { cls: "decisions", icon: "message", tag: "senin review'un" };
      if (/^Review \(codex/.test(s.title)) return { cls: "result", icon: "eye", tag: "Codex review" };
      return {};
    }));
  }
  if (det.deferred_text.trim()) {
    const entries = det.deferred || [];
    const open = entries.filter((e) => !e.ticket).length;
    out.push(h("details", { class: "section deferred-card" },
      h("summary", {}, h("span", { class: "section-title" }, icon("pin"), "Ertelenenler (deferred.md)"), h("span", { class: "grow" }),
        entries.length ? h("span", { class: "muted small" }, `${entries.length} bulgu · ${open} açık`) : "", icon("chevron", "chev")),
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
      out.push(h("h4", { class: "deferred-round" }, round == null ? "Tur belirtilmemiş" : `Tur ${round}`));
    }
    const made = e.ticket ? rowOf(det.target, e.ticket) : null;
    out.push(h("div", { class: "deferred-entry" },
      h("div", { class: "md" }, MD.blocks(e.text)),
      h("div", { class: "deferred-act" }, e.ticket
        ? h("button", { class: "small ghost", type: "button", disabled: !made, onclick: () => made && openDrawer(made, "plan") }, `→ ${e.ticket}`)
        : h("button", { class: "small", type: "button", title: `${e.type} ticket'ı taslağı; oluşturmadan önce düzenleyebilirsin`,
            onclick: () => convertDeferred(det.target, det.id, e) }, "Ticket'a çevir"))));
  }
  return out;
}

const PASS = /^(pass|passed|ok|success)$/i;
const checkChip = (c) => h("span", { class: `pill ${PASS.test(c.status) ? "ok" : /^(fail|failed|error)$/i.test(c.status) ? "bad" : "mute"}`,
  title: `${c.source === "engine" ? "pipeline çalıştırdı" : "ajan bildirdi"} · ${c.status}${c.detail ? `\n${c.detail}` : ""}` }, c.name);
const VERDICT_PILL = { READY: "ok", NEEDS_USER: "warn", FAILED: "bad" };
const STATE_TR = { running: "çalışıyor", ok: "tamam", failed: "başarısız", killed: "durduruldu", timeout: "zaman aşımı", lost: "kayıp" };

// What the project memory gave this ticket's sessions and which entries they said they relied on.
function memoryUse(det) {
  const sessions = (det.memory?.sessions || []).filter((s) => s.shown || s.used?.length);
  if (!sessions.length) return "";
  const used = det.memory.used || [];
  return h("details", { class: "section mem-use", open: true },
    h("summary", {}, h("span", { class: "section-title" }, icon("spec"), "Hafıza"),
      h("span", { class: "muted small" }, used.length ? `${used.length} kayıt kullanıldı` : "kullanılan kayıt yok"),
      h("span", { class: "grow" }), icon("chevron", "chev")),
    h("ul", { class: "section-body mem-use-list" }, sessions.map((s) => h("li", {},
      h("span", { class: "tag" }, s.stage),
      s.used?.length ? s.used.map((e) => h("a", { class: "mem-use-entry", href: "#memory", onclick: () => closeDrawer() }, h("span", { class: "id" }, e.id), " ", e.title))
        : h("span", { class: "muted small" }, `${s.shown} kayıt verildi, hiçbiri kullanılmadı`)))));
}

// Stage runs, grouped per run (newest first): verdict, route, the result's summary, checks, cost.
function renderStages(det) {
  if (!det.stage_runs.length) return [empty("Henüz oturum yok.", "layers")];
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
      h("h3", { class: "run-group-h" }, n === "–" ? "Oturumlar" : `Koşu ${n}`, h("span", { class: "muted small" }, ` · ${runs.length} oturum · ${money(cost)}`)),
      h("ol", { class: "timeline" }, runs.map(stageCard))));
  }
  out.push(
    det.worktree ? h("div", { class: "path muted mono" }, icon("folder"), det.worktree) : "",
    det.prepare_log ? h("details", { class: "section" }, h("summary", {}, h("span", { class: "section-title" }, icon("terminal"), "prepare.log")), h("pre", { class: "log" }, det.prepare_log)) : "");
  return out;
}

const RETRY_CAUSE = { nudge: "mesajınla yönlendirildi", lost: "süreci kayboldu", quiet: "sessiz kaldı", timeout: "süresi doldu" };
const COUNT_TR = [["important", "önemli", "bad"], ["nit", "nit", "mute"], ["pre_existing", "önceden vardı", "info"], ["unverified", "doğrulanmadı", "mute"]];

// How an interrupted session went on: "sessiz kaldı · yeniden denendi 1/2", or the user's nudge.
function retryPills(p) {
  const out = [];
  if (p.retry) {
    const cause = RETRY_CAUSE[p.retry.cause] || p.retry.cause;
    out.push(h("span", { class: "pill warn", title: p.retry.delay_s ? `${p.retry.delay_s} sn bekleyip aynı oturumda sürdü` : "aynı oturumda sürdü" },
      icon("retry"), p.retry.n ? `${cause} · yeniden denendi ${p.retry.n}/${p.retry.max ?? "?"}` : cause));
  }
  if (p.retry_of) out.push(h("span", { class: "pill info", title: "Yeni bir ziyaret değil: önceki oturum kaldığı yerden sürdü" }, `↻ ${p.retry_of} sürdü`));
  if (p.retry_exhausted) out.push(h("span", { class: "pill bad" }, p.retry_exhausted));
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
function askButton(t, question, label) {
  if (!window.ChatView) return "";
  return h("button", { class: "small ghost", title: "Sohbette sor (salt okunur bir oturum cevaplar)",
    onclick: (e) => { e.stopPropagation(); ChatView.ask(rowOf(t.target, t.id) || t, question, ctx); } }, icon("message"), label || "Sor");
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
  const question = `${p.stage} #${p.visit ?? "?"} (aşama oturumu ${p.name}, ${STATE_TR[p.state] || p.state}${p.verdict ? `, ${p.verdict}` : ""}): `
    + (ended ? "neden böyle bitti, ne düzeltir?" : "ne yaptı, neye karar verdi ve neden?");
  const counts = p.result?.counts || {};
  const nudge = p.nudged || p.nudge;
  return h("li", { class: `step ${tone}` + (drawer.highlight === p.name ? " row-highlight" : ""), id: `sr-${p.name}` },
    h("div", { class: "step-dot" }),
    h("div", { class: "step-body" },
      h("div", { class: "row tight wrap" },
        h("strong", {}, p.title || p.stage), p.visit > 1 || p.max_visits > 1 ? h("span", { class: "muted small", title: "ziyaret / en çok" }, `#${p.visit ?? "?"}${p.max_visits ? `/${p.max_visits}` : ""}`) : "",
        h("span", { class: `pill ${tone === "live" ? "info" : tone}` }, steered ? "yönlendirildi" : STATE_TR[p.state] || STATUS_TR[p.state] || p.state),
        p.verdict ? h("span", { class: `pill ${VERDICT_PILL[p.verdict] || "mute"}` }, p.verdict) : "",
        retryPills(p),
        p.route || p.next ? h("span", { class: "muted small", title: "rota → sıradaki adım" }, [p.route ? `rota ${p.route}` : "", p.next ? `→ ${p.next}` : ""].filter(Boolean).join(" ")) : "",
        p.via?.from ? h("span", { class: "muted small" }, `${p.via.from}${p.via.route ? ` (${p.via.route})` : ""} sonrası`) : "",
        h("span", { class: "grow" }), h("span", { class: "type" }, p.model || "?"), p.effort ? h("span", { class: "type", title: "efor" }, p.effort) : "",
        h("span", { class: "muted small" }, when(p.started_at))),
      nudge?.text ? h("div", { class: "nudge-note small" }, h("span", { class: "muted" }, `${p.nudged ? "Yönlendirme" : "Mesaj"} · ${nudge.by || "?"}: `), nudge.text) : "",
      COUNT_TR.some(([k]) => typeof counts[k] === "number") ? h("div", { class: "row tight wrap" },
        COUNT_TR.filter(([k]) => counts[k]).map(([k, label, cls]) => h("span", { class: `pill ${cls}` }, `${counts[k]} ${label}`)),
        COUNT_TR.every(([k]) => !counts[k]) ? h("span", { class: "pill ok" }, "açık bulgu yok") : "") : "",
      p.summary ? h("div", { class: "md stage-summary" }, MD.blocks(p.summary)) : "",
      evidence(p.artifacts),
      p.result_error ? h("pre", { class: "need-details" }, p.result_error) : "",
      p.result_warnings.length ? h("div", { class: "row tight wrap" }, p.result_warnings.map((w) => h("span", { class: "pill warn" }, icon("alert"), w))) : "",
      p.checks.length ? h("div", { class: "row tight wrap" }, h("span", { class: "muted small" }, "kontroller"), p.checks.map(checkChip)) : "",
      h("div", { class: "row tight wrap" },
        chip("dollar", money(p.cost_usd)),
        chip("clock", p.duration_ms ? duration(p.duration_ms / 1000) : "-"),
        chip("message", `${p.turns ?? "-"} tur`),
        p.remote_unchanged === true ? chip("shield", "remote'a dokunulmadı", "ok") : p.remote_unchanged === false ? chip("alert", `remote değişti: ${(p.remote_changed || []).join(", ") || "?"}`, "bad") : "",
        h("span", { class: "grow" }),
        askButton({ target: drawer.target, id: drawer.id }, question),
        h("button", { class: "small" + (open ? " on" : ""), "aria-expanded": String(open), onclick: toggle }, icon("activity"), "Transcript"),
        fileBtn("prompt", "Prompt"), fileBtn("result", "Sonuç"), p.has_checks_log ? fileBtn("checks", "Checks log") : ""),
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
  const t = rowOf(drawer.target, drawer.id) || det;
  const out = [];
  if (det.status === "failed" && !det.running) {
    // After a stop or a failure the user can turn the work: the text becomes a decision and the
    // run continues where it stopped.
    const ta = h("textarea", { rows: 3, class: "answer-box", placeholder: "Yön değiştiren not: ne farklı yapılmalı?", "aria-label": "Yön notu" });
    out.push(h("div", { class: "need failed wide" },
      h("div", { class: "need-kind" }, "Koşu durdu"),
      det.error ? h("pre", { class: "need-details" }, det.error) : "",
      h("div", { class: "muted small" }, "Not kararlarına eklenir ve koşu kaldığı aşamadan devam eder."),
      ta,
      h("div", { class: "row" }, h("button", { class: "primary", onclick: async (e) => {
        if (!ta.value.trim()) return toast("Önce notunu yaz", true);
        e.currentTarget.disabled = true;
        if (await act(t, "answer", ta.value.trim())) ta.value = "";
      } }, icon("message"), "Notla devam et"))));
  }
  if (!det.rounds.length) {
    if (!out.length) out.push(empty("Bu ticket için soru sorulmadı.", "message"));
    return out;
  }
  const openN = det.round?.n;
  for (const r of [...det.rounds].reverse()) {
    const isOpen = r.n === openN && !r.answered_at;
    const head = h("div", { class: "round-head" }, h("strong", {}, `Tur ${r.n}`),
      h("span", { class: "muted small" }, `koşu ${r.run ?? "?"} · ${r.stage || "?"} · ${when(r.asked_at)}`),
      r.answered_at ? h("span", { class: "pill ok" }, "cevaplandı") : isOpen ? h("span", { class: "pill warn" }, "seni bekliyor") : h("span", { class: "pill mute" }, "cevapsız"));
    if (isOpen) {
      out.push(h("article", { class: "round open" }, head, answerForm(t, r),
        h("div", { class: "row end" }, askButton(t, `Soru turu ${r.n} için cevap vermeme yardım et: her soru için ne önerirsin ve neden? Önce koda bak.`, "Yardım iste"))));
      continue;
    }
    out.push(h("article", { class: "round" }, head,
      r.intent_gap ? intentGapBox(t, r.intent_gap) : "",
      r.context && r.context !== r.intent_gap?.summary ? h("div", { class: "answer-context md" }, MD.blocks(r.context)) : "",
      h("ol", { class: "questions" }, (r.questions || []).map((q, i) => h("li", { class: "q" },
        h("div", { class: "q-text" }, h("span", { class: "q-id" }, q.id || `Q${i + 1}`), h("div", { class: "md grow" }, MD.blocks(q.text || ""))),
        q.options?.length ? h("ul", { class: "q-options small" }, [...new Set(q.options)].map((o) => h("li", {}, o))) : "",
        q.recommendation ? h("div", { class: "q-rec small" }, h("span", { class: "muted" }, "Önerilen: "), q.recommendation) : ""))),
      r.answer !== null && r.answer !== undefined ? h("div", { class: "answer-block" },
        h("div", { class: "muted small" }, `Cevap: ${r.by || "?"}${r.via ? ` · ${r.via}` : ""} · ${when(r.answered_at)}`),
        h("pre", { class: "answer-text" }, r.answer)) : ""));
  }
  if (det.answers_md.trim()) out.push(h("details", { class: "section" }, h("summary", {}, h("span", { class: "section-title" }, icon("spec"), "answers.md (ajanların gördüğü)")),
    h("pre", { class: "log small" }, det.answers_md)));
  return out;
}

// --- session transcript (Aşamalar) ------------------------------------------------------------------
// One session at a time, read in pages from /api/transcript. The main agent and each subagent get
// their own tab; in the main agent's flow a subagent is one line that opens its tab. The element is
// kept in drawer.tx and re-attached when the stage list re-renders, so tabs, open boxes and
// scrolling survive a refresh; a live session gets new items appended every few seconds.

const TX_FILTERS = [["all", "Hepsi"], ["text", "Mesajlar"], ["tool", "Komutlar"], ["error", "Hatalar"]];

function openTranscript(phase) {
  closeTranscript();
  const tabs = h("div", { class: "tx-tabs", role: "tablist", "aria-label": "Ajanlar" });
  const panes = h("div", { class: "tx-panes" });
  const foot = h("div", { class: "tx-foot" });
  const el = h("div", { class: "tx", "data-filter": "all" },
    tabs,
    h("div", { class: "tx-filters", role: "group", "aria-label": "Transcript filtresi" },
      TX_FILTERS.map(([key, label]) => h("button", { class: "small" + (key === "all" ? " on" : ""), "data-key": key, onclick: (e) => {
        el.dataset.filter = key;
        for (const b of e.currentTarget.parentNode.children) b.classList.toggle("on", b === e.currentTarget);
      } }, label))),
    panes, foot);
  drawer.tx = { phase, el, tabs, panes, foot, pos: 0, tools: new Map(), agents: new Map(), active: "main", loading: false };
  addAgent(drawer.tx, "main", "Ana ajan", null);
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
      icon("layers"), h("span", { class: "grow" }, `↳ ${label}`), h("span", { class: "tx-link-status muted small" }, "çalışıyor"), h("span", { class: "small" }, "sekmede aç →"));
    parentPane.append(agent.link);
    setAgentStatus(agent, "live", "çalışıyor");
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
  if (!main.children.length) main.append(empty(r.alive ? "Oturum başlıyor…" : "Bu oturumda kayıt yok.", "clock"));
  if (r.legacy && r.items.length) main.prepend(h("div", { class: "muted small" }, "Bu oturum canlı olay kaydından önce koştu; yalnız son mesajı var."));
  tx.tabs.hidden = tx.agents.size < 2;  // no subagents: no tab bar
  tx.foot.replaceChildren(
    r.more ? h("button", { class: "small", onclick: () => loadTranscript() }, "Devamını yükle") : "",
    r.alive && !r.more ? h("span", { class: "live-line" }, h("span", { class: "dot live" }), "oturum sürüyor") : "");
  if (r.alive && r.items.length && atBottom && d.tab === "stages") body.scrollTop = body.scrollHeight;
  if (r.alive && !r.more) tx.timer = setTimeout(loadTranscript, 3000);
}

function addTxItem(tx, item) {
  const main = tx.agents.get("main").pane;
  const where = (item.parent && tx.agents.get(item.parent)?.pane) || main;
  main.querySelector(":scope > .empty")?.remove();
  if (item.kind === "session") {
    where.append(h("div", { class: "tx-item tx-session" }, icon("play"), `oturum başladı · ${item.model || "?"}`));
  } else if (item.kind === "text") {
    where.append(h("div", { class: "tx-item tx-text" }, h("div", { class: "md" }, MD.blocks(item.text))));
  } else if (item.kind === "prompt") {
    where.append(h("details", { class: "tx-item tx-prompt" }, h("summary", {}, icon("message"), "subagent'a verilen görev"),
      h("pre", { class: "log small" }, item.text)));
  } else if (item.kind === "tool" && ["Agent", "Task"].includes(item.name)) {
    addAgent(tx, item.id, item.description || item.input?.subagent_type || "subagent", where);
    tx.tabs.hidden = false;
  } else if (item.kind === "tool" && item.name === "SubagentHandback" && typeof item.input?.message === "string") {
    // The subagent hands its report back through this call; its tool result only says so.
    const agent = item.parent && tx.agents.get(item.parent);
    if (agent) agent.handback = true;
    where.append(h("div", { class: "tx-item tx-report" }, h("div", { class: "muted small" }, "subagent raporu"), h("div", { class: "md" }, MD.blocks(item.input.message))));
  } else if (item.kind === "tool") {
    const box = h("details", { class: "tx-item tx-tool" },
      h("summary", {}, h("span", { class: "mono" }, item.summary || item.name)),
      toolInput(item));
    tx.tools.set(item.id, box);
    where.append(box);
  } else if (item.kind === "result") {
    const agent = tx.agents.get(item.tool_id);
    if (agent) {  // a subagent's final report closes its tab
      setAgentStatus(agent, item.is_error ? "bad" : "ok", item.is_error ? "hata" : "bitti");
      if (agent.handback && !item.is_error) return;  // the report is already in its tab
      agent.pane.append(h("div", { class: "tx-item tx-report" + (item.is_error ? " bad" : "") }, h("div", { class: "muted small" }, "subagent raporu"), h("div", { class: "md" }, MD.blocks(item.content))));
      if (item.is_error) agent.link?.classList.add("error");
      return;
    }
    const owner = tx.tools.get(item.tool_id);
    const out = h("div", { class: "tx-result" + (item.is_error ? " bad" : "") },
      h("div", { class: "muted small" }, item.is_error ? "hata" : "sonuç", item.truncated ? " · 16 KB'de kesildi" : ""),
      h("pre", { class: "log small" }, item.content || "(boş)"));
    if (owner) {
      owner.append(out);
      if (item.is_error) owner.classList.add("error");
    } else where.append(out);
  } else if (item.kind === "task") {
    where.append(h("div", { class: "tx-item tx-session" }, icon("terminal"), `arka plan ${item.status}: ${item.summary}`));
  } else if (item.kind === "end") {
    where.append(h("div", { class: "tx-item tx-end" + (item.is_error ? " bad" : "") },
      h("div", { class: "row tight" }, icon(item.is_error ? "alert" : "check"), h("strong", {}, "oturum bitti"), chip("dollar", money(item.cost)), chip("message", `${item.turns ?? "-"} tur`)),
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
  if (!jobs.length) return [empty("Panelden başlatılmış job yok.", "terminal")];
  return [h("ol", { class: "timeline" }, jobs.slice().reverse().map((j) => {
    const state = j.state === "running" ? "live" : j.state === "ok" ? "ok" : "bad";
    return h("li", { class: `step ${state}` }, h("div", { class: "step-dot" }),
      h("div", { class: "step-body" },
        h("div", { class: "row tight" }, h("strong", { class: "mono" }, j.command),
          h("span", { class: `pill ${state === "ok" ? "ok" : state === "live" ? "info" : "bad"}` }, j.state + (j.exit !== null ? ` · çıkış ${j.exit}` : "")),
          h("span", { class: "grow" }), h("span", { class: "muted small" }, when(j.started_at))),
        j.tail ? h("pre", { class: "log small" }, j.tail) : ""));
  }))];
}

// --- Codex reviews --------------------------------------------------------------------------------

function renderReviews(det) {
  const t = { target: drawer.target, id: drawer.id, pr: det.pr, branch: det.branch, base_branch: det.base_branch };
  const head = h("div", { class: "row" },
    det.status === "done" ? h("button", { class: "primary", onclick: () => act(t, "review") }, icon("eye"), det.reviews.length ? "Yeniden review" : "Codex review başlat") : "",
    h("span", { class: "muted small" }, `Codex, dalı ${det.base_branch}'e göre inceler; dosyaları değiştirmez.`));
  if (!det.reviews.length) return [head, empty("Henüz review yok.", "eye")];
  return [head, ...det.reviews.map((r, i) => {
    const pills = [
      h("span", { class: `pill ${r.state === "ok" ? "ok" : r.state === "running" ? "info" : "bad"}` }, r.state === "running" ? "sürüyor" : r.state === "ok" ? "bitti" : "hata"),
      r.head ? chip("branch", r.head.slice(0, 7), "mono") : "",
      r.stale ? h("span", { class: "pill warn" }, "eski: dal sonra değişti") : "",
      r.posted_at ? h("span", { class: "pill ok" }, icon("pr"), "PR'da") : "",
      r.findings?.length ? h("span", { class: "pill warn" }, `${r.findings.length} bulgu`) : "",
    ];
    const canPost = r.state === "ok" && det.pr && !r.posted_at && r.head && r.head === det.published_head;
    const actions = [];
    if (canPost) actions.push(h("button", { onclick: async () => {
      if (await confirmBox(`Bu review ${det.pr} PR'ına yorum olarak eklensin mi?`)) act(t, "post_review", r.id);
    } }, icon("pr"), "PR'a yorum ekle"));
    const box = h("div", { class: "fix-box" });
    if (r.state === "ok" && det.status === "done" && !det.legacy && !r.stale && r.findings?.length) actions.push(h("button", { class: "primary", onclick: () => openFix(t, r, box) }, icon("hammer"), "Bulguları düzelttir"));
    return h("div", { class: "section review-card" + (i ? "" : " latest") },
      h("div", { class: "review-head" }, h("strong", {}, when(r.started_at)), ...pills, h("span", { class: "grow" }), ...actions),
      r.state === "error" ? h("pre", { class: "need-details" }, r.stderr_tail || "codex hata verdi") : "",
      r.text ? h("div", { class: "section-body md" }, MD.blocks(r.text)) : r.state === "running" ? empty("Codex inceliyor…", "clock") : "",
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

function openFix(t, r, box) {
  const kept = new Map(r.findings.map((f) => [f, true]));
  box.replaceChildren(
    h("div", { class: "muted small" }, "Düzeltilmesini istemediklerinin işaretini kaldır. Kalanlar plana görev olarak eklenir ve düzeltme koşusu başlar; bitince PR'ı güncellemen istenir."),
    h("ul", { class: "checks" }, r.findings.map((f) => {
      const cb = h("input", { type: "checkbox", onchange: () => kept.set(f, cb.checked) });
      cb.checked = true;
      return h("li", { class: "ok" }, h("label", { class: "check finding" }, cb,
        f.priority ? h("span", { class: `pill ${/[01]/.test(f.priority) ? "bad" : /2/.test(f.priority) ? "warn" : "info"}` }, f.priority.replace(/[[\]]/g, "")) : "",
        h("span", {}, f.title || f.text), f.location ? h("span", { class: "muted small mono" }, f.location) : ""));
    })),
    h("div", { class: "row end" },
      h("button", { class: "ghost", onclick: () => box.replaceChildren() }, "Vazgeç"),
      h("button", { class: "primary", onclick: async () => {
        const chosen = r.findings.filter((f) => kept.get(f));
        if (!chosen.length) return toast("Düzeltilecek bulgu kalmadı", true);
        if (!(await confirmBox(`${t.id}: ${chosen.length} bulgu plana görev olarak eklensin ve düzeltme koşusu başlasın mı?\nBirkaç dolar tutabilir; sonra PR'ı güncellemen istenir.`))) return;
        if (await act(t, "fix", chosen.map(finding).join("\n"), { review: r.id })) box.replaceChildren();
      } }, icon("hammer"), `Gönder ve koştur`)));
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
  const select = h("select", { "aria-label": "Oturum", onchange: (e) => { d.phase = e.target.value || null; d.follow = !e.target.value; d.offset = 0; renderActivity(); } },
    h("option", { value: "" }, "en yeni oturum (takip et)"));
  const allBox = h("input", { type: "checkbox", onchange: (e) => { d.all = e.target.checked; d.offset = 0; renderActivity(); } });
  allBox.checked = d.all;
  const log = h("div", { class: "log", id: "activity-log" });
  const legend = h("div", { class: "legend muted small" },
    h("span", { class: "ll cmd" }, "$ komut"), h("span", { class: "ll edit" }, "edit dosya"), h("span", { class: "ll agent" }, "alt-ajan"),
    h("span", { class: "ll say" }, "» ajan"), h("span", { class: "ll err" }, "! hata"));
  $("d-body").replaceChildren(h("div", { class: "row" }, select, h("label", { class: "check" }, allBox, " her şey (okumalar, alt-ajan içi)"), h("span", { class: "grow" }), legend), log);
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
      select.replaceChildren(h("option", { value: "" }, "en yeni oturum (takip et)"), ...a.phases.map((p) => h("option", { value: p }, p)));
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
    if (!a.phases.length) log.replaceChildren(empty("Henüz oturum yok (koşu hazırlanıyor olabilir).", "clock"));
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
  $("repo-list").replaceChildren(h("p", { class: "muted small" }, "repolar aranıyor…"));
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
    h("div", { class: "muted small" }, `~/Developer altındaki repolar (${shown.length}/${candidates.length}) · birini seç ya da yolu yaz`),
    h("div", { class: "repos" }, shown.map((c) => h("button", {
      type: "button", class: "repo" + (c.target ? " added" : ""), disabled: !!c.target, title: c.path,
      onclick: () => { $("detect-path").value = c.path; detect(); },
    },
      icon(c.target ? "check" : "folder"), h("span", { class: "repo-name" }, c.name),
      c.target ? h("span", { class: "pill ok" }, "eklendi") : "",
      !c.target && c.project_doc ? h("span", { class: "pill info" }, "PROJECT.md") : "",
      !c.target && !c.remote ? h("span", { class: "pill warn" }, "remote yok") : "")),
    ),
  );
}

// The form's model: the detected spec fields, the proposal's lines (each {…, why, source, verify}) and the background
// job (agent and/or verification) whose result replaces the lines when it ends. Nothing is saved before "Config'e ekle".
const ob = { found: null, spec: null, proposal: null, request: null, view: null, timer: null, gen: 0, prUrl: null };
const OB_PHASE = { queued: "sırada", start: "başlıyor", agent: "ajan inceliyor", verify: "deneniyor", repair: "ajan onarıyor",
  "verify-again": "yeniden deneniyor", done: "bitti" };

async function detect() {
  const out = $("onboard-result");
  clearTimeout(ob.timer);
  ob.gen += 1;
  ob.request = null;
  ob.view = null;
  ob.prUrl = null;
  out.replaceChildren(h("p", { class: "muted" }, "inceleniyor…"));
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
  if (v && v.status === "pass") return h("span", { class: "pill ok ob-badge", title: v.reason || "" }, icon("check"), v.seconds != null ? `denendi · ${Math.round(v.seconds)} sn` : "denendi");
  if (v && v.status === "fail") return h("span", { class: "pill bad ob-badge", title: v.reason }, icon("alert"), `geçmedi: ${v.reason || "hata"}`);
  return h("span", { class: "pill mute ob-badge", title: (v && v.reason) || "" }, "denenmedi");
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
    ? [h("input", { class: "grow mono", value: item.run, "aria-label": "prepare komutu", oninput: edit("run") })]
    : kind === "env"
      ? [h("input", { class: "mono ob-key", value: item.key, "aria-label": "değişken", oninput: edit("key") }),
         h("input", { class: "grow mono", value: item.value, "aria-label": "değer", oninput: edit("value") })]
      : [h("input", { class: "mono ob-key", value: item.name, "aria-label": "kontrol adı", oninput: edit("name") }),
         h("input", { class: "grow mono", value: item.run, "aria-label": "kontrol komutu", oninput: edit("run") })];
  return h("div", { class: "ob-line" },
    h("div", { class: "row tight" }, ...inputs, badgeEl,
      h("button", { type: "button", class: "small ghost", title: "satırı sil", onclick: () => { ob.proposal[kind].splice(index, 1); renderOnboard(); } }, icon("x"))),
    h("div", { class: "ob-why muted small" },
      item.source === "agent" ? h("span", { class: "pill info" }, "ajan") : item.source === "user" ? h("span", { class: "pill" }, "elle") : "",
      item.suggested ? h("span", { class: "pill warn" }, "öneri · sahte değer") : "",
      item.why ? h("span", {}, item.why) : ""),
    v && v.left_files && v.left_files.length ? h("div", { class: "warn-text small" }, `bıraktığı dosyalar silindi: ${v.left_files.join(", ")}`) : "",
    v && v.status === "fail" && v.tail ? h("details", { class: "ob-tail" }, h("summary", { class: "small muted" }, "çıktı"), h("pre", {}, v.tail)) : "",
  );
}

function obSection(kind, title, blank) {
  return h("div", { class: "ob-section" },
    h("div", { class: "row" }, h("h3", { class: "grow" }, title),
      h("button", { type: "button", class: "small ghost", onclick: () => { ob.proposal[kind].push({ ...blank, why: "", source: "user" }); renderOnboard(); } }, icon("plus"), "satır")),
    ob.proposal[kind].length ? ob.proposal[kind].map((item, i) => obLine(kind, item, i)) : h("p", { class: "muted small" }, "yok"));
}

function obJobStatus() {
  const v = ob.view;
  if (!ob.request && !v) return "";
  if (!v) return h("p", { class: "muted small" }, "başlatılıyor…");
  const parts = [];
  if (v.running || (v.phase && v.phase !== "done")) {
    parts.push(h("div", { class: "row tight" }, h("span", { class: "pill info" }, icon("clock"), OB_PHASE[v.phase] || v.phase), h("span", { class: "muted small" }, v.message || "")));
    for (const line of v.live_lines || []) parts.push(h("div", { class: "ll small mono" }, line));
  }
  if (v.lost) parts.push(h("p", { class: "error-banner small" }, `iş yarıda kaldı: ${(v.job && v.job.tail) || "çıktı yok"}`));
  if (v.error) parts.push(h("p", { class: "error-banner small" }, v.error));
  if (v.phase === "done" && v.summary) {
    const s = v.summary;
    parts.push(h("p", { class: s.ok ? "small" : "warn-text small" },
      s.error ? `doğrulama yapılamadı: ${s.error}` : s.ok ? `hepsi denendi, geçti (${Math.round(s.seconds)} sn)` : `${s.failures.length} satır geçmedi (${Math.round(s.seconds)} sn)`,
      v.cost_usd ? ` · ajan maliyeti ${money(v.cost_usd)}` : ""));
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
    ? `DİKKAT: ${s.path} bir ŞİRKET reposu.\n${s.github_repo} reposunda, ${s.gh_user} hesabıyla, ${s.base_branch} üzerine AGENTS.md için draft PR açılsın mı?`
    : `${s.github_repo} reposunda ${s.base_branch} üzerine AGENTS.md için draft PR açılsın mı? (geçici worktree'de docs/agents-md dalı)`;
  if (!(await confirmBox(question))) return;
  try {
    const r = await api("/api/onboard/agents-md", { path: s.path, text, gh_user: s.gh_user, github_repo: s.github_repo, base_branch: s.base_branch,
      company_confirmed: company === true });
    ob.prUrl = r.url;
    toast(`draft PR açıldı: ${r.url}`);
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
    icon(c.ok ? "check" : "alert"), h("div", {}, h("div", {}, c.message), c.fix ? h("div", { class: "fix" }, c.fix) : ""))));
  const add = h("button", {
    class: "primary", onclick: async () => {
      const spec = collectSpec();
      if (!(await confirmBox(`[targets.${spec.name}] config dosyasının sonuna eklensin mi?\nÖnce yedek alınır.`))) return;
      try {
        const r = await api("/api/targets", { spec });
        toast(`${r.added} eklendi · yedek: ${r.backup}`);
        ob.gen += 1;
        clearTimeout(ob.timer);
        $("onboard-result").replaceChildren();
        $("onboard").hidden = true;
        refresh();
      } catch (err) {
        toast(err.message, true);
      }
    },
  }, icon("plus"), "Config'e ekle");
  if (!found.can_add) add.disabled = true;
  const agentBtn = h("button", { type: "button", disabled: busy, title: "Salt-okur bir ajan oturumu (en çok $1): eksikleri doldurur, şüpheli satırları düzeltir, AGENTS.md taslağı yazar; ardından doğrulama ve gerekirse bir onarım turu",
    onclick: () => startOnboardJob(true) }, icon("zap"), "Ajanla doldur");
  const verifyBtn = h("button", { type: "button", disabled: busy, title: "Geçici bir klonda prepare'i ve kontrolleri motorun koşturduğu gibi dener",
    onclick: () => startOnboardJob(false) }, icon("play"), "Doğrula");
  const notes = ob.proposal.notes || [];
  let agentsBox = "";
  if (!found.has_rules && ob.proposal.agents_md) {
    const area = h("textarea", { id: "ob-agents-md", rows: 14, oninput: (e) => { ob.proposal.agents_md = e.target.value; } });
    area.value = ob.proposal.agents_md;
    agentsBox = h("div", { class: "ob-section ob-agents" },
      h("div", { class: "row" }, h("h3", { class: "grow" }, "AGENTS.md taslağı"), found.company ? h("span", { class: "pill warn" }, "şirket reposu") : ""),
      h("p", { class: "muted small" }, "Repoda AGENTS.md yok; ajan bunu yazdı. Düzenleyebilirsin. 'Draft PR aç' geçici bir worktree'de docs/agents-md dalını açar, commit'ler, push'lar ve draft PR açar; senin checkout'una dokunmaz."),
      area,
      // Once the PR is open the button goes: a second click would push the same branch again.
      h("div", { class: "row end" }, ob.prUrl ? link(ob.prUrl, icon("pr"), "Draft PR açıldı")
        : h("button", { type: "button", disabled: !s.github_repo || !s.gh_user, onclick: () => openAgentsPr(area.value) }, icon("pr"), "Draft PR aç")));
  }
  $("onboard-result").replaceChildren(
    checks,
    h("div", { class: "onboard-grid" },
      h("label", { for: "ob-name" }, "ad"), field("name"),
      h("label", {}, "yol"), h("div", { class: "mono" }, s.path),
      h("label", { for: "ob-base_branch" }, "base branch"), field("base_branch"),
      h("label", { for: "ob-github_repo" }, "github repo"), field("github_repo"),
      h("label", { for: "ob-gh_user" }, "gh hesabı"), field("gh_user"),
      h("label", { for: "ob-mode" }, "mod"), modeSelect(s, found),
    ),
    h("div", { class: "row ob-tools" }, agentBtn, verifyBtn, h("span", { class: "muted small grow" }, "Kaydetmeden önce denenir; hiçbir şey 'Config'e ekle'ye kadar yazılmaz.")),
    obJobStatus(),
    obSection("prepare", "prepare (koşudan önce, ağ açık)", { run: "" }),
    obSection("env", "test ortamı", { key: "", value: "" }),
    obSection("checks", "kontroller (implement sonrası, sandbox'ta)", { name: "", run: "" }),
    ob.proposal.check_sandbox?.length ? h("p", { class: "muted small" }, `Sandbox ön ayarı: ${ob.proposal.check_sandbox.join(", ")} `
      + "(xcode: SwiftPM önbelleğine yazma ve simülatörün test soketi; başka hiçbir şey açılmaz). Config'e check_sandbox olarak yazılır.") : "",
    notes.length ? h("div", { class: "ob-section" }, h("h3", {}, "notlar"), h("ul", { class: "small" }, notes.map((n) => h("li", {}, n)))) : "",
    agentsBox,
    h("p", { class: "muted small" }, "Yer tutucular: {run_id}, {state}, {worktree}, {checkout} (ana checkout: git'in yok saydığı dosyaları prepare'de oradan kopyalamak için)."),
    h("div", { class: "row end" }, found.can_add ? "" : h("span", { class: "error-banner small" }, "Kırmızı maddeyi çözmeden eklenemez."), add),
  );
}

// The project's mode: controlled (every plan waits for approval, dependants ask before they start, no PR replies) is
// proposed for a company repository, personal elsewhere.
function modeSelect(s, found) {
  const sel = h("select", { id: "ob-mode", title: CONTROLLED_TITLE, onchange: (e) => { s.mode = e.target.value; } },
    h("option", { value: "controlled" }, "Kontrollü"), h("option", { value: "personal" }, "Kişisel"));
  sel.value = s.mode || (found.company ? "controlled" : "personal");
  s.mode = sel.value;
  return sel;
}

// --- stats ----------------------------------------------------------------------------------------

const tokens = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n || 0));
const OUTCOME_TR = { ready: "hazır", needs_user: "soru", failed: "başarısız", killed: "durduruldu", timeout: "zaman aşımı", lost: "kayıp", running: "sürüyor",
  done: "bitti (v1)", "ready-for-dev": "spec hazır (v1)", blocked: "takıldı (v1)" };
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
      const title = `${d.day}: ${money(d.cost)} · ${Object.entries(d.stages).map(([k, v]) => `${k} ${money(v)}`).join(" · ") || "maliyet yok"} · ${d.sessions} oturum`;
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
  const perLabel = units.has("usd") && !units.has("kota") ? "PR başına maliyet" : units.has("usd") ? "PR başına kota / maliyet" : "PR başına kota";
  const perPr = (r) => (r.cost_per_pr === null ? "–" : r.unit === "usd" ? money(r.cost_per_pr) : `≈${money(r.cost_per_pr)} kota`);
  const dash = (v, f) => (v === null || v === undefined ? "–" : f(v));
  const deliveryRows = [...dl.by_target, ...(dl.by_target.length > 1 ? [dl.overall] : [])];
  const delivery = card("Teslim", `birleşen PR'lar · son ${dl.days} gün`,
    h("div", { class: "delivery-table" },
      ["proje", "birleşen PR", "istek olmadan", "PR başına tur", perLabel].map((x, i) => h("span", { class: "th" + (i ? " num" : "") }, x)),
      deliveryRows.map((r) => [
        r === dl.overall ? h("strong", {}, "hepsi") : h("span", {}, r.name),
        h("span", { class: "num mono" }, String(r.merged)),
        h("span", { class: "num mono", title: r.merged ? `${r.clean}/${r.merged} PR, PR açıldıktan sonra değişiklik istenmeden birleşti` : "" }, dash(r.clean_share, (v) => `%${Math.round(v * 100)}`)),
        h("span", { class: "num mono" }, dash(r.rounds_per_pr, (v) => v.toFixed(2))),
        h("span", { class: "num mono" }, perPr(r))])),
    units.has("kota") ? h("p", { class: "muted small card-foot" }, "Abonelikli hesapta para ödenmez: değer, oturumların API fiyatıyla harcadığı kotanın karşılığı.") : "",
    !dl.overall.merged ? h("p", { class: "muted small" }, `Son ${dl.days} günde birleşen PR yok.`) : "");
  $("stats-body").replaceChildren(
    h("div", { class: "kpis" },
      kpi("Bugün", money(s.totals.today), today ? `${today.sessions} oturum${top ? ` · en çok ${top[0]}` : ""}` : "", "now"),
      kpi("Son 7 gün", money(s.totals.week), `${week} oturum`), kpi("Son 30 gün", money(s.totals.month), `${month} oturum`),
      kpi("Toplam", money(s.totals.all), `${s.totals.sessions} oturum · ${s.totals.tickets} ticket`)),
    delivery,
    h("div", { class: "stats-row" },
      card("Günlük maliyet", `aşamaya göre · son ${s.daily.length} gün`, bars,
        h("div", { class: "legend small" }, stageNames.map((n) => h("span", { class: "legend-item" }, h("span", { class: "sw", style: `background:${color(n)}` }), n)))),
      card("Projeye göre", "", h("div", { class: "hbars" }, s.by_target.map((g, i) => h("div", { class: "hbar-row" },
        h("div", { class: "hbar-top" }, h("span", {}, g.name), h("span", { class: "mono" }, money(g.cost))),
        bar((g.cost / maxTarget) * 100, "var(--text)", i * 0.08),
        h("div", { class: "hbar-sub" }, `${g.sessions} oturum · ${g.turns} tur · ${duration(g.duration_ms / 1000)}`,
          s.budgets[g.name] ? ` · oturum başı bütçe ${money(s.budgets[g.name])}` : "")))),
        Object.values(s.budgets).some(Boolean) ? "" : h("p", { class: "muted small card-foot" }, "Proje bütçesi tanımlı değil."))),
    h("div", { class: "stats-row" },
      card("Aşamalar", "maliyet · oturum · ortalama süre",
        h("div", { class: "stage-table" },
          h("span", { class: "th" }, "aşama"), h("span", { class: "th" }, "maliyet"), h("span", { class: "th num" }, "toplam"), h("span", { class: "th num" }, "oturum"),
          h("span", { class: "th num" }, "ort. maliyet"), h("span", { class: "th num" }, "ort. süre"),
          s.by_stage.map((g, i) => [
            h("span", { class: "stage-name" }, h("span", { class: "sw", style: `background:${color(g.name)}` }), g.name),
            bar((g.cost / maxStage) * 100, color(g.name), i * 0.05),
            h("span", { class: "num mono" }, money(g.cost)), h("span", { class: "num mono muted" }, g.sessions),
            h("span", { class: "num mono muted" }, money(avg(g, "cost"))), h("span", { class: "num mono muted" }, duration(avg(g, "duration_ms") / 1000))]))),
      h("div", { class: "stats-side" },
        card("Modeller", "",
          h("div", { class: "split-bar" }, s.models.map((m, i) => h("div", { style: `width:${(m.cost / totalModels) * 100}%;background:${MODEL_SHADES[i % 3]}` }))),
          h("div", { class: "model-list" }, s.models.map((m, i) => h("div", { class: "model-row" },
            h("span", { class: "sw", style: `background:${MODEL_SHADES[i % 3]}` }),
            h("div", { class: "grow" }, h("div", { class: "mono" }, m.name),
              h("div", { class: "muted small mono" }, `${tokens(m.output)} çıktı · ${tokens(m.input)} girdi · ${tokens(m.cache_read)} cache okuma · ${tokens(m.cache_write)} yazma`)),
            h("span", { class: "mono" }, money(m.cost)))))),
        card("Oturum sonuçları", String(totalOut),
          h("div", { class: "split-bar" }, outcomes.map(([k, v]) => h("div", { title: `${OUTCOME_TR[k] || k}: ${v}`, style: `width:${(v / totalOut) * 100}%;background:${OUTCOME_COLOR[k] || "var(--bad)"}` }))),
          h("div", { class: "outcome-list" }, outcomes.map(([k, v]) => h("span", { class: "outcome" },
            h("span", { class: "sw", style: `background:${OUTCOME_COLOR[k] || "var(--bad)"}` }), OUTCOME_TR[k] || k, h("span", { class: "mono" }, String(v)))))))),
    card("En pahalı ticket'lar", "", h("div", { class: "top-tickets" }, s.top_tickets.map((t) => h("button", { class: "top-ticket", type: "button",
      onclick: () => { const row = rowOf(t.target, t.id); if (row) openDrawer(row, "stages"); else toast(`${t.id} artık listede değil`, true); } },
      h("span", { class: "top-cost" }, money(t.cost)), h("span", { class: "mono small" }, `${t.target} · ${t.id}`), h("span", { class: "muted small" }, `${t.sessions} oturum`))))),
  );
}

// --- memory: what each project remembers -------------------------------------------------------------
// Per project (memory.py): proposals from question-round answers and from the retro after a ticket ends,
// each with the evidence it came from, wait here; nothing reaches the memory files until it is approved
// (as proposed or edited). Below them, the entries the stage sessions are given, with how often they were used.

const SOURCE_TR = {
  user_comment: "senin review yorumun", overruled_finding: "review eledi, sen düzelttirdin",
  recurring_finding: "turlar boyunca tekrarlayan bulgu", answer_against_recommendation: "önerinin tersine cevabın",
  plan_note: "plana değişiklik notun", github: "PR'daki geri bildirim",
  answer: "soru turundaki cevabın", check_fixed: "sonra geçen kontrol", run_recovered: "sonra düzelen koşu", intent_gap: "niyet sapması",
};
const ON_REVIEW_PAGE = new Set(["user_comment", "overruled_finding", "recurring_finding"]);
const MEMORY_KINDS = [["kural", "Kurallar"], ["karar", "Kararlar"], ["tuzak", "Tuzaklar"], ["olay", "Olaylar"]];
const MEMORY_MAIN = { kural: "Kural", karar: "Karar", tuzak: "Ne yapılır", olay: "Sonuç", yol: "Başlık" };  // the field "Düzenle ve onayla" edits
const ORIGIN_TR = { answer: "soru turu", retro: "retro", migration: "eski kurallardan taşındı", lead: "lider" };
// Proposal groups on the Hafıza page: the entry kinds, then roadmap scope changes (approving one edits roadmap.md).
const PENDING_KINDS = [...MEMORY_KINDS.filter(([k]) => k !== "olay"), ["yol", "Yol haritası değişiklikleri"]];

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
  loadMemory.pending = data.targets.reduce((n, t) => n + t.pending.length, 0);
  renderMemory(data.targets);
  const unseen = data.targets.filter((t) => t.pending.length || t.warnings.length);
  if (unseen.length) Promise.all(unseen.map((t) => api("/api/attention/seen", { target: t.target, memory: true }))).then(() => refresh()).catch(() => { /* stays unread */ });
}

function memoryWhere(t) {
  const label = t.source === "vault" ? `kasa: ${t.vault_dir}` : t.source === "override" ? "özel klasör" : "Ziggy durumu";
  return h("span", { class: "muted small mono", title: t.dir }, `${label} · ${homeShort(t.dir)}`);
}

function renderMemory(targets) {
  const blocks = [];
  for (const t of targets) {
    const stored = MEMORY_KINDS.reduce((n, [k]) => n + t.entries[k].length, 0);
    const pending = PENDING_KINDS.map(([k, label]) => [label, t.pending.filter((c) => c.kind === k)]).filter(([, cs]) => cs.length);
    blocks.push(h("section", { class: "block mem-target" },
      h("div", { class: "block-head" },
        h("h2", {}, t.target, h("span", { class: "count" }, t.pending.length ? String(t.pending.length) : "")),
        memoryWhere(t)),
      t.hint ? h("p", { class: "muted small mem-hint" }, t.hint) : "",
      t.warnings.length ? h("div", { class: "notice mem-warnings" }, icon("alert"),
        h("div", { class: "grow" }, h("strong", {}, "Hafıza dosyalarında okunamayan yerler var"),
          h("ul", { class: "mem-warning-list small" }, t.warnings.map((w) => h("li", {}, w))))) : "",
      pending.length ? h("div", { class: "small muted mem-label" }, "Bekleyen öneriler") : "",
      ...pending.map(([label, cs]) => h("div", { class: "mem-kind" },
        h("h3", { class: "mem-kind-h" }, label, h("span", { class: "muted small" }, ` · ${cs.length} öneri`)),
        h("div", { class: "stack" }, cs.map((c) => memoryCard(t, c))))),
      stored ? [h("div", { class: "small muted mem-label" }, "Hafızada"), MEMORY_KINDS.map(([k, label]) => memoryEntries(t, k, label))]
        : !t.pending.length ? empty("Henüz hafıza kaydı yok. Soru turlarında verdiğin cevaplar karar önerisi olur; bir ticket bitince kısa bir retro oturumu review yorumlarından, düzelttirdiğin bulgulardan, sonradan geçen kontrollerden ve düzelen koşulardan kural, karar ve tuzak önerir. Sen onaylamadan hafızaya hiçbir şey yazılmaz.", "spec") : ""));
  }
  const decided = targets.flatMap((t) => t.decided.map((c) => ({ t, c })))
    .sort((a, b) => String(b.c.decided_at).localeCompare(String(a.c.decided_at))).slice(0, 20);
  if (decided.length) {
    blocks.push(h("details", { class: "section mem-decided" },
      h("summary", {}, h("span", { class: "section-title" }, "Son kararlar", h("span", { class: "muted small" }, String(decided.length)))),
      h("div", { class: "section-body" }, h("ul", { class: "mem-log" }, decided.map(({ t, c }) => h("li", {},
        h("span", { class: `pill ${c.status === "approved" ? "ok" : "mute"}` }, c.status === "approved" ? (c.edited ? "düzenlenip onaylandı" : "onaylandı") : "reddedildi"),
        h("span", { class: "muted small" }, `${t.target} · ${c.kind} · ${when(c.decided_at)}`),
        c.entry ? h("span", { class: "id" }, c.entry) : "",
        h("span", { class: "md small" }, MD.inline(c.approved_title || c.title)),
        c.replaced?.length ? h("span", { class: "muted small" }, `${c.replaced.join(", ")} yerine`) : ""))))));
  }
  const off = targets.filter((t) => !t.retro).map((t) => t.target);
  if (off.length) blocks.push(h("p", { class: "muted small" }, `Retro kapalı: ${off.join(", ")} (config'te retro = false); bu projelere yalnız soru turlarındaki cevaplardan öneri gelir.`));
  if (!targets.length) blocks.push(empty("Proje yok.", "spec"));
  $("memory-body").replaceChildren(...blocks);
  $("memory-body").querySelector(".mem-card")?.classList.add("lead");  // the first proposal: your next move here
}

function scopeLine(scope) {
  const paths = scope?.paths || [];
  const topics = scope?.topics || [];
  if (!paths.length && !topics.length) return "";
  return h("div", { class: "muted small mem-scope" },
    paths.length ? ["dosyalar ", ...paths.flatMap((p, i) => [i ? ", " : "", h("code", {}, p)])] : "",
    paths.length && topics.length ? " · " : "",
    topics.length ? `konular ${topics.join(", ")}` : "");
}

function memorySource(t, s) {
  const open = () => {
    const row = rowOf(t.target, s.ticket);
    if (row) openDrawer(row);
    else toast(`${s.ticket} bulunamadı`, true);
  };
  return h("li", { class: "mem-source" },
    h("div", { class: "row tight" },
      s.ticket ? h("span", { class: "id" }, s.ticket) : "", s.ref ? h("span", { class: "tag" }, s.ref) : "",
      h("span", { class: "muted small grow" }, SOURCE_TR[s.kind] || s.kind, s.where ? ` · ${s.where}` : ""),
      s.ticket ? h("button", { class: "small", onclick: open }, icon("ticket"), "Ticket") : "",
      s.ticket && ON_REVIEW_PAGE.has(s.kind) ? h("a", { class: "button small", href: ReviewPage.hash(t.target, s.ticket) }, icon("diff"), "İnceleme") : "",
      s.url ? h("a", { class: "button small", href: s.url, target: "_blank", rel: "noopener noreferrer" }, icon("external"), "GitHub") : ""),
    s.summary ? h("div", { class: "small mem-quote" }, s.summary) : "");
}

function memoryCard(t, c) {
  const main = MEMORY_MAIN[c.kind];
  const titleBox = h("input", { type: "text", "aria-label": "Başlık", maxlength: 120 });
  const box = h("textarea", { rows: 3, class: "answer-box", "aria-label": main, maxlength: 600 });
  const replace = h("input", { type: "checkbox" });
  const editor = h("div", { class: "mem-edit stack", hidden: true },
    h("label", { class: "mem-edit-field" }, h("span", { class: "muted small" }, "Başlık"), titleBox),
    h("label", { class: "mem-edit-field" }, h("span", { class: "muted small" }, main), box));
  const decide = async (btn, action, edited) => {
    btn.disabled = true;
    try {
      const out = await api(`/api/memory/${encodeURIComponent(t.target)}/candidates/${encodeURIComponent(c.id)}`,
        { action, ...(action === "approve" ? { replace: replace.checked } : {}), ...(edited || {}) });
      const made = out.candidate;
      toast(action === "approve" ? (c.kind === "yol" ? `${t.target}: yol haritasına işlendi (${made.entry})`
        : `${t.target}: ${made.entry} olarak eklendi${made.replaced?.length ? ` · ${made.replaced.join(", ")} yerine` : ""}`)
        : `${t.target}: öneri reddedildi; hafıza değişmedi`);
    } catch (e) {
      toast(e.message, true);
    }
    await loadMemory();
    refresh();
  };
  const approve = h("button", { class: "small primary", onclick: (e) => decide(e.currentTarget, "approve") }, icon("check"), "Onayla");
  const edit = h("button", { class: "small", onclick: () => {
    editor.hidden = false;
    titleBox.value = c.title;
    box.value = c.fields[main] || "";
    box.focus();
    actions.hidden = true;
  } }, icon("spec"), "Düzenle ve onayla");
  if (c.kind === "yol" && !["ekle", "evre ekle"].includes(c.fields?.["İşlem"])) edit.hidden = true;  // nothing to word in a removal or a move
  const reject = h("button", { class: "small ghost", onclick: (e) => decide(e.currentTarget, "reject") }, icon("x"), "Reddet");
  const actions = h("div", { class: "row" }, approve, edit, reject,
    h("span", { class: "muted small grow" }, [c.ticket, ORIGIN_TR[c.origin] || c.origin].filter(Boolean).join(" · "), ` · ${ago(c.created_at)}`));
  editor.append(h("div", { class: "row" },
    h("button", { class: "small primary", onclick: (e) => {
      if (!titleBox.value.trim()) return toast("Başlık boş olamaz", true);
      if (!box.value.trim()) return toast(`${main} boş olamaz`, true);
      decide(e.currentTarget, "approve", { title: titleBox.value, text: box.value });
    } }, icon("check"), "Kaydet ve onayla"),
    h("button", { class: "small ghost", onclick: () => { editor.hidden = true; actions.hidden = false; } }, "Vazgeç"),
    h("span", { class: "muted small" }, "Tek satır; kapsam, kaynak ve tarih olduğu gibi kalır.")));
  return h("article", { class: "panel mem-card" },
    h("div", { class: "row tight" }, h("span", { class: "tag" }, c.kind), h("strong", { class: "md mem-title" }, MD.inline(c.title))),
    h("ul", { class: "mem-fields" }, Object.entries(c.fields || {}).map(([name, value]) =>
      h("li", { class: "md" }, h("strong", {}, `${name}: `), MD.inline(value)))),
    scopeLine(c.scope),
    c.similar?.length ? h("div", { class: "mem-similar small" },
      c.similar.map((s) => h("div", {}, h("span", { class: "muted" }, "benzer: "), h("span", { class: "id" }, s.id), " ", s.title)),
      h("label", { class: "check", title: "Onaylanınca benzer kayıtlara \"yerini aldı\" yazılır; aşamalara artık verilmezler" }, replace, "yerine geçsin")) : "",
    c.sources?.length ? h("div", { class: "small muted mem-label" }, "Kaynaklar") : "",
    c.sources?.length ? h("ul", { class: "mem-sources" }, c.sources.map((s) => memorySource(t, s))) : "",
    editor, actions);
}

function memoryEntries(t, kind, label) {
  const all = t.entries[kind];
  if (!all.length) return "";
  const live = all.filter((e) => e.status === "active");
  const old = all.filter((e) => e.status !== "active");
  const list = (es) => h("ul", { class: "mem-entries" + (kind === "olay" ? " compact" : "") }, es.map((e) => memoryEntry(t, e)));
  return h("div", { class: "mem-kind" },
    h("h3", { class: "mem-kind-h" }, label, h("span", { class: "muted small" }, ` · ${live.length}`)),
    live.length ? list(live) : h("p", { class: "muted small" }, "Etkin kayıt yok."),
    old.length ? h("details", { class: "mem-old" }, h("summary", { class: "muted small" }, `Yerini almış ya da unutulmuş: ${old.length}`), list(old)) : "");
}

function memoryEntry(t, e) {
  const forget = async (btn) => {
    if (!(await confirmBox(`${e.id} unutulsun mu?\nAşamalara artık verilmez; metni dosyada "Durum: unutuldu" ile kalır.`))) return;
    btn.disabled = true;
    try {
      await api(`/api/memory/${encodeURIComponent(t.target)}/entries/${encodeURIComponent(e.id)}`, { action: "forget" });
      toast(`${t.target}: ${e.id} unutuldu`);
    } catch (err) {
      toast(err.message, true);
    }
    await loadMemory();
  };
  const main = e.fields[MEMORY_MAIN[e.kind]];
  const status = e.status === "replaced" ? h("span", { class: "pill mute" }, `yerini aldı ${e.replaced_by || ""}`.trim())
    : e.status === "forgotten" ? h("span", { class: "pill mute" }, "unutuldu") : "";
  return h("li", { class: "mem-entry" + (e.status === "active" ? "" : " inactive"), id: `mem-${t.target}-${e.id}` },
    h("div", { class: "row tight" },
      h("span", { class: "id" }, e.id), h("strong", { class: "md mem-title grow" }, MD.inline(e.title)), status,
      e.stale ? h("span", { class: "pill warn", title: `${t.stale_days} gündür kullanılmadı` }, "soluk") : "",
      e.stale ? h("span", { class: "muted small" }, "Unutalım mı?") : "",
      h("span", { class: "muted small nowrap" }, e.uses ? `${e.uses} kez kullanıldı · son ${ago(e.last_used)}` : "henüz kullanılmadı"),
      e.status === "active" ? h("button", { class: "small ghost", onclick: (ev) => forget(ev.currentTarget) }, icon("x"), "Unut") : ""),
    main ? h("div", { class: "md small" }, MD.inline(main)) : "",
    e.kind === "olay" && e.fields.PR ? h("div", { class: "muted small" }, /^https?:\/\//.test(e.fields.PR) ? link(e.fields.PR, e.fields.PR) : e.fields.PR,
      e.fields["Birleşti"] ? ` · birleşti ${e.fields["Birleşti"]}` : "") : "",
    e.kind === "olay" ? "" : scopeLine(e.scope));
}

// --- project page: PROJECT.md, the router, open PRs, this project's tickets -----------------------
// The project document comes from the base branch (/api/targets/<n>/project); a request goes to the
// router, whose proposal (a ticket type) waits for a click before a ticket is made and started.

const TYPE_OPTIONS = [["feat", "feat · yeni özellik"], ["fix", "fix · hata düzeltme"], ["refactor", "refactor"], ["chore", "chore · bakım"], ["docs", "docs"], ["project", "project · PROJECT.md'yi yaz/güncelle"]];
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
  const t = s.targets.find((x) => x.name === p.name);
  if (!t) {
    $("pj-head").replaceChildren(h("h1", {}, p.name), h("span", { class: "muted" }, "Böyle bir proje yok."));
    return;
  }
  // The document follows merged PRs: reload now and then.
  if (Date.now() - (p.at || 0) > 60000) loadProjectState(false);
  if (Date.now() - (p.pullsAt || 0) > 60000) loadPulls(false);
  const [cls, text] = t.error ? ["bad", "config hatası"] : t.active?.length ? ["live", t.active.length > 1 ? `${t.active.length} koşu` : "koşu var"] : ["ok", "hazır"];
  const headSig = JSON.stringify([t.name, cls, text, t.github_repo, t.bmad, t.mode]);
  if (headSig !== $("pj-head").dataset.sig) {
    $("pj-head").dataset.sig = headSig;
    $("pj-head").replaceChildren(h("h1", {}, t.name), h("span", { class: "badge" }, h("span", { class: `dot ${cls}` }), text), bmadPill(t), controlledPill(t),
      t.github_repo ? link(`https://github.com/${t.github_repo}`, h("span", { class: "mono small" }, t.github_repo)) : "");
  }
  const rows = s.tickets.filter((x) => x.target === p.name && !x.archived);
  ticketList($("pj-tickets"), rows, { key: ["project", p.name], showTarget: false, empty: "Bu projede açık ticket yok." });
  renderPulls(t, rows);
  // Progress follows the tickets (the server writes it); a draft being written is asked after more often.
  if (Date.now() - (p.roadAt || 0) > (p.roadmap?.draft?.running ? 4000 : 30000)) loadRoadmap();
  renderRoadmap(t);
  const docSig = JSON.stringify([p.state?.project_doc, p.state?.base, p.error, rows.filter((x) => x.type === "project").map((x) => [x.id, x.status, x.running])]);
  if (!force && docSig === $("pj-doc").dataset.sig) return;
  $("pj-doc").dataset.sig = docSig;
  $("pj-base").textContent = p.state ? `${p.state.base} dalından` : "";
  if (!p.state) {
    $("pj-doc").replaceChildren(empty(p.error || "Proje belgesi okunuyor…", p.error ? "alert" : "clock"));
    return;
  }
  $("pj-doc").replaceChildren(docCard(t, p.state.project_doc || {}, rows.filter((x) => x.type === "project")));
}

// --- the project's roadmap: phases with progress, items with their ticket and PR, "Ticket aç" on the next ones ---
// Source: the memory's roadmap.md (vault or Ziggy's state) or BMad's epics.md + sprint-status.yaml (read-only).
// Without one: "Yol haritası çıkar" (a read-only draft session), the draft in an editor, "Taslak olarak kaydet".

const ROAD_ITEM = {
  todo: ["yapılacak", "mute"], ticket: ["ticket açıldı", "info"], "in-progress": ["sürüyor", "info"], review: ["incelemede", "warn"],
  pr: ["PR açık", "info"], merged: ["birleşti", "ok"], done: ["bitti", "ok"],
};
const ROAD_PHASE = { todo: ["sırada", "mute"], active: ["aktif", "volt"], done: ["bitti", "ok"] };
const ROAD_WHERE = { vault: (r) => `kasa: ${r.vault_dir}`, override: () => "özel klasör", state: () => "Ziggy durumu", bmad: () => "BMad" };
const roadEditing = () => { const ta = $("pj-roadmap").querySelector("textarea"); return !!ta && (document.activeElement === ta || ta.value !== ta.dataset.orig); };

function renderRoadmap(t) {
  const p = project;
  const r = p.roadmap;
  const linked = (r?.phases || []).flatMap((ph) => ph.items.filter((it) => it.ticket).map((it) => {
    const row = rowOf(t.name, it.ticket);
    return [it.ticket, row?.status, row?.running];
  }));
  const sig = JSON.stringify([r, p.roadError, !!t.bmad?.installed, linked]);
  const box = $("pj-roadmap");
  if (sig === box.dataset.sig || roadEditing()) return;  // never under a cursor: the draft may be half edited
  box.dataset.sig = sig;
  $("pj-road-where").textContent = r?.source ? `${ROAD_WHERE[r.where]?.(r) || r.where} · ${homeShort(r.path)}` : "";
  $("pj-road-where").title = r?.path || "";
  if (!r) {
    box.replaceChildren(empty(p.roadError || "Yol haritası okunuyor…", p.roadError ? "alert" : "clock"));
    return;
  }
  const parts = [];
  if (r.shadowed) parts.push(h("p", { class: "muted small road-note" }, `Hafızadaki ${homeShort(r.shadowed)} de var; BMad'in epics.md'si kullanılıyor (iki kaynak olunca BMad kazanır).`));
  if (r.warnings?.length) {
    parts.push(h("div", { class: "notice mem-warnings" }, icon("alert"),
      h("div", { class: "grow" }, h("strong", {}, "Yol haritasında okunamayan yerler var"),
        h("ul", { class: "mem-warning-list small" }, r.warnings.map((w) => h("li", {}, w))))));
  }
  if (!r.source) parts.push(...roadmapEmpty(t, r));
  else if (!r.phases.length) parts.push(empty("Yol haritasında evre yok.", "spec"));
  else parts.push(h("div", { class: "road-phases" }, r.phases.map((ph) => roadPhase(t, r, ph))));
  box.replaceChildren(...parts);
}

function roadPhase(t, r, ph) {
  const [label, cls] = ROAD_PHASE[ph.status] || [ph.status, "mute"];
  const pct = ph.total ? (100 * ph.done) / ph.total : 0;
  return h("section", { class: `stage-card road-phase ${ph.id === r.current ? "current" : ""} ${ph.status}` },
    h("div", { class: "row tight wrap" }, h("span", { class: "tag" }, ph.id), h("strong", { class: "grow road-title" }, ph.title),
      h("span", { class: `pill ${cls}` }, label), h("span", { class: "muted small mono nowrap" }, `${ph.done}/${ph.total}`)),
    h("div", { class: "hbar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(ph.total), "aria-valuenow": String(ph.done),
      "aria-label": `${ph.id} ilerleme` }, h("div", { class: "hbar-fill", style: `width:${pct.toFixed(1)}%;background:var(--ok)` })),
    ph.items.length ? h("ul", { class: "road-items" }, ph.items.map((it) => roadItem(t, r, ph, it))) : h("p", { class: "muted small" }, "Bu evrede madde yok."));
}

function roadItem(t, r, ph, it) {
  const [label, cls] = ROAD_ITEM[it.status] || [it.status, "mute"];
  const row = it.ticket ? rowOf(t.name, it.ticket) : null;
  const next = r.next?.includes(it.id);
  return h("li", { class: `road-item ${it.status}` + (next ? " next" : "") },
    h("div", { class: "road-item-head" },
      h("span", { class: "id" }, it.id), h("span", { class: "road-item-title" }, it.title),
      h("span", { class: "road-item-side" },
        h("span", { class: `pill ${cls}` }, label),
        it.ticket ? (row ? h("button", { class: "small", onclick: () => openDrawer(row) }, icon("ticket"), it.ticket) : h("span", { class: "chip mono" }, it.ticket)) : "",
        it.pr ? link(it.pr, chip("pr", "PR #" + it.pr.split("/").pop(), "linked")) : "",
        it.status === "todo" && !it.ticket ? h("button", { class: `small ${next ? "primary" : ""}`, title: "Composer'ı bu maddeyle doldurur; oluşturmak senin elinde",
          onclick: () => roadmapTicket(t.name, ph, it) }, icon("plus"), "Ticket aç") : "")),
    it.detail ? h("div", { class: "muted small road-detail" }, it.detail) : "");
}

function roadmapEmpty(t, r) {
  if (t.bmad?.installed) {
    return [empty("Bu projede BMad kurulu ama epics.md yok. BMad'in epic ve hikâye akışıyla (create-epics-and-stories) _bmad-output/planning-artifacts/epics.md'yi oluştur; Ziggy onu ve sprint-status.yaml'ı yol haritası olarak okur.", "spec")];
  }
  const d = r.draft;
  const start = (label, cls) => h("button", { class: cls || "", onclick: async (e) => {
    e.currentTarget.disabled = true;
    try {
      await api(`/api/targets/${encodeURIComponent(t.name)}/roadmap/draft`, {});
      toast(`${t.name}: yol haritası taslağı yazılıyor`);
    } catch (err) {
      toast(err.message, true);
    }
    await loadRoadmap();
  } }, icon("play"), label);
  const cost = `${r.model} · en çok $1 · salt okunur: README, docs/PROJECT.md, specler ve hafızadaki bağlam ile kararlar`;
  if (!d) {
    return [h("div", { class: "stage-card road-empty" },
      h("strong", {}, "Bu projenin yol haritası yok"),
      h("p", { class: "muted small" }, "Evreler ve maddeleri: her sabah sıradaki maddeler önerilir, her madde bir ticket olur. Taslağı bir ajan çıkarır; sen düzenleyip kaydedene kadar hiçbir şey yazılmaz."),
      h("div", { class: "row tight wrap" }, start("Yol haritası çıkar", "primary"), h("span", { class: "muted small" }, cost)))];
  }
  if (d.running) return [h("div", { class: "stage-card road-empty" }, h("div", { class: "row tight" }, h("span", { class: "dot live" }), h("strong", {}, "Taslak yazılıyor…"),
    h("span", { class: "muted small" }, d.started_at ? ago(d.started_at) : "")), h("p", { class: "muted small" }, cost))];
  if (!d.draft) {
    return [h("div", { class: "stage-card road-empty" },
      h("div", { class: "row tight" }, h("span", { class: "pill bad" }, STATE_TR[d.state] || d.state || "hata"), h("strong", {}, "Taslak çıkmadı")),
      d.error ? h("pre", { class: "small road-error" }, d.error) : "",
      h("div", { class: "row tight wrap" }, start("Yeniden dene", "primary"), h("span", { class: "muted small" }, cost)))];
  }
  const ta = h("textarea", { class: "answer-box mono road-draft", rows: 18, spellcheck: "false", "aria-label": "Yol haritası taslağı" });
  ta.value = d.draft;
  ta.dataset.orig = d.draft;
  const save = h("button", { class: "primary", onclick: async (e) => {
    if (!ta.value.trim()) return toast("Taslak boş", true);
    e.currentTarget.disabled = true;
    try {
      project.roadmap = await api(`/api/targets/${encodeURIComponent(t.name)}/roadmap/save`, { text: ta.value });
      ta.dataset.orig = ta.value;
      toast(`${t.name}: yol haritası kaydedildi`);
    } catch (err) {
      toast(err.message, true);
      e.currentTarget.disabled = false;
      return;
    }
    ta.blur();
    $("pj-roadmap").dataset.sig = "";
    renderProject(snapshot, true);
  } }, icon("check"), "Taslak olarak kaydet");
  return [h("div", { class: "stage-card road-empty" },
    h("div", { class: "row tight wrap" }, h("span", { class: "pill volt" }, "taslak"), h("strong", { class: "grow" }, d.summary || "Yol haritası taslağı"),
      d.cost != null ? h("span", { class: "muted small mono" }, money(d.cost)) : ""),
    d.notes?.length ? h("ul", { class: "small muted road-notes" }, d.notes.map((n) => h("li", {}, n))) : "",
    h("p", { class: "muted small" }, `Düzenle, sonra kaydet: ${homeShort(r.memory_path)} olur. "## P1 · başlık" bir evre, "- [ ] R-1 · başlık" bir madde, altındaki girintili satır ayrıntısı.`),
    ta,
    h("div", { class: "row tight wrap" }, save, start("Yeniden çıkar", "ghost")))];
}

function docCard(target, doc, docTickets) {
  const active = docTickets.find((x) => x.status !== "done" || x.running);
  const actions = [];
  if (doc.exists) actions.push(h("button", { class: "small", onclick: () => openDoc("PROJECT.md", doc.path, doc.text || "") }, icon("spec"), "Oku"));
  if (active) actions.push(h("button", { class: "small", onclick: () => openDrawer(active) }, icon("ticket"), active.id));
  else actions.push(h("button", { class: "small ghost", onclick: () => showProposal({ type: "project", title: doc.exists ? "Update PROJECT.md" : "Write PROJECT.md", reason: "Proje belgesini sen seçtin.", text: $("pj-text").value.trim() }) },
    icon(doc.exists ? "retry" : "play"), doc.exists ? "Güncelle" : "Yaz"));
  return h("div", { class: `stage-card doc-card ${doc.exists ? "final" : ""}` },
    h("div", { class: "row tight" }, h("strong", {}, "PROJECT.md"),
      h("span", { class: `pill ${doc.exists ? "ok" : "mute"}` }, doc.exists ? "base dalında var" : "yok"),
      h("span", { class: "grow" }), active ? h("span", { class: "muted small" }, active.running ? "çalışıyor" : STATUS_TR[active.status] || active.status) : ""),
    h("div", { class: "muted small mono" }, doc.path || "docs/PROJECT.md"),
    doc.exists ? h("div", { class: "muted small" }, doc.sha ? h("span", { class: "mono" }, doc.sha.slice(0, 7)) : "", doc.subject ? ` · ${doc.subject}` : "", doc.date ? ` · ${when(doc.date)}` : "", doc.lines ? ` · ${doc.lines} satır` : "")
      : h("div", { class: "muted small" }, "Ürünün ne olduğu, kapsamı ve kararları burada durur; ajanlar her aşamada okur. 'project' tipinde bir ticket yazar ve PR açar."),
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
    box.replaceChildren(empty("PR'lar okunuyor…", "clock"));
    return;
  }
  if (!p.pulls.length) {
    box.replaceChildren(empty("Bu projede açık PR yok.", "pr"));
    return;
  }
  box.replaceChildren(...p.pulls.map((pr) => {
    const row = pr.ticket && rows.find((x) => x.id === pr.ticket);
    const who = pr.mine ? "sen" : pr.author.replace(/^app\//, "");
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
    actions.push(h("a", { class: "button small ghost", href: pr.url, target: "_blank", rel: "noopener noreferrer", "aria-label": `PR #${pr.number} GitHub'da`, title: "GitHub'da aç" }, icon("external")));
    return h("div", { class: "story pull" },
      h("span", { class: "decision-n" }, `#${pr.number}`),
      h("div", { class: "story-body" }, h("strong", {}, pr.title), meta),
      h("div", { class: "row tight wrap end pull-actions" }, actions));
  }));
}

async function adoptPull(target, pr) {
  const owner = pr.mine ? "" : `\nPR ${pr.author.replace(/^app\//, "")} kullanıcısının; review dala dokunmaz, düzeltmeleri push etmeden önce ayrıca sorulur.`;
  if (!(await confirmBox(`${target.name}: PR #${pr.number} — "${pr.title}" Codex'e review ettirilsin mi?\nPR, pr-${pr.number} ticket'ı olur (kendi worktree'si); ${pr.base}'e göre fark incelenir (1–3 dk).${owner}`))) return;
  try {
    const out = await api(`/api/targets/${encodeURIComponent(target.name)}/pulls`, { number: pr.number, review: true });
    toast(`${out.ticket.id}: Codex review başladı`);
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
  if (!text) return toast("Önce ne yapmak istediğini yaz", true);
  const btn = $("pj-route");
  btn.disabled = true;
  $("pj-proposal").replaceChildren(h("div", { class: "proposal muted" }, h("span", { class: "dot live" }), "Nasıl bir ticket gerektiğine bakılıyor…"));
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
  const kind = h("select", { "aria-label": "Tip" }, TYPE_OPTIONS.map(([v, label]) => h("option", { value: v, selected: v === p.type }, label)));
  const title = h("input", { class: "grow", value: p.title || "", placeholder: "Ticket başlığı", "aria-label": "Ticket başlığı" });
  const body = h("textarea", { rows: 3, placeholder: "Ayrıntı (isteğe bağlı): ajan bunu talep olarak okur", "aria-label": "Ayrıntı" });
  body.value = p.text || "";
  const start = h("button", { class: "primary", onclick: async () => {
    if (!title.value.trim()) return toast("Başlık boş", true);
    const type = kind.value;
    if (!(await confirmBox(`${project.name}: "${title.value.trim()}" (${type}) ticket'ı açılıp koşsun mu?\n${pipelineFor(type)} hattı aşama aşama koşar; birkaç dolar, 10–30 dk.`))) return;
    start.disabled = true;
    try {
      const out = await api("/api/new", { target: project.name, type, title: title.value.trim(), body: body.value, run: true, language: $("pj-lang").value });
      toast(out.job?.queued ? `${out.ticket.id} kuyruğa alındı; koşu slotları boşalınca başlar` : `${out.ticket.id} başladı`);
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
  } }, icon("check"), "Onayla ve başlat");
  $("pj-proposal").replaceChildren(h("div", { class: "proposal" },
    h("div", { class: "row tight" }, icon("zap"), h("strong", {}, "Öneri"), h("span", { class: "muted small grow" }, p.reason || "")),
    h("div", { class: "row" }, kind, title),
    body,
    h("div", { class: "row end" }, h("button", { class: "ghost", onclick: () => $("pj-proposal").replaceChildren() }, "Vazgeç"), start)));
  title.focus();
}

$("pj-lang").addEventListener("change", () => store.set(localStorage, "ziggy.lang", $("pj-lang").value || "-"));

// --- Sohbet ---------------------------------------------------------------------------------------
// Free chats about a project (talk.py): "okuyan" in the main checkout, "yazan" in a worktree of its own on
// chat/<id>. #talk/<target>, #talk/<target>/<id>, #talk/<target>/new?mode=write. The conversation itself is
// chat.js's, through a conv adapter; a new chat exists once its first message is sent.

const TALK_MODE = { read: "okuyan", write: "yazan" };
const talkHash = (target, id, mode) => `#talk/${enc(target)}${id ? `/${id}` : mode ? `/new?mode=${mode}` : ""}`;
const talk = { key: null, list: null, listAt: 0, target: null, view: null, viewSig: null };

function talkRoute() {
  const [path, query] = location.hash.slice(1).split("?");
  const [, target, id] = path.split("/");
  const live = (snapshot?.targets || []).filter((t) => !t.error).map((t) => t.name);
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
  const live = snapshot.targets.filter((t) => !t.error).map((t) => t.name);
  if ([...sel.options].map((o) => o.value).join() !== live.join()) sel.replaceChildren(...live.map((n) => h("option", { value: n }, n)));
  sel.value = route.target;
  $("talk-new-read").href = talkHash(route.target, null, "read");
  $("talk-new-write").href = talkHash(route.target, null, "write");
  if (!route.target) { $("talk-chat").replaceChildren(empty("Önce bir proje ekle.", "box")); return; }
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
      h("span", { class: "talk-item-title" }, c.live ? h("span", { class: "dot live" }) : "", c.title || "(boş sohbet)"),
      h("span", { class: "muted small" }, `${TALK_MODE[c.mode]} · ${c.turns} mesaj · ${ago(c.updated_at)}`))))
    : [h("li", { class: "muted small" }, "Henüz sohbet yok.")]));
}

function renderTalkHead(route) {
  const v = talk.view;
  const c = v?.chat;
  const mode = c?.mode || route.mode;
  const changes = v?.changes;
  const where = mode === "write"
    ? (c?.worktree ? h("span", { class: "muted small mono" }, icon("branch"), c.branch, changes ? ` · ${changes.ahead} commit · ${changes.dirty.length} değişmiş dosya` : "")
      : h("span", { class: "muted small" }, "İlk mesajla kendi worktree'si açılır (chat/<id> dalı, prepare komutlarıyla)."))
    : h("span", { class: "muted small" }, "Ana checkout'u okur; dosya değiştirmez, komut çalıştırmaz.");
  $("talk-head").replaceChildren(h("div", { class: "row tight wrap" },
    h("strong", { class: "grow talk-title" }, c?.title || (mode === "write" ? "Yeni yazan sohbet" : "Yeni okuyan sohbet")),
    h("span", { class: `pill ${mode === "write" ? "warn" : "mute"}` }, TALK_MODE[mode]),
    route.id ? h("button", { class: "small", type: "button", title: "Sohbetten bir ticket taslağı: composer'da düzenleyip oluşturursun",
      onclick: () => talkToTicket(route) }, icon("ticket"), "Ticket'a çevir") : "",
    route.id ? h("button", { class: "small ghost", type: "button", onclick: (e) => deleteTalk(e.currentTarget, route) }, icon("x"), "Sil") : ""),
  where);
}

function talkConv(route) {
  const base = `/api/talk/${enc(route.target)}`;
  const known = () => talk.list || { models: [], model: "" };
  const mode = () => talk.view?.chat?.mode || route.mode;
  return {
    key: `talk/${route.target}/${route.id || "new"}`, draftKey: `ziggy.talk.${route.target}.${route.id || `new-${route.mode}`}`,
    threads: false, placeholder: `${route.target} hakkında yaz…`,
    // An existing chat's mode and title come with its first load, after the tab is built.
    get writes() { return mode() === "write"; },
    get title() { return talk.view?.chat?.title || "Sohbet"; },
    empty: mode() === "write"
      ? `${route.target} üzerinde kendi worktree'sinde çalışan bir Claude oturumu: dosya düzenler, komut çalıştırır, commit atar.`
      : `${route.target} hakkında her şeyi sorabilirsin: kod nasıl çalışıyor, bir issue ne diyor, nereden başlanır.`,
    suggestions: () => mode() === "write"
      ? ["Bu repo nasıl kurulu, testler nasıl koşuyor?", "Bana atanmış açık issue'ları listele."]
      : ["Bu projenin mimarisini özetle.", "Bana atanmış açık issue'ları listele.", "Son değişiklikler neler?"],
    info: () => mode() === "write" ? "Yanıtlar bu sohbetin worktree'sinde çalışan bir Claude oturumundan gelir; push edemez."
      : "Yanıtlar salt-okunur bir Claude oturumundan gelir; dosya değiştirmez.",
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
    stop: () => api(`${base}/${route.id}`, { action: "stop" }).then(() => toast("Yanıt durduruluyor")),
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
  if (!(await confirmBox(`Bu sohbet silinsin mi?${write ? "\n\nWorktree'si ve chat dalı da silinir." : ""}`))) return;
  btn.disabled = true;
  const base = `/api/talk/${enc(route.target)}/${route.id}`;
  try {
    await api(base, { action: "delete" });
  } catch (e) {
    // A branch with work on it asks once more, naming what would go.
    if (!/still holds work/.test(e.message) || !(await confirmBox(`${e.message}\n\nYine de silinsin mi?`))) { toast(e.message, true); btn.disabled = false; return; }
    try { await api(base, { action: "delete", force: true }); } catch (e2) { toast(e2.message, true); btn.disabled = false; return; }
  }
  toast("Sohbet silindi");
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
        o.kind === "org" ? `${o.owner}  (organizasyon · ${o.gh_user} ile)` : `${o.owner}  (kişisel hesap)`)));
    }
    if (!$("np-owner").dataset.touched) $("np-owner").value = `${d.gh_user}|${d.gh_user}`;
    const logins = d.claude_logins || [];
    if ($("np-claude").options.length !== logins.length) {
      $("np-claude").replaceChildren(...logins.map((l) => h("option", { value: l.dir || "" },
        `${l.dir ? homeShort(l.dir) : "Varsayılan giriş (~/.claude)"} — ${l.account || "giriş yok"}`)));
    }
    if (!$("np-claude").dataset.touched) $("np-claude").value = d.claude_config_dir || "";
    const accounts = logins.map((l) => l.account).filter(Boolean);
    const same = accounts.length > 1 && new Set(accounts).size === 1;
    $("np-info").replaceChildren(
      same ? h("span", { class: "pill warn" }, icon("alert"), "Bütün Claude girişleri aynı hesapta") : "",
      same ? h("span", {}, "Kişisel hesap için terminalde: ", h("code", {}, "cd ~ && command claude /login")) : h("span", {}, "Klasöre göre önerildi; değiştirebilirsin."));
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
  if (!(await confirmBox(`${where} oluşturulsun mu?\n• README, AGENTS.md, docs/PROJECT.md taslağı ve .gitignore ile ilk commit main'e\n• GitHub'da ${body.owner}/${body.name} (${body.private ? "private" : "public"}) açılıp push edilir; PR'lar ${body.gh_user} hesabıyla\n• Koşular ${$("np-claude").selectedOptions[0]?.textContent || "varsayılan Claude girişi"} ile\n• Proje pipeline'a eklenir`))) return;
  const btn = e.submitter;
  btn.disabled = true;
  const log = h("pre", { class: "need-details" }, "başlatılıyor…");
  $("project-result").replaceChildren(log);
  try {
    const { job } = await api("/api/projects", body);
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500));
      const j = await api(`/api/jobs/${job.id}`);
      log.textContent = j.tail || "çalışıyor…";
      if (j.state === "running") continue;
      if (j.state === "ok") {
        toast(`${body.name} oluşturuldu ve eklendi`);
        $("project-form").reset();
        delete $("np-owner").dataset.touched;
        delete $("np-claude").dataset.touched;
        $("project").hidden = true;
        refresh();
      } else toast(`${body.name}: oluşturulamadı`, true);
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
  box.replaceChildren(h("p", { class: "muted small" }, "PR durumları GitHub'dan okunuyor…"));
  let found;
  try {
    found = (await api("/api/cleanup")).candidates;
  } catch (e) {
    box.replaceChildren(h("p", { class: "error-banner" }, e.message));
    return;
  }
  if (!found.length) {
    box.replaceChildren(empty("Temizlenecek worktree yok: PR'ı olan ve worktree'si duran ticket bulunmadı."));
    return;
  }
  const checks = found.map((c) => {
    const cb = h("input", { type: "checkbox", disabled: !c.cleanable });
    cb.checked = c.cleanable;
    return [c, cb];
  });
  const go = h("button", { class: "danger", onclick: async () => {
    const items = checks.filter(([, cb]) => cb.checked).map(([c]) => ({ target: c.target, id: c.id }));
    if (!items.length) return toast("Seçili ticket yok", true);
    if (!(await confirmBox(`${items.length} worktree silinsin mi?\nMerge edilmiş PR'ların yerel dalı da silinir; GitHub'daki dal, PR ve koşu kayıtları kalır.`))) return;
    try {
      const { results } = await api("/api/cleanup", { items });
      const bad = results.filter((r) => !r.ok);
      toast(bad.length ? `${results.length - bad.length} temizlendi, ${bad.length} atlandı: ${bad.map((r) => r.id + " (" + r.error + ")").join(", ")}` : `${results.length} worktree temizlendi`, bad.length > 0);
      box.hidden = true;
      refresh();
    } catch (e) {
      toast(e.message, true);
    }
  } }, icon("x"), "Seçilenleri temizle");
  box.replaceChildren(h("div", { class: "cleanup" },
    h("div", { class: "muted small" }, "PR'ı merge edilmiş ya da kapanmış ticket'ların worktree'leri. Değişikliği ya da PR'da olmayan commit'i olan atlanır."),
    h("ul", { class: "checks" }, checks.map(([c, cb]) => h("li", { class: c.cleanable ? "ok" : "warn" },
      h("label", { class: "check" }, cb, h("span", { class: "id" }, c.id), " ", c.title),
      h("span", { class: `pill ${c.pr_state === "MERGED" ? "ok" : c.pr_state === "CLOSED" ? "mute" : "warn"}` }, (c.pr_state || "?").toLowerCase()),
      c.cleanable ? h("span", { class: "muted small" }, c.delete_branch ? "worktree + yerel dal" : "worktree (dal kalır)") : h("span", { class: "muted small" }, c.reasons.join("; "))))),
    h("div", { class: "row end" }, h("button", { class: "ghost", onclick: () => { box.hidden = true; } }, "Vazgeç"), go)));
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

showView();
refresh();
setInterval(refresh, 3000);
