# Slice 9B (unit 2 of 2): the bounded research planner. Composes the frozen, SSRF-safe `fetch.py`
# (Slice 9A) with the private atomic cache and content-free audit (`cache.py`/`audit.py`, unit 1)
# into the single decision design.md "Research" describes: "`research.py` plans bounded work.
# With an enabled provider and admitted URL, `fetch.py` performs HTTPS GET/HEAD; otherwise it
# returns vendor-neutral `web_search`, `fetch_public_url`, `inspect_capability`,
# `request_jurisdiction`, or `consult_professional` actions. No hidden chaining."
#
# Guarantees this module adds ON TOP of fetch.py's own SSRF defenses (module docstring in
# `fetch.py`), in the order `research()` evaluates them:
#   1. Network is only ever attempted when BOTH the caller's `InvestigationRequest.network_policy
#      == "public_https"` AND the resolved platform policy (`deps.network_enabled`, from
#      `platform.resolve_paths`) agree, AND a candidate `url` was actually supplied. Any other
#      case returns vendor-neutral `HostAction`s (never fetches) -- "Read-Only Informational
#      Boundary and Host Actions": needed work is expressed as host actions, never performed.
#   2. Destination admission is evaluated by THIS module BEFORE the cache is consulted and before
#      any fetch attempt: the same host[:port] allowlist `fetch.py` itself enforces (checked here
#      too so refusal never wastes a concurrency slot or a network attempt), plus an
#      `AccessPolicy` host+path-prefix deny list -- the documented, scoped-down substitute for
#      live robots.txt fetch-and-parse (see `AccessPolicy`'s own docstring for why fetching
#      robots.txt itself is out of scope). A request the policy disallows is refused, never
#      fetched and never answered from cache.
#
#      The cache used to be read first, because a hit costs no network. That is true and it is
#      beside the point: these lists do not exist to save bandwidth, they declare which
#      destinations this installation may surface at all. Consulting the cache first meant a host
#      or path the operator REVOKED kept being served, in full, until its TTL ran out. Both
#      checks are local, so running them first costs nothing.
#   3. A cache hit is served WITHOUT any network attempt or concurrency-slot use, provided the
#      entry was written under the policy version in force now. An expired entry is always a
#      genuine miss, never served as current (`cache.py`), and so is an entry whose
#      `policy_version` no longer matches: that field was recorded on every write from the start
#      and never consulted, so a cached answer outlived the pack that authorised it. Neither case
#      deletes anything here -- TTL and `prune_expired` own removal.
#   4. Cross-call concurrency throttling (`ConcurrencyLimiter`): a bounded counter shared across
#      possibly-simultaneous `research()` calls. A call that finds the limit already reached
#      degrades typed (`status="degraded"`) instead of blocking or launching an unbounded fetch.
#      Only an actual live `fetch.py` invocation acquires/releases it -- cache hits and refusals
#      never touch it.
#   5. `fetch.py` remains the authoritative enforcer of `request.budgets` for the single hop-chain
#      one call makes. What this module adds is not a competing budget but a POOL: an optional
#      `NetworkLedger`, owned by whoever owns the investigation, that narrows the declared budget
#      to what the investigation as a whole has left and is charged for what each call actually
#      spent -- including calls that were refused mid-flight.
#
#      Without it every ceiling reset per candidate URL, because `service._run_research` handed
#      each URL the same `request.budgets`. A request declaring `max_bytes=1_000_000` and
#      `max_network_requests=5` made five connections and pulled 2,500,000 bytes, with every
#      individual fetch inside its budget the whole time. `ledger=None` keeps exactly that
#      per-call behaviour, so a single-URL caller is unaffected.
#   6. A successful fetch is cached (`cache.py`: atomic write, permitted-minimum excerpt unless
#      `deps.resolve_reuse` marks it `permitted`) and appended to the content-free audit
#      (`audit.py`: host + closed classification only, never path/query/body/secret).
#   7. Typed and total: every enumerated failure returns a typed `ResearchOutcome`, and the public
#      `research()` entry wraps the whole pipeline in that typed catch PLUS a bare
#      `except Exception` backstop -- no bare traceback can ever escape `research()` (closes the
#      untyped-escape defect class this change has hit eight times in prior slices).
from __future__ import annotations

import hashlib
import json
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from .audit import AuditDecision, AuditRecord, append_audit
from .cache import CacheError, ReuseState, build_cache_entry, read_cache, write_cache_atomic
from .contracts import Budgets, EvidenceRecord, HostAction, InvestigationRequest
from .fetch import ConnectionFactory, FetchError, Resolver, default_connect, default_resolver, fetch

