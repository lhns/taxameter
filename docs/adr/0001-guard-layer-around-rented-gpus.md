# 0001. A guard layer around rented GPU boxes

Status: Accepted

## Context

Tetraeder and LRN each carry a copy of `vastctl.py` (about 2000 lines) plus box scripts, and the copies drift. Renting is the easy part; what the projects need is the layer around it: hard money caps, a watchdog, a host blocklist, API-key redaction, and the flow "push committed HEAD, run detached, wait on the box, fetch with a DONE marker, destroy". More providers are wanted.

## Decision

Extract the tool into one package, `taxameter`, published from `github.com/lhns/taxameter`. Python 3.11+, one runtime dependency (httpx), CLI `taxameter`. Projects add it as a uv dependency and run `uv run taxameter ...`. Behaviour is ported from Tetraeder's copy (the newer one); LRN has no generic fix Tetraeder lacks. Project specifics (image, pushed paths, hooks, scripts, caps) live in the project's `taxameter.toml` (ADR 0004).

## Consequences

- One place for fixes and incident lessons (seed blocklist, lock races, redaction).
- Projects pin a git tag; an upgrade is a one-line change plus `uv lock`.
- The package must stay small and offline-testable: no provider SDKs, no cloud calls in tests.
