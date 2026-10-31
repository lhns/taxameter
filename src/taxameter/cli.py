"""taxameter command line: argparse commands over `core`. Exit codes: 0 ok, 1 error or failed run, 130 interrupt."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from taxameter import config, core
from taxameter.config import ConfigError
from taxameter.core import Ctx, Remote, ToolMissing, Unreachable, WatchFlags, say
from taxameter.output import Out, iso, redact
from taxameter.providers.base import ProviderError


def parse_env(pairs: list[str] | None) -> dict[str, str]:
    env = {}
    for p in pairs or []:
        k, sep, v = p.partition("=")
        if not sep or not k:
            raise ConfigError(f"--env expects KEY=VALUE, got {p!r}")
        env[k] = v
    return env


# ----------------------------------------------------------------------------------------------- commands
def cmd_balance(ctx: Ctx, a: argparse.Namespace) -> dict:
    s = core.spend_summary(ctx.provider.credit(), ctx.provider.instances())
    rw = f"{s['runway_h']:.1f} h" if s["runway_h"] is not None else "unlimited or unknown"
    credit = f"${s['credit']:.2f}" if s["credit"] is not None else "unknown"
    say(
        f"credit {credit}  spend ${s['spend_per_h']:.3f}/h across {s['instances']} instances "
        f"({s['running']} running)  runway {rw}"
    )
    return s


def cmd_offers(ctx: Ctx, a: argparse.Namespace) -> dict:
    offers = core.search_offers(ctx, a)
    say(
        f"{len(offers)} offers after filters (blocklist: {', '.join(sorted(ctx.store.blocklist()))}); "
        f"top {min(a.top, len(offers))} by {a.sort}:"
    )
    for o in offers[: a.top]:
        say(core.offer_line(o))
    keys = ("id", "gpu_name", "vram_gb", "price_per_h", "price_rank", "score", "reliability", "host_id", "location")
    return {"offers": [{k: getattr(o, k) for k in keys} for o in offers[: a.top]]}


def cmd_create(ctx: Ctx, a: argparse.Namespace) -> dict:
    if not (a.offer or a.auto):
        raise ProviderError("pass --offer ID or --auto")
    max_price = a.max_price if a.max_price is not None else ctx.policy["max_price_per_hour"]
    iid, _ = core.rent(
        ctx,
        lambda: core.search_offers(ctx, a, max_price=max_price),
        image=a.image,
        interruptible=a.interruptible,
        allow_second=a.allow_second,
        only_offer=a.offer,
        max_price=max_price,
    )
    ep = core.wait_ready(ctx, iid, ctx.policy["create_wait_min"])
    return {"id": iid, "endpoint": str(ep) if ep else None}


def cmd_nightly(ctx: Ctx, a: argparse.Namespace) -> dict:
    return core.nightly(ctx, a)


def cmd_adopt(ctx: Ctx, a: argparse.Namespace) -> dict:
    inst = ctx.provider.instance(a.instance)
    if not inst:
        raise ProviderError(f"instance {a.instance} not found")
    name = ctx.cfg.project.name
    if inst.label != name:
        ctx.provider.set_label(a.instance, name)
        say(f"relabelled instance {a.instance}: {inst.label or '-'} -> {name}")
    rec = ctx.store.rec(a.instance, create=True)
    rec.update({"host_id": inst.host_id, "created": inst.started_at, "label": name, "role": "active"})
    for d in a.runs if a.runs is not None else ctx.cfg.results.dirs:
        rec["runs"].setdefault(d, {"ok": False})
    ctx.store.state["current"] = str(a.instance)
    ctx.store.event(f"adopted (host {inst.host_id}, {inst.gpu_name}, status {inst.status})", a.instance)
    return {"id": str(a.instance)}


def cmd_endpoint(ctx: Ctx, a: argparse.Namespace) -> dict:
    iid = ctx.iid(a.id)
    inst = ctx.provider.instance(iid)
    if not inst:
        raise ProviderError(f"instance {iid} not found")
    st = ctx.store
    remote = Remote(ctx, iid) if str(iid) in st.state["instances"] else None
    res: dict[str, Any] = {"id": iid, "status": inst.status, "endpoints": []}
    tester = remote or core._TmpRemote(ctx, iid)
    for ep in inst.endpoints:
        up = tester.probe(ep) if inst.status == "running" else False
        res["endpoints"].append({**ep.to_dict(), "up": up})
        say(f"{ep}: {'UP' if up else 'down'}   {' '.join(shlex.quote(x) for x in tester.ssh_argv(ep, None))}")
        if up and remote:
            remote._mark_ok(ep)
    if not inst.endpoints:
        say(f"no SSH endpoints (status {inst.status})")
    return res


def cmd_ssh(ctx: Ctx, a: argparse.Namespace) -> int:
    iid = ctx.iid(a.id)
    remote = Remote(ctx, iid)
    cmd = " ".join(a.cmd).strip()
    if cmd:
        r = remote.run(cmd, timeout=a.timeout)
        if r.out:
            print(redact(r.out))
        if r.err.strip():
            print(redact(r.err), file=sys.stderr)
        return r.rc
    r = remote.run("echo up", timeout=30)
    say(f"connecting via {r.endpoint}")
    return subprocess.call(remote.ssh_argv(r.endpoint, None, tty=True))  # type: ignore[arg-type]


def cmd_put(ctx: Ctx, a: argparse.Namespace) -> dict:
    """Copy local files to the box through ssh stdin (long `ssh -- "echo <base64> | ..."` lines failed silently)."""
    iid = ctx.iid(a.id)
    remote = Remote(ctx, iid)
    out = {}
    for local in a.files:
        data = Path(local).read_bytes()
        if not a.binary:
            data = data.replace(b"\r\n", b"\n")
        dst = a.dest.rstrip("/") + "/" + Path(local).name if a.dest.endswith("/") else a.dest
        q = shlex.quote(dst)
        r = remote.run(
            f'mkdir -p "$(dirname {q})" && cat > {q}.tmp && mv -f {q}.tmp {q} && md5sum {q}', stdin=data, timeout=120
        )
        want = hashlib.md5(data).hexdigest()
        ok = want in r.out
        say(f"put {local} -> {dst}: {'ok' if ok else 'FAILED'} ({len(data)} bytes)")
        if not ok:
            raise ProviderError(f"upload of {local} failed: {r.err[-200:]}")
        out[dst] = want
    return out


def cmd_provision(ctx: Ctx, a: argparse.Namespace) -> dict:
    iid = ctx.iid(a.id)
    models = core.parse_models(a.models) if a.models else core.configured_models(ctx.cfg)
    ok = core.provision(ctx, iid, models, a.wait_min)
    return {"id": iid, "ok": ok}


def cmd_run(ctx: Ctx, a: argparse.Namespace) -> dict:
    iid = ctx.iid(a.id)
    batch = {
        "script": a.script,
        "args": a.args,
        "runs_dirs": list(ctx.cfg.results.dirs),
        "env": parse_env(a.env),
    }
    r = core.start_batch(ctx, iid, batch)
    say(r.out[-1500:])
    return {"id": iid, "rc": r.rc}


def cmd_fetch(ctx: Ctx, a: argparse.Namespace) -> dict:
    iid = ctx.iid(a.id)
    ok = core.fetch(ctx, iid, a.dirs or None, a.minutes)
    return {"id": iid, "ok": ok, "runs": ctx.store.rec(iid).get("runs")}


def cmd_health(ctx: Ctx, a: argparse.Namespace) -> dict:
    iid = ctx.iid(a.id)
    h = core.health_check(ctx, iid, heal_watchdog=not a.no_heal)
    say(core.health_line(h))
    rec = ctx.store.rec(iid)
    if (
        h["class"] == "unreachable"
        and a.auto_heal
        and h["unreachable_min"] >= a.reboot_after
        and not rec.get("rebooted_for_outage")
    ):
        say("WARNING: rebooting restarts the container (running jobs die)")
        ctx.provider.reboot(iid)
        rec["rebooted_for_outage"] = time.time()
        ctx.store.event(f"REBOOT (auto-heal) after {h['unreachable_min']:.0f} min unreachable", iid)
    if h["class"] == "stuck":
        say("STUCK: reporting only; nothing is killed automatically")
    if h["class"] == "idle" and a.stop_idle and h["idle_min"] >= ctx.policy["idle_stop_min"]:
        if rec.get("runs"):
            core.fetch(ctx, iid)
        core.stop(ctx, iid, f"(idle {h['idle_min']:.0f} min)")
    return h


def cmd_watch(ctx: Ctx, a: argparse.Namespace) -> int:
    while True:
        try:
            cmd_health(ctx, a)
        except Exception as e:  # noqa: BLE001 - keep watching
            say(f"health failed: {type(e).__name__}: {e}")
        ctx.sleep(a.interval)


def cmd_watchdog(ctx: Ctx, a: argparse.Namespace) -> int:
    flags = WatchFlags(
        auto_heal=a.auto_heal,
        allow_migrate=a.allow_migrate,
        stop_idle=not a.no_stop_idle,
        destroy=not a.no_destroy,
        dry_run=a.dry_run,
    )
    if not a.once:
        ctx.store.event(f"local watchdog started (interval {a.interval}s, {flags})")
    return core.watchdog_loop(ctx, flags, a.once, a.interval or ctx.policy["watch_interval_s"])


def cmd_status(ctx: Ctx, a: argparse.Namespace) -> dict:
    st = ctx.store
    res: dict[str, Any] = {}
    try:
        insts = ctx.provider.instances()
        bal = core.spend_summary(ctx.provider.credit(), insts)
        res["balance"] = bal
        rw = f"{bal['runway_h']:.1f} h" if bal["runway_h"] is not None else "unlimited or unknown"
        credit = f"${bal['credit']:.2f}" if bal["credit"] is not None else "unknown"
        say(f"credit {credit}  spend ${bal['spend_per_h']:.3f}/h  runway {rw}")
        for i in insts:
            rec = st.state["instances"].get(i.id)
            say(
                f"instance {i.id}  {i.status:<8} {i.gpu_name}  ${i.price_per_h:.3f}/h"
                f"  host {i.host_id}  label {i.label}  "
                f"{'managed:' + rec.get('role', '?') if rec else 'unmanaged'}"
            )
        res["instances"] = [
            {k: getattr(i, k) for k in ("id", "status", "gpu_name", "price_per_h", "host_id", "label")} for i in insts
        ]
    except ProviderError as e:
        say(f"API unavailable: {e}")
    say(f"current: {st.state.get('current')}  batch: {json.dumps(st.state.get('batch'))[:200]}")
    for iid, rec in st.state["instances"].items():
        if rec.get("role") == "gone":
            continue
        h = rec.get("last_health")
        if h:
            say(f"last health ({iso(h['t'])}): " + core.health_line(h))
        outs = rec.get("outages") or []
        if outs or rec.get("unreachable_since"):
            say(
                f"[{iid}] outages: "
                + ", ".join(f"{iso(o['start'])[11:16]}+{o['min']:.0f}m" for o in outs[-10:])
                + (f"  OPEN since {iso(rec['unreachable_since'])}" if rec.get("unreachable_since") else "")
            )
        ok, missing = core.all_fetched(rec)
        say(f"[{iid}] role={rec.get('role')} runs fetched: {'all' if ok else 'missing ' + ', '.join(missing)}")
    for p in st.state.get("pending", []):
        say(f"PENDING ({iso(p['t'])}): {p['msg']}")
    say(f"blocklist: {json.dumps(st.state['blocklist'])}")
    say(f"last watchdog pass: {iso(st.state.get('last_watchdog'))}")
    for e in st.state["events"][-a.events :]:
        say(f"  event {iso(e['t'])} {'[' + str(e['id']) + '] ' if e.get('id') else ''}{e['msg']}")
    res.update(
        {
            "current": st.state.get("current"),
            "pending": st.state.get("pending"),
            "managed": st.state["instances"],
            "blocklist": st.state["blocklist"],
        }
    )
    return res


def cmd_simple(ctx: Ctx, a: argparse.Namespace) -> dict:
    iid = ctx.iid(a.id)
    if a.command == "stop":
        core.stop(ctx, iid, "(manual)")
    elif a.command == "start":
        ctx.provider.start(iid)
        ctx.store.event("start requested (manual)", iid)
        if a.wait:
            core.wait_ready(ctx, iid, ctx.policy["create_wait_min"])
    elif a.command == "reboot":
        say("WARNING: reboot restarts the container; running jobs die")
        ctx.provider.reboot(iid)
        ctx.store.event("reboot requested (manual)", iid)
    return {"id": iid, "action": a.command}


def cmd_destroy(ctx: Ctx, a: argparse.Namespace) -> dict:
    iid = ctx.iid(a.id)
    if not a.yes:
        raise ProviderError("destroy is irreversible: pass --yes")
    ok = core.destroy(ctx, iid, force=a.force, reason="(manual)")
    return {"id": iid, "destroyed": ok}


def cmd_logs(ctx: Ctx, a: argparse.Namespace) -> dict:
    iid = ctx.iid(a.id)
    r = Remote(ctx, iid).box(f"logs {int(a.n)}", timeout=40)
    print(redact(r.out))
    return {"id": iid}


def cmd_block(ctx: Ctx, a: argparse.Namespace) -> dict:
    if a.remove:
        ctx.store.state["blocklist"].pop(str(a.host), None)
        ctx.store.event(f"host {a.host} removed from blocklist")
    else:
        ctx.store.state["blocklist"][str(a.host)] = a.reason or "manual"
        ctx.store.event(f"host {a.host} blocklisted: {a.reason or 'manual'}")
    return {"blocklist": ctx.store.state["blocklist"]}


# ----------------------------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS: the options work before or after the subcommand without the subparser resetting them
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output on stdout"
    )
    common.add_argument(
        "--id", default=argparse.SUPPRESS, help="instance id (default: the current instance in the state file)"
    )
    common.add_argument(
        "--config", default=argparse.SUPPRESS, help="path to taxameter.toml (default: nearest one above the cwd)"
    )
    p = argparse.ArgumentParser(prog="taxameter", description=(__doc__ or "").split("\n")[0], parents=[common])
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("balance", parents=[common], help="credit, spend rate, runway")
    for name in ("offers", "create"):
        s = sub.add_parser(name, parents=[common], help="search offers" if name == "offers" else "rent + wait")
        s.add_argument("--gpu", help="comma list (default [offers] gpus)")
        s.add_argument("--min-vram", type=float)
        s.add_argument("--max-price", type=float)
        s.add_argument("--min-reliability", type=float)
        s.add_argument("--min-inet", type=float)
        s.add_argument("--min-disk", type=float)
        s.add_argument("--min-cuda", type=float, help="default: [offers] min_cuda, else from the image name")
        s.add_argument("--exclude-hosts", default="", help="extra host ids to skip (comma list)")
        s.add_argument("--interruptible", action="store_true", default=None)
        s.add_argument("--sort", choices=["score", "price"], default="score")
        s.add_argument("--top", type=int, default=10)
        if name == "create":
            s.add_argument("--offer", help="offer id from `offers` (never falls through to another offer)")
            s.add_argument("--auto", action="store_true", help="best score within max price")
            s.add_argument("--allow-second", action="store_true", help="skip the per-project instance cap")
            s.add_argument("--image", help="container image (default [image] in taxameter.toml)")
    s = sub.add_parser("adopt", parents=[common], help="manage an existing instance")
    s.add_argument("instance")
    s.add_argument("--runs", nargs="*", help="run dirs on the box to track for fetch (default [results] dirs)")
    sub.add_parser("endpoint", parents=[common], help="resolve + test current SSH endpoints")
    s = sub.add_parser("ssh", parents=[common], help="ssh (interactive) or run `-- cmd`")
    s.add_argument("--timeout", type=float, default=120)
    s.add_argument("cmd", nargs=argparse.REMAINDER)
    s = sub.add_parser("put", parents=[common], help="copy local files to the box (via ssh stdin, md5-checked)")
    s.add_argument("files", nargs="+")
    s.add_argument("dest", help="remote path; ending in / means a directory (one file per local name)")
    s.add_argument("--binary", action="store_true", help="keep CRLF bytes (default: text, CRLF -> LF)")
    s = sub.add_parser("provision", parents=[common], help="push HEAD, run the provision hook, models, watchdog")
    s.add_argument("--models", default="", help="comma list of org/name:file (default: [models] of the config)")
    s.add_argument("--wait-min", type=float)
    s = sub.add_parser("run", parents=[common], help="push HEAD + start a script detached on the box")
    s.add_argument("script", help="repo-relative script")
    s.add_argument("args", nargs="*")
    s.add_argument("--env", action="append", metavar="K=V", help="env for the script, on top of [box] run_env")
    s = sub.add_parser(
        "nightly", parents=[common], help="create, provision, run the nightly script, wait, fetch, destroy"
    )
    s.add_argument("--models", default="", help="comma list of org/name:file (default: [models] of the config)")
    s.add_argument("--image", help="container image (default [image] in taxameter.toml)")
    s.add_argument("--gpu", help="comma list (default [offers] gpus)")
    s.add_argument("--max-price", type=float)
    s.add_argument("--wait-min", type=float, help="how long to wait for the on-box run (default nightly_wait_min)")
    s = sub.add_parser("fetch", parents=[common], help="pack results on the box, scp back, verify, unpack")
    s.add_argument("dirs", nargs="*")
    s.add_argument("--minutes", type=float, help="keep retrying this long")
    for name in ("health", "watch"):
        s = sub.add_parser(name, parents=[common], help="one health check" if name == "health" else "loop")
        s.add_argument("--auto-heal", action="store_true", help="reboot once when unreachable (restarts container)")
        s.add_argument("--reboot-after", type=float, default=config.DEFAULT_POLICY["reboot_after_unreachable_min"])
        s.add_argument("--stop-idle", action="store_true", help="fetch + stop when idle >= idle_stop_min")
        s.add_argument("--no-heal", action="store_true", help="do not relaunch a dead on-box watchdog")
        if name == "watch":
            s.add_argument("--interval", type=float, default=60)
    s = sub.add_parser("watchdog", parents=[common], help="local health + cost-control loop")
    s.add_argument("--once", action="store_true", help="one pass (for cron or Task Scheduler)")
    s.add_argument("--interval", type=float)
    s.add_argument("--auto-heal", action="store_true")
    s.add_argument("--allow-migrate", action="store_true")
    s.add_argument("--no-stop-idle", action="store_true")
    s.add_argument("--no-destroy", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="report decisions, change nothing")
    s = sub.add_parser("status", parents=[common], help="instances, balance, health, outages, pending actions")
    s.add_argument("--events", type=int, default=15)
    for name in ("stop", "start", "reboot"):
        s = sub.add_parser(name, parents=[common])
        if name == "start":
            s.add_argument("--wait", action="store_true")
    s = sub.add_parser("destroy", parents=[common], help="irreversible; needs --yes and fetched runs")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--force", action="store_true", help="destroy even with unfetched runs / unmanaged id")
    s = sub.add_parser("logs", parents=[common], help="tail the batch log and the watchdog log")
    s.add_argument("-n", type=int, default=30)
    s = sub.add_parser("block", parents=[common], help="add/remove a host in the blocklist")
    s.add_argument("host")
    s.add_argument("--reason")
    s.add_argument("--remove", action="store_true")
    return p


COMMANDS: dict[str, Callable[[Ctx, argparse.Namespace], Any]] = {
    "balance": cmd_balance,
    "offers": cmd_offers,
    "create": cmd_create,
    "adopt": cmd_adopt,
    "endpoint": cmd_endpoint,
    "ssh": cmd_ssh,
    "put": cmd_put,
    "provision": cmd_provision,
    "run": cmd_run,
    "nightly": cmd_nightly,
    "fetch": cmd_fetch,
    "health": cmd_health,
    "watch": cmd_watch,
    "watchdog": cmd_watchdog,
    "status": cmd_status,
    "stop": cmd_simple,
    "start": cmd_simple,
    "reboot": cmd_simple,
    "destroy": cmd_destroy,
    "logs": cmd_logs,
    "block": cmd_block,
}


def main(argv: list[str] | None = None, ctx: Ctx | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252; box output is UTF-8
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    a = build_parser().parse_args(argv)
    a.json, a.id = getattr(a, "json", False), getattr(a, "id", None)
    if a.command == "ssh" and a.cmd and a.cmd[0] == "--":
        a.cmd = a.cmd[1:]
    Out.json_mode = bool(a.json)
    try:
        if ctx is None:
            cfg_path = getattr(a, "config", None)
            ctx = Ctx(config.load(Path(cfg_path) if cfg_path else None))
        res = COMMANDS[a.command](ctx, a)
    except (ProviderError, Unreachable, ToolMissing, ConfigError) as e:
        say(f"ERROR: {e}")
        if a.json:
            print(redact(json.dumps({"error": str(e)})))
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001 - a bug must still print a readable, secret-free traceback
        print(redact(traceback.format_exc()), file=sys.stderr, flush=True)
        if a.json:
            print(redact(json.dumps({"error": "unexpected error, see stderr"})))
        return 1
    if isinstance(res, int):
        return res
    if a.json:
        print(redact(json.dumps(res, indent=1, default=str)))
    if a.command == "destroy" and isinstance(res, dict) and not res.get("destroyed"):
        return 1
    if a.command == "nightly" and isinstance(res, dict) and not res.get("ok"):
        return 1
    return 0
