"""Voice-keyer state machine.

Single mic input, one or two output sinks (radio + optional monitor), a
recording tap, and a playback source. One consumer thread owns the
transition between modes so audio blocks never race:

* PASSTHROUGH: mic block -> gain -> radio + monitor
* RECORDING:   mic block -> radio + monitor (still passthrough for
                natural monitoring) AND -> WAV writer
* PLAYBACK:    mic blocks discarded; WAV chunks -> gain -> radio +
                monitor, PTT keyed for the duration
* PILOT:       mic blocks discarded; 1 kHz sine -> gain -> radio +
                monitor, PTT keyed until user toggles off. Level knob
                is the same ``--recorder-out-level`` used for playback,
                so tuning it up during pilot carries over to the
                voice sequence.

Modes are switched by request-methods (``start_recording``, ``play``,
``toggle_pilot``); the mixer thread flips state at chunk boundaries.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile

from ham_parrot.keyer.audio import (
    AudioTarget,
    LiveInputStream,
    LiveOutputStream,
    read_wav,
)
from ham_parrot.keyer.constants import BLOCK_FRAMES, SAMPLE_RATE_HZ
from ham_parrot.keyer.exceptions import HamParrotError, PTTError
from ham_parrot.keyer.ptt import hamlib_ptt, read_ptt

_log = logging.getLogger("ham_parrot.keyer")

_MODE_PASSTHROUGH = "passthrough"
_MODE_RECORDING = "recording"
_MODE_PLAYBACK = "playback"
_MODE_PILOT = "pilot"

_PILOT_TONE_HZ = 1000.0


def _percent_to_gain(percent: float) -> float:
    """0-100 -> 0.0-1.0 linear gain."""
    return max(0.0, min(percent, 100.0)) / 100.0


class Keyer:
    """Owns audio streams, mode transitions, and the recording file."""

    def __init__(
        self,
        *,
        mic_target: AudioTarget,
        radio_target: AudioTarget,
        monitor_target: AudioTarget | None,
        recording_path: Path,
        mic_gain: float,
        recorder_out_gain: float,
        monitor_gain: float,
        ptt_spec: str | None,
    ) -> None:
        self._mic_target = mic_target
        self._radio_target = radio_target
        self._monitor_target = monitor_target
        self._recording_path = recording_path
        self._mic_gain = mic_gain
        self._recorder_out_gain = recorder_out_gain
        self._monitor_gain = monitor_gain
        self._ptt_spec = ptt_spec

        self._mic_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=64)
        self._mode = _MODE_PASSTHROUGH
        self._mode_lock = threading.Lock()
        self._stop_event = threading.Event()

        # Recording-side state (writer opened when we enter RECORDING).
        self._recorder: soundfile.SoundFile | None = None

        # Playback / pilot request signalling.
        self._playback_requested = threading.Event()
        self._pilot_toggle_requested = threading.Event()

        # Sinks are opened lazily inside run().
        self._radio_sink: LiveOutputStream | None = None
        self._monitor_sink: LiveOutputStream | None = None

    # ---- Public control surface ------------------------------------------

    def has_recording(self) -> bool:
        return self._recording_path.exists()

    def recording_path(self) -> Path:
        return self._recording_path

    def is_recording(self) -> bool:
        with self._mode_lock:
            return self._mode == _MODE_RECORDING

    def is_pilot(self) -> bool:
        with self._mode_lock:
            return self._mode == _MODE_PILOT

    def toggle_recording(self) -> str:
        """Start recording if idle, stop if already recording. Returns a
        short human-readable status for the CLI to print."""
        with self._mode_lock:
            if self._mode == _MODE_RECORDING:
                self._mode = _MODE_PASSTHROUGH
                self._close_recorder()
                return f"recording saved -> {self._recording_path}"
            if self._mode != _MODE_PASSTHROUGH:
                return f"cannot record while {self._mode}"
            self._open_recorder()
            self._mode = _MODE_RECORDING
            return "recording started (press 'r' again to stop)"

    def request_playback(self) -> str:
        """Trigger playback of the recorded WAV. Refuses if the radio is
        externally on-air, if no recording exists, or if we're not idle."""
        if not self._recording_path.exists():
            return f"no recording at {self._recording_path} -- press 'r' to record first"
        with self._mode_lock:
            if self._mode != _MODE_PASSTHROUGH:
                return f"cannot play while {self._mode}"
        ext = read_ptt(self._ptt_spec)
        if ext is True:
            return "radio is already transmitting -- refusing to play"
        self._playback_requested.set()
        return "playing recording..."

    def toggle_pilot(self) -> str:
        """Start or stop the level-set pilot tone."""
        with self._mode_lock:
            if self._mode == _MODE_PILOT:
                # Ask the mixer thread to end the pilot cleanly.
                self._pilot_toggle_requested.set()
                return "pilot stopping..."
            if self._mode != _MODE_PASSTHROUGH:
                return f"cannot start pilot while {self._mode}"
        ext = read_ptt(self._ptt_spec)
        if ext is True:
            return "radio is already transmitting -- refusing to start pilot"
        self._pilot_toggle_requested.set()
        return "pilot tone on (press 'p' again to stop)"

    def stop(self) -> None:
        self._stop_event.set()

    # ---- Main loop -------------------------------------------------------

    def run(self) -> None:
        """Open streams and run the mixer until ``stop()`` is called."""
        monitor_ctx: Optional[LiveOutputStream] = None
        with (
            LiveInputStream(
                sample_rate=SAMPLE_RATE_HZ,
                block_frames=BLOCK_FRAMES,
                callback=self._on_mic_block,
                target=self._mic_target,
            ),
            LiveOutputStream(
                sample_rate=SAMPLE_RATE_HZ,
                block_frames=BLOCK_FRAMES,
                target=self._radio_target,
            ) as radio_sink,
        ):
            self._radio_sink = radio_sink
            try:
                if self._monitor_target is not None:
                    monitor_ctx = LiveOutputStream(
                        sample_rate=SAMPLE_RATE_HZ,
                        block_frames=BLOCK_FRAMES,
                        target=self._monitor_target,
                    )
                    monitor_ctx.__enter__()
                    self._monitor_sink = monitor_ctx
                self._mixer_loop()
            finally:
                if monitor_ctx is not None:
                    monitor_ctx.__exit__(None, None, None)
                self._radio_sink = None
                self._monitor_sink = None
                self._close_recorder()

    # ---- Mic-side capture ------------------------------------------------

    def _on_mic_block(self, samples: np.ndarray) -> None:
        # PortAudio callback thread. Never block here; drop on backpressure.
        try:
            self._mic_q.put_nowait(samples)
        except queue.Full:
            _log.warning("mic queue overflow -- dropping %d frames", samples.size)

    # ---- Mixer thread (main thread inside run()) -------------------------

    def _mixer_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._playback_requested.is_set():
                self._playback_requested.clear()
                self._drain_mic_queue()
                self._run_playback()
                continue
            if self._pilot_toggle_requested.is_set():
                self._pilot_toggle_requested.clear()
                if self._mode == _MODE_PILOT:
                    # Already-in-flight toggle is handled by _run_pilot() exit.
                    continue
                self._drain_mic_queue()
                self._run_pilot()
                continue

            try:
                block = self._mic_q.get(timeout=0.05)
            except queue.Empty:
                continue

            self._forward_mic_block(block)

    def _forward_mic_block(self, block: np.ndarray) -> None:
        self._emit(block, self._mic_gain)
        # Snapshot mode + recorder under lock so a toggle mid-block can't
        # tear the WAV file. Recording captures the raw pre-gain mic
        # signal, so ``--mic-level`` doesn't bake into the WAV.
        with self._mode_lock:
            recording = self._mode == _MODE_RECORDING
            recorder = self._recorder
        if recording and recorder is not None:
            try:
                recorder.write(block.astype(np.float32, copy=False))
            except Exception as exc:
                _log.warning("recorder write failed: %s", exc)

    def _emit(self, source: np.ndarray, radio_gain: float) -> None:
        """Scale ``source`` independently for each sink and write it out.
        The monitor gets ``--monitor-level`` regardless of what the radio
        gets, so the operator can turn the local monitor down without
        changing mic drive / recorded-audio drive to the radio."""
        if self._radio_sink is not None:
            try:
                self._radio_sink.write(source * radio_gain)
            except Exception as exc:
                _log.warning("radio sink write failed: %s", exc)
        if self._monitor_sink is not None:
            try:
                self._monitor_sink.write(source * self._monitor_gain)
            except Exception as exc:
                _log.warning("monitor sink write failed: %s", exc)

    def _drain_mic_queue(self) -> None:
        """Discard any pending mic blocks. Called just before entering a
        mode where we don't want the mic on-air (playback, pilot)."""
        while True:
            try:
                self._mic_q.get_nowait()
            except queue.Empty:
                return

    # ---- Playback --------------------------------------------------------

    def _run_playback(self) -> None:
        try:
            samples, rate = read_wav(self._recording_path, expected_sample_rate=SAMPLE_RATE_HZ)
        except HamParrotError as exc:
            _log.error("cannot read %s: %s", self._recording_path, exc)
            return
        _log.info("playback: %d frames @ %d Hz", samples.size, rate)

        with self._mode_lock:
            self._mode = _MODE_PLAYBACK
        try:
            with hamlib_ptt(self._ptt_spec):
                for start in range(0, samples.size, BLOCK_FRAMES):
                    if self._stop_event.is_set():
                        break
                    chunk = samples[start : start + BLOCK_FRAMES]
                    self._emit(chunk, self._recorder_out_gain)
        except PTTError as exc:
            _log.error("playback aborted: %s", exc)
        finally:
            with self._mode_lock:
                self._mode = _MODE_PASSTHROUGH

    # ---- Pilot -----------------------------------------------------------

    def _run_pilot(self) -> None:
        """Emit a 1 kHz sine at ``recorder_out_gain`` until the user hits
        'p' again (which sets ``_pilot_toggle_requested``)."""
        phase_step = 2.0 * np.pi * _PILOT_TONE_HZ / SAMPLE_RATE_HZ
        phase = 0.0

        with self._mode_lock:
            self._mode = _MODE_PILOT
        try:
            with hamlib_ptt(self._ptt_spec):
                while not self._stop_event.is_set() and not self._pilot_toggle_requested.is_set():
                    t = phase + phase_step * np.arange(BLOCK_FRAMES, dtype=np.float64)
                    block = np.sin(t).astype(np.float32)
                    phase = (phase + phase_step * BLOCK_FRAMES) % (2.0 * np.pi)
                    self._emit(block, self._recorder_out_gain)
        except PTTError as exc:
            _log.error("pilot aborted: %s", exc)
        finally:
            self._pilot_toggle_requested.clear()
            with self._mode_lock:
                self._mode = _MODE_PASSTHROUGH

    # ---- Recorder handle -------------------------------------------------

    def _open_recorder(self) -> None:
        # Overwrite any prior recording. Contest workflow: record once,
        # play many times.
        self._recorder = soundfile.SoundFile(
            str(self._recording_path),
            mode="w",
            samplerate=SAMPLE_RATE_HZ,
            channels=1,
            subtype="FLOAT",
        )

    def _close_recorder(self) -> None:
        if self._recorder is not None:
            try:
                self._recorder.close()
            except Exception as exc:
                _log.warning("recorder close failed: %s", exc)
            self._recorder = None


# CLI convenience: build + run a keyer with parsed args, converting
# level percentages into gains here so ``Keyer`` sees only floats.
def build_keyer(
    *,
    mic_target: AudioTarget,
    radio_target: AudioTarget,
    monitor_target: AudioTarget | None,
    recording_path: Path,
    mic_level_percent: float,
    recorder_out_level_percent: float,
    monitor_level_percent: float,
    ptt_spec: str | None,
) -> Keyer:
    return Keyer(
        mic_target=mic_target,
        radio_target=radio_target,
        monitor_target=monitor_target,
        recording_path=recording_path,
        mic_gain=_percent_to_gain(mic_level_percent),
        recorder_out_gain=_percent_to_gain(recorder_out_level_percent),
        monitor_gain=_percent_to_gain(monitor_level_percent),
        ptt_spec=ptt_spec,
    )


# Silence unused-import warnings.
_ = time
