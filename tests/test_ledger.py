"""ADR 0012: explicit-offer retry and the running-time ledger."""

from __future__ import annotations

import json
import time

import pytest
from conftest import R, manage, offer, persist, tick

from taxameter import cli
from taxameter import core as vc


def searches(fake):
    return [r for r in fake.requests if "/bundles/" in r[1]]


def asks(fake):
    return [r[1] for r in fake.calls("PUT", "/asks/")]


# ------------------------------------------------------------------------------------------ offer retry
def test_missing_offer_is_found_on_the_first_retry(env, capsys, monkeypatch):
    ctx, fake, _ = env
    sleeps = []
    ctx.sleep = sleeps.append
    real = vc.search_offers
    n = []

    def flicker(c, *args, **kw):
        n.append(1)
        fake.offers = [] if len(n) == 1 else [offer(7, 10, 0.2, 400)]
        return real(c, *args, **kw)

    monkeypatch.setattr(vc, "search_offers", flicker)
    assert cli.main(["create", "--offer", "7"], ctx) == 0
    assert len(n) == 2 and asks(fake) == ["/api/v0/asks/7/"]
    assert sleeps == [10.0]
    assert "searching again" in capsys.readouterr().out


def test_offer_missing_every_time_is_refused_after_n_searches(env, capsys):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400)]
    ctx.policy["offer_retries"] = 2
    sleeps = []
    ctx.sleep = sleeps.append
    assert cli.main(["create", "--offer", "99"], ctx) == 1
    assert len(searches(fake)) == 3 and sleeps == [10.0, 10.0]
    assert "after 3 searches" in capsys.readouterr().out
    assert not asks(fake)


def test_wrong_gpu_count_is_refused_without_retry(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 555, 0.9, 400, num_gpus=2)]
    ctx.sleep = lambda s: pytest.fail("no retry for a wrong GPU count")
    assert cli.main(["create", "--offer", "1", "--max-price", "1"], ctx) != 0
    assert len(searches(fake)) == 1


def test_retry_releases_the_rent_lock_while_waiting(env):
    ctx, fake, _ = env
    fake.offers = []
    held = []

    def sleep(s):
        lock = vc.FileLock(vc.rent_lock_path())
        held.append(not lock.acquire())  # True when somebody still holds it
        lock.release()

    ctx.sleep = sleep
    with pytest.raises(vc.RentRefused, match="after 3 searches"):
        vc.rent(ctx, lambda: vc.search_offers(ctx), only_offer="5")
    assert held == [False, False]


def test_auto_rent_does_not_retry(env):
    ctx, fake, _ = env
    fake.offers = []
    ctx.sleep = lambda s: pytest.fail("no retry without --offer")
    with pytest.raises(vc.RentRefused, match="no offers match"):
        vc.rent(ctx, lambda: vc.search_offers(ctx))


# ------------------------------------------------------------------------------------------ ledger
def test_ledger_across_running_stopped_running():
    rec = {"created": 0.0}
    H = 3600.0
    vc.ledger_note(rec, True, 0.0)
    vc.ledger_note(rec, True, 1 * H)  # repeat: no change
    vc.ledger_note(rec, False, 2 * H)  # stopped at 2 h
    assert vc.running_h(rec, 10 * H) == pytest.approx(2.0)  # stopped time does not count
    vc.ledger_note(rec, True, 10 * H)  # started again
    assert vc.running_h(rec, 11.5 * H) == pytest.approx(3.5)
    vc.ledger_note(rec, False, 12 * H)
    assert vc.running_h(rec, 20 * H) == pytest.approx(4.0)


def test_ledger_follows_observed_status_in_the_watchdog(env):
    ctx, fake, _ = env
    manage(ctx, fake, status="exited")
    R(ctx)["created"] = time.time() - 5 * 3600
    R(ctx)["running_s"] = 3600.0  # one hour so far
    R(ctx)["running_since"] = time.time() - 600  # believed running, but the API says stopped
    tick(ctx)
    rec = R(ctx)
    assert rec["running_since"] is None
    assert 3600 + 590 < rec["running_s"] < 3600 + 700
    fake.instances[100] = {**fake.instances[100], "actual_status": "running"}
    fake.instances[100]["ports"] = {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "41022"}]}
    tick(ctx)
    assert R(ctx)["running_since"] is not None


def test_stop_and_start_move_the_ledger(env):
    ctx, fake, _ = env
    manage(ctx, fake)
    R(ctx)["running_s"] = 0.0
    R(ctx)["running_since"] = time.time() - 1800
    persist(ctx)
    vc.stop(ctx, "100", "test")
    assert R(ctx)["running_since"] is None and 1790 < R(ctx)["running_s"] < 1900
    vc.start(ctx, "100")
    assert R(ctx)["running_since"] is not None


def test_create_starts_the_ledger(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400)]
    assert cli.main(["create", "--auto"], ctx) == 0
    rec = R(ctx, 999)
    assert rec["running_s"] == 0.0 and rec["running_since"] is not None


def test_max_hours_stop_ignores_stopped_time(env):
    ctx, fake, _ = env
    rec = manage(ctx, fake)
    fake.instances[100]["start_date"] = time.time() - 30 * 3600  # provider start date: long ago
    rec["running_s"] = 1.0 * 3600  # but only 1 h + 0.5 h of it running
    rec["running_since"] = time.time() - 1800
    tick(ctx)
    assert not fake.calls("PUT", "/instances/100/")  # cap is 3 h: not stopped


def test_max_hours_stop_counts_running_time(env):
    ctx, fake, _ = env
    rec = manage(ctx, fake)
    rec["running_s"] = 2.9 * 3600
    rec["running_since"] = time.time() - 1800
    tick(ctx)
    assert json.loads(fake.calls("PUT", "/instances/100/")[0][2]) == {"state": "stopped"}


def test_record_without_ledger_behaves_as_before(env):
    ctx, fake, _ = env
    manage(ctx, fake)
    assert "running_s" not in R(ctx)
    fake.instances[100]["start_date"] = time.time() - 2 * 3600
    assert vc.box_age_h(ctx, "100", ctx.provider.instance("100")) == pytest.approx(2.0, abs=0.01)
    assert "running_s" not in R(ctx)  # reading does not create one


def test_age_notice_says_running(env, capsys):
    ctx, fake, _ = env
    rec = manage(ctx, fake)
    rec["running_s"] = 3600.0
    rec["running_since"] = None
    vc.age_notice(ctx, "100")
    assert "box running 1h00m of max 3h00m" in capsys.readouterr().out
