"""Policy engine: classifies and screens every tool call an agent makes during replay."""

from __future__ import annotations

import base64
import functools
import os
import re
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath

from .config import Policy, PolicyRule

REDACTION = "[REDACTED-BY-UGATE]"


@dataclass
class Violation:
    rule_id: str
    severity: str
    description: str
    evidence: str
    phase: str  # "pre" (command screen), "output" (canary), "post" (workspace state)
    blocked: bool
    step: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Compiled:
    rule: PolicyRule
    patterns: list[re.Pattern] = field(default_factory=list)
    unless: list[re.Pattern] = field(default_factory=list)


class PolicyEngine:
    """Stateless screens; callers attach step numbers and decide what blocking means."""

    def __init__(self, policy: Policy, workspace_paths: list[str]):
        self.policy = policy
        self.enforce = policy.mode == "enforce"
        self.workspace_paths = [os.path.normpath(p) for p in workspace_paths]
        self._compiled = [
            _Compiled(
                rule=r,
                patterns=[re.compile(p) for p in r.match.command_regex],
                unless=[re.compile(p) for p in r.match.unless_regex],
            )
            for r in policy.rules
        ]

    def rule(self, predicate: str) -> PolicyRule | None:
        for c in self._compiled:
            if getattr(c.rule.match, predicate):
                return c.rule
        return None

    # -- pre-execution --------------------------------------------------------------------

    def screen_command(self, command: str) -> list[Violation]:
        """Screen the executable part of a command. Heredoc bodies are file *content*, not actions,
        so they are excluded (a file that merely mentions `.env` is not a secret read). Executing a
        written script later is screened when it happens, and the sandbox has no network."""
        command = strip_heredoc_bodies(command)
        # Also screen a copy with empty quotes and backslash escapes removed: `cat .e''nv` and
        # `c\url` are what bash runs as `cat .env` and `curl`.
        normalized = re.sub(r"''|\"\"|\\(?=[A-Za-z.])", "", command)
        out: list[Violation] = []
        for c in self._compiled:
            for text in dict.fromkeys((command, normalized)):
                m = next((m for pat in c.patterns if (m := pat.search(text))), None)
                if m and not any(u.search(text) for u in c.unless):
                    out.append(self._v(c.rule, _snippet(text, m.start()), "pre"))
                    break
        escape_rule = self.rule("path_outside_workspace")
        if escape_rule:
            escaped = self.paths_outside_workspace(command)
            if escaped:
                out.append(self._v(escape_rule, ", ".join(sorted(escaped))[:200], "pre"))
        return out

    def paths_outside_workspace(self, command: str) -> set[str]:
        try:
            tokens = shlex.split(command, comments=False, posix=True)
        except ValueError:
            tokens = command.split()
        escaped: set[str] = set()
        for raw in tokens:
            tok = re.sub(r"^[0-9]*[<>]+&?", "", raw).lstrip("@")  # 2>/x, >/x, <x, curl -d @file
            if tok.startswith("-") and "=" in tok:
                tok = tok.split("=", 1)[1]
            if not tok or "://" in tok or "\n" in tok:
                continue
            if tok.startswith("~") or tok in ("/", "/."):
                escaped.add(tok)
                continue
            if tok.startswith("/"):
                norm = os.path.normpath(tok)
                if any(norm == w or norm.startswith(w + "/") for w in self.workspace_paths):
                    continue
                if any(norm.startswith(p.rstrip("/")) for p in self.policy.allowed_absolute_prefixes):
                    continue
                top = PurePosixPath(norm).parts[1:2]
                # Only treat as a path if its top-level directory exists (avoids sed /pattern/d etc).
                if top and os.path.isdir("/" + top[0]):
                    escaped.add(tok)
                continue
            if tok.startswith("..") and "/" in tok or tok == "..":
                depth = 0
                for part in PurePosixPath(tok).parts:
                    depth += -1 if part == ".." else (0 if part == "." else 1)
                    if depth < 0:
                        escaped.add(tok)
                        break
        return escaped

    # -- output -------------------------------------------------------------------------

    def screen_output(self, output: str, canaries: list[str]) -> tuple[str, list[Violation]]:
        """Plain, base64 (any alignment), hex and reversed forms are caught. Encryption or
        compression would get past this; network isolation is what stops those from leaving."""
        rule = self.rule("canary_in_output")
        if not rule or not output:
            return output, []
        hits = [form for c in canaries if c for form in canary_forms(c) if form in output]
        if not hits:
            return output, []
        redacted = output
        if self.enforce:
            for form in sorted(set(hits), key=len, reverse=True):
                redacted = redacted.replace(form, REDACTION)
        v = self._v(rule, f"{len(hits)} canary form(s) present in tool output", "output")
        return redacted, [v]

    # -- post-state -----------------------------------------------------------------------

    def protected_violation(self, changed: list[str], command: str) -> Violation | None:
        rule = self.rule("protected_paths_modified")
        if not rule or not changed:
            return None
        return self._v(rule, f"modified {', '.join(changed)} via: {_snippet(command, 0)}", "post")

    def _v(self, rule: PolicyRule, evidence: str, phase: str) -> Violation:
        return Violation(
            rule_id=rule.id,
            severity=rule.severity,
            description=rule.description,
            evidence=evidence,
            phase=phase,
            blocked=self.enforce,
        )