ResearchStatus = Literal[
    "disabled", "not_warranted", "cached", "fetched", "refused", "degraded", "error",
]


class ResearchError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ResearchOutcome:
    """Total, typed outcome of one `research()` call. Only `status in ("cached", "fetched")`
    carries evidence/excerpt; every other status is a safe refusal or degradation carrying zero
    body-derived content, with `host_actions` telling the host AI what it may do instead."""

    status: ResearchStatus
    code: str
    evidence: EvidenceRecord | None = None
    excerpt: str | None = None
    excerpt_only: bool = False
    host_actions: tuple[HostAction, ...] = ()
    cache_hit: bool = False


@dataclass(frozen=True)
class AccessPolicy:
    """A configured, static allow/deny policy at the host + path-prefix level -- the documented,
    intentionally scoped-down substitute for fetching and parsing `robots.txt` from each target
    site. Fetching `robots.txt` would itself be an additional live network request outside the
    caller's declared allowlist/budget for the URL actually requested, and a form of hidden
    chaining ("No hidden chaining"): a URL the caller never asked about, resolved by following a
    link `research()` discovered on its own. Scope: exact host match, path PREFIX match only (no
    wildcard/regex grammar, no `Allow`-override precedence, no crawl-delay/sitemap directives).
    A denied path is refused exactly like a policy-denied host -- never fetched anyway."""

    disallowed_path_prefixes: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def path_denied(self, host: str, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self.disallowed_path_prefixes.get(host, ()))


class ConcurrencyLimiter:
    """A bounded counter shared across possibly-simultaneous `research()` calls (e.g. multiple
    host threads investigating different candidate URLs at once) that limits how many LIVE
    `fetch.py` calls may be in flight at the same time. Backed by `threading.Semaphore` with a
    non-blocking `try_acquire` -- a call that finds the limit already reached degrades typed
    rather than blocking or launching an unbounded fetch."""

    def __init__(self, max_concurrent: int) -> None:
        if max_concurrent < 1:
            raise ResearchError("invalid_concurrency_limit")
        self._semaphore = threading.Semaphore(max_concurrent)

    def try_acquire(self) -> bool:
        return self._semaphore.acquire(blocking=False)

    def release(self) -> None:
        self._semaphore.release()


def build_proxy_connect(proxy_host: str, proxy_port: int) -> ConnectionFactory:
    """A transparent/forwarding-proxy connection factory for `fetch.py`'s `connect` seam: ignores
    the already-validated destination `ip`/`port` and dials `(proxy_host, proxy_port)` instead --
    the same redirection pattern `test_fetch.py`'s own loopback harness uses. Scope: transparent/
    forwarding proxies only. An HTTP `CONNECT`-tunnel proxy is explicitly OUT of scope: `connect`
    must return a plain, pre-TLS socket that `fetch.py` itself wraps with
    `ssl_context.wrap_socket(..., server_hostname=host)`; `CONNECT` tunnel negotiation is
    protocol-layer logic that would have to live inside the frozen `fetch.py`, not here."""

    def _connect(ip: str, port: int, timeout: float) -> socket.socket:
        try:
            return socket.create_connection((proxy_host, proxy_port), timeout=timeout)
        except OSError as error:
            raise FetchError("connect_failed") from error

    return _connect


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ResearchDeps:
    allowlist: frozenset[str]
    cache_dir: Path
    audit_path: Path
    network_enabled: bool
    concurrency: ConcurrencyLimiter
    access_policy: AccessPolicy = field(default_factory=AccessPolicy)
    resolve_reuse: Callable[[EvidenceRecord], ReuseState] | None = None
    policy_version: str = "unknown"
    default_ttl: timedelta = timedelta(hours=24)
    resolver: Resolver = default_resolver
    connect: ConnectionFactory = default_connect
    ssl_context: ssl.SSLContext | None = None
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = field(default_factory=lambda: _utcnow)


