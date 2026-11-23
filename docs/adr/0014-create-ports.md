# 0014. Publish ports on a created box

Status: Accepted.

## Context

`create` sent `env: {}`, so only SSH was reachable. A transport benchmark needed 4 TCP and 2 UDP ports and its driver patched `Api.create`.

## Decision

1. `create` and `nightly` take `--ports SPEC`, repeatable or comma-separated; each spec is `PORT`, `PORT/tcp` or `PORT/udp` (default tcp), 1 to 65535. `22/tcp` is refused (SSH is always published). A bad spec fails before anything is rented. `[box] ports = [...]` is the default; the flag replaces it.
2. The Vast provider publishes a port the way the vastai CLI's `--env "-p 7000:7000/udp"` does: one `env` key `-p PORT:PORT` (suffix `/udp` for UDP) with value `"1"` per port. The provider interface carries `CreateSpec.ports`.
3. The requested ports are kept in the instance record, so a watchdog replacement publishes the same ports. After `create` and in `endpoint`, each requested port prints as `port 7000/udp -> HOST:PORT` (`--json`: `ports`), read from the instance's `ports` map and `public_ipaddr`. Vast picks a new public port on every restart, so `endpoint` is the place to look again.

## Consequences

- Drivers need no monkeypatch and no guessing of the public port.
- The `-p` key format follows the vastai CLI, not a documented REST field; it was not exercised against the live API when written. The first real create with `--ports` should confirm it.
- Offers are not filtered by free host ports beyond the existing `direct_port_count >= 1`; a host with too few direct ports may map fewer ports than requested, shown as "not mapped".
