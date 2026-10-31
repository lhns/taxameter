"""nightly orchestration, the rent helper (offer fall-through, per-project cap) and the nightly lock."""

from __future__ import annotations

import itertools
import json

import pytest
from conftest import R, make_instance, manage, offer, tick

from taxameter import cli
from taxameter import core as vc
from taxameter.providers.base import Endpoint

SPEC = '[model.a]\nhf = "org/a:a.gguf"\n[model.b]\nhf = "org/b:b.gguf"\n[model.c]\nname = "no hf"\n'


@pytest.fixture
def nightly_env(env, monkeypatch):
    ctx, fake, shell = env
    fake.offers = [offer(1, 555, 0.3, 400)]
    order = []
    monkeypatch.setattr(vc, "wait_ready", lambda c, i, m: order.append("wait_ready") or Endpoint("d", "1.2.3.4", 1))
    monkeypatch.setattr(vc, "provision", lambda c, i, models=None, w=None: order.append(("provision", models)) or True)

    def start(c, i, b):
        order.append(("start", b["script"], b["args"]))
        c.store.rec(i)["runs"].update({"bench/results": {"ok": False}})
        return vc.Result(0, "", "")

    monkeypatch.setattr(vc, "start_batch", start)

    def fake_fetch(c, i, *a, **k):
        order.append("fetch")
        c.store.rec(i)["runs"]["bench/results"]["ok"] = shell.fetch_ok
        return shell.fetch_ok

    shell.fetch_ok = True
    monkeypatch.setattr(vc, "fetch", fake_fetch)
    spec = ctx.root / "bench" / "spec" / "phase0.toml"  # nightly resolves the spec against the repo root
    spec.parent.mkdir(parents=True)
    spec.write_text(SPEC, encoding="utf-8")
    return ctx, fake, shell, order


def run_nightly(ctx, *argv):
    return cli.main(["nightly", *argv], ctx)


def test_nightly_orchestration_order_and_cost(nightly_env):
    ctx, fake, shell, order = nightly_env
    assert run_nightly(ctx, "--models", "org/m:a.gguf,org/n:b.gguf") == 0
    assert order == [
        "wait_ready",
        ("provision", ["org/m:a.gguf", "org/n:b.gguf"]),
        ("start", "scripts/nightly.sh", []),
        "fetch",
    ]
    assert fake.calls("PUT", "/asks/1/") and fake.calls("DELETE", "/instances/999/")  # created, then destroyed last
    first_ask = next(i for i, r in enumerate(fake.requests) if r[0] == "PUT" and "/asks/" in r[1])
    assert first_ask < next(i for i, r in enumerate(fake.requests) if r[0] == "DELETE")
    assert any("runs/vast/nightly.log" in c and "sleep 5" in c for c in shell.ssh_cmds())  # wait on the box
    log = (ctx.store.dir / "events.log").read_text()
    assert "nightly OK" in log and "/h" in log and "DESTROYED" in log


def test_nightly_uses_configured_models_script_args_and_done_line(nightly_env):
    ctx, fake, shell, order = nightly_env
    ctx.cfg.nightly.args = ["--spec", "x.toml"]
    ctx.cfg.nightly.done_line = "ALL_FINISHED"
    ctx.cfg.box.batch_log = ""  # falls back to <logs_dir>/<script name>.log
    ctx.cfg.nightly.script = "tools/go.sh"
    shell.done_line = "ALL_FINISHED"
    assert run_nightly(ctx) == 0
    assert ("provision", ["org/a:a.gguf", "org/b:b.gguf"]) in order  # models of the configured TOML, from_key
    assert ("start", "tools/go.sh", ["--spec", "x.toml"]) in order
    wait = next(c for c in shell.ssh_cmds() if "sleep 5" in c)
    assert "runs/vast/go.log" in wait and "'^ALL_FINISHED'" in wait


def test_nightly_wait_returns_rc_only_for_the_configured_done_line(env, monkeypatch):
    ctx, fake, shell = env
    manage(ctx, fake)
    clock = itertools.count(0, 30)  # every time.time() call advances 30 s, so the 1 min deadline passes
    monkeypatch.setattr(vc.time, "time", lambda: next(clock))
    ctx.cfg.nightly.done_line = "OTHER_DONE"  # the fake box prints NIGHTLY_DONE: never matches
    assert vc.wait_nightly(ctx, "100", 1, "scripts/nightly.sh") is None
    assert any("sleep 5" in c for c in shell.ssh_cmds())  # it did ask the box
    ctx.cfg.nightly.done_line = "NIGHTLY_DONE"
    shell.nightly_rc = 3
    assert vc.wait_nightly(ctx, "100", 1, "scripts/nightly.sh") == 3