@dataclass
class NetworkLedger:
    """Requests, bytes and wall-clock POOLED ACROSS every `research()` call of one investigation.

    `Budgets` is declared once per `InvestigationRequest`, and `fetch.py` enforces it faithfully --
    for the single hop-chain of a single call. Nothing pooled it. `service._run_research` iterates
    the candidate URLs and hands each one the SAME `request.budgets`, so every ceiling reset per
    URL: a request declaring `max_bytes=1_000_000` and `max_network_requests=5` made five
    connections and pulled 2,500,000 bytes, and each individual fetch was within budget the whole
    time. `max_elapsed_ms` multiplied the same way. What the host declared as the cost of an
    investigation was really the cost of one URL inside it.

    Deliberately NOT a field on `ResearchDeps`. Deps are constructed once and live as long as the
    server; a ledger is per-investigation state and would leak spend between unrelated requests --
    and, being mutable and shared, would need a lock it has no business needing. It is created by
    the caller that owns the investigation and passed down.

    `deadline` is on the injected monotonic clock, the same one `fetch.py` measures against, so it
    stays testable and never reads wall-clock.
    """

    requests_remaining: int
    bytes_remaining: int
    deadline: float

    @classmethod
    def for_request(cls, budgets: Budgets, clock: Callable[[], float]) -> "NetworkLedger":
        return cls(
            requests_remaining=budgets.max_network_requests,
            bytes_remaining=budgets.max_bytes,
            deadline=clock() + (budgets.max_elapsed_ms / 1000),
        )

    def allowance(self, budgets: Budgets, clock: Callable[[], float]) -> Budgets | None:
        """The budget for the NEXT call: the declared one, narrowed to what the pool has left.

        `None` means the pool cannot fund another call. That is a separate answer from "narrowed
        to a small number", because `Budgets` has floors -- `max_bytes >= 1024`,
        `max_elapsed_ms >= 1` -- and a remainder under a floor cannot be expressed as a valid
        `Budgets` at all. Constructing one anyway (via `model_copy`, which skips validation) would
        smuggle an out-of-contract value into the frozen model rather than admit the pool is dry.
        """
        milliseconds_left = int((self.deadline - clock()) * 1000)
        if self.requests_remaining < 1 or self.bytes_remaining < 1024 or milliseconds_left < 1:
            return None
        return budgets.model_validate({
            **budgets.model_dump(),
            "max_network_requests": min(budgets.max_network_requests, self.requests_remaining),
            "max_bytes": min(budgets.max_bytes, self.bytes_remaining),
            "max_elapsed_ms": min(budgets.max_elapsed_ms, milliseconds_left),
        })

    def charge(self, requests: int, bytes_read: int) -> None:
        """Deduct what a call actually spent, whether it succeeded or was refused mid-flight.

        Charging only for success would make refusals free, and a hostile-but-allowlisted host
        would then be able to burn the pool indefinitely by always failing."""
        self.requests_remaining = max(self.requests_remaining - max(requests, 0), 0)
        self.bytes_remaining = max(self.bytes_remaining - max(bytes_read, 0), 0)


