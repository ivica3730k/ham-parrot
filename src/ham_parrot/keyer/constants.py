"""Shared module-level constants."""

from __future__ import annotations

from pathlib import Path

# ---- Audio ---------------------------------------------------------------

#: Sample rate for every stream (mic in, radio out, monitor out, recording).
#: 48 kHz is the native rate for most USB audio codecs; picking one rate
#: end-to-end sidesteps resampling entirely.
SAMPLE_RATE_HZ: int = 48000

#: Frames per audio block. ~20 ms at 48 kHz -- low enough for live speech
#: passthrough, big enough to survive scheduler jitter.
BLOCK_FRAMES: int = 960

# ---- Recording -----------------------------------------------------------

#: Voice sequence file. Persists between runs so a contest reboot doesn't
#: cost you the recording.
RECORDING_PATH: Path = Path("recording.wav")

# ---- Hamlib rigctld PTT --------------------------------------------------

#: rigctld TCP default; matches the ``--hamlib-ptt`` bare-flag default.
HAMLIB_DEFAULT_PORT: int = 4532

#: PTT-to-audio guard. Radios need a small delay between key-up and
#: first sample or the leading audio gets clipped by relay / AGC settling.
HAMLIB_PTT_LEAD_SECONDS: float = 0.15

#: Symmetric tail: hold PTT past the last sample so the trailing audio
#: makes it onto the air before the relay drops.
HAMLIB_PTT_TAIL_SECONDS: float = 0.15

#: Timeout for a single rigctld request/response cycle.
HAMLIB_QUERY_TIMEOUT_SECONDS: float = 1.0

# ---- CLI -----------------------------------------------------------------

#: Where CLI diagnostics land by default (kept out of stdout).
DEFAULT_LOG_PATH: Path = Path("log.txt")
