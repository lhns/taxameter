# 0002. A small provider interface

Status: Accepted

## Context

`vastctl` reads raw Vast records everywhere (`actual_status`, `dph_total`, `start_date`, `instance.diskHour`, port maps). Another backend would have to fake Vast's JSON. The guard logic (caps, watchdog, salvage, fetch, destroy guard) does not depend on the provider.

## Decision

`taxameter.providers.base.Provider` is a Protocol with ten methods: `offers(req)`, `create(offer, spec)`, `instance(id)`, `instances()`, `start(id)`, `stop(id)`, `set_label(id, label)`, `reboot(id)`, `destroy(id)`, `credit()`. They return normalised dataclasses: `Offer` (id, host_id, gpu_name, vram_gb, price_per_h, score, reliability, location, raw), `Instance` (id, status in running/loading/stopped/gone, host_id, gpu_name, price_per_h, storage_per_h, started_at, label, endpoints, raw) and `Endpoint` (kind, host, port, user). Errors are `ProviderError`; `OfferGone` (a subclass) means the offer was taken between listing and renting. `credit()` may return None when a provider has no balance API; runway checks are then skipped.

Provider-specific filtering (Vast: verified machines, direct ports, CUDA version) stays inside the provider. Generic ranking (blocklist, price cap, last-good host first) stays in the core. Each provider also names a DNS host for the box health probe and its on-box self-stop command (`box/idle_autostop.sh`). Vast is the only backend. The seed host blocklist ships with the provider, so every project gets new entries.

Renting falls through to the next ranked offer on `OfferGone`, at most 3 offers; any other create error aborts, because a timeout after the request may already have created a billed box. An explicit `--offer ID` never falls through.

## Consequences

- A new backend is one module plus a fake for its tests.
- Offers may lack a host id (clouds); the blocklist and last-good ranking then do nothing for that provider.
- `raw` keeps provider fields for display, never for decisions in the core.
