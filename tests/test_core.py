"""core.py: ranking, endpoint resolution, ssh retry, health classification, watchdog policies, destroy guard, push,
provision. No network: httpx.MockTransport plays the provider API, subprocess.run is a fake ssh/scp/git."""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import tarfile
import time

import pytest
from conftest import FAKE_BOX_FILES, KEY, R, manage, offer, tick

from taxameter import core as vc
from taxameter.config import DEFAULT_POLICY
from taxameter.providers.base import Endpoint, Instance
from taxameter.providers.vast import offer_from_row


def norm(*a, **kw):  # a normalised Offer from the raw Vast fixture
    return offer_from_row(offer(*a, **kw))


# ------------------------------------------------------------------------------------------------ offers
def test_ranking_blocklist_price_cap_and_price_rank():
    offers = [
        norm(1, 483833, 0.20, 400),  # best score but blocklisted host
        norm(2, 10, 0.30, 250),
        norm(3, 11, 0.15, 200),  # cheapest
        norm(5, 13, 0.60, 500),  # over max price
    ]
    ranked = vc.filter_rank_offers(offers, {"483833"}, max_price=0.45)
    assert [o.id for o in ranked] == ["2", "3"]
    assert {o.id: o.price_rank for o in ranked} == {"3": 1, "2": 2}
    by_price = vc.filter_rank_offers(offers, {"483833"}, max_price=0.45, sort="price")
    assert [o.id for o in by_price] == ["3", "2"]


def test_offers_prefer_last_good_host_but_blocklist_wins():
    offers = [norm(1, 10, 0.20, 400), norm(2, 20, 0.30, 250), norm(3, 30, 0.25, 300)]
    base = vc.filter_rank_offers(offers, set(), max_price=0.45)
    assert [o.id for o in base] == ["1", "3", "2"]
    pref = vc.filter_rank_offers(offers, set(), max_price=0.45, prefer_host="20")
    assert [o.id for o in pref] == ["2", "1", "3"]  # last-good host first, the rest keep their order
    blocked = vc.filter_rank_offers(offers, {"20"}, max_price=0.45, prefer_host="20")
    assert [o.id for o in blocked] == ["1", "3"]
    over = vc.filter_rank_offers(offers, set(), max_price=0.25, prefer_host="20")
    assert [o.id for o in over] == ["1", "3"]  # preference never overrides the price cap


def test_offers_without_host_id_are_never_blocked():
    o = norm(1, 10, 0.2, 100)
    o.host_id = None
    assert [x.id for x in vc.filter_rank_offers([o], {"None", "10"})] == ["1"]


def test_search_offers_uses_config_defaults_and_cli_overrides(env):
    ctx, fake, _ = env
    ctx.cfg.offers.gpus = ["RTX 4090"]
    ctx.cfg.offers.min_vram_gb = 20
    ctx.cfg.image.name, ctx.cfg.image.tag = "ghcr.io/x/box", "abc"
    vc.search_offers(ctx)
    body = json.loads(fake.calls("POST", "/bundles/")[-1][2])
    assert body["gpu_name"] == {"in": ["RTX 4090"]} and body["gpu_ram"]["gte"] == 20 * 950
    assert body["cuda_max_good"] == {"gte": 12.8}  # image tag has no "cuda:x.y": the default
    ctx.cfg.image.name, ctx.cfg.image.tag = "nvidia/cuda:12.4.1-devel", ""
    vc.search_offers(ctx)
    assert json.loads(fake.calls("POST", "/bundles/")[-1][2])["cuda_max_good"] == {"gte": 12.4}
    vc.search_offers(ctx, argparse.Namespace(gpu="A, B", min_cuda=12.1, min_vram=None, max_price=None))
    body = json.loads(fake.calls("POST", "/bundles/")[-1][2])
    assert body["gpu_name"] == {"in": ["A", "B"]} and body["cuda_max_good"] == {"gte": 12.1}
    assert body["gpu_ram"]["gte"] == 20 * 950  # None keeps the config value


