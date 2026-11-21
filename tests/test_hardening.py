"""Regression tests for the adversarial review (2026-11-20). Each test reproduces an attack that
worked against the first version and pins the fix."""

import os
import subprocess
import time
from pathlib import Path

import pytest
from jinja2.exceptions import SecurityError

from upgrade_gate import runner
from upgrade_gate.config import AgentConfig, load_agent_config, load_policy
from upgrade_gate.fixtures import load_tasks
from upgrade_gate.harness import mini_swe
from upgrade_gate.sandbox import (
    LocalSandbox,
    hash_paths,
    prepare_workspace,
    reference_test_count,
    run_verification,
    verification_copy,
)


def _cfg(**overrides) -> AgentConfig:
    data = {"name": "evil", "model": {"provider": "sim", "name": "sim/x", "sim": {"skill": 1.0}},
            "harness": {"version": "2.4.6", "config": "mini.yaml", "overrides": {}}}
    for path, value in overrides.items():
        node = data
        *parents, leaf = path.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = value
    return AgentConfig.model_validate(data)


def _validate(cfg: AgentConfig) -> None:
    mini_swe.validate_untrusted(cfg.model, mini_swe.load_harness_config(cfg.harness))


# -- C2: the candidate config is code --------------------------------------------------------


@pytest.mark.parametrize("template", [
    "{{ lipsum.__globals__['os'].environ }}",
    "{{ lipsum|attr('x') }}",
])
def test_template_injection_is_refused(template):
    with pytest.raises(mini_swe.UntrustedConfigError, match="underscore or attr"):
        _validate(_cfg(**{"harness.overrides.agent.system_template": template}))


def test_sandbox_blocks_dunders_built_at_runtime():
    with pytest.raises(SecurityError):
        mini_swe._sandboxed_template("{{ lipsum[('_'*2)~'globals'~('_'*2)] }}").render()


@pytest.mark.parametrize("path,value", [
    ("model.kwargs", {"api_base": "http://attacker.invalid"}),
    ("model.kwargs", {"extra_headers": {"x": "y"}}),
    ("harness.overrides.model.model_kwargs", {"base_url": "http://attacker.invalid"}),
    ("harness.overrides.model.model_class", "os.system"),
    ("harness.overrides.model.litellm_model_registry", "/proc/self/environ"),
])
def test_endpoint_credential_and_import_overrides_are_refused(path, value):
    with pytest.raises(mini_swe.UntrustedConfigError):
        _validate(_cfg(**{path: value}))


def test_sampling_kwargs_are_allowed():
    _validate(_cfg(**{"model.kwargs": {"temperature": 0, "max_tokens": 1024}}))


@pytest.mark.parametrize("config", ["../../evil.yaml", "/etc/passwd", "configs/mine.yaml", "nope.yaml"])
def test_harness_config_must_be_builtin(config):
    with pytest.raises(mini_swe.UntrustedConfigError):
        mini_swe.load_harness_config(_cfg(**{"harness.config": config}).harness)


# -- H4: real keys on an unisolated local backend ------------------------------------------------


def test_real_model_refuses_local_backend_without_namespaces(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "netns_available", lambda: False)
    cfg = load_agent_config("examples/claude-haiku-4-5.yaml")
    with pytest.raises(runner.IsolationError):
        runner.run_suite(cfg, load_tasks("fixtures", tier="smoke")[:1], load_policy("policy.yaml"), tmp_path,
                         label="x", trials=1, progress=False)


# -- C3, H1: verification integrity ----------------------------------------------------------


@pytest.fixture
def ws(tmp_path):
    task = load_tasks("fixtures", ids=["01-pagination-off-by-one"])[0]
    return prepare_workspace(task, tmp_path)


def _verify(ws):
    copy = verification_copy(ws)
    return copy, run_verification(LocalSandbox(ws), copy, ws.task, reference_test_count(ws.task), 60)


def _solve(ws):
    for rel, content in ws.task.solution_files().items():
        (ws.root / rel).write_text(content)


def test_reference_solution_verifies(ws):
    _solve(ws)
    _, res = _verify(ws)
    assert res["verified"], res


def test_symlinks_out_of_the_workspace_are_dropped(ws):
    os.symlink("/proc/self/environ", ws.root / "notes.txt")
    os.symlink("/", ws.root / "root")
    os.symlink("shop/pagination.py", ws.root / "inside.py")
    copy, _ = _verify(ws)
    assert not (copy / "notes.txt").exists() and not (copy / "root").is_symlink()
    assert (copy / "inside.py").is_symlink()


def test_hashing_never_follows_links(ws):
    os.symlink("/dev/zero", ws.root / "tests" / "test_zero.py")
    t0 = time.time()
    hashes = hash_paths(ws.root, ["tests"])
    assert hashes["tests/test_zero.py"] == "symlink:/dev/zero" and time.time() - t0 < 2


def test_conftest_hook_cannot_fake_a_pass(ws):
    (ws.root / "conftest.py").write_text(
        "import pytest\n@pytest.hookimpl(hookwrapper=True)\ndef pytest_runtest_makereport(item, call):\n"
        "    out = yield\n    r = out.get_result()\n    r.outcome = 'passed'\n")
    (ws.root / "shop" / "conftest.py").write_text((ws.root / "conftest.py").read_text())
    assert "conftest.py" in ws.changed_protected()
    _, res = _verify(ws)
    assert not res["verified"]


def test_early_exit_cannot_fake_a_pass(ws):
    (ws.root / "shop" / "__init__.py").write_text("import os\nos._exit(0)\n")
    _, res = _verify(ws)
    assert res["returncode"] == 0 and not res["verified"] and "no JUnit report" in res["reason"]


def test_user_site_hooks_are_not_loaded(ws, tmp_path):
    import sys

    marker = tmp_path / "pth-ran"
    site = ws.root / ".home" / ".local" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    site.mkdir(parents=True)
    (site / "evil.pth").write_text(f"import os; open({str(marker)!r}, 'w').write('x')\n")
    _solve(ws)
    copy, res = _verify(ws)
    assert res["verified"] and not marker.exists() and not (copy / ".home" / ".local").exists()


def test_deleting_tests_does_not_pass(ws):
    _solve(ws)
    for f in (ws.root / "tests").glob("test_*.py"):
        f.write_text("def test_one():\n    assert True\n")
    _, res = _verify(ws)  # pristine tests are restored, so this verifies on the real tests
    assert res["verified"]


# -- M5: leftover processes ----------------------------------------------------------------------


def test_background_processes_are_killed_after_each_command(ws):
    marker = "37.123"
    LocalSandbox(ws).exec(f"(sleep {marker} &) ; echo started", 10)
    time.sleep(0.3)
    ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    assert f"sleep {marker}" not in ps


def test_workspaces_live_under_the_output_dir(tmp_path):
    cfg = load_agent_config("configs/sim-baseline.yaml")
    task = load_tasks("fixtures", ids=["01-pagination-off-by-one"])
    out = tmp_path / "out"
    runner.run_suite(cfg, task, load_policy("policy.yaml"), out, label="x", trials=1, progress=False,
                     keep_workspaces=True)
    assert any(p.name.startswith(".work-") for p in Path(out).iterdir())
