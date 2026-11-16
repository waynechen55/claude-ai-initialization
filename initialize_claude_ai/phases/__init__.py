"""The four phases.

Importing this package registers every phase with :mod:`initialize_claude_ai.phases.base`,
which is what makes ``--phases 1,3`` / ``--phases 2-4`` / ``--phases all`` work
without the CLI knowing the phase list.
"""

from __future__ import annotations

from . import (  # noqa: F401
    phase1_install,
    phase2_authenticate,
    phase3_provision,
    phase4_mcp_auto_discovery,
)
from .base import Phase, all_phases, register, select

__all__ = ["Phase", "all_phases", "register", "select"]
