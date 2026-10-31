"""Shared fakes: httpx.MockTransport plays the Vast API, subprocess.run is replaced by a fake ssh/scp/git.
Nothing here touches the network or a real cloud."""

from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import tarfile
import time
from pathlib import Path

import httpx
import pytest

from taxameter import boxenv
from taxameter import core as vc
from taxameter.config import Config
from taxameter.output import Out

KEY = "sk-TESTKEY-0123456789abcdef"
INSTANCE_KEY = "INSTKEY-SECRET-987654321"
GONE_BODY = {"success": False, "error": "no_such_ask", "msg": "Instance type 123 not available"}


def make_instance(iid=100, status="running", port=41022, host_id=555, runtype="ssh_direc ssh_proxy", label="proj"):
    return {
        "id": iid, "actual_status": status, "host_id": host_id, "machine_id": 9, "gpu_name": "RTX 3090",
        "dph_total": 0.4, "public_ipaddr": "1.2.3.4", "ssh_host": "ssh5.vast.ai", "ssh_port": 19554,
        "image_runtype": runtype, "label": label, "start_date": time.time() - 3600,
        "ports": {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(port)}]} if status == "running" else None,
        "instance": {"diskHour": 0.06}, "extra_env": [["A", "1"]], "jupyter_token": "JUPYTOKEN-abcdef",
    }  # fmt: skip


def offer(oid, host, price, score, rel=0.995, verified=None, verification="verified", ports=10, vram=24564):
    return {
        "id": oid, "host_id": host, "machine_id": host + 1, "gpu_name": "RTX 3090", "gpu_ram": vram,
        "dph_total": price, "dlperf_per_dphtotal": score, "reliability2": rel, "verified": verified,
        "verification": verification, "direct_port_count": ports, "geolocation": "X", "inet_down": 900,
        "cuda_max_good": 12.8,
    }  # fmt: skip


class FakeVast:
    def __init__(self):
        self.requests: list[tuple[str, str, bytes]] = []
        self.instances: dict[int, dict] = {}
        self.offers: list[dict] = []
        self.credit = 10.0
        self.fail = False
        self.gone_asks: set[int] = set()  # create answers HTTP 400 no_such_ask for these offer ids
        self.create_mode: str | None = None  # "500" | "timeout": every create fails that way

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append((req.method, req.url.path, req.content))
        assert req.headers["authorization"] == f"Bearer {KEY}"
        if self.fail:  # an API that echoes the credential back must not leak it through our error messages
            return httpx.Response(500, text=f"internal error for {req.headers['authorization']}")
        p = req.url.path
        if p == "/api/v0/users/current/":
            return httpx.Response(200, json={"credit": self.credit, "balance": 0})
        if p == "/api/v1/instances/":
            return httpx.Response(200, json={"instances": list(self.instances.values()), "next_token": None})
        if p == "/api/v0/bundles/":
            return httpx.Response(200, json={"offers": self.offers})
        m = re.fullmatch(r"/api/v0/asks/(\d+)/", p)
        if m and req.method == "PUT":
            if self.create_mode == "timeout":
                raise httpx.ReadTimeout("timed out", request=req)
            if self.create_mode == "500":
                return httpx.Response(500, text="boom")
            if int(m.group(1)) in self.gone_asks:
                return httpx.Response(400, json=GONE_BODY)
            self.instances[999] = make_instance(999, host_id=777)
            return httpx.Response(200, json={"success": True, "new_contract": 999, "instance_api_key": INSTANCE_KEY})
        if re.fullmatch(r"/api/v0/instances/reboot/(\d+)/", p):
            return httpx.Response(200, json={"success": True})
        m = re.fullmatch(r"/api/v0/instances/(\d+)/", p)
        if m:
            iid = int(m.group(1))
            if req.method == "GET":
                return httpx.Response(200, json={"instances": self.instances.get(iid)})
            if req.method == "PUT":
                body = json.loads(req.content)
                if body.get("state") == "stopped":
                    self.instances[iid]["actual_status"] = "exited"
                return httpx.Response(200, json={"success": True})
            if req.method == "DELETE":
                self.instances.pop(iid, None)
                return httpx.Response(200, json={"success": True})
        return httpx.Response(404, text="not found")

    def calls(self, method, pattern=""):
        return [r for r in self.requests if r[0] == method and pattern in r[1]]


