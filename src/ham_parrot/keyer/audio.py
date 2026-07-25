"""Audio I/O. Live via sounddevice or ``paplay`` / ``parec`` subprocess
for named Pulse endpoints (PortAudio's Pulse compat ignores ``PULSE_*``).
Device hints: integer index, name substring, or Pulse sink/source.

Ported from the ``weaklink-modem`` reference so the device-resolution
rules stay consistent across the two tools.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import sounddevice
import soundfile

from ham_parrot.keyer.exceptions import ConfigError

_log = logging.getLogger("ham_parrot.audio")


def write_wav(path: Path | str, samples: np.ndarray, sample_rate: int) -> None:
    """Write float32 mono samples to a WAV file."""

    soundfile.write(str(path), np.asarray(samples, dtype=np.float32), int(sample_rate))


def read_wav(path: Path | str, *, expected_sample_rate: int | None = None) -> tuple[np.ndarray, int]:
    """Read a WAV file, downmixing to mono if needed.

    Returns ``(samples_float32, sample_rate)``. Raises if
    ``expected_sample_rate`` is given and doesn't match.
    """

    data, sample_rate = soundfile.read(str(path), dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1).astype(np.float32)
    if expected_sample_rate is not None and int(expected_sample_rate) != int(sample_rate):
        raise ConfigError(
            f"WAV sample rate {sample_rate} Hz does not match expected {expected_sample_rate} Hz"
        )
    return data, int(sample_rate)


@dataclass
class AudioTarget:
    """Resolved audio endpoint. Exactly one of ``sd_index`` / ``pulse_name`` set."""

    sd_index: int | None = None
    pulse_name: str | None = None

    def describe(self) -> str:
        if self.pulse_name is not None:
            return f"pulse:{self.pulse_name}"
        if self.sd_index is not None:
            return f"sounddevice[{self.sd_index}]"
        return "default"


def _pactl_lookup_id(id_str: str, *, kind: str) -> str | None:
    """Resolve a numeric Pulse sink/source index to its name via ``pactl
    list short``. Returns None if pactl is missing, fails, or has no
    matching row."""
    if not shutil.which("pactl"):
        return None
    subcmd = "sources" if kind == "input" else "sinks"
    try:
        proc = subprocess.run(
            ["pactl", "list", "short", subcmd],
            capture_output=True, text=True, check=True, timeout=5.0,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _log.warning("pactl %s failed for id %s: %s", subcmd, id_str, exc)
        return None
    for line in proc.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) >= 2 and fields[0] == id_str:
            return fields[1]
    return None


def _resolve_pulse(ref: str, *, kind: str) -> AudioTarget:
    """Handle explicit ``pulse:<x>`` where ``<x>`` is either a Pulse
    sink/source name or a numeric index."""
    if not ref:
        return AudioTarget()
    if ref.lstrip("-").isdigit():
        resolved = _pactl_lookup_id(ref, kind=kind)
        if resolved is not None:
            _log.debug("pulse:%s -> %s (via pactl)", ref, resolved)
            return AudioTarget(pulse_name=resolved)
        _log.warning("no Pulse endpoint at index %s; passing raw", ref)
    return AudioTarget(pulse_name=ref)


def resolve_audio_target(name_hint: str | None, *, kind: str) -> AudioTarget:
    """Turn a user-supplied device hint into a concrete backend target.

    ``kind`` is ``"input"`` or ``"output"``. Exact logic matches the
    gist reference:

    1. ``pulse:<x>``  -> force the paplay / parec path (respected exactly).
    2. Numeric index -> pactl lookup; falls back to a sounddevice index.
    3. Substring match against a sounddevice enumerated device name.
    4. paplay / parec fall-through if the subprocess tool is on PATH.
    5. OS default (empty target).
    """
    if not name_hint:
        return AudioTarget()

    if name_hint.startswith("pulse:"):
        return _resolve_pulse(name_hint[len("pulse:") :], kind=kind)

    if name_hint.lstrip("-").isdigit():
        resolved = _pactl_lookup_id(name_hint, kind=kind)
        if resolved is not None:
            _log.debug("hint %s -> pulse:%s (via pactl)", name_hint, resolved)
            return AudioTarget(pulse_name=resolved)
        return AudioTarget(sd_index=int(name_hint))

    sd = sounddevice
    channel_attr = "max_input_channels" if kind == "input" else "max_output_channels"
    try:
        devices = sd.query_devices()
    except Exception:
        _log.debug("sounddevice.query_devices() failed while resolving %r", name_hint)
        devices = []

    hint_lower = name_hint.lower()
    for index, info in enumerate(devices):
        if info.get(channel_attr, 0) <= 0:
            continue
        name = str(info.get("name", "")).lower()
        if name in ("pulse", "pipewire", "default"):
            continue
        if hint_lower in name or name in hint_lower:
            _log.debug("device hint %r -> sounddevice %d %r", name_hint, index, info["name"])
            return AudioTarget(sd_index=index)

    tool = "parec" if kind == "input" else "paplay"
    if shutil.which(tool):
        _log.debug("device hint %r -> pulse subprocess (%s --device=%s)",
                   name_hint, tool, name_hint)
        return AudioTarget(pulse_name=name_hint)

    _log.warning(
        "device hint %r did not match any sounddevice %s device and %s "
        "is not on PATH; using OS default", name_hint, kind, tool,
    )
    return AudioTarget()


class LiveInputStream:
    """Uniform live-audio input over sounddevice or ``parec``.

    Context manager. Pushes 1-D float32 chunks to ``callback`` from a
    producer thread (parec) or PortAudio's callback thread (sounddevice).
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        block_frames: int,
        callback: Callable[[np.ndarray], None],
        target: AudioTarget,
    ) -> None:
        self._sample_rate = sample_rate
        self._block_frames = block_frames
        self._callback = callback
        self._target = target
        self._sd_stream = None  # type: ignore[assignment]
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def __enter__(self) -> "LiveInputStream":
        # Do NOT touch OS-side gain here; the operator sets input volume
        # on the audio interface directly (pavucontrol / hardware knob).
        if self._target.pulse_name is not None:
            self._open_parec()
        else:
            self._open_sounddevice()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop_event.set()
        if self._sd_stream is not None:
            try:
                self._sd_stream.close()
            except Exception:
                pass
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass

    def _open_sounddevice(self) -> None:
        sd = sounddevice

        def _sd_callback(indata: np.ndarray, _frames: int, _time: object, _status: object) -> None:
            self._callback(indata.reshape(-1).astype(np.float32, copy=False).copy())

        self._sd_stream = sd.InputStream(
            samplerate=self._sample_rate,
            channels=1,
            dtype="float32",
            device=self._target.sd_index,
            blocksize=self._block_frames,
            callback=_sd_callback,
        )
        self._sd_stream.start()

    def _open_parec(self) -> None:
        assert self._target.pulse_name is not None
        self._proc = subprocess.Popen(
            [
                "parec",
                f"--device={self._target.pulse_name}",
                "--format=float32le",
                f"--rate={self._sample_rate}",
                "--channels=1",
                "--raw",
                "--latency-msec=20",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        _log.info("parec pid=%d --device=%s", self._proc.pid, self._target.pulse_name)

        def _reader() -> None:
            chunk_bytes = self._block_frames * 4  # 4 bytes / float32
            assert self._proc is not None and self._proc.stdout is not None
            while not self._stop_event.is_set():
                raw = self._proc.stdout.read(chunk_bytes)
                if not raw:
                    # parec closed its stdout: exited or errored. Pick up
                    # stderr so the log shows the reason (bad device
                    # name, permission, etc.) instead of silent death.
                    rc = self._proc.poll() if self._proc is not None else None
                    err = b""
                    if self._proc is not None and self._proc.stderr is not None:
                        try:
                            err = self._proc.stderr.read() or b""
                        except OSError:
                            pass
                    if err:
                        _log.error("parec exited rc=%s: %s", rc, err.decode(errors="replace").strip())
                    elif rc is not None:
                        _log.error("parec exited rc=%s (no stderr output)", rc)
                    break
                self._callback(np.frombuffer(raw, dtype=np.float32).copy())

        self._thread = threading.Thread(target=_reader, name="ham-parrot-parec", daemon=True)
        self._thread.start()


class LiveOutputStream:
    """Uniform live-audio output over sounddevice or ``paplay``.

    Context manager. ``write()`` pushes 1-D float32 chunks; blocks until
    the backend accepts them.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        block_frames: int,
        target: AudioTarget,
    ) -> None:
        self._sample_rate = sample_rate
        self._block_frames = block_frames
        self._target = target
        self._sd_stream = None  # type: ignore[assignment]
        self._proc: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> "LiveOutputStream":
        # Do NOT touch OS-side gain here; the operator sets output volume
        # on the audio interface directly (pavucontrol / hardware knob).
        if self._target.pulse_name is not None:
            self._open_paplay()
        else:
            self._open_sounddevice()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._sd_stream is not None:
            try:
                self._sd_stream.stop()
                self._sd_stream.close()
            except Exception:
                pass
        if self._proc is not None:
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=1.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass

    def write(self, samples: np.ndarray) -> None:
        arr = np.asarray(samples, dtype=np.float32).reshape(-1)
        if self._sd_stream is not None:
            self._sd_stream.write(arr.reshape(-1, 1))
            return
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._proc.stdin.write(arr.tobytes())
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            _log.warning("paplay pipe closed: %s", exc)
            self._drain_stderr_on_death()

    def _drain_stderr_on_death(self) -> None:
        """If paplay has already exited, capture whatever it printed on
        stderr so we can log the actual failure reason instead of a
        generic BrokenPipeError."""
        if self._proc is None or self._proc.stderr is None:
            return
        rc = self._proc.poll()
        if rc is None:
            return
        try:
            err = self._proc.stderr.read() or b""
        except OSError:
            err = b""
        if err:
            _log.error("paplay exited rc=%d: %s", rc, err.decode(errors="replace").strip())
        else:
            _log.error("paplay exited rc=%d (no stderr output)", rc)

    def _open_sounddevice(self) -> None:
        sd = sounddevice
        self._sd_stream = sd.OutputStream(
            samplerate=self._sample_rate,
            channels=1,
            dtype="float32",
            device=self._target.sd_index,
            blocksize=self._block_frames,
        )
        self._sd_stream.start()

    def _open_paplay(self) -> None:
        assert self._target.pulse_name is not None
        # Capture stderr so we can log it if paplay dies. Silent stderr
        # was the reason a wrong / mis-permissioned device name looked
        # like "audio just went to the default sink" from the outside.
        self._proc = subprocess.Popen(
            [
                "paplay",
                f"--device={self._target.pulse_name}",
                "--format=float32le",
                f"--rate={self._sample_rate}",
                "--channels=1",
                "--raw",
                "--latency-msec=20",
            ],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        _log.info("paplay pid=%d --device=%s", self._proc.pid, self._target.pulse_name)


def broadcast(sinks: Iterable[LiveOutputStream], samples: np.ndarray) -> None:
    """Write the same block to every sink. Errors from one sink are
    logged and swallowed so the others keep running -- the monitor
    dying should not take the radio path down mid-QSO."""
    for sink in sinks:
        try:
            sink.write(samples)
        except Exception as exc:  # keep other sinks alive
            _log.warning("output sink write failed: %s", exc)


# Silence unused-import warnings for callers importing just symbol names.
_ = os
