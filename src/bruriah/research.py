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
#   2. A cache hit is served WITHOUT any network attempt or concurrency-slot use; an expired
#      entry is always a genuine miss, never served as current (`cache.py`).
#   3. Destination admission is evaluated by THIS module before any fetch attempt: the same
#      host[:port] allowlist `fetch.py` itself enforces (checked here too so refusal never wastes
#      a concurrency slot or a network attempt), plus an `AccessPolicy` host+path-prefix deny
#      list -- the documented, scoped-down substitute for live robots.txt fetch-and-parse (see
#      `AccessPolicy`'s own docstring for why fetching robots.txt itself is out of scope). A
#      request the policy disallows is refused, never fetched anyway.
#   4. Cross-call concurrency throttling (`ConcurrencyLimiter`): a bounded counter shared across
#      possibly-simultaneous `research()` calls. A call that finds the limit already reached
#      degrades typed (`status="degraded"`) instead of blocking or launching an unbounded fetch.
#      Only an actual live `fetch.py` invocation acquires/releases it -- cache hits and refusals
#      never touch it.
#   5. `request.budgets` (`max_network_requests`, `max_bytes`, `max_elapsed_ms`, ...) is forwarded
#      UNCHANGED to `fetch.py`, which is the authoritative enforcer of all of them for the single
#      hop-chain this call makes; this module adds no second, competing budget system.
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


def _research_inner(request: InvestigationRequest, url: str | None, deps: ResearchDeps) -> ResearchOutcome:
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

    lookup = read_cache(deps.cache_dir, canonical, now=deps.now())
    if lookup.hit and lookup.entry is not None:
        entry = lookup.entry
        return _record(
            deps=deps, request_id=request_id, host=host, decision="cached", code="ok",
            bytes_transferred=len(entry.excerpt), started=started, evidence=entry.evidence,
            excerpt=entry.excerpt, excerpt_only=entry.excerpt_only, cache_hit=True,
        )

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

    if not deps.concurrency.try_acquire():
        return _record(
            deps=deps, request_id=request_id, host=host, decision="degraded",
            code="concurrency_limit_exceeded", bytes_transferred=0, started=started,
            host_actions=(_host_action_for("concurrency_limit_exceeded", url),),
        )
    try:
        result = fetch(
            canonical, "GET", request.budgets, network_enabled=True, allowlist=deps.allowlist,
            resolver=deps.resolver, connect=deps.connect, ssl_context=deps.ssl_context,
            clock=deps.clock, retrieved_at=deps.now(),
        )
    finally:
        deps.concurrency.release()

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
        max_excerpt_chars=request.budgets.max_extracted_chars, policy_version=deps.policy_version,
    )
    write_cache_atomic(deps.cache_dir, canonical, entry)

    return _record(
        deps=deps, request_id=request_id, host=host, decision="fetched", code="ok",
        bytes_transferred=len(result.body), started=started, evidence=entry.evidence,
        excerpt=entry.excerpt, excerpt_only=entry.excerpt_only,
    )


def research(request: InvestigationRequest, url: str | None, deps: ResearchDeps) -> ResearchOutcome:
    """Decide whether live research for `url` is warranted under `request`/`deps`'s resolved
    network policy, serving a valid cache hit or driving `fetch.py` when warranted, and otherwise
    returning vendor-neutral `HostAction`s -- NEVER fetching outside those two paths. Never raises
    (module docstring #7): typed `ResearchError`/`CacheError` failures AND any unenumerated
    exception are converted to a typed `ResearchOutcome(status="error", ...)`.
    """
    try:
        return _research_inner(request, url, deps)
    except (ResearchError, CacheError) as error:
        return ResearchOutcome(status="error", code=error.code, host_actions=(_host_action_for(error.code, url),))
    except Exception:  # Backstop (#7): no bare exception may ever escape `research()`.
        return ResearchOutcome(status="error", code="internal_error")


__all__ = [
    "AccessPolicy", "ConcurrencyLimiter", "ResearchDeps", "ResearchError", "ResearchOutcome",
    "ResearchStatus", "build_proxy_connect", "research",
]
