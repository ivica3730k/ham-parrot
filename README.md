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
    --radio-input-device "USB Audio" \
    --monitor-enable --monitor-device "MacBook Pro Speakers" \
    --hamlib-ptt localhost:4532 \
    --mic-level 100 \
    --recorder-out-level 90
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
| `--mic-device` | Audio input to read your voice from. Same device-hint syntax as `--radio-input-device`. |
| `--radio-input-device` | Audio output that feeds into the radio's mic / line-in. From the radio's point of view this is its *input*. |
| `--monitor-enable` | Turn on local monitoring (off by default). |
| `--monitor-device` | Optional monitor output device (only used when `--monitor-enable` is set). Leave unset for the OS default. |
| `--hamlib-ptt HOST:PORT` | rigctld endpoint. Bare `--hamlib-ptt` defaults to `localhost:4532`. |
| `--mic-level 0-100` | Gain (percent, linear) applied to the mic passthrough to the radio. 100 = unity. |
| `--recorder-out-level 0-100` | Gain applied to `recording.wav` (and the pilot tone) on the radio path. 100 = unity. |
| `--monitor-level 0-100` | Gain applied to the local monitor sink, independent of the radio-side gains. |

Device hints accept: a substring of the OS device name (`"USB Audio"`), a
numeric index (from `pactl list short sinks` on Linux, or `sounddevice`'s device
list elsewhere), or `pulse:<name>` to force the Pulse path.

## License

MIT. See [LICENSE](./LICENSE).
