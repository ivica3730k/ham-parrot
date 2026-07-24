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

#: PTT-to-audio guard. 100 ms of silence between PTT key-up and the
#: first playback / pilot sample -- covers relay settling and the
#: opening squelch tail on the receiving end.
HAMLIB_PTT_LEAD_SECONDS: float = 0.1

#: Symmetric tail: 100 ms of silence between the last playback sample
#: and PTT release so nothing gets clipped as the relay drops.
HAMLIB_PTT_TAIL_SECONDS: float = 0.1

#: Silence gap between the mode-flip (mic passthrough stops) and PTT
#: key-up. Lets residual mic audio drain from the radio-sink buffer
#: BEFORE the transmitter is keyed, so the "100 ms gap" after PTT-up
#: is real silence rather than the tail of the operator's last syllable
#: leaking on-air.
PRE_KEY_DRAIN_SECONDS: float = 0.1

#: Timeout for a single rigctld request/response cycle.
HAMLIB_QUERY_TIMEOUT_SECONDS: float = 1.0

# ---- CLI -----------------------------------------------------------------

#: Where CLI diagnostics land by default (kept out of stdout).
DEFAULT_LOG_PATH: Path = Path("log.txt")
