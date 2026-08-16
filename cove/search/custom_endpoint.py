"""Network-security policy for user-configured Torznab endpoints.

Search v2 slice S4. A user-configured endpoint URL is classified by its literal
authority - scheme, hostname, port, userinfo - and nothing else. This module is
pure: it never touches the network, DNS, sockets, routes or Qt, and it never
resolves a hostname to decide privilege.

The rule it encodes is deliberately explicit rather than built on
:meth:`ipaddress.IPv4Address.is_private`: Cove grants local routing privilege
only to exact localhost, literal loopback, RFC1918, link-local, and the IPv6
equivalents. Everything else - ordinary hostnames, documentation ranges,
CGNAT, multicast, reserved and unspecified addresses - stays
``PUBLIC_OR_UNRESOLVED``, which means HTTPS required and the caller's selected
interface preserved.

Local/private routing privilege is a transport decision only. It never makes
endpoint responses trusted: local XML is still untrusted parser input under
``cove.search.torznab``.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

# Explicit range policy. Deliberately not ``ip_address(...).is_private``: the
# stdlib "private" notion is broader than what Cove approves, and varies across
# Python versions. These constants are the whole allowlist.
_IPV4_LOOPBACK = ipaddress.IPv4Network("127.0.0.0/8")
_IPV4_PRIVATE_10 = ipaddress.IPv4Network("10.0.0.0/8")
_IPV4_PRIVATE_172 = ipaddress.IPv4Network("172.16.0.0/12")
_IPV4_PRIVATE_192 = ipaddress.IPv4Network("192.168.0.0/16")
_IPV4_LINK_LOCAL = ipaddress.IPv4Network("169.254.0.0/16")
_IPV6_LOOPBACK = ipaddress.IPv6Network("::1/128")
_IPV6_ULA = ipaddress.IPv6Network("fc00::/7")
_IPV6_LINK_LOCAL = ipaddress.IPv6Network("fe80::/10")


class EndpointClass(Enum):
    """How a literal endpoint authority is treated for routing purposes."""

    LOCAL_LOOPBACK = "local_loopback"
    PRIVATE_LAN = "private_lan"
    PUBLIC_OR_UNRESOLVED = "public_or_unresolved"


class EndpointPolicyError(ValueError):
    """A custom endpoint URL Cove refuses to interpret, before any network.

    The message is one of a small fixed set of safe, human-oriented phrases.
    It never carries the configured URL, its query string, credentials or the
    API key.
    """


@dataclass(frozen=True)
class EndpointPolicy:
    """The transport decision for one custom endpoint.

    ``classification`` is ``None`` only for a rejected endpoint that could not
    be interpreted. ``effective_interface`` is ``None`` for the unbound/local
    transport path and the caller's interface string otherwise.
    """

    classification: EndpointClass | None
    allowed: bool
    reason: str | None
    effective_interface: str | None
    suppress_env_proxy: bool


def classify_host(hostname: str) -> EndpointClass:
    """Classify one literal hostname. No DNS, no sockets, no HTTP.

    Exact ``localhost`` (case-insensitive, with or without exactly one trailing
    dot) is loopback. A literal IP is checked against the explicit range policy
    above, including the IPv4 address behind an IPv4-mapped IPv6 literal.
    Anything else - any other hostname, any address outside the allowlist -
    is ``PUBLIC_OR_UNRESOLVED``.
    """
    if hostname.lower() in ("localhost", "localhost."):
        return EndpointClass.LOCAL_LOOPBACK
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return EndpointClass.PUBLIC_OR_UNRESOLVED
    return _classify_address(address)


def classify_custom_torznab_endpoint(url: str) -> EndpointClass:
    """Classify a full endpoint URL by its parsed authority.

    Raises :class:`EndpointPolicyError` when the authority cannot be safely
    interpreted (missing hostname, broken IPv6 literal, out-of-range port).
    Scheme and userinfo are *not* checked here - they are transport policy, and
    belong to :func:`resolve_custom_torznab_transport`.
    """
    return classify_host(_parse_authority(url).hostname)


def resolve_custom_torznab_transport(
    url: str, requested_interface: str | None
) -> EndpointPolicy:
    """The full transport decision for one custom endpoint.

    Validates scheme, userinfo and authority, classifies the hostname, and
    returns a policy. A rejected endpoint is reported as ``allowed=False`` with
    a safe reason; it never raises and never touches the network.
    """
    try:
        parsed = _parse_authority(url)
    except EndpointPolicyError as error:
        return EndpointPolicy(None, False, str(error), None, False)

    scheme = (parsed.scheme or "").lower()
    if scheme == "":
        return _reject("Invalid Torznab endpoint")
    if scheme not in ("http", "https"):
        return _reject("Unsupported Torznab endpoint scheme")
    if parsed.username is not None or parsed.password is not None:
        return _reject("Torznab endpoint URL credentials are not supported")

    classification = classify_host(parsed.hostname)
    if classification in (EndpointClass.LOCAL_LOOPBACK, EndpointClass.PRIVATE_LAN):
        # Literal local/private destination: ordinary direct transport, no
        # interface binding, no environment-proxy routing.
        return EndpointPolicy(classification, True, None, None, True)
    if scheme != "https":
        return _reject("Torznab public endpoints require HTTPS")
    # Public or unresolved: HTTPS only, caller-selected interface preserved.
    return EndpointPolicy(classification, True, None, requested_interface, False)


def _reject(reason: str) -> EndpointPolicy:
    return EndpointPolicy(None, False, reason, None, False)


def _parse_authority(url: str):
    """Split and validate a custom endpoint URL, with no network work.

    Raises :class:`EndpointPolicyError` for a missing hostname, a broken
    bracketed IPv6 literal, or an out-of-range/non-numeric port. The raw URL is
    never embedded in the error.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise EndpointPolicyError("Invalid Torznab endpoint") from None
    try:
        # Accessing ``.port`` validates the numeric port range before any
        # transport could attempt it; ``None`` simply means "no port".
        parsed.port
    except ValueError:
        raise EndpointPolicyError("Invalid Torznab endpoint") from None
    try:
        hostname = parsed.hostname
    except ValueError:
        raise EndpointPolicyError("Invalid Torznab endpoint") from None
    if hostname is None:
        raise EndpointPolicyError("Invalid Torznab endpoint")
    return parsed


def _classify_address(address) -> EndpointClass:
    """Classify a parsed IP literal against the explicit range policy."""
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return _classify_ipv4(mapped)
        return _classify_ipv6(address)
    return _classify_ipv4(address)


def _classify_ipv4(address: ipaddress.IPv4Address) -> EndpointClass:
    if address in _IPV4_LOOPBACK:
        return EndpointClass.LOCAL_LOOPBACK
    if (
        address in _IPV4_PRIVATE_10
        or address in _IPV4_PRIVATE_172
        or address in _IPV4_PRIVATE_192
    ):
        return EndpointClass.PRIVATE_LAN
    if address in _IPV4_LINK_LOCAL:
        return EndpointClass.PRIVATE_LAN
    return EndpointClass.PUBLIC_OR_UNRESOLVED


def _classify_ipv6(address: ipaddress.IPv6Address) -> EndpointClass:
    if address in _IPV6_LOOPBACK:
        return EndpointClass.LOCAL_LOOPBACK
    if address in _IPV6_ULA:
        return EndpointClass.PRIVATE_LAN
    if address in _IPV6_LINK_LOCAL:
        return EndpointClass.PRIVATE_LAN
    return EndpointClass.PUBLIC_OR_UNRESOLVED
