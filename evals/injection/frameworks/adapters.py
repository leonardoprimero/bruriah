"""The two framework adapters: each one's typical agent retrieval-as-tool pattern, reduced to
`compare.FrameworkAdapter`'s contract. This module is the only place the optional
`frameworks-compare` dependency group (pinned `llama-index-core` and `langchain-core`) is
imported; everything comparable without a framework lives in `compare.py`.

Symmetric raw-text ingestion, on purpose: both adapters load each corpus file's exact text into
one framework Document with the file name as metadata, rather than going through each
framework's file loader. A markdown loader that parses front-matter differently would turn a
loader idiosyncrasy into a row difference; the comparison isolates the retriever-TOOL channel
-- what the serialized tool output carries -- so every row must ingest the identical text,
exactly as Bruriah's own indexer reads the identical bytes. What each framework's DEFAULT tool
serialization then does with the metadata is part of the measured architecture: LlamaIndex's
`RetrieverTool` serializes node content in `MetadataMode.LLM` (metadata, including the file
name, reaches the model); LangChain's `create_retriever_tool` formats `page_content` only (the
`source` metadata does not). Neither choice is a defect -- both are the framework's documented
default -- and the report never presents them as one.

Determinism, required by `compare.FrameworkAdapter`: files are ingested in sorted order;
LlamaIndex's `MockEmbedding` embeds every text to the same constant vector and LangChain's
`DeterministicFakeEmbedding` hashes the text to a seeded vector, so two runs over the same
corpus and task produce the identical `AdapterRun`. Retrieval `k` always covers every node, so
tie-breaking among constant-vector scores can drop nothing.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from compare import FRAMEWORK_CALLS_TO_REACH_CONTENT, AdapterRun  # noqa: E402

from langchain_core.documents import Document as LangChainDocument  # noqa: E402
from langchain_core.embeddings import DeterministicFakeEmbedding  # noqa: E402
from langchain_core.tools.retriever import create_retriever_tool  # noqa: E402
from langchain_core.vectorstores import InMemoryVectorStore  # noqa: E402
from llama_index.core import Document as LlamaIndexDocument  # noqa: E402
from llama_index.core import VectorStoreIndex  # noqa: E402
from llama_index.core.embeddings import MockEmbedding  # noqa: E402
from llama_index.core.tools import RetrieverTool  # noqa: E402

# One fake-embedding dimensionality for both adapters: the value is arbitrary (retrieval is
# degenerate by design, see `compare`'s module docstring), sharing it just removes one more
# asymmetry between rows.
_EMBED_DIM = 8

_TOOL_NAME = "search_project_memory"
_TOOL_DESCRIPTION = "Search the project's decision memory for passages relevant to the query."


def _corpus_texts(corpus_dir: Path) -> list[tuple[str, str]]:
    """(file name, exact file text) for every corpus file, in sorted order -- the shared
    ingestion both adapters build their Documents from."""
    return [
        (path.name, path.read_text(encoding="utf-8"))
        for path in sorted(corpus_dir.iterdir())
        if path.is_file()
    ]


@dataclass(frozen=True)
class _LlamaIndexAdapter:
    """LlamaIndex's typical agent retrieval-as-tool pattern: a `RetrieverTool` over a
    `VectorStoreIndex` retriever. The measured channel is `ToolOutput.content`, which
    `RetrieverTool.call` builds by concatenating every retrieved node's
    `get_content(MetadataMode.LLM)` -- retrieved text plus LLM-visible metadata, by design."""

    name: str = "llamaindex"
    calls_to_reach_content: int = FRAMEWORK_CALLS_TO_REACH_CONTENT

    def run(self, corpus_dir: Path, task: str) -> AdapterRun:
        documents = [
            LlamaIndexDocument(text=text, metadata={"file_name": file_name})
            for file_name, text in _corpus_texts(corpus_dir)
        ]
        index = VectorStoreIndex.from_documents(documents, embed_model=MockEmbedding(embed_dim=_EMBED_DIM))
        # Every node, not a plausible top-k: `MockEmbedding` scores every node identically, so
        # any smaller k would let tie-breaking (not architecture) decide which text the tool
        # returns -- and the executed-path invariant would rightly fail the run.
        node_count = len(index.docstore.docs)
        retriever = index.as_retriever(similarity_top_k=node_count)
        tool = RetrieverTool.from_defaults(
            retriever=retriever, name=_TOOL_NAME, description=_TOOL_DESCRIPTION
        )
        output = tool.call(task)
        retrieved_sources = frozenset(
            str(scored.node.metadata["file_name"]) for scored in output.raw_output
        )
        return AdapterRun(tool_output=str(output.content), retrieved_sources=retrieved_sources)


@dataclass(frozen=True)
class _LangChainAdapter:
    """LangChain's typical agent retrieval-as-tool pattern: `create_retriever_tool` over an
    `InMemoryVectorStore` retriever, at its defaults (`document_prompt=None`,
    `document_separator="\\n\\n"`). The measured channel is the tool's returned string -- the
    retrieved documents' `page_content` joined by the separator, which is what the resulting
    `ToolMessage` would carry to the model, by design."""

    name: str = "langchain"
    calls_to_reach_content: int = FRAMEWORK_CALLS_TO_REACH_CONTENT

    def run(self, corpus_dir: Path, task: str) -> AdapterRun:
        texts = _corpus_texts(corpus_dir)
        store = InMemoryVectorStore(embedding=DeterministicFakeEmbedding(size=_EMBED_DIM))
        store.add_documents(
            [
                LangChainDocument(page_content=text, metadata={"source": file_name})
                for file_name, text in texts
            ],
            ids=[file_name for file_name, _ in texts],
        )
        retriever = store.as_retriever(search_kwargs={"k": len(texts)})
        tool = create_retriever_tool(retriever, _TOOL_NAME, _TOOL_DESCRIPTION)
        tool_output = tool.invoke({"query": task})
        # The tool's string output deliberately carries no source metadata (its default
        # `document_prompt` formats `page_content` alone), so the retrieved set -- the executed
        # invariant's substrate -- comes from the same retriever the tool wraps, with the same
        # query and k. `DeterministicFakeEmbedding` makes the two invocations identical.
        retrieved_sources = frozenset(
            str(document.metadata["source"]) for document in retriever.invoke(task)
        )
        return AdapterRun(tool_output=str(tool_output), retrieved_sources=retrieved_sources)


LLAMAINDEX = _LlamaIndexAdapter()
LANGCHAIN = _LangChainAdapter()

# `compare.render_*` and the report keep this order; llamaindex first, matching the ODD doc's
# task order, is otherwise arbitrary.
ADAPTERS: tuple[_LlamaIndexAdapter, _LangChainAdapter] = (LLAMAINDEX, LANGCHAIN)