def test_nightly_without_script_fails_before_renting(nightly_env, capsys):
    ctx, fake, shell, order = nightly_env
    ctx.cfg.nightly.script = ""
    assert run_nightly(ctx) == 1
    assert "[nightly] script" in capsys.readouterr().out
    assert not fake.requests and order == []


def test_nightly_failed_fetch_keeps_box_and_fails(nightly_env):
    ctx, fake, shell, order = nightly_env
    shell.fetch_ok = False
    assert run_nightly(ctx) == 1
    assert not fake.calls("DELETE")  # the destroy guard holds the box for the watchdog
    assert "999" in ctx.store.state["instances"] and R(ctx, 999)["role"] == "active"


def test_nightly_script_failure_still_fetches_and_destroys_but_exits_nonzero(nightly_env):
    ctx, fake, shell, order = nightly_env
    shell.nightly_rc = 1
    assert run_nightly(ctx) == 1
    assert order[-1] == "fetch" and fake.calls("DELETE", "/instances/999/")


def test_nightly_provision_failure_destroys_empty_box(nightly_env, monkeypatch):
    ctx, fake, shell, order = nightly_env
    monkeypatch.setattr(vc, "provision", lambda *a, **k: False)
    assert run_nightly(ctx) == 1
    assert fake.calls("DELETE", "/instances/999/") and "fetch" not in order


def test_nightly_unreachable_during_provision_destroys_box(nightly_env, monkeypatch):
    ctx, fake, shell, order = nightly_env

    def boom(*a, **k):
        raise vc.Unreachable("no endpoints")

    monkeypatch.setattr(vc, "provision", boom)
    assert run_nightly(ctx) == 1
    assert fake.calls("DELETE", "/instances/999/")


def test_nightly_start_failure_destroys_box(nightly_env, monkeypatch):
    ctx, fake, shell, order = nightly_env

    def bad_start(c, i, b):
        raise vc.ProviderError("job did not start")

    monkeypatch.setattr(vc, "start_batch", bad_start)
    assert run_nightly(ctx) == 1
    assert fake.calls("DELETE", "/instances/999/")


def test_nightly_computes_models_before_renting(nightly_env):
    ctx, fake, shell, order = nightly_env
    (ctx.root / "bench" / "spec" / "phase0.toml").unlink()
    assert run_nightly(ctx) == 1
    assert not fake.calls("PUT", "/asks/")  # nothing rented


def test_nightly_resolves_model_list_against_config_root_not_cwd(nightly_env, monkeypatch, tmp_path):
    ctx, fake, shell, order = nightly_env
    (ctx.root / "bench" / "spec" / "phase0.toml").write_text('[model.a]\nhf = "org/root:root.gguf"\n', encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert run_nightly(ctx) == 0
    assert ("provision", ["org/root:root.gguf"]) in order


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), RuntimeError("boom")])
def test_nightly_any_exception_before_job_start_destroys_box(nightly_env, monkeypatch, exc):
    ctx, fake, shell, order = nightly_env

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(vc, "provision", boom)
    with pytest.raises(type(exc)):
        vc.nightly(ctx, cli.build_parser().parse_args(["nightly"]))
    assert fake.calls("DELETE", "/instances/999/")


@pytest.mark.parametrize("exc", [vc.Unreachable("down"), vc.ProviderError("api")])
def test_nightly_fetch_error_keeps_box_for_watchdog(nightly_env, monkeypatch, exc):
    ctx, fake, shell, order = nightly_env

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(vc, "fetch", boom)
    assert run_nightly(ctx) == 1
    assert not fake.calls("DELETE")
    assert "nightly FAILED" in (ctx.store.dir / "events.log").read_text()


def test_nightly_lock_excludes_watchdog_tick_and_second_nightly(nightly_env):
    ctx, fake, shell, order = nightly_env
    lock = vc.FileLock(ctx.store.dir / "nightly.lock")
    assert lock.acquire()
    try:
        assert vc.watchdog_tick(ctx, vc.WatchFlags()) == {"skipped": "nightly running"}
        assert not fake.requests  # the tick did not even call the API
        assert run_nightly(ctx) == 1 and not fake.calls("PUT", "/asks/")
    finally:
        lock.release()
    assert vc.watchdog_tick(ctx, vc.WatchFlags())["balance"]  # released: ticks run again
    assert run_nightly(ctx) == 0  # and the tick released its own hold


