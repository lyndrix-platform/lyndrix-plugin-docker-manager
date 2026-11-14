"""Where the core may send Docker API requests (SSRF guard).

A host's address is operator-supplied and the core process then issues HTTP
requests to it from inside the platform network. Private and loopback
addresses stay allowed — Docker proxies live on the LAN or on the host itself —
so this is a blocklist for the ranges that are never a Docker proxy: link-local
incl. the cloud metadata services, multicast, unspecified and reserved space.

Two checks, because a name can point anywhere and can change:
  * ``validate_target``  — when a host is stored (syntax, literals, and every
    address a hostname resolves to right now);
  * ``connect_address``  — before every request: resolve once, check every
    address, and return the checked IP so the caller connects to exactly the
    address that was checked (no second lookup → no DNS-rebinding window).
"""
from __future__ import annotations

import ipaddress
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import List, Optional, Union

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

# Hostnames per RFC 1123 (labels of [a-z0-9-], no leading/trailing hyphen).
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)
# A last label that is a number in any C notation: such a "hostname" is an
# IPv4 address to inet_aton-style resolvers (2852039166, 0xa9fea9fe, 0251.…).
_NUMERIC_LABEL_RE = re.compile(r"^(0[xX][0-9A-Fa-f]*|[0-9]+)$")

# Ranges the category flags below do not cover.
_BLOCKED_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "169.254.0.0/16",      # link-local incl. AWS/GCP/Azure metadata 169.254.169.254
    "fe80::/10",
    "fd00:ec2::/32",       # AWS IPv6 instance metadata (unique-local, not link-local)
    "100.100.100.200/32",  # Alibaba Cloud metadata
    "192.0.0.0/24",        # IETF protocol assignments incl. Oracle metadata 192.0.0.192
))
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

# Name resolution is not covered by the HTTP timeouts and blocks for the whole
# resolver timeout during a DNS outage. Lookups therefore run on their own
# small pool and the caller waits a bounded time; a hung lookup occupies one of
# these threads, never a worker of the shared default executor.
RESOLVE_TIMEOUT_SECONDS = 3.0
_RESOLVER_WORKERS = 4
_resolver_pool: Optional[ThreadPoolExecutor] = None


class TargetError(RuntimeError):
    """The target cannot be contacted: blocked address or failed resolution."""


def _embedded_ipv4(addr: ipaddress.IPv6Address) -> List[ipaddress.IPv4Address]:
    """IPv4 addresses an IPv6 address stands for (mapped, 6to4, Teredo, NAT64)."""
    found = [a for a in (addr.ipv4_mapped, addr.sixtofour) if a is not None]
    if addr.teredo is not None:
        found.extend(addr.teredo)
    if addr in _NAT64:
        found.append(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
    return found


def blocked_reason(addr: IPAddress) -> Optional[str]:
    """Why ``addr`` must not be contacted, or ``None`` when it is allowed."""
    if isinstance(addr, ipaddress.IPv6Address):
        for inner in _embedded_ipv4(addr):
            if blocked_reason(inner):
                return f"embeds {inner}"
    if addr.is_link_local or any(addr in net for net in _BLOCKED_NETWORKS):
        return "link-local/metadata"
    if addr.is_multicast:
        return "multicast"
    if addr.is_unspecified:
        return "unspecified"
    if addr.is_reserved:
        return "reserved"
    return None


def _literal(value: str) -> Optional[IPAddress]:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _legacy_ipv4(value: str) -> Optional[ipaddress.IPv4Address]:
    """``value`` as the C library reads it: decimal, hex, octal, 1–4 parts."""
    try:
        return ipaddress.IPv4Address(socket.inet_aton(value))
    except (OSError, ValueError, UnicodeError):
        return None


def _getaddrinfo(name: str) -> List[IPAddress]:
    infos = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
    seen: List[IPAddress] = []
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if addr not in seen:
            seen.append(addr)
    return seen


def resolve(name: str) -> List[IPAddress]:
    """Addresses of ``name`` within ``RESOLVE_TIMEOUT_SECONDS`` (blocking).

    Raises ``TargetError`` when the name does not resolve in time.
    """
    global _resolver_pool
    if _resolver_pool is None:
        _resolver_pool = ThreadPoolExecutor(
            max_workers=_RESOLVER_WORKERS, thread_name_prefix="docker-manager-dns"
        )
    future = _resolver_pool.submit(_getaddrinfo, name)
    try:
        addresses = future.result(timeout=RESOLVE_TIMEOUT_SECONDS)
    except FutureTimeout:
        future.cancel()
        raise TargetError(f"Name resolution of {name!r} timed out") from None
    except OSError as exc:
        raise TargetError(f"Cannot resolve {name!r}: {exc}") from None
    if not addresses:
        raise TargetError(f"Cannot resolve {name!r}")
    return addresses


def shutdown() -> None:
    """Drop the resolver pool (plugin unload); it is recreated on demand."""
    global _resolver_pool
    pool, _resolver_pool = _resolver_pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def _check_resolved(name: str, addresses: List[IPAddress]) -> None:
    for addr in addresses:
        reason = blocked_reason(addr)
        if reason:
            # One bad record poisons the name: we cannot know which one a
            # later lookup would hand out.
            raise ValueError(f"Disallowed host address: {name!r} resolves to {addr} ({reason})")


def validate_target(value: str) -> None:
    """Reject malformed or disallowed Docker host targets (``ValueError``).

    Blocking for hostnames (bounded DNS lookup). A name that does not resolve
    right now is accepted: it cannot be contacted in that state, and
    ``connect_address`` checks whatever it resolves to before every request.
    """
    addr = _literal(value)
    if addr is not None:
        reason = blocked_reason(addr)
        if reason:
            raise ValueError(f"Disallowed host address ({reason}): {value!r}")
        return
    legacy = _legacy_ipv4(value)
    if legacy is not None:
        # 2852039166, 0xa9fea9fe, 0251.0376.0251.0376, 169.254.43518 … all mean
        # an IPv4 address to the resolver. Refuse the notation outright instead
        # of guessing what the operator meant.
        raise ValueError(
            f"Invalid host address {value!r}: numeric notation for {legacy} — use dotted-quad notation"
        )
    if not _HOSTNAME_RE.match(value) or _NUMERIC_LABEL_RE.match(value.rsplit(".", 1)[-1]):
        raise ValueError(f"Invalid host address: {value!r}")
    try:
        addresses = resolve(value)
    except TargetError:
        return
    _check_resolved(value, addresses)


def connect_address(value: str) -> IPAddress:
    """The checked address to connect to for target ``value`` (blocking).

    Raises ``TargetError`` when the target is (or now resolves to) a blocked
    address or cannot be resolved.
    """
    addr = _literal(value)
    if addr is not None:
        candidates = [addr]
    else:
        if _legacy_ipv4(value) is not None:
            raise TargetError(f"Invalid host address {value!r}: numeric notation is not accepted")
        candidates = resolve(value)
    try:
        _check_resolved(value, candidates)
    except ValueError as exc:
        raise TargetError(str(exc)) from None
    return candidates[0]


def is_literal(value: str) -> bool:
    return _literal(value) is not None


def url_host(addr: IPAddress) -> str:
    """``addr`` as it appears in a URL authority (IPv6 in brackets)."""
    return f"[{addr}]" if isinstance(addr, ipaddress.IPv6Address) else str(addr)
