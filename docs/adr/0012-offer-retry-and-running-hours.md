# 0012. Explicit offer retry and running-hours cap

Status: Accepted.

## Context

1. `create --offer ID` once failed twice with "not among the filtered offers" seconds after `offers` listed the offer, then worked. Vast search results flicker, and an agent had to script a retry loop.
2. `max_box_hours` counted from the provider's start date (else the create time). A box stopped for a decision and started again was then refused new work by `run`, or stopped by the watchdog at once. Hours spent stopped are not GPU hours.

## Decision

1. **Offer retry**: when `--offer` is set and the offer is missing from the search, `rent` searches again `[policy] offer_retries` times (default 2), `offer_retry_s` seconds apart (default 10). Only "missing" retries; a wrong GPU count is refused at once. Each retry logs one line. After the last one the refusal keeps its message and adds "after N searches". The rent lock (ADR 0008) is taken per attempt and released before each wait, so a waiting rent never blocks other projects.
2. **Running-time ledger**: each instance record keeps `running_s` (accumulated) and `running_since` (set while running). `create` starts it; `start` opens and `stop` closes it at the request; health bookkeeping closes it when the API reports `stopped` and opens it when the API reports running or loading. `box_age_h`, `age_notice` and the watchdog's max-hours stop use `running_s` plus the open interval. Messages and `status` say "running".
3. **Unknowns count as running**: a change between two observations is booked at the time it was seen, with no sub-tick guess. A record without a ledger (old state, adopted box) uses the old provider start date until it is first observed; the ledger then starts as running since the recorded create time.

## Consequences

- A flickering offer rents without a script; a truly gone offer costs two extra searches and 20 s.
- A stopped and restarted box keeps only its running hours against the cap. A stop that was missed (the box stopped between ticks) is booked late, so the cap errs high, never low.
- An old record first seen stopped keeps its whole age as running time: the cap stays a cost guard.