def test_nightly_respects_runway_minimum(nightly_env):
    ctx, fake, shell, order = nightly_env
    fake.credit = 0.5  # 0.3 $/h -> 1.7 h < runway_rent_min_h
    assert run_nightly(ctx) == 1
    assert not fake.calls("PUT", "/asks/") and order == []


# ------------------------------------------------------------------------------------------------ OfferGone
def three_gone_offers(fake, price3=0.35):
    fake.offers = [offer(1, 10, 0.2, 400), offer(2, 11, 0.2, 300), offer(3, 12, price3, 200), offer(4, 13, 0.2, 100)]


def asks(fake):
    return [r[1] for r in fake.calls("PUT", "/asks/")]


def test_create_auto_falls_through_to_the_third_offer(env):
    ctx, fake, _ = env
    three_gone_offers(fake)
    fake.gone_asks = {1, 2}
    assert cli.main(["create", "--auto"], ctx) == 0
    assert asks(fake) == ["/api/v0/asks/1/", "/api/v0/asks/2/", "/api/v0/asks/3/"]
    assert ctx.store.state["current"] == "999" and R(ctx, 999)["offer"]["id"] == "3"
    log = (ctx.store.dir / "events.log").read_text()
    assert "offer 1 " in log and "offer 2 " in log and "taken meanwhile" in log


def test_create_auto_all_attempts_gone_is_an_error_without_instance(env, capsys):
    ctx, fake, _ = env
    three_gone_offers(fake)
    fake.gone_asks = {1, 2, 3, 4}
    assert cli.main(["create", "--auto"], ctx) == 1
    assert len(asks(fake)) == 3  # rent_attempts, the fourth offer is never tried
    assert not ctx.store.state["instances"] and ctx.store.state["current"] is None
    assert "all 3 offer(s) tried were gone" in capsys.readouterr().out


def test_rent_attempts_follow_policy(env):
    ctx, fake, _ = env
    three_gone_offers(fake)
    fake.gone_asks = {1, 2, 3, 4}
    ctx.policy["rent_attempts"] = 4
    with pytest.raises(vc.RentRefused, match="all 4"):
        vc.rent(ctx, lambda: vc.search_offers(ctx))
    assert len(asks(fake)) == 4


def test_create_explicit_offer_never_falls_through(env):
    ctx, fake, _ = env
    three_gone_offers(fake)
    fake.gone_asks = {1}
    assert cli.main(["create", "--offer", "1"], ctx) == 1
    assert asks(fake) == ["/api/v0/asks/1/"] and not ctx.store.state["instances"]
    assert cli.main(["create", "--offer", "99"], ctx) == 1  # not among the offers
    assert len(asks(fake)) == 1


@pytest.mark.parametrize("mode", ["500", "timeout"])
def test_other_create_errors_do_not_fall_through(env, mode):
    ctx, fake, _ = env
    three_gone_offers(fake)
    fake.create_mode = mode  # a timeout may already have created a billed box: try nothing else
    assert cli.main(["create", "--auto"], ctx) == 1
    assert len(asks(fake)) == 1 and not ctx.store.state["instances"]


def test_nightly_falls_through_and_checks_runway_for_the_rented_offer(nightly_env):
    ctx, fake, shell, order = nightly_env
    three_gone_offers(fake, price3=0.4)
    fake.gone_asks = {1, 2}
    fake.credit = 1.5  # 1.5 / 0.4 = 3.75 h >= runway_rent_min_h for the third offer
    assert run_nightly(ctx) == 0
    assert asks(fake) == ["/api/v0/asks/1/", "/api/v0/asks/2/", "/api/v0/asks/3/"]
    assert fake.calls("DELETE", "/instances/999/")


def test_nightly_runway_check_uses_the_offer_actually_tried(nightly_env):
    ctx, fake, shell, order = nightly_env
    three_gone_offers(fake, price3=0.4)
    fake.gone_asks = {1, 2}
    fake.credit = 1.0  # fine for the 0.2 $/h offers (5 h), too little for the third at 0.4 $/h (2.5 h)
    assert run_nightly(ctx) == 1
    assert len(asks(fake)) == 2 and not ctx.store.state["instances"] and order == []


def test_nightly_three_gone_offers_is_an_error(nightly_env):
    ctx, fake, shell, order = nightly_env
    three_gone_offers(fake)
    fake.gone_asks = {1, 2, 3, 4}
    assert run_nightly(ctx) == 1
    assert len(asks(fake)) == 3 and not ctx.store.state["instances"] and order == []


