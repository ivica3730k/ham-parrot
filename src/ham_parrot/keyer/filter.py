"""Radio-side audio filtering: mandatory 100 Hz - 4 kHz bandpass +
optional 17-band peaking EQ on the ISO 1/3-octave centres.

The filter runs only on the radio-bound path (mic passthrough,
playback, pilot). Recording is captured before the filter, and the
monitor sink is fed raw so the operator can hear what the mic sounds
like uncoloured.

State is per-instance -- create separate ``RadioFilter`` instances if
you need independent state per stream. In practice this codebase runs
a single instance for the whole radio path (mic passthrough and
playback/pilot are mutually exclusive, so continuity across mode
transitions is fine and avoids a click on switch).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

from ham_parrot.keyer.exceptions import ConfigError

_log = logging.getLogger("ham_parrot.filter")

#: ISO 1/3-octave centre frequencies within the 100 Hz - 4 kHz passband.
#: 17 bands. Changing this tuple is a breaking change to the --eq-json
#: schema; add a new list and gate it behind a flag if you need a
#: different band set.
EQ_BANDS_HZ: tuple[int, ...] = (
    100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
    1000, 1250, 1600, 2000, 2500, 3150, 4000,
)

#: Q for the peaking EQ biquads. Picked empirically so a uniform
#: N dB setting across all 17 bands produces ~N dB of overall
#: response instead of ~4N dB of stacking. Broader Q (e.g. 1.4)
#: makes neighbours' skirts overlap so every biquad's cut lands on
#: every other biquad's centre and the attenuation multiplies -- at
#: Q≈1.4 a "flat -6 dB" curve delivers about -23 dB, which sounds
#: silent. Q=8 keeps each biquad tight enough that the bands act
#: mostly independently.
_EQ_Q: float = 8.0

#: Order of the Butterworth bandpass. 4 SOS stages per band edge
#: => ~48 dB / octave rolloff, tight enough that anything past ~5 kHz
#: is inaudible on SSB.
_BANDPASS_ORDER: int = 4

_BANDPASS_LOW_HZ: float = 100.0
_BANDPASS_HIGH_HZ: float = 4000.0


def _peaking_biquad_sos(f0: float, gain_db: float, sample_rate: int, Q: float = _EQ_Q) -> np.ndarray:
    """RBJ audio-EQ cookbook peaking biquad. Returns a single SOS row
    ``[b0, b1, b2, 1, a1, a2]`` (already normalised by a0).
    """
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * f0 / sample_rate
    alpha = np.sin(w0) / (2.0 * Q)
    cos_w0 = np.cos(w0)
    b0 = 1.0 + alpha * A
    b1 = -2.0 * cos_w0
    b2 = 1.0 - alpha * A
    a0 = 1.0 + alpha / A
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha / A
    return np.array([b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0], dtype=np.float64)


def load_eq_json(path: Path | str) -> dict[int, float]:
    """Load a ``{freq: dB}`` JSON file. The keys MUST be exactly the
    17 frequencies in :data:`EQ_BANDS_HZ` (as strings or integers).
    Missing bands or unknown bands raise :class:`ConfigError` -- this
    is deliberate: silently defaulting an omitted band to 0 dB hides
    typos and half-finished configs.
    """
    try:
        raw = Path(path).read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read --eq-json {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"--eq-json {path}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"--eq-json {path}: top level must be a JSON object")

    expected: set[str] = {str(f) for f in EQ_BANDS_HZ}
    keys_as_str: dict[str, object] = {str(k): v for k, v in data.items()}
    missing = expected - keys_as_str.keys()
    extra = keys_as_str.keys() - expected
    if missing or extra:
        raise ConfigError(
            f"--eq-json {path}: bands must be exactly "
            f"{sorted(EQ_BANDS_HZ)} (missing={sorted(int(f) for f in missing)}, "
            f"extra={sorted(extra)})"
        )

    result: dict[int, float] = {}
    for freq_hz in EQ_BANDS_HZ:
        raw_val = keys_as_str[str(freq_hz)]
        try:
            result[freq_hz] = float(raw_val)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"--eq-json {path}: value for {freq_hz} must be a number, got {raw_val!r}"
            ) from exc
    return result


def build_radio_sos(
    sample_rate: int,
    *,
    eq_gains_db: dict[int, float] | None = None,
) -> np.ndarray:
    """Build the SOS matrix for the whole radio-side filter chain:
    Butterworth bandpass 100 Hz - 4 kHz followed by, when
    ``eq_gains_db`` is given, one peaking biquad per band with the
    specified boost / cut. Bands with 0 dB gain are skipped for CPU.
    """
    stages: list[np.ndarray] = [
        butter(_BANDPASS_ORDER, [_BANDPASS_LOW_HZ, _BANDPASS_HIGH_HZ],
               btype="band", fs=sample_rate, output="sos"),
    ]
    if eq_gains_db:
        for freq_hz in EQ_BANDS_HZ:
            gain_db = eq_gains_db[freq_hz]
            if abs(gain_db) < 1e-6:
                continue
            stages.append(_peaking_biquad_sos(float(freq_hz), gain_db, sample_rate)[np.newaxis, :])
    sos = np.vstack(stages)
    _log.debug("radio filter: %d SOS sections (%d bandpass + %d EQ)",
               sos.shape[0], stages[0].shape[0], sos.shape[0] - stages[0].shape[0])
    return sos


class RadioFilter:
    """Stateful cascade filter for one audio stream. Not thread-safe;
    only call :meth:`apply` from a single thread (in this codebase, the
    mixer thread)."""

    def __init__(self, sos: np.ndarray) -> None:
        self._sos = sos
        self._zi = sosfilt_zi(sos)
        self._primed = False

    def apply(self, block: Iterable[float]) -> np.ndarray:
        arr = np.asarray(block, dtype=np.float64).reshape(-1)
        if arr.size == 0:
            return arr.astype(np.float32, copy=False)
        if not self._primed:
            # Scale the steady-state IC by the first sample so we don't
            # start with a big DC step through the filter and produce a
            # transient thump.
            self._zi = self._zi * arr[0]
            self._primed = True
        out, self._zi = sosfilt(self._sos, arr, zi=self._zi)
        return out.astype(np.float32, copy=False)
