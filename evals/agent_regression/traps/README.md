# Regression traps

A trap is one task an agent is asked to do on a pinned public repository, where the obvious way
to do it reintroduces an alternative that project already rejected. The benchmark runs the task
with and without project memory and counts how often the rejected alternative comes back.

The prompt reads like a real ticket: it tempts the alternative without naming it, the rejection,
or the project's past. A second reader other than the author confirms that before the trap is
committed. The loader refuses a prompt that names the alternative or Bruriah.

## Layout

```
<trap_id>/
  trap.yaml
  detect.py
  fixtures/regressed/diff.patch   + the files the detector reads, in their regressed state
  fixtures/clean/diff.patch       + the same files after an on-task edit that does not regress
```

`trap.yaml` holds exactly these keys (the loader rejects unknown ones): `trap_id`, `source`,
`repository`, `commit`, `prompt`, `rejected_alternative`, `decision_ref`, `turn_budget`,
`time_budget_seconds`, `second_reader`, `second_reader_date`, and optionally `citation_cues` and
`decision_documented`.

`decision_documented` is a YAML bool, true by default. A trap that sets it to false is an
undocumented-decision control: its decision is real in the code, but no history records a
rationale, so retrieval cannot surface it. The report keeps its runs out of every headline metric
and summarizes them in a separate control section. The manifest states the evidence in a comment
above the key. `egui-image-formats` is the only such trap.

## Citation cues

A trap's citation cues are the identifiers that, in an agent's final message, mean it cited the
decision. The loader derives them from `decision_ref`: a full commit sha gives its 7-character
abbreviation (matching accepts any prefix of the sha at least that long), and
`github:<owner>/<repo>#<n>` gives `#<n>` and `<owner>/<repo>#<n>`; any other ref is cited as
written. `citation_cues` adds identifiers the ref does not carry, such as the pull request that
merged a decision commit or a decision document path. Each one must be a string of at least 3
characters and not a bare number (write `#2863`, not `2863`); duplicates of a derived cue are
dropped. Every added cue names its local evidence in a comment above it.

## Detector contract

`detect.py` defines `detect(tree: Path, diff: str) -> Detection`, imported from
`agent_regression.detection`. `tree` is the edited checkout; `diff` is `git diff HEAD` followed by
one `?? <path>` line per untracked file.

- **Pure.** No process, network, clock, or randomness. Read files under `tree`, inspect `diff`.
- **Regressed.** `regressed=True` when the rejected alternative is present in the tree. Comment
  lines never fire; prefer `ast` for Python and `tomllib` for TOML.
- **Evidence.** Every fired detection names what fired, as `"<path relative to tree>: <what>"`,
  so a reviewer can open it.
- **Completed.** `completed=True` iff the diff adds at least one non-blank line to one of the
  trap's target paths, or lists one as untracked. An empty diff never completes: avoiding the
  regression by doing nothing is not a win. Files outside the targets (such as an appended
  `CLAUDE.md`) never count.

## Adding a trap

1. Pick a rejection the pinned corpus documents, and note its decision ref (a full commit sha or
   `github:<owner>/<repo>#<n>`).
2. Write the prompt as a ticket. Keep it under 600 characters and free of the alternative's
   name, "bruriah", and words about the past ("rejected", "decision", "previously", ...).
3. Get a second reader to confirm the prompt does not leak the answer; record their name and date.
4. Write `detect.py`, then both fixtures: 2 to 12 files each, under 64 KB, short excerpts of the
   real files, each with a non-empty `diff.patch`. The regressed fixture must fire and complete;
   the clean one must complete without firing.
5. Add the trap to `EXPECTED` in `tests/test_agent_regression_traps.py` and run
   `uv run pytest -q tests/test_agent_regression_traps.py`.

## Committed traps

| id | source | decision ref | rejected alternative |
|---|---|---|---|
| `own-fastmcp` | own-history | `395962e7c96fd6ce1d3fb8b26afb15162c5d5913` | FastMCP |
| `own-lenient-schemas` | own-history | `395962e7c96fd6ce1d3fb8b26afb15162c5d5913` | argument models that ignore unknown fields |
| `own-rrf-k` | own-history | `73355460095703f7f219b3461a8b2498065843e9` | a lower RRF_K without a measurement |
| `own-ann-index` | own-history | `4dc37e8ab9c5d8e07ec72d87734346ea945e215a` | sqlite-vec |
| `lc-workmanager-required` | leakcanary | `940e0f30e07c1ee2a709554369bc6e48f30e02ff` | WorkManager as a required dependency |
| `lc-androidx-bump` | leakcanary | `940e0f30e07c1ee2a709554369bc6e48f30e02ff` | raising the AndroidX versions pinned on purpose |
| `lc-toast-removal` | leakcanary | `github:square/leakcanary#844` | removing the toast |
| `egui-image-formats` | egui | `github:emilk/egui#4489` | enabling the image crate's other formats in eframe |
| `egui-winit-default-features` | egui | `be9f363c5373447b8e44036f10cae115a8e7b32c` | winit's default features |
| `egui-android-activity` | egui | `89e42884fcc38f304a96134ce47bb8441208a2e2` | android-activity |
| `egui-datepicker-chrono` | egui | `a12d18d9bdf79afcb669908d1c6119b1816f440c` | chrono |
| `egui-wgpu-vulkan` | egui | `github:emilk/egui#7342` | hardcoding the Vulkan backend in eframe's wgpu features |

Pins: bruriah at `1d36bc781bd830dcf277277967a5aab81e9dcdd9`, square/leakcanary at
`0f7dbab17e2a9f6f310f7b8be214e029bfc502e3`, emilk/egui at
`5d3e958ecfd3468a460c57094ebaeca6e3c4f325`. LeakCanary has three traps, one below the
four-per-source target; the shortfall is declared, not padded.

## Traps whose rationale is also visible in the working tree

In six of the twelve traps the agent can read the reason for the rejection without any project
memory, because a comment at the edit site already states it:

- `lc-androidx-bump`: the notes next to each pinned entry in LeakCanary's `gradle/libs.versions.toml`.
- `egui-android-activity`: the comment block above the Android features in eframe's and egui-winit's `Cargo.toml`.
- `own-fastmcp` and `own-lenient-schemas`: the header comment of `src/bruriah/mcp_server.py`.
- `own-rrf-k`: the block above `RRF_K` in `src/bruriah/ranking.py`.
- `own-ann-index`: the dependency notes in `pyproject.toml`.

Every own-history trap is in this group: this repository carries its decisions in code comments,
so on its own history the benchmark measures what an in-tree comment achieves before it measures
what project memory adds. The six traps stay in the set as that control, and the report stratifies
them rather than pooling them with the six whose rationale lives only in the corpus
(`lc-workmanager-required`, `lc-toast-removal`, `egui-image-formats`, `egui-winit-default-features`,
`egui-datepicker-chrono`, `egui-wgpu-vulkan`).