# ------------------------------------------------------------------------------------------------ per-project cap
def unmanaged(fake, iid=200, status="running", label="proj"):  # "proj" is the test project's label
    fake.instances[iid] = make_instance(iid, status=status, label=label)


def test_create_refuses_when_an_unmanaged_box_of_this_project_is_up(env, capsys):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400)]
    unmanaged(fake)
    assert cli.main(["create", "--auto"], ctx) == 1
    assert not fake.calls("PUT", "/asks/")
    out = capsys.readouterr().out
    assert "200" in out and "instance cap" in out  # names the blocking box


def test_create_allow_second_overrides_the_cap(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400)]
    unmanaged(fake)
    assert cli.main(["create", "--auto", "--allow-second"], ctx) == 0
    assert fake.calls("PUT", "/asks/1/")


def test_boxes_of_other_projects_do_not_count(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400)]
    unmanaged(fake, label="other-project")
    unmanaged(fake, iid=201, label="")
    assert cli.main(["create", "--auto"], ctx) == 0
    assert fake.calls("PUT", "/asks/1/")


def test_nightly_ignores_boxes_of_other_projects(nightly_env):
    ctx, fake, shell, order = nightly_env
    unmanaged(fake, label="other-project")
    assert run_nightly(ctx) == 0


def test_replacement_overlap_counts_only_this_projects_boxes(env):
    ctx, fake, _ = env
    manage(ctx, fake)  # the old box, up
    unmanaged(fake, label="other-project")
    vc.check_cap(ctx, overlap_with="100")  # old + new = 2 <= max_instances_during_migration
    unmanaged(fake, iid=201)  # a second box of this project: 3 > 2
    with pytest.raises(vc.RentRefused):
        vc.check_cap(ctx, overlap_with="100")


@pytest.mark.parametrize("status,blocks", [("running", True), ("created", True), ("exited", False)])
def test_only_running_and_loading_boxes_count(env, status, blocks):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400)]
    unmanaged(fake, status=status)
    assert cli.main(["create", "--auto"], ctx) == (1 if blocks else 0)
    assert bool(fake.calls("PUT", "/asks/")) != blocks


def test_cap_follows_max_instances(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400)]
    unmanaged(fake)
    ctx.policy["max_instances"] = 2
    assert cli.main(["create", "--auto"], ctx) == 0


def test_nightly_refuses_when_an_unmanaged_box_of_this_project_is_up(nightly_env):
    ctx, fake, shell, order = nightly_env
    unmanaged(fake)
    assert run_nightly(ctx) == 1
    assert not fake.calls("PUT", "/asks/") and order == []


def test_nightly_refuses_second_managed_box(nightly_env):
    ctx, fake, shell, order = nightly_env
    manage(ctx, fake)
    assert run_nightly(ctx) == 1
    assert not fake.calls("PUT", "/asks/") and order == []


def test_watchdog_replacement_refuses_when_an_unmanaged_box_of_this_project_is_up(env):
    ctx, fake, _ = env
    ctx.cfg.policy["auto_replace"] = True
    ctx.policy = ctx.load_policy()
    ctx.store.state["batch"] = {"script": "s.sh", "args": [], "runs_dirs": ["bench/results"], "instance": "100"}
    ctx.store.state["need_replacement"] = {"t": 0, "old": "100", "why": "unreachable"}
    fake.offers = [offer(2, 20, 0.3, 250)]
    manage(ctx, fake, status="exited")  # the old box is stopped, so the steady-state limit applies
    unmanaged(fake)
    tick(ctx, vc.WatchFlags(destroy=False))
    assert not fake.calls("PUT", "/asks/")
    assert any(p["key"] == "replace" and "200" in p["msg"] for p in ctx.store.state["pending"])
    assert R(ctx)["role"] == "active"


# ------------------------------------------------------------------------------------------------ adopted boxes count
@pytest.mark.parametrize("label", ["other-project", ""])
def test_adopted_box_with_foreign_label_blocks_create_and_nightly(env, nightly_env, label, capsys):
    ctx, fake, shell, order = nightly_env
    unmanaged(fake, label=label)
    ctx.store.rec("200", create=True)  # adopted: in the state store, label not (yet) the project name
    assert cli.main(["create", "--auto"], ctx) == 1
    assert not fake.calls("PUT", "/asks/")
    out = capsys.readouterr().out
    assert f"200 (label {label or '-'}, running)" in out
    assert run_nightly(ctx) == 1
    assert not fake.calls("PUT", "/asks/") and order == []