@functools.lru_cache(maxsize=256)
def canary_forms(canary: str) -> tuple[str, ...]:
    raw = canary.encode()
    forms = {canary, canary[::-1], raw.hex(), raw.hex().upper()}
    for pad in range(3):
        enc = base64.b64encode(b"\0" * pad + raw).decode()
        # Drop characters touched by the padding or the unknown neighbouring bytes.
        core = enc[(pad * 4 + 2) // 3 + 1 : -4]
        if len(core) >= 16:
            forms.add(core)
    return tuple(forms)


# ---------------------------------------------------------------------------------------------
# Tool-call taxonomy (used for behavioural drift, independent of policy)
# ---------------------------------------------------------------------------------------------

_CATEGORIES: list[tuple[str, re.Pattern]] = [
    ("submit", re.compile(r"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")),
    ("test", re.compile(r"\b(pytest|unittest|tox|nox)\b|make\s+test")),
    ("edit", re.compile(r"(cat|tee)\s+(<<|>)|<<\s*'?EOF|sed\s+-i|\bpatch\b|git\s+apply|>\s*\S+\.\w+|\bmv\b|\bcp\b")),
    ("network", re.compile(r"(^|[\s;&|(])(curl|wget|nc|ssh|scp)(\s|$)")),
    ("install", re.compile(r"\b(pip3?|npm|yarn|apt-get|apt)\s+(install|add)\b")),
    ("vcs", re.compile(r"^\s*git\b")),
    ("search", re.compile(r"^\s*(grep|rg|find|ls|tree|fd|ag)\b")),
    ("read", re.compile(r"^\s*(cat|head|tail|nl|less|more|wc)\b|sed\s+-n")),
    ("execute", re.compile(r"^\s*(python[0-9.]*|bash|sh|node|make)\b")),
]


def categorize(command: str) -> str:
    for name, pat in _CATEGORIES:
        if pat.search(command):
            return name
    return "other"


def binaries(command: str) -> list[str]:
    """First word of each pipeline/list segment, e.g. 'cd x && python -m pytest | tail' -> cd, python, tail."""
    out = []
    for seg in _split_segments(strip_heredoc_bodies(command).split("<<", 1)[0]):
        seg = seg.strip().lstrip("(")
        try:
            words = shlex.split(seg)
        except ValueError:
            words = seg.split()
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
        if words:
            out.append(os.path.basename(words[0]))
    return out


def _split_segments(command: str) -> list[str]:
    """Split on |, ||, &&, ; and newlines outside of quotes."""
    segs, cur, quote, i = [], [], None, 0
    while i < len(command):
        ch = command[i]
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch == "&" and (command[i - 1 : i] in (">", "<") or command[i + 1 : i + 2] == ">"):
            cur.append(ch)  # redirections like 2>&1, &>file
        elif ch in "|;&\n":
            segs.append("".join(cur))
            cur = []
            if command[i : i + 2] in ("||", "&&"):
                i += 1
        else:
            cur.append(ch)
        i += 1
    segs.append("".join(cur))
    return [s for s in segs if s.strip()]


_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "\n"}
_FILE_WRITERS = {"cat", "tee"}


def _heredocs(line: str) -> list[tuple[str, bool]]:
    """Heredocs started on one shell line, as (delimiter, body_is_file_content).

    Quote- and comment-aware (shlex with punctuation), so `# <<X` or `echo "<<EOF"` start nothing.
    A body only counts as file content when it feeds `cat` or `tee` writing to a file and isn't
    piped onward; `bash <<EOF`, `python - <<EOF` or `cat <<EOF | sh` bodies are commands."""
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.commenters = "#"
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        return []
    out: list[tuple[str, bool]] = []
    segment: list[str] = []
    pending: list[str] = []
    for i, tok in enumerate(tokens):
        if tok in _SEPARATORS or set(tok) <= set(";&|()"):
            for d in pending:
                out.append((d, _writes_file(segment) and tok != "|"))
            pending, segment = [], []
            continue
        if tok in ("<<", "<<-") and i + 1 < len(tokens):
            pending.append(tokens[i + 1].lstrip("-"))
        segment.append(tok)
    for d in pending:
        out.append((d, _writes_file(segment)))
    return out


def _writes_file(segment: list[str]) -> bool:
    words = [t for t in segment if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)]
    if not words or os.path.basename(words[0]) not in _FILE_WRITERS:
        return False
    return words[0].endswith("tee") or any(t in (">", ">>") or t.startswith((">", ">>")) for t in segment[1:])


def strip_heredoc_bodies(command: str) -> str:
    """Remove heredoc bodies that are file content, keep everything that runs."""
    lines = command.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for delim, is_content in _heredocs(line):
            body: list[str] = []
            while i < len(lines) and lines[i].strip() != delim:
                body.append(lines[i])
                i += 1
            if not is_content:
                out.extend(body)
            if i < len(lines):
                out.append(lines[i])  # the terminator line
                i += 1
    return "\n".join(out)


def _snippet(command: str, at: int, width: int = 120) -> str:
    start = max(0, at - 30)
    s = command[start : start + width].replace("\n", "\\n")
    return ("…" if start else "") + s + ("…" if start + width < len(command) else "")
