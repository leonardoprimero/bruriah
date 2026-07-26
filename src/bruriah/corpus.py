from __future__ import annotations

import fnmatch
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from markdown_it import MarkdownIt

from .models import Document, Passage, SourceMetadata


class CorpusPolicyError(ValueError):
    """Raised when a source is outside the eligible corpus."""


@dataclass(frozen=True)
class CorpusPolicy:
    include: tuple[str, ...]
    exclude: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> CorpusPolicy:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise CorpusPolicyError("unsupported_policy")
        include = data.get("include", [])
        exclude = data.get("exclude", [])
        if not all(isinstance(item, str) for item in [*include, *exclude]):
            raise CorpusPolicyError("invalid_policy_pattern")
        return cls(tuple(include), tuple(exclude))

    def exclusion_reason(self, path: Path, root: Path) -> str | None:
        approved_root = root.resolve(strict=True)
        try:
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(approved_root).as_posix()
        except (FileNotFoundError, RuntimeError, ValueError):
            return "outside_approved_root"
        parts = {part.casefold() for part in Path(relative).parts}
        denied = {"archive", "config", "generated", "personal", "diary", "sensitive"}
        if any(part.startswith(".") for part in Path(relative).parts) or parts & denied:
            return "excluded_by_policy"
        if any(fnmatch.fnmatchcase(relative, pattern) for pattern in self.exclude):
            return "excluded_by_policy"
        if not any(fnmatch.fnmatchcase(relative, pattern) for pattern in self.include):
            return "not_allowlisted"
        return None

    def discover(self, root: Path) -> tuple[Path, ...]:
        candidates = sorted(root.rglob("*.md"), key=lambda item: item.as_posix())
        return tuple(
            path
            for path in candidates
            if path.is_file() and self.exclusion_reason(path, root) is None
        )


@dataclass(frozen=True)
class RefAliases:
    aliases: Mapping[str, str]
    tombstones: frozenset[str]

    @classmethod
    def load(cls, path: Path) -> RefAliases:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 1:
            raise ValueError("unsupported_alias_registry")
        aliases = data.get("aliases", {})
        tombstones = data.get("tombstones", [])
        if not isinstance(aliases, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in aliases.items()
        ):
            raise ValueError("invalid_alias_registry")
        if not isinstance(tombstones, list) or not all(
            isinstance(item, str) for item in tombstones
        ):
            raise ValueError("invalid_alias_registry")
        return cls(MappingProxyType(dict(aliases)), frozenset(tombstones))

    def resolve(self, ref: str) -> str | None:
        if ref in self.tombstones:
            return None
        return self.aliases.get(ref, ref)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _frontmatter(lines: list[str]) -> tuple[dict[str, Any], int]:
    if not lines or lines[0].rstrip("\r\n") != "---":
        return {}, 0
    for index, line in enumerate(lines[1:], 1):
        if line.rstrip("\r\n") == "---":
            loaded = yaml.safe_load("".join(lines[1:index])) or {}
            if not isinstance(loaded, dict):
                raise ValueError("invalid_frontmatter")
            return loaded, index + 1
    raise ValueError("unterminated_frontmatter")


def _metadata(frontmatter: dict[str, Any]) -> SourceMetadata:
    provenance: list[str] = []
    for key in ("source", "sources"):
        value = frontmatter.get(key, [])
        values = value if isinstance(value, list) else [value]
        provenance.extend(item for item in values if isinstance(item, str) and item)
    urls: list[str] = []
    for key in ("url", "urls", "install_url", "source_url"):
        value = frontmatter.get(key, [])
        values = value if isinstance(value, list) else [value]
        urls.extend(item for item in values if isinstance(item, str) and item.startswith("http"))
    verified = next(
        (
            frontmatter[key]
            for key in ("verificado", "verified", "verification_date", "last_verified")
            if frontmatter.get(key) not in (None, "")
        ),
        "unknown",
    )
    status = frontmatter.get("status") or "unknown"
    return SourceMetadata(
        provenance=tuple(dict.fromkeys(provenance)),
        provenance_urls=tuple(dict.fromkeys(urls)),
        status=str(status),
        verification_date=str(verified),
    )


def parse_document(path: Path, root: Path, policy: CorpusPolicy) -> Document:
    reason = policy.exclusion_reason(path, root)
    if reason:
        raise CorpusPolicyError(reason)
    resolved = path.resolve(strict=True)
    raw = resolved.read_bytes()
    # `source_hash` below stays over `raw`, deliberately: the digest is the provenance anchor and
    # must identify the exact bytes on disk, not an interpretation of them.
    #
    # The PARSED text is normalized, because it is a different thing with a different job. It gets
    # tokenized for BM25, embedded, stored in the passage table and handed back verbatim as
    # evidence content -- none of which should depend on how the file happened to be checked out.
    # A CRLF working tree (every default Windows clone) otherwise indexes `\r` into every token
    # boundary and quotes it back to the caller, so the same document retrieves differently on two
    # machines. On an LF file this replacement is a no-op, which is why POSIX behaviour is
    # unchanged. Line COUNT is preserved either way, so the heading line-map below still lines up.
    text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines(keepends=True)
    frontmatter, body_start = _frontmatter(lines)
    relative = resolved.relative_to(root.resolve(strict=True)).as_posix()
    source_hash = hashlib.sha256(raw).hexdigest()
    document_ref = f"doc:v1:{_digest(relative)}"
    metadata = _metadata(frontmatter)
    tokens = MarkdownIt("commonmark").parse("".join(lines[body_start:]))
    headings: list[tuple[int, int, str]] = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.map:
            title = tokens[index + 1].content
            headings.append((token.map[0] + body_start, int(token.tag[1]), title))
    if headings and headings[0][0] > body_start:
        preamble = "".join(lines[body_start : headings[0][0]])
        if preamble.strip():
            headings.insert(0, (body_start, 0, ""))
    elif not headings and body_start < len(lines):
        headings.append((body_start, 0, ""))

    passages: list[Passage] = []
    stack: list[str] = []
    occurrences: dict[tuple[str, ...], int] = {}
    for index, (start, level, title) in enumerate(headings):
        if level:
            stack = stack[: level - 1]
            stack.append(title)
            heading_path = tuple(stack)
        else:
            stack = []
            heading_path = ()
        ordinal = occurrences.get(heading_path, 0)
        occurrences[heading_path] = ordinal + 1
        end = headings[index + 1][0] if index + 1 < len(headings) else len(lines)
        ref = f"chunk:v1:{_digest(relative, *heading_path, str(ordinal), '0')}"
        passages.append(
            Passage(
                ref,
                document_ref,
                relative,
                heading_path,
                start + 1,
                end,
                "".join(lines[start:end]),
                source_hash,
                metadata,
            )
        )
    return Document(document_ref, relative, source_hash, metadata, tuple(passages))
