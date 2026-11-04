"""Guards for state that addresses worked at the same time have in common.

``--concurrency`` runs several addresses through one pipeline object on
separate threads. Almost everything a stage touches belongs to its own
:class:`~.types.TargetOutcome`, so it needs no guard at all; what is left is a
short list of things that outlive one address -- the agent workspace, a
prerequisite that two addresses install, the control answer for an origin that
two addresses probe. Each of those is keyed by *what* is shared (a path, a
command line, an origin), never by which server happens to be sharing it, so a
server nobody has heard of is covered exactly like the ones that exposed the
problem.

A run of one address at a time takes every one of these locks uncontended,
which costs nothing and changes nothing.
"""

from __future__ import annotations

from ..core.locks import KeyedLocks

#: Install steps (argument vectors) run for a trial. Two addresses that settle
#: on the same package -- one issue tracker's server serving two instances --
#: would otherwise run the same ``pip install`` or ``go install`` into the same
#: place at the same moment.
INSTALL_STEPS = KeyedLocks()

__all__ = ["INSTALL_STEPS", "KeyedLocks"]
