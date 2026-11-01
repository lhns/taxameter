"""Core: state store, lock, ssh/scp with endpoint re-resolution, push, provision, fetch, destroy guard, health,
watchdog (salvage, replacement) and nightly. Provider-agnostic: it only sees `providers.base` records."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import tarfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from taxameter import boxenv, config
from taxameter.config import Config, ConfigError
from taxameter.output import iso, redact, say
from taxameter.providers import get_provider
from taxameter.providers.base import (
    CreateSpec,
    Endpoint,
    Instance,
    Offer,
    OfferGone,
    OfferQuery,
    ProviderError,
)

BOX_HOME = boxenv.BOX_HOME
STAGING_DIR = boxenv.STAGING
BOX_SH = f"{BOX_HOME}/box.sh"
PROVISION_LOG = "/root/provision.log"
RESULTS_ARCHIVE = "/root/results.tgz"
BANNER = ("AI agents", "Welcome", "Have fun")  # login banners that would pollute parsed command output
UP_STATUSES = ("running", "loading")  # these bill the full price and count toward the instance cap


# ----------------------------------------------------------------------------------------------- pure helpers
def spend_summary(credit: float | None, instances: list[Instance]) -> dict:
    """Credit, $/h across instances (running or loading: full price; stopped: storage still bills) and hours of
    runway. Without a credit (provider has no balance API) the runway is None and runway checks are skipped."""
    rate = sum(i.price_per_h if i.status in UP_STATUSES else i.storage_per_h for i in instances)
    runway = credit / rate if credit is not None and rate > 0 else None
    return {
        "credit": None if credit is None else round(credit, 4),
        "spend_per_h": round(rate, 4),
        "runway_h": None if runway is None else round(runway, 2),
        "running": sum(1 for i in instances if i.status == "running"),
        "instances": len(instances),
    }


def runway_after(bal: dict, price: float) -> float | None:
    """Hours of runway if one more box at `price` $/h starts now; None when the credit is unknown."""
    if bal["credit"] is None:
        return None
    return bal["credit"] / (bal["spend_per_h"] + price) if bal["spend_per_h"] + price > 0 else None


def parse_kv(text: str) -> dict[str, str]:
    kv = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            if k and " " not in k:
                kv[k.strip()] = v.strip()
    return kv


def _num(kv: dict, k: str, default: float | None = None) -> float | None:
    try:
        return float(kv[k])
    except (KeyError, ValueError, TypeError):
        return default


def classify(api_status: str | None, probe: dict | None, policy: dict) -> tuple[str, list[str]]:
    """healthy / idle / stuck / unreachable (or the API status when not running)."""
    if api_status != "running":
        return (api_status or "gone"), []
    if probe is None:
        return "unreachable", ["no SSH on direct or proxy endpoint"]
    procs = int(_num(probe, "exp_procs", 0) or 0)
    batch = int(_num(probe, "batch_alive", 0) or 0)
    util = _num(probe, "gpu_util")
    age = _num(probe, "progress_age_s")
    if age is None:
        age = _num(probe, "batch_log_age_s")
    age_min = (age or 0) / 60.0
    if procs > 0:
        if age is not None and age_min > policy["stuck_log_min"]:
            return "stuck", [f"{procs} busy processes, no log progress for {age_min:.0f} min"]
        if util == 0 and age is not None and age_min > policy["stuck_gpu_zero_min"]:
            return "stuck", [f"{procs} busy processes, GPU util 0, no log progress for {age_min:.0f} min"]
        return "healthy", [f"{procs} busy processes, GPU {util:.0f}%" if util is not None else ""]
    if batch > 0:
        return "healthy", ["batch queue alive"]
    if util is not None and util >= policy["idle_gpu_util"]:
        return "healthy", [f"GPU busy {util:.0f}% without project processes"]
    return "idle", ["no busy processes"]


def outages_in_window(outages: list[dict], now: float, window_h: float) -> int:
    return sum(1 for o in outages if (o.get("end") or now) >= now - window_h * 3600)


def verify_archive(path: Path, marker: str) -> dict[str, int]:
    """Done-marker count per member path in a results archive (raises if it does not unpack)."""
    counts: dict[str, int] = {}
    with tarfile.open(path, "r:gz") as tf:
        for m in tf.getmembers():
            if m.isfile() and m.name.rsplit("/", 1)[-1] == marker:
                counts[m.name] = counts.get(m.name, 0) + 1
    return counts


# ----------------------------------------------------------------------------------------------- state
class Store:
    def __init__(self, state_dir: Path, seed_blocklist: dict[str, str] | None = None) -> None:
        self.dir = state_dir
        self.path = state_dir / "state.json"
        self.events_path = state_dir / "events.log"
        self.seed_blocklist = dict(seed_blocklist or {})
        self.state = self._load()

    def _load(self) -> dict:
        try:
            st = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = {}
        st.setdefault("instances", {})
        st.setdefault("current", None)
        st.setdefault("blocklist", {})
        for h, why in self.seed_blocklist.items():
            st["blocklist"].setdefault(h, why)
        st.setdefault("events", [])
        st.setdefault("batch", None)
        st.setdefault("pending", [])
        return st

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state["events"] = self.state["events"][-300:]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(redact(json.dumps(self.state, indent=1, sort_keys=True)), encoding="utf-8")
        os.replace(tmp, self.path)

    def event(self, msg: str, iid: Any = None, echo: bool = True) -> None:
        now = time.time()
        line = redact(f"{iso(now)} {'[' + str(iid) + '] ' if iid else ''}{msg}")
        self.state["events"].append({"t": now, "id": iid, "msg": redact(msg)})
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if echo:
            say(f"{'[' + str(iid) + '] ' if iid else ''}{msg}")
        self.save()

    def rec(self, iid: Any, create: bool = False) -> dict:
        key = str(iid)
        if key not in self.state["instances"]:
            if not create:
                raise ProviderError(f"instance {iid} is not managed by taxameter (use `adopt {iid}` first)")
            self.state["instances"][key] = {
                "id": key,
                "role": "active",
                "runs": {},
                "outages": [],
                "endpoints": {},
                "added": time.time(),
            }
        return self.state["instances"][key]

    def blocklist(self) -> set[str]:
        return set(self.state["blocklist"])

    def pend(self, key: str, msg: str) -> None:
        self.state["pending"] = [p for p in self.state["pending"] if p["key"] != key]
        self.state["pending"].append({"key": key, "t": time.time(), "msg": redact(msg)})

    def unpend(self, key: str) -> None:
        self.state["pending"] = [p for p in self.state["pending"] if p["key"] != key]


# ----------------------------------------------------------------------------------------------- process lock
class FileLock:
    """Exclusive, non-blocking OS file lock that dies with the process (msvcrt on Windows, flock elsewhere), so a
    crashed `nightly` never blocks the watchdog."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fh: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True

    def release(self) -> None:
        fh, self.fh = self.fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def nightly_lock(ctx: Ctx) -> FileLock:
    return FileLock(ctx.store.dir / "nightly.lock")


# ----------------------------------------------------------------------------------------------- context
class Ctx:
    """Everything a command needs: config, provider, state store and the effective policy."""

    def __init__(
        self, cfg: Config, transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep
    ):
        self.cfg, self.sleep = cfg, sleep
        self.provider = get_provider(cfg, transport)
        self.store = Store(cfg.state_dir, {**self.provider.seed_blocklist, **cfg.provider.blocklist})
        self.policy: dict[str, Any] = self.load_policy(warn=True)

    @property
    def root(self) -> Path:
        return self.cfg.root

    @property
    def ssh_key(self) -> Path:
        return self.cfg.ssh_key

    def load_policy(self, warn: bool = False) -> dict[str, Any]:
        """[policy] from taxameter.toml, then `<state_dir>/config.json` (local operator tuning, never created)."""
        overrides, warning = config.local_policy_overrides(self.cfg.state_dir)
        if warning and warn:
            say(f"WARNING: {warning}")
        return {**self.cfg.policy, **overrides}

    def iid(self, given: Any = None) -> str:
        iid = given or self.store.state.get("current")
        if not iid:
            raise ProviderError("no instance: pass --id or create/adopt one")
        return str(iid)


# ----------------------------------------------------------------------------------------------- box age
WARN_AGE_FRACTION = 0.8


def fmt_hours(h: float) -> str:
    m = int(round(h * 60))
    return f"{m // 60}h{m % 60:02d}m"


def box_age_h(ctx: Ctx, iid: str, inst: Instance | None = None) -> float | None:
    """Hours since the provider's start time of the box, else the time recorded at create/adopt; None if unknown.
    An API failure falls back to the record: a cost notice must never break the command."""
    started = inst.started_at if inst else None
    if started is None and inst is None:
        try:
            found = ctx.provider.instance(iid)
            started = found.started_at if found else None
        except ProviderError:
            pass
    if started is None:
        started = ctx.store.state["instances"].get(str(iid), {}).get("created")
    return (time.time() - float(started)) / 3600 if started else None


