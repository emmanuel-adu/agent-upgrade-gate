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

# A call takes the highest-ranked category of any command in it: `cd x && cat a.py` is a read.
_RANK = ("submit", "test", "edit", "network", "install", "execute", "vcs", "search", "read")
_BIN_CATEGORY = {
    **dict.fromkeys(("pytest", "tox", "nox"), "test"),
    **dict.fromkeys(("patch", "mv", "cp", "tee", "rm", "mkdir", "touch"), "edit"),
    **dict.fromkeys(("curl", "wget", "nc", "ssh", "scp"), "network"),
    **dict.fromkeys(("bash", "sh", "node", "make"), "execute"),
    "git": "vcs",
    **dict.fromkeys(("grep", "rg", "find", "ls", "tree", "fd", "ag"), "search"),
    **dict.fromkeys(("cat", "head", "tail", "nl", "less", "more", "wc", "sed"), "read"),
}
_PY_WRITES = re.compile(r"open\([^)]*,\s*['\"][wax]|\.write_(text|bytes)\(|shutil\.(copy|move)|os\.(rename|replace|remove)\(")
_REDIRECT = re.compile(r"^[0-9]*(>>?|<<?|&>>?|>&|<&|>\|)")
_SHELL_KEYWORDS = {"if", "then", "elif", "else", "while", "until", "do", "!", "{", "time"}
_SHELL_CLOSERS = {"fi", "done", "esac", "}"}
_SHELL_HEADERS = {"for", "case", "select", "function"}  # the rest of the segment is not a command
_COMMAND_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*$")


def categorize(command: str) -> str:
    if "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in command:
        return "submit"
    tagged, writes = _heredoc_scan(command)
    programs = "\n".join(line for line, role in tagged if role == "program")
    found = {c for raw, words in _commands(command) if (c := _segment_category(raw, words, programs))}
    if any(writes):
        found.add("edit")
    return next((c for c in _RANK if c in found), "other")


def _segment_category(raw: str, words: list[str], programs: str) -> str | None:
    at = _command_index(words)
    if at is None:
        return None
    name, args = os.path.basename(words[at]), words[at + 1 :]
    if _writes_to_file(raw):
        return "edit"
    if re.fullmatch(r"python[0-9.]*", name):
        if any(a == "-m" and b in ("pytest", "unittest") for a, b in zip(args, args[1:], strict=False)):
            return "test"
        # The program is the -c argument or a heredoc body fed to a command.
        return "edit" if _PY_WRITES.search(" ".join(args) + "\n" + programs) else "execute"
    if name in ("pip", "pip3", "npm", "yarn", "apt", "apt-get") and args[:1] and args[0] in ("install", "add"):
        return "install"
    if name == "make" and "test" in args:
        return "test"
    if name == "git" and args[:1] == ["apply"]:
        return "edit"
    if name in ("sed", "perl") and any(a.startswith(("-i", "-pi")) for a in args):
        return "edit"
    return _BIN_CATEGORY.get(name)


_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_OUTPUT_REDIRECT = re.compile(r"(?:^|\s)(?:[0-9]*|&)>>?\|?\s*([^\s<>]+)")


def _writes_to_file(raw: str) -> bool:
    """The segment redirects output (`>`, `>>`, `1>`, `&>`) to a file, not /dev/* or a descriptor.
    Quoted text is blanked first, so `grep ">" a.py` is not a redirect but `> "out file"` still is."""
    unquoted = _QUOTED.sub("''", raw)
    return any(not t.startswith(("/dev/", "&")) for t in _OUTPUT_REDIRECT.findall(unquoted))


def binaries(command: str) -> list[str]:
    """Command run by each pipeline/list segment, e.g. 'cd x && python -m pytest | tail' -> cd, python,
    tail. Shell keywords, redirections, assignments and heredoc bodies are not commands."""
    return [os.path.basename(words[at]) for _, words in _commands(command) if (at := _command_index(words)) is not None]