def _canonicalize(url: str) -> tuple[str, str, int, str]:
    """Parse `url` into (canonical_url, host, port, path_and_query) the same way `fetch.py`
    canonicalizes (scheme + host:port + path/query; userinfo dropped). Needed here -- BEFORE any
    `fetch.py` call -- so allowlist/access-policy admission and the cache key can be decided
    without a live fetch; `fetch.py` performs its own, authoritative re-canonicalization when it
    actually connects, so this is a non-authoritative echo used only for planning."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or 443
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    canonical = urlunsplit((parts.scheme, f"{host}:{port}", parts.path or "/", parts.query, ""))
    return canonical, host, port, path


def _request_id(request: InvestigationRequest) -> str:
    canonical = json.dumps(
        request.model_dump(mode="json", exclude={"cursor"}), sort_keys=True, separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _host_action_for(code: str, url: str | None) -> HostAction:
    if url is None:
        return HostAction(kind="web_search", reason=code, target=None)
    return HostAction(kind="fetch_public_url", reason=code, target=url)


def _record(
    *,
    deps: ResearchDeps,
    request_id: str,
    host: str | None,
    decision: ResearchStatus,
    code: str,
    bytes_transferred: int,
    started: float,
    evidence: EvidenceRecord | None = None,
    excerpt: str | None = None,
    excerpt_only: bool = False,
    host_actions: tuple[HostAction, ...] = (),
    cache_hit: bool = False,
) -> ResearchOutcome:
    elapsed_ms = max(int((deps.clock() - started) * 1000), 0)
    audit_decision: AuditDecision = decision  # ResearchStatus and AuditDecision share one vocabulary
    append_audit(
        deps.audit_path,
        AuditRecord(
            request_id=request_id, destination_host=host,
            destination_class="public_https" if host else "none",
            decision=audit_decision, code=code, bytes_transferred=bytes_transferred,
            elapsed_ms=elapsed_ms, timestamp=deps.now(),
        ),
    )
    return ResearchOutcome(
        status=decision, code=code, evidence=evidence, excerpt=excerpt, excerpt_only=excerpt_only,
        host_actions=host_actions, cache_hit=cache_hit,
    )


def _research_inner(
    request: InvestigationRequest, url: str | None, deps: ResearchDeps,
    ledger: NetworkLedger | None = None,
) -> ResearchOutcome:
    if not isinstance(request, InvestigationRequest):
        raise ResearchError("invalid_request_type")
    if not isinstance(deps, ResearchDeps):
        raise ResearchError("invalid_deps_type")
    request_id = _request_id(request)
    started = deps.clock()

    if request.network_policy != "public_https" or not deps.network_enabled:
        return _record(
            deps=deps, request_id=request_id, host=None, decision="disabled", code="network_disabled",
            bytes_transferred=0, started=started, host_actions=(_host_action_for("network_disabled", url),),
        )
    if url is None:
        return _record(
            deps=deps, request_id=request_id, host=None, decision="not_warranted", code="no_candidate_url",
            bytes_transferred=0, started=started, host_actions=(_host_action_for("no_candidate_url", None),),
        )
    if request.budgets.max_network_requests < 1:
        return _record(
            deps=deps, request_id=request_id, host=None, decision="not_warranted",
            code="network_budget_exhausted", bytes_transferred=0, started=started,
            host_actions=(_host_action_for("network_budget_exhausted", url),),
        )

    canonical, host, port, path = _canonicalize(url)

    # ADMISSION IS DECIDED BEFORE THE CACHE IS CONSULTED. The cache read used to come first, on
    # the reasoning that a hit costs no network -- true, and beside the point. The allowlist and
    # the access policy are not there to save bandwidth, they say WHICH DESTINATIONS THIS
    # INSTALLATION IS PERMITTED TO SURFACE. Answering from cache before asking meant a host or a
    # path removed from the policy kept being served, in full, for the whole TTL after the
    # operator revoked it. Measured: fetch once, empty the allowlist, ask again -- `status=cached`
    # with content. Same for a path the access policy denies.
    #
    # Both checks are local, so moving them ahead of the cache costs no network attempt and no
    # concurrency slot; the guarantee that a cache hit never touches either is unchanged.
    if host not in deps.allowlist and f"{host}:{port}" not in deps.allowlist:
        return _record(
            deps=deps, request_id=request_id, host=host, decision="refused", code="host_not_allowlisted",
            bytes_transferred=0, started=started, host_actions=(_host_action_for("host_not_allowlisted", url),),
        )
    if deps.access_policy.path_denied(host, path):
        return _record(
            deps=deps, request_id=request_id, host=host, decision="refused", code="access_restricted",
            bytes_transferred=0, started=started, host_actions=(_host_action_for("access_restricted", url),),
        )

    lookup = read_cache(deps.cache_dir, canonical, now=deps.now())
    if lookup.hit and lookup.entry is not None:
        entry = lookup.entry
        # The entry also has to have been written under the policy in force NOW. `policy_version`
        # was recorded on every write from the beginning and never consulted on read, so a cached
        # answer outlived the pack that authorised it: re-signing a research policy with different
        # source rules changed nothing about what was already stored. A stale-policy entry is
        # treated as a miss and refetched under the current rules, not served and not silently
        # deleted -- the TTL and `prune_expired` still own removal.
        if entry.policy_version == deps.policy_version:
            return _record(
                deps=deps, request_id=request_id, host=host, decision="cached", code="ok",
                bytes_transferred=len(entry.excerpt), started=started, evidence=entry.evidence,
                excerpt=entry.excerpt, excerpt_only=entry.excerpt_only, cache_hit=True,
            )
        return _fetch_under_current_policy(
            request, deps, request_id, started, canonical, host, url, ledger,
        )

    return _fetch_under_current_policy(
        request, deps, request_id, started, canonical, host, url, ledger,
    )


def _fetch_under_current_policy(
    request: InvestigationRequest, deps: ResearchDeps, request_id: str, started: float,
    canonical: str, host: str, url: str, ledger: NetworkLedger | None,
) -> ResearchOutcome:
    """The live leg: acquire a concurrency slot, fetch, cache, audit. Split out of
    `_research_inner` so the cache-miss path and the stale-policy path reach it by the same
    route -- a policy-stale entry must be refetched under the CURRENT rules, which is exactly a
    miss, not a second code path that could drift away from this one.

    A cache hit never gets here, which is why the ledger is charged only on this path: serving
    what is already on disk makes no request and reads no socket, so charging for it would let a
    repeated question exhaust an investigation's network budget without any network."""
    # Narrow the declared budget to what the investigation-wide pool has left, BEFORE taking a
    # concurrency slot -- a call the pool cannot fund should not occupy a slot another one could
    # have used.
    budgets = request.budgets
    if ledger is not None:
        allowance = ledger.allowance(budgets, deps.clock)
        if allowance is None:
            return _record(
                deps=deps, request_id=request_id, host=host, decision="not_warranted",
                code="network_budget_exhausted", bytes_transferred=0, started=started,
                host_actions=(_host_action_for("network_budget_exhausted", url),),
            )
        budgets = allowance

    if not deps.concurrency.try_acquire():
        return _record(
            deps=deps, request_id=request_id, host=host, decision="degraded",
            code="concurrency_limit_exceeded", bytes_transferred=0, started=started,
            host_actions=(_host_action_for("concurrency_limit_exceeded", url),),
        )
    try:
        result = fetch(
            canonical, "GET", budgets, network_enabled=True, allowlist=deps.allowlist,
            resolver=deps.resolver, connect=deps.connect, ssl_context=deps.ssl_context,
            clock=deps.clock, retrieved_at=deps.now(),
        )
    finally:
        deps.concurrency.release()

    # Charge before branching on the outcome. `fetch()` reports usage on every path, and a refused
    # or errored call still opened connections and read bytes -- billing only successes would make
    # failure the cheap way to drain the pool.
    if ledger is not None:
        ledger.charge(result.requests_made, result.bytes_read)

    if result.status != "ok" or result.evidence is None or result.body is None:
        decision: ResearchStatus = "error" if result.status == "error" else "refused"
        return _record(
            deps=deps, request_id=request_id, host=host, decision=decision, code=result.code,
            bytes_transferred=0, started=started, host_actions=(_host_action_for(result.code, url),),
        )

    evidence = result.evidence
    if deps.resolve_reuse is not None:
        evidence = evidence.model_copy(update={"reuse": deps.resolve_reuse(evidence)})

    entry = build_cache_entry(
        evidence, retrieved_at=deps.now(), ttl=deps.default_ttl, body=result.body,
        max_excerpt_chars=budgets.max_extracted_chars, policy_version=deps.policy_version,
    )
    write_cache_atomic(deps.cache_dir, canonical, entry)

    return _record(
        deps=deps, request_id=request_id, host=host, decision="fetched", code="ok",
        bytes_transferred=len(result.body), started=started, evidence=entry.evidence,
        excerpt=entry.excerpt, excerpt_only=entry.excerpt_only,
    )


