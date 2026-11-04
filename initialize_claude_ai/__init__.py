"""initialize-claude-ai -- a phased initializer for a Claude AI environment.

Four phases, deliberately mixed-mode:

1. ``install``    traditional: packages, binaries, SDKs, verification
2. ``auth``       traditional: Google Cloud, claude.ai OAuth, Anthropic API key
3. ``provision``  traditional: backend, model, region, settings, inventory
4. ``mcp``        agentic (deterministic last resort): MCP discovery & setup

Phases 1-3 cannot be agentic -- they are what *builds* the environment an agent
would need.  Phase 4 runs once that environment exists, so it uses it.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