# ------------------------------------------------------------------------------------------------ ssh / endpoints
def test_remote_run_reresolves_stale_cached_port(env):
    ctx, fake, shell = env
    rec = manage(ctx, fake, port=41022)
    rec["endpoints"] = {"last_ok": {"kind": "direct", "host": "1.2.3.4", "port": 22488}}  # port before a restart
    shell.down = {22488}
    r = vc.Remote(ctx, 100).run("echo up")
    assert r.out == "up"  # banner stripped
    ports = [int(a[a.index("-p") + 1]) for a in shell.argvs if a[0] == "ssh"]
    assert ports[:2] == [22488, 41022]
    assert ctx.store.rec(100)["endpoints"]["last_ok"]["port"] == 41022
    ssh = shell.argvs[0]
    for opt in ("ConnectTimeout=10", "BatchMode=yes", "StrictHostKeyChecking=accept-new", "IdentitiesOnly=yes"):
        assert opt in ssh
    assert "root@1.2.3.4" in ssh and str(ctx.ssh_key.as_posix()) in ssh


def test_remote_run_raises_unreachable_after_rounds(env):
    ctx, fake, shell = env
    manage(ctx, fake, port=41022)
    shell.down = {41022, 19554}
    with pytest.raises(vc.Unreachable):
        vc.Remote(ctx, 100).run("echo up", rounds=2)
    assert len([a for a in shell.argvs if a[0] == "ssh"]) == 6  # 3 rounds x (direct + proxy)


def test_ensure_helper_uploads_box_files_once_through_staging(env):
    ctx, fake, shell = env
    manage(ctx, fake)
    remote = vc.Remote(ctx, 100)
    remote.box("health")
    remote.box("health")
    cmds = shell.ssh_cmds()
    uploads = [c for c in cmds if ".incoming" in c]
    assert len(uploads) == 1  # one ssh call, once per Remote
    cmd = uploads[0]
    assert "tar -xz -C /root/.taxameter/.incoming" in cmd and "mv -f /root/.taxameter/.incoming/$f" in cmd
    for name in FAKE_BOX_FILES:
        assert name in cmd.split("for f in ")[1].split(";")[0].split()
    with tarfile.open(fileobj=io.BytesIO(shell.stdins[0]), mode="r:gz") as tf:
        assert {m.name: tf.extractfile(m).read() for m in tf} == FAKE_BOX_FILES
    assert [c for c in cmds if "box.sh health" in c] == ["bash /root/.taxameter/box.sh health"] * 2


def test_ensure_helper_failure_is_a_provider_error(env, monkeypatch):
    ctx, fake, shell = env
    manage(ctx, fake)

    def broken(argv, **kw):
        if argv[0] == "ssh" and ".incoming" in argv[-1]:
            return subprocess.CompletedProcess(argv, 1, b"", b"tar: bad archive")
        return shell(argv, **kw)

    monkeypatch.setattr(vc.subprocess, "run", broken)
    with pytest.raises(vc.ProviderError, match="could not upload"):
        vc.Remote(ctx, 100).ensure_helper()


# ------------------------------------------------------------------------------------------------ health
def test_classify_states():
    cfg = DEFAULT_POLICY
    assert vc.classify("running", None, cfg)[0] == "unreachable"
    assert vc.classify("running", {"exp_procs": "3", "gpu_util": "85", "progress_age_s": "60"}, cfg)[0] == "healthy"
    assert vc.classify("running", {"exp_procs": "0", "batch_alive": "0", "gpu_util": "0"}, cfg)[0] == "idle"
    assert vc.classify("running", {"exp_procs": "2", "gpu_util": "70", "progress_age_s": "1500"}, cfg)[0] == "stuck"
    assert vc.classify("running", {"exp_procs": "2", "gpu_util": "0", "progress_age_s": "400"}, cfg)[0] == "stuck"
    assert vc.classify("running", {"exp_procs": "2", "gpu_util": "0", "progress_age_s": "30"}, cfg)[0] == "healthy"
    assert vc.classify("stopped", None, cfg)[0] == "stopped"
    why = vc.classify("running", {"exp_procs": "2", "gpu_util": "70", "progress_age_s": "1500"}, cfg)[1][0]
    assert "busy processes" in why and "build/bench" not in why


def test_health_records_outage_and_recommends_migration(env):
    ctx, fake, shell = env
    rec = manage(ctx, fake)
    now = time.time()
    rec["outages"] = [
        {"start": now - 7000, "end": now - 5000, "min": 33},
        {"start": now - 3000, "end": now - 1500, "min": 25},
    ]
    shell.down = {41022, 19554}
    h = vc.health_check(ctx, "100")
    assert h["class"] == "unreachable" and rec["unreachable_since"]
    assert any(p["key"] == "migrate-100" for p in ctx.store.state["pending"])  # 3rd outage within 3 h
    shell.down = set()
    rec["unreachable_since"] = time.time() - 1680
    h = vc.health_check(ctx, "100")
    assert h["class"] == "healthy" and "unreachable_since" not in rec
    assert rec["outages"][-1]["min"] == pytest.approx(28, abs=0.5)


