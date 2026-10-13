"""Explicit local file inputs and non-overwriting publication of verified artifacts."""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack, suppress
from pathlib import Path

from beans_picker.errors import ToolError


def output_path(raw: str, *, suffix: str | None = None) -> Path:
    path = Path(raw)
    if not path.is_absolute() or not path.name or (suffix is not None and path.suffix.lower() != suffix):
        raise ToolError("bad_path", "give an absolute output path with the required extension")
    if not path.parent.is_dir() or path.parent.resolve() != path.parent:
        raise ToolError("bad_path", "the output parent must be an existing canonical directory")
    if path.exists() or path.is_symlink():
        raise ToolError("output_exists", "the output path already exists; nothing was overwritten")
    return path


def input_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or path.resolve() != path:
        raise ToolError("bad_path", "input files must be absolute canonical paths without symlinks")
    try:
        info = path.lstat()
    except OSError as err:
        raise ToolError("bad_path", "the input file is unavailable") from err
    if not stat.S_ISREG(info.st_mode):
        raise ToolError("bad_path", "the input must be a regular file")
    return path


def _directory_fd(path: Path) -> int:
    """Pin a canonical absolute directory without following any symlink ancestor."""
    if not path.is_absolute() or ".." in path.parts:
        raise ToolError("bad_path", "artifact directories must be absolute canonical paths")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def publish(source: Path, out: Path, *, expected_bytes: int | None = None) -> int:
    """Publish an owner-only verified inode without replacement through pinned directory descriptors."""
    with ExitStack() as stack:
        try:
            source_dir = _directory_fd(source.parent)
            stack.callback(os.close, source_dir)
            out_dir = _directory_fd(out.parent)
            stack.callback(os.close, out_dir)
            fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_dir)
            stack.callback(os.close, fd)
        except OSError as err:
            raise ToolError("unverified_output", "the completed output file could not be read safely") from err
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ToolError("unsafe_driver_output", "the staged output is not a regular single-link file")
        if expected_bytes is not None and info.st_size != expected_bytes:
            raise ToolError("unverified_output", "the output size does not match the completed operation")
        os.fchmod(fd, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise ToolError("unsafe_driver_output", "the staged output changed before publication")
        if expected_bytes is not None and info.st_size != expected_bytes:
            raise ToolError("unverified_output", "the output size changed before publication")
        # Pin the pathname inode separately so failed verification removes only our newly linked inode.
        linked = os.stat(source.name, dir_fd=source_dir, follow_symlinks=False)
        try:
            os.link(source.name, out.name, src_dir_fd=source_dir, dst_dir_fd=out_dir, follow_symlinks=False)
        except FileExistsError as err:
            raise ToolError("output_exists", "the output path already exists; nothing was overwritten") from err
        published = os.stat(out.name, dir_fd=out_dir, follow_symlinks=False)
        current = os.fstat(fd)
        if (
            _identity(published) != _identity(info)
            or not stat.S_ISREG(published.st_mode)
            or published.st_size != info.st_size
            or current.st_size != info.st_size
            or current.st_nlink != 2
            or stat.S_IMODE(published.st_mode) != 0o600
            or stat.S_IMODE(current.st_mode) != 0o600
        ):
            with suppress(FileNotFoundError):
                # A swapped source can also have been the inode we linked; do not delete an unrelated
                # destination substituted after link. A hardlink still at the source proves ownership.
                source_now = os.stat(source.name, dir_fd=source_dir, follow_symlinks=False)
                owned = {_identity(info), _identity(linked), _identity(source_now)}
                if _identity(published) in owned and _identity(
                    os.stat(out.name, dir_fd=out_dir, follow_symlinks=False)
                ) == _identity(published):
                    os.unlink(out.name, dir_fd=out_dir)
            raise ToolError("unsafe_driver_output", "the published output is not the verified staged inode")
        return info.st_size
