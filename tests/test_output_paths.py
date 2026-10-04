"""Engine-output validation contracts.

Publication safety lives in cove/output_paths.py. These tests pin the rules
that the extractor publication path depends on, in particular the difference
between "the engine named a file that is simply not there" and "the engine
named a path we refuse to touch".
"""
import os
import errno
import stat

import pytest

from cove.output_paths import (
    MissingEngineOutputError,
    OutputPathError,
    create_work_directory,
    validate_engine_output,
)


def unsupported_links(monkeypatch):
    import cove.output_paths as paths
    def unsupported(*args):
        raise OSError(errno.EOPNOTSUPP, "filesystem has no hardlinks")
    monkeypatch.setattr(paths, "_link_pinned_fd", unsupported)
    return paths


@pytest.mark.skipif(os.name == "nt", reason="POSIX default publication")
@pytest.mark.parametrize("mechanism", ["filesystem", "missing-proc", "missing-linkat"])
def test_default_publication_never_copies_and_preserves_baseline_errors(tmp_path, monkeypatch, mechanism):
    import cove.output_paths as paths
    work = create_work_directory(tmp_path)
    source = work.path / "report.pdf"
    source.write_bytes(b"staged")
    copied = []
    real_copy = paths._copy_pinned_fd
    def copy(*args):
        copied.append(True)
        return real_copy(*args)
    monkeypatch.setattr(paths, "_copy_pinned_fd", copy)
    if mechanism == "filesystem":
        unsupported_links(monkeypatch)
        message = "Could not publish output without overwriting"
    elif mechanism == "missing-proc":
        real_stat = os.stat
        def no_proc(path, *args, **kwargs):
            if os.fspath(path).startswith("/proc/self/fd/"):
                raise FileNotFoundError(errno.ENOENT, "procfs unavailable")
            return real_stat(path, *args, **kwargs)
        monkeypatch.setattr(os, "stat", no_proc)
        message = "Pinned descriptor reference is unavailable"
    else:
        monkeypatch.setattr(paths.ctypes, "CDLL", lambda *args, **kwargs: object())
        message = "Pinned-inode publication is unsupported"
    failure = None
    try:
        paths.publish_output(work, source, "report.pdf")
    except OutputPathError as exc:
        failure = exc
    assert copied == [], "default/media publication invoked the potentially blocking copy"
    assert type(failure) is OutputPathError
    assert str(failure).startswith(message)
    assert source.read_bytes() == b"staged"
    assert not (tmp_path / "report.pdf").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX opt-in publication")
@pytest.mark.parametrize("mechanism", ["missing-proc", "missing-linkat"])
def test_opt_in_copy_works_without_optional_hardlink_primitive(tmp_path, monkeypatch, mechanism):
    import cove.output_paths as paths
    work = create_work_directory(tmp_path)
    source = work.path / "report.pdf"
    source.write_bytes(b"staged")
    if mechanism == "missing-proc":
        real_stat = os.stat
        def no_proc(path, *args, **kwargs):
            if os.fspath(path).startswith("/proc/self/fd/"):
                raise FileNotFoundError(errno.ENOENT, "procfs unavailable")
            return real_stat(path, *args, **kwargs)
        monkeypatch.setattr(os, "stat", no_proc)
    else:
        monkeypatch.setattr(paths.ctypes, "CDLL", lambda *args, **kwargs: object())
    published = paths.publish_output(work, source, "report.pdf", allow_copy=True)
    assert published.read_bytes() == b"staged"
    assert not work.path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fallback")
def test_f1_f2_exclusive_copy_preserves_collision_and_mode(tmp_path, monkeypatch):
    paths = unsupported_links(monkeypatch)
    work = create_work_directory(tmp_path)
    source = work.path / "report.pdf"
    source.write_bytes(b"download" * 200000)
    source.chmod(0o640)
    original = tmp_path / "report.pdf"
    original.write_bytes(b"foreign")
    target = paths.publish_output(work, source, "report.pdf", allow_copy=True)
    assert target.name == "report (1).pdf"
    assert target.read_bytes() == b"download" * 200000
    assert original.read_bytes() == b"foreign"
    assert stat.S_IMODE(target.stat().st_mode) & ~0o640 == 0
    assert not work.path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fallback")
