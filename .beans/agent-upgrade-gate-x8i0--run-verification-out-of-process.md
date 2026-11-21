---
# agent-upgrade-gate-x8i0
title: Run verification out of process
status: draft
type: task
created_at: 2026-11-21T01:30:32Z
updated_at: 2026-11-21T01:30:32Z
parent: agent-upgrade-gate-8q32
---

Agent code runs inside the pytest process during verification and could subvert it from within. A results collector in a separate process (or per-test subprocesses) would close that.