def age_notice(ctx: Ctx, iid: str, inst: Instance | None = None, enforce: bool = False, past_cap: bool = False) -> None:
    """Print the box age against max_box_hours (and a warning past 80%). With `enforce`, refuse new work on a box
    older than the cap unless `past_cap`. Never used for fetch/destroy: they are how you stop paying."""
    age, cap = box_age_h(ctx, iid, inst), float(ctx.policy["max_box_hours"])
    if age is None:
        return
    say(f"box age {fmt_hours(age)} of max {fmt_hours(cap)}")
    if age > cap:
        if enforce and not past_cap:
            raise ProviderError(
                f"box {iid} is {fmt_hours(age)} old, past max_box_hours {fmt_hours(cap)}: not starting new work "
                "(fetch and destroy still work; pass --past-cap to override)"
            )
        say(f"WARNING: box is past max_box_hours {fmt_hours(cap)}: fetch results and destroy it")
    elif age > WARN_AGE_FRACTION * cap:
        say(f"WARNING: box age is past 80% of max_box_hours {fmt_hours(cap)}: wrap up, fetch and destroy")


# ----------------------------------------------------------------------------------------------- ssh / scp
class Unreachable(RuntimeError):
    pass


class ToolMissing(RuntimeError):
    """ssh/scp/git missing locally: never to be mistaken for an unreachable box (no cloud action may follow)."""


@dataclass
class Result:
    rc: int
    out: str
    err: str
    endpoint: Endpoint | None = None


def run_proc(argv: list[str], timeout: float, stdin: bytes | None = None) -> tuple[int, str, str]:
    """Run an executable with a hard timeout (no dependency on a `timeout` binary). Bytes in, text out."""
    try:
        p = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            timeout=timeout,
            check=False,
            stdin=None if stdin is not None else subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        return (
            124,
            _dec(e.stdout),
            _dec(e.stderr) + f"\n[timeout after {timeout:.0f}s; for long commands pass `ssh --timeout SECONDS` "
            "or run them detached with nohup]",
        )
    except FileNotFoundError:
        raise ToolMissing(f"{argv[0]} not found on PATH (Git for Windows / OpenSSH)") from None
    return p.returncode, _dec(p.stdout), _dec(p.stderr)


def _dec(b: bytes | str | None) -> str:
    if b is None:
        return ""
    return b if isinstance(b, str) else b.decode("utf-8", errors="replace")


def strip_banner(text: str) -> str:
    return "\n".join(
        line for line in text.replace("\r\n", "\n").split("\n") if not any(b in line for b in BANNER)
    ).strip("\n")


CONN_ERRORS = (
    "Connection refused",
    "Connection timed out",
    "timed out",
    "Connection closed",
    "lost connection",
    "Could not resolve",
    "No route to host",
    "Connection reset",
    "kex_exchange_identification",
    "Network is unreachable",
)


class Remote:
    """Every remote command goes through here: cached endpoint first, then a fresh API lookup (direct, proxy),
    retries with backoff; the working endpoint is written back to state."""

    def __init__(self, ctx: Ctx, iid: Any) -> None:
        self.ctx, self.iid = ctx, str(iid)
        self.rec = ctx.store.rec(iid)
        self.known_hosts = ctx.store.dir / f"known_hosts_{self.iid}"
        self.helper_ok = False

    def _common(self) -> list[str]:
        return [
            "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.known_hosts.as_posix()}", "-i", self.ctx.ssh_key.as_posix(),
            "-o", "IdentitiesOnly=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
            "-o", "LogLevel=ERROR",
        ]  # fmt: skip

    def ssh_argv(self, ep: Endpoint, cmd: str | None, tty: bool = False) -> list[str]:
        argv = ["ssh", *self._common(), "-p", str(ep.port)] + (["-t"] if tty else []) + [f"{ep.user}@{ep.host}"]
        return argv + ([cmd] if cmd else [])

    def scp_argv(self, ep: Endpoint, remote_path: str, local: Path) -> list[str]:
        return ["scp", *self._common(), "-P", str(ep.port), f"{ep.user}@{ep.host}:{remote_path}", local.as_posix()]

    def fresh_endpoints(self) -> list[Endpoint]:
        inst = self.ctx.provider.instance(self.iid)
        eps = list(inst.endpoints) if inst else []
        self.rec["endpoints"] = {
            "resolved_at": time.time(),
            "all": [e.to_dict() for e in eps],
            "last_ok": self.rec.get("endpoints", {}).get("last_ok"),
        }
        if inst:
            self.rec["host_id"] = inst.host_id
            self.rec["api_status"] = inst.status
        return eps

    def cached(self) -> Endpoint | None:
        return Endpoint.from_dict((self.rec.get("endpoints") or {}).get("last_ok"))

    def _mark_ok(self, ep: Endpoint) -> None:
        self.rec.setdefault("endpoints", {})["last_ok"] = dict(ep.to_dict(), at=time.time())
        self.ctx.store.save()

    def attempt(
        self,
        fn: Callable[[Endpoint], tuple[int, str, str]],
        is_conn_err: Callable[[int, str, str], bool],
        rounds: int | None = None,
        backoff: float | None = None,
        label: str = "ssh",
    ) -> Result:
        pol = self.ctx.policy
        rounds = pol["ssh_retry_rounds"] if rounds is None else rounds
        backoff = pol["ssh_retry_s"] if backoff is None else backoff
        last = ""
        for r in range(rounds + 1):
            tried: list[Endpoint] = []
            order: list[Endpoint] = []
            if r == 0 and self.cached():
                order.append(self.cached())  # type: ignore[arg-type]
            for i in range(2):
                for ep in order:
                    if ep in tried:
                        continue
                    tried.append(ep)
                    rc, out, err = fn(ep)
                    if not is_conn_err(rc, out, err):
                        self._mark_ok(ep)
                        return Result(rc, strip_banner(out), strip_banner(err), ep)
                    last = f"{ep}: rc={rc} {strip_banner(err).strip()[-160:]}"
                if i == 0:
                    try:
                        order = self.fresh_endpoints()
                    except ProviderError as e:
                        last = f"API lookup failed: {e}"
                        order = []
            if r < rounds:
                say(f"{label}: no endpoint reachable ({last}); retry in {backoff:.0f}s ({r + 1}/{rounds})")
                self.ctx.sleep(backoff)
        raise Unreachable(redact(last or "no endpoints"))

    def run(
        self,
        cmd: str,
        timeout: float = 60,
        stdin: bytes | None = None,
        rounds: int | None = None,
        backoff: float | None = None,
    ) -> Result:
        def conn_err(rc: int, out: str, err: str) -> bool:
            return rc == 255 or (rc == 124 and not out.strip())

        return self.attempt(lambda ep: run_proc(self.ssh_argv(ep, cmd), timeout, stdin), conn_err, rounds, backoff)

    def scp_from(
        self,
        remote_path: str,
        local: Path,
        timeout: float = 600,
        rounds: int | None = None,
        backoff: float | None = None,
    ) -> Result:
        def conn_err(rc: int, out: str, err: str) -> bool:
            return rc != 0 and (rc in (124, 255) or any(c in err for c in CONN_ERRORS))

        return self.attempt(
            lambda ep: run_proc(self.scp_argv(ep, remote_path, local), timeout), conn_err, rounds, backoff, label="scp"
        )

    def probe(self, ep: Endpoint) -> bool:
        rc, out, _ = run_proc(self.ssh_argv(ep, "echo up"), 25)
        return rc == 0 and "up" in strip_banner(out).split()

    def ensure_helper(self, rounds: int | None = None) -> None:
        """Upload the box scripts and the rendered env in one call. Each file is moved into place from a staging
        dir (a new inode), so a script that bash is executing right now is never rewritten."""
        if self.helper_ok:
            return
        prov = self.ctx.provider
        files = boxenv.box_files(self.ctx.cfg, prov.name, prov.dns_host)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, data in sorted(files.items()):
                ti = tarfile.TarInfo(name)
                ti.size, ti.mode, ti.mtime = len(data), 0o755 if name.endswith(".sh") else 0o644, int(time.time())
                tf.addfile(ti, io.BytesIO(data))
        inc = f"{BOX_HOME}/.incoming"
        cmd = (
            f"mkdir -p {inc} && tar -xz -C {inc} && for f in {' '.join(sorted(files))}; do "
            f"mv -f {inc}/$f {BOX_HOME}/$f; done && echo helper-ok"
        )
        r = self.run(cmd, stdin=buf.getvalue(), timeout=60, rounds=rounds)
        if "helper-ok" not in r.out:
            raise ProviderError(f"could not upload the box files to {BOX_HOME}: {r.err[-200:]}")
        self.helper_ok = True

    def box(self, args: str, timeout: float = 60, rounds: int | None = None, env: dict | None = None) -> Result:
        self.ensure_helper(rounds)
        prefix = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in (env or {}).items() if v not in (None, ""))
        return self.run(f"{prefix + ' ' if prefix else ''}bash {BOX_SH} {args}", timeout=timeout, rounds=rounds)