# ------------------------------------------------------------------------------------------------ money
def test_spend_summary_runway_and_unknown_credit():
    def inst(status, price=0.4, storage=0.06):
        return Instance("1", status, "5", "RTX 3090", price, storage, None, "x")

    s = vc.spend_summary(2.0, [inst("running"), inst("stopped"), inst("loading")])
    assert s["spend_per_h"] == pytest.approx(0.4 + 0.06 + 0.4) and s["running"] == 1 and s["instances"] == 3
    assert s["runway_h"] == pytest.approx(2.0 / 0.86, abs=0.01)
    assert vc.spend_summary(2.0, [])["runway_h"] is None  # nothing billing
    unknown = vc.spend_summary(None, [inst("running")])
    assert unknown["credit"] is None and unknown["runway_h"] is None
    assert vc.runway_after(unknown, 0.3) is None
    assert vc.runway_after(vc.spend_summary(3.0, [inst("running")]), 0.2) == pytest.approx(5.0)


def test_unknown_credit_skips_runway_checks(env, monkeypatch, capsys):
    ctx, fake, _ = env
    monkeypatch.setattr(ctx.provider, "credit", lambda: None)
    manage(ctx, fake)
    tick(ctx)
    assert not ctx.store.state["pending"] and "low balance" not in capsys.readouterr().out
    fake.instances.clear()
    fake.offers = [offer(1, 10, 0.3, 400)]
    iid, _ = vc.rent(ctx, lambda: vc.search_offers(ctx), check_runway=True)
    assert iid == "999"


# ------------------------------------------------------------------------------------------------ destroy / fetch
def test_destroy_refuses_without_fetch_and_force_overrides(env, capsys):
    ctx, fake, _ = env
    rec = manage(ctx, fake)
    rec["runs"] = {"bench/results": {"ok": False}}
    assert vc.destroy(ctx, "100") is False
    assert not fake.calls("DELETE")
    assert "not fetched" in capsys.readouterr().out
    assert vc.destroy(ctx, "100", force=True) is True
    assert fake.calls("DELETE", "/instances/100/")


def test_destroy_refuses_unmanaged_instance(env, capsys):
    ctx, fake, _ = env
    fake.instances[300] = {"id": 300}
    assert vc.destroy(ctx, "300") is False and not fake.calls("DELETE")
    assert "not managed by taxameter" in capsys.readouterr().out


def test_fetch_verifies_unpacks_and_allows_destroy(env):
    ctx, fake, _ = env
    rec = manage(ctx, fake)
    rec["runs"] = {"bench/results": {"ok": False}}
    assert vc.fetch(ctx, "100")
    assert (ctx.root / "bench/results/20260101/DONE").exists()
    assert rec["runs"]["bench/results"]["ok"] and rec["runs"]["bench/results"]["markers"] == 1
    assert vc.destroy(ctx, "100")
    assert fake.calls("DELETE")


def test_fetch_uses_configured_done_marker_and_default_dirs(env):
    ctx, fake, _ = env
    manage(ctx, fake)
    ctx.cfg.results.done_marker = "FINISHED"  # the archive only holds DONE files
    assert vc.fetch(ctx, "100") is False  # no dirs given: falls back to [results] dirs
    assert list(R(ctx)["runs"]) == ["bench/results"] and not R(ctx)["runs"]["bench/results"]["ok"]
    ctx.cfg.results.done_marker = "DONE"
    assert vc.fetch(ctx, "100") is True


def test_verified_fetch_records_last_good_host(env):
    ctx, fake, _ = env
    rec = manage(ctx, fake)
    rec["runs"] = {"bench/results": {"ok": False}}
    assert vc.fetch(ctx, "100")
    assert ctx.store.state["last_good_host"] == "555"


# ------------------------------------------------------------------------------------------------ watchdog
def test_watchdog_idle_fetches_then_stops(env):
    ctx, fake, shell = env
    rec = manage(ctx, fake)
    rec["runs"] = {"bench/results": {"ok": False}}
    rec["idle_since"] = time.time() - 25 * 60
    shell.health = "exp_procs=0\nbatch_alive=0\ngpu_util=0\nwatchdog=1\n"
    tick(ctx)
    assert any(a[0] == "scp" for a in shell.argvs)
    assert json.loads(fake.calls("PUT", "/instances/100/")[0][2]) == {"state": "stopped"}
    assert R(ctx)["runs"]["bench/results"]["ok"]


