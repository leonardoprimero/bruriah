from __future__ import annotations

from dataclasses import dataclass

from .packs import CapabilityPolicy, DomainPack, PackError, SourcePolicy
@dataclass(frozen=True)
class Registry:
    packs: tuple[DomainPack, ...]
    sources: tuple[SourcePolicy, ...]
    capabilities: tuple[CapabilityPolicy, ...]
    @classmethod
    def from_packs(cls, packs: list[DomainPack]) -> "Registry":
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
        return cls(ordered, sources, capabilities)
    @property
    def pack_ids(self) -> tuple[str, ...]:
        return tuple(item.pack_id for item in self.packs)
