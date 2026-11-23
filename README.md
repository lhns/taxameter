# Taxameter

A taxi meter for rented GPU boxes: you pay per minute while the box runs, and taxameter watches the meter.

Taxameter rents a GPU box, pushes your committed code, runs a job detached, waits on the box, fetches the results with a DONE-marker check and destroys the box. Around that it enforces hard money caps (USD/h, instances per project, hours per box), runs a watchdog for idle, stuck and unreachable boxes, keeps a host blocklist and redacts the API key from every line it prints. Vast.ai is the first provider.

## Install

Add it to the project as a uv dependency and commit a `taxameter.toml` at the repo root:

```toml
# pyproject.toml
[dependency-groups]
dev = ["taxameter"]

[tool.uv.sources]
taxameter = { git = "https://github.com/lhns/taxameter", tag = "v0.3.1" }
```

Then `uv run taxameter --help`. One-off use without a project dependency: `uvx --from git+https://github.com/lhns/taxameter taxameter --help`.

Needs `ssh`, `scp` and `git` on PATH (Git for Windows on Windows), the provider API key in a file (Vast: `~/.config/vastai/vast_api_key`) and an SSH key registered with the provider.

## Configuration

Minimal `taxameter.toml` (all tables are in [ADR 0004](docs/adr/0004-project-config-and-box-contract.md); unknown keys are an error):

```toml
[project]
name = "myproj"            # required, unique per project: the instance label; the instance cap counts boxes with it

[image]
name = "vastai/pytorch"
tag = "cuda-12.8.1-auto"

[offers]
min_cuda = 12.8            # optional: lowest host-driver CUDA to accept; default: the CUDA version in the image
                           # name/tag (cuda-12.8.1 -> 12.8), else 12.8
num_gpus = 1              # optional: exact GPUs per box (Vast num_gpus eq N); default 1, as before. --num-gpus overrides.
                           # The price caps compare the whole box's price
max_ram_gb = 64            # optional: drop offers whose host RAM exceeds this (or is unknown), e.g. so a model
                           # does not fit in page cache for benchmarks; default: no limit
min_cpu_cores = 8          # optional: drop offers with fewer usable CPU cores (cpu_cores_effective); default: no limit
max_inet_down_cost = 0.02  # optional: drop offers billing more than this USD/GB downloaded; default: no limit
expected_download_gb = 120 # optional: price this much download into score and shown price (default 0, ADR 0010):
                           # effective USD/h = dph_total + inet_down_cost * GB / expected_box_hours (default
                           # max_box_hours). Price caps still apply to dph_total only

[policy]
max_box_hours = 12         # required, no default: a managed box running longer is stopped
max_instances = 1          # this project's boxes up at once (default 1)
max_account_price_per_hour = 0.9  # optional: running + loading boxes on the whole account (any label) plus the new
                           # one may not cost more USD/h; default: off. Rents on one machine take a lock
```

`[policy]` holds the cap and watchdog thresholds (defaults in `config.py`). `[project] name` and `[policy] max_box_hours` are required. `offer_retries` (default 2) and `offer_retry_s` (default 10) set how often `create --offer ID` searches again when the offer is missing from the results. If the id rotated, `create --offer` takes the same machine's current offer, using the machine recorded by the last `offers` listing (`offers.json` in the state dir, ADR 0014).

## Ports

Only SSH is published by default. `create --ports 7000,7001/udp` (also `nightly`; repeat the flag or comma-separate; `PORT` means tcp) publishes more ports, and `[box] ports = ["7000/tcp", "7001/udp"]` sets the default (the flag replaces it; [ADR 0014](docs/adr/0014-create-ports.md)). `create` then prints the public mapping, and `endpoint` prints it again later (Vast picks new public ports on every restart):

```
port 7001/udp -> 203.0.113.7:41234
```

`--json` has `ports: {"7001/udp": "203.0.113.7:41234"}`. The Vast request carries `env: {"-p 7001:7001/udp": "1"}`, the form of the vastai CLI's `--env "-p ..."`.

## Usage

```bash
T="uv run taxameter"
$T balance                   # credit, spend rate, runway
$T offers                    # ranked offers under the price cap; the last-good host comes first
$T create --auto             # rent the best offer (falls through to the next if it was just taken)
$T offers --min-cpu-cores 8 --download-gb 120   # per-run overrides of the two [offers] settings above
$T provision                 # push committed HEAD, run the provision hook, models, on-box autostop
$T run scripts/job.sh        # start a repo script detached on the box
$T ssh --timeout 540 -- 'for i in $(seq 100); do grep -q ^DONE /root/proj/runs/job.log && break; sleep 5; done'
$T fetch                     # pack, scp back, verify DONE markers, unpack
$T destroy --yes             # refuses while results are unfetched
$T nightly                   # all of the above: create, provision, run, wait, fetch, destroy
$T watchdog --dry-run        # what the cost-control loop would do
```

`status` also shows per box the cores, the download price, the download GB billed so far and its cost (and the accumulated cost when the API gives it).

