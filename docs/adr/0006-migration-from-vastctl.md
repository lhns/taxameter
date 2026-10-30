# 0006. Migration from the vastctl copies

Status: Accepted

## Context

Tetraeder's scheduled tasks call `uv run python scripts/vastctl.py nightly` and `watchdog --once` from the main checkout, whose `.vast/` holds live state (managed instances, blocklist, last-good host, the nightly lock).

## Decision

- Projects depend on taxameter through uv: `taxameter = { git = "https://github.com/lhns/taxameter", tag = "vX.Y.Z" }` under `[tool.uv.sources]`. Before a tag exists a local `{ path = "../taxameter", editable = true }` works; switching is one line.
- `state_dir = ".vast"` keeps the old state, lock and fetch archives. The state format stays compatible: old records load, and statuses are normalised (`stopped`, `loading`) as each record is refreshed.
- `scripts/vastctl.py` becomes a three-line shim that calls `taxameter.cli.main()`, so registered scheduled tasks keep working; it can go once the tasks call `uv run taxameter`.
- The on-box files move from `/root/vast_box.sh` and `<repo>/scripts/*.sh` to `/root/.taxameter/`. A box provisioned by the old tool gets the new files on the next command; its old watchdog keeps running until the box is destroyed.

## Consequences

- No flag day: the switch is a merge, and the next scheduled run uses taxameter.
- The shim and the old `.vast` name stay until the projects clean them up.
