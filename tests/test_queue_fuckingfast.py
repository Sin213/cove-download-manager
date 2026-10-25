"""Non-interactive intake must never queue a share page as a file."""
import pytest
import requests
from PySide6.QtCore import QCoreApplication
from tests.test_queue import queue_env  # noqa: F401

REASON = ("This Fuckingfast.co page needs your browser; add it through Add Download with "
          "the Cove Firefox extension, or download it in your browser.")


def settle(queue):
    for _ in range(20):
        assert queue._pool.waitForDone(1000)
        QCoreApplication.processEvents()
        if not queue._inflight:
            return
    pytest.fail("workers did not settle")


@pytest.mark.parametrize("enabled", [False, True])
def test_queue_guard_never_dispatches_share_page(queue_env, monkeypatch, enabled):
    app = QCoreApplication.instance() or QCoreApplication([])
    queue, rpc, _ = queue_env(intelligent_segments=False, all_debrid_enabled=enabled)
    monkeypatch.setattr(requests.sessions.Session, "request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network disabled")))
    events = []
    monkeypatch.setattr(queue, "_diag", lambda *a, **k: events.append((a, k)))
    tid = queue.add_url("https://fuckingfast.co/6oavo2x52dq0#file.zip")
    queue._launch(queue.tasks[tid])
    settle(queue)
    assert queue.tasks[tid].status == "error"
    assert queue.tasks[tid].error == REASON
    assert rpc.added == [] and "https://" not in repr(events)
    assert app is not None


def test_queue_ordinary_url_unchanged(queue_env):
    queue, rpc, _ = queue_env(intelligent_segments=False)
    url = "https://example.com/file.zip?q=unchanged"
    tid = queue.add_url(url)
    queue._launch(queue.tasks[tid])
    settle(queue)
    assert rpc.added[0]["uris"] == [url]


LANDING_VARIANTS = [
    "https://user@fuckingfast.co/Ab_4",
    "https://user:password@fuckingfast.co/Ab_4",
    "https://fuckingfast.co:8443/Ab_4", "https://fuckingfast.co:bad/Ab_4",
    "https://fuckingfast.co./Ab_4", "HTTPS://FUCKINGFAST.CO/Ab_4",
    "https://fuck\tingfast.co/Ab_4", "https://fuck ingfast.co/Ab_4",
    "https://fuck\ningfast.co/Ab_4", "https://fuck\x00ingfast.co/Ab_4",
    "https://fuckingfast.co\\Ab_4", "https://fuckingfast.co\\@evil.com/Ab_4",
    "https://fuck\\ingfast.co/Ab_4",
    "https://www.fuckingfast.co/f/Ab_4", "https://www.fuckingfast.co.:8443/Ab_4",
    "https://fuckingfast.co/help", "https://fuckingfast.co/",
    "https://fuckingfast.co/dl/xyz", "https://fuckingfast.co/f/Ab_4/go?q=1#name",
]


@pytest.mark.parametrize("url", LANDING_VARIANTS)
def test_queue_guard_blocks_ineligible_landing_variants(queue_env, monkeypatch, url):
    queue, rpc, _ = queue_env(intelligent_segments=False)
    events = []
    monkeypatch.setattr(queue, "_diag", lambda *a, **k: events.append((a, k)))
    monkeypatch.setattr(requests.sessions.Session, "request",
                        lambda *a, **k: pytest.fail("unexpected network"))
    tid = queue.add_url(url)
    queue._launch(queue.tasks[tid])
    settle(queue)
    assert queue.tasks[tid].status == "error"
    assert queue.tasks[tid].error == REASON
    assert rpc.added == []
    assert url not in repr(events) and "password" not in repr(events)


@pytest.mark.parametrize("url", [
    "https://dl.fuckingfast.co/dl/xyz", "https://xfuckingfast.co/Ab_4",
    "https://fuckingfast.co.evil.com/Ab_4", "https://evil.com/fuckingfast.co/Ab_4",
    "https://evil.com/?u=fuckingfast.co",
])
def test_queue_guard_allows_delivery_and_unrelated_hosts(queue_env, url):
    queue, rpc, _ = queue_env(intelligent_segments=False)
    tid = queue.add_url(url)
    queue._launch(queue.tasks[tid])
    settle(queue)
    assert rpc.added[0]["uris"] == [url]
