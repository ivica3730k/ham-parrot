"""rigctld PTT helper. ``hamlib_ptt(spec)`` is a context manager that
keys the radio on entry and releases on exit; ``spec=None`` is a no-op.
``read_ptt(spec)`` polls the current state so we can refuse to play
when the radio is already transmitting.
"""

from __future__ import annotations

import logging
import socket
import time
from contextlib import contextmanager
from typing import Iterator

from ham_parrot.keyer.constants import (
    HAMLIB_DEFAULT_PORT,
    HAMLIB_PTT_LEAD_SECONDS,
    HAMLIB_PTT_TAIL_SECONDS,
    HAMLIB_QUERY_TIMEOUT_SECONDS,
)
from ham_parrot.keyer.exceptions import ConfigError, PTTError

_log = logging.getLogger("ham_parrot.ptt")


def parse_endpoint(spec: str) -> tuple[str, int]:
    """``host``, ``host:port``, or ``:port`` -> (host, port). Bare host
    keeps the default port; bare ``:port`` keeps localhost."""
    host, sep, port_text = spec.partition(":")
    host = host or "localhost"
    if not sep or not port_text:
        return host, HAMLIB_DEFAULT_PORT
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ConfigError(f"invalid --hamlib-ptt port {port_text!r}") from exc
    return host, port


def _open(spec: str) -> socket.socket:
    host, port = parse_endpoint(spec)
    try:
        return socket.create_connection((host, port), timeout=HAMLIB_QUERY_TIMEOUT_SECONDS)
    except OSError as e:
        raise PTTError(f"rigctld connect {host}:{port} failed: {e}") from e


def read_ptt(spec: str | None) -> bool | None:
    """Query rigctld for PTT state. Returns True (on-air), False
    (idle), or None if the query is not applicable (``spec=None``) or
    rigctld cannot be reached / did not answer cleanly.

    We deliberately swallow network errors here: on a contest laptop
    with a flaky USB-serial adapter, refusing to key the parrot every
    time rigctld hiccups is worse than trusting the operator to not
    mash Enter mid-QSO.
    """
    if spec is None:
        return None
    try:
        sock = _open(spec)
    except PTTError as exc:
        _log.warning("PTT query: %s", exc)
        return None
    try:
        sock.sendall(b"t\n")
        sock.settimeout(HAMLIB_QUERY_TIMEOUT_SECONDS)
        buf = b""
        # rigctld terminates single-value replies with a newline.
        while b"\n" not in buf and len(buf) < 32:
            chunk = sock.recv(32)
            if not chunk:
                break
            buf += chunk
    except OSError as exc:
        _log.warning("PTT query recv failed: %s", exc)
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass

    reply = buf.decode(errors="replace").strip()
    if reply == "1":
        return True
    if reply == "0":
        return False
    _log.warning("PTT query: unexpected reply %r", reply)
    return None


@contextmanager
def hamlib_ptt(spec: str | None) -> Iterator[None]:
    """Key PTT on entry, release on exit. ``spec=None`` -> no-op."""
    if spec is None:
        yield
        return

    sock = _open(spec)
    try:
        try:
            sock.sendall(b"T 1\n")
        except OSError as e:
            raise PTTError(f"rigctld T 1 (key up) failed: {e}") from e
        _log.debug("hamlib PTT: keyed, waiting %.0f ms", HAMLIB_PTT_LEAD_SECONDS * 1000)
        time.sleep(HAMLIB_PTT_LEAD_SECONDS)
        yield
        _log.debug("hamlib PTT: holding tail %.0f ms", HAMLIB_PTT_TAIL_SECONDS * 1000)
        time.sleep(HAMLIB_PTT_TAIL_SECONDS)
    finally:
        try:
            sock.sendall(b"T 0\n")
            _log.debug("hamlib PTT: released")
        except OSError:
            _log.warning("hamlib PTT: release failed", exc_info=True)
        sock.close()