class _TmpRemote(Remote):
    def __init__(self, ctx: Ctx, iid: Any) -> None:  # unmanaged instance: no state record
        self.ctx, self.iid, self.rec, self.helper_ok = ctx, str(iid), {}, False
        self.known_hosts = ctx.store.dir / f"known_hosts_{iid}"


# ----------------------------------------------------------------------------------------------- offers / renting
def filter_rank_offers(
    offers: list[Offer],
    blocklist: set[str],
    max_price: float | None = None,
    sort: str = "score",
    prefer_host: str | None = None,
    max_ram_gb: float = 0,
) -> list[Offer]:
    """Drop blocklisted hosts and over-price offers; rank by throughput per dollar (or by price), then move
    offers of `prefer_host` (the last host that delivered a verified run) to the front. The blocklist always wins.
    Provider-specific filtering (verified machines, ports, ...) has already happened inside the provider."""
    keep = []
    for o in offers:
        if o.host_id is not None and str(o.host_id) in blocklist:
            continue
        if max_price is not None and (o.price_per_h or 1e9) > max_price:
            continue
        if max_ram_gb and (o.ram_gb or 1e9) > max_ram_gb:  # unknown RAM cannot be shown to fit
            continue
        keep.append(o)
    by_price = sorted(keep, key=lambda o: o.price_per_h or 1e9)
    ranks = {id(o): i for i, o in enumerate(by_price, 1)}
    keep = [replace(o, price_rank=ranks[id(o)]) for o in keep]
    by_price = sorted(keep, key=lambda o: o.price_per_h or 1e9)
    ranked = by_price if sort == "price" else sorted(keep, key=lambda o: (-o.score, o.price_per_h or 1e9))
    if prefer_host:
        ranked = sorted(ranked, key=lambda o: str(o.host_id) != str(prefer_host))  # stable
    return ranked


def search_offers(
    ctx: Ctx, a: argparse.Namespace | None = None, extra_block: set[str] | None = None, max_price: float | None = None
) -> list[Offer]:
    """Ranked offers. Filters come from the CLI flags, else from [offers] of the config."""
    off = ctx.cfg.offers

    def g(k: str, default: Any) -> Any:
        v = getattr(a, k, None) if a is not None else None
        return default if v is None else v

    gpus = g("gpu", None)
    q = OfferQuery(
        gpus=[x.strip() for x in gpus.split(",") if x.strip()] if gpus else list(off.gpus),
        min_vram_gb=float(g("min_vram", off.min_vram_gb)),
        min_disk_gb=float(g("min_disk", off.min_disk_gb)),
        min_reliability=float(g("min_reliability", off.min_reliability)),
        min_inet_mbps=float(g("min_inet", off.min_inet_mbps)),
        min_cuda=float(g("min_cuda", ctx.cfg.min_cuda())),
        interruptible=bool(g("interruptible", off.interruptible)),
    )
    block = ctx.store.blocklist() | set(extra_block or set())
    block |= {h.strip() for h in (g("exclude_hosts", "") or "").split(",") if h.strip()}
    mp = max_price if max_price is not None else g("max_price", None)
    return filter_rank_offers(
        ctx.provider.offers(q),
        block,
        mp,
        g("sort", "score"),
        prefer_host=ctx.store.state.get("last_good_host"),
        max_ram_gb=off.max_ram_gb,
    )


def offer_line(o: Offer) -> str:
    return (
        f"offer {o.id:>9}  {o.gpu_name:<10} {o.vram_gb:4.0f}GB  ${o.price_per_h:.3f}/h (#{o.price_rank})  "
        f"score {o.score:>6}  rel {o.reliability:.4f}  host {o.host_id}  {o.location[:22]:<22}"
    )


def create_instance(
    ctx: Ctx,
    offer: Offer,
    interruptible: bool = False,
    reason: str = "",
    make_current: bool = True,
    image: str | None = None,
) -> str:
    st = ctx.store
    image = image or ctx.cfg.image_ref()
    spec = CreateSpec(image, ctx.policy["disk_gb"], ctx.cfg.project.name, boxenv.ONSTART, interruptible)
    iid = ctx.provider.create(offer, spec)
    rec = st.rec(iid, create=True)
    rec.update(
        {
            "created": time.time(),
            "label": spec.label,
            "host_id": offer.host_id,
            "role": "active" if make_current else "candidate",
            "api_status": "loading",
            "image": image,
            "offer": {
                "id": offer.id,
                "gpu_name": offer.gpu_name,
                "price_per_h": offer.price_per_h,
                "location": offer.location,
                "host_id": offer.host_id,
            },
        }
    )
    if make_current:
        st.state["current"] = iid
    st.event(
        f"created instance on host {offer.host_id} ({offer.gpu_name}, ${offer.price_per_h:.3f}/h) {reason}".strip(),
        iid,
    )
    return iid


class RentRefused(ProviderError):
    """A rent guard said no (instance cap, runway, no offer, all offers gone); nothing was created."""

    def __init__(self, msg: str, retry_later: bool = True) -> None:
        super().__init__(msg)
        self.retry_later = retry_later  # a changed market may help; a full account will not change by itself


def check_cap(ctx: Ctx, overlap_with: str | None = None) -> list[Instance]:
    """ADR 0007: refuse when this project's running + loading instances plus the new one would exceed
    max_instances. An instance is this project's if its label equals the project name or its id is in the
    project's state store (an adopted box keeps a foreign label until relabelled). A replacement whose old box is
    up may overlap it up to max_instances_during_migration."""
    insts = ctx.provider.instances()
    mine = ctx.store.state["instances"]
    up = [i for i in insts if i.status in UP_STATUSES and (i.label == ctx.cfg.project.name or i.id in mine)]
    pol = ctx.policy
    limit = (
        pol["max_instances_during_migration"]
        if overlap_with and any(i.id == str(overlap_with) for i in up)
        else pol["max_instances"]
    )
    if len(up) + 1 > limit:
        names = ", ".join(f"{i.id} (label {i.label or '-'}, {i.status})" for i in up)
        raise RentRefused(
            f"instance cap: {len(up)} instance(s) of this project already up: {names}; limit {limit} "
            "(max_instances). Destroy or stop one (`create --allow-second` overrides it for a manual rent)",
            retry_later=False,
        )
    return insts


RENT_LOCK_WAIT_S = 60


def rent_lock_path() -> Path:
    """One lock per machine and user, outside every project, so two local projects rent one at a time."""
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "taxameter" / "rent.lock"


def rent(ctx: Ctx, search: Callable[[], list[Offer]], **kw: Any) -> tuple[str, Offer]:
    """Serialise rents on this machine (ADR 0008): the lock is held from the cap checks until the create call
    returns, so two local projects cannot both pass the account price check against the same running boxes."""
    lock = FileLock(rent_lock_path())
    for _ in range(RENT_LOCK_WAIT_S):
        if lock.acquire():
            break
        ctx.sleep(1)
    else:
        raise RentRefused(f"another rent on this machine holds {lock.path} for over {RENT_LOCK_WAIT_S} s; try again")
    try:
        return _rent(ctx, search, **kw)
    finally:
        lock.release()


def check_account_price(offer: Offer, up: list[Instance], ceiling: float) -> str | None:
    """ADR 0008: a refusal message when the account's running + loading boxes (any label) plus `offer` would cost
    more than `ceiling` USD/h, else None."""
    total = sum(i.price_per_h for i in up)
    if total + offer.price_per_h <= ceiling + 1e-9:
        return None
    boxes = ", ".join(f"{i.id} (label {i.label or '-'}, ${i.price_per_h:.3f}/h, {i.status})" for i in up)
    return (
        f"account price ceiling: offer {offer.id} at ${offer.price_per_h:.3f}/h on top of ${total:.3f}/h already "
        f"running [{boxes or 'none'}] exceeds max_account_price_per_hour ${ceiling:.3f}"
    )


