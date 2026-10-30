"""Vast.ai backend: REST API via httpx (no vastai CLI). The key is read from a file and only sent as a header."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from taxameter.output import redact
from taxameter.providers.base import (
    CreateSpec,
    Endpoint,
    Instance,
    Offer,
    OfferGone,
    OfferQuery,
    ProviderError,
)

API_BASE = "https://console.vast.ai"
DEFAULT_KEY_FILE = Path.home() / ".config" / "vastai" / "vast_api_key"
SEED_BLOCKLIST = {
    "483833": "2026-09-29: host network down ~28 min of every ~45 min (SSH + DNS outages)",
    "366851": "2026-10: stopped from outside twice, GPU rented away while stopped",
}
_STATUS = {
    "running": "running",
    "loading": "loading",
    "created": "loading",
    "scheduling": "loading",
    "exited": "stopped",
    "stopped": "stopped",
    "offline": "stopped",
}


class _HttpError(ProviderError):
    def __init__(self, msg: str, status: int, body: str) -> None:
        super().__init__(msg)
        self.status, self.body = status, body


class Api:
    """Thin REST layer on raw Vast JSON."""

    def __init__(self, key_file: Path, transport: httpx.BaseTransport | None = None, base: str = API_BASE) -> None:
        self.key_file, self.transport, self.base = key_file, transport, base
        self._client: httpx.Client | None = None

    def client(self) -> httpx.Client:
        if self._client is None:
            try:
                key = self.key_file.read_text(encoding="utf-8").strip()
            except OSError as e:
                raise ProviderError(f"cannot read API key file {self.key_file}: {type(e).__name__}") from None
            redact.add(key)
            self._client = httpx.Client(
                base_url=self.base, headers={"Authorization": f"Bearer {key}"}, timeout=30, transport=self.transport
            )
        return self._client

    def req(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self.client().request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ProviderError(redact(f"{method} {path}: {type(e).__name__}: {e}")) from None
        if r.status_code >= 400:
            raise _HttpError(redact(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}"), r.status_code, r.text)
        try:
            data = r.json()
        except ValueError:
            raise ProviderError(f"{method} {path}: non-JSON response") from None
        if isinstance(data, dict):
            for k in ("instance_api_key", "jupyter_token", "api_key"):
                if isinstance(data.get(k), str):
                    redact.add(data[k])
        return data

    def user(self) -> dict:
        return self.req("GET", "/api/v0/users/current/")

    def instances(self) -> list[dict]:
        rows: list[dict] = []
        params = {"select_filters": "{}", "order_by": json.dumps([{"col": "id", "dir": "asc"}]), "limit": "25"}
        for _ in range(40):
            data = self.req("GET", "/api/v1/instances/", params=params)
            rows += [_norm_instance(r) for r in data.get("instances") or []]
            if not data.get("next_token"):
                break
            params["after_token"] = data["next_token"]
        return rows

    def instance(self, iid: str) -> dict | None:
        data = self.req("GET", f"/api/v0/instances/{iid}/", params={"owner": "me"})
        row = data.get("instances") if isinstance(data, dict) else None
        return _norm_instance(row) if row else None

    def offers(self, query: dict) -> list[dict]:
        return self.req("POST", "/api/v0/bundles/", json=query).get("offers", [])

    def create(self, offer_id: str, payload: dict) -> dict:
        return self.req("PUT", f"/api/v0/asks/{offer_id}/", json=payload)

    def set_state(self, iid: str, state: str) -> dict:
        return self.req("PUT", f"/api/v0/instances/{iid}/", json={"state": state})

    def reboot(self, iid: str) -> dict:
        return self.req("PUT", f"/api/v0/instances/reboot/{iid}/", json={})

    def destroy(self, iid: str) -> dict:
        return self.req("DELETE", f"/api/v0/instances/{iid}/", json={})


def _norm_instance(row: dict) -> dict:
    row = dict(row)
    if isinstance(row.get("extra_env"), list):
        row["extra_env"] = {e[0]: e[1] for e in row["extra_env"] if isinstance(e, list | tuple) and len(e) == 2}
    for k in ("jupyter_token", "instance_api_key"):
        if isinstance(row.get(k), str):
            redact.add(row[k])
    return row


def endpoints_from_instance(inst: dict | None) -> list[Endpoint]:
    """Current SSH endpoints of a raw instance row: direct (public ip + mapped 22/tcp) first, then the proxy.
    Ports change after every restart, so this is always computed from a fresh API record."""
    if not inst:
        return []
    eps: list[Endpoint] = []
    ip = (inst.get("public_ipaddr") or "").strip()
    p22 = (inst.get("ports") or {}).get("22/tcp") or []
    if ip and p22 and p22[0].get("HostPort"):
        eps.append(Endpoint("direct", ip, int(p22[0]["HostPort"])))
    host, port = inst.get("ssh_host"), inst.get("ssh_port")
    if host and port:
        # the vastai CLI's rule: jupyter runtypes put sshd on the proxy's port + 1
        if "jupyter" in (inst.get("image_runtype") or ""):
            eps.append(Endpoint("proxy", host, int(port) + 1))
        eps.append(Endpoint("proxy", host, int(port)))
    return eps


def _host(row: dict) -> str | None:
    return str(row["host_id"]) if row.get("host_id") is not None else None


def instance_from_row(row: dict) -> Instance:
    status = _STATUS.get(str(row.get("actual_status") or "").lower(), "loading")  # unknown counts toward the cap
    started = row.get("start_date")
    return Instance(
        id=str(row["id"]),
        status=status,
        host_id=_host(row),
        gpu_name=row.get("gpu_name") or "?",
        price_per_h=float(row.get("dph_total") or 0.0),
        storage_per_h=float((row.get("instance") or {}).get("diskHour") or 0.0),
        started_at=float(started) if started else None,
        label=row.get("label") or "",
        endpoints=endpoints_from_instance(row),
        raw=row,
    )


def build_offer_query(q: OfferQuery) -> dict:
    return {
        "verified": {"eq": True},
        "external": {"eq": False},
        "rentable": {"eq": True},
        "rented": {"eq": False},
        "num_gpus": {"eq": 1},
        "gpu_name": {"in": q.gpus},
        "gpu_ram": {"gte": q.min_vram_gb * 950},  # MiB; 5 % slack because a "24 GB" L4 reports 23034 MiB
        "reliability2": {"gte": q.min_reliability},
        "inet_down": {"gte": q.min_inet_mbps},
        "disk_space": {"gte": q.min_disk_gb},
        "direct_port_count": {"gte": 1},
        "cuda_max_good": {"gte": q.min_cuda},
        "order": [["dph_total", "asc"]],
        "type": "bid" if q.interruptible else "on-demand",
        "limit": q.limit,
        "allocated_storage": q.min_disk_gb,
    }


def offer_score(o: dict) -> float:
    if o.get("dlperf_per_dphtotal"):
        return float(o["dlperf_per_dphtotal"])
    if o.get("dlperf") and o.get("dph_total"):
        return float(o["dlperf"]) / float(o["dph_total"])
    return 0.0


def offer_from_row(o: dict) -> Offer:
    return Offer(
        id=str(o["id"]),
        host_id=_host(o),
        gpu_name=o.get("gpu_name") or "?",
        vram_gb=float(o.get("gpu_ram") or 0) / 1024,
        price_per_h=float(o.get("dph_total") or 0.0),
        score=round(offer_score(o), 1),
        reliability=float(o.get("reliability2") or o.get("reliability") or 0),
        location=str(o.get("geolocation") or "?"),
        raw=o,
    )


def usable(o: dict, q: OfferQuery) -> bool:
    """Vast-specific: verified machines only (the API returns verified=None with the truth in `verification`),
    with direct ports, enough reliability and VRAM."""
    if not (o.get("verified") is True or o.get("verification") == "verified"):
        return False
    if (o.get("direct_port_count") or 0) < 1:
        return False
    if float(o.get("reliability2") or o.get("reliability") or 0) < q.min_reliability:
        return False
    return float(o.get("gpu_ram") or 0) >= q.min_vram_gb * 950


def _gone(text: str) -> bool:
    return "no_such_ask" in text or "not available" in text


class VastProvider:
    name = "vast"
    dns_host = "console.vast.ai"
    seed_blocklist = SEED_BLOCKLIST
    default_key_file = DEFAULT_KEY_FILE

    def __init__(self, key_file: Path | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self.api = Api(key_file or self.default_key_file, transport)

    def offers(self, q: OfferQuery) -> list[Offer]:
        return [offer_from_row(o) for o in self.api.offers(build_offer_query(q)) if usable(o, q)]

    def create(self, offer: Offer, spec: CreateSpec) -> str:
        bid = float(offer.raw.get("min_bid") or offer.price_per_h) * 1.15
        payload = {
            "client_id": "me",
            "image": spec.image,
            "env": {},
            "disk": spec.disk_gb,
            "label": spec.label,
            "runtype": "ssh_direc ssh_proxy",
            "onstart": spec.onstart,
            "image_login": None,
            "python_utf8": False,
            "lang_utf8": False,
            "use_jupyter_lab": False,
            "jupyter_dir": None,
            "force": False,
            "cancel_unavail": True,
            "extra": None,
            "price": round(bid, 4) if spec.interruptible else None,
        }
        try:
            res = self.api.create(offer.id, payload)
        except _HttpError as e:
            if e.status == 400 and _gone(e.body):
                raise OfferGone(f"offer {offer.id} is gone: {e}") from None
            raise
        if not res.get("success") or not res.get("new_contract"):
            text = redact(json.dumps(res)[:300])
            if _gone(text):
                raise OfferGone(f"offer {offer.id} is gone: {text}")
            raise ProviderError(f"create failed: {text}")
        return str(res["new_contract"])

    def instance(self, iid: str) -> Instance | None:
        row = self.api.instance(iid)
        return instance_from_row(row) if row else None

    def instances(self) -> list[Instance]:
        return [instance_from_row(r) for r in self.api.instances()]

    def start(self, iid: str) -> None:
        self.api.set_state(iid, "running")

    def stop(self, iid: str) -> None:
        self.api.set_state(iid, "stopped")

    def reboot(self, iid: str) -> None:
        self.api.reboot(iid)

    def destroy(self, iid: str) -> None:
        self.api.destroy(iid)

    def credit(self) -> float | None:
        return float(self.api.user().get("credit") or 0.0)
