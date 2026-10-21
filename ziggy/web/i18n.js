"use strict";
// The panel's language: English or Turkish. The texts are written in Turkish in the code (its author's language) and
// wrapped in t(); the dictionaries in i18n/*.en.js map each to English. Python's own texts reach the panel in Turkish
// too and go through t() where they are shown; i18nPatterns cover the ones with numbers or names in them.
// tests/test_i18n.py keeps it whole: no Turkish literal outside t(), no t() key without its English.
const I18N = { lang: "en", en: Object.create(null), patterns: [] };
const LS_LANG = "ziggy.lang";

(function pickLanguage() {
  let saved = null;
  try { saved = localStorage.getItem(LS_LANG); } catch { /* private mode: the browser's language decides */ }
  I18N.lang = saved === "tr" || saved === "en" ? saved : (navigator.language || "").toLowerCase().startsWith("tr") ? "tr" : "en";
  document.documentElement.lang = I18N.lang;
})();

// A dictionary file: { "<Turkish>": "<English>" }, strict JSON inside the call (the guard test parses it).
function i18nAdd(dict) { Object.assign(I18N.en, dict); }
// Patterns for texts with variable parts: [[/^(\d+) dk önce$/, "$1 min ago"], …]; $1… are the groups.
function i18nPatterns(list) { I18N.patterns.push(...list); }

// The text in the panel's language; {0}, {1}… are replaced by the arguments after the lookup. A key may carry a
// context after "@@" when one Turkish word needs two English ones ("küçük@@size" is "small", "küçük" a nit): Turkish
// shows the part before it.
function t(text, ...args) {
  let out = text;
  if (I18N.lang === "tr" && typeof text === "string" && text.includes("@@")) out = text.slice(0, text.indexOf("@@"));
  if (I18N.lang !== "tr" && typeof text === "string" && text) {
    if (text in I18N.en) out = I18N.en[text];
    else {
      for (const [re, en] of I18N.patterns) {
        const m = text.match(re);
        if (m) { out = en.replace(/\$(\d)/g, (_, i) => m[i] ?? ""); break; }
      }
    }
  }
  return args.length ? String(out).replace(/\{(\d+)\}/g, (_, i) => (args[i] ?? "")) : out;
}

// A multi-line text from the server (an inbox item's details): the whole of it when the dictionaries know it, else line
// by line (most are lists of lines each a known text or the agent's own words).
function tLines(text) {
  if (I18N.lang === "tr" || !text) return text;
  const whole = t(text);
  return whole !== text ? whole : String(text).split("\n").map((line) => t(line)).join("\n");
}

function setLanguage(lang) {
  try { localStorage.setItem(LS_LANG, lang); } catch { /* the choice lasts this page only */ }
  location.reload();
}

// index.html: an element with data-i18n gets its text through t(); data-i18n-attr="placeholder,title,aria-label" does
// the same for those attributes. Runs before the other deferred scripts' DOMContentLoaded handlers (it is listed first).
document.addEventListener("DOMContentLoaded", () => {
  if (I18N.lang === "tr") return;
  for (const el of document.querySelectorAll("[data-i18n]")) el.textContent = t(el.textContent.trim());
  for (const el of document.querySelectorAll("[data-i18n-attr]")) {
    for (const name of el.dataset.i18nAttr.split(",")) {
      const value = el.getAttribute(name.trim());
      if (value) el.setAttribute(name.trim(), t(value));
    }
  }
});
