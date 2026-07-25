# ham-parrot

Voice keyer for ham radio contests. Runs as a long-lived CLI: proxies your
microphone straight to the radio the whole time, and when you press Enter it
plays back a pre-recorded voice sequence over the air (keying PTT via
[Hamlib rigctld](https://hamlib.github.io/)) so you don't have to shout the
same CQ / exchange fifty times an hour.

## Install

```sh
pipx install ham-parrot
# or, for development:
poetry install
```

## Use

```sh
ham-parrot \
    --mic-device "USB Audio" \
    --radio-audio-device "USB Audio" \
    --monitor-enable --monitor-device "MacBook Pro Speakers" \
    --hamlib-ptt localhost:4532 \
    --mic-passthrough-level 100 \
    --playback-level 90 \
    --eq-json eq_examples/ssb.json
```

Keys, while running:

- **`r`** — start recording your voice sequence (mic is muted from the air while recording so you don't stomp on yourself). Press `r` again to stop; the file is saved as `./recording.wav` and a filtered companion `./recording_eq.wav` is rendered next to it.
- **Enter** — key PTT, play the recording over the radio (and to your monitor device, if enabled), release PTT.
- **`p`** — toggle a 1 kHz pilot tone (PTT-keyed) at `--playback-level`. Useful for setting mic drive on the radio.
- **`q`** or `Ctrl-C` — quit.

`recording.wav` in the current directory is auto-loaded on startup, so a contest
run is: record once, then hit Enter every time you need it. If the radio is
already transmitting (PTT reads high on rigctld) when you hit Enter or `p`,
the action is refused so you don't collide with an ongoing over.

## Flags

| Flag | Meaning |
| ---- | ------- |
| `--mic-device` | Audio input device to read your voice from. Same device-hint syntax as `--radio-audio-device`. |
| `--radio-audio-device` | Audio *output* device on this host that feeds the radio's mic / line-in. On Linux this is typically `alsa_output.usb-...` — do not pass a source / input name. |
| `--monitor-enable` | Turn on local monitoring (off by default). Monitor is silent during passthrough (so mic bleed can't feed back through your headphones) and plays only what goes on-air during playback and pilot. |
| `--monitor-device` | Optional monitor output device (only used when `--monitor-enable` is set). Leave unset for the OS default. |
| `--hamlib-ptt HOST:PORT` | rigctld endpoint. Bare `--hamlib-ptt` defaults to `localhost:4532`; omit entirely for VOX / manual keying. |
| `--mic-passthrough-level 0-500` | Gain (percent, linear) applied to the live mic → radio passthrough. 100 = unity, 200 = +6 dB, 500 = +14 dB. Overshoots past ±1.0 are hard-clipped. |
| `--playback-level 0-500` | Gain applied to the recording and the pilot tone on the radio path. 100 = unity. |
| `--monitor-level 0-500` | Gain applied to the local monitor sink, independent of the radio-side gains. |
| `--eq-json PATH` | Optional 17-band peaking EQ (ISO 1/3-octave centres, 100 Hz – 4 kHz). See [`eq_examples/flat.json`](./eq_examples/flat.json) for the required schema and [`eq_examples/ssb.json`](./eq_examples/ssb.json) for a voice-shaped starting curve. |
| `--recording-path PATH` | Where to store / read the voice sequence. Default `./recording.wav`; the filtered companion is `<stem>_eq.wav` next to it. |
| `--log-file PATH` | Diagnostics log path. Default `./log.txt`. stdout / stderr are never used for logs. |
| `--debug` | Verbose diagnostics (DEBUG level) in the log file. |

## Audio processing

Everything sent to the radio (mic passthrough, playback, pilot) goes through
the same chain:

- Butterworth **bandpass 100 Hz – 4 kHz** (4th order per edge → ~48 dB/octave rolloff), always on.
- Optional **17-band peaking EQ** on the ISO 1/3-octave centres, controlled by `--eq-json`.
- Hard-clip at ±1.0 after gain + filter so overshoots don't wrap into artefacts.

`recording.wav` is captured **raw** (pre-gain, pre-filter) so you can change
`--eq-json` between runs without re-recording. `recording_eq.wav` is a
filtered snapshot re-rendered on every startup and every stop-recording — use
it in VLC / any other player to hear the on-air version. Playback prefers the
pre-rendered file so the IIR chain doesn't run over every playback sample.

## Diagnostics

- The banner at startup prints the resolved audio targets (mic, radio, monitor) so you can verify device routing before pressing anything.
- A rate-limited `mic clipping (peak=X.XXX)` line is printed to stdout whenever the raw mic hits the ADC ceiling — back off the mic or lower input gain on the interface.
- `paplay` / `parec` stderr is captured in `log.txt` so silent-death of the subprocess doesn't stay silent.

Device hints accept: a substring of the OS device name (`"USB Audio"`), a
numeric index (from `pactl list short sinks` on Linux, or `sounddevice`'s device
list elsewhere), or `pulse:<name>` to force the Pulse path.

## License

MIT. See [LICENSE](./LICENSE).
