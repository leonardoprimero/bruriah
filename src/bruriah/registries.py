from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType

from .packs import CapabilityPolicy, Currency, DomainPack, PackError, SourcePolicy, pack_currency
@dataclass(frozen=True)
class Registry:
    packs: tuple[DomainPack, ...]
    sources: tuple[SourcePolicy, ...]
    capabilities: tuple[CapabilityPolicy, ...]
    # `pack_id -> current|stale|expired`, for the packs whose review window was actually assessed.
    # A pack missing from the map is `current`, because the absence of a claim is not a claim of
    # aging -- a registry built without a date says nothing about currency and must behave exactly
    # as it did before currency existed.
    currency: Mapping[str, Currency] = field(default_factory=lambda: MappingProxyType({}))
    @classmethod
    def from_packs(cls, packs: list[DomainPack], *, today: date | None = None) -> "Registry":
        """`today` is optional and never read from the clock: pass it to have each pack's review
        window assessed, omit it to build a registry that makes no currency claim. The loader that
        knows the date supplies it (`platform.load_registry`); nothing here goes looking."""
        ordered = tuple(sorted(packs, key=lambda item: item.pack_id))
        if len({item.pack_id for item in ordered}) != len(ordered):
            raise PackError("duplicate_pack_id")
        seen_sources: set[str] = set()
        seen_capabilities: set[str] = set()
        for pack in ordered:
            for source in pack.sources:
                if source.source_id in seen_sources:
                    raise PackError("duplicate_source_id")
                seen_sources.add(source.source_id)
            for capability in pack.capabilities:
                if capability.capability_id in seen_capabilities:
                    raise PackError("duplicate_capability_id")
                seen_capabilities.add(capability.capability_id)
        sources = tuple(item for pack in ordered for item in sorted(pack.sources, key=lambda value: value.source_id))
        capabilities = tuple(item for pack in ordered for item in sorted(pack.capabilities, key=lambda value: value.capability_id))
        currency = MappingProxyType(
            {} if today is None
            else {item.pack_id: pack_currency(item, today) for item in ordered}
        )
        return cls(ordered, sources, capabilities, currency)
    @property
    def pack_ids(self) -> tuple[str, ...]:
        return tuple(item.pack_id for item in self.packs)

    def currency_of(self, pack_id: str) -> Currency:
        """What this registry knows about one pack's review window; `current` when it knows
        nothing, for the reason recorded on the `currency` field."""
        return self.currency.get(pack_id, "current")