def _rent(
    ctx: Ctx,
    search: Callable[[], list[Offer]],
    *,
    reason: str = "",
    make_current: bool = True,
    image: str | None = None,
    interruptible: bool | None = None,
    allow_second: bool = False,
    overlap_with: str | None = None,
    check_runway: bool = False,
    only_offer: str | None = None,
    max_price: float | None = None,
    announce: str = "renting ",
) -> tuple[str, Offer]:
    """The one place that rents (under the rent lock). (1) Per-project cap. (2) Try the ranked offers in order, at
    most rent_attempts: per offer the account price ceiling and the runway check, then create. OfferGone (taken
    meanwhile) moves on to the next offer; any other error propagates at once, because a timeout may already have
    created a billed box."""
    st, pol = ctx.store, ctx.policy
    if interruptible is None:  # same default as the offer query, so a bid offer is never rented on-demand
        interruptible = ctx.cfg.offers.interruptible
    insts = [] if allow_second else check_cap(ctx, overlap_with)
    offers = search()
    if only_offer:  # an explicit choice never falls through to another offer
        offers = [o for o in offers if o.id == str(only_offer)]
        if not offers:
            raise RentRefused(
                f"offer {only_offer} is not among the filtered offers (blocklisted/over price/gone?)", retry_later=False
            )
    elif not offers:
        raise RentRefused(f"no offers match under ${max_price}/h" if max_price is not None else "no offers match")
    ceiling = pol.get("max_account_price_per_hour")  # not skipped by allow_second: it is a money cap
    acct_up = [i for i in (insts or ctx.provider.instances()) if i.status in UP_STATUSES] if ceiling is not None else []
    tried: list[str] = []
    over: list[str] = []
    for offer in offers[: int(pol["rent_attempts"])]:
        if ceiling is not None:
            why = check_account_price(offer, acct_up, float(ceiling))
            if why:
                over.append(why)
                continue
        if check_runway:
            bal = spend_summary(ctx.provider.credit(), insts or ctx.provider.instances())
            runway = runway_after(bal, offer.price_per_h)
            if runway is not None and runway < pol["runway_rent_min_h"]:
                raise RentRefused(
                    f"runway would be {runway:.1f} h < runway_rent_min_h {pol['runway_rent_min_h']}: not renting "
                    f"(credit ${bal['credit']:.2f}, offer {offer.id} at ${offer.price_per_h:.3f}/h)"
                )
        say(announce + offer_line(offer))
        try:
            return create_instance(ctx, offer, interruptible, reason, make_current, image), offer
        except OfferGone as e:
            tried.append(offer.id)
            st.event(f"offer {offer.id} (host {offer.host_id}) was taken meanwhile: {e}")
    if over:
        gone = f"; {len(tried)} other offer(s) were gone" if tried else ""
        raise RentRefused(" | ".join(over) + gone)
    raise RentRefused(f"all {len(tried)} offer(s) tried were gone: {', '.join(tried)}")


def wait_ready(ctx: Ctx, iid: str, minutes: float, every: float = 15) -> Endpoint | None:
    """Poll API status and `echo up` on both endpoints until one answers."""
    remote = Remote(ctx, iid)
    deadline = time.time() + minutes * 60
    while True:
        try:
            inst = ctx.provider.instance(iid)
            status = inst.status if inst else None
        except ProviderError as e:
            inst, status = None, f"api error {e}"
        msg = f"status={status}"
        if inst is not None and status == "running":
            eps = list(inst.endpoints)
            ok = {str(ep): remote.probe(ep) for ep in eps}
            remote.rec["endpoints"] = {
                "resolved_at": time.time(),
                "all": [e.to_dict() for e in eps],
                "probe": ok,
                "last_ok": remote.rec.get("endpoints", {}).get("last_ok"),
            }
            msg += "  " + "  ".join(f"{k}={'up' if v else 'down'}" for k, v in ok.items())
            good = [ep for ep in eps if ok[str(ep)]]
            if good:
                remote._mark_ok(good[0])
                ctx.store.event(f"ready: {msg}", iid)
                return good[0]
        say(f"[{iid}] waiting: {msg}")
        if time.time() >= deadline:
            ctx.store.event(f"not ready after {minutes:.0f} min ({msg})", iid)
            return None
        ctx.sleep(every)


# ----------------------------------------------------------------------------------------------- push
def _git(ctx: Ctx, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ctx.root, capture_output=True, check=False)


def git_archive(ctx: Ctx) -> bytes:
    """Committed HEAD (push.paths that exist in HEAD, or the whole tree when none are set) plus each submodule's
    index tree, as one tar.gz. `git archive` skips submodules, so they are archived with a second call and merged
    into the same tarball. A submodule comes from its index (write-tree), not its HEAD: a patch queue that stages
    changes there is included."""
    want = list(ctx.cfg.push.paths)
    if want:
        p = _git(ctx, "ls-tree", "--name-only", "HEAD", "--", *want)
        paths = [ln.strip() for ln in _dec(p.stdout).splitlines() if ln.strip()]
        if p.returncode != 0 or not paths:
            raise ProviderError("nothing to push: commit first (git ls-tree HEAD found none of the archive paths)")
        skipped = [x for x in want if x not in paths]
        if skipped:
            say(f"git archive: not in HEAD, skipped: {', '.join(skipped)}")
    else:
        paths = []
    dirty = _dec(_git(ctx, "status", "--porcelain", *(["--", *paths] if paths else [])).stdout).strip()
    if dirty:
        say(f"WARNING: uncommitted changes are NOT pushed (git archive HEAD): {len(dirty.splitlines())} paths")
    a = _git(ctx, "archive", "--format=tar", "HEAD", *(["--", *paths] if paths else []))
    if a.returncode != 0:
        raise ProviderError(f"git archive failed: {_dec(a.stderr)[-200:]}")
    tars = [a.stdout]
    for sub in ctx.cfg.push.submodules:
        if not (ctx.root / sub / ".git").exists():
            say(f"git archive: submodule {sub} not checked out; not pushed")
            continue
        wt = _git(ctx, "-C", sub, "write-tree")
        tree = _dec(wt.stdout).strip()
        if wt.returncode != 0 or not tree:
            raise ProviderError(f"git write-tree in {sub} failed: {_dec(wt.stderr)[-200:]}")
        s = _git(ctx, "-C", sub, "archive", "--format=tar", f"--prefix={sub}/", tree)
        if s.returncode != 0:
            raise ProviderError(f"git archive of {sub} failed: {_dec(s.stderr)[-200:]}")
        tars.append(s.stdout)
    return _merge_tars(tars)


def _merge_tars(tars: list[bytes]) -> bytes:
    """Concatenated tar streams do not unpack as one, so copy the members into a single gzip tar."""
    out = io.BytesIO()
    seen: set[str] = set()
    with tarfile.open(fileobj=out, mode="w:gz") as dst:
        for raw in tars:
            if not raw:
                continue
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as src:
                for m in src:
                    if m.name in seen or m.name == "pax_global_header":
                        continue
                    seen.add(m.name)
                    dst.addfile(m, src.extractfile(m) if m.isfile() else None)
    return out.getvalue()


def push_code(ctx: Ctx, remote: Remote) -> None:
    remote.ensure_helper()
    data = git_archive(ctx)
    rd = shlex.quote(ctx.cfg.remote_dir)
    r = remote.run(
        f"rm -rf {STAGING_DIR} && mkdir -p {STAGING_DIR} {rd} && tar -xz -C {STAGING_DIR} "
        f"&& bash {BOX_SH} install-push",
        stdin=data,
        timeout=600,
    )
    if r.rc != 0 or "installed" not in r.out:
        raise ProviderError(f"push failed rc={r.rc}: {(r.out + r.err)[-300:]}")
    say(f"[{remote.iid}] pushed HEAD ({len(data) // 1024} KB): {r.out.splitlines()[-1]}")


def push_runs(ctx: Ctx, remote: Remote, dirs: list[str]) -> int:
    """Upload locally fetched results so a resumed batch skips finished jobs (minus results.exclude)."""
    exclude = ctx.cfg.results.exclude
    buf = io.BytesIO()
    n = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for d in dirs:
            p = ctx.root / d
            if p.exists():
                for f in p.rglob("*"):
                    rel = f.relative_to(ctx.root).as_posix()
                    if f.is_file() and not any(fnmatch.fnmatch(rel, g) for g in exclude):
                        tf.add(f, arcname=rel)
                        n += 1
    if n:
        rd = shlex.quote(ctx.cfg.remote_dir)
        r = remote.run(f"mkdir -p {rd} && tar -xz -C {rd} && echo pushed-runs", stdin=buf.getvalue(), timeout=300)
        if "pushed-runs" not in r.out:
            raise ProviderError(f"pushing runs failed: {r.err[-200:]}")
    ctx.store.event(f"pushed {n} local result files back ({', '.join(dirs)})", remote.iid)
    return n


def parse_models(spec: str | None) -> list[str]:
    """`--models a/b:file.gguf,c/d:other.gguf` -> list; each entry is org/name:file[+file...] (see box.sh)."""
    return [m.strip() for m in (spec or "").split(",") if m.strip()]


def configured_models(cfg: Config) -> list[str]:
    try:
        return cfg.model_specs()
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read the model list: {e}") from None


