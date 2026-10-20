// A conversation with headless Claude turns: a ticket's chat (read-only, chat.py) by default, or any other
// conversation through a `conv` adapter (the Sohbet page's chats, talk.py): where it loads, sends and stops, and
// the words around it. DOM is built with textContent only (ctx.h); answers go through ctx.md (md.js, no HTML). The
// tab polls every 700 ms while a reply is live (3 s otherwise) and stops by itself once its element leaves the page.
(function () {
  "use strict";

  const LIVE_MS = 700;
  const IDLE_MS = 3000;
  const AFTER_MS = 800; // one more look after the reply ends, for the final record
  const PENDING_MS = 20000; // a sent question whose turn never shows up (the job was refused late)
  const MAX_TEXT = 8000;
  const LIVE_STEPS = 8;
  const STATE_TR = { starting: "başlıyor", running: "yazıyor", ok: "tamam", failed: "başarısız", killed: "durduruldu", lost: "kayıp" };
  const RETRY = new Set(["failed", "lost", "killed"]);
  const MODEL_KEY = "ziggy.chat.model";
  const seeds = new Map(); // "target/id" -> a question to put in the composer (ask buttons)
  let open = null; // the tab on screen

  const store = {
    get(area, key) { try { return area.getItem(key) || ""; } catch { return ""; } },
    set(area, key, value) { try { if (value) area.setItem(key, value); else area.removeItem(key); } catch { /* this view only */ } },
  };
  const reduced = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const money = (v) => (typeof v === "number" ? (v > 0 && v < 0.005 ? "<$0.01" : `$${v.toFixed(2)}`) : null);
  const secs = (ms) => (typeof ms === "number" ? (ms < 60000 ? `${Math.round(ms / 1000)} sn` : `${Math.floor(ms / 60000)} dk ${Math.round((ms % 60000) / 1000)} sn`) : null);

  function ago(iso) {
    if (!iso) return "";
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return "az önce";
    if (s < 3600) return `${Math.floor(s / 60)} dk önce`;
    if (s < 86400) return `${Math.floor(s / 3600)} sa önce`;
    return `${Math.floor(s / 86400)} g önce`;
  }

  function copy(text, ctx, label) {
    navigator.clipboard.writeText(text).then(() => ctx.toast(`${label} kopyalandı`), () => ctx.toast("Kopyalanamadı", true));
  }

  // --- answers and steps ------------------------------------------------------------------------

  function markdown(text, ctx) {
    const node = ctx.md(text);
    for (const pre of node.querySelectorAll("pre")) {
      const code = pre.textContent;
      const btn = ctx.h("button", { class: "small ghost chat-copy-code", type: "button", title: "Kodu kopyala", onclick: () => copy(code, ctx, "Kod") }, "Kopyala");
      pre.before(ctx.h("div", { class: "chat-code-bar" }, btn));
    }
    return node;
  }

  function step(s, h) {
    const cls = "chat-step" + (s.parent ? " sub" : "") + (s.error ? " err" : "");
    if (s.kind === "tool") return h("li", { class: cls }, h("span", { class: "chat-step-name" }, s.name), " ", s.summary || "");
    if (s.kind === "task") return h("li", { class: cls }, h("span", { class: "chat-step-name" }, "alt-ajan"), " ", s.summary || "");
    const text = (s.text || "").trim();
    return h("li", { class: cls + " note" }, "» ", text.length > 400 ? text.slice(0, 399) + "…" : text);
  }

  function liveLabel(t) {
    if (t.state === "starting") return "Başlıyor";
    if (t.draft) return "Yazıyor";
    const last = t.steps[t.steps.length - 1];
    if (last && last.kind !== "text") return `Çalışıyor · ${t.steps_total} adım`;
    return "Düşünüyor";
  }

  // What the live reply shows: the text being streamed, else the trailing complete message.
  function liveText(t) {
    if (t.draft) return t.draft;
    const last = t.steps[t.steps.length - 1];
    return last && last.kind === "text" && !last.parent ? last.text : "";
  }

  function killedText(error) {
    if (!error || error === "stopped") return "Yanıt durduruldu.";
    const timed = /^timed out after (.+)$/.exec(error);
    return timed ? `Yanıt zaman aşımına uğradı (${timed[1]}).` : `Yanıt kesildi: ${error}.`;
  }

  // What the turn left in the worktree: in a read-only chat despite its rules (chat.py stashes it), in a chat that
  // writes (conv.writes) simply what it did.
  function changeWarnings(t, conv) {
    const out = [];
    const files = t.changed_files || [];
    const list = files.slice(0, 5).join(", ") + (files.length > 5 ? ` +${files.length - 5}` : "");
    const commit = t.head_moved ? `${t.head_moved.before.slice(0, 10)}..${t.head_moved.after.slice(0, 10)}` : "";
    if (conv.writes) {
      if (commit) out.push({ text: `Commit: ${commit}.`, cls: "" });
      if (files.length) out.push({ text: `Commit'lenmemiş değişiklik: ${list}.`, cls: "" });
      return out;
    }
    if (files.length) {
      out.push({ cls: "warn", text: t.stash ? `Sohbet dosya değiştirdi; değişiklik stash'e alındı (${list}).`
        : `Sohbet dosya değiştirdi (${list}); stash'e alınmadı: ${t.stash_skipped || "?"}.` });
    }
    if (commit) out.push({ cls: "warn", text: `Sohbet commit attı (${commit}); hiçbir şey geri alınmadı.` });
    return out;
  }

  // The ticket chat: GET /api/chat/<target>/<id>, the "chat" and "chat_stop" actions.
  function ticketConv(row, ctx) {
    const at = { target: row.target, id: row.id };
    return {
      key: `${row.target}/${row.id}`, draftKey: `ziggy.chat.${row.target}.${row.id}`, threads: true, writes: false,
      title: `${row.id} hakkında sohbet`, placeholder: `${row.id} hakkında sor…`,
      empty: `${row.id} hakkında her şeyi sorabilirsin: plan ne diyor, bir aşama neden başarısız oldu, dalda ne değişti.`,
      load: (since) => ctx.api(`/api/chat/${row.target}/${row.id}` + (since ? `?since=${encodeURIComponent(since)}` : "")),
      send: (text, fresh, model) => ctx.api("/api/action", { ...at, action: "chat", text, new: !!fresh, ...(model ? { model } : {}) }),
      stop: () => ctx.api("/api/action", { ...at, action: "chat_stop" }).then(() => ctx.toast(`${row.id}: yanıt durduruluyor`)),
      info: (data) => `Yanıtlar salt-okunur bir Claude oturumundan gelir; ${data.cwd === "worktree" ? "ticket'ın worktree'sini" : "ana checkout'u"}, planı ve koşu kayıtlarını okur. Hiçbir şeyi değiştirmez.`
        + (data.cwd === "worktree" ? "" : " Worktree yok: sohbet yalnız okuyabilir, komut çalıştıramaz."),
      suggestions: (data) => ticketSuggestions(row, data),
    };
  }

  // Reveals a growing draft a few characters per frame.
  function smooth(box, ctx, prev) {
    const s = { target: prev ? prev.target : "", shown: prev ? prev.shown : 0, frame: 0 };
    const paint = () => box.replaceChildren(s.target ? ctx.md(s.target.slice(0, s.shown)) : "");
    s.set = (text) => {
      if (!text.startsWith(s.target.slice(0, s.shown))) s.shown = 0;
      s.target = text;
      s.shown = reduced() ? text.length : Math.min(s.shown, text.length);
      paint();
      if (s.shown < text.length && !s.frame) s.frame = requestAnimationFrame(tick);
    };
    const tick = () => {
      s.frame = 0;
      if (!box.isConnected) return;
      const left = s.target.length - s.shown;
      if (left <= 0) return;
      s.shown += Math.max(2, Math.ceil(left / 10));
      paint();
      s.frame = requestAnimationFrame(tick);
    };
    return s;
  }

  // --- the tab -------------------------------------------------------------------------------------

  function renderTab(container, row, ctx, conv) {
    const h = ctx.h;
    conv = conv || ticketConv(row, ctx);
    const { key, draftKey } = conv;
    const tab = {
      key, row, ctx, conv, gen: 0, timer: 0, data: null, turns: [], els: new Map(),
      fresh: false, queued: null, pending: null, liveDraft: null, wasLive: false,
    };
    open = tab;

    const dot = h("span", { class: "dot live", hidden: true, title: "yanıt yazılıyor" });
    const info = h("span", { class: "muted small grow" });
    const resume = h("div", { class: "chat-resume", hidden: true });
    const list = h("div", { class: "chat-turns", "aria-live": "polite" });
    const box = h("textarea", { rows: 3, maxlength: String(MAX_TEXT), "aria-label": "Soru" });
    const model = h("select", { "aria-label": "Model", class: "chat-model" });
    const freshBtn = h("button", { class: "small ghost", type: "button", "aria-pressed": "false", hidden: !conv.threads,
      title: "Sıradaki mesaj önceki mesajlar olmadan yeni bir oturum başlatır" }, "Yeni sohbet");
    const stopBtn = h("button", { class: "small danger", type: "button", hidden: true }, "Durdur");
    const sendBtn = h("button", { class: "small primary", type: "button" }, "Gönder");
    const queueBox = h("div", { class: "chat-queue", hidden: true });
    tab.box = box;

    const root = h("div", { class: "chat" },
      h("div", { class: "row tight chat-head" }, dot, info,
        h("button", { class: "small ghost", type: "button", onclick: () => copyConversation(tab) }, "Sohbeti kopyala")),
      resume, list, queueBox,
      h("div", { class: "chat-composer" }, box,
        h("div", { class: "row tight wrap" },
          model, freshBtn,
          h("span", { class: "muted small grow" }, "Enter gönderir · Shift+Enter yeni satır · ↑ son soruyu düzenler · Esc durdurur"),
          stopBtn, sendBtn)));
    tab.root = root;
    container.replaceChildren(root);

    box.value = seeds.get(key) || store.get(sessionStorage, draftKey);
    if (seeds.has(key)) { seeds.delete(key); setTimeout(() => box.focus(), 0); }
    box.addEventListener("input", () => store.set(sessionStorage, draftKey, box.value));
    box.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(tab); }
      else if (e.key === "Escape" && tab.data?.live) { e.preventDefault(); stop(tab); }
      else if (e.key === "ArrowUp" && !box.value) {
        const last = tab.turns[tab.turns.length - 1];
        if (last) { e.preventDefault(); setBox(tab, last.question); }
      }
    });
    freshBtn.addEventListener("click", () => { tab.fresh = !tab.fresh; freshBtn.setAttribute("aria-pressed", String(tab.fresh)); freshBtn.classList.toggle("on", tab.fresh); controls(tab); });
    model.addEventListener("change", () => store.set(localStorage, MODEL_KEY, model.value));
    stopBtn.addEventListener("click", () => stop(tab));
    sendBtn.addEventListener("click", () => submit(tab));

    Object.assign(tab, { dot, info, resume, list, model, freshBtn, stopBtn, sendBtn, queueBox, draftKey });
    poll(tab, ++tab.gen, true);
  }

  function current(tab, gen) {
    return open === tab && tab.gen === gen && tab.root.isConnected;
  }

  async function poll(tab, gen, full) {
    if (!current(tab, gen)) return;
    clearTimeout(tab.timer);
    const since = !full && tab.turns.length ? (tab.data?.live_turn || tab.turns[tab.turns.length - 1].id) : null;
    try {
      const data = await tab.conv.load(since);
      if (!current(tab, gen)) return;
      merge(tab, data, since);
    } catch (e) {
      if (!current(tab, gen)) return;
      tab.info.textContent = `Sohbet okunamadı: ${e.message}`;
    }
    const live = tab.data?.live || tab.pending;
    let wait = live ? LIVE_MS : IDLE_MS;
    if (tab.wasLive && !live) wait = AFTER_MS;
    tab.wasLive = !!tab.data?.live;
    tab.timer = setTimeout(() => poll(tab, gen), wait);
  }

  function merge(tab, data, since) {
    const kept = since ? tab.turns.filter((t) => t.id < since) : [];
    tab.turns = [...kept, ...data.turns];
    tab.data = data;
    if (tab.pending && (tab.turns.some((t) => t.id > tab.pending.after) || Date.now() - tab.pending.at > PENDING_MS)) {
      if (!tab.turns.some((t) => t.id > tab.pending.after)) tab.ctx.toast("Soru başlatılamadı; tekrar dene", true);
      tab.pending = null;
    }
    if (!data.live && !tab.pending && tab.queued) {
      const q = tab.queued;
      tab.queued = null;
      send(tab, q.text, q.fresh);
    }
    render(tab);
  }

  // --- rendering -----------------------------------------------------------------------------------

  function render(tab) {
    const { ctx, data } = tab;
    const h = ctx.h;
    tab.dot.hidden = !data.live;
    tab.info.textContent = tab.conv.info(data);
    tab.resume.hidden = !data.resume_command;
    if (data.resume_command && tab.resume.dataset.cmd !== data.resume_command) {
      tab.resume.dataset.cmd = data.resume_command;
      tab.resume.replaceChildren(h("details", {}, h("summary", { class: "muted small" }, "Terminalde devam et"),
        h("div", { class: "row tight" }, h("code", { class: "chat-cmd" }, data.resume_command),
          h("button", { class: "small ghost", type: "button", onclick: () => copy(data.resume_command, ctx, "Devam komutu") }, "Kopyala"))));
    }
    models(tab);

    const nodes = [];
    if (data.truncated) nodes.push(h("p", { class: "muted small" }, "Daha eski mesajlar gösterilmiyor."));
    if (!tab.turns.length && !tab.pending) nodes.push(emptyState(tab));
    const lastId = tab.turns.length ? tab.turns[tab.turns.length - 1].id : null;
    tab.turns.forEach((t, i) => {
      if (t.new_thread && i > 0) nodes.push(h("div", { class: "chat-divider" }, h("span", {}, "yeni sohbet")));
      // A turn is rebuilt when it changes; the live one between changes only gets its draft.
      const sig = JSON.stringify([t.state, t.answer, t.error, t.steps_total, t.cost_usd, t.id === lastId, data.live, !!tab.pending]);
      let entry = tab.els.get(t.id);
      if (entry && entry.sig === sig && entry.smooth && t.draft !== null) {
        entry.smooth.set(liveText(t));
      } else if (!entry || entry.sig !== sig) {
        entry = { sig, ...turnNode(tab, t, t.id === lastId, entry?.smooth) };
        tab.els.set(t.id, entry);
      }
      nodes.push(entry.el);
    });
    if (tab.pending) nodes.push(h("div", { class: "chat-q pending" }, h("div", { class: "chat-q-meta muted small" }, "gönderiliyor…"), tab.pending.text));
    const atBottom = tab.list.scrollHeight - tab.list.scrollTop - tab.list.clientHeight < 60;
    tab.list.replaceChildren(...nodes);
    if (atBottom) tab.list.scrollTop = tab.list.scrollHeight;

    tab.queueBox.hidden = !tab.queued;
    if (tab.queued) {
      tab.queueBox.replaceChildren(h("div", { class: "chat-q queued" },
        h("div", { class: "chat-q-meta muted small" }, "sırada · yanıt bitince gönderilir ",
          h("button", { class: "small ghost", type: "button", onclick: () => { tab.queued = null; render(tab); } }, "iptal")),
        tab.queued.text));
    }
    controls(tab);
  }

  function models(tab) {
    const { data, model, ctx } = tab;
    const sig = data.models.join(",");
    if (model.dataset.sig === sig) return;
    model.dataset.sig = sig;
    model.replaceChildren(ctx.h("option", { value: "" }, `varsayılan (${data.model})`),
      ...data.models.filter((m) => m !== data.model).map((m) => ctx.h("option", { value: m }, m)));
    const saved = store.get(localStorage, MODEL_KEY);
    model.value = data.models.includes(saved) && saved !== data.model ? saved : "";
  }

  function controls(tab) {
    const live = !!tab.data?.live;
    tab.stopBtn.hidden = !live;
    tab.sendBtn.textContent = live || tab.pending ? "Sıraya al" : "Gönder";
    tab.sendBtn.disabled = (live || tab.pending) && !!tab.queued;
    tab.box.placeholder = tab.fresh ? "Yeni sohbette sor…"
      : live || tab.pending ? "Önden yaz: yanıt bitince gönderilir…"
      : tab.turns.length ? "Devam sorusu sor…" : tab.conv.placeholder;
  }

  function ticketSuggestions(r, data) {
    const out = ["Bu ticket ne durumda?"];
    if (r.status === "failed") out.push("Son koşu neden başarısız oldu, ne düzeltir?");
    if (r.status === "needs-user") out.push("Açık soruları cevaplamama yardım et: ne önerirsin, neden?");
    if (r.status === "awaiting-approval" || r.status === "done" || r.status === "running") out.push("Planı ve kalan işi özetle.");
    if (data?.cwd === "worktree") out.push("Dalda şu ana kadar ne değişti?");
    if (r.status === "done") out.push("Review neler buldu, açık kalan var mı?");
    out.push("Bu değişiklik hangi dosyalara dokunacak?");
    return out.slice(0, 5);
  }

  function emptyState(tab) {
    const h = tab.ctx.h;
    return h("div", { class: "chat-empty" },
      h("p", { class: "muted" }, tab.conv.empty),
      h("div", { class: "row tight wrap" }, tab.conv.suggestions(tab.data).map((q) =>
        h("button", { class: "small", type: "button", onclick: () => send(tab, q, false) }, q))));
  }

  function turnNode(tab, t, isLast, prevSmooth) {
    const { ctx, data } = tab;
    const h = ctx.h;
    const isLive = data.live && data.live_turn === t.id;
    const edit = isLast && !data.live && !tab.pending
      ? h("button", { class: "small ghost", type: "button", onclick: () => setBox(tab, t.question) }, "düzenle") : "";
    const q = h("div", { class: "chat-q" },
      h("div", { class: "chat-q-meta row tight muted small" }, h("span", {}, `${t.by || "sen"} · ${ago(t.created_at)}`), h("span", { class: "grow" }), edit),
      h("div", { class: "chat-q-text" }, t.question));
    const reply = h("div", { class: "chat-a" + (isLive ? " live" : "") });
    let smoothText = null;
    if (isLive) {
      // The trailing message shows as the draft, so the step list leaves it out.
      const all = t.draft || !t.steps.length || t.steps[t.steps.length - 1].kind !== "text" || t.steps[t.steps.length - 1].parent
        ? t.steps : t.steps.slice(0, -1);
      const steps = all.slice(-LIVE_STEPS);
      const earlier = t.steps_total - (t.steps.length - all.length) - steps.length;
      reply.append(h("div", { class: "row tight muted small" }, h("span", { class: "dot live" }), liveLabel(t),
        t.started_at ? h("span", { "data-since": t.started_at }) : ""));
      if (steps.length) reply.append(h("ul", { class: "chat-steps" }, earlier > 0 ? h("li", { class: "chat-step muted" }, `${earlier} önceki adım`) : "", steps.map((s) => step(s, h))));
      const draftBox = h("div", { class: "chat-draft" });
      reply.append(draftBox);
      smoothText = smooth(draftBox, ctx, prevSmooth);
      smoothText.set(liveText(t));
    } else {
      if (t.steps_total) {
        const notes = t.steps.every((s) => s.kind === "text");
        reply.append(h("details", { class: "chat-steps-box" },
          h("summary", { class: "muted small" }, `${t.steps_total} ${notes ? "not" : "adım"}`),
          h("ul", { class: "chat-steps" }, t.steps.map((s) => step(s, h)))));
      }
      if (t.answer) reply.append(markdown(t.answer, ctx));
      if (t.state === "killed") reply.append(h("p", { class: "chat-note warn" }, killedText(t.error)));
      if (t.state === "failed" || t.state === "lost") reply.append(h("p", { class: "chat-note bad" }, `Yanıt yok: ${t.error || STATE_TR[t.state]}`));
      for (const w of changeWarnings(t, tab.conv)) reply.append(h("p", { class: "chat-note " + w.cls }, w.text));
      const meta = [t.model, secs(t.duration_ms), money(t.cost_usd)].filter(Boolean).join(" · ");
      reply.append(h("div", { class: "row tight chat-a-foot" },
        t.answer ? h("button", { class: "small ghost", type: "button", onclick: () => copy(t.answer, ctx, "Yanıt") }, "Kopyala") : "",
        isLast && RETRY.has(t.state) && !data.live && !tab.pending
          ? h("button", { class: "small ghost", type: "button", onclick: () => send(tab, t.question, false) }, "Tekrar dene") : "",
        h("span", { class: "grow" }), h("span", { class: "muted small" }, meta)));
    }
    return { el: h("div", { class: "chat-turn", "data-turn": t.id }, q, reply), smooth: smoothText };
  }

  // --- actions -------------------------------------------------------------------------------------

  function setBox(tab, text) {
    tab.box.value = text;
    store.set(sessionStorage, tab.draftKey, text);
    tab.box.focus();
  }

  function submit(tab) {
    const text = tab.box.value.trim();
    if (!text) return;
    if (tab.data?.live || tab.pending) {
      if (tab.queued) return;
      tab.queued = { text, fresh: tab.fresh };
      clearComposer(tab);
      render(tab);
      return;
    }
    clearComposer(tab);
    send(tab, text, tab.fresh);
  }

  function clearComposer(tab) {
    tab.box.value = "";
    store.set(sessionStorage, tab.draftKey, "");
  }

  async function send(tab, text, fresh) {
    const { ctx } = tab;
    const offered = tab.data?.models || [];
    const chosen = tab.model.value && offered.includes(tab.model.value) ? tab.model.value : null;
    const after = tab.turns.length ? tab.turns[tab.turns.length - 1].id : "";
    tab.pending = { text, after, at: Date.now() };
    if (fresh) { tab.fresh = false; tab.freshBtn.setAttribute("aria-pressed", "false"); tab.freshBtn.classList.remove("on"); }
    render(tab);
    try {
      await tab.conv.send(text, !!fresh, chosen);
    } catch (e) {
      tab.pending = null;
      if (!tab.box.value) setBox(tab, text);
      ctx.toast(e.message, true);
      render(tab);
      return;
    }
    poll(tab, ++tab.gen);
  }

  async function stop(tab) {
    try {
      await tab.conv.stop();
    } catch (e) {
      tab.ctx.toast(e.message, true);
    }
    poll(tab, ++tab.gen);
  }

  function copyConversation(tab) {
    const parts = [`# ${tab.conv.title}`];
    tab.turns.forEach((t, i) => {
      if (t.new_thread && i > 0) parts.push("---");
      parts.push(`**${t.by || "sen"}:** ${t.question}`);
      parts.push(t.answer || `_(yanıt yok: ${t.error || STATE_TR[t.state] || t.state})_`);
    });
    copy(parts.join("\n\n") + "\n", tab.ctx, "Sohbet");
  }

  // An ask button elsewhere in the drawer: put the question in the composer and show the tab.
  function ask(row, question, ctx) {
    const key = `${row.target}/${row.id}`;
    if (open && open.key === key && open.root.isConnected) {
      setBox(open, question);
      return;
    }
    seeds.set(key, question);
    ctx.openTab("chat");
  }

  window.ChatView = { renderTab, ask };
})();
