"""Runs a real embedding model against `index.py`'s pure build/promote pipeline.

This sits ABOVE both `index.py` and `platform.py` in the dependency graph on purpose: `platform.py`
already imports from `index.py` (`ActiveSnapshot`, `BuildConfig`, `IndexLifecycleError`,
`snapshot_active`), so a module that needs both `index.py`'s build pipeline and `platform.py`'s
`PlatformPaths`/`ensure_private_dirs`/`write_build_descriptor` cannot live inside either of them
without creating an import cycle. It previously lived in `cli.py` for the same reason `cli.py`
imports both -- but that made `cli.py` (the CLI adapter: argument parsing, process exit codes,
stdout/stderr formatting) a dependency of `watch.py` and `bootstrap.py`, which are infrastructure
that has nothing to do with argument parsing. This module is the fix: `run_index` and the real
`fastembed`-backed embedder factory it defaults to, with no adapter-layer code at all.

`cli.py` imports these names and re-exports them (`EmbedderFactory`, `_default_embedder_factory`,
`_embedding_fingerprint`, `run_index`) so existing imports of `bruriah.cli.run_index` and friends
keep working unchanged.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import sys
import uuid
import warnings
from array import array
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastembed import TextEmbedding

from ._cli.common import resolve_model_prefixes
from .corpus import CorpusPolicy
from .index import BuildConfig, BuildResult, Embedder, active_database, build_candidate, promote_candidate
from .platform import PlatformPaths, ensure_private_dirs, write_build_descriptor

EmbedderFactory = Callable[[str], tuple[Embedder, str, int]]


def _embedding_fingerprint(model: TextEmbedding) -> str:
    backend: Any = model.model
    description = backend.model_description
    pooling = {
        "OnnxTextEmbedding": "cls-normalized",
        "PooledEmbedding": "mean",
        "PooledNormalizedEmbedding": "mean-normalized",
    }.get(type(backend).__name__)
    source = description.sources.hf or description.sources.url
    model_dir = Path(backend._model_dir).resolve()
    relative_artifact = Path(description.model_file)
    artifact = model_dir / relative_artifact
    if (
        not pooling
        or not source
        or relative_artifact.is_absolute()
        or ".." in relative_artifact.parts
        or not artifact.is_file()
    ):
        raise ValueError("unsupported_embedding_runtime")
    return json.dumps(
        {
            "artifact": description.model_file,
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "pooling": pooling,
            "runtime": f"fastembed=={importlib.metadata.version('fastembed')}",
            "snapshot": model_dir.name,
            "source": source,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _default_embedder_factory(model_name: str) -> tuple[Embedder, str, int]:
    """Real fastembed model; tests inject a fake factory so the suite never loads real ONNX."""
    with warnings.catch_warnings():
        # fastembed announces on every construction that this model now pools by mean rather than
        # CLS, which lands in the middle of `index` and `ask --read` and reads, to someone running
        # the quickstart, like something went wrong. The change it reports is real and is already
        # handled harder than a printed line can manage: `_embedding_fingerprint` reads the
        # pooling off the backend class, REFUSES a runtime it cannot name, and writes the answer
        # into `embedding_fingerprint` -- which is part of what an index is validated against, so
        # a pooling change invalidates the index rather than scrolling past. That is pinned by
        # test_fastembed_fingerprint_binds_pooling_source_snapshot_and_artifact and by the pooling
        # case in the activation-metadata table.
        #
        # Matched by message, never by category: any OTHER warning fastembed raises has no such
        # backstop and must still reach the user.
        warnings.filterwarnings("ignore", message=".*mean pooling instead of CLS.*", category=UserWarning)
        model = TextEmbedding(model_name=model_name)
    fingerprint = _embedding_fingerprint(model)

    def embed(texts: list[str]) -> list[bytes]:
        return [array("f", vector).tobytes() for vector in model.embed(texts)]

    return embed, fingerprint, model.embedding_size


def run_index(
    paths: PlatformPaths,
    root: Path,
    policy_path: Path,
    *,
    model_name: str,
    embedder_factory: EmbedderFactory = _default_embedder_factory,
    query_prefix: str | None = None,
    passage_prefix: str | None = None,
) -> BuildResult:
    """Build+promote a candidate into the private `data_dir` (never `cerebro.db`); runs
    `ensure_private_dirs` first, closing the carried Slice 8A-1 descriptor-on-missing-dir WARNING.

    The build reuses the active snapshot's rows wherever the corpus has not moved. `build_candidate`
    has been able to do that since it was written, but nothing ever handed it a `previous`, so every
    reindex re-embedded the whole corpus to arrive at the same vectors -- the cost of a one-document
    edit was the cost of a first build. What makes reuse safe is not this call: `_compatible`
    refuses a snapshot built under a different model, parser or schema, and `_validate_candidate`
    re-verifies every reused row before the candidate is promoted."""
    ensure_private_dirs(paths)
    policy = CorpusPolicy.load(policy_path)
    embed, fingerprint, dimensions = embedder_factory(model_name)
    revision = json.loads(fingerprint)["snapshot"]
    resolved_query_prefix, resolved_passage_prefix = resolve_model_prefixes(
        model_name, query_prefix=query_prefix, passage_prefix=passage_prefix
    )
    # `service_version` is NOT `bruriah.__version__` and must not be wired to it. It belongs to the
    # same family as `parser_version="corpus-v2"` and `ranking_config="rrf-v1"`: a symbolic marker of
    # the snapshot contract, carried into the `expected` metadata that `promote_candidate` validates
    # (`index.py`). Binding it to the package version would put every release into the index
    # identity, so a patch bump would refuse the user's existing snapshot and force a full re-embed
    # of their corpus. It moves when the snapshot contract moves, and it has not moved.
    config = BuildConfig(
        root=root,
        policy_path=policy_path,
        schema_version=1,
        parser_version="corpus-v2",
        service_version="0.1.0",
        mcp_range=">=1.28.1,<2",
        embedding_model=model_name,
        embedding_revision=revision,
        embedding_dimensions=dimensions,
        embedding_fingerprint=fingerprint,
        ranking_config="rrf-v1",
        query_prefix=resolved_query_prefix,
        passage_prefix=resolved_passage_prefix,
    )
    pointer = paths.data_dir / "active.json"
    candidate_path = paths.data_dir / f"candidate-{uuid.uuid4().hex}.sqlite3"
    result = build_candidate(config, candidate_path, policy, embed, previous=active_database(pointer))
    activation = promote_candidate(candidate_path, pointer, config, policy)
    if activation.retention_discarded:
        print(
            "Note: the previous index in this data directory was built under a different "
            "configuration -- an edited policy, or another project -- so it was not kept as a "
            "rollback target. The new index is active. Give each project its own --data-dir to "
            "keep their generations separate.",
            file=sys.stderr,
        )
    write_build_descriptor(paths, config)
    return result
