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

    def _extract_list(field: str) -> tuple[str, ...]:
        raw = frontmatter.get(field, [])
        items = raw if isinstance(raw, list) else [raw]
        res: list[str] = []
        for it in items:
            if it is not None:
                for sub in str(it).replace(",", " ").split():
                    if sub.strip():
                        res.append(sub.strip().lower())
        return tuple(dict.fromkeys(res))

    commit_raw = frontmatter.get("commit")
    commit = str(commit_raw).strip().lower() if commit_raw is not None else None

    return SourceMetadata(
        provenance=tuple(dict.fromkeys(provenance)),
        provenance_urls=tuple(dict.fromkeys(urls)),
        status=str(status),
        verification_date=str(verified),
        supersedes=_extract_list("supersedes"),
        deprecates=_extract_list("deprecates"),
        amends=_extract_list("amends"),
        commit=commit,
    )


def _search_text(title: str, heading_path: tuple[str, ...], text: str) -> str:
    """The passage as the retrieval stages read it: the section under its ancestry.

    A passage holds its own section's lines and nothing more, so the headings it sits beneath are
    absent from it. A "Windows" section under `# Installation guide` / `## Prerequisites` is a
    section about installing on Windows in which the word "installation" never occurs, and BM25 can
    only score terms it was given, so the query the user actually asks cannot reach it. Embedding
    the same bare fragment loses the same context, in a way that is harder to see.

    The ancestry is therefore prefixed once here, at parse time, and stored BESIDE the text rather
    than inside it. Only this string is tokenized, embedded and language-sampled; `text` remains the
    exact section bytes that `read_evidence` slices by character offset, and a prefix there would
    silently move every offset a caller already holds.

    Two things are dropped where they would only repeat themselves: the section's own heading, which
    is already the first line of `text`, and the document title when it is the same H1 the heading
    path starts from. The blank line is a separator, so the last word of a heading and the first
    word of the body cannot fuse into one token."""
    own = heading_path[-1] if heading_path else None
    context: list[str] = []
    for item in (title, *heading_path[:-1]):
        if item and item != own and item not in context:
            context.append(item)
    return "\n".join([*context, "", text]) if context else text


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

    # The document's own name, for `_search_text` to put every passage under. Markdown carries no
    # title field this parser reads, so the first H1 is it -- that is what a reader treats as the
    # document's name, and it is the one heading a lower section is most likely to be missing. A
    # document without one falls back to its file name, which is the only other thing that is always
    # present and always about the document.
    # Named apart from the loops below, which both bind a `title` of their own: this one is the
    # DOCUMENT's, and a section heading overwriting it would silently empty the prefix.
    document_title = next(
        (heading for _, level, heading in headings if level == 1 and heading), Path(relative).stem
    )

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
        body = "".join(lines[start:end])
        passages.append(
            Passage(
                ref,
                document_ref,
                relative,
                heading_path,
                start + 1,
                end,
                body,
                _search_text(document_title, heading_path, body),
                source_hash,
                metadata,
            )
        )
    return Document(document_ref, relative, source_hash, metadata, tuple(passages))