def research(
    request: InvestigationRequest, url: str | None, deps: ResearchDeps,
    ledger: NetworkLedger | None = None,
) -> ResearchOutcome:
    """Decide whether live research for `url` is warranted under `request`/`deps`'s resolved
    network policy, serving a valid cache hit or driving `fetch.py` when warranted, and otherwise
    returning vendor-neutral `HostAction`s -- NEVER fetching outside those two paths. Never raises
    (module docstring #7): typed `ResearchError`/`CacheError` failures AND any unenumerated
    exception are converted to a typed `ResearchOutcome(status="error", ...)`.

    `ledger` pools `max_network_requests`/`max_bytes`/`max_elapsed_ms` across every call belonging
    to ONE investigation (see `NetworkLedger`). It defaults to `None`, which restores the previous
    per-call behaviour exactly -- so a caller investigating a single URL, and every existing test,
    is unaffected. `service._run_research` builds one per `investigate()` and passes it to all of
    them, because a budget the host declared once should not be granted once per candidate URL.
    """
    try:
        return _research_inner(request, url, deps, ledger)
    except (ResearchError, CacheError) as error:
        return ResearchOutcome(status="error", code=error.code, host_actions=(_host_action_for(error.code, url),))
    except Exception:  # Backstop (#7): no bare exception may ever escape `research()`.
        return ResearchOutcome(status="error", code="internal_error")


__all__ = [
    "AccessPolicy", "ConcurrencyLimiter", "NetworkLedger", "ResearchDeps", "ResearchError", "ResearchOutcome",
    "ResearchStatus", "build_proxy_connect", "research",
]
