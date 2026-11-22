"""ADR 0012 addendum: `create --offer ID` follows a rotated offer id to the same machine."""

from __future__ import annotations

import json

from conftest import offer

from taxameter import cli


def asks(fake):
    return [r[1] for r in fake.calls("PUT", "/asks/")]


def machine_queries(fake):
    out = []
    for r in fake.requests:
        if "/bundles/" in r[1]:
            q = json.loads(r[2] or b"{}")
            if "machine_id" in q:
                out.append(q)
    return out


def test_offers_records_the_machine_of_every_listed_offer(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400), offer(2, 20, 0.3, 300, num_gpus=2)]
    assert cli.main(["offers"], ctx) == 0
    rec = json.loads((ctx.store.dir / "offers.json").read_text(encoding="utf-8"))
    assert rec["1"] == {"machine_id": "11", "num_gpus": 1, "gpu_name": "RTX 3090"}
    assert rec["2"]["machine_id"] == "21" and rec["2"]["num_gpus"] == 2


def test_the_map_keeps_only_the_last_listing(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400)]
    cli.main(["offers"], ctx)
    fake.offers = [offer(5, 10, 0.2, 400)]
    cli.main(["offers"], ctx)
    rec = json.loads((ctx.store.dir / "offers.json").read_text(encoding="utf-8"))
    assert list(rec) == ["5"]


def test_rotated_id_is_rented_on_the_same_machine(env, capsys):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400), offer(2, 20, 0.1, 900)]
    cli.main(["offers"], ctx)
    fake.offers = [offer(8, 10, 0.2, 400), offer(2, 20, 0.1, 900)]  # id 1 rotated to 8, same machine 11
    ctx.sleep = lambda s: None
    assert cli.main(["create", "--offer", "1"], ctx) == 0
    assert asks(fake) == ["/api/v0/asks/8/"]
    assert "offer 1 rotated: using 8 (same machine 11)" in capsys.readouterr().out
    assert machine_queries(fake)[0]["machine_id"] == {"eq": 11}


def test_rotation_keeps_the_gpu_count_and_the_price_cap(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400)]
    cli.main(["offers"], ctx)
    ctx.sleep = lambda s: None
    fake.offers = [offer(8, 10, 0.2, 400, num_gpus=2)]  # same machine, other GPU count
    assert cli.main(["create", "--offer", "1"], ctx) == 1
    fake.offers = [offer(9, 10, 0.9, 400)]  # same machine, over the cap
    assert cli.main(["create", "--offer", "1", "--max-price", "0.5"], ctx) == 1
    assert not asks(fake)


def test_unknown_id_fails_with_the_auto_hint(env, capsys):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400)]
    ctx.sleep = lambda s: None
    assert cli.main(["create", "--offer", "99"], ctx) == 1  # no map yet
    out = capsys.readouterr().out
    assert "offer 99 is gone" in out and "create --auto" in out
    assert not machine_queries(fake) and not asks(fake)


def test_machine_without_a_current_offer_fails(env, capsys):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.2, 400)]
    cli.main(["offers"], ctx)
    fake.offers = [offer(2, 20, 0.2, 400)]
    ctx.sleep = lambda s: None
    assert cli.main(["create", "--offer", "1"], ctx) == 1
    assert "create --auto" in capsys.readouterr().out and not asks(fake)