Several boxes at once (`max_instances` above 1, ADR 0009): each agent drives its own box with its own process. Always pass `--id` (`$T run --id 123 ...`); without it a command uses the default `current` box, which `create` and `adopt` set only when none is set. `create` prints the new id (`--json` has `id` and `current`). State updates are locked, so parallel processes do not lose each other's changes; batches are tracked per box. `status` and `watchdog` cover all of the project's boxes.

Run `watchdog --once` every few minutes from cron or Task Scheduler whenever a box may exist.

On Windows, register it with `conhost.exe --headless` so it does not open a console window on every run:

```bat
schtasks /Create /TN <project>-vast-watchdog /SC MINUTE /MO 5 /F /TR "conhost.exe --headless cmd /c cd /d <repo> && uv run taxameter watchdog --once >> .vast\watchdog.out 2>&1"
```

Box running time: `status`, `run`, `ssh -- cmd`, `provision`, `put`, `fetch` and `health` print `box running 2h41m of max 3h00m` (hours the box ran; time spent stopped does not count, ADR 0012) and warn past 80% of `max_box_hours`. `run` and `provision` refuse to start work on a box older than the cap unless you pass `--past-cap`; `fetch` and `destroy` are never blocked. The watchdog still stops such a box.

## Rules for agents

Copy these into the project's AGENTS.md.

- Never print, log, commit or pass the API key on a command line. Taxameter reads it from the file and sends it only as a header.
- The box bills every running minute. Destroy it when done, after a verified fetch. Never raise the caps without asking, and never use `--past-cap` unasked.
- Commit before `provision` or `run`: only committed HEAD is pushed.
- Long work runs detached on the box. `ssh` stops remote commands after 120 s unless `--timeout`. Wait with loops on the box, not local polling.
- Launch scripts must be safe to run twice (`exec 8> /root/x.lock; flock -n 8 || exit 0`): a command is re-run after a dropped connection.
- Never put a `pgrep -f` or `pkill -f` pattern inline in `ssh '...'`; it matches the ssh shell itself. Put it in a script on the box.
- Never overwrite a shell script while bash runs it; upload new versions under a new name.
- Never launch GPU work on a box whose provision log does not say `cuda: ok` or `cuda: ok_preload`.
- On Windows Git Bash set `MSYS_NO_PATHCONV=1` for commands with `/root/...` arguments.

## Design

Decisions are in [docs/adr](docs/adr). In short:

- **Guard layer, not a cloud abstraction** (ADR 0001). The value is caps, watchdog, blocklist, redaction and the fetch-verify-destroy flow. Python, httpx only, offline tests.
- **Provider interface** (ADR 0002). Ten methods: `offers`, `create`, `instance`, `instances`, `start`, `stop`, `set_label`, `reboot`, `destroy`, `credit`, returning normalised `Offer`, `Instance` and `Endpoint`. `OfferGone` makes renting fall through to the next offer (at most 3); any other create error aborts.
- **SkyPilot** (ADR 0003). Evaluated; the seam allows a backend later, but it is not built: it brings a second state store and its own lifecycle, and lacks USD/h caps and a host blocklist.
- **Per-project config** (ADR 0004). `taxameter.toml` holds image, pushed paths, box patterns, results, models, nightly script and policy. The box scripts are generic and read a rendered env file in `/root/.taxameter/`; projects plug in `provision_hook` and `verify_hook` scripts.
- **Per-project instance cap** (ADR 0007, supersedes the account-wide count cap of 0005; ADR 0008 adds an optional account-wide USD/h ceiling and a per-machine rent lock). Instances labelled with the project name, and instances adopted into the project (`adopt` relabels them), count. Across projects, total spend is bounded by each project's caps plus `max_account_price_per_hour` when set.
- **Parallel instances** (ADR 0009). State changes are locked read-modify-writes (`state.lock`), batches and replacement requests are per box, `current` is only the default; always pass `--id` with several boxes.
- **Migration** (ADR 0006, [docs/migrating-from-vastctl.md](docs/migrating-from-vastctl.md)). Keep `state_dir = ".vast"`, replace `scripts/vastctl.py` by a shim.

Layout:

```
src/taxameter/
  cli.py              argparse commands, main()
  config.py           taxameter.toml loading and defaults
  core.py             state store, lock, ssh/scp with endpoint re-resolution, push, provision, fetch,
                      destroy guard, health, watchdog, salvage, replacement, nightly
  boxenv.py           renders the box env file, bundles the box scripts
  providers/base.py   Provider protocol, Offer, Instance, Endpoint, errors
  providers/vast.py   Vast REST backend and seed blocklist
  box/                box.sh, idle_autostop.sh, cuda_env.sh, pack_results.sh (run on the box)
```

State (`<state_dir>/`, git-ignored): `state.json` (managed instances, endpoint cache, blocklist, runs, outages, batches per box), `events.log`, `state.lock`, `nightly.lock`, `known_hosts_<id>`, `fetch/` (every fetched archive), optional `config.json` (local policy overrides).

## Development

```bash
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
```

Tests never touch the network: `httpx.MockTransport` plays the provider API and a fake replaces ssh/scp.

## License

Apache-2.0