def provision(ctx: Ctx, iid: str, models: list[str] | None = None, wait_min: float | None = None) -> bool:
    st = ctx.store
    remote = Remote(ctx, iid)
    # the watchdog must not idle-stop the box while it downloads and installs (see _tick_active)
    st.rec(iid).update(provision_started=time.time(), provisioned=None)
    st.save()
    push_code(ctx, remote)
    models = list(models or [])
    args = " ".join(shlex.quote(m) for m in models)
    # append (a repeated launch must not wipe the log); remember where this run starts so an old PROVISION_DONE
    # from an earlier provision of the same box is never mistaken for this one
    r = remote.run(
        f'touch {PROVISION_LOG}; n=$(wc -l < {PROVISION_LOG}); echo "launched $n"; '
        f"nohup setsid bash {BOX_SH} provision {args} >> {PROVISION_LOG} 2>&1 < /dev/null &",
        timeout=30,
    )
    m = re.search(r"launched (\d+)", r.out)
    if r.rc != 0 or not m:
        raise ProviderError(f"provision did not start: {(r.out + r.err)[-200:]}")
    offset = int(m.group(1))
    st.event(f"provision launched (models: {', '.join(models) or 'none'}); log {PROVISION_LOG}", iid)
    deadline = time.time() + 60 * (wait_min or ctx.policy["provision_wait_min"])
    last = ""
    while time.time() < deadline:
        ctx.sleep(20)
        try:
            out = remote.run(f"tail -n +{offset + 1} {PROVISION_LOG} | tail -n 40", timeout=30, rounds=1).out
        except Unreachable as e:
            say(f"[{iid}] provision poll: unreachable ({e}); still waiting")
            continue
        tail = out.strip().splitlines()[-1] if out.strip() else ""
        if tail != last:
            say(f"[{iid}] provision: {tail}")
            last = tail
        if "PROVISION_DONE" in out:
            ok = "PROVISION_DONE rc=0" in out
            kv = [ln for ln in out.splitlines() if ln.startswith(("model=", "run=", "watchdog=", "cuda:"))]
            rec = st.rec(iid)
            rec["inventory"] = {"t": time.time(), "models": models, "lines": kv}
            rec["provisioned"] = time.time() if ok else None
            st.event(f"provision {'OK' if ok else 'FAILED'}: {'; '.join(kv)[:300]}", iid)
            if not ok:
                say(out[-1500:])
            return ok
    st.event(f"provision did not finish in time (check {PROVISION_LOG})", iid)
    return False


def start_batch(ctx: Ctx, iid: str, batch: dict) -> Result:
    """Push HEAD and start `batch["script"]` detached on the box (box.sh run; the script guards itself with
    flock). Marks the tracked result dirs as unfetched. Env: [box] run_env plus the batch's own (CLI --env)."""
    remote = Remote(ctx, iid)
    push_code(ctx, remote)
    args = " ".join(shlex.quote(str(x)) for x in [batch["script"], *batch.get("args", [])])
    env = {**ctx.cfg.box.run_env, **batch.get("env", {})}
    r = remote.box(f"run {args}", timeout=90, env=env)
    if r.rc != 0 or "launched" not in r.out:
        raise ProviderError(f"job did not start (rc={r.rc}): {(r.out + r.err)[-300:]}")
    batch = dict(batch, instance=iid, started=time.time(), all_done=False)
    ctx.store.state["batch"] = batch
    rec = ctx.store.rec(iid)
    for d in batch.get("runs_dirs", ctx.cfg.results.dirs):
        rec["runs"].setdefault(d, {"ok": False})
        rec["runs"][d]["ok"] = False  # new results will appear; needs a fresh fetch
    ctx.store.event(f"job started: {batch['script']} {' '.join(map(str, batch.get('args', [])))}".strip(), iid)
    return r


def fetch(ctx: Ctx, iid: str, dirs: list[str] | None = None, minutes: float | None = None) -> bool:
    """Pack small result files on the box, scp the archive back (both endpoints, every ssh_retry_s), verify it
    unpacks and contains done markers, unpack into the repo, record per-dir success in state. A verified fetch
    also records the host as last-good for the next `offers` ranking."""
    st = ctx.store
    rec = st.rec(iid)
    dirs = dirs or list(rec.get("runs") or {}) or list(ctx.cfg.results.dirs)
    minutes = ctx.policy["fetch_wait_min"] if minutes is None else minutes
    rounds = max(1, int(minutes * 60 / ctx.policy["ssh_retry_s"]))
    remote = Remote(ctx, iid)
    r = remote.box("pack " + " ".join(shlex.quote(d) for d in dirs), timeout=300, rounds=rounds)
    if r.rc != 0:
        st.event(f"fetch: pack failed rc={r.rc}: {(r.out + r.err)[-200:]}", iid)
        return False
    remote_md5 = parse_kv(r.out).get("md5")
    local_dir = st.dir / "fetch"
    local_dir.mkdir(parents=True, exist_ok=True)
    archive = local_dir / f"results-{iid}-{datetime.now(tz=UTC).astimezone().strftime('%Y%m%d-%H%M%S')}.tgz"
    remote.scp_from(RESULTS_ARCHIVE, archive, rounds=rounds)
    if not archive.exists():
        st.event("fetch: archive missing after scp", iid)
        return False
    md5 = hashlib.md5(archive.read_bytes()).hexdigest()
    if remote_md5 and md5 != remote_md5:
        st.event(f"fetch: md5 mismatch ({md5} != {remote_md5})", iid)
        return False
    try:
        counts = verify_archive(archive, ctx.cfg.results.done_marker)
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(ctx.root, filter="data")
    except (tarfile.TarError, OSError, ValueError) as e:
        st.event(f"fetch: archive does not unpack: {e}", iid)
        return False
    all_ok = True
    for d in dirs:
        n = sum(v for k, v in counts.items() if k.startswith(d.rstrip("/") + "/"))
        rec["runs"][d] = {
            "ok": n > 0,
            "markers": n,
            "fetched_at": time.time(),
            "archive": archive.name,
            "md5": md5,
        }
        all_ok &= n > 0
    st.event(
        f"fetch: {archive.name} {archive.stat().st_size // 1024} KB, done markers per dir: "
        f"{ {d: rec['runs'][d]['markers'] for d in dirs} } -> {'verified' if all_ok else 'INCOMPLETE'}",
        iid,
    )
    if all_ok and rec.get("host_id"):
        st.state["last_good_host"] = str(rec["host_id"])
        st.save()
    return all_ok


def all_fetched(rec: dict) -> tuple[bool, list[str]]:
    missing = [d for d, v in (rec.get("runs") or {}).items() if not v.get("ok")]
    return (not missing), missing


def destroy(ctx: Ctx, iid: str, force: bool = False, reason: str = "") -> bool:
    st = ctx.store
    rec = st.state["instances"].get(str(iid))
    if rec is None and not force:
        say(f"refusing: {iid} is not managed by taxameter (pass --force to destroy anyway)")
        return False
    if rec is not None:
        ok, missing = all_fetched(rec)
        if not ok and not force:
            say(
                f"refusing to destroy {iid}: runs not fetched+verified: {', '.join(missing)} "
                "(run `fetch`, or pass --force to lose them)"
            )
            return False
        rec["destroyed_inventory"] = {
            "inventory": rec.get("inventory"),
            "runs": rec.get("runs"),
            "last_health": rec.get("last_health"),
        }
    ctx.provider.destroy(str(iid))
    if rec is not None:
        rec["role"] = "gone"
        rec["destroyed"] = time.time()
    if str(st.state.get("current")) == str(iid):
        st.state["current"] = None
    st.event(f"DESTROYED {reason}".strip(), iid)
    return True


def stop(ctx: Ctx, iid: str, reason: str = "") -> None:
    ctx.provider.stop(str(iid))
    rec = ctx.store.state["instances"].get(str(iid))
    if rec is not None:
        rec["stop_requested"] = time.time()
    ctx.store.event(f"stop requested {reason}".strip(), iid)


