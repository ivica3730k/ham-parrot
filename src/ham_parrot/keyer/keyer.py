"""Voice-keyer state machine.

Single mic input, a radio output sink (always on), an optional monitor
output sink that lives only while playback / pilot is active, a
recording tap, and a playback source. One consumer thread owns the
transition between modes so audio blocks never race:

* PASSTHROUGH: mic block -> gain -> radio. Monitor is silent.
* RECORDING:   mic block -> WAV writer only. NOT routed to the radio
                (avoids microphony via the operator's monitor chain).
* PLAYBACK:    mic blocks discarded; WAV chunks -> gain -> radio and
                (freshly opened) monitor. PTT keyed for the duration
                with a ``PRE_KEY_DRAIN_SECONDS`` silence gap before
                key-up and ``HAMLIB_PTT_TAIL_SECONDS`` after the last
                sample so nothing is clipped as the relay drops.
* PILOT:       mic blocks discarded; 1 kHz sine -> gain -> radio and
                monitor, PTT keyed until user toggles off. Level knob
                is the same ``--playback-level`` used for playback,
                so tuning it up during pilot carries over to the voice
                sequence.

The monitor sink is opened + closed inside ``_run_playback`` and
``_run_pilot``. Keeping it always-open left residual audio in paplay's
ring buffer that felt like the monitor "kept running" after playback
finished.

Modes are switched by request-methods (``toggle_recording``,
``request_playback``, ``toggle_pilot``); the mixer thread flips state
at chunk boundaries.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import numpy as np
import soundfile

from ham_parrot.keyer.audio import (
    AudioTarget,
    LiveInputStream,
    LiveOutputStream,
    read_wav,
)
from ham_parrot.keyer.constants import (
    BLOCK_FRAMES,
    CLIP_WARN_INTERVAL_SECONDS,
    MAX_LEVEL_PERCENT,
    MIC_CLIP_THRESHOLD,
    PRE_KEY_DRAIN_SECONDS,
    SAMPLE_RATE_HZ,
)
from ham_parrot.keyer.exceptions import HamParrotError, PTTError
from ham_parrot.keyer.filter import RadioFilter, build_radio_sos
from ham_parrot.keyer.ptt import hamlib_ptt, read_ptt

_log = logging.getLogger("ham_parrot.keyer")

_MODE_PASSTHROUGH = "passthrough"
_MODE_RECORDING = "recording"
_MODE_PLAYBACK = "playback"
_MODE_PILOT = "pilot"

_PILOT_TONE_HZ = 1000.0


def _percent_to_gain(percent: float) -> float:
    """0-MAX_LEVEL_PERCENT -> 0.0-(MAX_LEVEL_PERCENT/100) linear gain.
    100 = unity. Above 100 is boost; the mixer hard-clips at ±1.0
    before writing to the sinks so overshoots don't wrap into artefacts.
    """
    return max(0.0, min(percent, MAX_LEVEL_PERCENT)) / 100.0


class Keyer:
    """Owns audio streams, mode transitions, and the recording file."""

    def __init__(
        self,
        *,
        mic_target: AudioTarget,
        radio_target: AudioTarget,
        monitor_target: AudioTarget | None,
        recording_path: Path,
        mic_passthrough_gain: float,
        playback_gain: float,
        monitor_gain: float,
        ptt_spec: str | None,
        eq_gains_db: dict[int, float] | None = None,
    ) -> None:
        self._mic_target = mic_target
        self._radio_target = radio_target
        self._monitor_target = monitor_target
        self._recording_path = recording_path
        self._mic_passthrough_gain = mic_passthrough_gain
        self._playback_gain = playback_gain
        self._monitor_gain = monitor_gain
        self._ptt_spec = ptt_spec

        # Radio-side filter: 100 Hz - 4 kHz bandpass, optional peaking EQ.
        # State is continuous across passthrough / playback / pilot -- avoids
        # a click at the switch, and the modes are mutually exclusive so
        # there is no per-stream contention.
        sos = build_radio_sos(SAMPLE_RATE_HZ, eq_gains_db=eq_gains_db)
        self._radio_filter = RadioFilter(sos)

        # Rate-limits the mic-clip warning so a hot mic doesn't flood stdout.
        self._last_clip_warn_time: float = 0.0

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
        """Open the mic input + radio output sinks and run the mixer until
        ``stop()`` is called. The monitor sink is opened per playback /
        pilot session inside ``_run_playback`` / ``_run_pilot``."""
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
                self._mixer_loop()
            finally:
                self._radio_sink = None
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
        # Warn (rate-limited) if the raw mic hits the ADC ceiling. Check
        # runs before gain so we're flagging the actual capture chain,
        # not our own boost.
        peak = float(np.max(np.abs(block))) if block.size else 0.0
        if peak >= MIC_CLIP_THRESHOLD:
            now = time.monotonic()
            if now - self._last_clip_warn_time >= CLIP_WARN_INTERVAL_SECONDS:
                print(f"mic clipping (peak={peak:.3f}) -- back off the mic or lower input gain")
                self._last_clip_warn_time = now

        # Snapshot mode + recorder under lock so a toggle mid-block can't
        # tear the WAV file.
        with self._mode_lock:
            recording = self._mode == _MODE_RECORDING
            recorder = self._recorder

        # In RECORDING mode the mic is captured to WAV and NOT routed to
        # the radio. Otherwise the operator's voice comes back through
        # the radio's monitor / speaker while they're speaking, loops
        # into the mic acoustically, and produces the "microphony" howl.
        if not recording:
            self._write_to_radio(block, self._mic_passthrough_gain)

        if recording and recorder is not None:
            # Recording captures the raw pre-gain, pre-filter mic signal
            # so ``--mic-passthrough-level`` and the radio bandpass / EQ
            # do not bake into the WAV file.
            try:
                recorder.write(block.astype(np.float32, copy=False))
            except Exception as exc:
                _log.warning("recorder write failed: %s", exc)

    def _emit(self, source: np.ndarray, radio_gain: float) -> None:
        """Scale ``source`` independently for each sink and write it out.
        Used for playback + pilot (on-air content); mic passthrough uses
        its own radio-only path so the monitor never carries mic audio.

        The radio path is bandpass-filtered (and EQ'd when configured);
        the monitor gets the raw source so the operator hears what the
        transmit chain sounds like before filtering.
        """
        self._write_to_radio(source, radio_gain)
        if self._monitor_sink is not None:
            monitor_out = np.asarray(source, dtype=np.float32) * self._monitor_gain
            np.clip(monitor_out, -1.0, 1.0, out=monitor_out)
            try:
                self._monitor_sink.write(monitor_out)
            except Exception as exc:
                _log.warning("monitor sink write failed: %s", exc)

    def _write_to_radio(self, source: np.ndarray, gain: float) -> None:
        """Common radio-write path: gain, then filter (bandpass + EQ),
        then hard-clip at ±1.0, then write. All three call-sites
        (passthrough, playback, pilot) go through here so the chain is
        applied uniformly."""
        if self._radio_sink is None:
            return
        audio = np.asarray(source, dtype=np.float32) * gain
        audio = self._radio_filter.apply(audio)
        np.clip(audio, -1.0, 1.0, out=audio)
        try:
            self._radio_sink.write(audio)
        except Exception as exc:
            _log.warning("radio sink write failed: %s", exc)

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
            print(f"playback failed: {exc}")
            return
        _log.info("playback: %d frames @ %d Hz", samples.size, rate)

        with self._mode_lock:
            self._mode = _MODE_PLAYBACK
        # Let residual mic audio drain out of the radio-sink buffer BEFORE
        # keying PTT so the LEAD gap is actual silence rather than the tail
        # of the operator's last syllable.
        time.sleep(PRE_KEY_DRAIN_SECONDS)

        completed = False
        try:
            with self._monitor_session(), hamlib_ptt(self._ptt_spec):
                for start in range(0, samples.size, BLOCK_FRAMES):
                    if self._stop_event.is_set():
                        break
                    chunk = samples[start : start + BLOCK_FRAMES]
                    self._emit(chunk, self._playback_gain)
                completed = not self._stop_event.is_set()
        except PTTError as exc:
            _log.error("playback aborted: %s", exc)
            print(f"playback aborted: {exc}")
        finally:
            with self._mode_lock:
                self._mode = _MODE_PASSTHROUGH
            # Discard mic blocks that piled up during playback so the
            # resumed passthrough does not burst a second of stale audio
            # at the radio.
            self._drain_mic_queue()
            if completed:
                print("playback done.")

    # ---- Pilot -----------------------------------------------------------

    def _run_pilot(self) -> None:
        """Emit a 1 kHz sine at ``playback_gain`` until the user hits
        'p' again (which sets ``_pilot_toggle_requested``)."""
        phase_step = 2.0 * np.pi * _PILOT_TONE_HZ / SAMPLE_RATE_HZ
        phase = 0.0

        with self._mode_lock:
            self._mode = _MODE_PILOT
        time.sleep(PRE_KEY_DRAIN_SECONDS)

        try:
            with self._monitor_session(), hamlib_ptt(self._ptt_spec):
                while not self._stop_event.is_set() and not self._pilot_toggle_requested.is_set():
                    t = phase + phase_step * np.arange(BLOCK_FRAMES, dtype=np.float64)
                    block = np.sin(t).astype(np.float32)
                    phase = (phase + phase_step * BLOCK_FRAMES) % (2.0 * np.pi)
                    self._emit(block, self._playback_gain)
        except PTTError as exc:
            _log.error("pilot aborted: %s", exc)
            print(f"pilot aborted: {exc}")
        finally:
            self._pilot_toggle_requested.clear()
            with self._mode_lock:
                self._mode = _MODE_PASSTHROUGH
            self._drain_mic_queue()
            print("pilot off.")

    # ---- Monitor session (per playback / pilot) -------------------------

    @contextmanager
    def _monitor_session(self) -> Iterator[None]:
        """Open the monitor sink for the body, close it on exit. Closing
        after each session guarantees no residual audio bleeds out of
        paplay's ring buffer while the operator is back in passthrough.
        """
        if self._monitor_target is None:
            yield
            return
        sink = LiveOutputStream(
            sample_rate=SAMPLE_RATE_HZ,
            block_frames=BLOCK_FRAMES,
            target=self._monitor_target,
        )
        try:
            sink.__enter__()
        except Exception as exc:
            _log.warning("could not open monitor sink: %s", exc)
            yield
            return
        self._monitor_sink = sink
        try:
            yield
        finally:
            self._monitor_sink = None
            try:
                sink.__exit__(None, None, None)
            except Exception as exc:
                _log.warning("could not close monitor sink cleanly: %s", exc)

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
    mic_passthrough_level_percent: float,
    playback_level_percent: float,
    monitor_level_percent: float,
    ptt_spec: str | None,
    eq_gains_db: dict[int, float] | None = None,
) -> Keyer:
    return Keyer(
        mic_target=mic_target,
        radio_target=radio_target,
        monitor_target=monitor_target,
        recording_path=recording_path,
        mic_passthrough_gain=_percent_to_gain(mic_passthrough_level_percent),
        playback_gain=_percent_to_gain(playback_level_percent),
        monitor_gain=_percent_to_gain(monitor_level_percent),
        ptt_spec=ptt_spec,
        eq_gains_db=eq_gains_db,
    )


