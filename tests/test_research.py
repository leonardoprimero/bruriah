# Slice 9B real-pipeline suite for the bounded research planner. The "ON path" tests drive the
# REAL `fetch.py` (constraint 1 discipline, same technique as `test_fetch.py`): a real local TLS
# loopback server (`127.0.0.1:0`, an OS-assigned port) plus the injected `resolver`/`connect` seam
# -- the resolver returns a real, well-known public IP (never actually connected to) so SSRF
# validation runs honestly, and `connect` redirects the actual TCP connection to the local server.
# No test in this file ever makes a real external network connection.
from __future__ import annotations

import os
import socket
import ssl
import stat
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bruriah.audit import read_audit_records
from bruriah.cache import cache_key, read_cache
from bruriah.contracts import Budgets, InvestigationRequest
from bruriah.research import (
    AccessPolicy, ConcurrencyLimiter, ResearchDeps, ResearchOutcome, build_proxy_connect, research,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "fetch"
_CERT = _FIXTURES / "testcert.pem"
_KEY = _FIXTURES / "testkey.pem"
_HOST = "cerebro-test.local"
_PUBLIC_IP = "93.184.216.34"  # example.com; never actually connected to -- see module docstring.
Responder = Callable[[bytes], tuple[int, dict[str, str], bytes]]


class _LocalTlsServer:
    """A minimal loopback HTTPS server, mirroring `test_fetch.py`'s own harness. Each accepted
    connection's raw request bytes are handed to `responder`, which returns
    (status, headers, body); `Content-Length` is added automatically."""

    def __init__(self, responder: Responder) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(_CERT), str(_KEY))
        self._context = context
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self._socket.settimeout(0.2)
        self.port = self._socket.getsockname()[1]
        self._responder = responder
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                raw_conn, _addr = self._socket.accept()
            except socket.timeout:
                continue
            try:
                tls_conn = self._context.wrap_socket(raw_conn, server_side=True)
            except (ssl.SSLError, OSError):
                continue
            try:
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = tls_conn.recv(4096)
                    if not chunk:
                        break
                    request += chunk
                status, headers, body = self._responder(request)
                head = f"HTTP/1.1 {status} status\r\n".encode()
                for key, value in headers.items():
                    head += f"{key}: {value}\r\n".encode()
                if "Content-Length" not in headers:
                    head += f"Content-Length: {len(body)}\r\n".encode()
                tls_conn.sendall(head + b"\r\n" + body)
            except (ssl.SSLError, OSError):
                pass
            finally:
                tls_conn.close()

    def close(self) -> None:
        self._stop = True
        self._thread.join(timeout=2)
        self._socket.close()


def _redirect_connect(server: "_LocalTlsServer") -> Callable[[str, int, float], socket.socket]:
    def _connect(ip: str, port: int, timeout: float) -> socket.socket:
        return socket.create_connection(("127.0.0.1", server.port), timeout=timeout)

    return _connect


def _trusting_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(_CERT))


def _url(server: "_LocalTlsServer", path: str = "/page") -> str:
    return f"https://{_HOST}:{server.port}{path}"


def _allowlist(server: "_LocalTlsServer") -> frozenset[str]:
    return frozenset({f"{_HOST}:{server.port}"})


def _fake_public_resolver(_host: str) -> list[str]:
    return [_PUBLIC_IP]


def _request(**overrides: object) -> InvestigationRequest:
    payload: dict[str, object] = {
        "task": "Find the current published guidance for this integration.",
        "network_policy": "public_https", "budgets": Budgets(max_network_requests=5),
    }
    payload.update(overrides)
    return InvestigationRequest(**payload)


class _Clock:
    """An injectable, mutable `now`/`clock` pair for deterministic TTL and timing tests."""

    def __init__(self, start: datetime) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.current.timestamp()

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _deps(tmp_path: Path, server: "_LocalTlsServer", clock: "_Clock", **overrides: object) -> ResearchDeps:
    base: dict[str, object] = dict(
        allowlist=_allowlist(server), cache_dir=tmp_path / "cache", audit_path=tmp_path / "audit" / "research.jsonl",
        network_enabled=True, concurrency=ConcurrencyLimiter(4), resolver=_fake_public_resolver,
        connect=_redirect_connect(server), ssl_context=_trusting_ssl_context(),
        clock=clock.monotonic, now=clock.now,
    )
    base.update(overrides)
    return ResearchDeps(**base)


def _ok_responder(body: bytes = b"Real page content from the loopback server.") -> Responder:
    def _responder(_request: bytes) -> tuple[int, dict[str, str], bytes]:
        return 200, {"Content-Type": "text/plain"}, body

    return _responder


