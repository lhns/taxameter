"""Provider interface (ADR 0002): normalised records and the protocol every backend implements.

The core never reads provider JSON; `raw` is kept for display only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class ProviderError(RuntimeError):
    pass


class OfferGone(ProviderError):
    """The offer was taken between listing and renting: the next ranked offer may still work."""


@dataclass(frozen=True)
class Endpoint:
    kind: str  # direct | proxy
    host: str
    port: int
    user: str = "root"

    def __str__(self) -> str:
        return f"{self.kind} {self.host}:{self.port}"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "host": self.host, "port": self.port, "user": self.user}

    @classmethod
    def from_dict(cls, d: dict | None) -> Endpoint | None:
        if d and d.get("host") and d.get("port"):
            return cls(d["kind"], d["host"], int(d["port"]), d.get("user") or "root")
        return None


@dataclass
class Offer:
    id: str
    host_id: str | None  # None on providers without host ids: blocklist and last-good ranking do nothing
    gpu_name: str
    vram_gb: float
    price_per_h: float
    score: float  # throughput per dollar where the provider knows it, else 0
    reliability: float
    location: str
    raw: dict[str, Any] = field(default_factory=dict)
    ram_gb: float = 0.0  # host RAM; 0 when the provider does not report it
    cpu_cores: float = 0.0  # usable CPU cores; 0 when unknown
    inet_down_cost: float | None = None  # USD per GB downloaded; None when unknown
    price_rank: int = 0  # set by the core's ranking
    traffic_per_h: float = 0.0  # expected download cost per box hour (set by the core, ADR 0010)
    num_gpus: int = 1  # GPUs in the box; price_per_h is for all of them
    effective_price_per_h: float = 0.0  # price_per_h + traffic_per_h; informational, caps use price_per_h
    machine_id: str | None = None  # stable across offer-id rotation; None on providers without machine ids

    def __post_init__(self) -> None:
        if not self.effective_price_per_h:
            self.effective_price_per_h = self.price_per_h


@dataclass
class Instance:
    id: str
    status: str  # running | loading | stopped | gone
    host_id: str | None
    gpu_name: str
    price_per_h: float
    storage_per_h: float  # what a stopped instance still bills
    started_at: float | None  # epoch seconds
    label: str
    endpoints: list[Endpoint] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    cpu_cores: float = 0.0  # usable CPU cores; 0 when unknown
    inet_down_cost: float | None = None  # USD per GB downloaded; None when unknown
    inet_down_billed_gb: float | None = None  # traffic billed so far
    num_gpus: int = 1


@dataclass
class OfferQuery:
    gpus: list[str]
    min_vram_gb: float
    min_disk_gb: float
    min_reliability: float
    min_inet_mbps: float
    min_cuda: float
    interruptible: bool = False
    limit: int = 100
    min_cpu_cores: float = 0  # 0: no filter
    max_inet_down_cost: float = 0  # USD per GB; 0: no filter
    num_gpus: int = 1  # exact GPU count of the box (ADR 0011)
    machine_id: str | None = None  # only this machine's offers (follows a rotated offer id, ADR 0012)


@dataclass
class CreateSpec:
    image: str
    disk_gb: float
    label: str
    onstart: str
    interruptible: bool = False


class Provider(Protocol):
    name: str
    dns_host: str  # resolved by the box health probe
    seed_blocklist: dict[str, str]  # host id -> reason, shipped with the provider
    default_key_file: Path

    def offers(self, q: OfferQuery) -> list[Offer]: ...
    def create(self, offer: Offer, spec: CreateSpec) -> str: ...
    def instance(self, iid: str) -> Instance | None: ...
    def instances(self) -> list[Instance]: ...
    def start(self, iid: str) -> None: ...
    def stop(self, iid: str) -> None: ...
    def set_label(self, iid: str, label: str) -> None: ...
    def reboot(self, iid: str) -> None: ...
    def destroy(self, iid: str) -> None: ...
    def credit(self) -> float | None: ...
