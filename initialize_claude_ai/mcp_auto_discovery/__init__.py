"""Phase 4: MCP auto-discovery, agentic with a deterministic safety net.

Discovery is a Claude subagent, and stays one: a stage that does not settle on
its first answer is retried with exactly what went wrong, on another runtime or
another model when the failure was in the machinery rather than the answer. The
traditional pipeline is what an operator chooses with ``--no-agent``, and what
an exhausted stage falls through to. That works only because the two are
interchangeable -- same stages, same
:class:`~initialize_claude_ai.mcp_auto_discovery.types.Candidate` objects, same
verifier -- so a stage can switch mid-run without losing what has already been
learned, and each records in its
:class:`~initialize_claude_ai.mcp_auto_discovery.types.Mode` which of the two
actually did the work.
"""

from __future__ import annotations

from .agentic import AgenticPipeline
from .probes import McpProber
from .ranking import ASPECTS, CandidateRanker, Score
from .traditional import TraditionalPipeline
from .types import Candidate, Handshake, Mode, Stage, StageOutcome, TargetOutcome, Transport
from .verifier import CHECKS, CandidateVerifier, Check
from .writer import McpWriter, Registration

__all__ = [
    "ASPECTS",
    "AgenticPipeline",
    "TraditionalPipeline",
    "CandidateRanker",
    "CandidateVerifier",
    "CHECKS",
    "Check",
    "Score",
    "McpProber",
    "McpWriter",
    "Registration",
    "Candidate",
    "Handshake",
    "Mode",
    "Stage",
    "StageOutcome",
    "TargetOutcome",
    "Transport",
]