def tar_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


class FakeShell:
    """Stands in for subprocess.run: ssh/scp by port; ports in `down` refuse connections."""

    def __init__(self, tmp: Path):
        self.argvs: list[list[str]] = []
        self.down: set[int] = set()
        self.health = "exp_procs=2\nbatch_alive=1\ngpu_util=90\nprogress_age_s=30\nwatchdog=1\n"
        self.archive = self._archive(tmp)
        self.verify = "verify_gpu=1\nverify_cuda=1\nverify_hook=1\nverify_ok=1\n"
        self.git_paths = ["crates", "scripts", "Cargo.toml"]
        self.sub_files = {"CMakeLists.txt": b"project(llama)"}
        self.stdins: list[bytes] = []
        self.nightly_rc = 0
        self.done_line = "NIGHTLY_DONE"
        self.run_out = "launched scripts/nightly.sh at 12:00:00\n"
        self.provision_log = "cuda: ok\n12:01:00 PROVISION_DONE rc=0\n"

    @staticmethod
    def _archive(tmp: Path) -> bytes:
        src = tmp / "box"
        f = src / "bench" / "results" / "20260101" / "DONE"
        f.parent.mkdir(parents=True)
        f.write_text('{"ok": 1}')
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(f, arcname="bench/results/20260101/DONE")
        return buf.getvalue()

    def ssh_cmds(self) -> list[str]:
        return [a[-1] for a in self.argvs if a[0] == "ssh"]

    def __call__(self, argv, input=None, capture_output=True, timeout=None, check=False, stdin=None, **kw):
        self.argvs.append(list(argv))
        flag = "-P" if argv[0] == "scp" else "-p"
        port = int(argv[argv.index(flag) + 1]) if flag in argv else 0

        def res(rc, out=b"", err=b""):
            return subprocess.CompletedProcess(argv, rc, out, err)

        if argv[0] == "git":
            if "ls-tree" in argv:
                return res(0, "\n".join(self.git_paths).encode())
            if "archive" in argv:
                if "-C" in argv:  # a submodule
                    prefix = next(a.split("=", 1)[1] for a in argv if a.startswith("--prefix="))
                    return res(0, tar_bytes({prefix + k: v for k, v in self.sub_files.items()}))
                if "--" not in argv:  # whole HEAD
                    return res(0, tar_bytes({"README.md": b"x", "src/main.rs": b"x"}))
                paths = argv[argv.index("--") + 1 :]
                return res(0, tar_bytes({f"{x}/f.txt" if "." not in x else x: b"x" for x in paths}))
            if "write-tree" in argv:
                return res(0, b"deadbeef\n")
            return res(0, b"")
        if input is not None:
            self.stdins.append(input)
        if port in self.down:
            return res(255, b"", b"ssh: connect to host 1.2.3.4 port %d: Connection refused" % port)
        if argv[0] == "scp":
            Path(argv[-1]).write_bytes(self.archive)
            return res(0)
        cmd = argv[-1]
        banner = b"Welcome to vast.ai. Have fun!\nAI agents: read the docs\n"
        if cmd == "echo up":
            return res(0, banner + b"up\n")
        if ".incoming" in cmd:
            return res(0, b"helper-ok\n")
        if "box.sh health" in cmd:
            return res(0, banner + self.health.encode())
        if "box.sh pack" in cmd:
            return res(
                0, b"1 files, 4K -> /root/results.tgz\nmd5=%s\n" % hashlib.md5(self.archive).hexdigest().encode()
            )
        if "box.sh verify" in cmd:
            return res(0, self.verify.encode())
        if "pushed-runs" in cmd:
            return res(0, b"pushed-runs\n")
        if "install-push" in cmd:
            return res(0, b"installed 3 files into /root/proj\n")
        if "sleep 5" in cmd:  # the wait loop on the box
            return res(0, f"12:00:00\n{self.done_line} rc={self.nightly_rc}\n".encode())
        if "box.sh run " in cmd:
            return res(0, banner + self.run_out.encode())
        if "nohup" in cmd:
            return res(0, banner + b"launched 0\n")
        if "provision.log" in cmd and "tail" in cmd:
            return res(0, self.provision_log.encode())
        if "ensure-watchdog" in cmd:
            return res(0, b"watchdog=1 mode=supervisor\n")
        return res(0, b"ok\n")