def test_watchdog_relaunches_dead_onbox_watchdog(env):
    ctx, fake, shell = env
    manage(ctx, fake)
    shell.health = "exp_procs=1\nbatch_alive=1\ngpu_util=80\nprogress_age_s=10\nwatchdog=0\n"
    tick(ctx)
    assert any("ensure-watchdog" in c for c in shell.ssh_cmds())


@pytest.mark.parametrize("busy,minutes,stops", [(False, 50, True), (True, 50, False), (True, 185, True)])
def test_watchdog_stops_unreachable_and_blocklists_host(env, busy, minutes, stops):
    ctx, fake, shell = env
    rec = manage(ctx, fake)
    rec["runs"] = {"bench/results": {"ok": False}}
    rec["last_contact"] = {"t": time.time() - minutes * 60, "exp_procs": 2 if busy else 0, "batch_alive": 0}
    rec["unreachable_since"] = time.time() - minutes * 60
    shell.down = {41022, 19554}
    tick(ctx)
    assert bool(fake.calls("PUT", "/instances/100/")) == stops
    assert ("555" in ctx.store.state["blocklist"]) == stops
    assert (R(ctx)["role"] == "salvage") == stops
    assert not fake.calls("DELETE")


def test_unreachable_stop_without_runs_keeps_box_for_the_12h_rule(env):
    ctx, fake, shell = env
    rec = manage(ctx, fake)
    rec["unreachable_since"] = time.time() - 60 * 60
    shell.down = {41022, 19554}
    tick(ctx)
    tick(ctx)
    assert fake.calls("PUT", "/instances/100/") and not fake.calls("DELETE")
    assert R(ctx)["role"] == "active"


def test_missing_ssh_binary_is_not_an_outage(env, monkeypatch):
    ctx, fake, _ = env
    rec = manage(ctx, fake)
    rec["unreachable_since"] = time.time() - 5 * 3600

    def no_ssh(argv, **kw):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(vc.subprocess, "run", no_ssh)
    ctx.store.save()
    assert vc.watchdog_loop(ctx, vc.WatchFlags(), once=True, interval=1) == 0
    assert not fake.calls("PUT") and not fake.calls("DELETE")
    assert "not found on PATH" in (ctx.store.dir / "events.log").read_text()


def test_watchdog_destroys_long_stopped_only_when_fetched(env):
    ctx, fake, _ = env
    rec = manage(ctx, fake, status="exited")
    rec["stopped_since"] = time.time() - 13 * 3600
    rec["runs"] = {"bench/results": {"ok": False}}
    tick(ctx)
    assert not fake.calls("DELETE") and R(ctx)["role"] == "salvage"
    R(ctx)["runs"]["bench/results"]["ok"] = True
    tick(ctx)
    assert fake.calls("DELETE", "/instances/100/") and R(ctx)["role"] == "gone"


def test_salvage_starts_old_box_then_gives_up_and_logs_losses(env):
    ctx, fake, _ = env
    rec = manage(ctx, fake, status="exited")
    rec.update(
        role="salvage",
        salvage_since=time.time() - 3 * 3600,
        last_salvage_attempt=time.time() - 3 * 3600,
        runs={"bench/results": {"ok": False}},
    )
    tick(ctx)
    assert json.loads(fake.calls("PUT", "/instances/100/")[-1][2]) == {"state": "running"}
    R(ctx)["salvage_since"] = time.time() - 25 * 3600
    tick(ctx)
    assert fake.calls("DELETE")
    log = (ctx.store.dir / "events.log").read_text()
    assert "LOST results" in log and "bench/results" in log


def test_instance_missing_at_provider_is_marked_gone(env):
    ctx, fake, _ = env
    manage(ctx, fake)
    fake.instances.clear()
    tick(ctx)
    assert R(ctx)["role"] == "gone"


