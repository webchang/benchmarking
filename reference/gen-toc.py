#!/usr/bin/env python3
"""Regenerate the table of contents in a hand-maintained markdown doc.

Usage: gen-toc.py <file.md> [--check]

Rewrites whatever sits between the `<!-- toc -->` and `<!-- /toc -->` markers with a nested list of
the document's `##` / `###` headings and GitHub-style anchors. `--check` exits non-zero instead of
writing, so CI can catch a TOC that has drifted from the headings.

A hand-typed TOC in a doc that keeps growing goes stale silently, which is worse than having none —
hence a generator rather than a one-off edit. Headings inside fenced code blocks are skipped, since
a `#` there is a shell comment, not a heading.
"""
import pathlib
import re
import sys

START, END = "<!-- toc -->", "<!-- /toc -->"


def anchor(heading: str) -> str:
    """GitHub's slug rules: strip markdown, lowercase, drop punctuation, spaces -> hyphens.

    Note the double hyphen this legitimately produces for "limits & error codes" — the `&` is
    dropped and both surrounding spaces become hyphens. That matches GitHub, so leave it.
    """
    h = heading.replace("`", "")
    h = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", h)          # [text](url) -> text
    h = h.lower()
    h = re.sub(r"[^a-z0-9 _-]", "", h)                      # drop punctuation, em dashes, etc.
    return h.strip().replace(" ", "-")


def headings(text: str) -> list[tuple[int, str]]:
    out, in_fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{2,3}) +(.*?)\s*$", line)
        if m:
            out.append((len(m.group(1)), m.group(2)))
    return out


def build(text: str) -> str:
    lines = ["**Contents**", ""]
    for level, title in headings(text):
        indent = "  " * (level - 2)
        lines.append(f"{indent}- [{title}](#{anchor(title)})")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    path = pathlib.Path(argv[0])
    check = "--check" in argv[1:]
    text = path.read_text()
    if START not in text or END not in text:
        print(f"error: {path} has no {START} / {END} markers", file=sys.stderr)
        return 2
    head, rest = text.split(START, 1)
    _stale, tail = rest.split(END, 1)
    new = f"{head}{START}\n\n{build(text)}\n\n{END}{tail}"
    if new == text:
        print(f"{path}: table of contents is up to date")
        return 0
    if check:
        print(f"{path}: table of contents is STALE — run reference/gen-toc.py {path}",
              file=sys.stderr)
        return 1
    path.write_text(new)
    print(f"{path}: table of contents regenerated ({len(headings(text))} headings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
