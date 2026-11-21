---
# agent-upgrade-gate-c19p
title: Separate provider retry time from latency
status: draft
type: task
created_at: 2026-11-21T01:30:32Z
updated_at: 2026-11-21T01:30:32Z
parent: agent-upgrade-gate-8q32
---

Latency is wall time per call including litellm retries, so throttling reads as a latency regression. Record retries and time-to-response separately.
