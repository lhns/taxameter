# 0007. The instance cap counts this project's instances

Status: Accepted. Supersedes 0005.

## Context

ADR 0005 counted every instance on the account, so one project's box blocked another's rent. In practice the projects sharing an account are independent, and a box left up by one (or by an experiment outside taxameter) stopped the other's nightly with no way to tell whether the cap or a stale box was to blame.

## Decision

`check_cap` (create, nightly, watchdog replacement) counts an instance as this project's if its label equals `[project] name` (taxameter sets that label on every instance it creates) or its id is in the project's state store (an adopted box). `running + loading` of those, plus the new one, must not exceed `max_instances` (default 1; `max_instances_during_migration`, default 2, for a replacement while the old box is up). Other boxes do not count. The refusal names each blocking box with its label and status.

`adopt` relabels the instance to the project name when its label differs (new provider method `set_label`, Vast: `PUT /api/v0/instances/{id}/` with `{"label": ...}`), so later cap checks match by label even from another checkout.

`[project] name` is required and non-empty. The label is the cap's identity: two projects that both omitted the name would share one label and one cap. `create --allow-second` still overrides the check for a manual rent.

`[policy] max_box_hours` has no default and must be set (config load fails without it): with caps per project, the per-box time limit is the project's own spend bound and should be a conscious choice.

## Consequences

- No cross-project cap any more. Total spend across projects is bounded only by each project's own caps (`max_price_per_hour`, `max_instances`, `max_box_hours`) multiplied by the number of projects.
- A box rented before the migration under a different label is not counted until it is adopted (which relabels it) or destroyed.
- An adopted box in the state store keeps blocking the cap while up, whatever its label; the state store is per checkout, so another checkout counts it only after the relabel.
- Stopped boxes still do not count.
