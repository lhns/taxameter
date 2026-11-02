"""ADR 0009: several boxes of one project, driven by separate processes, share one state dir."""

from __future__ import annotations

import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest
from conftest import R, manage, offer, persist, tick

from taxameter import cli
from taxameter import core as vc
from taxameter.providers.base import ProviderError

WORKERS, ROUNDS = 6, 8


def _writer(state_dir: str, n: int) -> None:
    store = vc.Store(Path(state_dir))
    for k in range(ROUNDS):
        iid = f"{n}{k:02d}"
        with store.update() as st:
            store.rec(iid, create=True)["label"] = f"w{n}"
            st["batches"][iid] = {"script": f"s{n}.sh", "instance": iid}
        store.event(f"worker {n} round {k}", iid, echo=False)


def test_concurrent_processes_lose_no_update(tmp_path):
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(str(tmp_path), n)) for n in range(WORKERS)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    store = vc.Store(tmp_path)
    want = {f"{n}{k:02d}" for n in range(WORKERS) for k in range(ROUNDS)}
    assert set(store.state["instances"]) == want
    assert set(store.state["batches"]) == want
    assert len(store.state["events"]) == WORKERS * ROUNDS
    assert len((tmp_path / "events.log").read_text(encoding="utf-8").splitlines()) == WORKERS * ROUNDS
    assert not list(tmp_path.glob("*.tmp"))


def test_update_waits_for_the_lock_then_gives_up(tmp_path, monkeypatch):
    a, b = vc.Store(tmp_path), vc.Store(tmp_path)
    monkeypatch.setattr(vc, "STATE_LOCK_WAIT_S", 0.3)
    with a.update():
        with pytest.raises(ProviderError, match="state lock"):
            with b.update():
                pass
    with b.update() as st:  # released: the second store gets it
        st["current"] = "7"
    assert vc.Store(tmp_path).state["current"] == "7"


def test_update_is_reentrant_and_sees_other_writers(tmp_path):
    a, b = vc.Store(tmp_path), vc.Store(tmp_path)
    with b.update() as st:
        st["current"] = "9"
    with a.update() as st:  # reloads from disk under the lock
        assert st["current"] == "9"
        with a.update():  # nested: no second lock, no reload
            a.event("inside", echo=False)
        st["pending"].append({"key": "k"})
    s = vc.Store(tmp_path).state
    assert [e["msg"] for e in s["events"]] == ["inside"] and s["pending"] == [{"key": "k"}]


def test_file_lock_wait_and_release(tmp_path):
    one, two = vc.FileLock(tmp_path / "x.lock"), vc.FileLock(tmp_path / "x.lock")
    assert one.acquire()
    t0 = time.monotonic()
    assert not two.acquire(wait=0.2) and time.monotonic() - t0 >= 0.15
    one.release()
    assert two.acquire(wait=1)
    two.release()


def test_old_single_batch_and_replacement_migrate_on_load(tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "instances": {"100": {"id": "100", "runs": {}}, "101": {"id": "101", "runs": {}}},
                "current": "101",
                "batch": {"script": "a.sh", "instance": "100"},
                "need_replacement": {"old": "100", "why": "migrate", "t": 1},
            }
        ),
        encoding="utf-8",
    )
    st = vc.Store(tmp_path).state
    assert "batch" not in st and "need_replacement" not in st
    assert st["batches"] == {"100": {"script": "a.sh", "instance": "100"}}
    assert st["instances"]["100"]["need_replacement"]["why"] == "migrate"
    (tmp_path / "state.json").write_text(
        json.dumps({"current": "5", "batch": {"script": "b.sh"}}), encoding="utf-8"
    )  # no owner recorded: the current box
    assert vc.Store(tmp_path).state["batches"] == {"5": {"script": "b.sh"}}


def two_boxes(ctx, fake):
    manage(ctx, fake, iid=100)
    manage(ctx, fake, iid=101)
    with ctx.store.update() as st:
        st["current"] = "100"
        for iid in ("100", "101"):
            st["instances"][iid]["runs"] = {"bench/results": {"ok": True}}


def test_two_boxes_have_independent_batches_through_run_fetch_destroy(env):
    ctx, fake, shell = env
    two_boxes(ctx, fake)
    assert cli.main(["run", "--id", "101", "scripts/b.sh", "--env", "K=1"], ctx) == 0
    assert cli.main(["run", "--id", "100", "scripts/a.sh"], ctx) == 0
    batches = ctx.store.state["batches"]
    assert batches["100"]["script"] == "scripts/a.sh" and batches["101"]["script"] == "scripts/b.sh"
    assert batches["101"]["env"] == {"K": "1"} and batches["100"]["env"] == {}
    # run marked both boxes' dirs unfetched; fetch --id 101 verifies only 101
    assert cli.main(["fetch", "--id", "101"], ctx) == 0
    assert R(ctx, 101)["runs"]["bench/results"]["ok"] is True
    assert R(ctx, 100)["runs"]["bench/results"]["ok"] is False
    assert cli.main(["destroy", "--id", "100", "--yes"], ctx) == 1  # 100 is unfetched: refused, 101 untouched
    assert not fake.calls("DELETE")
    assert cli.main(["destroy", "--id", "101", "--yes"], ctx) == 0
    assert [r[1] for r in fake.calls("DELETE")] == ["/api/v0/instances/101/"]
    s = ctx.store.state
    assert set(s["batches"]) == {"100"} and s["current"] == "100"  # 101's batch gone, 100 keeps both
    assert R(ctx, 100)["role"] == "active" and R(ctx, 101)["role"] == "gone"
    cmds = shell.ssh_cmds()
    assert any("box.sh run scripts/b.sh" in c for c in cmds) and any("box.sh run scripts/a.sh" in c for c in cmds)


