"""cli.py: commands, exit codes, output hygiene (the API key never leaves the key file)."""

from __future__ import annotations

import json
import time

import pytest
from conftest import GONE_BODY, INSTANCE_KEY, KEY, manage, offer
from conftest import make_instance as vc_make

from taxameter import cli
from taxameter import core as vc
from taxameter.output import redact


def test_offers_command_uses_state_blocklist(env, capsys):
    ctx, fake, _ = env
    fake.offers = [offer(1, 483833, 0.2, 400), offer(2, 10, 0.3, 250)]
    assert cli.main(["offers"], ctx) == 0
    out = capsys.readouterr().out
    assert "offer         2" in out and "host 483833" not in out
    body = json.loads(fake.calls("POST", "/bundles/")[0][2])
    assert body["verified"] == {"eq": True} and body["direct_port_count"] == {"gte": 1}
    assert body["gpu_name"] == {"in": ["RTX 3090"]}  # [offers] gpus default
    assert body["cuda_max_good"] == {"gte": 12.8} and body["gpu_ram"]["gte"] == 24 * 950


def test_offers_flags_override_config(env):
    ctx, fake, _ = env
    assert (
        cli.main(["offers", "--gpu", "RTX 4090", "--min-cuda", "12.1", "--min-vram", "16", "--min-disk", "80"], ctx)
        == 0
    )
    body = json.loads(fake.calls("POST", "/bundles/")[0][2])
    assert body["gpu_name"] == {"in": ["RTX 4090"]} and body["cuda_max_good"] == {"gte": 12.1}
    assert body["gpu_ram"]["gte"] == 16 * 950 and body["disk_space"] == {"gte": 80}


def test_offers_command_ranks_last_good_host_first(env, capsys):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.20, 400), offer(2, 20, 0.30, 250)]
    ctx.store.state["last_good_host"] = "20"
    assert cli.main(["offers"], ctx) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "offer " in ln and "host" in ln]
    assert "offer         2" in lines[0]


def test_offers_json(env, capsys):
    ctx, fake, _ = env
    fake.offers = [offer(2, 10, 0.3, 250)]
    assert cli.main(["--json", "offers"], ctx) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["offers"][0]["id"] == "2" and out["offers"][0]["price_per_h"] == 0.3


def test_destroy_refused_without_fetch(env, capsys):
    ctx, fake, _ = env
    rec = manage(ctx, fake)
    rec["runs"] = {"bench/results": {"ok": False}}
    assert cli.main(["destroy"], ctx) == 1  # no --yes
    assert cli.main(["destroy", "--yes"], ctx) == 1  # runs not fetched
    assert not fake.calls("DELETE")
    assert "not fetched" in capsys.readouterr().out
    assert cli.main(["destroy", "--yes", "--force"], ctx) == 0
    assert fake.calls("DELETE", "/instances/100/")


def test_fetch_then_destroy_via_cli(env):
    ctx, fake, _ = env
    rec = manage(ctx, fake)
    rec["runs"] = {"bench/results": {"ok": False}}
    assert cli.main(["fetch"], ctx) == 0
    assert cli.main(["destroy", "--yes"], ctx) == 0 and fake.calls("DELETE")


def test_create_uses_image_override_and_project_label(env):
    ctx, fake, shell = env
    fake.offers = [offer(1, 10, 0.3, 400)]
    assert cli.main(["create", "--auto", "--image", "nvidia/cuda:12.8.1-devel-ubuntu24.04"], ctx) == 0
    body = json.loads(fake.calls("PUT", "/asks/")[0][2])
    assert body["image"] == "nvidia/cuda:12.8.1-devel-ubuntu24.04" and body["label"] == "proj"
    assert body["disk"] == 50 and body["onstart"].startswith("[ -f /root/.taxameter/box.sh ]")
    assert cli.main(["create", "--auto", "--allow-second"], ctx) == 0
    assert json.loads(fake.calls("PUT", "/asks/")[1][2])["image"] == "ghcr.io/lhns/proj-box:latest"


def test_interruptible_from_config_reaches_both_query_and_create(env):
    ctx, fake, _ = env
    ctx.cfg.offers.interruptible = True
    fake.offers = [{**offer(1, 10, 0.3, 400), "min_bid": 0.2}]
    assert cli.main(["create", "--auto"], ctx) == 0
    assert json.loads(fake.calls("POST", "/bundles/")[0][2])["type"] == "bid"
    assert json.loads(fake.calls("PUT", "/asks/")[0][2])["price"] == pytest.approx(0.23)


def test_create_requires_offer_or_auto(env, capsys):
    ctx, fake, _ = env
    assert cli.main(["create"], ctx) == 1
    assert "--offer ID or --auto" in capsys.readouterr().out and not fake.requests


def test_create_reports_gone_offer_message(env, capsys):
    ctx, fake, _ = env
    fake.offers = [offer(1, 10, 0.3, 400)]
    fake.gone_asks = {1}
    assert cli.main(["--json", "create", "--auto"], ctx) == 1
    cap = capsys.readouterr()
    assert "gone" in json.loads(cap.out)["error"] and GONE_BODY["error"] in cap.err