@pytest.fixture
def replace_env(env, monkeypatch):
    ctx, fake, shell = env
    ctx.cfg.policy["auto_replace"] = True  # off by default
    ctx.policy = ctx.load_policy()
    ctx.store.state["batch"] = {
        "script": "scripts/nightly.sh",
        "args": [],
        "runs_dirs": ["bench/results"],
        "instance": "100",
    }
    ctx.store.state["need_replacement"] = {"t": time.time(), "old": "100", "why": "migrate"}
    fake.offers = [offer(1, 555, 0.2, 400), offer(2, 20, 0.3, 250)]
    started, order = [], []
    monkeypatch.setattr(vc, "wait_ready", lambda c, i, m: Endpoint("direct", "1.2.3.4", 41022))
    monkeypatch.setattr(vc, "provision", lambda c, i, *a, **k: order.append(("provision", i)) or True)
    monkeypatch.setattr(vc, "push_runs", lambda c, r, d: order.append(("push_runs", r.iid)) or 0)
    monkeypatch.setattr(vc, "start_batch", lambda c, i, b: order.append(("start_batch", i)) or started.append(i))
    return ctx, fake, shell, started, order


def test_make_before_break_switches_only_after_verify(replace_env):
    ctx, fake, shell, _, order = replace_env
    rec = manage(ctx, fake)  # old box still running and reachable
    rec["runs"] = {"bench/results": {"ok": False}}
    rec["inventory"] = {"models": ["org/model:m.gguf"]}
    tick(ctx)
    asks = fake.calls("PUT", "/asks/")
    assert asks and asks[0][1] == "/api/v0/asks/2/"  # the old host 555 is excluded
    assert any("box.sh verify" in c for c in shell.ssh_cmds()), "replacement not verified"
    stops = [r for r in fake.requests if r[0] == "PUT" and r[1] == "/api/v0/instances/100/"]
    assert stops and json.loads(stops[0][2]) == {"state": "stopped"}  # old one stopped only after the switch
    assert order == [("provision", "999"), ("push_runs", "999"), ("start_batch", "999")]
    assert ctx.store.state["current"] == "999" and ctx.store.state["need_replacement"] is None
    assert R(ctx)["runs"]["bench/results"]["ok"]  # final fetch from the old box before retiring it
    assert fake.calls("DELETE", "/instances/100/")  # retired and fully fetched -> destroyed


def test_failed_replacement_is_destroyed_and_old_kept(replace_env):
    ctx, fake, shell, started, _ = replace_env
    manage(ctx, fake)
    shell.verify = "verify_gpu=1\nverify_cuda=0\nverify_hook=1\nverify_ok=0\n"
    tick(ctx)
    assert fake.calls("DELETE", "/instances/999/") and not fake.calls("DELETE", "/instances/100/")
    assert ctx.store.state["current"] == "100" and started == []
    assert "20" in ctx.store.state["blocklist"]
    assert not [r for r in fake.requests if r[0] == "PUT" and r[1] == "/api/v0/instances/100/"]


def test_replacement_limits_and_spend_guard(replace_env):
    ctx, fake, _, _, _ = replace_env
    started = replace_env[3]
    manage(ctx, fake, status="exited")
    manage(ctx, fake, iid=101)  # another running box; old one stopped -> steady-state max 1 applies
    ctx.store.state["current"] = "100"
    tick(ctx, vc.WatchFlags(destroy=False))
    assert not fake.calls("PUT", "/asks/")
    ctx.store.state["instances"].pop("101")
    fake.instances.pop(101)
    fake.credit = 0.5  # runway < 3 h with the new box billing too -> no rental
    tick(ctx, vc.WatchFlags(destroy=False))
    assert not fake.calls("PUT", "/asks/")
    ctx.store.state["need_replacement"]["next_try"] = 0
    fake.credit = 50.0
    tick(ctx, vc.WatchFlags(destroy=False))
    assert fake.calls("PUT", "/asks/2/") and started == ["999"]


def test_replacement_not_rented_is_pending_and_retried_later(replace_env):
    ctx, fake, _, started, _ = replace_env
    manage(ctx, fake)
    fake.offers = []
    tick(ctx)
    assert any(p["key"] == "replace" and "no offers" in p["msg"] for p in ctx.store.state["pending"])
    assert ctx.store.state["need_replacement"]["next_try"] > time.time() and not started


def test_low_balance_warns(env, capsys):
    ctx, fake, _ = env
    manage(ctx, fake)
    fake.credit = 1.0  # 0.4 $/h -> 2.5 h runway
    tick(ctx)
    assert any(p["key"] == "balance" for p in ctx.store.state["pending"])
    assert "low balance" in capsys.readouterr().out


