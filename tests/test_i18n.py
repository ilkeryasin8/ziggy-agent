"""The panel's two languages stay whole: every Turkish text in the panel's code goes through t(), every t() key and
data-i18n text has its English, and the English has no Turkish left in it (web/i18n.js says how it works)."""
import json
import re
from html.parser import HTMLParser
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "ziggy" / "web"
CODE = ("app.js", "review.js", "chat.js", "sessions.js", "md.js", "i18n.js")
TURKISH_CHARS = re.compile(r"[çğıöşüÇĞİÖŞÜ]")
# Turkish words without those letters, so "Kapat" or "Yeni ticket" are caught as well.
TURKISH_WORDS = re.compile(r"\b(ve|bir|yok|ile|Kapat|Evet|Sil|Yeni|Durdur|tamam|hata|dosya|saat|dk|Seni|bekliyor|Sohbet|"
                           r"Projeler|Hafiza|Ayrintilar|Tekrar|Plan onayi|Onayla|Bitti|Kaydet|Temizle|Komuta|Gelen kutusu)\b")
REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^") | {""}
REGEX_WORDS = ("return", "typeof", "case", "of", "in", "void", "delete", "throw", "else")
PLACEHOLDER = re.compile(r"\{(\d+)\}")


def turkish(text: str) -> bool:
    return bool(TURKISH_CHARS.search(text) or TURKISH_WORDS.search(text))


def literals(src: str):
    """(kind, text, start, line) for each string and template literal of a JavaScript source, comments and regex
    literals skipped; a template's text is its literal parts only (what is inside ${…} is code and scanned too)."""
    out, i, n, last = [], 0, len(src), ""

    def line_of(pos):
        return src.count("\n", 0, pos) + 1

    def word_before(pos):
        m = re.search(r"([A-Za-z_$][\w$]*)\s*$", src[:pos])
        return m.group(1) if m else ""

    def scan(i, depth=0):
        nonlocal last
        while i < n:
            c = src[i]
            if depth and c == "}" and depth_stack[-1] == 0:
                return i + 1
            if c == "{" and depth:
                depth_stack[-1] += 1
            elif c == "}" and depth:
                depth_stack[-1] -= 1
            if src.startswith("//", i):
                i = src.find("\n", i)
                i = n if i < 0 else i
                continue
            if src.startswith("/*", i):
                i = src.find("*/", i + 2) + 2
                continue
            if c in "'\"":
                j, buf = i + 1, []
                while src[j] != c:
                    if src[j] == "\\":
                        buf.append(src[j:j + 2])
                        j += 2
                        continue
                    buf.append(src[j])
                    j += 1
                out.append(("str", "".join(buf), i, line_of(i)))
                i, last = j + 1, "x"
                continue
            if c == "`":
                j, parts = i + 1, []
                start = i
                while src[j] != "`":
                    if src[j] == "\\":
                        parts.append(src[j:j + 2])
                        j += 2
                        continue
                    if src.startswith("${", j):
                        depth_stack.append(0)
                        j = scan(j + 2, depth + 1)
                        depth_stack.pop()
                        parts.append("\0")
                        continue
                    parts.append(src[j])
                    j += 1
                out.append(("tpl", "".join(parts), start, line_of(start)))
                i, last = j + 1, "x"
                continue
            if c == "/" and (last in REGEX_BEFORE or word_before(i) in REGEX_WORDS):
                j, cls = i + 1, False
                while j < n and (src[j] != "/" or cls):
                    if src[j] == "\\":
                        j += 1
                    elif src[j] == "[":
                        cls = True
                    elif src[j] == "]":
                        cls = False
                    j += 1
                i, last = j + 1, "x"
                continue
            if not c.isspace():
                last = c if not (c.isalnum() or c in "_$") else "x"
            i += 1
        return i

    depth_stack = [0]
    scan(0)
    return out


