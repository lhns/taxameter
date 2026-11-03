"""Offer cost model (ADR 0010): cores and download price filters, effective price in score, status fields."""

from __future__ import annotations

import argparse
import json

from conftest import manage, offer

from taxameter import cli
from taxameter import core as vc
from taxameter.providers.base import OfferQuery
from taxameter.providers.vast import build_offer_query, instance_from_row, offer_from_row, usable


def norm(*a, **kw):
    return offer_from_row(offer(*a, **kw))


def test_query_has_server_filters_only_when_set():
    q = OfferQuery(["A"], 1, 1, 0.9, 1, 12)
    body = build_offer_query(q)
    assert "cpu_cores_effective" not in body and "inet_down_cost" not in body
    q = OfferQuery(["A"], 1, 1, 0.9, 1, 12, min_cpu_cores=8, max_inet_down_cost=0.02)
    body = build_offer_query(q)
    assert body["cpu_cores_effective"] == {"gte": 8} and body["inet_down_cost"] == {"lte": 0.02}


def test_usable_filters_cores_and_download_cost_client_side():
    q = OfferQuery(["RTX 3090"], 24, 50, 0.99, 500, 12.4, min_cpu_cores=8, max_inet_down_cost=0.02)
    assert usable(offer(1, 10, 0.2, 100, cores=8, down_cost=0.02), q)
    assert not usable(offer(1, 10, 0.2, 100, cores=4, down_cost=0.01), q)
    assert not usable(offer(1, 10, 0.2, 100, cores=8, down_cost=0.03), q)
    assert not usable(offer(1, 10, 0.2, 100, cores=8), q)  # unknown price cannot be shown to fit
    assert not usable(offer(1, 10, 0.2, 100, down_cost=0.01), q)  # unknown cores likewise
    assert usable(offer(1, 10, 0.2, 100), OfferQuery(["RTX 3090"], 24, 50, 0.99, 500, 12.4))


def test_offer_maps_cores_and_download_cost():
    o = norm(1, 10, 0.2, 100, cores=12.5, down_cost=0.013)
    assert (o.cpu_cores, o.inet_down_cost) == (12.5, 0.013)
    o = norm(2, 10, 0.2, 100)
    assert (o.cpu_cores, o.inet_down_cost) == (0.0, None)


def test_download_cost_lowers_score_and_raises_effective_price():
    cheap_net = norm(1, 10, 0.20, 400, down_cost=0.013)
    dear_net = norm(2, 11, 0.20, 400, down_cost=0.026)
    unknown = norm(3, 12, 0.20, 390)
    offers = [dear_net, unknown, cheap_net]
    plain = vc.filter_rank_offers(offers, set())
    assert [o.score for o in plain] == [400, 400, 390] and all(o.traffic_per_h == 0 for o in plain)
    ranked = vc.filter_rank_offers(offers, set(), download_gb=200, box_hours=10)
    by_id = {o.id: o for o in ranked}
    assert by_id["1"].traffic_per_h == 0.26 and by_id["1"].effective_price_per_h == 0.46
    assert by_id["2"].effective_price_per_h == 0.72
    assert by_id["1"].score == round(400 * 0.20 / 0.46, 1)
    assert by_id["3"].effective_price_per_h == 0.20 and by_id["3"].score == 390  # unknown cost: no penalty
    assert [o.id for o in ranked] == ["3", "1", "2"]
    assert by_id["1"].price_per_h == 0.20  # the hourly price itself is untouched
    by_price = vc.filter_rank_offers(offers, set(), sort="price", download_gb=200, box_hours=10)
    assert [o.id for o in by_price] == ["3", "1", "2"] and by_price[0].price_rank == 1


def test_price_cap_uses_hourly_price_not_effective(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.40, 900, down_cost=0.026)]
    ctx.cfg.offers.expected_download_gb = 500  # far above any hourly cap once spread over max_box_hours
    got = vc.search_offers(ctx, max_price=0.45)
    assert [o.id for o in got] == ["1"] and got[0].effective_price_per_h > 0.45 and got[0].price_per_h == 0.40


