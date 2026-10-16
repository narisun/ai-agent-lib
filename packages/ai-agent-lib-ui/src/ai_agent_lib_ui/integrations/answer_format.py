"""Present structured data citations separately from the agent's answer text.

This is a display projection, never a rewrite of the stored answer or evidence.
Only table[key=value;...] citations are collected. Ordinary Markdown links and
code examples keep their original meaning, and raw HTML remains disabled.
"""

import re
from dataclasses import dataclass

_REFERENCE_TEXT = r"[a-z][a-z_0-9]*\[[a-z][a-z_0-9]*=[^\[\]\n;]+(?:;[a-z][a-z_0-9]*=[^\[\]\n;]+)*\]"
_REFERENCE = re.compile(_REFERENCE_TEXT)
_BREAK = r"(?i:<br\s*/?>)"
_CITATION = rf"(?:\[{_REFERENCE_TEXT}(?:[ \t]*[,;][ \t]*{_REFERENCE_TEXT})*\]|{_REFERENCE_TEXT})"
_CITATION_RUN = rf"{_CITATION}(?:[ \t]*[,;]?[ \t]*{_CITATION})*"
_LIMIT_FIELDS = r"[a-z_][a-z_0-9]*=[a-z_][a-z_0-9]*(?:;[ \t]*[a-z_][a-z_0-9]*=[a-z_][a-z_0-9]*)*"
_INLINE = re.compile(
    # Keep inline code, links (including images), and autolinks out of the formatter.
    r"(?P<literal>(?P<ticks>`+).*?(?P=ticks)(?!`)"
    r"|[ \t]*!?\[(?:[^\[\]\n]|\[[^\]\n]*\])*\](?:\([^\n]*?\)|\[[^\]\n]*\])"
    r"|<https?://[^>\n]+>|https?://[^\s<>]+)"
    rf"|(?P<citation>[ \t]*(?:{_BREAK}[ \t]*)?{_CITATION_RUN})"
    rf"|(?P<limits>\[evidence_limits:[ \t]*(?P<fields>{_LIMIT_FIELDS})\])"
    rf"|(?P<break>{_BREAK})"
)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_EMPTY_LIST_ITEM = re.compile(r"\s*(?:[-*+]|\d+[.)])\s*")
_SOURCE_HEADING = re.compile(r"(?:#{1,6}\s+|\*\*)(?:References|Sources):?(?:\*\*)?", re.I)


@dataclass(frozen=True)
class AnswerPresentation:
    """Separate the always-visible body from an optional source disclosure."""

    body: str
    references: str = ""


def format_answer(answer: str) -> str:
    """Return a plain Markdown view for consumers without disclosure controls."""
    view = present_answer(answer)
    if not view.references:
        return view.body
    return (view.body + "\n\n" if view.body else "") + "### References\n\n" + view.references


def present_answer(answer: str) -> AnswerPresentation:
    """Move data citations to a deduplicated reference list, preserving source order.

    Bare HTML breaks become separators so Markdown table rows stay intact. Code
    blocks/spans and links are passed through. Source fields are quoted as code,
    so a field value cannot introduce links, formatting or executable HTML.
    """
    references: dict[str, None] = {}
    fence = ""
    lines: list[str] = []

    for line in answer.splitlines(keepends=True):
        marker = _FENCE.match(line)
        if fence:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*", line):
                fence = ""
            lines.append(line)
        elif marker:
            fence = marker[1]
            lines.append(line)
        elif line.startswith(("    ", "\t")):
            lines.append(line)
        else:
            formatted = _INLINE.sub(lambda match: _replace_inline(match, references), line)
            # Removing a citation-only list item must not leave an empty bullet.
            if formatted != line and _EMPTY_LIST_ITEM.fullmatch(formatted):
                formatted = ""
            lines.append(formatted)
    body = "".join(lines)
    if not references:
        return AnswerPresentation(body)
    body = body.rstrip()
    # Reuse a trailing source heading when the agent already supplied a list.
    last_line = body.rsplit("\n", 1)[-1]
    if _SOURCE_HEADING.fullmatch(last_line.strip()):
        body = body[: -len(last_line)].rstrip()
    sources = "\n".join(f"{i}. {_describe(ref)}" for i, ref in enumerate(references, 1))
    return AnswerPresentation(body, sources)


def _replace_inline(match: re.Match[str], references: dict[str, None]) -> str:
    """Collect only recognized citations, retaining literal Markdown tokens."""
    if match.group("literal"):
        return match[0]
    if match.group("citation"):
        for reference in _REFERENCE.findall(match[0]):
            references.setdefault(reference, None)
        return ""
    if match.group("limits"):
        # Evidence limits are meaningful caveats, not source citations. Retain
        # them in readable prose instead of hiding them with the references.
        fields = []
        for field in match.group("fields").split(";"):
            key, value = field.strip().split("=", 1)
            fields.append(f"{key.replace('_', ' ').capitalize()}: {value.replace('_', ' ')}")
        return "(" + "; ".join(fields) + ")"
    return " · "


def _describe(reference: str) -> str:
    """Show the source table and every identifying field with natural wrap points."""
    table, fields = reference[:-1].split("[", 1)
    details = []
    for field in fields.split(";"):
        key, value = field.split("=", 1)
        label = "UEN" if key == "uen" else key.replace("_", " ").capitalize()
        # Choose a delimiter longer than any literal backticks in the value.
        delimiter = "`" * (1 + max((len(m[0]) for m in re.finditer(r"`+", value)), default=0))
        details.append(f"{label}: {delimiter} {value} {delimiter}")
    return f"**{table.replace('_', ' ').capitalize()}** — " + "; ".join(details)
