---
# agent-upgrade-gate-4laa
title: Calibrate gate thresholds on real-model noise
status: todo
type: task
created_at: 2026-11-21T01:30:32Z
updated_at: 2026-11-21T01:30:32Z
parent: agent-upgrade-gate-j9eu
---

Run an unchanged real config against itself (A/A) at 5 trials and check the false-reject and staged rates against scripts/calibrate.py. Tune gate.yaml if real noise is wider.