def _command_index(words: list[str]) -> int | None:
    """Index of the command word, past leading assignments and redirections; None if there is none or
    it doesn't look like a command name (`[`, an empty string, a fragment of a quoted argument)."""
    i = 0
    while i < len(words):
        w = words[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            i += 1
        elif m := _REDIRECT.match(w):
            i += 1 if w[m.end() :] else 2
        else:
            return i if _COMMAND_NAME.match(os.path.basename(w)) else None
    return None


def _commands(command: str) -> list[tuple[str, list[str]]]:
    """(raw text, words) of each simple command, with shell keywords stripped from the words and
    heredoc bodies removed."""
    out: list[tuple[str, list[str]]] = []
    for seg in _split_segments(strip_heredoc_bodies(command, all_bodies=True)):
        try:
            words = shlex.split(seg)
        except ValueError:
            words = seg.split()
        while words and words[0] in _SHELL_KEYWORDS | _SHELL_CLOSERS:
            words = words[1:]
        if words and words[0] not in _SHELL_HEADERS:
            out.append((seg, words))
    return out


def _split_segments(command: str) -> list[str]:
    """Split on |, ||, &&, ;, &, newlines and subshell parens outside quotes. Backslash escapes and
    comments follow bash; `$(...)` stays inside its word."""
    segs: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if quote == "'":
            cur.append(ch)
            quote = None if ch == "'" else quote
        elif ch == "\\" and quote != "'":
            cur.append(" " if command[i + 1 : i + 2] == "\n" and not quote else command[i : i + 2])
            i += 1
        elif quote == '"':
            cur.append(ch)
            quote = None if ch == '"' else quote
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch == "#" and (not cur or cur[-1].isspace()):
            while i + 1 < n and command[i + 1] != "\n":
                i += 1
        elif ch == "$" and command[i + 1 : i + 2] == "(":
            end = _matching_paren(command, i + 1)
            cur.append(command[i:end])
            i = end - 1
        elif ch == "&" and (command[i - 1 : i] in (">", "<") or command[i + 1 : i + 2] == ">"):
            cur.append(ch)  # redirections like 2>&1, &>file
        elif ch in "|;&\n()":
            segs.append("".join(cur))
            cur = []
            if command[i : i + 2] in ("||", "&&", ";;"):
                i += 1
        else:
            cur.append(ch)
        i += 1
    segs.append("".join(cur))
    return [s for s in segs if s.strip()]


def _matching_paren(command: str, open_at: int) -> int:
    """Index just past the `)` closing the `(` at open_at (quote-aware), or len(command)."""
    depth, quote, i = 0, None, open_at
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(command)


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


def strip_heredoc_bodies(command: str, *, all_bodies: bool = False) -> str:
    """Remove heredoc bodies that are file content, keep everything that runs. With all_bodies, also
    drop bodies fed to programs (`python - <<EOF`) and terminators: only the typed shell is left."""
    drop = {"content", "program", "end"} if all_bodies else {"content"}
    return "\n".join(line for line, role in _heredoc_scan(command)[0] if role not in drop)


def _heredoc_scan(command: str) -> tuple[list[tuple[str, str]], list[bool]]:
    """Each line tagged 'shell', 'content' (file body), 'program' (body fed to a command) or 'end',
    plus whether each heredoc writes a file."""
    lines = command.split("\n")
    tagged: list[tuple[str, str]] = []
    writes: list[bool] = []
    i = 0
    while i < len(lines):
        tagged.append((lines[i], "shell"))
        i += 1
        for delim, is_content in _heredocs(tagged[-1][0]):
            writes.append(is_content)
            while i < len(lines) and lines[i].strip() != delim:
                tagged.append((lines[i], "content" if is_content else "program"))
                i += 1
            if i < len(lines):
                tagged.append((lines[i], "end"))
                i += 1
    return tagged, writes


def _snippet(command: str, at: int, width: int = 120) -> str:
    start = max(0, at - 30)
    s = command[start : start + width].replace("\n", "\\n")
    return ("…" if start else "") + s + ("…" if start + width < len(command) else "")