def test_search_uses_config_and_cli_overrides(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.20, 400, cores=8, down_cost=0.02)]
    off = ctx.cfg.offers
    off.min_cpu_cores, off.max_inet_down_cost, off.expected_download_gb = 6, 0.03, 100
    o = vc.search_offers(ctx)[0]
    body = json.loads(fake.calls("POST", "/bundles/")[-1][2])
    assert body["cpu_cores_effective"] == {"gte": 6} and body["inet_down_cost"] == {"lte": 0.03}
    assert o.traffic_per_h == 0.02 * 100 / ctx.policy["max_box_hours"]
    off.expected_box_hours = 10
    assert vc.search_offers(ctx)[0].traffic_per_h == 0.2
    a = argparse.Namespace(min_cpu_cores=12, download_gb=None)
    assert vc.search_offers(ctx, a) == []  # 8 cores < 12
    assert json.loads(fake.calls("POST", "/bundles/")[-1][2])["cpu_cores_effective"] == {"gte": 12}
    a = argparse.Namespace(min_cpu_cores=None, download_gb=0)
    assert vc.search_offers(ctx, a)[0].traffic_per_h == 0  # --download-gb 0 turns the config value off


def test_cli_offers_flags_and_listing(env, capsys):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.20, 400, cores=4, down_cost=0.02), offer(2, 11, 0.25, 300, cores=12, down_cost=0.013)]
    assert cli.main(["--json", "offers", "--min-cpu-cores", "8", "--download-gb", "100"], ctx) == 0
    got = json.loads(capsys.readouterr().out)["offers"]
    assert [o["id"] for o in got] == ["2"]
    assert got[0]["cpu_cores"] == 12 and got[0]["inet_down_cost"] == 0.013
    assert got[0]["traffic_per_h"] > 0 and got[0]["effective_price_per_h"] > 0.25
    assert cli.main(["offers", "--download-gb", "100"], ctx) == 0
    out = capsys.readouterr().out
    assert "12 cores" in out and "down $0.013/GB" in out and "traffic" in out


def test_create_accepts_overrides(env):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.20, 400, cores=4), offer(2, 11, 0.25, 300, cores=12)]
    assert cli.main(["create", "--auto", "--min-cpu-cores", "8", "--download-gb", "50"], ctx) == 0
    assert fake.calls("PUT", "/asks/2/") and not fake.calls("PUT", "/asks/1/")


def test_instance_maps_cost_fields():
    row = {"id": 5, "actual_status": "running", "cpu_cores_effective": 11.5, "inet_down_cost": 0.02,
           "inet_down_billed": 150e9, "total_cost": 3.2}  # fmt: skip
    i = instance_from_row(row)
    assert (i.cpu_cores, i.inet_down_cost, i.inet_down_billed_gb, i.total_cost) == (11.5, 0.02, 150.0, 3.2)
    assert vc.billed_download_cost(i) == 3.0
    assert "11.5 cores" in vc.instance_cost_line(i) and "billed 150.0 GB ($3.00)" in vc.instance_cost_line(i)
    bare = instance_from_row({"id": 6, "actual_status": "running"})
    assert vc.billed_download_cost(bare) is None and "billed ? GB" in vc.instance_cost_line(bare)


def test_status_shows_cost_fields(env, capsys):
    ctx, fake, _ = env
    manage(ctx, fake)
    fake.instances[100].update(cpu_cores_effective=9, inet_down_cost=0.02, inet_down_billed=100e9, total_cost=1.5)
    assert cli.main(["status"], ctx) == 0
    out = capsys.readouterr().out
    assert "9 cores" in out and "down $0.020/GB" in out and "billed 100.0 GB ($2.00)" in out and "total $1.50" in out
    assert cli.main(["--json", "status"], ctx) == 0
    inst = json.loads(capsys.readouterr().out)["instances"][0]
    assert inst["cpu_cores"] == 9 and inst["inet_down_billed_gb"] == 100 and inst["inet_down_billed_cost"] == 2.0