def test_watchdog_loop_survives_errors(env):
    ctx, fake, _ = env
    fake.fail = True
    assert vc.watchdog_loop(ctx, vc.WatchFlags(), once=True, interval=1) == 0


def test_watchdog_stops_box_running_past_max_box_hours(env):
    ctx, fake, shell = env
    rec = manage(ctx, fake)
    rec["runs"] = {"bench/results": {"ok": False}}
    fake.instances[100]["start_date"] = time.time() - 4 * 3600
    tick(ctx)
    assert json.loads(fake.calls("PUT", "/instances/100/")[0][2]) == {"state": "stopped"}
    assert not fake.calls("DELETE")
    assert "max_box_hours" in (ctx.store.dir / "events.log").read_text()
    assert R(ctx)["role"] == "salvage"  # unfetched results: the salvage path fetches them later


def test_watchdog_leaves_young_box_alone(env):
    ctx, fake, shell = env
    manage(ctx, fake)
    tick(ctx)
    assert not fake.calls("PUT", "/instances/100/")


def test_watchdog_does_not_idle_stop_while_provisioning(env):
    ctx, fake, shell = env
    rec = manage(ctx, fake)
    rec["idle_since"] = time.time() - 25 * 60
    rec["provision_started"] = time.time() - 600
    rec["provisioned"] = None
    shell.health = "exp_procs=0\nbatch_alive=0\ngpu_util=0\nwatchdog=1\n"
    tick(ctx)
    assert not fake.calls("PUT", "/instances/100/")
    R(ctx)["provision_started"] = time.time() - 3 * 3600  # a provision that never finished stops blocking
    tick(ctx)
    assert fake.calls("PUT", "/instances/100/")


# ------------------------------------------------------------------------------------------------ push / provision
def git_env(env):
    ctx, fake, shell = env
    (ctx.root / "vendor" / "llama.cpp").mkdir(parents=True)
    (ctx.root / "vendor" / "llama.cpp" / ".git").write_text("gitdir: ../../.git/modules/llama.cpp\n")
    return ctx, shell


def test_git_archive_includes_submodule_in_one_tarball(env):
    ctx, shell = git_env(env)
    data = vc.git_archive(ctx)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        names = tf.getnames()
    assert "vendor/llama.cpp/CMakeLists.txt" in names and "Cargo.toml" in names and "crates/f.txt" in names
    sub_calls = [a for a in shell.argvs if a[0] == "git" and "-C" in a and "archive" in a]
    assert sub_calls and "--prefix=vendor/llama.cpp/" in sub_calls[0]
    main_call = next(a for a in shell.argvs if a[:2] == ["git", "archive"])
    assert main_call[main_call.index("--") + 1 :] == ["crates", "scripts", "Cargo.toml"]  # missing paths skipped


def test_git_archive_without_submodule_or_missing_paths(env, capsys):
    ctx, fake, shell = env
    shell.git_paths = ["scripts"]
    data = vc.git_archive(ctx)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        assert tf.getnames() == ["scripts/f.txt"]
    out = capsys.readouterr().out
    assert "skipped" in out and "submodule vendor/llama.cpp not checked out" in out


def test_git_archive_without_push_paths_takes_whole_head(env):
    ctx, fake, shell = env
    ctx.cfg.push.paths, ctx.cfg.push.submodules = [], []
    with tarfile.open(fileobj=io.BytesIO(vc.git_archive(ctx)), mode="r:gz") as tf:
        assert sorted(tf.getnames()) == ["README.md", "src/main.rs"]
    assert not any("ls-tree" in a for a in shell.argvs)
    main_call = next(a for a in shell.argvs if a[:2] == ["git", "archive"])
    assert main_call[-1] == "HEAD"


def test_git_archive_takes_submodule_from_index_tree(env):
    ctx, shell = git_env(env)
    vc.git_archive(ctx)
    wt = next(a for a in shell.argvs if "write-tree" in a)
    assert wt[:3] == ["git", "-C", "vendor/llama.cpp"]
    sub = next(a for a in shell.argvs if a[0] == "git" and "-C" in a and "archive" in a)
    assert sub[-1] == "deadbeef" and "HEAD" not in sub  # staged patches are included


def test_git_archive_refuses_when_nothing_is_committed(env):
    ctx, fake, shell = env
    shell.git_paths = []
    with pytest.raises(vc.ProviderError, match="commit first"):
        vc.git_archive(ctx)