def test_explicit_id_never_falls_back_to_current(env):
    ctx, fake, _ = env
    two_boxes(ctx, fake)
    assert ctx.iid("101") == "101" and ctx.iid() == "100"
    with pytest.raises(ProviderError, match="empty --id"):
        ctx.iid("")
    # a command for an unmanaged id fails instead of touching `current`
    assert cli.main(["run", "--id", "555", "scripts/x.sh"], ctx) == 1
    assert not [c for c in fake.requests if "/instances/100/" in c[1] and c[0] in ("PUT", "DELETE")]
    assert ctx.store.state["batches"] == {}


def test_batch_done_and_pending_work_are_per_box(env):
    ctx, fake, _ = env
    two_boxes(ctx, fake)
    with ctx.store.update() as st:
        st["batches"]["100"] = {"script": "a.sh", "instance": "100"}
        st["batches"]["101"] = {"script": "b.sh", "instance": "101"}
    with ctx.store.update() as st:
        st["batches"]["101"]["all_done"] = True
    assert vc.pending_work(ctx.store, "100") and not vc.pending_work(ctx.store, "101")
    assert not vc.pending_work(ctx.store, "102")


def test_create_keeps_current_of_another_box_and_prints_the_new_id(env, capsys):
    ctx, fake, _ = env
    ctx.cfg.policy["max_instances"] = 2
    ctx.policy = ctx.load_policy()
    manage(ctx, fake, iid=100)
    fake.offers = [offer(1, 555, 0.3, 400)]
    assert cli.main(["--json", "create", "--auto"], ctx) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "999" and out["current"] == "100"
    assert ctx.store.state["current"] == "100" and "999" in ctx.store.state["instances"]


def test_create_sets_current_when_none(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 555, 0.3, 400)]
    assert cli.main(["create", "--auto"], ctx) == 0
    assert ctx.store.state["current"] == "999"


def test_adopt_sets_current_only_when_none(env):
    ctx, fake, _ = env
    manage(ctx, fake, iid=100)
    fake.instances[300] = vc_instance(300)
    assert cli.main(["adopt", "300"], ctx) == 0
    assert ctx.store.state["current"] == "100" and "300" in ctx.store.state["instances"]


def vc_instance(iid):
    from conftest import make_instance

    return make_instance(iid)


def test_watchdog_covers_both_boxes_with_their_own_state(env):
    ctx, fake, _ = env
    two_boxes(ctx, fake)
    with ctx.store.update() as st:
        st["instances"]["101"]["provision_started"] = time.time()  # only 101 is provisioning
    rep = tick(ctx)
    assert set(rep["instances"]) == {"100", "101"}
    assert R(ctx, 100)["last_health"]["id"] == "100" and R(ctx, 101)["last_health"]["id"] == "101"


def test_replacement_of_one_box_leaves_the_other_boxs_batch_and_current(env, monkeypatch):
    ctx, fake, shell = env
    ctx.cfg.policy.update(auto_replace=True, max_instances_during_migration=3)
    ctx.policy = ctx.load_policy()
    ctx.test_need = {"t": time.time(), "old": "100", "why": "migrate"}
    fake.offers = [offer(2, 20, 0.3, 250)]
    started = []
    from taxameter.providers.base import Endpoint

    monkeypatch.setattr(vc, "wait_ready", lambda c, i, m: Endpoint("direct", "1.2.3.4", 41022))
    monkeypatch.setattr(vc, "provision", lambda c, i, *a, **k: True)
    monkeypatch.setattr(vc, "push_runs", lambda c, r, d: 0)
    monkeypatch.setattr(vc, "start_batch", lambda c, i, b: started.append(i))
    manage(ctx, fake, iid=101)
    manage(ctx, fake, iid=100)
    with ctx.store.update() as st:
        st["current"] = "101"
        st["batches"]["100"] = {"script": "a.sh", "instance": "100", "runs_dirs": ["bench/results"]}
        st["batches"]["101"] = {"script": "b.sh", "instance": "101"}
    persist(ctx)
    tick(ctx, vc.WatchFlags(destroy=False))
    s = ctx.store.state
    assert started == ["999"] and s["current"] == "101"
    assert "100" not in s["batches"] and s["batches"]["101"]["script"] == "b.sh"
    assert R(ctx, 101)["role"] == "active" and not R(ctx, 101).get("need_replacement")