# --- Network off / not warranted -- HostActions only, never a fetch -----------------------------


def test_request_network_policy_off_returns_host_action_without_connecting(tmp_path: Path) -> None:
    server = _LocalTlsServer(_ok_responder())
    try:
        clock = _Clock(datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc))
        deps = _deps(tmp_path, server, clock, connect=lambda *_a: (_ for _ in ()).throw(
            AssertionError("must never connect when request.network_policy == off"),
        ))
        outcome = research(_request(network_policy="off"), _url(server), deps)
        assert outcome.status == "disabled"
        assert outcome.code == "network_disabled"
        assert outcome.evidence is None
        assert outcome.host_actions and outcome.host_actions[0].kind == "fetch_public_url"
        assert read_audit_records(deps.audit_path)[0].decision == "disabled"
    finally:
        server.close()


def test_platform_network_disabled_returns_host_action_without_connecting(tmp_path: Path) -> None:
    server = _LocalTlsServer(_ok_responder())
    try:
        clock = _Clock(datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc))
        deps = _deps(tmp_path, server, clock, network_enabled=False, connect=lambda *_a: (_ for _ in ()).throw(
            AssertionError("must never connect when platform network is disabled"),
        ))
        outcome = research(_request(), _url(server), deps)
        assert outcome.status == "disabled"
        assert outcome.code == "network_disabled"
    finally:
        server.close()


def test_no_candidate_url_returns_web_search_host_action(tmp_path: Path) -> None:
    server = _LocalTlsServer(_ok_responder())
    try:
        clock = _Clock(datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc))
        deps = _deps(tmp_path, server, clock)
        outcome = research(_request(), None, deps)
        assert outcome.status == "not_warranted"
        assert outcome.code == "no_candidate_url"
        assert outcome.host_actions[0].kind == "web_search"
        assert outcome.host_actions[0].target is None
    finally:
        server.close()


def test_zero_network_request_budget_is_not_warranted(tmp_path: Path) -> None:
    server = _LocalTlsServer(_ok_responder())
    try:
        clock = _Clock(datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc))
        deps = _deps(tmp_path, server, clock)
        outcome = research(_request(budgets=Budgets(max_network_requests=0)), _url(server), deps)
        assert outcome.status == "not_warranted"
        assert outcome.code == "network_budget_exhausted"
    finally:
        server.close()


# --- Policy-disallowed URL refused, never fetched ------------------------------------------------


def test_host_not_allowlisted_is_refused_without_connecting(tmp_path: Path) -> None:
    server = _LocalTlsServer(_ok_responder())
    try:
        clock = _Clock(datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc))
        deps = _deps(
            tmp_path, server, clock, allowlist=frozenset({"not-this-host.example"}),
            connect=lambda *_a: (_ for _ in ()).throw(AssertionError("must not connect")),
        )
        outcome = research(_request(), _url(server), deps)
        assert outcome.status == "refused"
        assert outcome.code == "host_not_allowlisted"
        assert outcome.host_actions[0].target == _url(server)
    finally:
        server.close()


def test_access_policy_denied_path_is_refused_without_connecting(tmp_path: Path) -> None:
    server = _LocalTlsServer(_ok_responder())
    try:
        clock = _Clock(datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc))
        # `AccessPolicy` is keyed by the bare hostname `_canonicalize` extracts -- never host:port.
        policy = AccessPolicy(disallowed_path_prefixes={_HOST: ("/private",)})
        deps = _deps(
            tmp_path, server, clock, access_policy=policy,
            connect=lambda *_a: (_ for _ in ()).throw(AssertionError("must not connect")),
        )
        outcome = research(_request(), _url(server, "/private/account"), deps)
        assert outcome.status == "refused"
        assert outcome.code == "access_restricted"
    finally:
        server.close()


def test_access_policy_allows_undenied_paths_on_the_same_host() -> None:
    policy = AccessPolicy(disallowed_path_prefixes={"example.test": ("/admin",)})
    assert not policy.path_denied("example.test", "/public/page")
    assert policy.path_denied("example.test", "/admin/settings")


# --- Real end-to-end ON path: fetch -> cache -> audit --------------------------------------------


