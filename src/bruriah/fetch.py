# Slice 9A: the SSRF-safe HTTPS fetch core -- the highest security-risk module in this change,
# because it is the only one that introduces network egress. Stdlib only: `requests`/`httpx`
# re-resolve DNS per connection and cannot be pinned to an already-validated IP, which defeats the
# DNS-rebinding defense below (design.md "Network").
#
# The ten safety requirements this module implements, in the order they run:
#   1. Network is OFF unless the caller explicitly passes `network_enabled=True` -- no connection
#      attempt is ever made otherwise; `fetch()` returns a typed `status="disabled"` result.
#   2. HTTPS only, `GET`/`HEAD` only; every other scheme/method is rejected before any I/O.
#   3. A strict caller-supplied `host[:port]` allowlist is checked BEFORE any DNS lookup.
#   4. DNS-rebinding defense (the crux): `resolver(host)` is called EXACTLY ONCE per hop. Its
#      result is validated, then `connect()` is called with one of THOSE addresses -- never a
#      second, independent resolution. This structurally closes the classic
#      validate-then-re-resolve gap; a naive client that re-resolves between check and connect is
#      the vulnerability this module exists to prevent.
#   5. Every resolved IP (not just the one connected to) is checked against loopback, private,
#      link-local (including the 169.254.169.254 cloud metadata address), multicast, reserved, and
#      unspecified ranges via `ipaddress`, plus `is_global` as a second, version-independent
#      signal -- belt and suspenders.
#   6. Every redirect (`Location` header) is a FRESH URL and re-enters the exact same
#      scheme/allowlist/resolve/IP-validate pipeline before it is followed; `max_redirects` bounds
#      the chain.
#   7. No userinfo, cookies, or `Authorization` are ever sent: the outbound header set is a fixed,
#      minimal constant with no caller-supplied header option in this slice, so there is no
#      injection vector to strip in the first place. Userinfo present in a URL is simply never
#      read (canonical URLs are built from host/port only).
#   8. Hard limits: connect+read+total elapsed (`max_elapsed_ms`), `max_redirects`, total
#      connection attempts per `fetch()` call (`max_network_requests`), and a byte ceiling
#      (`max_bytes`) enforced on BOTH the raw/compressed wire stream and the decompressed output --
#      the frozen `Budgets` model has one bytes field, so this module deliberately uses it for
#      both the transfer ceiling and the decompression-bomb ceiling (see `_read_bounded_body`).
#      Content-Type is checked against a fixed allowlist (`text/*`, `application/pdf`, declared
#      JSON). True concurrent-fetch throttling across simultaneously-running `fetch()` calls is out
#      of scope for this synchronous, single-call core; `max_network_requests` bounds the
#      connections made by ONE call (initial hop + each followed redirect), which is the concrete,
#      enforceable meaning available here. Multi-call concurrency belongs to whatever orchestrates
#      several fetches (Slice 9B's `research.py` or later), never to this module -- "No hidden
#      chaining."
#   9. The response body is returned as opaque `bytes` alongside an `EvidenceRecord(kind=
#      "captured_live")`; nothing in this module parses, interprets, or acts on body content --
#      it cannot be steered by anything the response says.
#  10. Typed and total: every failure raises `FetchError` with a `.code` (mirrors
#      `PackError`/`ServiceError`/`PlatformError`/`CliError`), and the public `fetch()` entry point
#      wraps the whole pipeline in that typed catch PLUS a bare `except Exception` backstop that
#      converts anything unenumerated into `FetchResult(status="error", code="internal_error")` --
#      no bare traceback can ever escape `fetch()`.
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import socket
import ssl
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

from .contracts import Budgets, EvidenceRecord

# hostname -> distinct numeric IP strings, called EXACTLY ONCE per hop (see module docstring, #4).
Resolver = Callable[[str], list[str]]
# (validated_ip, port, timeout_seconds) -> a connected, plain (not yet TLS-wrapped) TCP socket.
ConnectionFactory = Callable[[str, int, float], socket.socket]

