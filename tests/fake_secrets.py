"""Synthetic credentials for tests, assembled at run time.

Tests about credentials need values with a real credential's shape -- a GitLab
token's ``glpat-`` prefix, a JWT's three parts, a long random key -- because
the program recognises credentials by shape. Written as one literal, such a
value is indistinguishable from a leaked one: GitHub's push protection refused
the first push of this repository over a synthetic ``glpat-`` value in a test,
and secret scanners flag them all.

So no test writes one literally. :func:`fake` joins short pieces, none of which
is a credential on its own, and the test receives exactly the value it needs::

    token = fake("glpat-", "Q7xLm2Vd", "9kPa4RtZ", "w8Ns")

Every value built here is synthetic and must stay so: never paste a real
credential into a test, split or not.
"""

from __future__ import annotations


def fake(*pieces: str) -> str:
    """The synthetic credential made of ``pieces``, joined."""
    return "".join(pieces)
