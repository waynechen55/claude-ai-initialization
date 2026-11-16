"""Where the files a written MCP entry launches live -- beside the file that names them.

Generated bridges, manifests and CA bundles were kept in this program's state
folder. Copying a project's ``.mcp.json`` to another machine took the entries
and left their files behind: gitlab-example-net was pointed by hand at an older
script that read another variable, and its token never reached GitLab. So
``mcp.registration.artifacts_home`` may be relative -- the default is
``mcp-servers`` -- and is then taken beside ``mcp.mcp_json_path``: the files
travel with the file, and ``ICA_MCP_SERVERS`` points the entries at them on
another machine (phase 1, ``install.machine_paths``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

#: Fallback for ``mcp.registration.artifacts_home``, equal to the packaged value.
DEFAULT_ARTIFACTS_HOME = "mcp-servers"


def artifacts_home(settings: Any) -> Path | None:
    """The folder written entries' files live in, absolute; None when not configured."""
    raw = str(settings.str_("mcp.registration.artifacts_home", DEFAULT_ARTIFACTS_HOME) or "").strip()
    if not raw:
        return None
    home = Path(os.path.expanduser(raw))
    if not home.is_absolute():
        json_path = Path(os.path.expanduser(str(settings.str_("mcp.mcp_json_path", ".mcp.json") or ".mcp.json")))
        base = json_path.parent if json_path.is_absolute() else (Path.cwd() / json_path).parent
        home = base / home
    return home.resolve()


def collapsed(parts: Any) -> list[str]:
    """Path parts with a folder repeated in a row folded into one (``smelt-example-net/smelt-example-net`` → one)."""
    out: list[str] = []
    for part in parts:
        if not out or part != out[-1]:
            out.append(part)
    return out


def server_destination(home: Path, slug: str, source: Path) -> Path:
    """Where ``source`` belongs in a server's own folder ``home/slug`` -- the one rule every mover uses.

    The server's own folder *is* ``home/slug``, never a folder nested in it:
    the writer moved files that way, while the copy restored from what last
    worked computed its own path and made ``smelt-example-net/smelt-example-net``.
    """
    if source == home or home in source.parents:
        return home.joinpath(*collapsed(source.relative_to(home).parts))
    return home / slug if source.name == slug else home / slug / source.name


__all__ = ["artifacts_home", "collapsed", "server_destination"]
