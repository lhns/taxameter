# 0005. The instance cap counts the whole account

Status: Accepted

## Context

`vastctl create` only refused when its own current instance was still up. Two projects share one Vast account with a cap of one instance, so each project's copy could rent its own box.

## Decision

Before any rent (`create`, `nightly`, watchdog replacement), taxameter lists every instance on the account, whatever its label, and refuses when `running + loading + created` would exceed `max_instances` (`max_instances_during_migration` for a replacement). `create --allow-second` overrides it for a manual rent. The USD/h cap filters offers before ranking, and `max_box_hours` stops any managed box that runs longer.

## Consequences

- A nightly of one project blocks a rent in another; that is the intent.
- Stopped boxes do not count (they bill only storage), so salvage windows can still start them.
