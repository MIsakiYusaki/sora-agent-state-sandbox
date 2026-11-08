"""Cross-process fence shared by normal startup and offline erasure."""
import os
from pathlib import Path


def acquire_profile_lock(directory: Path):
    directory.parent.mkdir(parents=True, exist_ok=True)
    path = directory.parent / (directory.name + ".runtime.lock")
    file = path.open("a+b")
    try:
        file.seek(0, os.SEEK_END)
        if file.tell() == 0:
            file.write(b"\0")
            file.flush()
        file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return file
    except BaseException:
        file.close()
        raise ValueError("profile is already open in another runtime or erasure") from None