def test_run_env_and_box_run_env_reach_the_box_command_line(env):
    ctx, fake, shell = env
    manage(ctx, fake)
    ctx.cfg.box.run_env = {"BASE": "1", "OVERRIDE": "cfg"}
    assert cli.main(["run", "scripts/job.sh", "arg1", "--env", "OVERRIDE=cli", "--env", "MSG=two words"], ctx) == 0
    cmd = next(c for c in shell.ssh_cmds() if "box.sh run" in c)
    assert cmd == "BASE=1 OVERRIDE=cli MSG='two words' bash /root/.taxameter/box.sh run scripts/job.sh arg1"
    assert ctx.store.state["batch"]["env"] == {"OVERRIDE": "cli", "MSG": "two words"}  # replacement reuses the CLI env


def test_run_without_env_uses_only_config_env(env):
    ctx, fake, shell = env
    manage(ctx, fake)
    ctx.cfg.box.run_env = {"BASE": "1"}
    assert cli.main(["run", "scripts/job.sh"], ctx) == 0
    assert (
        next(c for c in shell.ssh_cmds() if "box.sh run" in c)
        == "BASE=1 bash /root/.taxameter/box.sh run scripts/job.sh"
    )


def test_run_rejects_malformed_env(env, capsys):
    ctx, fake, shell = env
    manage(ctx, fake)
    assert cli.main(["run", "scripts/job.sh", "--env", "NOEQUALS"], ctx) == 1
    assert "KEY=VALUE" in capsys.readouterr().out and not any("box.sh run" in c for c in shell.ssh_cmds())


def test_status_lists_unmanaged_instances_and_blocklist(env, capsys):
    ctx, fake, _ = env
    manage(ctx, fake)
    fake.instances[200] = {**fake.instances[100], "id": 200, "label": "other"}
    assert cli.main(["status"], ctx) == 0
    out = capsys.readouterr().out
    assert "instance 100" in out and "managed:active" in out and "instance 200" in out and "unmanaged" in out
    assert "483833" in out
    assert cli.main(["--json", "status"], ctx) == 0
    assert json.loads(capsys.readouterr().out)["instances"][0]["status"] == "running"


def test_balance_without_credit_api(env, capsys, monkeypatch):
    ctx, fake, _ = env
    monkeypatch.setattr(ctx.provider, "credit", lambda: None)
    assert cli.main(["balance"], ctx) == 0
    assert "credit unknown" in capsys.readouterr().out


def test_block_command(env):
    ctx, fake, _ = env
    assert cli.main(["block", "77", "--reason", "slow"], ctx) == 0
    assert ctx.store.state["blocklist"]["77"] == "slow"
    assert cli.main(["block", "77", "--remove"], ctx) == 0
    assert "77" not in ctx.store.state["blocklist"]


def test_options_work_before_and_after_the_subcommand(env):
    ctx, fake, _ = env
    manage(ctx, fake)
    manage(ctx, fake, iid=101)
    assert cli.main(["--id", "101", "stop"], ctx) == 0
    assert cli.main(["stop", "--id", "100"], ctx) == 0
    assert fake.calls("PUT", "/instances/101/") and fake.calls("PUT", "/instances/100/")


def test_main_prints_redacted_traceback_for_unexpected_errors(env, monkeypatch, capsys):
    ctx, fake, shell = env
    redact.add(KEY)

    def boom(c, a):
        raise RuntimeError(f"bad {KEY}")

    monkeypatch.setitem(cli.COMMANDS, "balance", boom)
    assert cli.main(["balance"], ctx) == 1
    err = capsys.readouterr()
    text = err.out + err.err
    assert "Traceback" in text and "RuntimeError" in text and KEY not in text and "***" in text


def test_exit_code_130_on_interrupt(env, monkeypatch):
    ctx, fake, _ = env

    def boom(c, a):
        raise KeyboardInterrupt

    monkeypatch.setitem(cli.COMMANDS, "balance", boom)
    assert cli.main(["balance"], ctx) == 130


def test_ssh_command_prints_remote_output_and_returns_rc(env, capsys):
    ctx, fake, shell = env
    manage(ctx, fake)
    assert cli.main(["ssh", "--", "echo", "up"], ctx) == 0
    assert "up" in capsys.readouterr().out
    assert shell.ssh_cmds()[-1] == "echo up"


