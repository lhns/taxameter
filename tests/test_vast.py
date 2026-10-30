"""providers/vast.py: Vast-specific offer filters, status mapping, endpoint resolution, create errors."""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import GONE_BODY, KEY, FakeVast, make_instance, offer

from taxameter import boxenv
from taxameter.config import Config, ConfigError
from taxameter.providers import get_provider
from taxameter.providers.base import CreateSpec, Endpoint, OfferGone, OfferQuery, ProviderError
from taxameter.providers.vast import VastProvider, build_offer_query, endpoints_from_instance, offer_from_row

QUERY = OfferQuery(["RTX 3090"], 24, 50, 0.99, 500, 12.4)


@pytest.fixture
def vast(tmp_path):
    key = tmp_path / "key"
    key.write_text(KEY + "\n")
    fake = FakeVast()
    return VastProvider(key, httpx.MockTransport(fake.handler)), fake


def spec(**kw):
    return CreateSpec(**{"image": "img:1", "disk_gb": 50, "label": "proj", "onstart": boxenv.ONSTART, **kw})


def test_vast_filters_unverified_ports_reliability_and_vram(vast):
    prov, fake = vast
    fake.offers = [
        offer(2, 10, 0.30, 250),  # verified=None, verification=verified -> kept
        offer(3, 11, 0.15, 200),
        offer(4, 12, 0.25, 300, verification="unverified"),
        offer(6, 14, 0.22, 260, ports=0),  # no direct ports
        offer(7, 15, 0.21, 280, rel=0.95),  # unreliable
        offer(8, 16, 0.18, 270, vram=12288),  # too little VRAM
        offer(9, 17, 0.20, 100, verified=True, verification="unverified"),  # verified flag alone is enough
    ]
    got = prov.offers(QUERY)
    assert [o.id for o in got] == ["2", "3", "9"]
    o = got[0]
    assert (o.host_id, o.price_per_h, o.score, o.reliability, o.location) == ("10", 0.30, 250.0, 0.995, "X")
    assert o.vram_gb == pytest.approx(24564 / 1024) and o.raw["id"] == 2


def test_offer_query_body():
    q = build_offer_query(QUERY)
    assert q["gpu_ram"]["gte"] < 23034  # an L4 (23034 MiB) still counts as a 24 GB card
    assert q["reliability2"] == {"gte": 0.99} and q["type"] == "on-demand"
    assert q["verified"] == {"eq": True} and q["direct_port_count"] == {"gte": 1}
    assert q["cuda_max_good"] == {"gte": 12.4} and q["gpu_name"] == {"in": ["RTX 3090"]}
    assert build_offer_query(OfferQuery(["A"], 1, 1, 0.9, 1, 12, interruptible=True))["type"] == "bid"


def test_offer_without_host_id_has_none():
    row = offer(1, 10, 0.2, 100)
    row["host_id"] = None
    assert offer_from_row(row).host_id is None


@pytest.mark.parametrize(
    "vast_status,want",
    [
        ("running", "running"),
        ("loading", "loading"),
        ("created", "loading"),
        ("scheduling", "loading"),
        ("exited", "stopped"),
        ("stopped", "stopped"),
        ("offline", "stopped"),
        (None, "loading"),
        ("something-new", "loading"),
    ],
)
def test_status_mapping(vast, vast_status, want):
    prov, fake = vast
    fake.instances[100] = make_instance(100)
    fake.instances[100]["actual_status"] = vast_status
    assert prov.instance("100").status == want
    assert prov.instances()[0].status == want


def test_instance_fields_and_missing_instance(vast):
    prov, fake = vast
    fake.instances[100] = make_instance(100, label="other")
    i = prov.instance("100")
    assert (i.id, i.host_id, i.label, i.gpu_name, i.price_per_h, i.storage_per_h) == (
        "100",
        "555",
        "other",
        "RTX 3090",
        0.4,
        0.06,
    )
    assert i.started_at and i.endpoints[0] == Endpoint("direct", "1.2.3.4", 41022)
    assert prov.instance("101") is None


def test_endpoint_resolution_from_instance_with_changed_ports():
    inst = make_instance(port=41022, runtype="jupyter_direc ssh_direc ssh_proxy")
    eps = endpoints_from_instance(inst)
    assert eps[0] == Endpoint("direct", "1.2.3.4", 41022)
    assert Endpoint("proxy", "ssh5.vast.ai", 19555) in eps  # jupyter runtype: proxy port + 1
    assert endpoints_from_instance(make_instance(status="exited"))[0].kind == "proxy"
    assert endpoints_from_instance(None) == []


