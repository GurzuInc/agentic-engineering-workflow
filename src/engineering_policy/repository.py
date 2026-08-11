from __future__ import annotations

import os
import secrets
import shutil
import stat
import subprocess
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from engineering_policy.constants import (
    MANAGED_EXACT_OUTPUTS,
    MANAGED_OUTPUT_PREFIXES,
    PROTECTED_CONSUMER_PATHS,
)
from engineering_policy.errors import PolicyError

_GIT_TIMEOUT_SECONDS = 30
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "clock$",
    "con",
    "conin$",
    "conout$",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def require_git_repository(repo: Path) -> Path:
    repo = repo.resolve()
    git = shutil.which("git")
    if git is None:
        raise PolicyError("Git is required for consumer repository operations")
    try:
        result = subprocess.run(  # noqa: S603 - absolute executable resolved from fixed name
            [git, "-C", str(repo), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise PolicyError("Git repository discovery timed out") from exc
    except OSError as exc:
        raise PolicyError("Git repository discovery could not be started") from exc
    if result.returncode != 0:
        raise PolicyError(f"consumer is not a Git repository: {repo}")
    root = Path(result.stdout.strip()).resolve()
    if root != repo:
        raise PolicyError(f"--repo must name the Git root: expected {root}")
    return root


def require_clean_worktree(repo: Path) -> None:
    git = shutil.which("git")
    if git is None:
        raise PolicyError("Git is required for consumer repository operations")
    try:
        result = subprocess.run(  # noqa: S603 - absolute executable resolved from fixed name
            [git, "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise PolicyError("Git worktree inspection timed out") from exc
    except OSError as exc:
        raise PolicyError("Git worktree inspection could not be started") from exc
    if result.returncode != 0:
        raise PolicyError("Git worktree inspection failed")
    if result.stdout:
        raise PolicyError("consumer worktree must be clean before init or update")


def is_managed_output(path: PurePosixPath) -> bool:
    return path in MANAGED_EXACT_OUTPUTS or any(
        path.is_relative_to(prefix) for prefix in MANAGED_OUTPUT_PREFIXES
    )


def validate_relative_path(raw: str) -> PurePosixPath:
    if not isinstance(raw, str) or "\\" in raw or "\x00" in raw or "//" in raw:
        raise PolicyError(f"unsafe consumer path: {raw!r}")
    if unicodedata.normalize("NFC", raw) != raw:
        raise PolicyError(f"consumer path is not Unicode NFC: {raw!r}")
    raw_parts = raw.split("/")
    if not raw or any(part in {"", ".", ".."} for part in raw_parts):
        raise PolicyError(f"unsafe consumer path: {raw!r}")
    for part in raw_parts:
        _validate_portable_segment(part, raw)
    path = PurePosixPath(raw)
    if path.is_absolute():
        raise PolicyError(f"unsafe consumer path: {raw!r}")
    return path


def atomic_write(repo: Path, relative: PurePosixPath, content: bytes, mode: int = 0o644) -> None:
    if not is_managed_output(relative):
        raise PolicyError(f"refusing to write outside managed allowlist: {relative}")
    with _open_relative_parent(repo, relative, create=True) as (parent_fd, name):
        _atomic_replace_at(parent_fd, name, content, mode, str(relative))


def atomic_write_protected(repo: Path, relative: PurePosixPath, content: bytes) -> None:
    if relative not in PROTECTED_CONSUMER_PATHS:
        raise PolicyError(f"refusing protected write outside bootstrap allowlist: {relative}")
    with _open_relative_parent(repo, relative, create=True) as (parent_fd, name):
        _atomic_replace_at(parent_fd, name, content, 0o644, str(relative))


def remove_managed_file(repo: Path, relative: PurePosixPath) -> None:
    if not is_managed_output(relative):
        raise PolicyError(f"refusing to remove outside managed allowlist: {relative}")
    with _open_relative_parent(repo, relative, create=False) as (parent_fd, name):
        try:
            mode = os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISREG(mode):
            raise PolicyError(f"managed path is not a regular file: {relative}")
        os.unlink(name, dir_fd=parent_fd)


def atomic_write_path(path: Path, content: bytes, mode: int = 0o644) -> None:
    """Atomically write an arbitrary build output without following path symlinks."""
    absolute = path.absolute()
    _validate_portable_segment(absolute.name, str(path))
    with _open_directory(absolute.parent, create=True) as parent_fd:
        _atomic_replace_at(parent_fd, absolute.name, content, mode, str(path))


def read_regular_file(
    repo: Path,
    relative: PurePosixPath,
    *,
    label: str,
    maximum: int,
    missing_ok: bool = False,
) -> bytes | None:
    """Read a bounded regular file without following any path component."""
    relative = validate_relative_path(relative.as_posix())
    if maximum < 0:
        raise ValueError("maximum must be non-negative")
    try:
        with _open_relative_parent(repo, relative, create=False) as (parent_fd, name):
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                if missing_ok:
                    return None
                raise PolicyError(f"{label} is missing: {relative}") from None
            except OSError as exc:
                raise PolicyError(f"{label} could not be read securely: {relative}") from exc
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise PolicyError(f"{label} is not a regular file: {relative}")
                if metadata.st_size > maximum:
                    raise PolicyError(f"{label} exceeds the allowed size: {relative}")
                content = bytearray()
                while True:
                    chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(content)))
                    if not chunk:
                        return bytes(content)
                    content.extend(chunk)
                    if len(content) > maximum:
                        raise PolicyError(f"{label} exceeds the allowed size: {relative}")
            finally:
                os.close(descriptor)
    except PolicyError as exc:
        if str(exc).startswith("managed directory is missing:"):
            if missing_ok:
                return None
            raise PolicyError(f"{label} is missing: {relative}") from exc
        raise


def enumerate_regular_files(repo: Path, root: PurePosixPath, *, label: str) -> set[str]:
    """List a relative tree using stable directory handles and rejecting special files."""
    root = validate_relative_path(root.as_posix())
    with _open_relative_parent(repo, root, create=False) as (parent_fd, name):
        try:
            root_fd = _open_child_directory(parent_fd, name, create=False)
        except PolicyError as exc:
            raise PolicyError(f"{label} is missing or unsafe: {root}") from exc
        try:
            return _enumerate_directory(root_fd, PurePosixPath(), label)
        finally:
            os.close(root_fd)


def _validate_portable_segment(part: str, raw: str) -> None:
    if any(ord(character) < 32 for character in part):
        raise PolicyError(f"consumer path contains a control character: {raw!r}")
    if ":" in part or part.endswith((".", " ")):
        raise PolicyError(f"consumer path is not portable: {raw!r}")
    device_name = part.split(".", 1)[0].casefold()
    if device_name in _WINDOWS_RESERVED_NAMES:
        raise PolicyError(f"consumer path uses a reserved device name: {raw!r}")


def _directory_flags() -> int:
    if os.name != "posix" or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise PolicyError("secure filesystem operations are not supported on this platform")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def _open_directory(path: Path, *, create: bool) -> Iterator[int]:
    absolute = path.absolute()
    anchor = Path(absolute.anchor)
    descriptors = [os.open(anchor, _directory_flags())]
    try:
        for part in absolute.parts[1:]:
            _validate_portable_segment(part, str(path))
            descriptors.append(_open_child_directory(descriptors[-1], part, create=create))
        yield descriptors[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def _open_relative_parent(
    repo: Path, relative: PurePosixPath, *, create: bool
) -> Iterator[tuple[int, str]]:
    descriptors: list[int] = []
    try:
        directory_context = _open_directory(repo, create=False)
        root_fd = directory_context.__enter__()
        descriptors.append(root_fd)
        for part in relative.parts[:-1]:
            descriptors.append(_open_child_directory(descriptors[-1], part, create=create))
        yield descriptors[-1], relative.name
    finally:
        for descriptor in reversed(descriptors[1:]):
            os.close(descriptor)
        if descriptors:
            directory_context.__exit__(None, None, None)


def _open_child_directory(parent_fd: int, name: str, *, create: bool) -> int:
    try:
        return os.open(name, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise PolicyError(f"managed directory is missing: {name}") from None
        try:
            os.mkdir(name, 0o755, dir_fd=parent_fd)
        except FileExistsError:
            pass
        try:
            return os.open(name, _directory_flags(), dir_fd=parent_fd)
        except OSError as exc:
            _raise_directory_open_error(parent_fd, name, exc)
    except OSError as exc:
        _raise_directory_open_error(parent_fd, name, exc)


def _raise_directory_open_error(parent_fd: int, name: str, cause: OSError) -> None:
    try:
        mode = os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
    except OSError:
        mode = 0
    if stat.S_ISLNK(mode):
        raise PolicyError(f"managed directory has a symlinked path component: {name}") from cause
    raise PolicyError(f"managed directory is unsafe: {name}") from cause


def _enumerate_directory(directory_fd: int, prefix: PurePosixPath, label: str) -> set[str]:
    found: set[str] = set()
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        raise PolicyError(f"{label} could not be enumerated securely") from exc
    for name in names:
        _validate_portable_segment(name, (prefix / name).as_posix())
        relative = prefix / name
        try:
            mode = os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode
        except OSError as exc:
            raise PolicyError(f"{label} changed during enumeration: {relative}") from exc
        if stat.S_ISDIR(mode):
            try:
                child_fd = _open_child_directory(directory_fd, name, create=False)
            except PolicyError as exc:
                raise PolicyError(f"{label} contains an unsafe directory: {relative}") from exc
            try:
                found.update(_enumerate_directory(child_fd, relative, label))
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(mode):
            found.add(relative.as_posix())
        elif stat.S_ISLNK(mode):
            raise PolicyError(f"{label} contains a symlink: {relative}")
        else:
            raise PolicyError(f"{label} contains a non-regular file: {relative}")
    return found


def _atomic_replace_at(parent_fd: int, name: str, content: bytes, mode: int, label: str) -> None:
    try:
        existing_mode = os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        existing_mode = None
    if existing_mode is not None and not stat.S_ISREG(existing_mode):
        raise PolicyError(f"destination is not a regular file: {label}")

    temporary_name = ""
    handle = -1
    for _attempt in range(10):
        temporary_name = f".policyctl-{secrets.token_hex(12)}.tmp"
        try:
            handle = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                mode,
                dir_fd=parent_fd,
            )
            break
        except FileExistsError:
            continue
    if handle < 0:
        raise PolicyError(f"could not allocate a secure temporary file for {label}")
    try:
        with os.fdopen(handle, "wb") as stream:
            handle = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.rename(temporary_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary_name = ""
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
    finally:
        if handle >= 0:
            os.close(handle)
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