def test_research_fetches_via_real_pipeline_and_caches_and_audits(tmp_path: Path) -> None:
    body = b"Real captured content from the local TLS loopback server."
    server = _LocalTlsServer(_ok_responder(body))
    try:
        clock = _Clock(datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc))
        deps = _deps(tmp_path, server, clock)
        outcome = research(_request(), _url(server), deps)

        assert outcome.status == "fetched"
        assert outcome.code == "ok"
        assert not outcome.cache_hit
        assert outcome.evidence is not None
        assert outcome.evidence.kind == "captured_live"
        assert outcome.evidence.digest.startswith("sha256:")
        # fetch.py always reports license=reuse="unknown" (no pack context) -> excerpt-only default.
        assert outcome.excerpt_only
        assert outcome.excerpt == body.decode()[: len(outcome.excerpt)]

        cache_path = deps.cache_dir / f"{cache_key(_url(server))}.json"
        assert cache_path.is_file()
        assert stat.S_IMODE(os.stat(cache_path).st_mode) == 0o600

        records = read_audit_records(deps.audit_path)
        assert len(records) == 1
        assert records[0].decision == "fetched"
        assert records[0].destination_host == _HOST
        assert records[0].destination_class == "public_https"
    finally:
        server.close()


def test_second_call_within_ttl_serves_from_cache_without_refetching(tmp_path: Path) -> None:
    connect_count = {"n": 0}

    def _counting_connect(ip: str, port: int, timeout: float) -> socket.socket:
        connect_count["n"] += 1
        return socket.create_connection(("127.0.0.1", server.port), timeout=timeout)

    server = _LocalTlsServer(_ok_responder())
    try:
        clock = _Clock(datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc))
        deps = _deps(tmp_path, server, clock, connect=_counting_connect, default_ttl=timedelta(hours=1))

        first = research(_request(), _url(server), deps)
        assert first.status == "fetched" and connect_count["n"] == 1

        clock.advance(timedelta(minutes=30))
        second = research(_request(), _url(server), deps)
        assert second.status == "cached"
        assert second.cache_hit
        assert second.evidence == first.evidence
        assert connect_count["n"] == 1  # no second connection was ever attempted

        records = read_audit_records(deps.audit_path)
        assert [record.decision for record in records] == ["fetched", "cached"]
    finally:
        server.close()


def test_cache_expires_after_ttl_and_genuinely_refetches(tmp_path: Path) -> None:
    connect_count = {"n": 0}

    def _counting_connect(ip: str, port: int, timeout: float) -> socket.socket:
        connect_count["n"] += 1
        return socket.create_connection(("127.0.0.1", server.port), timeout=timeout)

    server = _LocalTlsServer(_ok_responder())
    try:
        clock = _Clock(datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc))
        deps = _deps(tmp_path, server, clock, connect=_counting_connect, default_ttl=timedelta(minutes=10))

        first = research(_request(), _url(server), deps)
        assert first.status == "fetched"

        clock.advance(timedelta(minutes=11))
        lookup = read_cache(deps.cache_dir, _url(server), now=clock.now())
        assert not lookup.hit and lookup.expired  # the raw cache layer itself confirms expiry

        second = research(_request(), _url(server), deps)
        assert second.status == "fetched"  # a genuine re-fetch, never a stale cache hit
        assert not second.cache_hit
        assert connect_count["n"] == 2
    finally:
        server.close()


# --- Reuse-gated body discard, exercised through the real pipeline ------------------------------


def test_prohibited_reuse_discards_body_to_metadata_and_bounded_excerpt(tmp_path: Path) -> None:
    body = b"This entire body must never be persisted in full. " * 20
    server = _LocalTlsServer(_ok_responder(body))
    try:
        clock = _Clock(datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc))
        deps = _deps(tmp_path, server, clock, resolve_reuse=lambda _evidence: "prohibited")
        outcome = research(_request(), _url(server), deps)

        assert outcome.status == "fetched"
        assert outcome.excerpt_only
        assert len(outcome.excerpt) <= 280
        assert outcome.evidence is not None
        assert outcome.evidence.reuse == "prohibited"

        cache_path = deps.cache_dir / f"{cache_key(_url(server))}.json"
        raw = cache_path.read_text(encoding="utf-8")
        assert body.decode()[281:] not in raw  # nothing beyond the excerpt cap reaches disk
    finally:
        server.close()


def test_permitted_reuse_via_real_pipeline_stores_full_body(tmp_path: Path) -> None:
    body = b"Short permitted content."
    server = _LocalTlsServer(_ok_responder(body))
    try:
        clock = _Clock(datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc))
        deps = _deps(tmp_path, server, clock, resolve_reuse=lambda _evidence: "permitted")
        outcome = research(_request(), _url(server), deps)
        assert not outcome.excerpt_only
        assert outcome.excerpt == body.decode()
    finally:
        server.close()


# --- Content-free audit: no query, body, or secret ever reaches the audit trail ------------------