# ------------------------------------------------------------------------------------------------ adopt relabels
def test_adopt_relabels_only_when_the_label_differs(env, capsys):
    ctx, fake, _ = env
    unmanaged(fake, label="other-project")
    unmanaged(fake, iid=201, label="proj")
    assert cli.main(["adopt", "200"], ctx) == 0
    assert "relabelled" in capsys.readouterr().out
    puts = [json.loads(r[2]) for r in fake.calls("PUT", "/instances/200/")]
    assert puts == [{"label": "proj"}] and fake.instances[200]["label"] == "proj"
    assert ctx.store.rec("200")["label"] == "proj"
    assert cli.main(["adopt", "201"], ctx) == 0
    assert not fake.calls("PUT", "/instances/201/")


# ------------------------------------------------------------------------------------------------ account price ceiling
def test_account_ceiling_counts_boxes_of_any_label(env, capsys):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400)]
    unmanaged(fake, label="other-project")  # 0.4 USD/h, another project: not in the count cap, but in the price sum
    ctx.policy["max_account_price_per_hour"] = 0.5
    assert cli.main(["create", "--auto"], ctx) == 1
    assert not fake.calls("PUT", "/asks/")
    out = capsys.readouterr().out
    assert "max_account_price_per_hour" in out and "200 (label other-project, $0.400/h, running)" in out
    ctx.policy["max_account_price_per_hour"] = 0.6
    assert cli.main(["create", "--auto"], ctx) == 0
    assert fake.calls("PUT", "/asks/1/")


def test_account_ceiling_ignores_stopped_boxes(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400)]
    unmanaged(fake, status="exited", label="other-project")
    ctx.policy["max_account_price_per_hour"] = 0.25
    assert cli.main(["create", "--auto"], ctx) == 0


def test_account_ceiling_is_off_by_default(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 9.0, 400)]
    unmanaged(fake, label="other-project")
    assert ctx.policy["max_account_price_per_hour"] is None
    assert cli.main(["create", "--auto", "--max-price", "10"], ctx) == 0


def test_account_ceiling_falls_through_to_a_cheaper_offer(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.45, 900), offer(2, 11, 0.2, 400)]  # ranked by score: the dear one first
    unmanaged(fake, label="other-project")
    ctx.policy["max_account_price_per_hour"] = 0.65
    assert cli.main(["create", "--auto"], ctx) == 0
    assert not fake.calls("PUT", "/asks/1/") and fake.calls("PUT", "/asks/2/")


def test_account_ceiling_is_not_skipped_by_allow_second(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400)]
    unmanaged(fake)
    ctx.policy["max_account_price_per_hour"] = 0.5
    assert cli.main(["create", "--auto", "--allow-second"], ctx) == 1
    assert not fake.calls("PUT", "/asks/")


def test_account_ceiling_applies_to_nightly(nightly_env):
    ctx, fake, shell, order = nightly_env
    unmanaged(fake, label="other-project")
    ctx.policy["max_account_price_per_hour"] = 0.5
    assert run_nightly(ctx) == 1
    assert not fake.calls("PUT", "/asks/") and order == []


# ------------------------------------------------------------------------------------------------ rent lock
def test_rent_waits_for_the_machine_wide_lock_then_refuses(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400)]
    held = vc.FileLock(vc.rent_lock_path())
    assert held.acquire()
    try:
        assert cli.main(["create", "--auto"], ctx) == 1
        assert not fake.calls("PUT", "/asks/")
    finally:
        held.release()
    assert cli.main(["create", "--auto"], ctx) == 0


def test_rent_releases_the_lock_after_create(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400)]
    assert cli.main(["create", "--auto"], ctx) == 0
    probe = vc.FileLock(vc.rent_lock_path())
    assert probe.acquire()
    probe.release()


# ------------------------------------------------------------------------------------------------ offer RAM filter
def test_max_ram_gb_drops_offers_with_more_or_unknown_ram(env):
    ctx, fake, _ = env
    fake.offers = [
        offer(1, 10, 0.2, 900, ram=128 * 1024),
        offer(2, 11, 0.3, 400, ram=32 * 1024),
        offer(3, 12, 0.25, 500),
    ]
    ctx.cfg.offers.max_ram_gb = 64
    ids = [o.id for o in vc.search_offers(ctx)]
    assert ids == ["2"]
    assert vc.search_offers(ctx)[0].ram_gb == 32
    ctx.cfg.offers.max_ram_gb = 0
    assert sorted(o.id for o in vc.search_offers(ctx)) == ["1", "2", "3"]
