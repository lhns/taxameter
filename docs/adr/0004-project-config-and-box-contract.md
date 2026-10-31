# 0004. Per-project `taxameter.toml` and the box contract

Status: Accepted

## Context

`vastctl` hard-codes project facts: image, label, pushed paths, submodule, remote dir, busy process patterns, batch log and its done line, DONE marker, pack rules, model list, nightly script. The box scripts hard-code the same facts a second time, and pgrep patterns must never appear on an ssh command line.

## Decision

Each project commits a `taxameter.toml` at its root (found by walking up from the current directory, or `--config`). Tables: `[project]` (name, required; remote_dir, state_dir, ssh_key), `[provider]`, `[offers]` (gpus, min_vram_gb, min_disk_gb, min_reliability, min_inet_mbps, min_cuda, interruptible, max_ram_gb), `[image]` (name, tag, or a tag read from a file by regex), `[push]` (paths, submodules), `[box]` (busy, batch, batch_log, batch_done, logs_dir, logs_depth, provision_hook, verify_hook, run_env), `[results]` (dirs, done_marker, extra, include, whole, exclude), `[models]` (specs, or read from a TOML file by a dotted path with `*`), `[nightly]` (script, args, done_line) and `[policy]` (the watchdog and cap thresholds; `max_box_hours` is required; `max_account_price_per_hour` is optional). Unknown keys are an error. Policy precedence: package defaults, then `[policy]`, then `<state_dir>/config.json` if it exists (local operator tuning, as before; it is no longer created).

The box side is generic and ships in the package: `box.sh`, `idle_autostop.sh`, `cuda_env.sh`, `pack_results.sh`. Taxameter uploads them with a rendered `env` file to `/root/.taxameter/` (temp name then `mv`, so a running script is never rewritten). The env file carries every project knob as `TAXAMETER_*` variables, so patterns live in a file on the box. Project work on the box goes through hooks: `provision_hook` (install what the image lacks) and `verify_hook` (replacement acceptance), both repo-relative scripts. Project scripts source `$TAXAMETER_HOME/cuda_env.sh` (default `/root/.taxameter`).

## Consequences

- A project adapts taxameter by config and two optional hook scripts, not by editing it.
- Health keys (`exp_procs`, `batch_alive`, `batch_all_done`, ...) stay as before, so the classifier is unchanged.
- Tetraeder's `nightly.sh` and LRN's batch scripts stay in their projects; they only change where they source `cuda_env.sh`.
