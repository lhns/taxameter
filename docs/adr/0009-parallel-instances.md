# 0009. Several boxes of one project at once

Status: Accepted. Complements 0007.

## Context

ADR 0007 caps a project's boxes at `max_instances`. With a value above 1, parallel agents each want to own one box and drive it with its own CLI process (`--id`). Three things stopped that: `state.json` was loaded once per process and rewritten whole, so two processes lost each other's updates; the state held a single `batch` and a single `need_replacement`; and `create` and `adopt` always took over `current`.

## Decision

1. **Locked read-modify-write state.** Every mutation runs in `with store.update():`. It takes an exclusive lock on `<state_dir>/state.lock` (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows, waits up to 30 s), reloads `state.json`, runs the body, writes it (tmp + `os.replace`) and releases. The body is short: no network calls, no waits. The lock is re-entrant within a process. `Store.patch(iid, **fields)` is the shorthand for field updates. Reads use the snapshot loaded at start or at the last update or `refresh()`; code never holds a record across an update. `events.log` stays a line append.
2. **Per-instance batch.** `state["batches"][<instance id>]` replaces `state["batch"]`; `run`, `nightly`, `fetch`, `destroy`, the watchdog and `status` use the batch of the instance in question. `destroy` drops the box's batch. A replacement moves the old box's batch to the new box. An old `batch` key is migrated on load to the instance it names. `need_replacement` likewise moves from the state into the record of the box that needs replacing, so several boxes can wait for one at once.
3. **`current` is only the default.** A command given `--id` uses exactly that id and never falls back to `current`. `create`, `adopt` and a watchdog replacement set `current` only when none is set (a replacement also when it pointed at the replaced box); `create` prints the new id and `--json` includes `id` and `current`. With several boxes, always pass `--id`.
4. **Watchdog and status cover every instance of the project** (the watchdog already iterated all records; each box has its own idle, age, outage, batch and replacement handling).

`max_instances` (default 1), `max_price_per_hour` and `max_account_price_per_hour` are unchanged and apply to every create.

## Consequences

- N processes can add events, boxes and batches to one state dir without losing any.
- `nightly.lock` stays project-wide: a `nightly` blocks the watchdog tick and another `nightly`, but not `create`, `run`, `fetch` or `destroy` on other boxes.
- The watchdog is project-wide: it stops and destroys idle or old boxes of every agent, by the same rules as before.
- A crashed process never leaves the lock held (the OS drops it). A stale snapshot can only be read, never written back.
- The lock covers one machine and one state dir, like the rent lock (0008).
