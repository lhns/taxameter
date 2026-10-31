"""Project configuration: `taxameter.toml` at the repo root (ADR 0004).

Policy precedence: DEFAULT_POLICY, then `[policy]` (which must set REQUIRED_POLICY), then `<state_dir>/config.json`
if it exists.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

CONFIG_NAME = "taxameter.toml"
DEFAULT_MIN_CUDA = 12.8

DEFAULT_POLICY: dict[str, Any] = {
    "idle_stop_min": 20,  # running + idle this long -> fetch, then stop
    "idle_gpu_util": 10,  # GPU util below this (%) with no busy processes = idle
    "stuck_log_min": 20,  # busy processes alive but no log progress this long = stuck
    "stuck_gpu_zero_min": 5,  # ... or GPU util 0 with jobs alive and no log progress this long
    "destroy_after_stopped_h": 12,  # stopped this long (storage still bills) -> destroy once results fetched
    "unreachable_stop_idle_min": 45,  # unreachable while the API says running; last contact idle -> stop
    "unreachable_stop_busy_min": 180,  # ... last contact running jobs (they keep running through host outages)
    "reboot_after_unreachable_min": 30,  # with --auto-heal: one reboot (restarts the container)
    "outage_count": 3,  # >= this many outages ...
    "outage_window_h": 3,  # ... within this window -> recommend migration
    "runway_warn_h": 6,
    "runway_stop_h": 1,  # below: stop idle instances
    "runway_rent_min_h": 3,  # below: never rent
    "auto_replace": False,  # an automatic replacement could start a second paid box
    "max_price_per_hour": 0.45,
    "max_account_price_per_hour": None,  # optional ceiling on the whole account's running + loading USD/h (ADR 0008)
    "max_instances": 1,  # instances of this project (label = project name) up at once (ADR 0007)
    "max_instances_during_migration": 2,  # make-before-break: old + replacement overlap while it is verified
    "salvage_every_h": 2,  # start a stopped old box this often to fetch its unfetched results
    "salvage_window_min": 30,  # ... and stop it again after this long
    "destroy_unfetched_after_h": 24,  # give up salvage, destroy, log what was lost
    "replace_retry_min": 30,
    "watch_interval_s": 60,
    "ssh_retry_s": 20,
    "ssh_retry_rounds": 3,
    "api_backoff_max_s": 900,
    "disk_gb": 50,
    "create_wait_min": 15,
    "provision_wait_min": 40,
    "fetch_wait_min": 10,
    "nightly_wait_min": 90,  # `nightly` gives the on-box run this long, then fetches whatever exists
    "rent_attempts": 3,  # offers tried when the chosen one was taken meanwhile (OfferGone)
}


# No default on purpose: the spend cap is a decision the project makes. A managed box running longer than this
# many hours is STOPPED (disk kept).
REQUIRED_POLICY = ("max_box_hours",)
POLICY_KEYS = frozenset(DEFAULT_POLICY) | set(REQUIRED_POLICY)


class ConfigError(ValueError):
    pass


@dataclass
class Project:
    name: str = ""  # required: instance label (the per-project cap counts it); remote dir default /root/<name>
    remote_dir: str = ""
    state_dir: str = ".taxameter"
    ssh_key: str = "~/.ssh/taxameter_ed25519"


@dataclass
class ProviderCfg:
    name: str = "vast"
    api_key_file: str = ""  # empty: the provider's default
    blocklist: dict[str, str] = field(default_factory=dict)  # extra seed entries: host id -> reason


@dataclass
class Offers:
    gpus: list[str] = field(default_factory=lambda: ["RTX 3090"])
    min_vram_gb: float = 24
    min_disk_gb: float = 50
    min_reliability: float = 0.99
    min_inet_mbps: float = 500
    min_cuda: float = 0  # highest CUDA the host driver must support; 0: from the image name (cuda:12.8...), else 12.8
    interruptible: bool = False
    max_ram_gb: float = 0  # drop offers whose host RAM exceeds this (or is unknown); 0: no limit


@dataclass
class Image:
    name: str = "nvidia/cuda:12.8.1-devel-ubuntu24.04"
    tag: str = ""  # appended as name:tag when set (or read from tag_file)
    tag_file: str = ""  # repo-relative file to read the tag from ...
    tag_regex: str = ""  # ... with this regex (group 1, multiline); falls back to `tag`
    fallback: str = ""  # shown in --image help only


@dataclass
class Push:
    paths: list[str] = field(default_factory=list)  # repo paths pushed from committed HEAD (empty: whole tree)
    submodules: list[str] = field(default_factory=list)  # pushed from their index tree


@dataclass
class Box:
    busy: str = ""  # pgrep -f -E pattern: processes that count as work
    batch: str = ""  # pgrep -f -E pattern: the batch/nightly driver is alive
    batch_log: str = ""  # repo-relative log with "] start" / "] done ... rc=N" lines
    batch_done: str = ""  # grep -E pattern of the batch log's final line
    logs_dir: str = "runs/taxameter"  # repo-relative: `run` logs; newest *.log mtime = progress
    logs_depth: int = 2  # find -maxdepth below logs_dir for progress logs (jobs logging into nested run dirs)
    provision_hook: str = ""  # repo-relative script run during provision
    verify_hook: str = ""  # repo-relative script run by verify (replacement acceptance)
    run_env: dict[str, str] = field(default_factory=dict)  # env for `run` and `nightly`


@dataclass
class Results:
    dirs: list[str] = field(default_factory=lambda: ["results"])  # fetched; each needs done markers
    done_marker: str = "DONE"  # file name that marks a finished run
    extra: list[str] = field(default_factory=list)  # also packed when present; never gate destroy
    include: list[str] = field(default_factory=lambda: ["*.json", "*.jsonl", "DONE", "*.log"])  # name globs
    whole: list[str] = field(default_factory=list)  # dirs packed completely
    exclude: list[str] = field(default_factory=list)  # path globs never packed or pushed back


@dataclass
class Models:
    specs: list[str] = field(default_factory=list)  # org/name:file[+file...]
    from_toml: str = ""  # repo-relative TOML file ...
    from_key: str = ""  # ... dotted path with `*`, e.g. "model.*.hf"
    dir: str = ".hf_cache"


@dataclass
class Nightly:
    script: str = ""
    args: list[str] = field(default_factory=list)
    done_line: str = "NIGHTLY_DONE"  # the script's last log line: "<done_line> rc=N"


SECTIONS: dict[str, type] = {
    "project": Project,
    "provider": ProviderCfg,
    "offers": Offers,
    "image": Image,
    "push": Push,
    "box": Box,
    "results": Results,
    "models": Models,
    "nightly": Nightly,
}


@dataclass
class Config:
    root: Path
    project: Project = field(default_factory=Project)
    provider: ProviderCfg = field(default_factory=ProviderCfg)
    offers: Offers = field(default_factory=Offers)
    image: Image = field(default_factory=Image)
    push: Push = field(default_factory=Push)
    box: Box = field(default_factory=Box)
    results: Results = field(default_factory=Results)
    models: Models = field(default_factory=Models)
    nightly: Nightly = field(default_factory=Nightly)
    policy: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_POLICY))

    @property
    def remote_dir(self) -> str:
        return self.project.remote_dir or f"/root/{self.project.name}"

    @property
    def state_dir(self) -> Path:
        return self.root / self.project.state_dir

    @property
    def ssh_key(self) -> Path:
        return Path(self.project.ssh_key).expanduser()

    def image_ref(self) -> str:
        tag = self.image.tag
        if self.image.tag_file and self.image.tag_regex:
            try:
                text = (self.root / self.image.tag_file).read_text(encoding="utf-8")
                m = re.search(self.image.tag_regex, text, re.M)
                if m:
                    tag = m.group(1)
            except OSError:
                pass
        return f"{self.image.name}:{tag}" if tag else self.image.name

    def min_cuda(self) -> float:
        """Offer filter on the host driver's CUDA version: a host below the image's CUDA leaves the box stuck in
        "loading" (seen with CUDA 12.5 hosts and a 12.8 image)."""
        if self.offers.min_cuda:
            return float(self.offers.min_cuda)
        m = re.search(r"cuda[:-]?(\d+\.\d+)", self.image_ref(), re.I)
        return float(m.group(1)) if m else DEFAULT_MIN_CUDA

    def model_specs(self) -> list[str]:
        out = list(self.models.specs)
        if self.models.from_toml and self.models.from_key:
            data = tomllib.loads((self.root / self.models.from_toml).read_text(encoding="utf-8"))
            out += [str(v) for v in _dotted(data, self.models.from_key.split(".")) if v]
        return out


def _dotted(node: Any, keys: list[str]) -> list[Any]:
    """Values at a dotted path; `*` matches every key of a table."""
    if not keys:
        return [node]
    if not isinstance(node, dict):
        return []
    k, rest = keys[0], keys[1:]
    if k == "*":
        return [v for child in node.values() for v in _dotted(child, rest)]
    return _dotted(node[k], rest) if k in node else []


def _section(cls: type, data: Any, name: str) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"[{name}] must be a table")
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown key(s) in [{name}]: {', '.join(unknown)}")
    return cls(**data)


def find_config(start: Path | None = None) -> Path | None:
    d = (start or Path.cwd()).resolve()
    for p in (d, *d.parents):
        if (p / CONFIG_NAME).is_file():
            return p / CONFIG_NAME
    return None


def load(path: Path | None = None) -> Config:
    """Load `path`, or the nearest taxameter.toml above the current directory."""
    path = path or find_config()
    if path is None:
        raise ConfigError(f"no {CONFIG_NAME} found in this directory or above")
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from None
    unknown = sorted(set(data) - set(SECTIONS) - {"policy"})
    if unknown:
        raise ConfigError(f"unknown table(s) in {path}: {', '.join(unknown)}")
    cfg = Config(root=Path(path).resolve().parent)
    for name, cls in SECTIONS.items():
        if name in data:
            setattr(cfg, name, _section(cls, data[name], name))
    policy = data.get("policy", {})
    unknown = sorted(set(policy) - POLICY_KEYS)
    if unknown:
        raise ConfigError(f"unknown key(s) in [policy]: {', '.join(unknown)}")
    if not str(cfg.project.name).strip():
        raise ConfigError(
            f"{path}: [project] name is required and must not be empty (it is the instance label the per-project cap "
            "counts; two projects with the same name would share a cap)"
        )
    missing = [k for k in REQUIRED_POLICY if k not in policy]
    if missing:
        raise ConfigError(f"{path}: [policy] {', '.join(missing)} is required (no default: it caps spend per box)")
    cfg.policy.update(policy)
    return cfg


def local_policy_overrides(state_dir: Path) -> tuple[dict[str, Any], str | None]:
    """`<state_dir>/config.json` if present (local operator tuning). Returns (overrides, warning)."""
    p = state_dir / "config.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except OSError:
        return {}, None
    except ValueError:
        return {}, f"{p} is not valid JSON; ignored"
    if not isinstance(data, dict):
        return {}, f"{p} is not a JSON object; ignored"
    return {k: v for k, v in data.items() if k in POLICY_KEYS}, None
