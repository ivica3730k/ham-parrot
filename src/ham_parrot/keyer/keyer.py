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
    write_wav,
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
        rx_target: AudioTarget | None,
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
        # Optional radio-RX input tap. When paired with a monitor sink, the
        # RX callback pipes the radio's speaker audio into the monitor while
        # we're NOT transmitting (passthrough / recording). During playback
        # and pilot the RX audio is dropped so it doesn't fight the on-air
        # content on the same monitor sink.
        self._rx_target = rx_target if monitor_target is not None else None
        self._recording_path = recording_path
        self._mic_passthrough_gain = mic_passthrough_gain
        self._playback_gain = playback_gain
        self._monitor_gain = monitor_gain
        self._ptt_spec = ptt_spec

        # Radio-side filter: 100 Hz - 4 kHz bandpass, optional peaking EQ.
        # State is continuous across passthrough / playback / pilot -- avoids
        # a click at the switch, and the modes are mutually exclusive so
        # there is no per-stream contention.
        self._eq_gains_db = eq_gains_db
        sos = build_radio_sos(SAMPLE_RATE_HZ, eq_gains_db=eq_gains_db)
        self._radio_filter = RadioFilter(sos)

        # Companion WAV that has the same audio as the raw recording but
        # run through the current filter chain once, offline. Regenerated
        # on every startup and every stop-recording so it always reflects
        # the current --eq-json curve.
        self._eq_recording_path = recording_path.with_stem(recording_path.stem + "_eq")

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
        stopped = False
        with self._mode_lock:
            if self._mode == _MODE_RECORDING:
                self._mode = _MODE_PASSTHROUGH
                self._close_recorder()
                stopped = True
            elif self._mode != _MODE_PASSTHROUGH:
                return f"cannot record while {self._mode}"
            else:
                self._open_recorder()
                self._mode = _MODE_RECORDING
                return "recording started (press 'r' again to stop)"
        # Render the _eq.wav companion outside the lock: a long recording
        # can take a second or two to filter and we don't want to block
        # the mixer thread's mode reads while it renders.
        if stopped:
            self._render_eq_wav()
        return f"recording saved -> {self._recording_path}"

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
        pilot session inside ``_run_playback`` / ``_run_pilot`` -- unless
        we also have an RX input tap, in which case the monitor is opened
        for the whole session so RX audio has a persistent destination."""
        # Refresh the _eq.wav companion at startup: the operator may have
        # tweaked --eq-json between runs, so the file on disk is stale.
        self._render_eq_wav()
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
                if self._rx_target is not None and self._monitor_target is not None:
                    self._run_with_rx_tap()
                else:
                    self._mixer_loop()
            finally:
                self._radio_sink = None
                self._close_recorder()

    def _run_with_rx_tap(self) -> None:
        """Variant of ``run()``'s inner loop that keeps a persistent
        monitor sink open (so RX audio has somewhere to go) and taps the
        radio's speaker/line-out with an input stream."""
        with (
            LiveOutputStream(
                sample_rate=SAMPLE_RATE_HZ,
                block_frames=BLOCK_FRAMES,
                target=self._monitor_target,  # type: ignore[arg-type]
            ) as monitor_sink,
            LiveInputStream(
                sample_rate=SAMPLE_RATE_HZ,
                block_frames=BLOCK_FRAMES,
                callback=self._on_rx_block,
                target=self._rx_target,  # type: ignore[arg-type]
            ),
        ):
            self._monitor_sink = monitor_sink
            try:
                self._mixer_loop()
            finally:
                self._monitor_sink = None

    # ---- Mic-side capture ------------------------------------------------

    def _on_mic_block(self, samples: np.ndarray) -> None:
        # PortAudio callback thread. Never block here; drop on backpressure.
        try:
            self._mic_q.put_nowait(samples)
        except queue.Full:
            _log.warning("mic queue overflow -- dropping %d frames", samples.size)

    def _on_rx_block(self, samples: np.ndarray) -> None:
        """Radio-RX callback (parec / sounddevice thread). Forwards the
        radio's speaker audio to the monitor sink while we're NOT
        transmitting. During playback / pilot the monitor is carrying
        on-air content already, so we drop RX blocks to avoid mixing."""
        sink = self._monitor_sink
        if sink is None:
            return
        with self._mode_lock:
            if self._mode not in (_MODE_PASSTHROUGH, _MODE_RECORDING):
                return
        out = np.asarray(samples, dtype=np.float32) * self._monitor_gain
        np.clip(out, -1.0, 1.0, out=out)
        try:
            sink.write(out)
        except Exception as exc:
            _log.warning("rx -> monitor write failed: %s", exc)

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

    def _emit(self, source: np.ndarray, radio_gain: float, *, filtered: bool = True) -> None:
        """Scale ``source`` independently for each sink and write it out.
        Used for playback + pilot (on-air content); mic passthrough uses
        its own radio-only path so the monitor never carries mic audio.

        ``filtered=False`` skips the bandpass + EQ on the radio path --
        used when the source is already pre-filtered (e.g. reading
        ``recording_eq.wav`` back).

        When an RX tap is wired to the monitor, the monitor is treated
        as RX-only -- the operator wants to hear the radio in their
        headphones, not their own voice / pilot tone bleeding back
        through the same sink -- so we skip the monitor write here.
        """
        self._write_to_radio(source, radio_gain, filtered=filtered)
        if self._monitor_sink is not None and self._rx_target is None:
            monitor_out = np.asarray(source, dtype=np.float32) * self._monitor_gain
            np.clip(monitor_out, -1.0, 1.0, out=monitor_out)
            try:
                self._monitor_sink.write(monitor_out)
            except Exception as exc:
                _log.warning("monitor sink write failed: %s", exc)

    def _write_to_radio(self, source: np.ndarray, gain: float, *, filtered: bool = True) -> None:
        """Common radio-write path: gain, then (optionally) filter
        (bandpass + EQ), then hard-clip at ±1.0, then write. Passthrough
        and pilot use ``filtered=True``; pre-filtered WAV playback uses
        ``filtered=False`` to avoid double-filtering."""
        if self._radio_sink is None:
            return
        audio = np.asarray(source, dtype=np.float32) * gain
        if filtered:
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
        # Prefer the pre-filtered companion so we don't run the IIR
        # chain twice for every playback. Falls back to raw with live
        # filtering if the companion was deleted between renders.
        if self._eq_recording_path.exists():
            src_path = self._eq_recording_path
            live_filter = False
        else:
            src_path = self._recording_path
            live_filter = True
        try:
            samples, rate = read_wav(src_path, expected_sample_rate=SAMPLE_RATE_HZ)
        except HamParrotError as exc:
            _log.error("cannot read %s: %s", src_path, exc)
            print(f"playback failed: {exc}")
            return
        _log.info("playback: %s (%d frames @ %d Hz, filter=%s)",
                  src_path.name, samples.size, rate, "live" if live_filter else "pre-rendered")

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
                    self._emit(chunk, self._playback_gain, filtered=live_filter)
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

        If an RX tap is running the monitor is already open for the whole
        session (so RX has a persistent destination); in that case this
        is a no-op and the sink stays up across the playback / pilot.
        """
        if self._monitor_target is None:
            yield
            return
        if self._monitor_sink is not None:
            # Persistent RX-tap mode: monitor is already open. The RX
            # callback will stop writing on its own once the mode flips
            # to PLAYBACK / PILOT, so the on-air content owns the sink
            # uncontested for the duration.
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

    # ---- Offline EQ render ------------------------------------------------

    def _render_eq_wav(self) -> None:
        """Read the raw ``recording.wav`` and write a companion
        ``recording_eq.wav`` that has been run through the current
        filter chain (bandpass + EQ) once, offline.

        Called on startup and after every stop-recording so the file
        always reflects the current ``--eq-json`` curve. If the raw
        file does not exist, any stale companion is removed instead.
        """
        raw = self._recording_path
        dst = self._eq_recording_path
        if not raw.exists():
            if dst.exists():
                try:
                    dst.unlink()
                    _log.info("no raw recording; removed stale %s", dst)
                except OSError as exc:
                    _log.warning("could not remove stale %s: %s", dst, exc)
            return
        try:
            samples, _ = read_wav(raw, expected_sample_rate=SAMPLE_RATE_HZ)
        except HamParrotError as exc:
            _log.error("cannot read raw recording %s: %s", raw, exc)
            return
        # Fresh filter state so the offline render is deterministic and
        # matches what a first-shot live playback would sound like.
        filt = RadioFilter(build_radio_sos(SAMPLE_RATE_HZ, eq_gains_db=self._eq_gains_db))
        out = filt.apply(samples).astype(np.float32, copy=False)
        np.clip(out, -1.0, 1.0, out=out)
        write_wav(dst, out, SAMPLE_RATE_HZ)
        seconds = out.size / SAMPLE_RATE_HZ
        _log.info("rendered %s (%.1f s)", dst, seconds)
        print(f"eq render -> {dst} ({seconds:.1f} s)")


# CLI convenience: build + run a keyer with parsed args, converting
# level percentages into gains here so ``Keyer`` sees only floats.
def build_keyer(
    *,
    mic_target: AudioTarget,
    radio_target: AudioTarget,
    monitor_target: AudioTarget | None,
    rx_target: AudioTarget | None = None,
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
        rx_target=rx_target,
        recording_path=recording_path,
        mic_passthrough_gain=_percent_to_gain(mic_passthrough_level_percent),
        playback_gain=_percent_to_gain(playback_level_percent),
        monitor_gain=_percent_to_gain(monitor_level_percent),
        ptt_spec=ptt_spec,
        eq_gains_db=eq_gains_db,
    )


