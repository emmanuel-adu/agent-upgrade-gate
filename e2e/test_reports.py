"""End-to-end checks for generated HTML reports (adapted from build-kit tools/e2e_template.py).

Opens every report under a directory in headless Chrome at desktop and phone widths, in light and
dark mode, and fails on page errors, console errors, failed or external requests, horizontal
overflow, or missing structure. Screenshots go to e2e/screenshots/.

  python e2e/test_reports.py demo            # every */report.html below demo/
  python e2e/test_reports.py demo --chrome /usr/bin/google-chrome
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

VIEWPORTS = {"desktop": (1440, 900), "phone": (390, 844)}
SHOTS = Path("e2e/screenshots")
DECISIONS = {"Approve", "Staged rollout", "Reject"}


def journey(page: Page, expected_decision: str | None) -> list[tuple[str, bool, str]]:
    results = []
    h1 = page.locator("h1")
    results.append(("one h1 naming both configs", h1.count() == 1 and "→" in h1.inner_text(), h1.first.inner_text()))
    decision = page.locator(".decision").inner_text().strip()
    results.append(("decision pill shows a known decision", decision.title() in {d.title() for d in DECISIONS}, decision))
    if expected_decision:
        results.append(("decision matches comparison.json", decision.lower() == expected_decision.lower(), decision))
    checks = page.locator("section:has(h2:text('Gate checks')) tbody tr").count()
    results.append(("all nine gate checks listed", checks == 9, f"{checks} rows"))
    pills = page.locator("section:has(h2:text('Gate checks')) .pill").all_inner_texts()
    results.append(("every check has a pass/warn/fail pill", all(p.lower() in {"pass", "warn", "fail"} for p in pills), ",".join(pills)))
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    results.append(("no horizontal page overflow", overflow <= 0, f"{overflow}px"))
    loose = page.evaluate("[...document.querySelectorAll('table')].filter(t => !t.closest('.scroll')).length")
    results.append(("every table sits in a scroll region", loose == 0, f"{loose} loose"))
    text = page.locator("main").inner_text()
    results.append(("no em dashes in rendered text", "—" not in text, ""))
    return results


def keyboard_check(page: Page) -> tuple[str, bool, str]:
    page.keyboard.press("Tab")
    info = page.evaluate("(() => { const a = document.activeElement; return a ? a.getAttribute('aria-label') || a.tagName : null })()")
    return ("Tab reaches a labelled table region", bool(info) and info != "BODY", f"focused {info}")


def run(root: Path, chrome: str | None) -> dict:
    SHOTS.mkdir(parents=True, exist_ok=True)
    reports = sorted(root.rglob("report.html"))
    summary: dict = {"reports": [str(r) for r in reports], "checks": [], "errors": []}
    if not reports:
        summary["errors"].append(f"no report.html under {root}")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chrome) if chrome else p.chromium.launch()
        for report in reports:
            cmp_file = report.parent / "comparison.json"
            expected = None
            if cmp_file.exists():
                expected = {"APPROVE": "Approve", "STAGED_ROLLOUT": "Staged rollout", "REJECT": "Reject"}[
                    json.loads(cmp_file.read_text())["decision"]]
            name = report.parent.name
            for vp, (w, h) in VIEWPORTS.items():
                for scheme in ("light", "dark"):
                    ctx = browser.new_context(viewport={"width": w, "height": h}, color_scheme=scheme)
                    page = ctx.new_page()
                    label = f"{name}-{vp}-{scheme}"
                    errors: list[str] = []
                    page.on("pageerror", lambda e, errors=errors: errors.append(f"pageerror: {e}"))
                    page.on("console", lambda m, errors=errors: errors.append(f"console: {m.text}") if m.type == "error" else None)
                    page.on("request", lambda r, errors=errors: None if r.url.startswith(("file:", "data:")) else errors.append(f"external request: {r.url}"))
                    page.on("requestfailed", lambda r, errors=errors: errors.append(f"failed: {r.url}"))
                    page.goto(report.resolve().as_uri(), wait_until="load")
                    page.screenshot(path=str(SHOTS / f"{label}.png"), full_page=False)
                    if vp == "phone" and scheme == "light":
                        page.screenshot(path=str(SHOTS / f"{label}-full.png"), full_page=True)
                    for check, ok, note in journey(page, expected) + [keyboard_check(page)]:
                        summary["checks"].append({"report": name, "viewport": f"{vp}-{scheme}", "check": check,
                                                  "passed": ok, "note": note})
                    summary["errors"] += [f"{label}: {e}" for e in dict.fromkeys(errors)]
                    ctx.close()
        browser.close()
    summary["passed"] = bool(reports) and all(c["passed"] for c in summary["checks"]) and not summary["errors"]
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path, help="directory searched for report.html files")
    ap.add_argument("--chrome", default=None, help="Chrome executable (default: Playwright's Chromium)")
    a = ap.parse_args()
    result = run(a.root, a.chrome)
    Path("e2e/results.json").write_text(json.dumps(result, indent=2))
    failed = [c for c in result["checks"] if not c["passed"]]
    for c in failed:
        print(f"FAIL {c['report']:<22} {c['viewport']:<14} {c['check']}  {c['note']}")
    for e in result["errors"]:
        print("ERROR", e)
    print(f"{len(result['checks']) - len(failed)}/{len(result['checks'])} checks passed across "
          f"{len(result['reports'])} reports x 4 views -> {'PASSED' if result['passed'] else 'FAILED'}")
    sys.exit(0 if result["passed"] else 1)
