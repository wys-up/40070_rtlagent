"""Load SKILL.md files and select at most two skills per generation round.

Front matter supplies name and description. Backticked Log signatures are
matched case-insensitively against the task statement and previous tool feedback.
A broad sequential-timing fallback has lower priority than direct matches.
"""

from __future__ import annotations

import os
import re


class Skill:
    def __init__(self, name: str, description: str, signatures: list[str],
                 trigger: str, body: str, path: str):
        self.name = name
        self.description = description
        self.signatures = signatures
        self.trigger = trigger
        self.body = body
        self.path = path

    def matches(self, *texts: str) -> bool:
        return self.match_score(*texts)[0] > 0

    def match_score(self, *texts: str) -> tuple[int, int]:
        if not self.signatures:
            return (0, 0)
        haystack = "\n".join(t.lower() for t in texts if t)
        hits = [sig for sig in self.signatures if sig.lower() in haystack]
        if hits:
            return (2 + len(hits), max(map(len, hits)))
        if self.name == "rtl-sequential-timing":
            broad_match = bool(re.search(
                r"\b(?:synchronous|asynchronous|posedge|negedge|moore|mealy|"
                r"state machine|finite.state|clock edge|rising edge|falling edge|"
                r"serial|counter|shift register)\b", haystack))
            return (1, 0) if broad_match else (0, 0)
        return (0, 0)

    def render(self, budget: int = 12000) -> str:
        return f'<skill name="{self.name}">\n{_trim(self.body, budget)}\n</skill>'


def load_skills(skill_dir: str) -> list[Skill]:
    skills: list[Skill] = []
    if not os.path.isdir(skill_dir):
        return skills

    for entry in sorted(os.listdir(skill_dir)):
        path = os.path.join(skill_dir, entry, "SKILL.md")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            continue

        meta, body = _split_front_matter(raw)
        skills.append(
            Skill(
                name=meta.get("name", entry),
                description=meta.get("description", ""),
                signatures=_bullet_code_items(body, "Log signatures"),
                trigger=_bullet_text(body, "Trigger"),
                body=body,
                path=path,
            )
        )
    return skills


def select(skills: list[Skill], *context: str, limit: int = 2) -> list[Skill]:
    matched = [(s.match_score(*context), s) for s in skills]
    matched = [(score, skill) for score, skill in matched if score[0] > 0]
    matched.sort(key=lambda item: (-item[0][0], -item[0][1], item[1].name))
    return [skill for _, skill in matched[:limit]]


# ------------------------------------------------------------- truncation

def _trim(body: str, budget: int) -> str:
    """Drop whole trailing sections rather than cutting mid-sentence.

    Cutting at a character offset is worse than it looks. A skill's last two
    sections are usually the output format and the behavioural constraints --
    exactly the parts that keep a model from freelancing. Losing them silently
    turns a precise skill into vague advice.

    So: cut at a `## ` boundary, and say out loud which sections went.
    """
    if len(body) <= budget:
        return body

    sections = re.split(r"(?=^## )", body, flags=re.MULTILINE)
    kept: list[str] = []
    used = 0
    for sec in sections:
        if used + len(sec) > budget and kept:
            break
        kept.append(sec)
        used += len(sec)

    dropped = [
        s.splitlines()[0].lstrip("# ").strip()
        for s in sections[len(kept):] if s.strip()
    ]
    note = f"\n\n... [skill truncated, sections omitted: {', '.join(dropped)}]"
    return "".join(kept).rstrip() + note


# --------------------------------------------------------- Skill Metadata

def _bullet_code_items(body: str, label: str) -> list[str]:
    """Backticked items from a `- **<label>:** \\`a\\`, \\`b\\`` bullet."""
    line = _bullet_text(body, label)
    return re.findall(r"`([^`]+)`", line)


def _bullet_text(body: str, label: str) -> str:
    m = re.search(rf"^\s*[-*]\s*\*\*{re.escape(label)}:?\*\*\s*(.+)$",
                  body, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else ""


# ------------------------------------------------------------ front matter

def _split_front_matter(raw: str) -> tuple[dict, str]:
    """Parse the two scalar keys the official format uses.

    A real YAML parser would be one more dependency to bake into the image for
    two fields. If your skills need richer front matter, add pyyaml -- but add it
    to requirements.txt, because the sandbox cannot download it.
    """
    if not raw.lstrip().startswith("---"):
        return {}, raw.strip()

    m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", raw, re.DOTALL)
    if not m:
        return {}, raw.strip()

    head, body = m.group(1), m.group(2)
    meta: dict = {}
    key: str | None = None
    block: list[str] = []

    for line in head.splitlines():
        if not line.strip():
            continue
        # Continuation of a folded/literal block, or a wrapped value.
        if line[0] in " \t" and key:
            block.append(line.strip())
            continue

        if key and block:
            meta[key] = " ".join(block).strip()
            block = []
        key = None

        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if v in ("|", ">", "|-", ">-", ""):
            key = k                       # value continues on indented lines
        else:
            meta[k] = v.strip("\"'")

    if key and block:
        meta[key] = " ".join(block).strip()

    return meta, body.strip()
