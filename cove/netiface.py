"""Network interface enumeration for aria2's --interface binding.

Cove runs a single shared aria2 daemon, so binding it to an interface binds
*every* aria2-managed transfer, not only BitTorrent. The Settings note says
so explicitly; this module only supplies and validates the names.

Enumeration goes through Qt (QNetworkInterface) rather than a new
dependency: PySide6 is already required, and it is cross-platform.
"""
from __future__ import annotations

# "" is the stored value for "Any interface" — aria2 is then launched with
# no --interface flag at all, which is its default behaviour.
ANY_INTERFACE = ""
ANY_INTERFACE_LABEL = "Any interface"


def list_interfaces() -> list[str]:
    """Bindable interface names, loopback excluded.

    Loopback is left out on purpose: binding aria2 to it would break every
    download, and offering it in a dropdown invites exactly that.
    """
    try:
        from PySide6.QtNetwork import QNetworkInterface
    except ImportError:
        return []
    names: list[str] = []
    for iface in QNetworkInterface.allInterfaces():
        if iface.flags() & QNetworkInterface.IsLoopBack:
            continue
        name = iface.name()
        if name and name not in names:
            names.append(name)
    names.sort()
    return names


def interface_exists(name: str) -> bool:
    """Whether a saved interface name is still present on this machine.

    A stale name must never fall back to "any interface": a user who bound
    Cove to a VPN adapter has not agreed to send traffic over whatever
    adapter happens to be up instead. Callers treat False as fatal.
    """
    if not name:
        return True
    return name in list_interfaces()


def interface_address(name: str) -> str | None:
    """First bindable address on interface `name`, IPv4 preferred.

    aria2 binds by interface name via --interface, but requests/urllib only
    know how to bind a socket to an address, so the direct HTTP calls Cove
    makes outside of aria2 (debrid API, queue probes, update checks) need
    the address behind the name.
    """
    if not name:
        return None
    try:
        from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
    except ImportError:
        return None
    for iface in QNetworkInterface.allInterfaces():
        if iface.name() != name:
            continue
        entries = iface.addressEntries()
        for entry in entries:
            if entry.ip().protocol() == QAbstractSocket.IPv4Protocol:
                return entry.ip().toString()
        for entry in entries:
            return entry.ip().toString()
    return None


class NetworkInterfaceUnavailable(RuntimeError):
    """A configured interface has no address to bind to right now.

    Raised instead of silently sending the request over the default route:
    debrid/update traffic must fail the same way aria2 does when the bound
    adapter drops, not leak out unbound.
    """


def bound_requests_session(name: str):
    """A requests.Session whose outgoing sockets originate from `name`.

    Covers the direct HTTP calls Cove makes outside of aria2 (debrid API
    resolution, queue probes) so binding an interface in Settings protects
    all of Cove's network traffic, not only aria2-managed downloads.
    """
    import requests
    from requests.adapters import HTTPAdapter

    session = requests.Session()
    if not name:
        return session
    address = interface_address(name)
    if not address:
        raise NetworkInterfaceUnavailable(
            f"Network interface '{name}' has no address right now."
        )

    class _BoundAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs["source_address"] = (address, 0)
            return super().init_poolmanager(*args, **kwargs)

    adapter = _BoundAdapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def bound_urlopen(request, *, name: str, timeout=None):
    """urllib.request.urlopen, with the socket bound to `name`'s address.

    updater.py's GitHub calls use urllib rather than requests; this keeps
    them under the same binding guarantee instead of leaking out the
    default route whenever an interface is configured.
    """
    import functools
    import http.client
    import urllib.request

    if not name:
        return urllib.request.urlopen(request, timeout=timeout)

    address = interface_address(name)
    if not address:
        raise NetworkInterfaceUnavailable(
            f"Network interface '{name}' has no address right now."
        )

    class _BoundHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(
                functools.partial(http.client.HTTPConnection, source_address=(address, 0)),
                req,
            )

    class _BoundHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(
                functools.partial(http.client.HTTPSConnection, source_address=(address, 0)),
                req,
            )

    opener = urllib.request.build_opener(_BoundHTTPHandler(), _BoundHTTPSHandler())
    return opener.open(request, timeout=timeout)
