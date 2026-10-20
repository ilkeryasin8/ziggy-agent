"use strict";
// Review the ticket's changes before a PR: the ticket page's
// "Değişiklikler" tab. Diff text, comments and findings are untrusted: every
// text goes in through text nodes; syntax colours come from Prism.tokenize as <span> classes.

(function () {
  const LS_LAYOUT = "ziggy.review.layout";
  const LS_FILTER = "ziggy.review.findings";
  const SEVERITIES = ["critical", "high", "medium", "low"];
  const DISPOSITIONS = ["fix", "decision", "defer", "dismiss"];
  const SEV_TR = { critical: "kritik", high: "yüksek", medium: "orta", low: "düşük" };
  const DISP_TR = { fix: "düzelt", decision: "karar", defer: "ertele", dismiss: "boşver" };
  const LS_GUIDE = "ziggy.review.guide";
  const CLASSES = ["important", "nit", "pre-existing"];
  const CLASS_TR = { important: "önemli", nit: "küçük", "pre-existing": "önceden vardı" };
  const CLASS_TITLE = {
    important: "Bu değişiklikte düzeltilmeli",
    nit: "Yalnızca not; işi geri göndermez",
    "pre-existing": "Bu değişiklikle gelmedi (deferred.md'ye yazıldı)",
  };
  const CLASS_PILL = { important: "bad", nit: "mute", "pre-existing": "info" };
  const OUTCOME_TR = { fixed: "düzeltti", skipped: "atladı", no_change: "değişiklik gerekmedi" };
  const CONF_TR = { low: "düşük", medium: "orta", high: "yüksek" };
  const SERVED = /\.(png|jpe?g|gif|webp|svg|mp4|webm|mov|log|txt|md|json)$/i; // what /api/files serves
  const MAX_PR_TITLE = 256;
  const MAX_PR_BODY = 60000;
  const STATUS_LETTER = { added: "A", modified: "M", deleted: "D", renamed: "R", copied: "C", typechange: "T" };
  const MAX_HIGHLIGHT = 300000;
  const enc = encodeURIComponent;
  let h = null; // ctx.h of the current render

  const apiBase = (t, id) => `/api/changes/${enc(t)}/${enc(id)}`;

  // The review is the ticket page's Değişiklikler tab. `show=dismissed` opens it with the card of findings the AI
  // review dismissed unfolded; `show=github` scrolls to the GitHub feedback card.
  function hash(target, id, file, range, show) {
    const q = ["tab=changes"];
    if (file) q.push(`file=${enc(file)}`);
    if (range && range !== "all") q.push(`range=${enc(range)}`);
    if (show) q.push(`show=${enc(show)}`);
    return `#ticket/${enc(target)}/${enc(id)}?${q.join("&")}`;
  }

  // The tab's hash, and the old #review/<target>/<id> pages (links kept in notes and notifications).
  function parseHash(text) {
    const m = /^#(?:review|ticket)\/([^/?]+)\/([^/?]+)(?:\?(.*))?$/.exec(text || "");
    if (!m) return null;
    const q = new URLSearchParams(m[3] || "");
    if (text.startsWith("#ticket/") && q.get("tab") !== "changes") return null;
    return { target: decodeURIComponent(m[1]), id: decodeURIComponent(m[2]), file: q.get("file"), range: q.get("range"), show: q.get("show") };
  }

  const ago = (iso) => {
    if (!iso) return "-";
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return "az önce";
    if (s < 3600) return `${Math.floor(s / 60)} dk önce`;
    if (s < 86400) return `${Math.floor(s / 3600)} sa önce`;
    return `${Math.floor(s / 86400)} gün önce`;
  };
  const short = (sha) => (sha || "").slice(0, 9);
  const plural = (n, word) => `${n} ${word}`;
  const blobKey = (f) => f.new_blob || "deleted";

  async function ask(ctx, text) {
    return ctx.confirm ? ctx.confirm(text) : window.confirm(text);
  }

  const clsOf = (x) => (CLASSES.includes(x.class) ? x.class : "important");
  const confidenceText = (c) => (typeof c === "number" ? `%${Math.round(c * 100)} emin` : c ? `güven: ${CONF_TR[c] || c}` : "");
  const stageLabel = (r) => `${r.stage}${r.visit ? ` #${r.visit}` : ""}`;

  // A link to the stage run (phase dir) that made a commit or listed evidence: the ticket page's Aşamalar tab.
  function stageLink(ctx, target, id, r, title) {
    return h("button", { class: "link-btn small", title, onclick: (e) => { e.stopPropagation(); ctx.openStage(target, id, r.stage_dir); } },
      stageLabel(r));
  }

  // Commits, newest first, each with the stage run that made it; "elle" (by hand) only when the runs recorded heads.
  function commitList(d, ctx, target, id, onCommit) {
    const attributed = d.commits.some((c) => c.stage_dir);
    return h("ul", { class: "rv-list" }, d.commits.map((c) => h("li", { class: "row" },
      h("button", { class: "ghost small mono", onclick: () => onCommit(c) }, c.short),
      h("span", { class: "grow rv-ellipsis" }, c.subject),
      c.stage_dir ? stageLink(ctx, target, id, c, "Bu commit'i yapan aşama koşusunu aç")
        : attributed ? h("span", { class: "muted small", title: "Hiçbir aşama koşusu bu commit'i yapmadı" }, "elle") : "",
      h("span", { class: "muted small" }, ago(c.at)))));
  }

  // Evidence groups (newest stage run first): images and videos in a gallery, logs and text as links.
  function evidenceView(groups, ctx, target, id) {
    return groups.map((g) => {
      const media = g.artifacts.filter((a) => a.exists && (a.kind === "image" || a.kind === "video") && SERVED.test(a.path));
      const rest = g.artifacts.filter((a) => !media.includes(a));
      return h("div", { class: "rv-ev-group" },
        h("div", { class: "row small" }, stageLink(ctx, target, id, g, "Bu kanıtı listeleyen aşama koşusunu aç"),
          g.at ? h("span", { class: "muted" }, ago(g.at)) : ""),
        media.length ? h("div", { class: "rv-gallery" }, media.map(evidenceFigure)) : "",
        rest.length ? h("ul", { class: "rv-list rv-ev-files" }, rest.map(evidenceRow)) : "");
    });
  }

  function evidenceFigure(a) {
    const cap = a.caption || a.path;
    const media = a.kind === "video"
      ? h("video", { controls: true, preload: "metadata", src: a.url, title: a.path })
      : h("a", { href: a.url, target: "_blank", rel: "noopener noreferrer", title: a.path }, h("img", { src: a.url, loading: "lazy", alt: cap }));
    return h("figure", { class: "rv-ev" }, media, h("figcaption", {}, cap));
  }

  function evidenceRow(a) {
    const cap = a.caption ? ` — ${a.caption}` : "";
    if (!a.exists) return h("li", { class: "small muted" }, h("span", { class: "mono" }, a.path), " (dosya yok)", cap);
    if (!SERVED.test(a.path)) return h("li", { class: "small" }, h("span", { class: "mono" }, a.path), cap,
      h("span", { class: "muted" }, " (panelde açılmaz)"));
    return h("li", { class: "small" },
      h("a", { class: "mono", href: a.url, target: "_blank", rel: "noopener noreferrer" }, a.path), cap);
  }

  // --- highlighting -------------------------------------------------------------------------------

  const BY_EXT = {
    js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "jsx", ts: "typescript", mts: "typescript",
    cts: "typescript", tsx: "tsx", py: "python", pyi: "python", sh: "bash", bash: "bash", zsh: "bash",
    json: "json", jsonc: "json", yml: "yaml", yaml: "yaml", md: "markdown", markdown: "markdown", go: "go",
    rs: "rust", java: "java", kt: "kotlin", kts: "kotlin", swift: "swift", c: "c", h: "c", cc: "cpp",
    cpp: "cpp", cxx: "cpp", hpp: "cpp", hh: "cpp", cs: "csharp", rb: "ruby", php: "php", sql: "sql",
    toml: "toml", css: "css", scss: "scss", html: "markup", htm: "markup", xml: "markup", svg: "markup",
    vue: "markup", graphql: "graphql", gql: "graphql", ini: "ini", cfg: "ini", dart: "dart", diff: "diff",
    patch: "diff",
  };

  function grammarFor(path) {
    const P = window.Prism;
    if (!P || !P.languages) return null;
    const base = path.slice(path.lastIndexOf("/") + 1);
    if (/^Dockerfile/.test(base)) return P.languages.docker || null;
    const dot = base.lastIndexOf(".");
    const lang = dot > 0 ? BY_EXT[base.slice(dot + 1).toLowerCase()] : null;
    return (lang && P.languages[lang]) || null;
  }

  // Prism's token tree flattened to lines of {text, cls}; a token spanning a newline keeps its class on each line.
  function tokenizeLines(text, grammar) {
    const lines = [[]];
    const push = (s, cls) => s.split("\n").forEach((part, i) => {
      if (i > 0) lines.push([]);
      if (part) lines[lines.length - 1].push({ text: part, cls });
    });
    const walk = (tokens, cls) => {
      for (const t of tokens) {
        if (typeof t === "string") { push(t, cls); continue; }
        const own = ["tok-" + t.type, ...[].concat(t.alias || []).map((a) => "tok-" + a)];
        const c = [cls, ...own].filter(Boolean).join(" ");
        if (typeof t.content === "string") push(t.content, c);
        else walk(Array.isArray(t.content) ? t.content : [t.content], c);
      }
    };
    if (!grammar || text.length > MAX_HIGHLIGHT) return text.split("\n").map((s) => (s ? [{ text: s, cls: "" }] : []));
    try {
      walk(window.Prism.tokenize(text, grammar), "");
      return lines;
    } catch (e) {
      return text.split("\n").map((s) => (s ? [{ text: s, cls: "" }] : []));
    }
  }

  // Each hunk's old side (ctx+del) and new side (ctx+add) tokenized as one text, so multi-line strings come out right.
  function highlightDiff(diff, grammar) {
    for (const hunk of diff.hunks) {
      const oldL = hunk.lines.filter((l) => l.t === "ctx" || l.t === "del");
      const newL = hunk.lines.filter((l) => l.t === "ctx" || l.t === "add");
      const oldSeg = tokenizeLines(oldL.map((l) => l.s).join("\n"), grammar);
      const newSeg = tokenizeLines(newL.map((l) => l.s).join("\n"), grammar);
      let oi = 0, ni = 0;
      for (const l of hunk.lines) {
        if (l.t === "del") l.seg = oldSeg[oi++];
        else if (l.t === "add") l.seg = newSeg[ni++];
        else if (l.t === "ctx") { l.seg = newSeg[ni++]; oi++; }
      }
    }
  }

  function code(line) {
    const td = h("td", { class: "rv-code" });
    if (line.seg) for (const s of line.seg) td.append(s.cls ? h("span", { class: s.cls }, s.text) : document.createTextNode(s.text));
    else td.append(document.createTextNode(line.s));
    return td;
  }

  // --- drawer tab -------------------------------------------------------------------------------

  // The user_reviews index (since:user:<n>) of a sent round, by its head.
  function pointFor(d, r) {
    const p = d.points.find((x) => x.kind === "user" && x.head === r.head);
    return p ? p.n : null;
  }

  const sevClass = (s) => (s === "critical" || s === "high" ? "bad" : s === "medium" ? "warn" : "mute");

  // --- page ---------------------------------------------------------------------------------------

  function loadFilter() {
    try {
      const f = JSON.parse(localStorage.getItem(LS_FILTER) || "null");
      if (f && Array.isArray(f.severities)) {
        if (!Array.isArray(f.classes)) f.classes = CLASSES.slice(); // saved before findings had classes
        return f;
      }
    } catch (e) { /* default */ }
    return { show: true, latestOnly: true, hideFixed: true, severities: SEVERITIES.slice(), dispositions: DISPOSITIONS.slice(),
      classes: CLASSES.slice() };
  }

  function renderPage(container, params, ctx, opts) {
    const embedded = !!(opts && opts.embedded);  // inside the ticket page, which shows the ticket and the way back
    h = ctx.h;
    const S = {
      target: params.target, id: params.id, file: params.file || null, range: params.range || "all", pin: null,
      data: null, sig: "", layout: localStorage.getItem(LS_LAYOUT) === "split" ? "split" : "unified",
      filter: loadFilter(), collapsed: new Map(), full: new Set(), diffs: new Map(), showUncommitted: false,
      timer: null, observer: null, alive: true, scrolled: false, editing: 0, selected: new Set(), top: null,
      dismissedOpen: params.show === "dismissed", showDismissed: params.show === "dismissed",
      ghSelected: new Set(), ghLogs: new Set(), showGithub: params.show === "github",
    };
    const base = apiBase(S.target, S.id);

    async function load(force) {
      const q = new URLSearchParams({ range: S.range });
      if (S.pin) q.set("to", S.pin);
      let d;
      try {
        d = await ctx.api(`${base}?${q}`);
      } catch (e) {
        if (!S.data) container.replaceChildren(h("div", { class: "banner bad" }, e.message));
        else ctx.toast(e.message, true);
        return;
      }
      if (!S.alive) return;
      if (!S.pin && d.range.kind !== "commit" && d.range.to) S.pin = d.range.to;
      const sig = JSON.stringify([d.range, d.new_commits, d.files.map((f) => [f.path, f.new_blob, f.viewed]), d.comments,
        d.rounds.length, d.findings.map((f) => [f.key, f.fixed, f.outcome && f.outcome.outcome]), d.running, d.status,
        d.uncommitted, d.error, d.submit_blocked, d.send_blocked, d.guide && d.guide.stage_dir,
        d.evidence.map((g) => g.artifacts.map((a) => [a.url, a.size])), d.commits.map((c) => c.stage_dir),
        d.github && [d.github.last_sync, d.github.blocked, d.github.items.map((i) => [i.id, i.body, i.sent_round, i.fixed_sha, i.replied_at])]]);
      if (!force && sig === S.sig) return;
      if (!force && S.editing) return; // an open editor keeps its text; the next poll catches up
      S.sig = sig;
      S.data = d;
      const selectable = new Set(d.findings.filter((f) => f.selectable).map((f) => f.key));
      for (const k of [...S.selected]) if (!selectable.has(k)) S.selected.delete(k);
      const waiting = new Set(d.github ? d.github.items.filter((i) => i.sent_round == null).map((i) => i.id) : []);
      for (const k of [...S.ghSelected]) if (!waiting.has(k)) S.ghSelected.delete(k);
      render();
    }

    function schedule() {
      clearTimeout(S.timer);
      if (!S.alive) return;
      S.timer = setTimeout(async () => {
        if (document.visibilityState === "visible") await load(false);
        schedule();
      }, S.data && S.data.running ? 10000 : 60000);
    }

    function setRange(spec) {
      S.range = spec;
      S.pin = null;
      S.collapsed.clear();
      history.replaceState(null, "", hash(S.target, S.id, null, spec));
      load(true);
    }

    async function post(payload) {
      try {
        const out = await ctx.api(`${base}/review`, payload);
        await load(true);
        return out;
      } catch (e) {
        ctx.toast(e.message, true);
        return null;
      }
    }

    async function requestChanges() {
      const d = S.data;
      const next = (d.rounds.reduce((m, r) => Math.max(m, r.round), 0) || 0) + 1;
      const ok = await ask(ctx, `${d.drafts} yorum tur ${next} olarak gönderilsin mi?\n`
        + `Plana "Review (user, round ${next})" bölümü eklenir ve yeni bir koşu başlar; sonra review adımı yorumların karşılandığını kontrol eder.\n`
        + "Ajanlar çalışır ve para harcar.");
      if (!ok) return;
      try {
        await ctx.api("/api/action", { target: S.target, id: S.id, action: "request_changes" });
        ctx.toast(`${S.id}: ${d.drafts} yorum gönderildi, yeni koşu başlıyor`);
      } catch (e) {
        ctx.toast(`${S.id}: ${e.message}`, true);
      }
      load(true);
    }

    async function fixSelected() {
      const d = S.data;
      const keys = [...S.selected];
      const others = d.drafts ? ` ve diğer ${d.drafts} taslağın da gönderilsin` : "";
      const ok = await ask(ctx, `Uygulayıcıdan ${keys.length} bulguyu düzeltmesi istensin mi${others}?\n`
        + "Her bulgu için \"düzelt\" yanıtı yazılır ve yeni bir koşu başlar; uygulayıcı her biri için ne yaptığını bildirir, review adımı kontrol eder.\n"
        + "Ajanlar çalışır ve para harcar.");
      if (!ok) return;
      try {
        const out = await ctx.api(`${base}/review`, { op: "findings.fix", ids: keys });
        S.selected.clear();
        await ctx.api("/api/action", { target: S.target, id: S.id, action: "request_changes" });
        ctx.toast(`${S.id}: ${out.message}; yeni koşu başlıyor`);
      } catch (e) {
        ctx.toast(`${S.id}: ${e.message}`, true);
      }
      load(true);
    }

    // PR feedback the user picked goes back like the review page's comments: a `## Review (github, round N)` plan
    // section and a new run; when it ends done the branch is pushed so the draft PR updates.
    async function fixGithub() {
      const g = S.data.github;
      const ids = [...S.ghSelected];
      const ok = await ask(ctx, `GitHub'dan ${ids.length} geri bildirim uygulayıcıya gönderilsin mi?\n`
        + "Plana \"Review (github, round N)\" bölümü eklenir ve yeni bir koşu başlar; koşu bitince dal push edilir ve draft PR güncellenir"
        + (g.replies ? ", GitHub'daki yorumlara \"Fixed in …\" yanıtı yazılır.\n" : ".\n")
        + "Ajanlar çalışır ve para harcar.");
      if (!ok) return;
      try {
        await ctx.api("/api/action", { target: S.target, id: S.id, action: "request_changes", github: ids });
        S.ghSelected.clear();
        ctx.toast(`${S.id}: ${ids.length} geri bildirim gönderildi, yeni koşu başlıyor`);
      } catch (e) {
        ctx.toast(`${S.id}: ${e.message}`, true);
      }
      load(true);
    }

    async function refreshGithub(btn) {
      btn.disabled = true;
      try {
        const out = await ctx.api(`${base}/github`, {});
        ctx.toast(out.new ? `${S.id}: GitHub'dan ${out.new} yeni geri bildirim` : `${S.id}: GitHub'da yeni bir şey yok`);
      } catch (e) {
        ctx.toast(`${S.id}: ${e.message}`, true);
      }
      btn.disabled = false;
      load(true);
    }

    function toggleSelected(x, on) {
      if (on) S.selected.add(x.key);
      else S.selected.delete(x.key);
      const fresh = topBar(S.data); // only the counts change; the rest of the page (and open editors) stays
      S.top.replaceWith(fresh);
      S.top = fresh;
    }

    async function approve() {
      const d = S.data;
      const warn = d.drafts ? `${d.drafts} taslak yorum gönderilmeden kalır (taslak olarak durur).` : "";
      if (d.pr) { // the PR exists: push the new commits, its title and description stay as they are
        if (!(await ask(ctx, `${S.id}: ${d.branch} dalındaki yeni commit'ler push edilip PR güncellensin mi?\nReady yapmak ve merge sende kalır.${warn ? `\n\n${warn}` : ""}`))) return;
        try {
          await ctx.api("/api/action", { target: S.target, id: S.id, action: "publish" });
          ctx.toast(`${S.id}: publish başladı`);
        } catch (e) {
          ctx.toast(`${S.id}: ${e.message}`, true);
        }
        load(true);
        return;
      }
      let draft;
      try {
        draft = await ctx.api(`${base}/pr-draft`);
      } catch (e) {
        ctx.toast(`${S.id}: ${e.message}`, true);
        return;
      }
      if (S.alive) prForm(d, draft, warn);
    }

    // The PR draft, editable before publish runs; the body is used word for word (publish appends the ticket line).
    function prForm(d, draft, warn) {
      const title = h("input", { type: "text", maxlength: String(MAX_PR_TITLE), class: "rv-pr-title" });
      title.value = draft.title || "";
      const body = h("textarea", { rows: "18", maxlength: String(MAX_PR_BODY), class: "rv-pr-body" });
      body.value = draft.body || "";
      const close = () => {
        document.removeEventListener("keydown", onKey);
        S.editing = Math.max(0, S.editing - 1);
        S.closeModal = null;
        modal.remove();
      };
      const onKey = (e) => { if (e.key === "Escape") { e.preventDefault(); close(); } };
      const send = async () => {
        const t = title.value.trim();
        if (!t) {
          ctx.toast("PR başlığı boş olamaz", true);
          title.focus();
          return;
        }
        sendBtn.disabled = true;
        try {
          await ctx.api("/api/action", { target: S.target, id: S.id, action: "publish", title: t, body: body.value });
          ctx.toast(`${S.id}: publish başladı`);
          close();
          load(true);
        } catch (e) {
          sendBtn.disabled = false;
          ctx.toast(`${S.id}: ${e.message}`, true);
        }
      };
      const sendBtn = h("button", { class: "primary", onclick: send }, "Push et, draft PR aç");
      const modal = h("div", { class: "modal", onclick: (e) => { if (e.target === modal) close(); } },
        h("div", { class: "modal-box wide rv-pr-form", role: "dialog", "aria-modal": "true", "aria-label": "Draft PR" },
          h("h3", {}, "Draft PR"),
          h("div", { class: "muted small" }, `${d.branch} dalı push edilir ve PR draft olarak açılır; ready yapmak ve merge sende kalır. `
            + "Metin plandan, reviewer rehberinden ve kanıttan derlenen bir taslak; olduğu gibi kullanılır, sonuna ticket satırı eklenir."),
          warn ? h("div", { class: "rv-banner warn" }, warn) : "",
          draft.protected_pending?.length ? h("div", { class: "rv-banner warn" }, "Onay bekleyen korumalı dosya değişikliği var "
            + `(${draft.protected_pending.join(", ")}): PR onsuz açılır; uygularsan sonra 'PR'ı güncelle' ile gönderirsin.`) : "",
          h("label", { class: "rv-field" }, h("span", { class: "small muted" }, "Başlık"), title),
          h("label", { class: "rv-field" }, h("span", { class: "small muted" }, "Açıklama (Markdown)"), body),
          h("div", { class: "row end" }, h("button", { class: "ghost", onclick: close }, "Vazgeç"), sendBtn)));
      S.closeModal = close; // leaving the page closes the form too
      document.body.append(modal);
      document.addEventListener("keydown", onKey);
      S.editing++;
      requestAnimationFrame(() => title.focus());
    }

    // --- layout pieces ----------------------------------------------------------------------------

    function render() {
      const d = S.data;
      if (S.observer) S.observer.disconnect();
      S.observer = new IntersectionObserver((entries) => {
        for (const e of entries) if (e.isIntersecting && e.target.rvLoad) { S.observer.unobserve(e.target); e.target.rvLoad(); }
      }, { rootMargin: "800px" });
      const files = treeOrder(d.files);
      const layout = window.matchMedia("(max-width: 900px)").matches ? "unified" : S.layout;
      const findings = visibleFindings(d);
      S.top = topBar(d);
      const page = h("div", { class: "rv-page" },
        S.top, metaLine(d), toolbar(d, layout), filterBox(d, findings), banners(d),
        d.error ? h("div", { class: "banner bad" }, d.error) : "",
        h("div", { class: "rv-body" },
          h("nav", { class: "rv-tree" }, tree(files)),
          h("div", { class: "rv-files" },
            githubCard(d), guideCard(d), evidenceCard(d), dismissedCard(d),
            files.length ? files.map((f) => fileSection(f, layout, findings, false)) : h("div", { class: "empty" }, "Bu aralıkta değişiklik yok."),
            offDiffFindings(findings),
            unverifiedCard(d),
            roundsCard(d))));
      container.replaceChildren(page);
      if (S.showDismissed) {
        S.showDismissed = false;
        const el = container.querySelector("#rv-dismissed");
        if (el) el.scrollIntoView({ block: "start" });
      } else if (S.showGithub) {
        S.showGithub = false;
        const el = container.querySelector("#rv-github");
        if (el) el.scrollIntoView({ block: "start" });
      } else if (S.file && !S.scrolled) {
        S.scrolled = true;
        const el = container.querySelector(`[data-path="${CSS.escape(S.file)}"]`);
        if (el) el.scrollIntoView({ block: "start" });
      }
    }

    function topBar(d) {
      const back = () => (history.length > 1 ? history.back() : ctx.navigate("#"));
      const important = d.findings.filter((f) => f.open_important);
      const n = S.selected.size;
      return h("div", { class: "rv-top" + (embedded ? " embedded" : "") },
        embedded ? "" : h("button", { class: "ghost small", onclick: back }, "← Geri"),
        embedded ? "" : h("strong", { class: "mono" }, d.ticket_id),
        embedded ? "" : h("span", { class: "pill" }, d.status || "-"),
        h("span", { class: "grow rv-ellipsis" }, embedded ? "" : d.title || ""),
        d.drafts ? h("span", { class: "pill info" }, `${d.drafts} taslak`) : "",
        !n && important.length ? h("button", { class: "small", title: "Son turun hâlâ açık, yanıtlanmamış önemli bulgularını seç",
          onclick: () => { for (const f of important) S.selected.add(f.key); render(); } }, `Açık önemlileri seç (${important.length})`) : "",
        n ? h("button", { class: "ghost small", onclick: () => { S.selected.clear(); render(); } }, "Seçimi temizle") : "",
        n ? h("button", { class: "danger small", disabled: !!d.send_blocked,
          title: d.send_blocked || "Seçilen bulgulara \"düzelt\" yanıtı yazılır ve değişiklik istenir", onclick: fixSelected },
        `Seçilenleri düzelt (${n})`) : "",
        h("button", { class: "small", disabled: !!d.submit_blocked, title: d.submit_blocked || "", onclick: requestChanges },
          `Değişiklik iste (${d.drafts})`),
        h("button", { class: "primary small", disabled: !d.approve, title: d.approve ? "" : "Ticket bitmiş olmalı ve koşu sürmemeli",
          onclick: approve }, d.pr ? "Onayla → PR'ı güncelle" : "Onayla → Draft PR"));
    }

    function metaLine(d) {
      const last = d.commits[0];
      return h("div", { class: "rv-meta muted small" },
        h("span", { class: "mono" }, d.branch || "-"), ` · ${d.base} tabanlı`,
        d.base_commit && d.head ? h("span", { class: "mono" }, ` · ${short(d.base_commit)}..${d.head.short}`) : "",
        " · ", d.commits.length ? h("details", { class: "rv-commits" }, h("summary", {}, `${d.commits.length} commit`),
          commitList(d, ctx, S.target, S.id, (c) => setRange(`commit:${c.hash}`))) : "0 commit",
        last ? ` · son commit ${ago(last.at)}` : "");
    }

    // What people and CI said on the PR (the panel's PR sync reads it). Every text is someone else's: text nodes only.
    function githubCard(d) {
      const g = d.github;
      if (!g) return "";
      const n = S.ghSelected.size;
      const waiting = g.items.filter((i) => i.sent_round == null);
      const sent = g.items.filter((i) => i.sent_round != null);
      const refresh = h("button", { class: "ghost small", title: "PR'ı şimdi GitHub'dan oku", onclick: () => refreshGithub(refresh) }, "Şimdi yenile");
      const box = h("section", { class: "rv-card rv-github", id: "rv-github" },
        h("div", { class: "row" },
          h("h3", { class: "grow" }, "GitHub geri bildirimi",
            h("span", { class: "muted small" }, ` · ${waiting.length} bekliyor${sent.length ? `, ${sent.length} gönderildi` : ""}`)),
          h("span", { class: "muted small" }, g.last_sync ? `son okuma ${ago(g.last_sync)}` : "henüz okunmadı"),
          refresh,
          waiting.length ? h("button", { class: "ghost small", onclick: () => {
            if (n === waiting.length) S.ghSelected.clear(); else for (const i of waiting) S.ghSelected.add(i.id);
            render();
          } }, n === waiting.length ? "Seçimi temizle" : "Hepsini seç") : "",
          h("button", { class: "danger small", disabled: !n || !!g.blocked,
            title: g.blocked || (n ? "Seçilenler plana yazılır ve yeni bir koşu başlar" : "Önce geri bildirim seç"), onclick: fixGithub },
          `Seçilenleri düzelt (${n})`)),
        h("div", { class: "muted small" }, "PR'daki inceleme yorumları, sohbet yorumları ve kırmızı CI kontrolleri. Metinler başkalarının; "
          + "ajana veri olarak (talimat değil) gider. Bot yorumları atlanır (pr_feedback_bots hariç)."),
        g.items.length ? h("div", { class: "rv-gh-list" }, [...waiting, ...sent.slice().reverse()].map(githubItem))
          : h("div", { class: "empty small" }, "PR'da henüz yorum ya da kırmızı kontrol yok."));
      return box;
    }

    const GH_KIND = { review_comment: "satır yorumu", issue_comment: "yorum", review: "review", ci: "CI" };
    const safeUrl = (u) => (/^https:\/\//.test(u || "") ? u : null);

    function githubItem(i) {
      const d = S.data;
      const open = i.sent_round == null;
      const where = i.path ? `${i.path}${i.line ? `:${i.line}` : ""}` : "";
      const inDiff = i.path && d.files.some((f) => f.path === i.path);
      const url = safeUrl(i.url);
      return h("div", { class: "rv-finding rv-gh" + (open ? "" : " fixed") + (i.kind === "ci" ? " ci" : "") },
        h("div", { class: "row small" },
          open ? h("label", { class: "rv-check", title: "\"Seçilenleri düzelt\" için seç" },
            h("input", { type: "checkbox", checked: S.ghSelected.has(i.id), "aria-label": `${i.id} seç`, onchange: (e) => {
              if (e.target.checked) S.ghSelected.add(i.id); else S.ghSelected.delete(i.id);
              render();
            } })) : "",
          h("span", { class: `pill ${i.kind === "ci" ? "bad" : "info"}` }, GH_KIND[i.kind] || i.kind),
          h("strong", {}, i.kind === "ci" ? i.author : `@${i.author}`),
          where ? (inDiff ? h("button", { class: "link-btn mono", title: "Dosyaya git", onclick: () => scrollTo(i.path) }, where)
            : h("span", { class: "mono" }, where)) : "",
          i.kind === "review" && i.state ? h("span", { class: "pill mute" }, i.state.toLowerCase()) : "",
          h("span", { class: "muted" }, ago(i.created_at)),
          i.sent_round != null ? h("span", { class: "pill mute" }, `tur ${i.sent_round}'de gönderildi`) : "",
          i.fixed_sha ? h("span", { class: "pill ok", title: i.fixed_sha }, `düzeltildi ${short(i.fixed_sha)}`) : "",
          i.replied_at ? h("span", { class: "pill ok", title: i.replied_at }, "GitHub'da yanıtlandı") : "",
          h("span", { class: "grow" }),
          url ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer", class: "small" }, "GitHub'da aç") : ""),
        i.kind === "ci" ? ciLog(i) : h("div", { class: "rv-comment-body" }, i.body));
    }

    // A re-render (a checkbox, the poll) keeps the logs the user opened open.
    function ciLog(i) {
      const box = h("details", { class: "small" }, h("summary", {}, `Başarısız log (son ${i.body.split("\n").length} satır)`),
        h("pre", { class: "rv-gh-log" }, i.body));
      box.open = S.ghLogs.has(i.id);
      box.addEventListener("toggle", () => { if (box.open) S.ghLogs.add(i.id); else S.ghLogs.delete(i.id); });
      return box;
    }

    function guideCard(d) {
      const g = d.guide;
      if (!g || !g.entries.length) return "";
      const box = h("details", { class: "rv-card rv-guide" },
        h("summary", {}, h("strong", {}, "Reviewer rehberi"), h("span", { class: "muted" }, ` · ${g.entries.length} durak, okuma sırasıyla`)),
        g.summary ? h("p", { class: "rv-guide-summary" }, g.summary) : "",
        h("ol", { class: "rv-guide-list" }, g.entries.map((e) => h("li", {},
          e.path ? h("button", { class: "link-btn mono", title: "Dosyaya git", onclick: () => scrollTo(e.path) }, e.file)
            : h("span", { class: "mono muted", title: "Bu aralıktaki değişen dosyalarda yok" }, e.file),
          e.summary ? h("div", {}, e.summary) : "",
          e.how_to_verify ? h("div", { class: "muted small" }, h("strong", {}, "Nasıl doğrulanır: "), e.how_to_verify) : ""))));
      box.open = localStorage.getItem(LS_GUIDE) !== "closed";
      box.addEventListener("toggle", () => localStorage.setItem(LS_GUIDE, box.open ? "open" : "closed"));
      return box;
    }

    function evidenceCard(d) {
      if (!d.evidence.length) return "";
      const n = d.evidence.reduce((m, g) => m + g.artifacts.length, 0);
      const box = h("details", { class: "rv-card rv-evidence" },
        h("summary", {}, h("strong", {}, "Kanıt"), h("span", { class: "muted" }, ` · ${plural(n, "dosya")}`)),
        evidenceView(d.evidence, ctx, S.target, S.id));
      box.open = S.evidenceOpen !== false;
      box.addEventListener("toggle", () => { S.evidenceOpen = box.open; });
      return box;
    }

    function unverifiedCard(d) {
      if (!d.unverified.length) return "";
      return h("details", { class: "rv-card" },
        h("summary", {}, `Doğrulanamayan adaylar (${d.unverified.length})`),
        h("div", { class: "muted small" }, "Son review'un doğrulama adımı bunları teyit edemedi; bulgu sayılmadılar, kayıt için listeleniyor."),
        h("ul", { class: "rv-list" }, d.unverified.map((u) => h("li", { class: "small" },
          u.location ? h("span", { class: "mono" }, `${u.location} — `) : "", u.summary,
          u.reason ? h("span", { class: "muted" }, ` (${u.reason})`) : ""))));
    }

    function rangeSelect(d) {
      const sel = h("select", { class: "rv-range", onchange: (e) => setRange(e.target.value) });
      sel.append(h("option", { value: "all" }, `Tüm değişiklikler (${d.commits.length} commit)`));
      const seen = new Set([d.head && d.head.hash]);
      const since = d.points.slice().reverse().filter((p) => !seen.has(p.head) && seen.add(p.head));
      const group = h("optgroup", { label: "Bir review'dan beri" },
        since.map((p) => h("option", { value: p.spec }, `${p.label} sonrası (${p.head.slice(0, 7)})`)));
      if (d.range.kind === "since" && !since.some((p) => p.spec === d.range.spec)) group.append(h("option", { value: d.range.spec }, d.range.label));
      if (group.children.length) sel.append(group);
      if (d.commits.length) sel.append(h("optgroup", { label: "Tek commit" },
        d.commits.map((c) => h("option", { value: `commit:${c.hash}` }, `${c.short} ${c.subject.slice(0, 70)}`))));
      sel.value = d.range.spec;
      return sel;
    }

    function toolbar(d, layout) {
      const viewed = d.files.filter((f) => f.viewed === "viewed").length;
      const setLayout = (l) => { S.layout = l; localStorage.setItem(LS_LAYOUT, l); render(); };
      const allCollapsed = (v) => { for (const f of d.files) S.collapsed.set(f.path, v); render(); };
      return h("div", { class: "rv-toolbar row" },
        rangeSelect(d),
        h("span", {}, plural(d.totals.files, "dosya"), " ", h("span", { class: "rv-add" }, `+${d.totals.additions}`), " ",
          h("span", { class: "rv-del" }, `−${d.totals.deletions}`)),
        h("span", { class: "muted" }, `görüldü ${viewed}/${d.files.length}`),
        h("span", { class: "grow" }),
        h("div", { class: "rv-seg" },
          h("button", { class: "small" + (layout === "unified" ? " on" : ""), onclick: () => setLayout("unified") }, "Tek sütun"),
          h("button", { class: "small" + (layout === "split" ? " on" : ""), onclick: () => setLayout("split") }, "Yan yana")),
        h("button", { class: "ghost small", onclick: () => allCollapsed(true) }, "Tümünü daralt"),
        h("button", { class: "ghost small", onclick: () => allCollapsed(false) }, "Tümünü aç"));
    }

    function visibleFindings(d) {
      const f = S.filter;
      if (!f.show) return [];
      return d.findings.filter((x) => (!f.latestOnly || x.latest) && (!f.hideFixed || !x.fixed)
        && f.severities.includes(SEVERITIES.includes(x.severity) ? x.severity : "medium")
        && f.dispositions.includes(DISPOSITIONS.includes(x.disposition) ? x.disposition : "fix")
        && f.classes.includes(clsOf(x)));
    }

    function filterBox(d, shown) {
      if (!d.findings.length) return "";
      const f = S.filter;
      const save = () => { localStorage.setItem(LS_FILTER, JSON.stringify(f)); render(); };
      const check = (label, on, set) => h("label", { class: "rv-check" },
        h("input", { type: "checkbox", checked: on, onchange: (e) => { set(e.target.checked); save(); } }), label);
      const toggle = (list, v) => (on) => { const i = list.indexOf(v); if (on && i < 0) list.push(v); if (!on && i >= 0) list.splice(i, 1); };
      const box = h("details", { class: "rv-filter" },
        h("summary", {}, `AI review bulguları — ${d.findings.length} bulgudan ${shown.length} gösteriliyor`),
        h("div", { class: "row small" },
          check("göster", f.show, (v) => { f.show = v; }),
          check("yalnız son tur", f.latestOnly, (v) => { f.latestOnly = v; }),
          check("düzeltilenleri gizle", f.hideFixed, (v) => { f.hideFixed = v; })),
        h("div", { class: "row small" }, h("span", { class: "muted" }, "Sınıf:"),
          CLASSES.map((c) => check(CLASS_TR[c], f.classes.includes(c), toggle(f.classes, c)))),
        h("div", { class: "row small" }, h("span", { class: "muted" }, "Önem:"),
          SEVERITIES.map((s) => check(SEV_TR[s], f.severities.includes(s), toggle(f.severities, s)))),
        h("div", { class: "row small" }, h("span", { class: "muted" }, "Karar:"),
          DISPOSITIONS.map((s) => check(DISP_TR[s], f.dispositions.includes(s), toggle(f.dispositions, s)))));
      box.open = !!S.filterOpen;
      box.addEventListener("toggle", () => { S.filterOpen = box.open; });
      return box;
    }

    function banners(d) {
      const out = [];
      if (d.new_commits && d.range.kind !== "commit") out.push(h("div", { class: "rv-banner info row" },
        `Bu sayfa açıldığından beri dalda ${d.new_commits} yeni commit var.`,
        h("button", { class: "small", onclick: () => { S.pin = null; load(true); } }, d.new_commits === 1 ? "Onu göster" : "Onları göster")));
      if (d.running) out.push(h("div", { class: "rv-banner info" },
        `Bu ticket'ta koşu sürüyor${d.stage ? ` (${d.stage})` : ""}. Taslak yazabilirsin; değişiklik isteme koşu bitince açılır.`));
      if (d.uncommitted.length) {
        const tracked = d.uncommitted.filter((u) => u.status !== "untracked");
        const untracked = d.uncommitted.filter((u) => u.status === "untracked");
        out.push(h("div", { class: "rv-banner warn" },
          h("div", { class: "row" }, `Worktree'de ${d.uncommitted.length} commit'lenmemiş dosya var. PR'a girmezler.`,
            h("button", { class: "small", onclick: () => { S.showUncommitted = !S.showUncommitted; render(); } },
              S.showUncommitted ? "Gizle" : "Göster")),
          S.showUncommitted && d.head ? h("div", { class: "rv-uncommitted" },
            tracked.map((u) => fileSection({ path: u.path, old_path: null, status: "modified", additions: null, deletions: null,
              binary: false, image: false, generated: false, from: d.head.hash, to: "worktree", uncommitted: u.status }, "unified", [], true)),
            untracked.length ? h("ul", { class: "rv-list small" }, untracked.map((u) => h("li", { class: "mono" }, `${u.status} · ${u.path}`))) : "") : ""));
      }
      return out;
    }

    // --- file tree ------------------------------------------------------------------------------

    function buildTree(files) {
      const root = { dirs: new Map(), files: [] };
      for (const f of files) {
        const parts = f.path.split("/");
        let node = root;
        for (const p of parts.slice(0, -1)) {
          if (!node.dirs.has(p)) node.dirs.set(p, { dirs: new Map(), files: [] });
          node = node.dirs.get(p);
        }
        node.files.push(f);
      }
      return root;
    }

    function treeOrder(files) {
      const out = [];
      const walk = (node) => {
        for (const k of [...node.dirs.keys()].sort()) walk(node.dirs.get(k));
        out.push(...node.files.slice().sort((a, b) => a.path.localeCompare(b.path)));
      };
      walk(buildTree(files));
      return out;
    }

    function tree(files) {
      const walk = (node, depth) => {
        const out = [];
        for (const name of [...node.dirs.keys()].sort()) {
          let label = name, child = node.dirs.get(name);
          while (child.files.length === 0 && child.dirs.size === 1) { // merge single-child chains
            const [k, v] = [...child.dirs.entries()][0];
            label += "/" + k;
            child = v;
          }
          out.push(h("div", { class: "rv-tdir", style: `padding-left:${depth * 12}px` }, label + "/"), ...walk(child, depth + 1));
        }
        for (const f of node.files.slice().sort((a, b) => a.path.localeCompare(b.path))) {
          const mark = f.viewed === "viewed" ? "✓" : f.viewed === "changed" ? "•" : "";
          out.push(h("a", { class: `rv-tfile rv-v-${f.viewed}`, href: hash(S.target, S.id, f.path, S.range), style: `padding-left:${depth * 12}px`,
            title: f.path, onclick: (e) => { e.preventDefault(); scrollTo(f.path); } },
          h("span", { class: `rv-st rv-st-${f.status}` }, STATUS_LETTER[f.status] || "M"),
          h("span", { class: "rv-ellipsis" }, f.path.slice(f.path.lastIndexOf("/") + 1)),
          h("span", { class: "rv-mark" }, mark)));
        }
        return out;
      };
      return walk(buildTree(files), 0);
    }

    function scrollTo(path) {
      S.collapsed.set(path, false);
      const el = container.querySelector(`.rv-file[data-path="${CSS.escape(path)}"]`);
      if (!el) return;
      if (el.classList.contains("collapsed")) render();
      const again = container.querySelector(`.rv-file[data-path="${CSS.escape(path)}"]`);
      if (again) again.scrollIntoView({ block: "start", behavior: "smooth" });
      history.replaceState(null, "", hash(S.target, S.id, path, S.range));
    }

    // --- file section -----------------------------------------------------------------------------

    function isCollapsed(f) {
      if (S.collapsed.has(f.path)) return S.collapsed.get(f.path);
      if (f.path === S.file) return false;
      return f.generated || (f.additions || 0) + (f.deletions || 0) > 1500 || f.viewed === "viewed";
    }

    function fileSection(f, layout, findings, uncommitted) {
      const d = S.data;
      const collapsed = !uncommitted && isCollapsed(f);
      const from = uncommitted ? f.from : d.range.from;
      const to = uncommitted ? f.to : d.range.to;
      const body = h("div", { class: "rv-file-body" });
      const toggle = () => { S.collapsed.set(f.path, !collapsed); render(); };
      const viewedBox = uncommitted ? "" : h("label", { class: "rv-check small" },
        h("input", { type: "checkbox", checked: f.viewed === "viewed", onchange: async (e) => {
          const on = e.target.checked;
          S.collapsed.set(f.path, on);
          await post({ op: "viewed.set", path: f.path, blob: on ? blobKey(f) : null });
        } }), "Görüldü", f.viewed === "changed" ? h("span", { class: "pill warn" }, "değişti") : "");
      const section = h("section", { class: "rv-file" + (collapsed ? " collapsed" : ""), "data-path": uncommitted ? null : f.path },
        h("header", { class: "rv-file-head row" },
          h("button", { class: "ghost small", onclick: toggle, "aria-expanded": String(!collapsed) }, collapsed ? "▸" : "▾"),
          h("span", { class: `rv-st rv-st-${f.status}` }, uncommitted ? f.uncommitted : STATUS_LETTER[f.status] || "M"),
          h("span", { class: "mono grow rv-ellipsis" }, f.old_path ? `${f.old_path} → ${f.path}` : f.path),
          f.generated ? h("span", { class: "pill mute" }, "üretilmiş") : "",
          f.additions !== null && f.additions !== undefined ? h("span", { class: "small" },
            h("span", { class: "rv-add" }, `+${f.additions}`), " ", h("span", { class: "rv-del" }, `−${f.deletions}`)) : "",
          !f.binary && !collapsed ? h("button", { class: "ghost small", onclick: () => {
            if (S.full.has(f.path)) S.full.delete(f.path); else S.full.add(f.path);
            render();
          } }, S.full.has(f.path) ? "Yalnız değişenler" : "Tüm dosya") : "",
          viewedBox),
        collapsed ? "" : body);
      if (collapsed) return section;
      if (f.image && !uncommitted) {
        body.append(images(f));
        return section;
      }
      if (f.binary) {
        body.append(h("div", { class: "muted small rv-note" }, "Binary dosya; metin farkı yok."));
        return section;
      }
      body.append(h("div", { class: "muted small rv-note" }, "Fark yükleniyor…"));
      body.rvLoad = async () => {
        try {
          const diff = await fetchDiff(f, from, to, S.full.has(f.path));
          if (!S.alive) return;
          body.replaceChildren(diffView(f, diff, layout, uncommitted ? [] : findings.filter((x) => x.path === f.path), uncommitted));
        } catch (e) {
          body.replaceChildren(h("div", { class: "banner bad" }, e.message));
        }
      };
      S.observer.observe(body);
      return section;
    }

    function fetchDiff(f, from, to, full) {
      const q = new URLSearchParams({ path: f.path, from, to });
      if (f.old_path) q.set("old_path", f.old_path);
      if (full) q.set("context", "all");
      const url = `${base}/diff?${q}`;
      if (to !== "worktree" && S.diffs.has(url)) return S.diffs.get(url);
      const p = ctx.api(url).then((diff) => { highlightDiff(diff, grammarFor(f.path)); return diff; });
      if (to !== "worktree") {
        S.diffs.set(url, p);
        p.catch(() => S.diffs.delete(url));
        if (S.diffs.size > 200) S.diffs.delete(S.diffs.keys().next().value);
      }
      return p;
    }

    function images(f) {
      const d = S.data;
      const src = (rev, path) => `${base}/blob?${new URLSearchParams({ rev, path })}`;
      const fig = (cap, url) => h("figure", { class: "rv-img" }, h("img", { src: url, loading: "lazy", alt: cap }), h("figcaption", {}, cap));
      return h("div", { class: "row rv-images" },
        f.status !== "added" ? fig("Önce", src(d.range.from, f.old_path || f.path)) : "",
        f.status !== "deleted" ? fig(f.status === "added" ? "Eklendi" : "Sonra", src(d.range.to, f.path)) : "");
    }

    // --- diff table -------------------------------------------------------------------------------

    function diffView(f, diff, layout, findings, uncommitted) {
      const d = S.data;
      const wrap = h("div", { class: "rv-diff" });
      if (diff.binary) return h("div", { class: "muted small rv-note" }, "Binary dosya; metin farkı yok.");
      // Anchors: line comments while their text still matches the line, findings at the last line of their range.
      const anchors = new Map();
      const add = (k, node) => { if (!anchors.has(k)) anchors.set(k, []); anchors.get(k).push(node); };
      const texts = new Map();
      for (const hk of diff.hunks) for (const l of hk.lines) {
        if (l.n !== null && l.t !== "del") texts.set(`new:${l.n}`, l.s);
        if (l.t === "del") texts.set(`old:${l.o}`, l.s);
      }
      const top = [];
      if (!uncommitted) {
        for (const c of d.comments.filter((c) => c.path === f.path && !c.finding_id)) {
          const k = `${c.side}:${c.line}`;
          if (c.line && texts.has(k) && (c.line_text === null || texts.get(k) === c.line_text)) add(k, commentView(c));
          else top.push(h("div", { class: "rv-outdated" },
            h("div", { class: "muted small" }, c.line ? `eski satır ${c.line} · ${c.line_text || ""}` : "dosya yorumu"), commentView(c)));
        }
        for (const x of findings) {
          const k = `new:${x.end_line || x.line}`;
          if (x.line && texts.has(k)) add(k, findingCard(x));
          else top.push(findingCard(x));
        }
      }
      if (top.length) wrap.append(h("div", { class: "rv-top-notes" }, h("div", { class: "muted small" }, "Bu diffte satırı olmayanlar"), top));
      if (diff.truncated) wrap.append(h("div", { class: "rv-banner warn" }, "Fark çok uzun; ilk 20000 satır gösteriliyor."));
      const table = h("table", { class: `rv-table rv-${layout}` });
      const cols = layout === "split" ? 6 : 4;
      // table-layout: fixed takes column widths from the first row, which is a colspan hunk header;
      // a colgroup gives the narrow number and "+" columns their width up front.
      const col = (cls) => h("col", { class: cls });
      table.append(h("colgroup", {}, ...(layout === "split"
        ? [col("c-num"), col("c-plus"), col("c-code"), col("c-num"), col("c-plus"), col("c-code")]
        : [col("c-num"), col("c-num"), col("c-plus"), col("c-code")])));
      const extra = (keys) => {
        const nodes = keys.flatMap((k) => anchors.get(k) || []);
        return nodes.length ? h("tr", { class: "rv-anchor" }, h("td", { colspan: String(cols) }, nodes)) : null;
      };
      const plus = (l, side) => uncommitted || l.t === "note" ? h("td", { class: "rv-plus" })
        : h("td", { class: "rv-plus" }, h("button", { class: "rv-plus-btn", title: "Yorum ekle",
          onclick: (e) => openEditor(e.target.closest("tr"), cols, {
            path: f.path, line: side === "old" ? l.o : l.n, side, line_text: l.s, commit_sha: d.range.to,
          }) }, "+"));
      for (const hk of diff.hunks) {
        table.append(h("tr", { class: "rv-hunk" }, h("td", { colspan: String(cols) }, hk.header)));
        if (layout === "split") {
          for (const [left, right] of splitRows(hk.lines)) {
            table.append(h("tr", { class: "rv-line" },
              left ? h("td", { class: "rv-num" }, String(left.o ?? "")) : h("td", { class: "rv-num" }),
              left ? plus(left, left.t === "del" ? "old" : "new") : h("td", {}),
              left ? cell(left, left.t === "ctx" ? "" : left.t) : h("td", { class: "rv-code rv-empty" }),
              right ? h("td", { class: "rv-num" }, String(right.n ?? "")) : h("td", { class: "rv-num" }),
              right ? plus(right, "new") : h("td", {}),
              right ? cell(right, right.t === "ctx" ? "" : right.t) : h("td", { class: "rv-code rv-empty" })));
            const keys = [];
            if (left && left.t === "del") keys.push(`old:${left.o}`);
            if (right && right.n !== null) keys.push(`new:${right.n}`);
            const row = extra(keys);
            if (row) table.append(row);
          }
        } else {
          for (const l of hk.lines) {
            table.append(h("tr", { class: `rv-line rv-${l.t}` },
              h("td", { class: "rv-num" }, l.o === null ? "" : String(l.o)),
              h("td", { class: "rv-num" }, l.n === null ? "" : String(l.n)),
              plus(l, l.t === "del" ? "old" : "new"),
              code(l)));
            const row = extra([l.t === "del" ? `old:${l.o}` : `new:${l.n}`]);
            if (row) table.append(row);
          }
        }
      }
      if (!diff.hunks.length) wrap.append(h("div", { class: "muted small rv-note" }, "İçerik farkı yok (yalnız ad ya da kip değişmiş)."));
      else wrap.append(table);
      return wrap;
    }

    function cell(l, kind) {
      const td = code(l);
      if (kind) td.classList.add(`rv-${kind}`);
      return td;
    }

    // A run of deletions pairs with the additions that follow it.
    function splitRows(lines) {
      const rows = [];
      let i = 0;
      while (i < lines.length) {
        const l = lines[i];
        if (l.t === "ctx") { rows.push([l, l]); i++; continue; }
        if (l.t === "note") { rows.push([l, null]); i++; continue; }
        const dels = [], adds = [];
        while (i < lines.length && lines[i].t === "del") dels.push(lines[i++]);
        while (i < lines.length && lines[i].t === "add") adds.push(lines[i++]);
        for (let k = 0; k < Math.max(dels.length, adds.length); k++) rows.push([dels[k] || null, adds[k] || null]);
      }
      return rows;
    }

    // --- comments and findings --------------------------------------------------------------------

    function openEditor(afterRow, cols, fields, initial, onSave) {
      const ta = h("textarea", { rows: "4", placeholder: "Yorumun (Cmd/Ctrl+Enter kaydeder, Esc vazgeçer)" });
      ta.value = initial || "";
      const close = () => { S.editing = Math.max(0, S.editing - 1); holder.remove(); };
      const save = async () => {
        const body = ta.value.trim();
        if (!body) return;
        S.editing = Math.max(0, S.editing - 1);
        const ok = onSave ? await onSave(body) : await post({ op: "comment.add", ...fields, body });
        if (!ok) S.editing++;
      };
      ta.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save(); }
        if (e.key === "Escape") { e.preventDefault(); close(); }
      });
      const box = h("div", { class: "rv-editor" }, ta, h("div", { class: "row end" },
        h("button", { class: "ghost small", onclick: close }, "Vazgeç"),
        h("button", { class: "primary small", onclick: save }, "Taslak kaydet")));
      const holder = afterRow && afterRow.tagName === "TR" ? h("tr", { class: "rv-anchor" }, h("td", { colspan: String(cols) }, box)) : box;
      if (afterRow) afterRow.after(holder);
      S.editing++;
      requestAnimationFrame(() => ta.focus()); // callers without a row insert the editor after this returns
      return holder;
    }

    function commentView(c) {
      const sent = c.round !== null && c.round !== undefined;
      const node = h("div", { class: "rv-comment" + (sent ? " sent" : " draft") },
        h("div", { class: "row small" },
          h("strong", {}, c.author || "sen"), h("span", { class: "muted" }, ago(c.updated_at || c.created_at)),
          sent ? h("span", { class: "pill ok" }, `tur ${c.round} ile gönderildi`) : h("span", { class: "pill info" }, "taslak"),
          c.finding_action ? h("span", { class: "pill warn" }, c.finding_action === "fix" ? "düzeltilsin" : "boşverilsin") : "",
          h("span", { class: "grow" }),
          sent ? "" : h("button", { class: "ghost small", onclick: () => {
            const holder = openEditor(null, 0, null, c.body, async (body) => !!(await post({ op: "comment.edit", id: c.id, body })));
            node.after(holder);
          } }, "Düzenle"),
          sent ? "" : h("button", { class: "ghost small", onclick: () => post({ op: "comment.delete", id: c.id }) }, "Sil")),
        c.body ? h("div", { class: "rv-comment-body" }, c.body) : "");
      return node;
    }

    function findingCard(x) {
      const d = S.data;
      const replies = d.comments.filter((c) => c.finding_id === x.id && (!c.finding_stage || c.finding_stage === x.stage_dir));
      const has = (a) => replies.some((c) => c.finding_action === a);
      const reply = (fields) => post({ op: "comment.add", finding_id: x.id, finding_stage: x.stage_dir, path: x.path, line: x.line,
        commit_sha: d.range.to, ...fields });
      const cls = clsOf(x);
      const out = x.outcome && !x.fixed ? x.outcome : null;
      const card = h("div", { class: "rv-finding" + (x.fixed ? " fixed" : "") },
        h("div", { class: "row small" },
          x.selectable ? h("label", { class: "rv-check", title: "\"Seçilenleri düzelt\" için seç" },
            h("input", { type: "checkbox", checked: S.selected.has(x.key), "aria-label": `${x.id} seç`,
              onchange: (e) => toggleSelected(x, e.target.checked) })) : "",
          h("span", { class: "pill info" }, "AI review"), h("span", { class: "mono" }, x.id),
          h("span", { class: `pill ${CLASS_PILL[cls]}`, title: CLASS_TITLE[cls] }, CLASS_TR[cls]),
          h("span", { class: `pill ${sevClass(x.severity)}` }, SEV_TR[x.severity] || x.severity),
          h("span", { class: "pill mute" }, x.kind), h("span", { class: "pill mute" }, DISP_TR[x.disposition] || x.disposition),
          x.confidence !== null && x.confidence !== undefined ? h("span", { class: "muted" }, confidenceText(x.confidence)) : "",
          x.verified === true ? h("span", { class: "pill ok", title: "Review konumu açıp sonucu bu diff üzerinde teyit etti" }, "doğrulandı") : "",
          x.verified === false ? h("span", { class: "pill warn", title: "Review bu bulguyu teyit etmedi" }, "doğrulanmadı") : "",
          out ? h("span", { class: `pill ${out.outcome === "fixed" ? "ok" : "mute"}`, title: out.note || "" },
            `uygulayıcı: ${OUTCOME_TR[out.outcome] || out.outcome}`) : "",
          x.fixed ? h("span", { class: "pill ok" }, "sonra düzeltildi") : "",
          h("span", { class: "muted" }, `tur ${x.round}`)),
        h("div", { class: "rv-finding-summary" }, x.summary),
        x.evidence || x.reason || x.location || (out && out.note) ? h("details", { class: "small" },
          h("summary", {}, x.verified ? "Neden işaretlendi (doğrulandı)" : "Ayrıntı"),
          x.location ? h("div", { class: "mono" }, x.location) : "",
          x.evidence ? h("div", { class: "rv-comment-body" }, x.evidence) : "",
          x.reason ? h("div", { class: "rv-comment-body muted" }, x.reason) : "",
          out && out.note ? h("div", { class: "rv-comment-body" }, h("strong", {}, "Uygulayıcı: "), out.note) : "") : "",
        replies.map(commentView));
      const actions = h("div", { class: "row" },
        h("button", { class: "ghost small", onclick: () => {
          const holder = openEditor(null, 0, null, "", async (body) => !!(await reply({ body })));
          actions.after(holder);
        } }, "Yanıtla"),
        (x.disposition !== "fix" || x.fixed) && !has("fix") ? h("button", { class: "small", onclick: () => reply({ body: "", finding_action: "fix" }) }, "Düzelt") : "",
        x.disposition === "fix" && !x.fixed && !has("dismiss") ? h("button", { class: "small", onclick: () => reply({ body: "", finding_action: "dismiss" }) }, "Boşver") : "");
      card.append(actions);
      return card;
    }

    // What the AI review dismissed stays in view: each card keeps its "Düzelt" reply and its "Seçilenleri düzelt" box.
    function dismissedCard(d) {
      const list = d.findings.filter((x) => x.dismissed);
      if (!list.length) return "";
      const box = h("details", { class: "rv-card", id: "rv-dismissed" },
        h("summary", {}, h("strong", {}, `Review ${list.length} bulguyu eledi`)),
        h("div", { class: "muted small" }, "AI review bunlara \"boşver\" dedi; gerekçesi her bulgunun Ayrıntı'sında. "
          + "Katılmadığını Düzelt ile ya da seçip \"Seçilenleri düzelt\" ile uygulayıcıya gönder."),
        list.map(findingCard));
      box.open = S.dismissedOpen;
      box.addEventListener("toggle", () => { S.dismissedOpen = box.open; });
      return box;
    }

    function offDiffFindings(findings) {
      const rest = findings.filter((x) => !x.path);
      if (!rest.length) return "";
      return h("section", { class: "rv-card" }, h("h3", {}, `Değişen bir dosyada olmayan bulgular (${rest.length})`), rest.map(findingCard));
    }

    function roundsCard(d) {
      if (!d.rounds.length) return "";
      return h("section", { class: "rv-card" }, h("h3", {}, "Senin review'ların"),
        h("ul", { class: "rv-list" }, d.rounds.slice().reverse().map((r) => {
          const n = pointFor(d, r);
          return h("li", { class: "row" },
            h("span", {}, `Tur ${r.round} · ${plural(r.comments, "yorum")} · ${r.by} · ${ago(r.at)}`),
            h("span", { class: "mono muted small" }, short(r.head)),
            n ? h("button", { class: "ghost small", onclick: () => setRange(`since:user:${n}`) }, "O zamandan beri") : "");
        })));
    }

    // The panel re-renders only when the ticket changes; file and range changes of this ticket land here.
    const onHash = () => {
      const p = parseHash(location.hash);
      if (!p || p.target !== S.target || p.id !== S.id || !S.data) return;
      if ((p.range || "all") !== S.range) { S.file = p.file; S.scrolled = false; setRange(p.range || "all"); return; }
      if (p.file && p.file !== S.file) { S.file = p.file; scrollTo(p.file); }
      if (p.show === "dismissed") { S.dismissedOpen = S.showDismissed = true; render(); }
      if (p.show === "github") { S.showGithub = true; render(); }
    };
    window.addEventListener("hashchange", onHash);
    load(true).then(schedule);
    return () => {
      S.alive = false;
      window.removeEventListener("hashchange", onHash);
      clearTimeout(S.timer);
      if (S.observer) S.observer.disconnect();
      if (S.closeModal) S.closeModal();
    };
  }

  window.ReviewPage = { renderPage, hash, parseHash };
})();
