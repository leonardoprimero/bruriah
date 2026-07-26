"""How does retrieval behave as the corpus grows?

The lexical leg is a bounded pure-Python BM25 scan, not FTS5, and every query loads every passage.
That is linear by construction. Linear is fine until it is not, and nobody has ever measured where
that point is -- so the README has no scale table and a reader cannot tell whether this works for
their repository. This produces the numbers rather than an opinion about them.
"""
import statistics, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1] + "/src")
from bruriah import cli
from bruriah.retrieval import search

OUT = Path(sys.argv[2])   # a scratch directory; everything written here is disposable
WORDS = ("deployment staging migration rollback schema index retrieval provenance signature "
         "digest pointer atomic lock snapshot embedding policy corpus evidence authority "
         "freshness abstention dispatch envelope candidate promotion").split()

def corpus(root: Path, documents: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for n in range(documents):
        filler = " ".join(WORDS[(n + i) % len(WORDS)] for i in range(120))
        (root / f"note-{n:06d}.md").write_text(
            f"# Decision {n}\n\nWe chose option {n % 7} over the alternative.\n{filler}\n"
            f"\n## Rationale {n}\n\n{filler}\n", encoding="utf-8")

QUERIES = ["why did we choose the staging rollback policy",
           "what is the provenance of the atomic pointer snapshot",
           "which embedding policy governs corpus freshness"]

print(f"{'documents':>11} {'passages':>9} {'index':>10} {'query p50':>13} {'p95':>9}")
print("-" * 58)
for documents in (100, 500, 2000, 8000):
    root = OUT / f"c{documents}"
    corpus(root / "notes", documents)
    policy = OUT / f"p{documents}.yaml"
    policy.write_text("version: 1\ninclude: ['**']\nexclude: []\n")
    data, config = OUT / f"d{documents}", OUT / f"g{documents}"
    started = time.perf_counter()
    code = cli.bruriah_main(["index", "--corpus-root", str(root), "--policy", str(policy),
                             "--data-dir", str(data), "--config-dir", str(config)])
    index_seconds = time.perf_counter() - started
    if code != 0:
        print(f"{documents:>11}  INDEXING FAILED"); continue
    import os
    deps = cli.build_serve_deps(cli.resolve_paths(
        env=dict(os.environ, BRURIAH_DATA_DIR=str(data), BRURIAH_CONFIG_DIR=str(config))))
    passages = deps.snapshot.database.execute("SELECT count(*) FROM passages").fetchone()[0]
    samples = []
    for _ in range(3):
        for query in QUERIES:
            begin = time.perf_counter()
            search(deps.snapshot, query, embed_query=deps.embed_query)
            samples.append((time.perf_counter() - begin) * 1000)
    samples.sort()
    p50 = statistics.median(samples)
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    print(f"{documents:>11} {passages:>9} {index_seconds:>9.1f}s {p50:>12.0f}ms {p95:>7.0f}ms")
