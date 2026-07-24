"""Public exception hierarchy for ham_parrot.keyer.

Library callers catch these; the CLI wraps them into SystemExit(2)
with the message rendered as ``error: <message>`` so shell users see
a clean line instead of a traceback.
"""

from __future__ import annotations


class HamParrotError(Exception):
    """Base for anything the keyer raises. Catch this to catch them all."""


class ConfigError(HamParrotError):
    """Invalid configuration -- level out of range, PTT endpoint malformed, etc."""


class PTTError(HamParrotError):
    """rigctld connect / response failure."""
