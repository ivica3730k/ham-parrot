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
    --playback-level 90
```

Keys, while running:

- **`r`** — start recording your voice sequence (mic is muted from the air while recording so you don't stomp on yourself). Press `r` again to stop; the file is saved as `./recording.wav`.
- **Enter** — key PTT, play `recording.wav` over the radio (and to your monitor device, if configured), release PTT.
- **`q`** or `Ctrl-C` — quit.

`recording.wav` in the current directory is auto-loaded on startup, so a contest
run is: record once, then hit Enter every time you need it.

If the radio is already transmitting (PTT reads high on rigctld) when you hit
Enter, playback is refused so you don't collide with an ongoing over.

## Flags

| Flag | Meaning |
| ---- | ------- |
| `--mic-device` | Audio input to read your voice from. Same device-hint syntax as `--radio-audio-device`. |
| `--radio-audio-device` | Audio *output* device on this host that feeds the radio's mic / line-in. On Linux this is typically `alsa_output.usb-...` — do not pass a source / input name. |
| `--monitor-enable` | Turn on local monitoring (off by default). Monitor plays back only what is being transmitted from the recording / pilot tone — the live mic passthrough is not routed to the monitor to avoid the operator hearing their own voice with headphone lag. |
| `--monitor-device` | Optional monitor output device (only used when `--monitor-enable` is set). Leave unset for the OS default. |
| `--hamlib-ptt HOST:PORT` | rigctld endpoint. Bare `--hamlib-ptt` defaults to `localhost:4532`. |
| `--mic-passthrough-level 0-500` | Gain (percent, linear) applied to the live mic → radio passthrough. 100 = unity, 200 = +6 dB, 500 = +14 dB. Overshoots past ±1.0 are hard-clipped. |
| `--playback-level 0-500` | Gain applied to `recording.wav` (and the pilot tone) on the radio path. 100 = unity. |
| `--monitor-level 0-500` | Gain applied to the local monitor sink, independent of the radio-side gains. |
| `--eq-json PATH` | Optional 17-band peaking EQ (ISO 1/3-octave centres, 100 Hz – 4 kHz). See [`examples/flat.eq.json`](./examples/flat.eq.json) for the required schema. |

## Audio processing

Everything sent to the radio (mic passthrough, playback, pilot) is filtered:

- Butterworth **bandpass 100 Hz – 4 kHz** (4th order per edge → ~48 dB/octave rolloff), always on.
- Optional **17-band peaking EQ** on the ISO 1/3-octave centres, controlled by `--eq-json`.

The monitor sink is fed the **raw** source so you can hear what your voice
actually sounds like uncoloured; the recording is captured **before** the
filter for the same reason (the shaping happens on-air, not in the file).

Device hints accept: a substring of the OS device name (`"USB Audio"`), a
numeric index (from `pactl list short sinks` on Linux, or `sounddevice`'s device
list elsewhere), or `pulse:<name>` to force the Pulse path.

## License

MIT. See [LICENSE](./LICENSE).
