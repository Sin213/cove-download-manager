"""Private output work-directory and publication primitives."""

from __future__ import annotations

import ctypes
import errno
import ntpath
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from ctypes import wintypes


_FILENAME_RESERVED_CHARS = '/\\:<>"|?*'
_WINDOWS_RESERVED_NAMES = frozenset(
    ("CON", "PRN", "AUX", "NUL")
    + tuple(f"COM{i}" for i in range(1, 10))
    + tuple(f"LPT{i}" for i in range(1, 10))
)


class OutputPathError(RuntimeError):
    """Raised when an engine output cannot be handled safely."""


class _WindowsApiError(OSError):
    """An error returned by the small Windows publication boundary."""

    def __init__(self, operation: str, winerror: int, subject: object):
        raw_winerror = int(winerror)
        super().__init__(
            raw_winerror,
            f"{operation} failed with Windows error {raw_winerror}: {subject}",
        )
        self.operation = operation
        self.winerror_code = raw_winerror
        self.subject = subject


class _WindowsByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("file_attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("last_access_time", wintypes.FILETIME),
        ("last_write_time", wintypes.FILETIME),
        ("volume_serial_number", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("number_of_links", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    ]


class _WindowsFileRenameInfoOptions(ctypes.Union):
    _fields_ = [
        ("replace_if_exists", wintypes.BOOLEAN),
        ("flags", wintypes.DWORD),
    ]


class _WindowsFileRenameInfo(ctypes.Structure):
    _anonymous_ = ("options",)
    _fields_ = [
        ("options", _WindowsFileRenameInfoOptions),
        ("root_directory", wintypes.HANDLE),
        ("file_name_length", wintypes.DWORD),
        ("file_name", ctypes.c_wchar * 1),
    ]


class _WindowsFileDispositionInfo(ctypes.Structure):
    _fields_ = [("delete_file", wintypes.BOOLEAN)]


class _WindowsFindData(ctypes.Structure):
    _fields_ = [
        ("file_attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("last_access_time", wintypes.FILETIME),
        ("last_write_time", wintypes.FILETIME),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("reserved0", wintypes.DWORD),
        ("reserved1", wintypes.DWORD),
        ("file_name", ctypes.c_wchar * 260),
        ("alternate_file_name", ctypes.c_wchar * 14),
    ]


_WINDOWS_ERROR_FILE_NOT_FOUND = 2
_WINDOWS_ERROR_PATH_NOT_FOUND = 3
_WINDOWS_ERROR_NO_MORE_FILES = 18
_WINDOWS_ERROR_FILE_EXISTS = 80
_WINDOWS_ERROR_INVALID_PARAMETER = 87
_WINDOWS_ERROR_ALREADY_EXISTS = 183
_WINDOWS_ERROR_INVALID_HANDLE = 6
_WINDOWS_ERROR_NOT_SAME_DEVICE = 17
_WINDOWS_ERROR_ACCESS_DENIED = 5
_WINDOWS_ERROR_SHARING_VIOLATION = 32
_WINDOWS_ERROR_LOCK_VIOLATION = 33
_WINDOWS_ERROR_DIR_NOT_EMPTY = 145
_WINDOWS_ERROR_INVALID_FUNCTION = 1
_WINDOWS_ERROR_NOT_SUPPORTED = 50
_WINDOWS_ERROR_CALL_NOT_IMPLEMENTED = 120
_WINDOWS_ERROR_DIRECTORY = 267
_WINDOWS_ERROR_CANT_ACCESS_FILE = 1920

_FILE_READ_DATA = 0x0001
_FILE_LIST_DIRECTORY = 0x0001
_FILE_ADD_FILE = 0x0002
_FILE_TRAVERSE = 0x0020
_FILE_READ_ATTRIBUTES = 0x0080
_DELETE = 0x00010000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_RENAME_INFO = 3
_FILE_DISPOSITION_INFO = 4


def _is_windows_runtime() -> bool:
    return os.name == "nt"


class _WindowsPublicationApi:
    """Handle-relative, no-replace publication for supported Windows builds."""

    def __init__(self):
        if not _is_windows_runtime():
            raise OutputPathError("Windows publication is unavailable on this platform")
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._create_file = kernel32.CreateFileW
            self._close_handle = kernel32.CloseHandle
            self._get_file_information = kernel32.GetFileInformationByHandle
            self._get_final_path = kernel32.GetFinalPathNameByHandleW
            self._set_file_information = kernel32.SetFileInformationByHandle
            self._find_first_file = kernel32.FindFirstFileW
            self._find_next_file = kernel32.FindNextFileW
            self._find_close = kernel32.FindClose
        except (AttributeError, OSError) as exc:
            raise OutputPathError("Windows atomic publication API is unavailable") from exc

        self._create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._create_file.restype = wintypes.HANDLE
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL
        self._get_file_information.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_WindowsByHandleFileInformation),
        ]
        self._get_file_information.restype = wintypes.BOOL
        self._get_final_path.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._get_final_path.restype = wintypes.DWORD
        self._set_file_information.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._set_file_information.restype = wintypes.BOOL
        self._find_first_file.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(_WindowsFindData),
        ]
        self._find_first_file.restype = wintypes.HANDLE
        self._find_next_file.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_WindowsFindData),
        ]
        self._find_next_file.restype = wintypes.BOOL
        self._find_close.argtypes = [wintypes.HANDLE]
        self._find_close.restype = wintypes.BOOL

    @staticmethod
    def _handle_value(handle) -> int:
        return int(handle.value if hasattr(handle, "value") else handle)

    @staticmethod
    def _invalid_handle_value() -> int:
        return int(ctypes.c_void_p(-1).value)

    def _raise_last_error(self, operation: str, subject: object) -> None:
        raise _WindowsApiError(operation, ctypes.get_last_error(), subject)

    def _open_handle(
        self,
        path: Path,
        access: int,
        *,
        share_mode: int = _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
    ) -> int:
        handle = self._create_file(
            str(path),
            access,
            share_mode,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        value = self._handle_value(handle)
        if value == self._invalid_handle_value():
            self._raise_last_error("open handle", path)
        return value

    def _close(self, handle: int | None) -> None:
        if handle is None:
            return
        self._close_handle(handle)

    def _file_information(self, handle: int, subject: object):
        info = _WindowsByHandleFileInformation()
        if not self._get_file_information(handle, ctypes.byref(info)):
            self._raise_last_error("read handle identity", subject)
        return info

    @staticmethod
    def _identity(info) -> tuple[int, int]:
        file_index = (int(info.file_index_high) << 32) | int(info.file_index_low)
        return int(info.volume_serial_number), file_index

    def _open_directory(
        self,
        path: Path,
        expected_identity: tuple[int, int] | None,
        *,
        share_mode: int = _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
    ) -> tuple[int, tuple[int, int]]:
        handle = self._open_handle(
            path,
            _FILE_LIST_DIRECTORY
            | _FILE_ADD_FILE
            | _FILE_TRAVERSE
            | _FILE_READ_ATTRIBUTES,
            share_mode=share_mode,
        )
        try:
            info = self._file_information(handle, path)
            if not (int(info.file_attributes) & _FILE_ATTRIBUTE_DIRECTORY):
                raise _WindowsApiError("destination directory validation", _WINDOWS_ERROR_DIRECTORY, path)
            if int(info.file_attributes) & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise _WindowsApiError(
                    "reparse-point validation",
                    _WINDOWS_ERROR_CANT_ACCESS_FILE,
                    path,
                )
            identity = self._identity(info)
            if expected_identity is not None and identity != expected_identity:
                raise _WindowsApiError(
                    "directory identity validation",
                    _WINDOWS_ERROR_INVALID_HANDLE,
                    path,
                )
            return handle, identity
        except BaseException:
            self._close(handle)
            raise

    def capture_directory_identity(self, path: Path) -> tuple[int, int]:
        handle, identity = self._open_directory(path, None)
        self._close(handle)
        return identity

    def _open_cleanup_directory(
        self,
        path: Path,
        expected_identity: tuple[int, int],
    ) -> int:
        handle = self._open_handle(
            path,
            _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _DELETE,
            share_mode=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
        )
        try:
            info = self._file_information(handle, path)
            attributes = int(info.file_attributes)
            if not (attributes & _FILE_ATTRIBUTE_DIRECTORY):
                raise _WindowsApiError("cleanup root validation", _WINDOWS_ERROR_DIRECTORY, path)
            if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise _WindowsApiError(
                    "cleanup root reparse-point validation",
                    _WINDOWS_ERROR_CANT_ACCESS_FILE,
                    path,
                )
            if self._identity(info) != expected_identity:
                raise _WindowsApiError(
                    "cleanup root identity validation",
                    _WINDOWS_ERROR_INVALID_HANDLE,
                    path,
                )
            return handle
        except BaseException:
            self._close(handle)
            raise

    def _enumerate_children(self, path: Path) -> list[str]:
        data = _WindowsFindData()
        find_handle = self._find_first_file(str(path / "*"), ctypes.byref(data))
        value = self._handle_value(find_handle)
        if value == self._invalid_handle_value():
            error = ctypes.get_last_error()
            if error in {_WINDOWS_ERROR_FILE_NOT_FOUND, _WINDOWS_ERROR_PATH_NOT_FOUND}:
                return []
            raise _WindowsApiError("enumerate cleanup directory", error, path)
        names = []
        try:
            while True:
                if data.file_name not in {".", ".."}:
                    names.append(data.file_name)
                if self._find_next_file(value, ctypes.byref(data)):
                    continue
                error = ctypes.get_last_error()
                if error == _WINDOWS_ERROR_NO_MORE_FILES:
                    return names
                raise _WindowsApiError("enumerate cleanup directory", error, path)
        finally:
            if not self._find_close(value):
                self._raise_last_error("cleanup child enumeration close", path)

    def _open_cleanup_child(self, path: Path) -> tuple[int, int] | None:
        try:
            handle = self._open_handle(
                path,
                _FILE_READ_ATTRIBUTES | _DELETE,
                share_mode=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
            )
        except _WindowsApiError as exc:
            if exc.winerror_code in {
                _WINDOWS_ERROR_FILE_NOT_FOUND,
                _WINDOWS_ERROR_PATH_NOT_FOUND,
            }:
                return None
            raise
        try:
            return handle, int(self._file_information(handle, path).file_attributes)
        except BaseException:
            self._close(handle)
            raise

    def _delete_handle(self, handle: int, subject: Path) -> None:
        disposition = _WindowsFileDispositionInfo(delete_file=1)
        if not self._set_file_information(
            handle,
            _FILE_DISPOSITION_INFO,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            self._raise_last_error("delete cleanup object", subject)

    def _delete_directory_contents(self, path: Path) -> None:
        for name in self._enumerate_children(path):
            child_path = path / name
            opened = self._open_cleanup_child(child_path)
            if opened is None:
                continue
            child_handle, attributes = opened
            try:
                if (
                    attributes & _FILE_ATTRIBUTE_DIRECTORY
                    and not attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    self._delete_directory_contents(child_path)
                self._delete_handle(child_handle, child_path)
            finally:
                self._close(child_handle)

    def _verify_cleanup_empty(self, path: Path) -> None:
        remaining = self._enumerate_children(path)
        if not remaining:
            return
        child_path = path / remaining[0]
        opened = self._open_cleanup_child(child_path)
        if opened is not None:
            self._close(opened[0])
        raise _WindowsApiError(
            "delete cleanup descendant",
            _WINDOWS_ERROR_DIR_NOT_EMPTY,
            child_path,
        )

    def pin_cleanup_root(self, work: "WorkDirectory") -> None:
        """Delete the exact validated non-reparse private directory tree."""

        if work.native_work_identity is None or work.native_destination_identity is None:
            raise _WindowsApiError(
                "directory identity validation",
                _WINDOWS_ERROR_INVALID_HANDLE,
                work.path,
            )
        destination_handle = None
        work_handle = None
        try:
            destination_handle, _ = self._open_directory(
                work.destination, work.native_destination_identity
            )
            work_handle = self._open_cleanup_directory(
                work.path, work.native_work_identity
            )
            self._delete_directory_contents(work.path)
            self._verify_cleanup_empty(work.path)
            self._delete_handle(work_handle, work.path)
        finally:
            self._close(work_handle)
            self._close(destination_handle)

    def _open_source(self, path: Path) -> int:
        handle = self._open_handle(path, _DELETE | _FILE_READ_ATTRIBUTES)
        try:
            info = self._file_information(handle, path)
            if int(info.file_attributes) & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise _WindowsApiError("source reparse-point validation", _WINDOWS_ERROR_CANT_ACCESS_FILE, path)
            if int(info.file_attributes) & _FILE_ATTRIBUTE_DIRECTORY:
                raise _WindowsApiError("source containment validation", _WINDOWS_ERROR_DIRECTORY, path)
            return handle
        except BaseException:
            self._close(handle)
            raise

    def _final_path(self, handle: int, subject: object) -> str:
        size = 32768
        for _ in range(2):
            buffer = ctypes.create_unicode_buffer(size)
            length = self._get_final_path(handle, buffer, size, 0)
            if length == 0:
                self._raise_last_error("final path validation", subject)
            if length < size:
                return buffer.value
            size = int(length) + 1
        raise _WindowsApiError("final path validation", _WINDOWS_ERROR_INVALID_HANDLE, subject)

    @staticmethod
    def _win32_path(final_path: str) -> str:
        """Return the Win32 path ``FILE_RENAME_INFO`` should carry.

        ``GetFinalPathNameByHandleW`` returns the extended-length ``\\\\?\\``
        form, and that form is kept verbatim.  Native probing showed the
        earlier no-clobber violation was caused by an undersized
        ``FILE_RENAME_INFO`` buffer (the header was sized from
        ``FileName.offset`` instead of ``ctypes.sizeof``), not by the prefix:
        with the correct buffer size the ``\\\\?\\`` form renames correctly and
        still reports collisions as ``ERROR_ALREADY_EXISTS``.  Keeping the
        prefix removes any dependence on ``LongPathsEnabled``, interpreter
        long-path awareness, or the application manifest, so publication to a
        destination beyond ``MAX_PATH`` works everywhere.
        """

        return final_path

    def _rename_no_replace(
        self, source_handle: int, destination_directory: str, candidate: str
    ) -> None:
        # SetFileInformationByHandle is a Win32 wrapper over NtSetInformationFile
        # that does not honour a RootDirectory handle: the relative form always
        # fails with ERROR_INVALID_PARAMETER.  RootDirectory must be NULL and
        # FileName must be the fully qualified destination path.  Containment is
        # still enforced by holding the validated destination directory handle
        # open without FILE_SHARE_DELETE for the duration of the rename.
        encoded_name = ntpath.join(destination_directory, candidate).encode("utf-16-le")
        name_offset = _WindowsFileRenameInfo.file_name.offset
        fixed_size = ctypes.sizeof(_WindowsFileRenameInfo)
        assert ctypes.sizeof(_WindowsFileRenameInfoOptions) == ctypes.sizeof(wintypes.DWORD)
        assert _WindowsFileRenameInfo.root_directory.offset % ctypes.alignment(wintypes.HANDLE) == 0
        assert name_offset == (
            _WindowsFileRenameInfo.file_name_length.offset + ctypes.sizeof(wintypes.DWORD)
        )
        assert fixed_size >= name_offset + ctypes.sizeof(ctypes.c_wchar)
        buffer = ctypes.create_string_buffer(fixed_size + len(encoded_name))
        info = ctypes.cast(buffer, ctypes.POINTER(_WindowsFileRenameInfo)).contents
        info.replace_if_exists = 0
        info.root_directory = None
        info.file_name_length = len(encoded_name)
        ctypes.memmove(ctypes.addressof(buffer) + name_offset, encoded_name, len(encoded_name))
        if not self._set_file_information(
            source_handle,
            _FILE_RENAME_INFO,
            ctypes.cast(buffer, ctypes.c_void_p),
            len(buffer),
        ):
            self._raise_last_error("rename", candidate)

    def publish_no_replace(self, work: "WorkDirectory", source_path: Path, candidate: str) -> None:
        if work.native_work_identity is None or work.native_destination_identity is None:
            raise _WindowsApiError(
                "directory identity validation",
                _WINDOWS_ERROR_INVALID_HANDLE,
                work.destination,
            )
        relative_source = _relative_to_work(source_path, work)
        work_handle = None
        destination_handle = None
        source_handle = None
        try:
            work_handle, work_identity = self._open_directory(work.path, work.native_work_identity)
            destination_handle, destination_identity = self._open_directory(
                work.destination,
                work.native_destination_identity,
                share_mode=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
            )
            if work_identity != work.native_work_identity or destination_identity != work.native_destination_identity:
                raise _WindowsApiError(
                    "directory identity validation",
                    _WINDOWS_ERROR_INVALID_HANDLE,
                    work.destination,
                )
            source_handle = self._open_source(source_path)
            expected_source = ntpath.join(self._final_path(work_handle, work.path), *relative_source.parts)
            actual_source = self._final_path(source_handle, source_path)
            if ntpath.normcase(ntpath.normpath(actual_source)) != ntpath.normcase(
                ntpath.normpath(expected_source)
            ):
                raise _WindowsApiError(
                    "source containment validation",
                    _WINDOWS_ERROR_CANT_ACCESS_FILE,
                    source_path,
                )
            destination_directory = self._win32_path(
                self._final_path(destination_handle, work.destination)
            )
            self._rename_no_replace(source_handle, destination_directory, candidate)
        finally:
            self._close(source_handle)
            self._close(destination_handle)
            self._close(work_handle)


def _windows_publication_api_factory() -> _WindowsPublicationApi:
    return _WindowsPublicationApi()


def _windows_output_error(exc: _WindowsApiError) -> OutputPathError:
    if (
        "reparse" in exc.operation
        or "containment" in exc.operation
        or exc.winerror_code == _WINDOWS_ERROR_CANT_ACCESS_FILE
    ):
        category = "reparse-point or containment failure"
    elif (
        "identity" in exc.operation
        or exc.winerror_code == _WINDOWS_ERROR_INVALID_HANDLE
    ):
        category = "invalid or replaced directory handle"
    elif exc.winerror_code in {
        _WINDOWS_ERROR_INVALID_FUNCTION,
        _WINDOWS_ERROR_NOT_SUPPORTED,
        _WINDOWS_ERROR_CALL_NOT_IMPLEMENTED,
    }:
        category = "unsupported filesystem or Windows API"
    elif exc.winerror_code == _WINDOWS_ERROR_NOT_SAME_DEVICE:
        category = "cross-device operation"
    elif exc.winerror_code in {
        _WINDOWS_ERROR_ACCESS_DENIED,
        _WINDOWS_ERROR_SHARING_VIOLATION,
        _WINDOWS_ERROR_LOCK_VIOLATION,
    }:
        category = "permission or sharing failure"
    else:
        category = "unexpected Windows API error"
    return OutputPathError(f"Windows {category} during {exc.operation}: {exc}")


@dataclass(frozen=True)
class WorkDirectory:
    path: Path
    destination: Path
    device: int
    inode: int
    destination_device: int
    destination_inode: int
    native_work_identity: tuple[int, int] | None = None
    native_destination_identity: tuple[int, int] | None = None


def _destination_path(destination: str | os.PathLike[str]) -> Path:
    raw_path = Path(destination)
    windows_api = None
    raw_identity = None
    if _is_windows_runtime():
        windows_api = _windows_publication_api_factory()
        try:
            raw_identity = windows_api.capture_directory_identity(raw_path)
        except _WindowsApiError as exc:
            raise _windows_output_error(exc) from exc
    path = raw_path.resolve(strict=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise OutputPathError(f"Destination is not a directory: {path}")
    if windows_api is not None:
        try:
            resolved_identity = windows_api.capture_directory_identity(path)
            if resolved_identity != raw_identity:
                raise _WindowsApiError(
                    "directory identity validation",
                    _WINDOWS_ERROR_INVALID_HANDLE,
                    path,
                )
        except _WindowsApiError as exc:
            raise _windows_output_error(exc) from exc
    return path


def create_work_directory(destination: str | os.PathLike[str]) -> WorkDirectory:
    """Create an owned, hidden work directory below ``destination``."""

    root = _destination_path(destination)
    try:
        work_path = Path(tempfile.mkdtemp(prefix=".cove-work-", dir=root))
    except OSError as exc:
        raise OutputPathError(f"Could not create private output directory: {exc}") from exc
    try:
        work_info = work_path.lstat()
        destination_info = root.stat()
        if not stat.S_ISDIR(work_info.st_mode) or work_info.st_dev != destination_info.st_dev:
            raise OutputPathError(f"Private output directory is not on the destination filesystem: {work_path}")
        native_work_identity = None
        native_destination_identity = None
        if _is_windows_runtime():
            api = _windows_publication_api_factory()
            native_destination_identity = api.capture_directory_identity(root)
            native_work_identity = api.capture_directory_identity(work_path)
        return WorkDirectory(
            work_path,
            root,
            work_info.st_dev,
            work_info.st_ino,
            destination_info.st_dev,
            destination_info.st_ino,
            native_work_identity,
            native_destination_identity,
        )
    except Exception as exc:
        try:
            work_path.rmdir()
        except OSError:
            pass
        if isinstance(exc, _WindowsApiError):
            raise _windows_output_error(exc) from exc
        raise


def _assert_owned(work: WorkDirectory) -> None:
    if _is_windows_runtime() and (
        work.native_work_identity is None or work.native_destination_identity is None
    ):
        raise OutputPathError("Validated Windows directory identity is unavailable")
    try:
        info = work.path.lstat()
    except FileNotFoundError:
        raise OutputPathError(f"Private output directory is missing: {work.path}") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_dev != work.device or info.st_ino != work.inode:
        raise OutputPathError(f"Private output directory ownership changed: {work.path}")
    try:
        destination_info = work.destination.stat()
    except FileNotFoundError:
        raise OutputPathError(f"Destination directory is missing: {work.destination}") from None
    if (
        not stat.S_ISDIR(destination_info.st_mode)
        or destination_info.st_dev != work.destination_device
        or destination_info.st_ino != work.destination_inode
    ):
        raise OutputPathError(f"Destination directory ownership changed: {work.destination}")


def _relative_to_work(path: Path, work: WorkDirectory) -> Path:
    try:
        relative = path.relative_to(work.path)
    except ValueError as exc:
        raise OutputPathError(f"Engine output is outside its private directory: {path}") from exc
    if not relative.parts:
        raise OutputPathError(f"Engine output is the private directory: {path}")
    return relative


def validate_engine_output(work: WorkDirectory, reported: str | os.PathLike[str]) -> Path:
    """Validate an engine-reported regular file strictly inside ``work``."""

    _assert_owned(work)
    if not reported:
        raise OutputPathError("Engine did not report a final output path")
    try:
        reported_path = Path(os.fspath(reported))
        lexical = Path(os.path.abspath(reported_path))
        _relative_to_work(lexical, work)
        current = work.path
        for part in lexical.relative_to(work.path).parts:
            current /= part
            if stat.S_ISLNK(current.lstat().st_mode):
                raise OutputPathError(f"Engine output contains a symlink: {reported_path}")
        resolved = reported_path.resolve(strict=True)
        _relative_to_work(resolved, work)
        info = resolved.stat()
    except OutputPathError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise OutputPathError(f"Invalid engine output path: {reported}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise OutputPathError(f"Engine output is not a regular file: {resolved}")
    return resolved


def _filename_component_length(value: str) -> int:
    if _is_windows_runtime():
        return len(value.encode("utf-16-le")) // 2
    return len(os.fsencode(value))


def _truncate_filename_component(value: str, limit: int) -> str:
    length = 0
    end = 0
    for end, char in enumerate(value, 1):
        char_length = _filename_component_length(char)
        if length + char_length > limit:
            return value[: end - 1]
        length += char_length
    return value[:end]


def validate_public_filename(filename: str | os.PathLike[str]) -> str:
    try:
        value = os.fspath(filename)
    except TypeError as exc:
        raise OutputPathError(f"Invalid public filename: {filename!r}") from exc
    if not isinstance(value, str):
        raise OutputPathError(f"Invalid public filename: {value!r}")
    # This is the canonical public filename contract used by publication and
    # API input validation.
    if not value or value in {".", ".."} or os.path.isabs(value):
        raise OutputPathError(f"Invalid public filename: {value!r}")
    try:
        component_length = _filename_component_length(value)
    except UnicodeError as exc:
        raise OutputPathError(f"Invalid public filename encoding: {value!r}") from exc
    if component_length > 255:
        raise OutputPathError(f"Public filename is too long: {value!r}")
    if any(char in value for char in _FILENAME_RESERVED_CHARS):
        raise OutputPathError(f"Public filename must be a basename: {value!r}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise OutputPathError(f"Public filename contains a control character: {value!r}")
    if value.endswith((" ", ".")):
        raise OutputPathError(f"Public filename has a trailing space or period: {value!r}")
    if value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise OutputPathError(f"Public filename is reserved by Windows: {value!r}")
    return value


def collision_candidates(filename: str | os.PathLike[str]):
    """Yield deterministic public collision candidates."""

    name = validate_public_filename(filename)
    stem, extension = os.path.splitext(name)
    yield name
    index = 1
    while True:
        suffix = f" ({index})"
        suffix_length = _filename_component_length(suffix)
        if suffix_length > 255:
            raise OutputPathError("Collision suffix exceeds the filename length limit")

        available = 255 - suffix_length
        if _filename_component_length(extension) <= available:
            candidate_stem = _truncate_filename_component(
                stem, available - _filename_component_length(extension)
            )
            candidate = f"{candidate_stem}{suffix}{extension}"
        else:
            candidate_extension = _truncate_filename_component(extension, available)
            candidate_extension = candidate_extension.rstrip(" .")
            candidate = f"{suffix}{candidate_extension}"
        yield validate_public_filename(candidate)
        index += 1


def _close_fd(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _open_owned_directory(path: Path, device: int, inode: int) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except (OSError, NotImplementedError) as exc:
        raise OutputPathError(f"Could not open owned output directory: {path}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_dev != device or info.st_ino != inode:
            raise OutputPathError(f"Output directory ownership changed: {path}")
        return fd
    except BaseException:
        _close_fd(fd)
        raise


def _open_owned_child_directory(
    parent_fd: int, name: str, device: int, inode: int
) -> int:
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except (OSError, NotImplementedError, TypeError) as exc:
        raise OutputPathError(f"Could not pin private output child: {name}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (device, inode):
            raise OutputPathError(f"Private output child identity changed: {name}")
        return fd
    except BaseException:
        _close_fd(fd)
        raise


def _remove_pinned_descendants(directory_fd: int) -> None:
    try:
        names = os.listdir(directory_fd)
    except (OSError, NotImplementedError, TypeError) as exc:
        raise OutputPathError("Could not enumerate pinned private output directory") from exc
    for name in names:
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(before.st_mode):
            try:
                child_fd = _open_owned_child_directory(
                    directory_fd, name, before.st_dev, before.st_ino
                )
            except OutputPathError:
                try:
                    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(current.st_mode):
                    os.unlink(name, dir_fd=directory_fd)
                    continue
                raise
            try:
                _remove_pinned_descendants(child_fd)
            finally:
                _close_fd(child_fd)
            try:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(current.st_mode):
                os.unlink(name, dir_fd=directory_fd)
            elif stat.S_ISDIR(current.st_mode) and (
                current.st_dev,
                current.st_ino,
            ) == (before.st_dev, before.st_ino):
                os.rmdir(name, dir_fd=directory_fd)
            else:
                raise OutputPathError(f"Private output child identity changed: {name}")
        else:
            os.unlink(name, dir_fd=directory_fd)


def _relative_link_supported() -> bool:
    return (
        getattr(os, "O_DIRECTORY", None) is not None
        and getattr(os, "O_NOFOLLOW", None) is not None
        and os.open in getattr(os, "supports_dir_fd", ())
    )


def _windows_link_candidate(work: WorkDirectory, source_path: Path, candidate: str) -> None:
    try:
        _windows_publication_api_factory().publish_no_replace(work, source_path, candidate)
    except _WindowsApiError as exc:
        if exc.operation == "rename" and exc.winerror_code in {
            _WINDOWS_ERROR_FILE_EXISTS,
            _WINDOWS_ERROR_ALREADY_EXISTS,
        }:
            collision = FileExistsError(errno.EEXIST, f"Public output already exists: {exc.subject}")
            collision.winerror = exc.winerror_code
            raise collision from exc
        raise _windows_output_error(exc) from exc


def _open_pinned_source(work: WorkDirectory, source_path: Path) -> int:
    if not _relative_link_supported():
        raise OutputPathError("Descriptor-relative publication is unsupported")
    relative_source = _relative_to_work(source_path, work)
    work_fd = _open_owned_directory(work.path, work.device, work.inode)
    source_fd = None
    try:
        try:
            source_fd = os.open(
                relative_source,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=work_fd,
            )
        except (OSError, NotImplementedError, TypeError) as exc:
            raise OutputPathError(f"Could not pin private output: {source_path}") from exc
        try:
            info = os.fstat(source_fd)
        except OSError as exc:
            raise OutputPathError(f"Could not validate pinned output: {source_path}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise OutputPathError(f"Pinned engine output is not a regular file: {source_path}")
        return source_fd
    except BaseException:
        _close_fd(source_fd)
        raise
    finally:
        _close_fd(work_fd)


def _link_pinned_fd(source_fd: int, destination_fd: int, candidate: str) -> None:
    """Hard-link an already pinned inode without resolving its former pathname."""

    source_reference = f"/proc/self/fd/{source_fd}"
    try:
        pinned_info = os.fstat(source_fd)
        reference_info = os.stat(source_reference)
    except OSError as exc:
        raise OutputPathError("Pinned descriptor reference is unavailable") from exc
    if (reference_info.st_dev, reference_info.st_ino) != (
        pinned_info.st_dev,
        pinned_info.st_ino,
    ):
        raise OutputPathError("Pinned descriptor reference does not identify the source")

    try:
        linkat = ctypes.CDLL(None, use_errno=True).linkat
    except (AttributeError, OSError) as exc:
        raise OutputPathError("Pinned-inode publication is unsupported") from exc
    linkat.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    linkat.restype = ctypes.c_int
    if (
        linkat(
            -100,  # AT_FDCWD
            os.fsencode(source_reference),
            destination_fd,
            os.fsencode(candidate),
            0x400,  # AT_SYMLINK_FOLLOW
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), candidate)


def _link_candidate(
    work: WorkDirectory,
    source_path: Path,
    candidate: str,
    *,
    source_fd: int | None = None,
) -> None:
    if _is_windows_runtime():
        _windows_link_candidate(work, source_path, candidate)
        return

    if source_fd is None:
        raise OutputPathError("Pinned-inode publication is unsupported")
    destination_fd = _open_owned_directory(
        work.destination,
        work.destination_device,
        work.destination_inode,
    )
    try:
        _link_pinned_fd(source_fd, destination_fd, candidate)
    finally:
        _close_fd(destination_fd)


def cleanup_work_directory(work: WorkDirectory) -> None:
    """Remove only the still-owned private work directory and its contents."""

    if _is_windows_runtime():
        try:
            _windows_publication_api_factory().pin_cleanup_root(work)
        except _WindowsApiError as exc:
            raise _windows_output_error(exc) from exc
        return
    if not _relative_link_supported():
        raise OutputPathError("Descriptor-relative cleanup is unsupported")
    destination_fd = _open_owned_directory(
        work.destination,
        work.destination_device,
        work.destination_inode,
    )
    work_fd = None
    try:
        work_fd = _open_owned_child_directory(
            destination_fd, work.path.name, work.device, work.inode
        )
        _remove_pinned_descendants(work_fd)
        current = os.stat(
            work.path.name, dir_fd=destination_fd, follow_symlinks=False
        )
        if stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (
            work.device,
            work.inode,
        ):
            os.rmdir(work.path.name, dir_fd=destination_fd)
        else:
            raise OutputPathError("Private output work directory identity changed")
    except FileNotFoundError:
        return
    finally:
        _close_fd(work_fd)
        _close_fd(destination_fd)


def publish_output(
    work: WorkDirectory,
    source: str | os.PathLike[str],
    requested_filename: str | os.PathLike[str],
) -> Path:
    """Publish a completed private file with atomic no-clobber semantics."""

    source_path = validate_engine_output(work, source)
    requested = validate_public_filename(requested_filename)
    source_fd = None if _is_windows_runtime() else _open_pinned_source(work, source_path)
    try:
        for candidate in collision_candidates(requested):
            try:
                _link_candidate(work, source_path, candidate, source_fd=source_fd)
            except FileExistsError:
                continue
            except OSError as exc:
                raise OutputPathError(f"Could not publish output without overwriting: {exc}") from exc
            break
        else:
            raise OutputPathError("No collision-free public output name is available")
    finally:
        _close_fd(source_fd)
    # A successful no-clobber publication is the publication commit point.  Cleanup is
    # best effort and must not turn a published task into a failed task.
    try:
        cleanup_work_directory(work)
    except (OSError, OutputPathError):
        pass
    return work.destination / candidate
