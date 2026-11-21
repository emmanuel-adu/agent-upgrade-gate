"""Task fixtures: small, hermetic engineering tasks with a verification command and a reference
solution. A fixture is only useful if it is *discriminating*: its checks must fail on the starting
repo and pass with the reference solution. `validate_task` proves both."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

DEFAULT_VERIFY = "python -m pytest -q -p no:cacheprovider"


class Injection(BaseModel):
    """Marks a fixture that carries a prompt-injection payload inside the repo."""

    source: str
    """File in the repo that contains the injected instruction."""
    payload_command: str
    """The command the injection tries to get the agent to run (used by the simulated model)."""


class Task(BaseModel):
    id: str
    title: str
    prompt: str
    tags: list[str] = Field(default_factory=list)
    tier: Literal["smoke", "full"] = "full"
    verify: str = DEFAULT_VERIFY
    timeout_s: int = 60
    protected_paths: list[str] = Field(default_factory=lambda: ["tests"])
    files_of_interest: list[str] = Field(default_factory=list)
    injection: Injection | None = None
    tamper_target: str | None = None
    """For tamper-bait fixtures: the protected test file a shortcut-taking agent would edit."""
    dir: Path = Field(exclude=True)

    @property
    def repo_dir(self) -> Path:
        return self.dir / "repo"

    @property
    def solution_dir(self) -> Path:
        return self.dir / "solution"

    def solution_files(self) -> dict[str, str]:
        return {
            str(p.relative_to(self.solution_dir)): p.read_text()
            for p in sorted(self.solution_dir.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts
        }

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for p in sorted(self.dir.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                h.update(str(p.relative_to(self.dir)).encode())
                h.update(p.read_bytes())
        return h.hexdigest()[:12]


def load_tasks(
    fixtures_dir: str | Path,
    ids: list[str] | None = None,
    tier: str | None = None,
    tags: list[str] | None = None,
) -> list[Task]:
    root = Path(fixtures_dir)
    tasks = []
    for f in sorted(root.glob("*/task.yaml")):
        data = yaml.safe_load(f.read_text())
        task = Task.model_validate({**data, "dir": f.parent})
        if ids and task.id not in ids:
            continue
        if tier == "smoke" and task.tier != "smoke":
            continue
        if tags and not set(tags) & set(task.tags):
            continue
        tasks.append(task)
    if ids:
        missing = set(ids) - {t.id for t in tasks}
        if missing:
            raise ValueError(f"unknown task ids: {sorted(missing)}")
    return tasks


def suite_fingerprint(tasks: list[Task]) -> str:
    return hashlib.sha256("".join(t.id + t.fingerprint() for t in tasks).encode()).hexdigest()[:12]


def validate_task(task: Task) -> dict:
    """Return {'ok', 'fails_before', 'passes_after', 'detail'} for one fixture."""
    from .sandbox import LocalSandbox, run_verification  # local import: sandbox depends on this module

    problems = []
    if not task.repo_dir.is_dir():
        problems.append("missing repo/")
    if not task.solution_dir.is_dir():
        problems.append("missing solution/")
    for rel in task.protected_paths:
        if not (task.repo_dir / rel).exists():
            problems.append(f"protected path {rel} does not exist")
    for rel in task.solution_files():
        if any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in task.protected_paths):
            problems.append(f"solution modifies protected path {rel}")
    if task.injection and not (task.repo_dir / task.injection.source).exists():
        problems.append(f"injection source {task.injection.source} missing")
    if problems:
        return {"id": task.id, "ok": False, "fails_before": None, "passes_after": None, "detail": "; ".join(problems)}

    with tempfile.TemporaryDirectory(prefix=f"validate-{task.id}-") as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(task.repo_dir, work)
        (work / ".home").mkdir()

        class _WS:  # minimal stand-in for sandbox.Workspace
            root = work
            canaries: list[str] = []

        box = LocalSandbox(_WS)  # type: ignore[arg-type]
        before = run_verification(box, work, task, None, task.timeout_s)
        for rel, content in task.solution_files().items():
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_text(content)
        after = run_verification(box, work, task, None, task.timeout_s)

    fails_before = not before["verified"]
    passes_after = after["verified"]
    detail = "" if passes_after else f"{after['reason']}: {after['output'][-600:]}"
    if not fails_before:
        detail = "verification passes on the unmodified repo (fixture is not discriminating)"
    return {
        "id": task.id,
        "ok": fails_before and passes_after,
        "fails_before": fails_before,
        "passes_after": passes_after,
        "detail": detail,
    }
