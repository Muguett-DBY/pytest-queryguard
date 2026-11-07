from __future__ import annotations

import os
import secrets
from pathlib import Path


def _create_temp(path: Path) -> tuple[int, str]:
    """A new file next to ``path``, created with mode 0666 less the umask.

    Not ``tempfile.mkstemp``: its file is owner-only (0600), and the files
    written here go into the repository and CI artifacts as a plain write
    would make them. The kernel applies the umask, so it is never changed.
    """
    for _ in range(100):
        tmp = str(path.parent / f"{path.name}.{secrets.token_hex(6)}.tmp")
        try:
            return os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666), tmp
        except FileExistsError:
            continue
    raise FileExistsError(f"no free temporary name next to {path}")


def atomic_write_text(path: Path, text: str) -> None:
    """Write UTF-8 text with ``\\n`` line ends; the file is never half written.

    A file that is replaced keeps its permission bits.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode: int | None = path.stat().st_mode & 0o777
    except OSError:
        mode = None
    fd, tmp = _create_temp(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