@pytest.mark.parametrize("swapped", [False, True])
def test_f3_f4_failed_copy_cleans_only_exact_created_identity(tmp_path, monkeypatch, swapped):
    paths = unsupported_links(monkeypatch)
    work = create_work_directory(tmp_path)
    source = work.path / "report.pdf"
    source.write_bytes(b"download")
    candidate = tmp_path / "report.pdf"
    victim = tmp_path / "foreign.pdf"
    victim.write_bytes(b"foreign")
    real_write = os.write
    calls = []
    def fail(fd, chunk):
        calls.append(True)
        # The partial is visible before commit: the fallback is not atomic.
        assert candidate.exists()
        if swapped:
            candidate.unlink()
            os.link(victim, candidate)
        else:
            real_write(fd, chunk[:2])
        raise OSError(errno.EIO, "mid-copy failure")
    monkeypatch.setattr(os, "write", fail)
    with pytest.raises(OutputPathError, match="Could not copy"):
        paths.publish_output(work, source, "report.pdf", allow_copy=True)
    assert calls
    assert source.read_bytes() == b"download"
    assert victim.read_bytes() == b"foreign"
    if swapped:
        assert candidate.exists(), "failed-copy cleanup deleted a replacement inode"
        assert candidate.read_bytes() == b"foreign"
    else:
        assert not candidate.exists()


def test_windows_publication_returns_identity_from_the_native_operation(tmp_path, monkeypatch):
    from tests.test_queue import _windows_test_work
    import cove.output_paths as paths
    api, work, source = _windows_test_work(tmp_path, monkeypatch)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"new source")
    original = api.publish_no_replace
    def native(work, source, candidate):
        source.rename(work.path / "detached-source")
        replacement.replace(source)
        pinned = source.stat()
        original(work, source, candidate)
        return pinned.st_dev, pinned.st_ino
    monkeypatch.setattr(api, "publish_no_replace", native)
    published, identity = paths.publish_output(work, source, "result", return_identity=True)
    committed = published.stat()
    assert identity == (committed.st_dev, committed.st_ino), "native operation identity was ignored"
    assert published.read_bytes() == b"new source"


@pytest.mark.skipif(os.name == "nt", reason="POSIX copy fallback")
def test_finding_f3_successful_copy_refuses_a_replaced_candidate(tmp_path, monkeypatch):
    paths = unsupported_links(monkeypatch)
    work = create_work_directory(tmp_path)
    source = work.path / "report.pdf"
    source.write_bytes(b"download")
    candidate = tmp_path / "report.pdf"
    replacement = tmp_path / "foreign.pdf"
    replacement.write_bytes(b"foreign")
    real_write = os.write
    def write(fd, chunk):
        candidate.unlink()
        os.link(replacement, candidate)
        return real_write(fd, chunk)  # Both write and fsync succeed.
    monkeypatch.setattr(os, "write", write)
    failure = None
    try:
        paths.publish_output(work, source, "report.pdf", allow_copy=True)
    except OutputPathError as exc:
        failure = exc
    assert isinstance(failure, OutputPathError), "F3: replacement was committed as owned output"
    assert source.read_bytes() == b"download"
    assert candidate.read_bytes() == b"foreign"


@pytest.mark.skipif(os.name == "nt", reason="POSIX fallback")
@pytest.mark.parametrize("code", [errno.EACCES, errno.ENOSPC, errno.EIO, errno.EXDEV])
def test_real_link_errors_never_trigger_copy(tmp_path, monkeypatch, code):
    import cove.output_paths as paths
    work = create_work_directory(tmp_path)
    source = work.path / "report.pdf"
    source.write_bytes(b"download")
    def fail(*args):
        raise OSError(code, "real link failure")
    monkeypatch.setattr(paths, "_link_pinned_fd", fail)
    monkeypatch.setattr(paths, "_copy_pinned_fd", lambda *args: pytest.fail("unsafe fallback"))
    with pytest.raises(OutputPathError):
        paths.publish_output(work, source, "report.pdf", allow_copy=True)
    assert source.read_bytes() == b"download"
    assert not (tmp_path / "report.pdf").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fallback")
