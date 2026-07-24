"""ham-parrot CLI. Always-on mic passthrough with one-key voice-keyer playback.

Key bindings (raw stdin, single byte):

* ``r``       toggle recording
* ``Enter``   play the recorded WAV over the air (keys PTT)
* ``p``       toggle a 1 kHz pilot tone (PTT-keyed) for setting mic-drive on the radio
* ``q`` or Ctrl-C  quit

Diagnostics go to ``log.txt`` (never stdout / stderr) so the status
prints you see line up with what you just pressed.
"""

from __future__ import annotations

import argparse
import logging
import selectors
import sys
import termios
import threading
import tty
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Sequence

from ham_parrot.keyer.audio import AudioTarget, resolve_audio_target
from ham_parrot.keyer.constants import DEFAULT_LOG_PATH, MAX_LEVEL_PERCENT, RECORDING_PATH
from ham_parrot.keyer.exceptions import ConfigError, HamParrotError
from ham_parrot.keyer.filter import load_eq_json
from ham_parrot.keyer.keyer import build_keyer

_log = logging.getLogger("ham_parrot.cli")


def _build_parser() -> argparse.ArgumentParser:
    try:
        version = _pkg_version("ham-parrot")
    except PackageNotFoundError:
        version = "unknown"

    parser = argparse.ArgumentParser(
        prog="ham-parrot",
        description=(
            "Voice keyer for ham radio: mic passthrough to the radio, plus "
            "one-key playback of a pre-recorded voice sequence."
        ),
    )
    parser.add_argument("--version", action="version", version=f"ham-parrot {version}")

    parser.add_argument(
        "--mic-device",
        type=str,
        default=None,
        help="Audio input device for your mic. Substring of a device name, "
        "a numeric index, or 'pulse:<name>' to force the Pulse path.",
    )
    parser.add_argument(
        "--radio-audio-device",
        type=str,
        default=None,
        help="Audio output device on this host that feeds the radio's mic / line-in. "
        "Same device-hint syntax as --mic-device but resolves against output devices "
        "(e.g. `alsa_output.usb-...` on Linux). Do NOT pass a source / input name here.",
    )
    parser.add_argument(
        "--monitor-enable",
        action="store_true",
        default=False,
        help="Enable local monitoring (headphones / speakers). Same content as goes "
        "to the radio. Off by default so the recorded voice doesn't leak into a "
        "shack mic that's still hot.",
    )
    parser.add_argument(
        "--monitor-device",
        type=str,
        default=None,
        help="Optional monitor output device (only used when --monitor-enable is set). "
        "Same syntax as --radio-audio-device. Leave unset to use the OS default output.",
    )
    parser.add_argument(
        "--hamlib-ptt",
        nargs="?",
        const="localhost:4532",
        default=None,
        metavar="HOST:PORT",
        help="rigctld endpoint for PTT. Bare --hamlib-ptt defaults to "
        "localhost:4532; pass HOST:PORT to override. Omit entirely to run "
        "without PTT (VOX / manual keying).",
    )
    level_range = f"0-{int(MAX_LEVEL_PERCENT)}"
    parser.add_argument(
        "--mic-passthrough-level",
        type=float,
        default=100.0,
        metavar=level_range,
        help=f"Gain applied to the live mic -> radio passthrough, {level_range} "
        "(default: 100 = unity, 200 = +6 dB, 500 = +14 dB). Recording is always "
        "captured at the raw mic level; this knob only scales what the radio hears "
        "live. Overshoots past unity are hard-clipped at ±1.0 to prevent wraparound.",
    )
    parser.add_argument(
        "--playback-level",
        type=float,
        default=100.0,
        metavar=level_range,
        help=f"Gain applied when playing the recorded sequence and the pilot tone, "
        f"{level_range} (default: 100 = unity source level, 200 = +6 dB, 500 = +14 dB).",
    )
    parser.add_argument(
        "--monitor-level",
        type=float,
        default=100.0,
        metavar=level_range,
        help=f"Gain applied to the local monitor sink, {level_range} (default: 100 = "
        "unity). Independent of --mic-passthrough-level and --playback-level, so you "
        "can turn the monitor down without changing what the radio hears.",
    )
    parser.add_argument(
        "--recording-path",
        type=Path,
        default=RECORDING_PATH,
        help=f"Where to store / read the recorded voice sequence "
        f"(default: ./{RECORDING_PATH}).",
    )
    parser.add_argument(
        "--eq-json",
        type=Path,
        default=None,
        metavar="PATH",
        help="Optional 17-band peaking EQ, applied on top of the mandatory "
        "100 Hz - 4 kHz radio bandpass. JSON keys are the ISO 1/3-octave "
        "centre frequencies (100, 125, 160, 200, 250, 315, 400, 500, 630, "
        "800, 1000, 1250, 1600, 2000, 2500, 3150, 4000); values are gain "
        "in dB. All 17 bands MUST be present -- omitted or unknown bands "
        "are rejected up front. See eq_examples/flat.json (unity across "
        "the passband) and eq_examples/ssb.json (voice-shaped for SSB).",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=DEFAULT_LOG_PATH,
        help=f"Path to the diagnostics log (default: ./{DEFAULT_LOG_PATH}). "
        "stdout/stderr are never used for diagnostics.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Verbose diagnostics (DEBUG level) in the log file.",
    )
    return parser


