"""Direct tests for the ``ham_parrot.keyer`` public API surface. Anything
tested here is part of the compatibility contract; changing the signature
or exception type these assert against is a breaking change.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from pathlib import Path

from ham_parrot.keyer import ConfigError, HamParrotError, PTTError
from ham_parrot.keyer.constants import (
    HAMLIB_DEFAULT_PORT,
    SAMPLE_RATE_HZ,
)
from ham_parrot.keyer.filter import EQ_BANDS_HZ, load_eq_json
from ham_parrot.keyer.ptt import parse_endpoint

_REPO_ROOT = Path(__file__).resolve().parent.parent


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


def test_eq_bands_are_iso_third_octave() -> None:
    # Renaming or reordering the band list is a breaking change to the
    # --eq-json schema; keep this assertion in lock-step with the docs.
    assert EQ_BANDS_HZ == (
        100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
        1000, 1250, 1600, 2000, 2500, 3150, 4000,
    )


def test_load_eq_json_flat_example() -> None:
    gains = load_eq_json(_REPO_ROOT / "eq_examples" / "flat.json")
    assert set(gains.keys()) == set(EQ_BANDS_HZ)
    assert all(v == 0.0 for v in gains.values())


def test_load_eq_json_ssb_example() -> None:
    gains = load_eq_json(_REPO_ROOT / "eq_examples" / "ssb.json")
    assert set(gains.keys()) == set(EQ_BANDS_HZ)
    # SSB curve is a voice-shaped notch: rolled-off lows, gentle presence lift.
    assert gains[100] < gains[1000]
    assert gains[2000] > gains[4000]


def test_load_eq_json_rejects_missing_band(tmp_path: Path) -> None:
    incomplete = tmp_path / "eq.json"
    incomplete.write_text('{"100": 0}')
    with pytest.raises(ConfigError, match="bands must be exactly"):
        load_eq_json(incomplete)


def test_load_eq_json_rejects_unknown_band(tmp_path: Path) -> None:
    bad = tmp_path / "eq.json"
    payload = ", ".join(f'"{f}": 0' for f in EQ_BANDS_HZ)
    bad.write_text("{" + payload + ', "9999": 0}')
    with pytest.raises(ConfigError, match="bands must be exactly"):
        load_eq_json(bad)


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