def dictionary() -> dict:
    out = {}
    for path in sorted((WEB / "i18n").glob("*.en.js")):
        src = path.read_text(encoding="utf-8")
        m = re.search(r"i18nAdd\((\{.*?\n\})\);", src, re.S)
        assert m, f"{path.name}: no i18nAdd({{…}}) block"
        part = json.loads(m.group(1))
        dup = set(part) & set(out)
        assert not dup or all(out[k] == part[k] for k in dup), f"{path.name}: {sorted(dup)[:5]} translated differently elsewhere"
        out.update(part)
    return out


def keys_in(src: str) -> list[tuple[str, int]]:
    """The literal first arguments of t() calls."""
    found = []
    for kind, text, start, line in literals(src):
        if kind == "str" and re.search(r"(?<![\w$.])t\(\s*$", src[:start]):
            found.append((text.replace("\\'", "'").replace('\\"', '"').replace("\\n", "\n"), line))
    return found


def test_every_turkish_text_in_the_panel_code_goes_through_t():
    bare = []
    for name in CODE:
        src = (WEB / name).read_text(encoding="utf-8")
        lines = src.splitlines()
        for kind, text, start, line in literals(src):
            if not turkish(text) or "i18n-ok" in lines[line - 1]:
                continue
            if kind == "str" and re.search(r"(?<![\w$.])t\(\s*$", src[:start]):
                continue
            bare.append(f"{name}:{line}: {text[:60]!r}")
    assert bare == [], f"{len(bare)} Turkish texts outside t():\n" + "\n".join(bare[:40])


def test_every_t_key_has_its_english_and_the_english_is_english():
    en = dictionary()
    missing = sorted({f"{name}:{line}: {key[:60]!r}" for name in CODE
                      for key, line in keys_in((WEB / name).read_text(encoding="utf-8")) if key not in en})
    assert missing == [], f"{len(missing)} t() keys without English:\n" + "\n".join(missing[:40])
    still = [k for k, v in en.items() if TURKISH_CHARS.search(v)]
    assert still == [], f"English values with Turkish letters: {still[:10]}"
    holes = [k for k, v in en.items() if sorted(set(PLACEHOLDER.findall(k))) != sorted(set(PLACEHOLDER.findall(v)))]
    assert holes == [], f"placeholders differ between key and English: {holes[:10]}"


class _Html(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.bare, self.keys = [], [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        marked = "data-i18n" in a
        self.stack.append((tag, marked))
        named = {x.strip() for x in (a.get("data-i18n-attr") or "").split(",") if x.strip()}
        for name, value in attrs:
            if value and name not in ("class", "id", "href", "src", "style", "data-i18n-attr") and turkish(value):
                (self.keys if name in named else self.bare).append(f"{tag}[{name}]={value!r}")
                if name in named:
                    self.keys[-1] = value

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.stack.pop()

    def handle_endtag(self, tag):
        while self.stack and self.stack.pop()[0] != tag:
            pass

    def handle_data(self, data):
        text = data.strip()
        if not text or not turkish(text) or (self.stack and self.stack[-1][0] in ("script", "style")):
            return
        if self.stack and self.stack[-1][1]:
            self.keys.append(text)
        else:
            self.bare.append(text)


def test_the_page_skeleton_is_translated_too():
    page = _Html()
    page.feed((WEB / "index.html").read_text(encoding="utf-8"))
    assert page.bare == [], f"Turkish in index.html without data-i18n: {page.bare[:20]}"
    en = dictionary()
    assert [k for k in page.keys if k not in en] == []


def test_the_scanner_reads_literals_comments_regexes_and_templates():
    src = ("// 'yorum'\nconst a = t(\"Kapat\"); const r = /ç'/g; x = `merhaba ${t(\"iç\")} ok`; /* \"ş\" */ y = 'düz';\n"
           "z = a / 2 / b; w = \"çift\"; // i18n-ok\n")
    got = [(k, s) for k, s, _, _ in literals(src)]
    assert got == [("str", "Kapat"), ("str", "iç"), ("tpl", "merhaba \0 ok"), ("str", "düz"), ("str", "çift")]
    assert [k for k, _ in keys_in(src)] == ["Kapat", "iç"]
