import fcntl
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator

from .models import FoundryError


def reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise FoundryError(f"Refusing to replace symlink: {path}")


def atomic_write(path: Path, text: str) -> None:
    reject_symlink(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        reject_symlink(path)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked(root: Path) -> Iterator[None]:
    reject_symlink(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / ".lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
