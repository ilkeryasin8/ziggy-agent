// Oturumlar: the other Claude Code sessions on this machine (a page of its own + read-only
// transcript drawer). DOM is built with textContent only (ctx.h); transcript text is never HTML.
(function () {
  "use strict";

  const STATUS = { working: "çalışıyor", idle: "boşta", ended: "bitti" }; // i18n-ok: the keys of statusText()
  const statusText = (status) => (status in STATUS ? t(STATUS[status]) : status);
  const VIA = { "claude-desktop": "masaüstü", cli: "terminal", "sdk-cli": "headless", "sdk-ts": "headless", "sdk-py": "headless", "claude-vscode": "vscode" }; // i18n-ok: the keys of viaText()
  const viaText = (entry) => (entry in VIA ? t(VIA[entry]) : entry);
  let open = null; // {session, ctx, pos, timer, el, list, foot, follow}

  function ago(iso) {
    if (!iso) return "";
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return t("az önce");
    if (s < 3600) return t("{0} dk önce", Math.floor(s / 60));
    if (s < 86400) return t("{0} sa önce", Math.floor(s / 3600));
    return t("{0} gün önce", Math.floor(s / 86400));
  }

  function shortPath(p) {
    return (p || "").replace(/^\/Users\/[^/]+/, "~");
  }

  function title(s) {
    return s.title || (s.cwd || "").replace(/\/+$/, "").split("/").pop() || t("oturum {0}", s.id.slice(0, 8));
  }

  function where(s) {
    if (s.ticket) return `${s.target} / ${s.ticket}`;
    return s.target || t("diğer");
  }

  function copy(text, ctx) {
    navigator.clipboard.writeText(text).then(() => ctx.toast(t("Devam komutu kopyalandı")), () => ctx.toast(t("Kopyalanamadı")));
  }

  function resumeButton(s, ctx) {
    const h = ctx.h;
    if (!s.resume) return h("span", { class: "muted" }, t("dizin artık yok"));
    return h("button", { class: "btn small", type: "button", title: s.resume, onclick: (e) => { e.stopPropagation(); copy(s.resume, ctx); } }, t("Devam komutunu kopyala"));
  }

  // One row: status, the session (title and your last prompt), where it runs, when it last moved, and its actions.
  function row(s, ctx) {
    const h = ctx.h;
    const showPrompt = s.last_prompt && s.last_prompt !== s.title;
    return h("tr", { class: `s-row s-${s.status}` },
      h("td", { class: "s-state" }, h("span", { class: `session-dot s-${s.status}` }), statusText(s.status)),
      h("td", { class: "s-main" },
        h("button", { class: `session-title link${s.title_source === "prompt" ? " italic" : ""}`, type: "button", onclick: () => openDrawer(s, ctx) }, title(s)),
        showPrompt ? h("div", { class: "session-prompt clamp-2" }, h("span", { class: "muted" }, t("sen: ")), s.last_prompt) : ""),
      h("td", { class: "s-via" }, viaText(s.entrypoint) || "-", h("div", { class: "muted small" }, where(s))),
      h("td", { class: "s-where mono" }, shortPath(s.cwd), h("div", { class: "muted" }, [s.branch, s.model].filter(Boolean).join(" · "))),
      h("td", { class: "s-when nowrap" }, ago(s.last_activity), s.started_at ? h("div", { class: "muted small" }, t("başladı {0}", ago(s.started_at))) : ""),
      h("td", { class: "s-actions" },
        resumeButton(s, ctx),
        h("button", { class: "btn small ghost", type: "button", disabled: !s.has_transcript, title: s.has_transcript ? "" : t("henüz döküm yok"), onclick: () => openDrawer(s, ctx) }, t("Döküm"))));
  }

  let shown = "open";

  // The Oturumlar page: open sessions or the recently ended ones, as a table.
  function renderPage(container, sessions, ctx, hidden = 0) {
    const h = ctx.h;
    sessions = sessions || [];
    const live = sessions.filter((s) => s.status !== "ended");
    const ended = sessions.filter((s) => s.status === "ended");
    const working = live.filter((s) => s.status === "working").length;
    const rows = shown === "open" ? live : ended;
    const seg = (key, label, n) => h("button", { class: "seg" + (shown === key ? " on" : ""), type: "button", "aria-pressed": String(shown === key),
      onclick: () => { shown = key; renderPage(container, sessions, ctx, hidden); } }, label, h("span", { class: "vcount" }, String(n)));
    container.replaceChildren(
      h("div", { class: "row sessions-bar" },
        h("div", { class: "segs", role: "group", "aria-label": t("Oturumlar") }, seg("open", t("Açık"), live.length), seg("ended", t("Yakında biten"), ended.length)),
        h("span", { class: "muted small grow" }, t("{0} çalışıyor", working) + (hidden ? t(" · {0} oturum gizli ([sessions] hide_cwd_prefixes)", hidden) : ""))),
      rows.length
        ? h("div", { class: "table-box" }, h("table", { class: "stable" },
            h("thead", {}, h("tr", {}, ["Durum", "Oturum", "Kaynak", "Konum", "Son", ""].map((x) => h("th", { scope: "col" }, x && t(x))))),
            h("tbody", {}, rows.map((s) => row(s, ctx)))))
        : h("div", { class: "empty" }, shown === "open" ? t("Açık bir Claude Code oturumu yok.") : t("Son günlerde biten oturum yok.")));
    if (open) {
      const fresh = sessions.find((s) => s.config === open.session.config && s.id === open.session.id);
      if (fresh) open.session = fresh;
    }
  }

  // --- drawer ---------------------------------------------------------------------------------

  function item(it, h) {
    switch (it.kind) {
      case "user":
        return h("div", { class: "tx-item tx-user" }, h("div", { class: "tx-who" }, t("sen")), h("div", { class: "tx-text pre-wrap" }, it.text));
      case "text":
        return h("div", { class: `tx-item tx-text-item${it.parent ? " nested" : ""}` }, h("div", { class: "tx-who" }, "claude"), h("div", { class: "tx-text pre-wrap" }, it.text));
      case "tool":
        return h("div", { class: `tx-item tx-tool${it.parent ? " nested" : ""}` }, h("span", { class: "mono" }, it.summary || it.name));
      case "result":
        return h("details", { class: `tx-item tx-result${it.is_error ? " bad" : ""}${it.parent ? " nested" : ""}` },
          h("summary", { class: "muted small" }, it.is_error ? t("hata çıktısı") : t("araç çıktısı")),
          h("pre", { class: "mono small pre-wrap" }, it.content + (it.truncated ? "\n…" : "")));
      case "prompt":
        return h("div", { class: "tx-item nested muted small pre-wrap" }, it.text);
      default:
        return null;
    }
  }

  function head(s, h) {
    return h("div", { class: "session-drawer-head" },
      h("div", { class: "session-head" },
        h("span", { class: `session-dot s-${s.status}` }),
        h("span", { class: `pill s-${s.status}` }, statusText(s.status)),
        s.entrypoint ? h("span", { class: "chip" }, viaText(s.entrypoint)) : "",
        h("span", { class: "chip" }, where(s))),
      h("h2", null, title(s)),
      h("div", { class: "muted mono small session-where" },
        [shortPath(s.cwd) + (s.cwd_exists ? "" : t(" (dizin artık yok)")), s.branch, s.model, s.version && `Claude Code ${s.version}`].filter(Boolean).join(" · ")),
      h("div", { class: "muted small" },
        [s.started_at && t("başladı {0}", s.started_at.slice(0, 16).replace("T", " ")), s.last_activity && t("son etkinlik {0}", ago(s.last_activity)), s.pid && `pid ${s.pid}`].filter(Boolean).join(" · ")),
      s.resume ? h("div", { class: "session-resume" }, h("code", { class: "mono small" }, s.resume), resumeButton(s, open.ctx)) : "");
  }

  async function load() {
    const d = open;
    if (!d) return;
    const s = d.session;
    let page;
    try {
      const q = d.pos == null ? "" : `?pos=${d.pos}`;
      page = await d.ctx.api(`/api/sessions/${encodeURIComponent(s.config)}/${encodeURIComponent(s.id)}/transcript${q}`);
    } catch (err) {
      if (open !== d) return;
      if (d.pos == null) d.list.replaceChildren(d.ctx.h("div", { class: "muted" }, t("Bu oturumun dökümü yok (henüz yazılmadı ya da artık listede değil).")));
      return;
    }
    if (open !== d) return;
    if (d.pos == null && page.truncated) d.foot.textContent = t("Uzun bir dökümün yalnızca son 4 MB'ı gösteriliyor.");
    for (const it of page.items || []) {
      const node = item(it, d.ctx.h);
      if (node) d.list.append(node);
    }
    d.pos = page.pos;
    if (d.follow) d.body.scrollTop = d.body.scrollHeight;
    if (page.more) d.timer = setTimeout(load, 0);
    else if (s.status !== "ended") d.timer = setTimeout(load, 5000);
  }

  function close() {
    if (!open) return;
    clearTimeout(open.timer);
    open.el.remove();
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("hashchange", close);
    open = null;
  }

  function onKey(e) {
    if (e.key === "Escape") close();
  }

  function openDrawer(session, ctx) {
    close();
    const h = ctx.h;
    open = { session, ctx, pos: null, timer: null, follow: true };
    const list = h("div", { class: "tx-list" });
    const foot = h("div", { class: "muted small" });
    const body = h("div", { class: "session-drawer-body" }, foot, list);
    body.addEventListener("scroll", () => { if (open) open.follow = body.scrollTop + body.clientHeight >= body.scrollHeight - 40; });
    const el = h("div", { class: "session-drawer-backdrop", onclick: (e) => { if (e.target === el) close(); } },
      h("aside", { class: "session-drawer", role: "dialog", "aria-label": t("Oturum dökümü") },
        h("button", { class: "btn small session-drawer-close", type: "button", onclick: close, "aria-label": t("Kapat") }, t("Kapat")),
        head(session, h),
        h("div", { class: "muted small session-drawer-note" }, t("Salt okunur döküm")),
        body));
    Object.assign(open, { el, list, foot, body });
    document.body.append(el);
    document.addEventListener("keydown", onKey);
    window.addEventListener("hashchange", close);  // the back button or a link leaves the transcript too
    load();
  }

  window.SessionsView = { renderPage, openDrawer };
})();
