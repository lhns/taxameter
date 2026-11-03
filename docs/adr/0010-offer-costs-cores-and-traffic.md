# 0010. Offer filters and scoring for CPU cores and download traffic

Status: Accepted.

## Context

Vast bills internet traffic per GB (`inet_down_cost`, 0.013 to 0.026 USD/GB seen). A project that downloads a 100+ GB model pays more for traffic than for the hour (`dph_total`), but the offer filter and score ignored it. Usable CPU cores (`cpu_cores_effective`) range from 4 to 15 between hosts and matter for CPU-heavy work; users rented and destroyed boxes to find one with enough.

## Decision

1. **Filters in `[offers]`**: `min_cpu_cores` (on `cpu_cores_effective`) and `max_inet_down_cost` (USD/GB). Both default to 0 = off. The Vast query sends them as server-side filters (`cpu_cores_effective gte`, `inet_down_cost lte`) only when set, and `usable()` re-checks client-side. An offer with unknown cores or download price fails an active filter.
2. **Effective price**: `expected_download_gb` (default 0) and `expected_box_hours` (default 0 = `[policy] max_box_hours`), both in `[offers]`. Per offer: `traffic_per_h = inet_down_cost * expected_download_gb / expected_box_hours` and `effective_price = price_per_h + traffic_per_h`. The score is multiplied by `price_per_h / effective_price` (the provider score is throughput per dollar, so this prices the traffic in). Price rank and `--sort price` use the effective price. Offers with unknown download price get no traffic cost.
3. **Caps unchanged**: `max_price_per_hour`, `max_account_price_per_hour`, the runway check and instance accounting use `price_per_h` (dph_total) only. They are hourly money caps.
4. **CLI**: `offers` and `create` take `--min-cpu-cores` and `--download-gb` (override the config; `--download-gb 0` turns pricing off). `offers` shows cores, download price and traffic per hour; `--json` adds `cpu_cores`, `inet_down_cost`, `traffic_per_h`, `effective_price_per_h`.
5. **`status`** prints per instance cores, download price, billed download GB and its cost (`inet_down_billed`, which Vast reports in kB, times `inet_down_cost`); `--json` carries the same fields.
6. `Offer` and `Instance` gain `cpu_cores`, `inet_down_cost` (and on `Instance` `inet_down_billed_gb`); `OfferQuery` gains `min_cpu_cores`, `max_inet_down_cost`. Providers that do not know a value leave 0 or None.

## Consequences

- With the default config nothing changes except the extra columns.
- The effective price is a ranking aid, not a cap: an offer can pass `max_price_per_hour` and still cost more per hour once traffic is included. Use `max_inet_down_cost` to bound it.
- `inet_down_billed` is in kB (checked 2026-11-03: a box with 149 GB of models on disk showed 171966464, the rest being the image and other downloads). Vast's instance row has no accumulated-cost field. A missing field shows as `?`.
