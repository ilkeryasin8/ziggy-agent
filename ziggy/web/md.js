"use strict";
// Markdown and frontmatter for specs and tickets, rendered as DOM nodes. No innerHTML anywhere:
// the text comes from the agent and from tickets, so it is untrusted.

const MD = (() => {
  // Inline code tokens that read better as labels (review triage verdicts and severities).
  const PILLS = {
    high: "bad", medium: "warn", low: "info", false: "mute", "maybe-false": "mute",
    reject: "mute", defer: "warn", patch: "ok", fix: "ok", accept: "ok", done: "ok", blocked: "warn",
  };

  function el(tag, cls, ...kids) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    for (const k of kids.flat(Infinity)) if (k !== null && k !== undefined && k !== false) n.append(k instanceof Node ? k : document.createTextNode(String(k)));
    return n;
  }

  const INLINE = /(\[P[0-3]\])|(`+)([^`]+?)\2|\*\*(.+?)\*\*|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)|(https?:\/\/[^\s)<>]+[^\s)<>.,;:])|(^|[^\w*])\*([^*\s][^*]*?)\*(?!\w)/g;

  function inline(text) {
    const frag = document.createDocumentFragment();
    let last = 0;
    for (const m of text.matchAll(INLINE)) {
      let start = m.index;
      if (m[9] !== undefined) {
        start += m[8].length; // keep the character before an *italic*
      }
      if (start > last) frag.append(text.slice(last, start));
      if (m[1] !== undefined) {
        // Codex review priorities: P0/P1 must fix, P2 should, P3 nice to have.
        frag.append(el("span", `pill ${{ "[P0]": "bad", "[P1]": "bad", "[P2]": "warn", "[P3]": "info" }[m[1]]}`, m[1].slice(1, -1)));
      } else if (m[3] !== undefined) {
        const code = m[3];
        frag.append(PILLS[code] ? el("span", `pill ${PILLS[code]}`, code) : el("code", null, code));
      } else if (m[4] !== undefined) {
        frag.append(el("strong", null, inline(m[4])));
      } else if (m[5] !== undefined) {
        const a = el("a", null, m[5]);
        a.href = m[6]; a.target = "_blank"; a.rel = "noopener noreferrer";
        frag.append(a);
      } else if (m[7] !== undefined) {
        const a = el("a", null, m[7]);
        a.href = m[7]; a.target = "_blank"; a.rel = "noopener noreferrer";
        frag.append(a);
      } else if (m[9] !== undefined) {
        frag.append(el("em", null, inline(m[9])));
      }
      last = m.index + m[0].length;
    }
    if (last < text.length) frag.append(text.slice(last));
    return frag;
  }

  const LIST = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
  const KV = /^([A-Z][\w ()/.-]{0,40}):\s+(\S.*)$/;

  function kvGrid(pairs) {
    return el("dl", "kv", pairs.map(([k, v]) => [el("dt", null, k), el("dd", null, inline(v))]));
  }

  function list(lines) {
    // lines: [{indent, ordered, text}] -> nested <ul>/<ol>
    const root = el(lines[0].ordered ? "ol" : "ul", "md-list");
    const stack = [{ indent: lines[0].indent, node: root, last: null }];
    for (const item of lines) {
      while (stack.length > 1 && item.indent < stack[stack.length - 1].indent) stack.pop();
      let top = stack[stack.length - 1];
      if (item.indent > top.indent && top.last) {
        const sub = el(item.ordered ? "ol" : "ul", "md-list");
        top.last.append(sub);
        stack.push({ indent: item.indent, node: sub, last: null });
        top = stack[stack.length - 1];
      }
      // Indented lines under an item are its body (e.g. a review finding's explanation). A task
      // item (`- [ ] T001 …` / `- [x] …`) gets a box, done or open, instead of the bullet.
      const task = item.text.match(/^\[([ xX])\]\s+(.*)$/);
      const text = task ? task[2] : item.text;
      const li = el("li", task ? `task ${task[1] === " " ? "open" : "done"}` : null,
        task ? el("span", "task-box", task[1] === " " ? "" : "✓") : null,
        inline(text), item.more.length ? el("div", "li-body", inline(item.more.join(" "))) : null);
      top.node.append(li);
      top.last = li;
    }
    return root;
  }

  function table(rows) {
    const cells = (line) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
    const head = cells(rows[0]);
    return el("div", "md-table", el("table", null,
      el("thead", null, el("tr", null, head.map((c) => el("th", null, inline(c))))),
      el("tbody", null, rows.slice(2).map((r) => el("tr", null, cells(r).map((c) => el("td", null, inline(c))))))));
  }

  // Blocks of one markdown body (no frontmatter, no section splitting).
  function blocks(text) {
    const out = [];
    const lines = text.replace(/\r\n/g, "\n").split("\n");
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      const fence = line.match(/^\s*(```|~~~)(.*)$/);
      if (fence) {
        const body = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
        i++;
        out.push(el("pre", "md-code", body.join("\n")));
        continue;
      }
      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        out.push(el(`h${Math.min(6, heading[1].length + 1)}`, "md-h", inline(heading[2])));
        i++;
        continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.push(el("hr")); i++; continue; }
      if (/^\s*<\/?[a-z][\w-]*>\s*$/.test(line)) { i++; continue; } // tag lines, handled by the caller
      if (line.startsWith(">")) {
        const quote = [];
        while (i < lines.length && lines[i].startsWith(">")) quote.push(lines[i++].replace(/^>\s?/, ""));
        out.push(el("blockquote", null, blocks(quote.join("\n"))));
        continue;
      }
      if (line.trim().startsWith("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        const rows = [];
        while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(lines[i++]);
        out.push(table(rows));
        continue;
      }
      if (LIST.test(line)) {
        const items = [];
        while (i < lines.length) {
          const m = lines[i].match(LIST);
          if (m) {
            items.push({ indent: m[1].length, ordered: /\d/.test(m[2]), text: m[3], more: [] });
            i++;
          } else if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && items.length) {
            items[items.length - 1].more.push(lines[i++].trim()); // the item's own indented lines
          } else break;
        }
        const kv = items.every((it) => it.indent === items[0].indent && !it.more.length && KV.test(it.text));
        out.push(kv ? kvGrid(items.map((it) => it.text.match(KV).slice(1))) : list(items));
        continue;
      }
      const para = [];
      while (i < lines.length && lines[i].trim() && !LIST.test(lines[i]) && !/^(#{1,6})\s/.test(lines[i]) && !/^\s*(```|~~~)/.test(lines[i]) && !/^\s*<\/?[a-z][\w-]*>\s*$/.test(lines[i])) {
        para.push(lines[i++].trim());
      }
      if (para.length >= 2 && para.every((p) => KV.test(p))) out.push(kvGrid(para.map((p) => p.match(KV).slice(1))));
      else out.push(el("p", null, inline(para.join(" "))));
    }
    return out;
  }

  // Top-level "## " sections; lines inside <tag>…</tag> wrappers are marked with the tag.
  function sections(text) {
    const result = { intro: [], sections: [] };
    let current = null;
    let wrapper = null;
    for (const line of text.replace(/\r\n/g, "\n").split("\n")) {
      const open = line.match(/^\s*<([a-z][\w-]*)>\s*$/);
      const close = line.match(/^\s*<\/([a-z][\w-]*)>\s*$/);
      if (open) { wrapper = open[1]; continue; }
      if (close) { wrapper = null; continue; }
      const h = line.match(/^##\s+(.*)$/);
      if (h) {
        current = { title: h[1].trim(), lines: [], wrapper };
        result.sections.push(current);
        continue;
      }
      if (current) current.lines.push(line);
      else result.intro.push(line);
    }
    for (const s of result.sections) s.body = s.lines.join("\n");
    result.intro = result.intro.join("\n");
    return result;
  }

  // Frontmatter: the YAML subset agents write (scalars, quoted strings, [inline] lists, lists of
  // maps, folded/literal block scalars). Returns {data, body}; data is null when there is none
  // or it does not parse, and then the caller shows it raw.
  function frontmatter(text) {
    const m = text.match(/^---\n([\s\S]*?)\n---\n?/);
    if (!m) return { data: null, raw: "", body: text };
    try {
      return { data: yaml(m[1]), raw: m[1], body: text.slice(m[0].length) };
    } catch {
      return { data: null, raw: m[1], body: text.slice(m[0].length) };
    }
  }

  function scalar(v) {
    v = v.trim();
    if (/^'.*'$/.test(v)) return v.slice(1, -1).replace(/''/g, "'");
    if (/^".*"$/.test(v)) return JSON.parse(v);
    if (/^\[.*\]$/.test(v)) return v.slice(1, -1).split(",").map((x) => x.trim()).filter(Boolean).map(scalar);
    if (v === "true" || v === "false") return v === "true";
    if (v === "null" || v === "~" || v === "") return null;
    if (/^-?\d+(\.\d+)?$/.test(v)) return Number(v);
    return v;
  }

  function yaml(src) {
    const lines = src.split("\n");
    let i = 0;
    const indentOf = (l) => l.match(/^\s*/)[0].length;

    function block(style, parentIndent) {
      const body = [];
      while (i < lines.length && (!lines[i].trim() || indentOf(lines[i]) > parentIndent)) body.push(lines[i++]);
      const min = Math.min(...body.filter((l) => l.trim()).map(indentOf));
      const text = body.map((l) => l.slice(min)).join("\n").replace(/\n+$/, "");
      return style.startsWith(">") ? text.replace(/([^\n])\n(?=[^\n])/g, "$1 ") : text;
    }

    function value(rest, indent) {
      if (/^[>|][+-]?$/.test(rest.trim())) return block(rest.trim(), indent);
      if (rest.trim() === "") {
        if (i < lines.length && lines[i].trim().startsWith("- ") && indentOf(lines[i]) >= indent) return seq(indentOf(lines[i]));
        if (i < lines.length && indentOf(lines[i]) > indent) return map(indentOf(lines[i]));
        return null;
      }
      return scalar(rest);
    }

    function map(indent, first) {
      const obj = {};
      if (first) {
        const [k, rest] = first;
        obj[k] = value(rest, indent);
      }
      while (i < lines.length) {
        const line = lines[i];
        if (!line.trim()) { i++; continue; }
        if (indentOf(line) !== indent || line.trim().startsWith("- ")) break;
        const kv = line.trim().match(/^([\w-]+):(?:\s+(.*))?$/);
        if (!kv) throw new Error(`yaml: ${line}`);
        i++;
        obj[kv[1]] = value(kv[2] || "", indent);
      }
      return obj;
    }

    function seq(indent) {
      const arr = [];
      while (i < lines.length && lines[i].trim().startsWith("- ") && indentOf(lines[i]) === indent) {
        const item = lines[i].trim().slice(2);
        i++;
        const kv = item.match(/^([\w-]+):(?:\s+(.*))?$/);
        arr.push(kv ? map(indent + 2, [kv[1], kv[2] || ""]) : scalar(item));
      }
      return arr;
    }

    return map(0);
  }

  return { el, inline, blocks, sections, frontmatter };
})();
