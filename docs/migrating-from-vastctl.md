# Migrating a project from vastctl

For a repo that carries its own copy of `scripts/vastctl.py`, `vast_box.sh`, `idle_autostop.sh`, `cuda_env.sh` and `pack_results.sh` (Tetraeder, LRN). Background: ADR 0004 (config and box contract) and ADR 0006 (migration). Never rent a box to test the switch; the tests are offline.

## 1. Before you start

- Do not switch while a box of this project is up, or while a scheduled `nightly` may start: check `.vast/state.json` (`current`, roles) and the provider console.
- Work on a branch, ideally in a `git worktree`, so the main checkout and its `.vast/` stay untouched.

## 2. Add the dependency

```toml
# pyproject.toml
[dependency-groups]
dev = ["taxameter"]   # next to pytest etc.

[tool.uv.sources]
taxameter = { git = "https://github.com/lhns/taxameter", tag = "v0.1.0" }
```

Then `uv lock`. Requires Python 3.11+.

## 3. Write `taxameter.toml`

Map the constants at the top of your `vastctl.py` and your `.vast/config.json`:

| vastctl | taxameter.toml |
|---|---|
| `LABEL`, `REMOTE_DIR` | `[project] name`, `remote_dir` |
| `.vast/` | `[project] state_dir = ".vast"` (keeps state, blocklist, last-good host, fetch archives) |
| `SSH_KEY` | `[project] ssh_key` |
| `GPU_CLASS`, offer flags | `[offers] gpus`, `min_vram_gb`, `min_reliability`, ... (`min_cuda` defaults to the image's CUDA version, else 12.8; set it explicitly to pin it) |
| `IMAGE` | `[image] name`, `tag` (or `tag_file` + `tag_regex`) |
| `git_archive` paths, submodule | `[push] paths`, `submodules` |
| busy pgrep pattern in `vast_box.sh` / `idle_autostop.sh` | `[box] busy` (keep the `[x]` bracket trick) |
| batch process pattern, batch log, final line | `[box] batch`, `batch_log`, `batch_done` (grep -E) |
| progress `find ... -maxdepth N -name '*.log'` | `[box] logs_dir`, `logs_depth` (default 2) |
| `vast_box.sh deps`, project parts of `verify` | `[box] provision_hook`, `verify_hook`: repo scripts run on the box from the repo dir |
| env set by `run` (RESUME=1 ...) | `[box] run_env`, or `taxameter run --env K=V` |
| runs dirs and fetch marker (`DONE`, `summary.json`) | `[results] dirs`, `done_marker` |
| `pack_results.sh` include list | `[results] include` (name globs), `whole`, `exclude`, `extra` |
| model list | `[models] specs = ["org/name:file+file", ...]` (subdirs allowed: `org/x:1_Pooling/config.json`); list the largest file first, the download speed probe uses the first file |
| nightly script | `[nightly] script`, `args`, `done_line` |
| `.vast/config.json` thresholds | `[policy]`; a remaining `.vast/config.json` still overrides it locally. `max_box_hours` has no default: set it explicitly or config loading fails |

Known bad hosts ship with the Vast provider; add project-only ones under `[provider] blocklist`.

## 4. Move project work into hooks

- `provision_hook`: whatever your `deps` did (pip installs into the image's venv, toolchains, one-off edits). Exit nonzero on failure.
- `verify_hook`: replacement acceptance beyond GPU, CUDA and model files (e.g. a tiny torch matmul). Exit nonzero on failure.
- Scripts that sourced `scripts/cuda_env.sh` now source `"${TAXAMETER_HOME:-/root/.taxameter}/cuda_env.sh"`.

## 5. Remove the copies

- Delete `vast_box.sh`, `idle_autostop.sh`, `cuda_env.sh`, `pack_results.sh` and the generic tests. Keep tests that check your own config (for example that `[push] paths` covers what the box builds).
- Replace `scripts/vastctl.py` by a shim so scheduled tasks keep working:

```python
import sys
from taxameter.cli import main

sys.exit(main())
```

- Commands become `uv run taxameter ...` (same subcommands and flags). Update README, AGENTS.md/CLAUDE.md, CI (`uv run pytest` no longer runs vastctl tests).

## 6. Check offline

```bash
uv run taxameter status            # reads state, lists instances (read-only API calls)
uv run taxameter offers            # read-only
uv run taxameter watchdog --dry-run --once
```

A box provisioned by the old tool gets the new files under `/root/.taxameter/` on its next command; its old watchdog keeps running until the box is destroyed.

## LRN specifics

- `[project] name = "lrn"`, `remote_dir = "/root/LRN"`, `state_dir = ".vast"`, `ssh_key = "~/.ssh/lrn_vast_ed25519"`.
- `[image] name = "vastai/pytorch"`, `tag = "cuda-12.8.1-auto"`; `[offers] gpus` = the old `GPU_CLASS` list; `[policy] max_box_hours = 12` (required), `auto_replace = true`, `disk_gb = 50`; `[offers] min_cuda = 12.8` pinned explicitly.
- `[box] busy = "lrn[.]cli exper[i]ment|pytes[t]|longmemeval_eva[l]|oracle_contex[t]|eval_save[d]"`, `batch = "gpu_batch[.]s[h]"`, `batch_log = "runs/batch.log"`, `batch_done = "^ALL DONE"`, `logs_dir = "runs"`, `logs_depth = 4` (job logs sit in `runs/gpu/s<seed>/`), `run_env = { RESUME = "1", PYTHONUNBUFFERED = "1" }`.
- Batches: `taxameter run --env KINDS="operator spacing" scripts/gpu_restart_batch.sh 4 0 1 2` (check how `gpu_restart_batch.sh` takes its arguments; the old `vast_box.sh run` passed `<par> <seeds...>`).
- `provision_hook`: the old `deps` (uv pip install into `/venv/main`, `-e . --no-deps`) plus the `integrate_base_after_levels` sed. `verify_hook`: the torch import and CUDA matmul check.
- `[results] dirs = ["runs/gpu"]`, `done_marker = "summary.json"`, `include` = the old `pack_results.sh` list (`summary.json`, `arm.json`, `hypotheses.*`, `meta.json`, `events.jsonl`, `*.log`, `tree.json`, `config.yaml`), plus `cycle_*.json`, `features.jsonl`, `labels.jsonl` for the old `*/cycles/*` and `*/importance_dataset/*` rules (`whole` matches a packed dir and its subdirs, not nested dirs by name), `extra = ["runs/diag"]`, `exclude = ["*.lance*", "*.safetensors"]`.
- Models: the per-model file lists of the old `models()` become explicit specs, e.g. `Qwen/Qwen3-0.6B:model.safetensors+config.json+generation_config.json+tokenizer.json+tokenizer_config.json+vocab.json+merges.txt` (largest file first: it is the one the speed probe downloads).
- LRN's `cuda_env.sh` probed torch; taxameter's probes `cuInit` through ctypes, which catches the same error 804.
- LRN gains: host 366851 in the seed blocklist, lock races fixed, `max_box_hours`, submodule-aware push, `nightly`, the per-project instance cap (ADR 0007: only boxes labelled with `[project] name` count) and offer fallthrough.