def test_endpoint_roundtrip_and_str():
    ep = Endpoint("direct", "1.2.3.4", 22)
    assert Endpoint.from_dict(ep.to_dict()) == ep and str(ep) == "direct 1.2.3.4:22"
    assert Endpoint.from_dict({"kind": "direct", "host": "h", "port": "7"}) == Endpoint("direct", "h", 7)  # old state
    assert Endpoint.from_dict({}) is None and Endpoint.from_dict(None) is None


def test_create_payload_and_interruptible_bid(vast):
    prov, fake = vast
    o = offer_from_row({**offer(1, 10, 0.3, 400), "min_bid": 0.1})
    assert prov.create(o, spec()) == "999"
    body = json.loads(fake.calls("PUT", "/asks/1/")[0][2])
    assert body["runtype"] == "ssh_direc ssh_proxy" and body["onstart"] == boxenv.ONSTART
    assert (body["image"], body["disk"], body["label"], body["price"]) == ("img:1", 50, "proj", None)
    prov.create(o, spec(interruptible=True))
    assert json.loads(fake.calls("PUT", "/asks/1/")[1][2])["price"] == pytest.approx(0.115)


def test_create_400_no_such_ask_is_offer_gone(vast):
    prov, fake = vast
    fake.gone_asks = {1}
    with pytest.raises(OfferGone, match="offer 1"):
        prov.create(offer_from_row(offer(1, 10, 0.3, 400)), spec())
    assert GONE_BODY["error"] == "no_such_ask"


def prov_with(tmp_path, handler):
    key = tmp_path / "key"
    key.write_text(KEY)
    return VastProvider(key, httpx.MockTransport(handler))


def test_create_400_not_available_message_is_offer_gone(tmp_path):
    prov = prov_with(tmp_path, lambda req: httpx.Response(400, text='{"msg": "Instance type 1 not available"}'))
    with pytest.raises(OfferGone):
        prov.create(offer_from_row(offer(1, 10, 0.3, 400)), spec())


@pytest.mark.parametrize("mode", ["500", "timeout"])
def test_create_other_errors_are_not_offer_gone(vast, mode):
    prov, fake = vast
    fake.create_mode = mode
    with pytest.raises(ProviderError) as e:
        prov.create(offer_from_row(offer(1, 10, 0.3, 400)), spec())
    assert not isinstance(e.value, OfferGone)


def test_create_400_with_other_reason_is_plain_error(tmp_path):
    prov = prov_with(tmp_path, lambda req: httpx.Response(400, text='{"error": "insufficient_credit"}'))
    with pytest.raises(ProviderError) as e:
        prov.create(offer_from_row(offer(1, 10, 0.3, 400)), spec())
    assert not isinstance(e.value, OfferGone)


def test_lifecycle_calls_and_credit(vast):
    prov, fake = vast
    fake.instances[100] = make_instance(100)
    fake.credit = 7.5
    assert prov.credit() == 7.5
    prov.stop("100")
    assert json.loads(fake.calls("PUT", "/instances/100/")[0][2]) == {"state": "stopped"}
    prov.start("100")
    assert json.loads(fake.calls("PUT", "/instances/100/")[1][2]) == {"state": "running"}
    prov.reboot("100")
    assert fake.calls("PUT", "/instances/reboot/100/")
    prov.destroy("100")
    assert fake.calls("DELETE", "/instances/100/")


def test_missing_key_file_error_hides_nothing_secret(tmp_path):
    prov = VastProvider(tmp_path / "nope", httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(ProviderError, match="cannot read API key file"):
        prov.credit()


def test_provider_attributes():
    assert VastProvider.name == "vast" and VastProvider.dns_host == "console.vast.ai"
    assert {"483833", "366851"} <= set(VastProvider.seed_blocklist)
    assert VastProvider.default_key_file.name == "vast_api_key"


def test_get_provider_by_name_and_unknown(tmp_path):
    cfg = Config(root=tmp_path)
    cfg.provider.api_key_file = "~/some_key"
    prov = get_provider(cfg)
    assert isinstance(prov, VastProvider) and prov.api.key_file.name == "some_key" and "~" not in str(prov.api.key_file)
    cfg.provider.api_key_file = ""
    assert get_provider(cfg).api.key_file == VastProvider.default_key_file
    cfg.provider.name = "nope"
    with pytest.raises(ConfigError, match="unknown provider 'nope'"):
        get_provider(cfg)