def test_key_never_printed_logged_or_on_command_line(env, capsys):
    ctx, fake, shell = env
    rec = manage(ctx, fake)
    rec["runs"] = {"bench/results": {"ok": False}}
    fake.offers = [offer(2, 10, 0.3, 250)]
    for argv in (
        ["balance"],
        ["--json", "balance"],
        ["offers"],
        ["status"],
        ["endpoint"],
        ["health"],
        ["fetch"],
        ["logs"],
        ["ssh", "--", "nvidia-smi"],
        ["create", "--auto", "--allow-second"],
    ):
        cli.main(argv, ctx)
    fake.fail = True
    for argv in (["balance"], ["offers"], ["status"], ["destroy", "--yes", "--force"], ["stop"], ["--json", "reboot"]):
        cli.main(argv, ctx)
    vc.watchdog_loop(ctx, vc.WatchFlags(), once=True, interval=1)
    out = capsys.readouterr()
    text = out.out + out.err
    text += (ctx.store.dir / "state.json").read_text() + (ctx.store.dir / "events.log").read_text()
    text += json.dumps(shell.argvs)
    for secret in (KEY, INSTANCE_KEY, "JUPYTOKEN-abcdef"):
        assert secret not in text
    assert "***" in text  # the echoed credential was redacted, not silently dropped


def test_module_entry_point_exists():
    import importlib

    assert callable(importlib.import_module("taxameter.cli").main)
    assert importlib.util.find_spec("taxameter.__main__") is not None


@pytest.mark.parametrize("argv", [[], ["--bogus"]])
def test_no_command_is_an_argparse_error(argv):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2


# ------------------------------------------------------------------------------------------------ box age cap
def _aged(fake, hours, iid=100):
    fake.instances[iid]["start_date"] = time.time() - hours * 3600


@pytest.mark.parametrize(
    "argv",
    [["status"], ["ssh", "--", "echo", "up"], ["fetch"], ["health"], ["put", "{f}", "/root/x"], ["run", "s.sh"]],
)
def test_commands_print_box_age_without_warning_when_young(env, capsys, tmp_path, argv):
    ctx, fake, _ = env
    manage(ctx, fake)
    f = tmp_path / "f.txt"
    f.write_text("x")
    _aged(fake, 1)
    cli.main([a.replace("{f}", str(f)) for a in argv], ctx)
    out = capsys.readouterr().out
    assert "box age 1h00m of max 3h00m" in out and "WARNING: box age" not in out


def test_provision_prints_age_and_warns_past_80_percent(env, capsys):
    ctx, fake, _ = env
    manage(ctx, fake)
    _aged(fake, 2.7)
    assert cli.main(["provision", "--models", "org/m:f.gguf"], ctx) == 0
    out = capsys.readouterr().out
    assert "box age 2h42m of max 3h00m" in out and "past 80% of max_box_hours 3h00m" in out


@pytest.mark.parametrize("argv", [["run", "scripts/job.sh"], ["provision", "--models", "org/m:f.gguf"]])
def test_new_work_refused_past_the_cap_unless_overridden(env, capsys, argv):
    ctx, fake, shell = env
    manage(ctx, fake)
    _aged(fake, 3.63)
    assert cli.main(argv, ctx) == 1
    out = capsys.readouterr().out
    assert "3h38m" in out and "max_box_hours 3h00m" in out and "--past-cap" in out
    assert not [c for c in shell.ssh_cmds() if "box.sh run" in c or "provision" in c]
    assert cli.main([*argv, "--past-cap"], ctx) == 0
    assert "WARNING: box is past max_box_hours" in capsys.readouterr().out


def test_fetch_ssh_and_destroy_are_never_blocked_past_the_cap(env, capsys):
    ctx, fake, _ = env
    rec = manage(ctx, fake)
    rec["runs"] = {"bench/results": {"ok": False}}
    _aged(fake, 4)
    assert cli.main(["ssh", "--", "echo", "up"], ctx) == 0
    assert cli.main(["fetch"], ctx) == 0
    assert cli.main(["destroy", "--yes"], ctx) == 0
    assert "WARNING: box is past max_box_hours" in capsys.readouterr().out


def test_age_falls_back_to_recorded_creation_time(env, capsys):
    ctx, fake, _ = env
    rec = manage(ctx, fake)
    fake.instances[100]["start_date"] = None  # provider gives no start time
    rec["created"] = time.time() - 3.5 * 3600  # as recorded by create/adopt
    assert cli.main(["run", "scripts/job.sh"], ctx) == 1
    assert "3h30m" in capsys.readouterr().out


def test_adopt_records_provider_start_time_and_age_is_known(env, capsys):
    ctx, fake, _ = env
    fake.instances[100] = vc_make(100)
    _aged(fake, 3.2)
    assert cli.main(["adopt", "100"], ctx) == 0
    assert ctx.store.rec(100)["created"] == pytest.approx(fake.instances[100]["start_date"])
    fake.fail = True  # API down: the recorded time still gives the age
    capsys.readouterr()
    vc.age_notice(ctx, "100")
    assert "box age 3h12m" in capsys.readouterr().out


def test_no_age_line_when_age_unknown(env, capsys):
    ctx, fake, _ = env
    manage(ctx, fake)
    fake.instances[100]["start_date"] = None
    assert cli.main(["run", "scripts/job.sh"], ctx) == 0
    assert "box age" not in capsys.readouterr().out
