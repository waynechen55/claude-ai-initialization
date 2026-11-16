"""Writing files that may hold something private.

Every file this program writes for the operator -- environment files, Claude
Code's settings, their backups, the sign-in URL, ``.mcp.json`` -- can carry a
credential or a reference to one. Writing the text and *then* narrowing the
mode leaves a moment in which the file is readable by everyone the umask allows,
and a write that fails half-way leaves that readable copy behind. Here the file
is created ``0600`` before a byte is written, in the target's own directory so
the rename is atomic, and nothing is left behind on failure.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_private(path: Path, content: str | bytes, *, encoding: str = "utf-8", mode: int = 0o600) -> Path:
    """Replace ``path`` with ``content`` atomically, readable by its owner only (``mode``).

    Atomic for every file this program generates, not only the private ones: a
    bridge's operations manifest written with a plain ``write_text`` on a full
    disk was left *empty* -- truncated first, never filled -- and the next run
    judged the working bridge broken and replaced it. Here the content goes to a
    temporary file beside ``path`` and is renamed over it only once complete, so
    a failed write leaves the old file exactly as it was.
    """
    path = Path(path)
    data = content.encode(encoding) if isinstance(content, str) else content
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode != 0o600:
            temp.chmod(mode)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return path


#: Every file :func:`write_generated` wrote in this process -- for a verdict
#: taken before a file changed to be taken again (see McpWriter.retrial_rewritten).
REWRITTEN: set[str] = set()


def write_generated(path: Path, content: str | bytes, *, executable: bool = False) -> Path:
    """Write a file this program generates (a manifest, a bridge script) atomically, and remember it was rewritten."""
    written = write_private(Path(path), content, mode=0o700 if executable else 0o600)
    REWRITTEN.add(str(Path(path).resolve()))
    return written


__all__ = ["REWRITTEN", "write_generated", "write_private"]
