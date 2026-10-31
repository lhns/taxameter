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
    price_rank: int = 0  # set by the core's ranking


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