# ----------------------------------------------------------------------------------------------- health
def health_check(ctx: Ctx, iid: str, inst: Instance | None = None, heal_watchdog: bool = True, rounds: int = 0) -> dict:
    """One check: API status, reachable endpoint, on-box probe; outage/idle bookkeeping in state."""
    st, pol = ctx.store, ctx.policy
    rec = st.rec(iid)
    now = time.time()
    if inst is None:
        inst = ctx.provider.instance(iid)
    status = inst.status if inst else "gone"
    rec["api_status"] = status
    if inst:
        rec["host_id"] = inst.host_id
    probe = None
    endpoint = None
    if status == "running":
        remote = Remote(ctx, iid)
        try:
            r = remote.box("health", timeout=40, rounds=rounds)
            probe, endpoint = parse_kv(r.out), r.endpoint
            if "exp_procs" not in probe:
                probe = None
                say(f"[{iid}] health probe returned no data: {(r.out + r.err)[-200:]}")
        except (Unreachable, ProviderError) as e:
            say(f"[{iid}] unreachable: {e}")
        if probe is not None and heal_watchdog and probe.get("watchdog") == "0":
            try:
                w = remote.box("ensure-watchdog", timeout=60, rounds=0)
                st.event(f"on-box watchdog was not running; relaunched: {w.out.strip()[-120:]}", iid)
            except (Unreachable, ProviderError) as e:
                say(f"[{iid}] could not relaunch watchdog: {e}")
    cls, why = classify(status, probe, pol)
    # outage bookkeeping
    if cls == "unreachable":
        if not rec.get("unreachable_since"):
            rec["unreachable_since"] = now
            st.event("outage started (neither direct nor proxy SSH answers)", iid)
    elif rec.get("unreachable_since") and status == "running":
        start = rec.pop("unreachable_since")
        rec.setdefault("outages", []).append({"start": start, "end": now, "min": round((now - start) / 60, 1)})
        rec["outages"] = rec["outages"][-50:]
        dns = probe.get("dns_ok") if probe else "?"
        st.event(f"outage ended after {(now - start) / 60:.0f} min (box DNS ok={dns})", iid)
    elif status != "running":
        rec.pop("unreachable_since", None)
    # idle bookkeeping
    if cls == "idle":
        rec.setdefault("idle_since", now)
    else:
        rec.pop("idle_since", None)
    if status == "stopped":
        rec.setdefault("stopped_since", now)
    elif status == "running":
        rec.pop("stopped_since", None)
    if probe is not None:
        rec["last_contact"] = {
            "t": now,
            "exp_procs": int(_num(probe, "exp_procs", 0) or 0),
            "batch_alive": int(_num(probe, "batch_alive", 0) or 0),
            "batch_done": probe.get("batch_done"),
            "batch_started": probe.get("batch_started"),
            "batch_all_done": probe.get("batch_all_done"),
        }
        b = st.state.get("batch")
        if (
            b
            and str(b.get("instance")) == str(iid)
            and not b.get("all_done")
            and probe.get("batch_all_done") == "1"
            and probe.get("batch_alive") == "0"
        ):
            b["all_done"] = True
            b["done_at"] = now
            st.event(f"batch ALL DONE ({probe.get('batch_done')} jobs, {probe.get('batch_failed')} failed)", iid)
    unreach_min = (now - rec["unreachable_since"]) / 60 if rec.get("unreachable_since") else 0.0
    idle_min = (now - rec["idle_since"]) / 60 if rec.get("idle_since") else 0.0
    n_out = outages_in_window(rec.get("outages", []), now, pol["outage_window_h"]) + (
        1 if rec.get("unreachable_since") else 0
    )
    h = {
        "t": now,
        "id": iid,
        "status": status,
        "class": cls,
        "why": [w for w in why if w],
        "endpoint": str(endpoint) if endpoint else None,
        "unreachable_min": round(unreach_min, 1),
        "idle_min": round(idle_min, 1),
        "outages_in_window": n_out,
        "probe": probe,
    }
    rec["last_health"] = h
    if n_out >= pol["outage_count"] and status == "running":
        rec["migrate_recommended"] = now
        st.pend(
            f"migrate-{iid}",
            f"instance {iid} (host {rec.get('host_id')}) had {n_out} outages in "
            f"{pol['outage_window_h']} h: migrate (auto with auto_replace / --allow-migrate)",
        )
    elif rec.pop("migrate_recommended", None):
        st.unpend(f"migrate-{iid}")
    st.save()
    return h


def health_line(h: dict) -> str:
    p = h.get("probe") or {}
    bits = [f"[{h['id']}] {h['class'].upper():<11} api={h['status']}"]
    if h.get("endpoint"):
        bits.append(f"via {h['endpoint']}")
    if p:
        bits.append(f"gpu {p.get('gpu_util', '?')}% {p.get('gpu_mem_used', '?')}/{p.get('gpu_mem_total', '?')}MiB")
        bits.append(f"exp={p.get('exp_procs')} batch={p.get('batch_alive')}")
        if p.get("batch_started"):
            bits.append(
                f"jobs {p.get('batch_done')}/{p.get('batch_started')} started"
                f"{' ALL DONE' if p.get('batch_all_done') == '1' else ''}"
            )
        age = _num(p, "progress_age_s")
        if age is not None:
            bits.append(f"log age {age / 60:.0f}m")
        bits.append(f"watchdog={p.get('watchdog')} disk={p.get('disk_free_gb')}G dns={p.get('dns_ok')}")
        bits.append(f"ram={p.get('ram_gb', '?')}G disk_read={p.get('disk_read_mbs', '?')}MB/s")
    if h.get("unreachable_min"):
        bits.append(f"unreachable {h['unreachable_min']:.0f} min")
    if h.get("idle_min"):
        bits.append(f"idle {h['idle_min']:.0f} min")
    if h.get("outages_in_window"):
        bits.append(f"outages/{h.get('outages_in_window')} in window")
    if h.get("why"):
        bits.append("(" + "; ".join(h["why"]) + ")")
    return "  ".join(bits)


# ----------------------------------------------------------------------------------------------- watchdog
@dataclass
class WatchFlags:
    auto_heal: bool = False
    allow_migrate: bool = False
    stop_idle: bool = True
    destroy: bool = True
    dry_run: bool = False


def pending_work(st: Store) -> bool:
    b = st.state.get("batch")
    return bool(b and not b.get("all_done"))


def watchdog_tick(ctx: Ctx, flags: WatchFlags) -> dict:
    """One pass over every managed instance: health, cost control, unreachability, salvage, replacement.
    Raises ProviderError on API failure (the loop backs off). Skipped while `nightly` (another process sharing
    the state dir) holds nightly.lock; the tick holds the lock itself so a nightly cannot start mid-pass."""
    lock = nightly_lock(ctx)
    if not lock.acquire():
        say("watchdog: nightly running, tick skipped")
        return {"skipped": "nightly running"}
    try:
        return _watchdog_tick_locked(ctx, flags)
    finally:
        lock.release()


def _watchdog_tick_locked(ctx: Ctx, flags: WatchFlags) -> dict:
    st = ctx.store
    st.state, ctx.policy = st._load(), ctx.load_policy()  # pick up manual fetch/adopt/block and config edits
    pol = ctx.policy
    now = time.time()
    credit = ctx.provider.credit()
    insts = {i.id: i for i in ctx.provider.instances()}
    bal = spend_summary(credit, list(insts.values()))
    st.state["last_balance"] = dict(bal, t=now)
    runway = bal["runway_h"]
    low = runway is not None and runway < pol["runway_stop_h"]
    if runway is not None and runway < pol["runway_warn_h"]:
        st.pend("balance", f"LOW BALANCE: ${bal['credit']:.2f}, ${bal['spend_per_h']:.3f}/h, runway {runway:.1f} h")
        say(f"WARNING low balance: runway {runway:.1f} h")
    else:
        st.unpend("balance")
    report: dict[str, Any] = {"balance": bal, "instances": {}}
    for iid, rec in list(st.state["instances"].items()):
        if rec.get("role") == "gone":
            continue
        inst = insts.get(iid)
        if inst is None:
            rec["role"] = "gone"
            st.event(f"instance no longer exists at {ctx.provider.name}; marked gone", iid)
            continue
        try:
            rec["api_status"] = inst.status
            if rec.get("role") == "salvage":
                report["instances"][iid] = _tick_salvage(ctx, flags, iid, rec, inst)
            elif rec.get("role") == "candidate":
                _tick_candidate(ctx, iid, rec)
            else:
                report["instances"][iid] = _tick_active(ctx, flags, iid, rec, inst, low)
        except (Unreachable, ProviderError, OSError, subprocess.SubprocessError) as e:
            st.event(f"watchdog step failed: {type(e).__name__}: {e}", iid)
    _maybe_replace(ctx, flags)
    st.state["last_watchdog"] = time.time()
    st.save()
    return report


def _stop_over_max_hours(ctx: Ctx, flags: WatchFlags, iid: str, rec: dict, inst: Instance) -> bool:
    """Hard cost cap: STOP (not destroy: the disk keeps the results) a box running longer than max_box_hours.
    Unfetched results move the box to salvage, which starts it again for short fetch windows."""
    pol = ctx.policy
    if inst.status != "running":
        return False
    started = inst.started_at or rec.get("created") or time.time()
    hours = (time.time() - float(started)) / 3600
    if hours < pol["max_box_hours"]:
        return False
    if time.time() - rec.get("stop_requested", 0) < 600:  # already asked; the API status may lag
        return True
    if flags.dry_run:
        say(f"[{iid}] (dry-run) would stop: running {hours:.1f} h > max_box_hours {pol['max_box_hours']}")
        return True
    stop(ctx, iid, f"(running {hours:.1f} h > max_box_hours {pol['max_box_hours']})")
    if not all_fetched(rec)[0]:
        rec["role"] = "salvage"
        rec["salvage_since"] = time.time()
        rec["last_salvage_attempt"] = time.time()
    return True