_ALLOWED_SCHEME = "https"
_ALLOWED_METHODS = frozenset({"GET", "HEAD"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_METADATA_IPS = frozenset({"169.254.169.254", "fd00:ec2::254"})
_ALLOWED_MIME_PREFIXES = ("text/",)
_ALLOWED_MIME_EXACT = frozenset({"application/pdf", "application/json"})
_USER_AGENT = "bruriah/1 (+policy-gated, local-only research)"
_CHUNK_SIZE = 65_536
_BLOCKED_CODES = frozenset({
    "unsupported_scheme", "unsupported_method", "invalid_url", "host_not_allowlisted",
    "dangerous_ip_blocked", "invalid_ip", "content_type_not_allowed",
    "raw_bytes_limit_exceeded", "decompressed_limit_exceeded", "redirect_count_exceeded",
    "network_request_count_exceeded", "unsupported_content_encoding",
})


class FetchError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FetchResult:
    """Total, typed outcome of one `fetch()` call. Only `status="ok"` carries a body/evidence;
    every other status is a safe, empty refusal -- never a partial or prohibited body."""

    status: Literal["disabled", "blocked", "error", "ok"]
    code: str
    evidence: EvidenceRecord | None = None
    body: bytes | None = None
    content_type: str | None = None
    redirect_chain: tuple[str, ...] = ()


def default_resolver(host: str) -> list[str]:
    """Production resolver: `socket.getaddrinfo`, the OS/stdlib DNS path. Called once per hop by
    `_fetch_one_hop`; its result is what gets validated AND connected to -- never re-resolved."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise FetchError("dns_resolution_failed") from error
    ips: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        if sockaddr[0] not in ips:
            ips.append(sockaddr[0])
    if not ips:
        raise FetchError("dns_resolution_failed")
    return ips


def default_connect(ip: str, port: int, timeout: float) -> socket.socket:
    """Production connection factory: a plain TCP connect to the already-validated numeric `ip`
    -- never a hostname, so there is no second resolution left to rebind against."""
    try:
        return socket.create_connection((ip, port), timeout=timeout)
    except OSError as error:
        raise FetchError("connect_failed") from error


def _validate_ip(raw_ip: str) -> None:
    """Reject loopback, private, link-local (including the cloud metadata address), multicast,
    reserved, and unspecified addresses. `is_global` is the version-independent stdlib signal;
    the explicit checks name exactly which classes are rejected, per the SSRF requirement list."""
    try:
        address = ipaddress.ip_address(raw_ip)
    except ValueError as error:
        raise FetchError("invalid_ip") from error
    if (
        raw_ip in _METADATA_IPS
        or address.is_loopback or address.is_private or address.is_link_local
        or address.is_multicast or address.is_reserved or address.is_unspecified
        or not address.is_global
    ):
        raise FetchError("dangerous_ip_blocked")


def _validate_destination(url: str, allowlist: frozenset[str]) -> tuple[str, str, int, str]:
    """Parse `url`, enforce HTTPS-only, and check the host[:port] allowlist BEFORE any DNS lookup
    or connection -- the cheapest, strictest gate runs first. Userinfo is never read (the
    canonical URL/host below is built from host+port only, so credentials in the URL have no
    effect and are never forwarded). Returns (canonical_url, host, port, path_and_query)."""
    parts = urlsplit(url)
    if parts.scheme != _ALLOWED_SCHEME:
        raise FetchError("unsupported_scheme")
    host = parts.hostname
    if not host:
        raise FetchError("invalid_url")
    try:
        port = parts.port or 443
    except ValueError as error:
        raise FetchError("invalid_url") from error
    if host not in allowlist and f"{host}:{port}" not in allowlist:
        raise FetchError("host_not_allowlisted")
    path_qs = parts.path or "/"
    if parts.query:
        path_qs = f"{path_qs}?{parts.query}"
    canonical = urlunsplit((parts.scheme, f"{host}:{port}", parts.path or "/", parts.query, ""))
    return canonical, host, port, path_qs


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An `HTTPSConnection` whose `connect()` is a no-op that assigns an ALREADY-validated,
    ALREADY-TLS-wrapped socket. `http.client` itself never resolves or opens a connection, so the
    validate-then-reconnect DNS-rebinding gap is closed structurally, not by convention."""

    def __init__(self, tls_socket: ssl.SSLSocket, host: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_socket = tls_socket

    def connect(self) -> None:
        self.sock = self._pinned_socket


def _fetch_one_hop(
    url: str, method: str, allowlist: frozenset[str], resolver: Resolver, connect: ConnectionFactory,
    ssl_context: ssl.SSLContext, deadline: float, clock: Callable[[], float],
) -> tuple[http.client.HTTPSConnection, http.client.HTTPResponse, str]:
    """Fully validate `url`'s destination (scheme, allowlist, DNS resolved ONCE, every resolved
    IP), open one TLS connection pinned to the first validated numeric IP with `server_hostname
    =host` for SNI and certificate verification, and issue one request."""
    canonical, host, port, path_qs = _validate_destination(url, allowlist)
    candidate_ips = resolver(host)
    for candidate in candidate_ips:
        _validate_ip(candidate)  # every resolved IP must be safe -- rejects mixed-answer DNS too
    remaining = deadline - clock()
    if remaining <= 0:
        raise FetchError("timeout_exceeded")
    raw_socket = connect(candidate_ips[0], port, remaining)
    raw_socket.settimeout(remaining)
    try:
        tls_socket = ssl_context.wrap_socket(raw_socket, server_hostname=host)
    except (ssl.SSLError, OSError) as error:
        raw_socket.close()
        raise FetchError("tls_failed") from error

    connection = _PinnedHTTPSConnection(tls_socket, host, port, remaining)
    try:
        connection.request(
            method, path_qs,
            headers={
                "Host": host, "User-Agent": _USER_AGENT, "Accept-Encoding": "gzip, deflate",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
    except (http.client.HTTPException, OSError) as error:
        connection.close()
        raise FetchError("request_failed") from error
    return connection, response, canonical


def _decompressor(encoding: str) -> zlib.decompressobj | None:
    if encoding in ("", "identity"):
        return None
    if encoding == "gzip":
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    if encoding == "deflate":
        return zlib.decompressobj()
    raise FetchError("unsupported_content_encoding")


def _read_bounded_body(
    response: http.client.HTTPResponse, max_bytes: int, deadline: float, clock: Callable[[], float],
) -> bytes:
    """Stream the body in fixed chunks, enforcing BOTH the raw/compressed byte ceiling and the
    decompressed byte ceiling at `max_bytes` (see module docstring #8). Each `decompress()` call
    is itself capped at the remaining decompressed budget via `max_length`, so a single chunk can
    never balloon past the limit before it is checked -- the concrete defense against a
    decompression bomb (tiny wire payload, huge decompressed output)."""
    decompressor = _decompressor((response.getheader("Content-Encoding") or "").strip().lower())
    raw_total = 0
    out = bytearray()
    while True:
        if clock() > deadline:
            raise FetchError("timeout_exceeded")
        try:
            chunk = response.read(_CHUNK_SIZE)
        except OSError as error:
            raise FetchError("read_failed") from error
        if not chunk:
            break
        raw_total += len(chunk)
        if raw_total > max_bytes:
            raise FetchError("raw_bytes_limit_exceeded")
        if decompressor is None:
            out.extend(chunk)
        else:
            out.extend(decompressor.decompress(chunk, max(1, max_bytes - len(out) + 1)))
        if len(out) > max_bytes:
            raise FetchError("decompressed_limit_exceeded")
    if decompressor is not None and (decompressor.unconsumed_tail or not decompressor.eof):
        # decompress() stopped early because output hit its per-call cap: the bomb shape.
        raise FetchError("decompressed_limit_exceeded")
    return bytes(out)


def _content_type_allowed(content_type: str) -> bool:
    if not content_type:
        return False
    return (
        content_type.startswith(_ALLOWED_MIME_PREFIXES)
        or content_type in _ALLOWED_MIME_EXACT
        or content_type.endswith("+json")
    )


def _extraction_method(content_type: str) -> str:
    if content_type == "application/pdf":
        return "pdf_text"
    if content_type == "application/json" or content_type.endswith("+json"):
        return "api_json"
    if content_type == "text/html":
        return "html_text"
    if content_type.startswith("text/"):
        return "raw_lines"
    return "unknown"


def _status_for(code: str) -> Literal["disabled", "blocked", "error"]:
    if code == "network_disabled":
        return "disabled"
    return "blocked" if code in _BLOCKED_CODES else "error"


def _fetch_inner(
    url: str, method: str, budgets: Budgets, *, network_enabled: bool, allowlist: frozenset[str],
    resolver: Resolver, connect: ConnectionFactory, ssl_context: ssl.SSLContext,
    clock: Callable[[], float], retrieved_at: datetime,
) -> FetchResult:
    if not network_enabled:
        raise FetchError("network_disabled")
    if method not in _ALLOWED_METHODS:
        raise FetchError("unsupported_method")

    deadline = clock() + (budgets.max_elapsed_ms / 1000)
    redirect_chain: list[str] = []
    current_url = url
    hops_made = 0

    for _ in range(budgets.max_redirects + 1):
        if hops_made >= budgets.max_network_requests:
            raise FetchError("network_request_count_exceeded")
        hops_made += 1
        connection, response, canonical = _fetch_one_hop(
            current_url, method, allowlist, resolver, connect, ssl_context, deadline, clock,
        )
        try:
            redirect_chain.append(canonical)
            if response.status in _REDIRECT_STATUSES:
                location = response.getheader("Location")
                response.read()  # drain and discard -- a redirect body is never evidence
                if not location:
                    raise FetchError("invalid_redirect_location")
                current_url = urljoin(canonical, location)  # a fresh URL; re-validated next loop
                continue
            if not 200 <= response.status < 300:
                response.read()
                raise FetchError(f"http_status_{response.status}")

            content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if not _content_type_allowed(content_type):
                response.read()
                raise FetchError("content_type_not_allowed")
            body = b"" if method == "HEAD" else _read_bounded_body(response, budgets.max_bytes, deadline, clock)
        finally:
            connection.close()

        digest_hex = hashlib.sha256(body).hexdigest()
        evidence = EvidenceRecord(
            ref=f"live:sha256:{digest_hex[:32]}", kind="captured_live",
            publisher=urlsplit(canonical).hostname or "unknown", locator=canonical,
            citation_locator=canonical, digest=f"sha256:{digest_hex}",
            extraction_method=_extraction_method(content_type), redirect_chain=redirect_chain[:-1],
            retrieved_at=retrieved_at, authority="unknown",
            authority_rationale="Live HTTP fetch; authority/pack context not assessed by fetch.py.",
            freshness="unknown", license="unknown", conflict="unknown",
        )
        return FetchResult(
            status="ok", code="ok", evidence=evidence, body=body,
            content_type=content_type or None, redirect_chain=tuple(redirect_chain),
        )

    raise FetchError("redirect_count_exceeded")


def fetch(
    url: str,
    method: Literal["GET", "HEAD"],
    budgets: Budgets,
    *,
    network_enabled: bool,
    allowlist: frozenset[str],
    resolver: Resolver = default_resolver,
    connect: ConnectionFactory = default_connect,
    ssl_context: ssl.SSLContext | None = None,
    clock: Callable[[], float] = time.monotonic,
    retrieved_at: datetime | None = None,
) -> FetchResult:
    """Policy-opt-in, SSRF-safe HTTPS `GET`/`HEAD`. Never raises -- every expected AND
    unexpected failure returns a typed `FetchResult` (module docstring #10). `resolver`/`connect`
    are the DNS-rebinding-defense test seam (module docstring #4); production defaults are the
    real OS resolver and a real TCP `connect()`. `ssl_context` defaults to
    `ssl.create_default_context()` (hostname + chain verification ON); tests inject one that
    trusts a local loopback test certificate. `retrieved_at` defaults to the real current time.
    """
    try:
        return _fetch_inner(
            url, method, budgets, network_enabled=network_enabled, allowlist=allowlist,
            resolver=resolver, connect=connect,
            ssl_context=ssl_context or ssl.create_default_context(),
            clock=clock, retrieved_at=retrieved_at or datetime.now(timezone.utc),
        )
    except FetchError as error:
        return FetchResult(status=_status_for(error.code), code=error.code)
    except Exception:  # Backstop (#10): no bare exception may ever escape `fetch()`.
        return FetchResult(status="error", code="internal_error")


__all__ = ["ConnectionFactory", "FetchError", "FetchResult", "Resolver", "default_connect", "default_resolver", "fetch"]
