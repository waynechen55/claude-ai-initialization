"""Phase 1 machinery: the component catalogue, the installers and the verifier.

The distro-specific knowledge lives in ``initialize_claude_ai/data/10-components.toml``,
not here -- this package only knows *kinds* of installation (system package,
brew, flatpak, snap, npm, pip, uv tool, script, download, command, manual).
"""

from __future__ import annotations

from .components import ComponentSpec, load_components, select_components
from .installers import Installer, InstallOutcome
from .verify import CheckResult, Verifier, version_at_least

__all__ = [
    "ComponentSpec",
    "load_components",
    "select_components",
    "Installer",
    "InstallOutcome",
    "Verifier",
    "CheckResult",
    "version_at_least",
]
