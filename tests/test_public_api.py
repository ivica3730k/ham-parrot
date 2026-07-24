"""Direct tests for the ``ham_parrot.keyer`` public API surface. Anything
tested here is part of the compatibility contract; changing the signature
or exception type these assert against is a breaking change.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from ham_parrot.keyer import ConfigError, HamParrotError, PTTError
from ham_parrot.keyer.constants import (
    HAMLIB_DEFAULT_PORT,
    SAMPLE_RATE_HZ,
)
from ham_parrot.keyer.ptt import parse_endpoint


def test_exception_hierarchy() -> None:
    assert issubclass(ConfigError, HamParrotError)
    assert issubclass(PTTError, HamParrotError)


def test_parse_endpoint_bare_host_keeps_default_port() -> None:
    host, port = parse_endpoint("radio.local")
    assert host == "radio.local"
    assert port == HAMLIB_DEFAULT_PORT


def test_parse_endpoint_host_port_pair() -> None:
    host, port = parse_endpoint("radio.local:4533")
    assert host == "radio.local"
    assert port == 4533


def test_parse_endpoint_bare_port_falls_back_to_localhost() -> None:
    host, port = parse_endpoint(":4533")
    assert host == "localhost"
    assert port == 4533


def test_parse_endpoint_rejects_non_integer_port() -> None:
    with pytest.raises(ConfigError):
        parse_endpoint("radio.local:not-a-port")


def test_sample_rate_is_48khz() -> None:
    # 48 kHz is chosen so we skip resampling entirely -- catches
    # accidental drift to 44.1 kHz or 16 kHz.
    assert SAMPLE_RATE_HZ == 48000


def test_cli_help_smoke() -> None:
    # Import path used by GH Actions' `Check CLI help` step -- keep it working.
    proc = subprocess.run(
        [sys.executable, "-m", "ham_parrot.keyer.cli", "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0
    assert "ham-parrot" in proc.stdout
    assert "--mic-device" in proc.stdout
    assert "--hamlib-ptt" in proc.stdout