def _validate_levels(args: argparse.Namespace) -> None:
    for name, val in (
        ("--mic-passthrough-level", args.mic_passthrough_level),
        ("--playback-level", args.playback_level),
        ("--monitor-level", args.monitor_level),
    ):
        if not (0.0 <= val <= MAX_LEVEL_PERCENT):
            raise ConfigError(
                f"{name} must be between 0 and {int(MAX_LEVEL_PERCENT)} (got {val})"
            )


def _resolve_devices(args: argparse.Namespace) -> tuple[AudioTarget, AudioTarget, AudioTarget | None]:
    mic = resolve_audio_target(args.mic_device, kind="input")
    radio = resolve_audio_target(args.radio_audio_device, kind="output")
    # Monitor is opt-in. With --monitor-enable and no --monitor-device,
    # resolve_audio_target(None, ...) returns an unbound AudioTarget which
    # the LiveOutputStream opens against the OS default output.
    monitor = (
        resolve_audio_target(args.monitor_device, kind="output")
        if args.monitor_enable
        else None
    )
    if args.monitor_device and not args.monitor_enable:
        _log.warning("--monitor-device provided without --monitor-enable; monitor stays off")
    return mic, radio, monitor


def _run_key_loop(keyer, stop_event: threading.Event, args, mic, radio, monitor) -> None:  # type: ignore[no-untyped-def]
    """Raw-mode stdin reader. Runs in the main thread.

    Uses ``selectors`` so ``stop_event`` can wake us out of the read
    without needing a signal on stdin.
    """
    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    sel = selectors.DefaultSelector()
    sel.register(fd, selectors.EVENT_READ)
    try:
        tty.setcbreak(fd)
        _print_banner(keyer, args, mic, radio, monitor)
        while not stop_event.is_set():
            events = sel.select(timeout=0.1)
            if not events:
                continue
            ch = sys.stdin.buffer.read(1)
            if not ch:  # EOF (stdin closed)
                break
            _handle_key(ch, keyer, stop_event)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        sel.close()


def _print_banner(keyer, args: argparse.Namespace, mic, radio, monitor) -> None:  # type: ignore[no-untyped-def]
    have = "yes" if keyer.has_recording() else "no"
    monitor_desc = monitor.describe() if monitor is not None else "off"
    print(
        f"ham-parrot ready.\n"
        f"  mic     : {mic.describe()}\n"
        f"  radio   : {radio.describe()}\n"
        f"  monitor : {monitor_desc}\n"
        f"  ptt     : {args.hamlib_ptt or 'off'}\n"
        f"  recording present: {have} ({keyer.recording_path()})\n"
        "  r = toggle record   ENTER = play   p = toggle pilot   q = quit"
    )


def _handle_key(ch: bytes, keyer, stop_event: threading.Event) -> None:  # type: ignore[no-untyped-def]
    # Ctrl-C in raw mode arrives as \x03; termios' ISIG is off under cbreak,
    # so we translate manually. Ctrl-D arrives as \x04 -> also treat as quit.
    if ch in (b"\x03", b"\x04", b"q", b"Q"):
        print("quitting.")
        stop_event.set()
        return
    if ch in (b"\r", b"\n"):
        print(keyer.request_playback())
        return
    if ch in (b"r", b"R"):
        print(keyer.toggle_recording())
        return
    if ch in (b"p", b"P"):
        print(keyer.toggle_pilot())
        return
    # Everything else is silently ignored so a stray arrow-key doesn't
    # spam the console mid-contest.


def _run(args: argparse.Namespace) -> int:
    _validate_levels(args)
    mic, radio, monitor = _resolve_devices(args)
    _log.info(
        "devices: mic=%s radio=%s monitor=%s ptt=%s",
        mic.describe(), radio.describe(),
        monitor.describe() if monitor is not None else "off",
        args.hamlib_ptt or "off",
    )

    eq_gains_db = load_eq_json(args.eq_json) if args.eq_json is not None else None
    if eq_gains_db is not None:
        _log.info("--eq-json %s: %d bands loaded", args.eq_json, len(eq_gains_db))

    keyer = build_keyer(
        mic_target=mic,
        radio_target=radio,
        monitor_target=monitor,
        recording_path=args.recording_path,
        mic_passthrough_level_percent=args.mic_passthrough_level,
        playback_level_percent=args.playback_level,
        monitor_level_percent=args.monitor_level,
        ptt_spec=args.hamlib_ptt,
        eq_gains_db=eq_gains_db,
    )

    stop_event = threading.Event()

    def _run_keyer() -> None:
        try:
            keyer.run()
        except Exception:
            _log.exception("keyer crashed")
        finally:
            stop_event.set()

    thread = threading.Thread(target=_run_keyer, name="ham-parrot-keyer", daemon=True)
    thread.start()

    try:
        _run_key_loop(keyer, stop_event, args, mic, radio, monitor)
    except KeyboardInterrupt:
        pass
    finally:
        keyer.stop()
        thread.join(timeout=2.0)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    handler = logging.FileHandler(args.log_file, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("ham_parrot")
    root.setLevel(logging.DEBUG if args.debug else logging.INFO)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.propagate = False

    _log.debug("ham-parrot starting")
    try:
        try:
            return _run(args)
        except HamParrotError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    finally:
        logging.shutdown()


if __name__ == "__main__":
    sys.exit(main())
