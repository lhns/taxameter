"""Files uploaded to the box: the four generic scripts plus the rendered `env` (ADR 0004)."""

from __future__ import annotations

import shlex
from importlib import resources

from .config import Config

BOX_HOME = "/root/.taxameter"
STAGING = f"{BOX_HOME}/push"
SCRIPTS = ("box.sh", "idle_autostop.sh", "cuda_env.sh", "pack_results.sh")
# Passed to the provider's create call: relaunches the watchdog on every container start.
ONSTART = f"[ -f {BOX_HOME}/box.sh ] && bash {BOX_HOME}/box.sh ensure-watchdog >> /root/autostop.log 2>&1 || true"


def _words(name: str, items: list[str]) -> str:
    """Space-joined list for the env file; the box splits it on whitespace, so an entry cannot contain one."""
    for item in items:
        if any(c.isspace() for c in item):
            raise ValueError(f"{name}: entry {item!r} contains whitespace")
    return " ".join(items)


def render_env(cfg: Config, provider: str, dns_host: str) -> str:
    box, res = cfg.box, cfg.results
    values = {
        "TAXAMETER_HOME": BOX_HOME,
        "TAXAMETER_PROVIDER": provider,
        "TAXAMETER_PROJECT": cfg.project.name,
        "TAXAMETER_DIR": cfg.remote_dir,
        "TAXAMETER_STAGING": STAGING,
        "TAXAMETER_BUSY": box.busy,
        "TAXAMETER_BATCH": box.batch,
        "TAXAMETER_BATCH_LOG": box.batch_log,
        "TAXAMETER_BATCH_DONE": box.batch_done,
        "TAXAMETER_LOGS_DIR": box.logs_dir,
        "TAXAMETER_LOGS_DEPTH": str(box.logs_depth),
        "TAXAMETER_PROVISION_HOOK": box.provision_hook,
        "TAXAMETER_VERIFY_HOOK": box.verify_hook,
        "TAXAMETER_DONE_MARKER": res.done_marker,
        "TAXAMETER_RESULTS": _words("results.dirs", res.dirs),
        "TAXAMETER_PACK_EXTRA": _words("results.extra", res.extra),
        "TAXAMETER_PACK_INCLUDE": _words("results.include", res.include),
        "TAXAMETER_PACK_WHOLE": _words("results.whole", res.whole),
        "TAXAMETER_PACK_EXCLUDE": _words("results.exclude", res.exclude),
        "TAXAMETER_MODELS_DIR": cfg.models.dir,
        "TAXAMETER_IDLE_MIN": str(cfg.policy["idle_stop_min"]),
        "TAXAMETER_BUSY_UTIL": str(cfg.policy["idle_gpu_util"]),
        "TAXAMETER_DNS_HOST": dns_host,
    }
    lines = ["# Rendered by taxameter from taxameter.toml. Do not edit; it is replaced on every upload."]
    lines += [f"export {k}={shlex.quote(v)}" for k, v in values.items()]
    return "\n".join(lines) + "\n"


def box_files(cfg: Config, provider: str, dns_host: str) -> dict[str, bytes]:
    """Box file name -> content (LF endings), ready to upload into BOX_HOME."""
    pkg = resources.files("taxameter") / "box"
    files = {n: (pkg / n).read_bytes().replace(b"\r\n", b"\n") for n in SCRIPTS}
    files["env"] = render_env(cfg, provider, dns_host).encode()
    return files
