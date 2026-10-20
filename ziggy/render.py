"""The Mustache subset the prompts use: {{a.b}}, {{#x}}…{{/x}}, {{^x}}…{{/x}}, {{.}}, {{! comment}} and
{{> partial}}. No HTML escaping; a missing or None value renders as "". A section on a dict pushes it as
context, on a list repeats once per item, on anything else shows when truthy (0, "", [], {} are false).
A tag that stands alone on its line (section, inverted, closing, comment, partial) takes its line with it."""
import re
from collections.abc import Callable

_TAG = re.compile(r"\{\{\s*([#^/!>]?)\s*(.*?)\s*\}\}", re.S)


class RenderError(ValueError):
    pass


def _tokens(template: str) -> list:
    """("text", s) | ("var", name) | ("open", kind, name) | ("close", name) | ("partial", name, indent)."""
    out, pos = [], 0
    for m in _TAG.finditer(template):
        kind, name = m.group(1), m.group(2)
        start, end = m.start(), m.end()
        if kind in "#^/!>" and kind:
            line_start = template.rfind("\n", 0, start) + 1
            line_end = template.find("\n", end)
            line_end = len(template) if line_end == -1 else line_end
            before, after = template[line_start:start], template[end:line_end]
            if not before.strip() and not after.strip():  # standalone: drop the whole line
                start = max(line_start, pos)
                end = line_end + 1 if line_end < len(template) else line_end
        out.append(("text", template[pos:start]))
        if kind in ("#", "^"):
            out.append(("open", kind, name))
        elif kind == "/":
            out.append(("close", name))
        elif kind == ">":
            out.append(("partial", name))
        elif kind != "!":
            out.append(("var", name))
        pos = end
    out.append(("text", template[pos:]))
    return [t for t in out if t != ("text", "")]


def _tree(tokens: list) -> list:
    root: list = []
    stack: list[tuple[str, list]] = [("", root)]
    for tok in tokens:
        if tok[0] == "open":
            node = ["section", tok[1], tok[2], []]
            stack[-1][1].append(node)
            stack.append((tok[2], node[3]))
        elif tok[0] == "close":
            if len(stack) == 1 or stack[-1][0] != tok[1]:
                raise RenderError(f"unexpected {{{{/{tok[1]}}}}}")
            stack.pop()
        else:
            stack[-1][1].append(tok)
    if len(stack) > 1:
        raise RenderError(f"unclosed {{{{#{stack[-1][0]}}}}}")
    return root


_MISSING = object()


def _lookup(stack: list, name: str):
    if name == ".":
        return stack[-1]
    parts = name.split(".")
    for frame in reversed(stack):
        value = frame
        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                value = _MISSING
                break
        if value is not _MISSING:
            return value
    return None


def _text(value) -> str:
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    if isinstance(value, bool):
        return "true" if value else ""
    return str(value)


def _render(nodes: list, stack: list, partial: Callable[[str], str], depth: int) -> str:
    out = []
    for node in nodes:
        kind = node[0]
        if kind == "text":
            out.append(node[1])
        elif kind == "var":
            out.append(_text(_lookup(stack, node[1])))
        elif kind == "partial":
            if depth > 8:
                raise RenderError(f"partials nested too deep at {{{{> {node[1]}}}}}")
            out.append(_render(_tree(_tokens(partial(node[1]))), stack, partial, depth + 1))
        else:
            _, sigil, name, children = node
            value = _lookup(stack, name)
            truthy = bool(value) and value is not _MISSING
            if sigil == "^":
                if not truthy:
                    out.append(_render(children, stack, partial, depth))
            elif isinstance(value, (list, tuple)):
                for item in value:
                    out.append(_render(children, [*stack, item], partial, depth))
            elif isinstance(value, dict):
                if value:
                    out.append(_render(children, [*stack, value], partial, depth))
            elif truthy:
                out.append(_render(children, stack, partial, depth))
    return "".join(out)


def render(template: str, context: dict, partial: Callable[[str], str] = lambda name: "") -> str:
    return _render(_tree(_tokens(template)), [context], partial, 0)