def _is_provisioning(pol: dict, rec: dict) -> bool:
    started = rec.get("provision_started")
    return bool(
        started and not rec.get("provisioned") and time.time() - started < (pol["provision_wait_min"] + 10) * 60
    )


def _tick_active(ctx: Ctx, flags: WatchFlags, iid: str, rec: dict, inst: Instance, low_balance: bool) -> dict:
    st, pol = ctx.store, ctx.policy
    if _stop_over_max_hours(ctx, flags, iid, rec, inst):
        return {"id": iid, "status": "max_box_hours"}
    h = health_check(ctx, iid, inst)
    say(health_line(h))
    cls, status = h["class"], h["status"]
    if status == "running" and cls == "unreachable":
        last = rec.get("last_contact") or {}
        busy = (last.get("exp_procs") or 0) > 0 or (last.get("batch_alive") or 0) > 0
        limit = pol["unreachable_stop_busy_min"] if busy else pol["unreachable_stop_idle_min"]
        mins = h["unreachable_min"]
        if mins >= limit:
            why = (
                f"unreachable {mins:.0f} min >= {limit} min; last contact {iso(last.get('t'))}: "
                f"{last.get('exp_procs', '?')} busy procs, batch alive={last.get('batch_alive', '?')}, "
                f"jobs {last.get('batch_done', '?')}/{last.get('batch_started', '?')}"
            )
            if not flags.dry_run:
                stop(ctx, iid, f"({why})")
                host = str(rec.get("host_id") or inst.host_id)
                if host and host != "None" and host not in st.state["blocklist"]:
                    st.state["blocklist"][host] = f"{iso(time.time())}: auto-blocked, {why}"
                    st.event(f"host {host} added to blocklist", iid)
                if not all_fetched(rec)[0]:  # else stay active: stopped_since + destroy_after_stopped_h apply
                    rec["role"] = "salvage"
                    rec["salvage_since"] = time.time()
                    rec["last_salvage_attempt"] = time.time()
                if pending_work(st):
                    st.state["need_replacement"] = {"t": time.time(), "old": iid, "why": "unreachable"}
            else:
                say(f"[{iid}] (dry-run) would stop: {why}")
        elif flags.auto_heal and mins >= pol["reboot_after_unreachable_min"] and not rec.get("rebooted_for_outage"):
            if not flags.dry_run:
                ctx.provider.reboot(iid)
                rec["rebooted_for_outage"] = time.time()
                st.event(
                    f"REBOOT (auto-heal) after {mins:.0f} min unreachable; this restarts the container "
                    "and kills running jobs",
                    iid,
                )
        return h
    if status == "running":
        rec.pop("rebooted_for_outage", None)
    if cls == "stuck":
        st.pend(f"stuck-{iid}", f"instance {iid} looks stuck: {'; '.join(h['why'])} (not killing anything)")
    else:
        st.unpend(f"stuck-{iid}")
    migrate = bool(rec.get("migrate_recommended")) and status == "running" and cls != "unreachable"
    if (
        migrate
        and (flags.allow_migrate or pol["auto_replace"])
        and pending_work(st)
        and not flags.dry_run
        and not st.state.get("need_replacement")
    ):
        st.event("migration: fetching results before replacing this host", iid)
        fetch(ctx, iid)
        st.state["need_replacement"] = {"t": time.time(), "old": iid, "why": "migrate"}
        return h
    if status == "running" and cls == "idle":
        idle_for = h["idle_min"]
        provisioning = _is_provisioning(pol, rec)  # downloads and installs look idle; low balance still stops
        if (flags.stop_idle and idle_for >= pol["idle_stop_min"] and not provisioning) or (
            low_balance and flags.stop_idle
        ):
            why = "low balance" if low_balance and idle_for < pol["idle_stop_min"] else f"idle {idle_for:.0f} min"
            if flags.dry_run:
                say(f"[{iid}] (dry-run) would fetch + stop ({why})")
            else:
                if rec.get("runs"):
                    fetch(ctx, iid)
                stop(ctx, iid, f"({why})")
    if status == "stopped":
        stopped_h = (time.time() - rec.get("stopped_since", time.time())) / 3600
        if stopped_h >= pol["destroy_after_stopped_h"] and flags.destroy:
            ok, missing = all_fetched(rec)
            if ok and not flags.dry_run:
                destroy(ctx, iid, reason=f"(stopped {stopped_h:.1f} h, all results fetched)")
            elif not ok:
                rec["role"] = "salvage"
                rec.setdefault("salvage_since", time.time())
                rec.setdefault("last_salvage_attempt", 0)
                st.event(f"stopped {stopped_h:.1f} h with unfetched runs {missing}: salvage", iid)
    return h


def _tick_salvage(ctx: Ctx, flags: WatchFlags, iid: str, rec: dict, inst: Instance) -> dict:
    """Old box with results not yet fetched: periodically start it, fetch in a short window, destroy when
    verified; give up after destroy_unfetched_after_h and log what was lost."""
    st, pol = ctx.store, ctx.policy
    now = time.time()
    status = inst.status
    ok, missing = all_fetched(rec)
    info = {"id": iid, "role": "salvage", "status": status, "missing": missing}
    say(f"[{iid}] SALVAGE api={status} unfetched={missing or 'none'}")
    if flags.dry_run:
        return info
    if ok:
        if flags.destroy:
            destroy(ctx, iid, reason="(salvage complete: all results fetched and verified)")
        return info
    age_h = (now - rec.get("salvage_since", now)) / 3600
    if age_h >= pol["destroy_unfetched_after_h"] and flags.destroy:
        lost = {d: rec["runs"][d] for d in missing}
        st.event(
            f"salvage gave up after {age_h:.1f} h; LOST results: {json.dumps(lost)}; last contact "
            f"{json.dumps(rec.get('last_contact'))}; inventory {json.dumps(rec.get('inventory'))}",
            iid,
        )
        destroy(ctx, iid, force=True, reason="(salvage timeout, results lost as logged)")
        return info
    window = rec.get("salvage_window_start")
    if status == "running":
        h = health_check(ctx, iid, inst, heal_watchdog=False)
        if h["class"] != "unreachable" and fetch(ctx, iid, minutes=2) and flags.destroy:
            destroy(ctx, iid, reason="(salvage: results fetched and verified)")
            return info
        if window and (now - window) / 60 >= pol["salvage_window_min"]:
            stop(ctx, iid, "(salvage window over)")
            rec.pop("salvage_window_start", None)
        elif not window:
            stop(ctx, iid, "(re-issuing stop: API still reports running)")
    elif status == "stopped":
        rec.pop("salvage_window_start", None)
        if (now - rec.get("last_salvage_attempt", 0)) / 3600 >= pol["salvage_every_h"]:
            ctx.provider.start(iid)
            rec["salvage_window_start"] = now
            rec["last_salvage_attempt"] = now
            st.event(f"salvage: started old instance for a {pol['salvage_window_min']} min fetch window", iid)
    return info


def verify_box(ctx: Ctx, iid: str, models: list[str]) -> bool:
    """Replacement acceptance (box.sh verify): SSH (cached or re-resolved endpoint), GPU visible, CUDA initialises,
    the project's verify hook passes."""
    try:
        r = Remote(ctx, iid).box("verify " + " ".join(models), timeout=240, rounds=2)
    except (Unreachable, ProviderError) as e:
        ctx.store.event(f"verify: unreachable ({e})", iid)
        return False
    kv = parse_kv(r.out)
    ok = kv.get("verify_ok") == "1"
    ctx.store.event(
        f"verify {'OK' if ok else 'FAILED'}: gpu={kv.get('verify_gpu')} cuda={kv.get('verify_cuda')} "
        f"hook={kv.get('verify_hook')}" + ("" if ok else f" {(r.out + r.err)[-300:]}"),
        iid,
    )
    return ok


