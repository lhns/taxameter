# 0011. GPU count filter for offers and create

Status: Accepted.

## Context

The Vast query hard-coded `num_gpus eq 1`, so a project could only rent single-GPU boxes. Multi-GPU work (2x and 4x boxes) needs the count as a parameter.

## Decision

1. **Exact count**: `offers` and `create` take `--num-gpus N`; the default comes from the new `[offers] num_gpus` (default 1). The Vast query always sends `num_gpus: {"eq": N}`, so an unset option sends `eq 1`, exactly as before.
2. **Prices stay whole-box**: `dph_total` is the all-in price of the box. `--max-price`, `max_price_per_hour`, the account ceiling and the runway check compare it unchanged. Score is not divided by the GPU count.
3. **Explicit offer**: `create --offer ID` refuses an offer whose GPU count differs from the requested one, with a clear error. Since the search already filters by count, an offer with another count also shows up as "not among the filtered offers", and the message names the count as a cause.
4. **Display**: `Offer` and `Instance` gain `num_gpus` (from the row, default 1). `offers` and `status` print `Nx GPU name`; `--json` carries `num_gpus`.

## Consequences

- Default behaviour is unchanged.
- A project renting 4 GPUs raises its own `max_price_per_hour`; the account ceiling still bounds the total.
- `nightly` and replacement use `[offers] num_gpus` (no flag), so a project that sets it gets the same box shape throughout.