def test_push_code_sends_gzip_to_configured_dirs(env):
    ctx, fake, shell = env
    manage(ctx, fake)
    git_env(env)
    shell.stdins.clear()
    vc.push_code(ctx, vc.Remote(ctx, 100))
    cmd = next(c for c in shell.ssh_cmds() if "install-push" in c)
    assert "/root/.taxameter/push" in cmd and "/root/proj" in cmd and "tar -xz" in cmd
    assert "bash /root/.taxameter/box.sh install-push" in cmd
    assert shell.stdins[-1][:2] == b"\x1f\x8b"


def test_push_runs_skips_excluded_globs(env):
    ctx, fake, shell = env
    manage(ctx, fake)
    d = ctx.root / "bench" / "results" / "run1"
    d.mkdir(parents=True)
    (d / "DONE").write_text("x")
    (d / "m.json").write_text("{}")
    (d / "model.gguf").write_bytes(b"big")
    (d / "scratch.tmp").write_text("t")
    ctx.cfg.results.exclude = ["*.gguf", "bench/results/*/scratch.tmp"]
    shell.stdins.clear()
    assert vc.push_runs(ctx, vc.Remote(ctx, 100), ["bench/results"]) == 2
    with tarfile.open(fileobj=io.BytesIO(shell.stdins[-1]), mode="r:gz") as tf:
        assert sorted(tf.getnames()) == ["bench/results/run1/DONE", "bench/results/run1/m.json"]
    ctx.cfg.results.exclude = []
    assert vc.push_runs(ctx, vc.Remote(ctx, 100), ["bench/results"]) == 4


def test_parse_models():
    assert vc.parse_models("") == [] and vc.parse_models(None) == []
    assert vc.parse_models("a/b:x.gguf, c/d:y.gguf+z.gguf") == ["a/b:x.gguf", "c/d:y.gguf+z.gguf"]


def test_start_batch_requires_launched_marker(env):
    ctx, fake, shell = env
    manage(ctx, fake)
    shell.run_out = "no such script: scripts/nightly.sh\n"
    with pytest.raises(vc.ProviderError, match="did not start"):
        vc.start_batch(ctx, "100", {"script": "scripts/nightly.sh", "args": []})
    assert ctx.store.state["batch"] is None


def test_start_batch_marks_default_dirs_unfetched(env):
    ctx, fake, shell = env
    rec = manage(ctx, fake)
    rec["runs"] = {"bench/results": {"ok": True}}
    vc.start_batch(ctx, "100", {"script": "scripts/nightly.sh", "args": ["--fast"]})
    assert rec["runs"]["bench/results"]["ok"] is False and ctx.store.state["batch"]["instance"] == "100"
    assert any(c.endswith("box.sh run scripts/nightly.sh --fast") for c in shell.ssh_cmds())


def test_provision_appends_log_and_polls_only_new_lines(env):
    ctx, fake, shell = env
    manage(ctx, fake)
    assert vc.provision(ctx, "100", ["org/m:a.gguf"], wait_min=1)
    launch = next(c for c in shell.ssh_cmds() if "nohup" in c)
    assert ">> /root/provision.log" in launch and "> /root/provision.log" not in launch.replace(">>", "")
    assert "bash /root/.taxameter/box.sh provision org/m:a.gguf" in launch
    poll = next(c for c in shell.ssh_cmds() if "tail -n +" in c)
    assert "provision.log" in poll  # only lines after the launch count, so an old PROVISION_DONE is ignored
    assert R(ctx)["provisioned"]


def test_store_never_creates_config_json(env):
    ctx, fake, _ = env
    manage(ctx, fake)
    ctx.store.event("something")
    assert (ctx.store.dir / "state.json").exists() and not (ctx.store.dir / "config.json").exists()


def test_seed_blocklist_merges_provider_and_config(tmp_path):
    from conftest import make_cfg

    cfg = make_cfg(tmp_path, tmp_path / "k")
    cfg.provider.blocklist = {"4242": "bad host"}
    ctx = vc.Ctx(cfg, sleep=lambda s: None)
    assert {"483833", "366851", "4242"} <= ctx.store.blocklist()
    ctx.store.state["blocklist"]["4242"] = "edited reason"
    ctx.store.save()
    assert vc.Ctx(cfg).store.state["blocklist"]["4242"] == "edited reason"  # the seed never overwrites state
    assert KEY not in json.dumps(ctx.store.state)