def test_audit_contains_no_query_body_or_secret(tmp_path: Path) -> None:
    secret_body_marker = b"TOTALLY_SECRET_BODY_MARKER_998877"
    secret_token = "SUPERSECRET-TOKEN-123456"
    server = _LocalTlsServer(_ok_responder(secret_body_marker))
    try:
        clock = _Clock(datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc))
        deps = _deps(tmp_path, server, clock)
        url = _url(server, f"/private/lookup?token={secret_token}")
        outcome = research(_request(), url, deps)
        assert outcome.status == "fetched"

        raw_audit = deps.audit_path.read_text(encoding="utf-8")
        assert secret_token not in raw_audit
        assert "TOTALLY_SECRET_BODY_MARKER" not in raw_audit
        assert "/private/lookup" not in raw_audit  # only the host is recorded, never the path/query
        assert _HOST in raw_audit
    finally:
        server.close()


# --- Concurrency throttling: real dual-thread proof through the real pipeline --------------------


def test_concurrency_limit_degrades_second_call_without_fetching_real_threads(tmp_path: Path) -> None:
    entered_server = threading.Event()
    release_response = threading.Event()

    def _slow_responder(_request: bytes) -> tuple[int, dict[str, str], bytes]:
        entered_server.set()
        release_response.wait(timeout=5)
        return 200, {"Content-Type": "text/plain"}, b"slow but real content"

    server = _LocalTlsServer(_slow_responder)
    try:
        clock = _Clock(datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc))
        deps = _deps(tmp_path, server, clock, concurrency=ConcurrencyLimiter(1))
        results: dict[str, ResearchOutcome] = {}

        def _run_first() -> None:
            results["first"] = research(_request(), _url(server), deps)

        first_thread = threading.Thread(target=_run_first)
        first_thread.start()
        assert entered_server.wait(timeout=5)  # the first call now holds the concurrency slot

        second = research(_request(), _url(server, "/other"), deps)

        release_response.set()
        first_thread.join(timeout=5)

        assert second.status == "degraded"
        assert second.code == "concurrency_limit_exceeded"
        assert results["first"].status == "fetched"
    finally:
        server.close()


def test_concurrency_limiter_direct_acquire_release() -> None:
    limiter = ConcurrencyLimiter(1)
    assert limiter.try_acquire()
    assert not limiter.try_acquire()  # already exhausted
    limiter.release()
    assert limiter.try_acquire()  # released slot is available again


# --- Fetch-layer failures surface as typed refusals, never bare exceptions -----------------------


def test_disallowed_content_type_surfaces_as_typed_refused(tmp_path: Path) -> None:
    def _bad_mime_responder(_request: bytes) -> tuple[int, dict[str, str], bytes]:
        return 200, {"Content-Type": "application/octet-stream"}, b"binary"

    server = _LocalTlsServer(_bad_mime_responder)
    try:
        clock = _Clock(datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc))
        deps = _deps(tmp_path, server, clock)
        outcome = research(_request(), _url(server), deps)
        assert outcome.status == "refused"
        assert outcome.code == "content_type_not_allowed"
        assert outcome.evidence is None
        assert not (deps.cache_dir / f"{cache_key(_url(server))}.json").exists()
    finally:
        server.close()


# --- Typed-total backstop: no bare exception ever escapes research() ----------------------------


def test_unexpected_exception_is_converted_to_typed_error_not_raised(tmp_path: Path) -> None:
    server = _LocalTlsServer(_ok_responder())
    try:
        def _exploding_now() -> datetime:
            raise RuntimeError("unexpected failure unrelated to any typed ResearchError code")

        clock = _Clock(datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc))
        deps = _deps(tmp_path, server, clock, now=_exploding_now)
        outcome = research(_request(), _url(server), deps)
        assert outcome.status == "error"
        assert outcome.code == "internal_error"
    finally:
        server.close()


def test_invalid_request_type_is_typed_not_raised(tmp_path: Path) -> None:
    server = _LocalTlsServer(_ok_responder())
    try:
        clock = _Clock(datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc))
        deps = _deps(tmp_path, server, clock)
        outcome = research({"task": "not a real request"}, _url(server), deps)  # type: ignore[arg-type]
        assert outcome.status == "error"
        assert outcome.code == "invalid_request_type"
    finally:
        server.close()


# --- Proxy hook (transparent/forwarding pass-through to fetch's connection seam) -----------------


def test_build_proxy_connect_dials_the_configured_proxy_not_the_validated_ip() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(2)
    proxy_port = server.getsockname()[1]
    try:
        connect = build_proxy_connect("127.0.0.1", proxy_port)
        sock = connect("203.0.113.5", 443, 2.0)  # a validated-but-irrelevant destination IP
        accepted, _addr = server.accept()
        accepted.close()
        sock.close()
    finally:
        server.close()