def test_f7_safe_copy_unavailable_preserves_source(tmp_path, monkeypatch):
    paths = unsupported_links(monkeypatch)
    work = create_work_directory(tmp_path)
    source = work.path / "report.pdf"
    source.write_bytes(b"download")
    real_open = os.open
    def fail(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            raise OSError(errno.EOPNOTSUPP, "exclusive creation unsupported")
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", fail)
    # os.supports_dir_fd stores callable identities, including the replaced open.
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {fail})
    with pytest.raises(OutputPathError):
        paths.publish_output(work, source, "report.pdf", allow_copy=True)
    assert source.read_bytes() == b"download"
    assert not (tmp_path / "report.pdf").exists()


def test_adoption_captures_identity_and_rejects_changed_directory(tmp_path):
    from cove.output_paths import adopt_work_directory, cleanup_work_directory
    work = create_work_directory(tmp_path)
    adopted = adopt_work_directory(tmp_path, work.path)
    assert adopted == work
    detached = tmp_path / "detached"
    work.path.rename(detached)
    work.path.mkdir()
    (work.path / "foreign").write_bytes(b"foreign")
    with pytest.raises(OutputPathError):
        cleanup_work_directory(adopted)
    assert (work.path / "foreign").read_bytes() == b"foreign"


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner validation")
def test_adoption_rejects_another_uid(tmp_path, monkeypatch):
    from cove.output_paths import adopt_work_directory
    work = create_work_directory(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: work.path.stat().st_uid + 1)
    with pytest.raises(OutputPathError):
        adopt_work_directory(tmp_path, work.path)


def test_a_missing_engine_output_is_classified_separately(tmp_path):
    work = create_work_directory(tmp_path)
    missing = work.path / "video.mp4"

    with pytest.raises(MissingEngineOutputError) as excinfo:
        validate_engine_output(work, missing)

    assert isinstance(excinfo.value, OutputPathError)
    assert "does not exist" in str(excinfo.value)


def test_a_malformed_engine_output_path_is_still_invalid(tmp_path):
    work = create_work_directory(tmp_path)
    malformed = str(work.path / "video\x00.mp4")

    with pytest.raises(OutputPathError) as excinfo:
        validate_engine_output(work, malformed)

    assert not isinstance(excinfo.value, MissingEngineOutputError)
    assert "Invalid engine output path" in str(excinfo.value)


def test_a_missing_path_outside_the_work_directory_is_rejected_as_outside(tmp_path):
    work = create_work_directory(tmp_path)
    outside = tmp_path / "elsewhere" / "video.mp4"

    with pytest.raises(OutputPathError) as excinfo:
        validate_engine_output(work, outside)

    assert not isinstance(excinfo.value, MissingEngineOutputError)
    assert "outside its private directory" in str(excinfo.value)


def test_an_existing_regular_file_inside_the_work_directory_validates(tmp_path):
    work = create_work_directory(tmp_path)
    output = work.path / "video.mkv"
    output.write_bytes(b"payload")

    assert validate_engine_output(work, output) == output.resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_symlinked_engine_output_is_rejected(tmp_path):
    work = create_work_directory(tmp_path)
    real = tmp_path / "real.mkv"
    real.write_bytes(b"payload")
    link = work.path / "video.mkv"
    link.symlink_to(real)

    with pytest.raises(OutputPathError) as excinfo:
        validate_engine_output(work, link)

    assert not isinstance(excinfo.value, MissingEngineOutputError)
    assert "symlink" in str(excinfo.value)
