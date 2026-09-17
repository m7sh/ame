# ytmusic-tui

> A minimal, keyboard-driven YouTube Music player for the terminal.
> Runs entirely in **guest mode** — no login, cookies or API keys.

ytmusic-tui is built with [Textual](https://textual.textualize.io/), uses
`ytmusicapi` for discovery, `yt-dlp` to resolve audio streams and a headless
`mpv` daemon for playback. The interface is deliberately sparse: one tab bar,
one list, and a slim player — nothing else competing for attention.

---

## Design

- **Terminal-native chrome** — no emoji, no boxes-within-boxes. Unicode
  rules, block glyphs and reverse-style selection only.
- **One screen at a time** — the sidebar and status badges are gone; views
  are switched from a single tab line or with `1`–`5`.
- **Three-line player** — now-playing, time and playback status, then a
  full-width clickable seek bar, then a key-pill help bar.
- **Help on demand** — press `?` for the keybinding overlay, or `h` to hide
  the persistent help bar.
- **Live Omarchy theme sync** — colors are injected as native Textual theme
  variables, so switching your desktop theme restyles the app instantly with
  no flicker and no file rewrites.
- **Real spectrum visualizer** — `cava` taps the PipeWire monitor and drives
  fractional-block bars with a green→yellow→red gradient. Press `v` to hide
  it; it is omitted automatically when `cava` is not installed.

The chrome takes its cues from [cliamp](https://github.com/bjarneo/cliamp):
a letterspaced wordmark, bracketed chips, `▸─ section ──` dividers, a block
volume meter and key pills in the help bar.

---

## Features

- Zero authentication / guest mode (`YTMusic()` with no credentials).
- Trending charts, mood and genre browsing, search and song radio.
- Infinite radio plus continuous autoplay when the queue runs dry.
- Full queue control: append, play-all, shuffle, remove and clear.
- Asynchronous discovery and stream resolution — the UI never blocks.
- Headless `mpv` playback over a JSON IPC socket with stream prefetching.
- Real-time spectrum visualizer via `cava` (optional).

---

## Keyboard

| Key | Action |
| --- | --- |
| `space` | play / pause |
| `n` / `p` | next / previous track |
| `←` / `→` | seek −5s / +5s |
| `+` / `-` | volume up / down |
| `j` / `k` | move down / up |
| `enter` | play highlighted item |
| `a` | append highlighted track or playlist to the queue |
| `A` | queue every track in the current view |
| `P` | play every track in the current view |
| `r` | start a song radio from the highlighted track |
| `d` | remove the highlighted track from the queue |
| `s` / `c` | shuffle / clear the queue |
| `/` | focus search |
| `1` – `5` | jump to trending / radio / moods / search / queue |
| `h` | show / hide the help bar |
| `v` | show / hide the spectrum visualizer |
| `esc` | back or close overlay |
| `t` | re-sync the Omarchy theme |
| `?` | toggle the keybinding overlay |
| `q` | quit (stops audio and cleans up sockets) |

---

## Installation

Requires `mpv` and `yt-dlp` on your `PATH`:

```bash
sudo pacman -S mpv yt-dlp     # Arch / Omarchy
sudo apt install mpv yt-dlp   # Debian / Ubuntu
```

`cava` is optional and only used for the spectrum visualizer:

```bash
sudo pacman -S cava           # Arch / Omarchy
sudo apt install cava         # Debian / Ubuntu
```

Then install the Python dependencies and run:

```bash
git clone https://github.com/m7sh/ytmusic-tui.git
cd ytmusic-tui
python -m venv .venv
.venv/bin/pip install -r requirements.txt
./ytmusic-tui
```

`ytmusic-tui` is a small launcher that uses `.venv` when present and falls
back to the system `python3`. Symlink it into `~/.local/bin` to run it from
anywhere:

```bash
ln -sf "$PWD/ytmusic-tui" ~/.local/bin/ytmusic-tui
```

---

## Project layout

```
ytmusic-tui/
├── app.py            # Textual app: layout, bindings, workers, theme sync
├── api.py            # Guest-mode ytmusicapi client + yt-dlp stream resolver
├── player.py         # Headless mpv IPC client, queue state machine
├── spectrum.py       # cava subprocess bridge for the spectrum visualizer
├── theme.py          # Omarchy palette detection -> native Textual Theme
├── styles.tcss       # Static stylesheet driven by theme CSS variables
├── requirements.txt
├── ytmusic-tui       # Launcher script
└── ui/
    ├── widgets.py    # TopBar, SectionHeader, SeekBar, HelpBar, PlayerBar
    └── views.py      # Trending, Radio, Moods, Playlist, Search, Queue
```

---

## How theme sync works

At startup `theme.py` reads
`~/.local/state/omarchy/current/theme/colors.toml`, normalizes the palette
and registers it as a Textual `Theme` with custom variables (`$ytm-accent`,
`$ytm-muted`, …). `styles.tcss` only ever refers to those variables. When the
file changes, the app registers a fresh theme and reassigns it — Textual
re-applies every variable live, so nothing on disk is touched and the
interface never re-parses a stylesheet mid-session.

## License

MIT
