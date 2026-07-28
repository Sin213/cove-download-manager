"""Interface enumeration and the no-silent-fallback validation rule."""
from unittest.mock import patch

import pytest

from cove.netiface import (
    ANY_INTERFACE,
    NetworkInterfaceUnavailable,
    bound_requests_session,
    bound_urlopen,
    interface_address,
    interface_exists,
    list_interfaces,
)


def test_any_interface_is_the_empty_string():
    """The stored value for "Any interface" must stay falsy: aria2 is then
    launched with no --interface flag at all."""
    assert ANY_INTERFACE == ""


def test_enumeration_skips_loopback_and_deduplicates():
    class FakeIface:
        IsLoopBack = 1

        def __init__(self, name, loopback=False):
            self._name = name
            self._loopback = loopback

        def name(self):
            return self._name

        def flags(self):
            return 1 if self._loopback else 0

    fakes = [
        FakeIface("lo", loopback=True),
        FakeIface("wlan0"),
        FakeIface("eno1"),
        FakeIface("eno1"),
    ]
    with patch("PySide6.QtNetwork.QNetworkInterface") as qni:
        qni.allInterfaces.return_value = fakes
        qni.IsLoopBack = 1
        names = list_interfaces()

    assert names == ["eno1", "wlan0"]


def test_empty_interface_always_validates():
    """"Any interface" can never be the thing that blocks a launch."""
    assert interface_exists("") is True


def test_missing_interface_does_not_validate():
    with patch("cove.netiface.list_interfaces", return_value=["eno1"]):
        assert interface_exists("wg0-mullvad") is False
        assert interface_exists("eno1") is True


def test_real_enumeration_returns_strings():
    """Smoke test against the actual machine: never raises, names only."""
    assert all(isinstance(n, str) and n for n in list_interfaces())


def test_interface_address_empty_name_is_none():
    assert interface_address("") is None


def test_interface_address_prefers_ipv4():
    class FakeIp:
        def __init__(self, ip, protocol):
            self._ip = ip
            self._protocol = protocol

        def protocol(self):
            return self._protocol

        def toString(self):
            return self._ip

    class FakeAddr:
        def __init__(self, ip, is_v4):
            self._entry_ip = FakeIp(ip, 0 if is_v4 else 1)

        def ip(self):
            return self._entry_ip

    class FakeIface:
        def __init__(self, name, entries):
            self._name = name
            self._entries = entries

        def name(self):
            return self._name

        def addressEntries(self):
            return self._entries

    fakes = [FakeIface("wg0-mullvad", [FakeAddr("fe80::1", False), FakeAddr("10.8.0.2", True)])]
    with patch("PySide6.QtNetwork.QNetworkInterface") as qni, \
         patch("PySide6.QtNetwork.QAbstractSocket") as qas:
        qni.allInterfaces.return_value = fakes
        qas.IPv4Protocol = 0
        assert interface_address("wg0-mullvad") == "10.8.0.2"


def test_bound_requests_session_with_no_interface_is_plain_session():
    session = bound_requests_session("")
    assert session.head is not None  # ordinary, unmodified Session


def test_bound_requests_session_raises_when_address_unresolvable():
    with patch("cove.netiface.interface_address", return_value=None):
        with pytest.raises(NetworkInterfaceUnavailable):
            bound_requests_session("wg0-mullvad")


def test_bound_urlopen_raises_when_address_unresolvable():
    with patch("cove.netiface.interface_address", return_value=None):
        with pytest.raises(NetworkInterfaceUnavailable):
            bound_urlopen("https://example.com", name="wg0-mullvad")
