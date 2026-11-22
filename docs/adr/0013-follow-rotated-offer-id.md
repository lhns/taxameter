# 0013. Follow a rotated offer id to the same machine

Status: Accepted. Extends ADR 0012 (the retry stays).

## Context

Vast offer ids rotate: between `offers` and `create --offer ID` the id can vanish while the same machine is still on offer under a new id. ADR 0012's retry re-checks the same id and never helps. Vast cannot resolve an id after the fact: the search has no id filter (it ignores or rejects one) and no endpoint reads an offer by id, even for a live offer. The machine of an id is known only while it is listed.

## Decision

1. `offers` writes `offers.json` in the state dir: for every listed offer `{id: {machine_id, num_gpus, gpu_name}}`. Each listing overwrites the file, so it holds the last listing only.
2. When `create --offer ID` finds ID missing, it looks ID up in that file. With a machine, it searches again with the same filters and caps plus `machine_id`, and takes the best-ranked offer with the same GPU count and GPU name. It prints `offer ID rotated: using NEWID (same machine M)` and creates with it.
3. Without a recorded machine or a matching offer, the refusal says the id is gone and suggests `create --auto` with the same filters. The 0012 retry still runs first on that refusal.
4. `Offer` gains `machine_id`; `OfferQuery` gains an optional `machine_id`. No user-facing flag.

## Consequences

- It works only when `offers` ran in the same project state dir before the id rotated; otherwise behavior is as before, with a clearer message.
- The rotated offer passes the same blocklist, price cap and account ceiling checks as any offer.
