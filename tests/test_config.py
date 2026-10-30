"""Config: image tag, model list, min CUDA, policy precedence, discovery and error reporting."""

from __future__ import annotations

import json
import re

import pytest
from conftest import KEY

from taxameter import cli, config
from taxameter import core as vc
from taxameter.config import Config, ConfigError


def write_toml(path, text):
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------------------------------------ image / models / cuda
def test_image_ref_from_tag_file_and_regex(tmp_path):
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "PIN").write_text("commit=abc\ntag=b4567\n", encoding="utf-8")
    cfg = Config(root=tmp_path)
    cfg.image.name, cfg.image.tag = "ghcr.io/x/box", "latest"
    cfg.image.tag_file, cfg.image.tag_regex = "vendor/PIN", r"^tag=(\S+)"
    assert cfg.image_ref() == "ghcr.io/x/box:b4567"


@pytest.mark.parametrize("files", [{}, {"vendor/PIN": "commit=abc\n"}])
def test_image_ref_falls_back_to_tag_when_regex_misses_or_file_absent(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    cfg = Config(root=tmp_path)
    cfg.image.name, cfg.image.tag = "ghcr.io/x/box", "latest"
    cfg.image.tag_file, cfg.image.tag_regex = "vendor/PIN", r"^tag=(\S+)"
    assert cfg.image_ref() == "ghcr.io/x/box:latest"
    cfg.image.tag = ""
    assert cfg.image_ref() == "ghcr.io/x/box"


def test_model_specs_from_toml_key_resolve_against_root_not_cwd(tmp_path, monkeypatch):
    (tmp_path / "bench").mkdir()
    write_toml(
        tmp_path / "bench" / "spec.toml",
        '[model.a]\nhf = "org/a:a.gguf"\n[model.b]\nhf = "org/b:b.gguf"\n[model.c]\nx = 1\n',
    )
    cfg = Config(root=tmp_path)
    cfg.models.specs = ["org/extra:e.gguf"]
    cfg.models.from_toml, cfg.models.from_key = "bench/spec.toml", "model.*.hf"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert cfg.model_specs() == ["org/extra:e.gguf", "org/a:a.gguf", "org/b:b.gguf"]


def test_missing_model_file_is_a_config_error(tmp_path):
    cfg = Config(root=tmp_path)
    cfg.models.from_toml, cfg.models.from_key = "nope.toml", "model.*.hf"
    with pytest.raises(ConfigError, match="model list"):
        vc.configured_models(cfg)


def test_min_cuda_from_config_image_name_or_default(tmp_path):
    cfg = Config(root=tmp_path)
    assert cfg.min_cuda() == 12.8  # default image nvidia/cuda:12.8.1-...
    cfg.image.name = "ghcr.io/x/box"
    assert cfg.min_cuda() == 12.8  # no CUDA version in the name: the default
    cfg.image.name, cfg.image.tag = "nvidia/cuda", "12.4.1-devel"
    assert cfg.min_cuda() == 12.4  # parsed from the full reference
    cfg.image.name, cfg.image.tag = "ghcr.io/x/box", "cuda-12.1-base"
    assert cfg.min_cuda() == 12.1
    cfg.offers.min_cuda = 12.1
    assert cfg.min_cuda() == 12.1


# ------------------------------------------------------------------------------------------------ policy precedence
def project(tmp_path, extra=""):
    key = tmp_path / "k"
    key.write_text(KEY)
    return write_toml(
        tmp_path / "taxameter.toml",
        f'[project]\nname = "p"\nstate_dir = ".st"\n[provider]\napi_key_file = "{key.as_posix()}"\n{extra}',
    )


def test_policy_precedence_defaults_then_toml_then_config_json(tmp_path):
    path = project(tmp_path, "[policy]\nmax_price_per_hour = 0.3\nidle_stop_min = 5\n")
    cfg = config.load(path)
    assert vc.Ctx(cfg).policy["max_price_per_hour"] == 0.3  # [policy] beats the default 0.45
    assert vc.Ctx(cfg).policy["disk_gb"] == 50  # untouched default
    cfg.state_dir.mkdir()
    (cfg.state_dir / "config.json").write_text(json.dumps({"max_price_per_hour": 0.2, "bogus": 1}))
    pol = vc.Ctx(cfg).policy
    assert pol["max_price_per_hour"] == 0.2 and pol["idle_stop_min"] == 5 and "bogus" not in pol


def test_config_json_is_never_created(tmp_path):
    cfg = config.load(project(tmp_path))
    ctx = vc.Ctx(cfg)
    ctx.store.event("something")
    assert (cfg.state_dir / "state.json").exists() and not (cfg.state_dir / "config.json").exists()


def test_invalid_config_json_warns_and_is_ignored(tmp_path, capsys):
    cfg = config.load(project(tmp_path, "[policy]\nidle_stop_min = 7\n"))
    cfg.state_dir.mkdir()
    (cfg.state_dir / "config.json").write_text("{not json")
    ctx = vc.Ctx(cfg)
    assert ctx.policy["idle_stop_min"] == 7
    assert "not valid JSON; ignored" in capsys.readouterr().out
    (cfg.state_dir / "config.json").write_text("[1, 2]")
    assert vc.Ctx(cfg).policy["idle_stop_min"] == 7
    assert "not a JSON object" in capsys.readouterr().out


def test_watchdog_picks_up_config_json_edits(env):
    ctx, fake, _ = env
    ctx.store.save()
    (ctx.store.dir / "config.json").write_text(json.dumps({"max_box_hours": 9}))
    vc.watchdog_tick(ctx, vc.WatchFlags())
    assert ctx.policy["max_box_hours"] == 9


def test_state_dir_and_remote_dir_follow_project_table(tmp_path):
    cfg = config.load(project(tmp_path))
    assert cfg.state_dir == tmp_path.resolve() / ".st" and cfg.remote_dir == "/root/p"
    cfg.project.remote_dir = "/work/p"
    assert cfg.remote_dir == "/work/p"


# ------------------------------------------------------------------------------------------------ --config / discovery
def test_config_flag_selects_the_file_and_state_dir(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    path = project(tmp_path / "proj")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert cli.main(["--config", str(path), "block", "9", "--reason", "x"]) == 0
    assert json.loads((path.parent / ".st" / "state.json").read_text())["blocklist"]["9"] == "x"
    assert cli.main(["block", "9", "--remove", "--config", str(path)]) == 0  # also after the subcommand


def test_config_found_by_walking_up(tmp_path, monkeypatch):
    path = project(tmp_path)
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert config.find_config() == path.resolve()
    assert cli.main(["block", "5"]) == 0
    assert (tmp_path / ".st" / "state.json").exists() and not (deep / ".st").exists()


def test_no_config_found(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "find_config", lambda start=None: None)
    assert cli.main(["balance"]) == 1
    assert "no taxameter.toml found" in capsys.readouterr().out


def test_unknown_table_and_key_errors(tmp_path, capsys):
    for text, msg in [
        ("[nope]\nx = 1\n", "unknown table(s)"),
        ("[project]\nbogus = 1\n", "unknown key(s) in [project]: bogus"),
        ("[policy]\nbogus = 1\n", "unknown key(s) in [policy]: bogus"),
        ("[box]\nbusy = \n", "cannot read"),
    ]:
        path = write_toml(tmp_path / "taxameter.toml", text)
        with pytest.raises(ConfigError, match=re.escape(msg)):
            config.load(path)
        assert cli.main(["--config", str(path), "balance"]) == 1
        assert msg in capsys.readouterr().out


def test_unknown_provider_is_a_clear_error(tmp_path, capsys):
    path = write_toml(tmp_path / "taxameter.toml", '[provider]\nname = "lambda"\n')
    assert cli.main(["--config", str(path), "balance"]) == 1
    assert "unknown provider 'lambda'" in capsys.readouterr().out
