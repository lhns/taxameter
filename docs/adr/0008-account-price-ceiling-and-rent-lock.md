# 0008. Account price ceiling and rent lock

Status: Accepted. Complements 0007.

## Context

ADR 0007 caps the number of boxes per project. Several projects share one Vast account, so nothing bounds the account's total USD/h except each project's own caps multiplied by the number of projects. Two local projects renting at the same moment can also both read the same running boxes and both pass a check that only one of them should.

## Decision

- Per-project count cap (0007) stays the primary guard.
- Optional `[policy] max_account_price_per_hour` (no default: off). At every rent (`create`, `nightly`, watchdog replacement, all through `core.rent`), the `price_per_h` of all running + loading instances on the account, any label, plus the chosen offer's price must not exceed it. Otherwise the rent is refused with a message listing those boxes, their labels and prices. With offer fall-through the check runs per offer tried, so a cheaper later offer can still fit. `create --allow-second` does not skip it: it is a money cap, not a count cap.
- Rents are serialised on one machine by an exclusive file lock at `$XDG_CONFIG_HOME/taxameter/rent.lock` (default `~/.config/taxameter/rent.lock`), the same OS lock as `nightly.lock` (`FileLock`: msvcrt on Windows, flock elsewhere; it dies with the process). It is taken before the cap checks and held until the create call returns; a rent waits up to 60 s for it, then refuses.
- `[offers] max_ram_gb` (optional) drops offers whose host RAM (`Offer.ram_gb`, Vast `cpu_ram` in MB) exceeds it, or is unknown. Its use is making a model not fit in page cache for benchmarks.

## Consequences

- Total account spend is bounded when the ceiling is set, by the ceiling plus at most one box.
- The lock covers one machine. Two machines (or a console rent) can race: the overshoot is at most one box per racing machine, and the next rent sees it.
- Boxes of other projects count toward the price sum but never toward a project's count cap.
- Offers from providers that do not report RAM are dropped when `max_ram_gb` is set.
