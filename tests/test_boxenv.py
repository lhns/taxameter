import shlex
import shutil
import subprocess
import tarfile
from importlib import resources
from pathlib import Path

import pytest

from taxameter import boxenv
from taxameter.config import Config


def _bash() -> str | None:
    exe = shutil.which("bash")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-c", "echo ok"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return exe if r.returncode == 0 and r.stdout.strip() == "ok" else None


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="no working bash")
EXPECTED = [
    "TAXAMETER_HOME", "TAXAMETER_PROVIDER", "TAXAMETER_PROJECT", "TAXAMETER_DIR", "TAXAMETER_STAGING",
    "TAXAMETER_BUSY", "TAXAMETER_BATCH", "TAXAMETER_BATCH_LOG", "TAXAMETER_BATCH_DONE", "TAXAMETER_LOGS_DIR",
    "TAXAMETER_PROVISION_HOOK", "TAXAMETER_VERIFY_HOOK", "TAXAMETER_DONE_MARKER", "TAXAMETER_RESULTS",
    "TAXAMETER_PACK_EXTRA", "TAXAMETER_PACK_INCLUDE", "TAXAMETER_PACK_WHOLE", "TAXAMETER_PACK_EXCLUDE",
    "TAXAMETER_MODELS_DIR", "TAXAMETER_IDLE_MIN", "TAXAMETER_BUSY_UTIL", "TAXAMETER_DNS_HOST",
]  # fmt: skip


def parse_env(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or not line:
            continue
        head, _, rest = line.partition(" ")
        assert head == "export"
        (assign,) = shlex.split(rest)
        k, _, v = assign.partition("=")
        out[k] = v
    return out


def test_render_env_has_every_variable(tmp_path):
    cfg = Config(root=tmp_path)
    cfg.box.busy = r"(^|/)(cargo|rustc)( |$)|target/(release|debug)/x$y"
    cfg.results.dirs = ["bench/results", "runs"]
    cfg.results.exclude = ["*.gguf", "*/target/*"]
    text = boxenv.render_env(cfg, "vast", "console.vast.ai")
    env = parse_env(text)
    assert list(env) == EXPECTED
    assert env["TAXAMETER_BUSY"] == cfg.box.busy
    assert env["TAXAMETER_RESULTS"] == "bench/results runs"
    assert env["TAXAMETER_PACK_EXCLUDE"] == "*.gguf */target/*"
    assert env["TAXAMETER_PROVIDER"] == "vast"
    assert env["TAXAMETER_DIR"] == cfg.remote_dir
    assert env["TAXAMETER_IDLE_MIN"] == "20"
    assert env["TAXAMETER_BATCH"] == ""
    assert "\r" not in text


@needs_bash
def test_env_sources_in_bash(tmp_path):
    cfg = Config(root=tmp_path)
    cfg.box.busy = "a|(b)$c 'q'"
    f = tmp_path / "env"
    f.write_bytes(boxenv.render_env(cfg, "vast", "").encode())
    r = subprocess.run(
        [BASH, "-c", f'. "{f.as_posix()}"; printf %s "$TAXAMETER_BUSY"'], capture_output=True, text=True, timeout=20
    )
    assert r.stdout == cfg.box.busy


def test_box_files(tmp_path):
    files = boxenv.box_files(Config(root=tmp_path), "vast", "console.vast.ai")
    assert set(files) == {"box.sh", "idle_autostop.sh", "cuda_env.sh", "pack_results.sh", "env"}
    assert all(b"\r\n" not in b for b in files.values())
    assert "/root/.taxameter/box.sh" in boxenv.ONSTART


@pytest.mark.parametrize("field", ["include", "whole", "exclude", "extra", "dirs"])
def test_space_in_list_entry_rejected(tmp_path, field):
    cfg = Config(root=tmp_path)
    setattr(cfg.results, field, ["a b"])
    with pytest.raises(ValueError):
        boxenv.render_env(cfg, "vast", "")


def _script(name: str) -> Path:
    return Path(str(resources.files("taxameter") / "box" / name))


@needs_bash
@pytest.mark.parametrize("name", boxenv.SCRIPTS)
def test_bash_syntax(name):
    r = subprocess.run([BASH, "-n", _script(name).as_posix()], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


@needs_bash
def test_count_procs_empty_is_zero():
    box = _script("box.sh")
    r = subprocess.run(
        [BASH, "-c", f'. "{box.as_posix()}"; count_procs ""; count_procs "" ""; count_procs'],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "TAXAMETER_HOME": box.parent.as_posix()},
    )
    assert r.stdout.split() == ["0", "0", "0"], r.stderr


@needs_bash
def test_pack_results_selection(tmp_path):
    proj = tmp_path / "proj"
    for rel in [
        "results/a/r.json", "results/a/DONE", "results/a/big.gguf", "results/a/notes.txt",
        "results/a/target/x.json", "whole/w/data.bin", "whole/w/keep.gguf", "extra/e.log", "extra/e.bin",
    ]:  # fmt: skip
        p = proj / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    out = tmp_path / "out.tgz"  # relative OUT: GNU tar reads "C:" as a remote host
    (tmp_path / "env").write_text(
        "export TAXAMETER_PACK_INCLUDE='*.json *.log DONE'\n"
        "export TAXAMETER_PACK_WHOLE=whole\n"
        "export TAXAMETER_PACK_EXCLUDE='*.gguf */target/*'\n"
    )
    shutil.copy(_script("pack_results.sh"), tmp_path / "pack_results.sh")
    r = subprocess.run(
        [BASH, (tmp_path / "pack_results.sh").as_posix(), "results", "whole/w", "extra"],
        cwd=proj,
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin:/mingw64/bin", "OUT": "../out.tgz", "TAXAMETER_HOME": tmp_path.as_posix()},
    )
    assert r.returncode == 0, r.stderr
    with tarfile.open(out) as t:
        names = sorted(m.name for m in t.getmembers() if m.isfile())
    assert names == ["extra/e.log", "results/a/DONE", "results/a/r.json", "whole/w/data.bin"]
