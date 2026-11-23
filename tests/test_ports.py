"""create --ports (ADR 0014): validation, the request's env mappings, the config default, the printed mapping."""

from __future__ import annotations

import json

import pytest
from conftest import manage, offer, persist

from taxameter import cli
from taxameter.config import ConfigError, parse_ports
from taxameter.providers.vast import port_env


def create_env(fake):
    return json.loads(fake.calls("PUT", "/asks/")[-1][2])["env"]


def test_parse_ports_normalises_and_validates():
    assert parse_ports(["7000", "7001/UDP,7000/tcp", " 8080/tcp "]) == ["7000/tcp", "7001/udp", "8080/tcp"]
    assert parse_ports(None) == []
    for bad in ("0", "65536", "abc", "70/sctp", "-1", "22", "7000/"):
        with pytest.raises(ConfigError):
            parse_ports([bad])


def test_port_env_uses_vast_p_keys():
    assert port_env(("7000/tcp", "7001/udp")) == {"-p 7000:7000": "1", "-p 7001:7001/udp": "1"}
    assert port_env(()) == {}


def test_create_sends_mappings_and_prints_public_ports(env, capsys):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.3, 400)]
    assert cli.main(["--json", "create", "--auto", "--ports", "7000,7001/udp", "--ports", "7002/tcp"], ctx) == 0
    assert create_env(fake) == {"-p 7000:7000": "1", "-p 7001:7001/udp": "1", "-p 7002:7002": "1"}
    out = json.loads(capsys.readouterr().out)
    assert out["ports"] == {"7000/tcp": "1.2.3.4:37000", "7001/udp": "1.2.3.4:37001", "7002/tcp": "1.2.3.4:37002"}
    assert ctx.store.rec("999")["ports"] == ["7000/tcp", "7001/udp", "7002/tcp"]


def test_create_prints_one_line_per_port(env, capsys):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.3, 400)]
    assert cli.main(["create", "--auto", "--ports", "7001/udp"], ctx) == 0
    assert "port 7001/udp -> 1.2.3.4:37001" in capsys.readouterr().out


def test_without_ports_env_stays_empty_and_nothing_is_printed(env, capsys):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.3, 400)]
    assert cli.main(["create", "--auto"], ctx) == 0
    assert create_env(fake) == {} and "port " not in capsys.readouterr().out


def test_config_default_and_cli_override(env):
    ctx, fake, _ = env
    ctx.cfg.box.ports = ["9000/tcp"]
    fake.offers = [offer(1, 10, 0.3, 400)]
    assert cli.main(["create", "--auto"], ctx) == 0
    assert create_env(fake) == {"-p 9000:9000": "1"}
    assert cli.main(["create", "--auto", "--allow-second", "--ports", "9001/udp"], ctx) == 0
    assert create_env(fake) == {"-p 9001:9001/udp": "1"}  # the flag replaces the default


def test_invalid_ports_refuse_before_renting(env, capsys):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.3, 400)]
    assert cli.main(["create", "--auto", "--ports", "70000"], ctx) == 1
    assert not fake.calls("PUT", "/asks/") and "bad port" in capsys.readouterr().out


def test_endpoint_prints_mapping_after_a_restart(env, capsys):
    ctx, fake, _ = env
    manage(ctx, fake)
    ctx.store.rec(100)["ports"] = ["7000/udp", "7001/tcp"]
    persist(ctx)
    fake.instances[100]["ports"]["7000/udp"] = [{"HostIp": "0.0.0.0", "HostPort": "41234"}]
    assert cli.main(["endpoint"], ctx) == 0
    out = capsys.readouterr().out
    assert "port 7000/udp -> 1.2.3.4:41234" in out and "port 7001/tcp -> not mapped (yet)" in out


def test_ports_in_toml_are_validated(tmp_path):
    from taxameter import config

    f = tmp_path / "taxameter.toml"
    body = '[project]\nname = "p"\n[policy]\nmax_box_hours = 3\n[box]\nports = {}\n'
    f.write_text(body.format('["7000", "7001/udp"]'), encoding="utf-8")
    assert config.load(f).box.ports == ["7000/tcp", "7001/udp"]
    f.write_text(body.format('["99999"]'), encoding="utf-8")
    with pytest.raises(ConfigError):
        config.load(f)