FAKE_BOX_FILES = {
    "box.sh": b"#!/usr/bin/env bash\n",
    "idle_autostop.sh": b"#!/usr/bin/env bash\n",
    "cuda_env.sh": b"# cuda\n",
    "pack_results.sh": b"#!/usr/bin/env bash\n",
    "env": b"export TAXAMETER_HOME=/root/.taxameter\n",
}


def make_cfg(root: Path, key: Path) -> Config:
    """A tetraeder-like project."""
    cfg = Config(root=root)
    cfg.project.name = "proj"
    cfg.project.state_dir = ".vast"
    cfg.project.ssh_key = str(root / "id_test")
    cfg.provider.api_key_file = str(key)
    cfg.image.name = "ghcr.io/lhns/proj-box"
    cfg.image.tag = "latest"
    cfg.push.paths = ["crates", "scripts", "Cargo.toml"]
    cfg.push.submodules = ["vendor/llama.cpp"]
    cfg.results.dirs = ["bench/results"]
    cfg.results.done_marker = "DONE"
    cfg.results.exclude = ["*.gguf"]
    cfg.nightly.script = "scripts/nightly.sh"
    cfg.nightly.done_line = "NIGHTLY_DONE"
    cfg.box.batch_log = "runs/vast/nightly.log"
    cfg.box.logs_dir = "runs/vast"
    cfg.models.from_toml = "bench/spec/phase0.toml"
    cfg.models.from_key = "model.*.hf"
    cfg.policy["max_box_hours"] = 3
    return cfg


@pytest.fixture(autouse=True)
def _plain_output():
    Out.json_mode = False  # cli.main sets it per call; do not leak it into the next test
    yield
    Out.json_mode = False


@pytest.fixture
def env(tmp_path, monkeypatch):
    key = tmp_path / "vast_api_key"
    key.write_text(KEY + "\n")
    fake = FakeVast()
    shell = FakeShell(tmp_path)
    monkeypatch.setattr(vc.subprocess, "run", shell)
    monkeypatch.setattr(vc.subprocess, "call", lambda argv: shell(argv).returncode)
    monkeypatch.setattr(boxenv, "box_files", lambda cfg, provider, dns_host: dict(FAKE_BOX_FILES))
    cfg = make_cfg(tmp_path, key)
    ctx = vc.Ctx(cfg, transport=httpx.MockTransport(fake.handler), sleep=lambda s: None)
    return ctx, fake, shell


def tick(ctx, flags=None):
    """The watchdog reloads state from disk each pass (so manual commands in another shell are seen): persist
    the test setup first, and re-read records afterwards via R(ctx)."""
    ctx.store.save()
    return vc.watchdog_tick(ctx, flags or vc.WatchFlags())


def R(ctx, iid=100):
    return ctx.store.rec(iid)


def manage(ctx, fake, iid=100, **kw):
    fake.instances[iid] = make_instance(iid, **kw)
    rec = ctx.store.rec(iid, create=True)
    rec["host_id"] = str(fake.instances[iid]["host_id"])
    ctx.store.state["current"] = str(iid)
    return rec