def _maybe_replace(ctx: Ctx, flags: WatchFlags) -> None:
    """Make-before-break: rent, provision and verify a replacement while the old box keeps running (or stays
    stopped); only then switch state, push fetched results, resume the batch, and fetch + retire the old box.
    A replacement that fails any step is destroyed (it holds no data) and the old box is kept."""
    st, pol = ctx.store, ctx.policy
    need = st.state.get("need_replacement")
    if not need:
        return
    old_id = str(need["old"])
    if not pending_work(st):
        st.state["need_replacement"] = None
        return
    if not (pol["auto_replace"] or flags.allow_migrate):
        st.pend("replace", f"replacement needed ({need.get('why')}) but auto_replace is off")
        return
    if flags.dry_run:
        say("(dry-run) would rent + provision + verify a replacement, then resume the batch there")
        return
    if time.time() < need.get("next_try", 0):
        return
    old = st.rec(old_id)
    max_price = pol["max_price_per_hour"]
    models = list((old.get("inventory") or {}).get("models") or [])
    try:
        new, best = rent(
            ctx,
            lambda: search_offers(ctx, None, extra_block={str(old.get("host_id"))}, max_price=max_price),
            reason=f"(candidate replacement for {old_id}: {need.get('why')})",
            make_current=False,
            overlap_with=old_id,
            check_runway=True,  # the combined rate during the overlap
            max_price=max_price,
        )
    except RentRefused as e:
        st.pend("replace", f"replacement needed but not rented: {e}")
        if e.retry_later:
            need["next_try"] = time.time() + 60 * pol["replace_retry_min"]
        return
    st.unpend("replace")
    ok = (
        wait_ready(ctx, new, pol["create_wait_min"]) is not None
        and provision(ctx, new, models)
        and verify_box(ctx, new, models)
    )
    if not ok:
        st.event(f"candidate failed; destroying it (holds no data) and keeping {old_id}", new)
        st.state["blocklist"].setdefault(str(best.host_id), f"{iso(time.time())}: replacement failed")
        st.rec(new)["runs"] = {}
        destroy(ctx, new, reason="(failed replacement)")
        need["next_try"] = time.time() + 60 * pol["replace_retry_min"]
        return
    # switch
    st.rec(new)["role"] = "active"
    st.state["current"] = new
    batch = st.state["batch"]
    push_runs(ctx, Remote(ctx, new), batch.get("runs_dirs", ctx.cfg.results.dirs))
    start_batch(ctx, new, batch)
    st.state["need_replacement"] = None
    st.unpend(f"migrate-{old_id}")
    st.event(f"switched: {new} is current and running the batch (old {old_id})", new)
    _retire(ctx, flags, old_id)


def _retire(ctx: Ctx, flags: WatchFlags, iid: str) -> None:
    """Old box after a switch: final fetch if it is up, stop it, destroy when everything is fetched, else salvage."""
    st = ctx.store
    rec = st.rec(iid)
    rec["role"] = "retired"
    if rec.get("api_status") == "running":
        try:
            fetch(ctx, iid, minutes=2)
        except (Unreachable, ProviderError) as e:
            say(f"[{iid}] final fetch failed: {e}")
        stop(ctx, iid, "(retired after switch)")
    if all_fetched(rec)[0]:
        if flags.destroy:
            destroy(ctx, iid, reason="(retired; all results fetched)")
    else:
        rec.update(role="salvage", salvage_since=time.time(), last_salvage_attempt=time.time())
        st.event(f"retired with unfetched runs {all_fetched(rec)[1]}: salvage", iid)


def _tick_candidate(ctx: Ctx, iid: str, rec: dict) -> None:
    """A candidate left over from an interrupted replacement (it holds no data): destroy once it is stale."""
    pol = ctx.policy
    age_min = (time.time() - rec.get("created", time.time())) / 60
    if age_min > pol["create_wait_min"] + pol["provision_wait_min"] + 15:
        rec["runs"] = {}
        destroy(ctx, iid, reason=f"(stale candidate, {age_min:.0f} min old, never switched to)")


def watchdog_loop(ctx: Ctx, flags: WatchFlags, once: bool, interval: float) -> int:
    backoff = interval
    while True:
        try:
            watchdog_tick(ctx, flags)
            backoff = interval
        except ProviderError as e:
            backoff = min(max(backoff * 2, interval), ctx.policy["api_backoff_max_s"])
            say(f"watchdog: API error ({e}); next try in {backoff:.0f}s")
        except Exception as e:  # noqa: BLE001 - never let one bad iteration kill the loop
            try:
                ctx.store.event(f"watchdog iteration crashed: {type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                say(f"watchdog iteration crashed: {type(e).__name__}: {e}")
        if once:
            return 0
        ctx.sleep(backoff)


# ----------------------------------------------------------------------------------------------- nightly
def nightly_log(cfg: Config, script: str) -> str:
    """Repo-relative log whose final `<done_line> rc=N` line ends the nightly run."""
    return cfg.box.batch_log or f"{cfg.box.logs_dir}/{Path(script).name.removesuffix('.sh')}.log"


def wait_nightly(ctx: Ctx, iid: str, minutes: float, script: str) -> int | None:
    """Wait on the box (the loop runs there; each ssh call stays under its 540 s timeout) for the nightly log's
    final `<done_line> rc=N` line. Returns rc, or None on timeout."""
    cfg = ctx.cfg
    log, done = nightly_log(cfg, script), cfg.nightly.done_line
    remote = Remote(ctx, iid)
    deadline = time.time() + minutes * 60
    cmd = (
        f"cd {shlex.quote(cfg.remote_dir)} && for i in $(seq 100); do grep -q {shlex.quote('^' + done)} "
        f"{shlex.quote(log)} 2>/dev/null && break; sleep 5; done; date +%T; tail -n 3 {shlex.quote(log)} 2>/dev/null"
    )
    while time.time() < deadline:
        try:
            out = remote.run(cmd, timeout=540).out
        except Unreachable as e:
            say(f"[{iid}] wait: unreachable ({e}); still waiting")
            ctx.sleep(30)
            continue
        say(f"[{iid}] nightly: {(out.strip().splitlines() or [''])[-1]}")
        m = re.search(rf"^{re.escape(done)} rc=(\d+)", out, re.M)
        if m:
            return int(m.group(1))
    return None


def nightly(ctx: Ctx, a: argparse.Namespace) -> dict:
    """create --auto, provision, run the nightly script detached, wait on the box, fetch, destroy. The destroy
    guard applies: a box whose results are not verified stays up for the watchdog to salvage."""
    lock = nightly_lock(ctx)
    if not lock.acquire():
        raise ProviderError(f"another nightly or watchdog tick holds {lock.path}; try again shortly")
    try:
        return _nightly_locked(ctx, a)
    finally:
        lock.release()


def _nightly_locked(ctx: Ctx, a: argparse.Namespace) -> dict:
    st, cfg, pol = ctx.store, ctx.cfg, ctx.policy
    t0 = time.time()
    res: dict[str, Any] = {"ok": False, "rc": None, "id": None}
    # everything that can fail locally happens before money is spent
    script = cfg.nightly.script
    if not script:
        raise ConfigError("no nightly script: set [nightly] script in taxameter.toml")
    models = parse_models(a.models) if a.models else configured_models(cfg)
    max_price = a.max_price if a.max_price is not None else pol["max_price_per_hour"]
    iid, chosen = rent(
        ctx,
        lambda: search_offers(ctx, a, max_price=max_price),
        reason="(nightly)",
        image=a.image,
        check_runway=True,
        max_price=max_price,
        announce="nightly: renting ",
    )
    res["id"] = iid
    price = chosen.price_per_h

    def finish(ok: bool, why: str = "") -> dict:
        hours = (time.time() - t0) / 3600
        res.update(ok=ok, minutes=round(hours * 60, 1), cost=round(hours * price, 3))
        st.event(
            f"nightly {'OK' if ok else 'FAILED'}{' (' + why + ')' if why else ''}: {hours * 60:.0f} min, "
            f"~${hours * price:.2f} at ${price:.3f}/h",
            iid,
        )
        return res

    def abort(why: str) -> dict:
        st.rec(iid)["runs"] = {}  # nothing was produced on the box: the destroy guard has nothing to protect
        destroy(ctx, iid, reason=f"(nightly aborted: {why})")
        return finish(False, why)

    try:
        if wait_ready(ctx, iid, pol["create_wait_min"]) is None:
            return abort("box not reachable")
        if not provision(ctx, iid, models):
            return abort("provision failed")
        start_batch(
            ctx,
            iid,
            {"script": script, "args": list(cfg.nightly.args), "runs_dirs": list(cfg.results.dirs)},
        )
    except (Unreachable, ProviderError) as e:  # nothing was produced yet: do not leave a billed box behind
        return abort(f"{type(e).__name__}: {e}")
    except BaseException as e:  # noqa: BLE001 - incl. KeyboardInterrupt: same rule, then let it propagate
        try:
            abort(f"{type(e).__name__}")
        except Exception as e2:  # noqa: BLE001
            say(f"[{iid}] abort after {type(e).__name__} failed: {e2}; box may still be running")
        raise
    rc = wait_nightly(ctx, iid, a.wait_min or pol["nightly_wait_min"], script)
    res["rc"] = rc
    if rc is not None and st.state.get("batch"):
        st.state["batch"]["all_done"] = True
    try:
        fetched = fetch(ctx, iid)
    except (Unreachable, ProviderError) as e:
        say(f"[{iid}] fetch failed: {e}")
        fetched = False
    if not fetched:
        say(f"[{iid}] results not verified: box kept for the watchdog (destroy guard)")
        return finish(False, "fetch not verified" if rc is not None else "timeout, fetch not verified")
    destroy(ctx, iid, reason="(nightly: results fetched and verified)")
    return finish(rc == 0, "" if rc == 0 else f"nightly rc={rc}" if rc is not None else "timeout")
