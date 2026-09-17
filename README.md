# ame

> A minimal, keyboard-driven YouTube Music player for the terminal.
> Runs entirely in **guest mode** — no login, cookies or API keys.

`ame` (雨, "rain") is built with [Textual](https://textual.textualize.io/), uses
`ytmusicapi` for discovery, `yt-dlp` to resolve audio streams and a headless
`mpv` daemon for playback. The interface is deliberately sparse: one tab bar,
one list, and a slim player — nothing else competing for attention.

---

## Design

- **Terminal-native chrome** — no emoji, no boxes-within-boxes. Unicode
  rules, block glyphs and reverse-style selection only.
- **One screen at a time** — the sidebar and status badges are gone; views
  are switched from a single tab line, where each view's hotkey is
  highlighted and works as its shortcut (`t` `r` `m` `s` `F` `q`).
- **Two-line player** — now-playing with time and playback status, then a
  full-width clickable seek bar. No key-hint bar; press `?` when you need the
  cheatsheet.
- **Help on demand** — press `?` for the keybinding overlay.
- **Live Omarchy theme sync** — colors are injected as native Textual theme
  variables, so switching your desktop theme restyles the app instantly with
  no flicker and no file rewrites.
- **Terminal transparency** — the app paints no background of its own; every
  panel resolves to `ansi_default`, so your terminal's opacity/blur shows
  through and the UI blends into the terminal background.
- **Matrix visualizer** — `cava` taps the PipeWire monitor and drives a
  digital-rain field at the top of the screen: each column is a falling stream
  of katakana/digit glyphs whose speed and trail length follow its frequency
  band, with a near-white head fading into the active theme's accent color. It
  appears only while audio is playing; press `v` to disable it, and it is
  omitted automatically when `cava` is not installed.

The chrome takes its cues from [cliamp](https://github.com/bjarneo/cliamp):
a letterspaced wordmark, bracketed chips and `▸─ section ──` dividers.

---

## Features

- Zero authentication / guest mode (`YTMusic()` with no credentials).
- Trending charts, mood and genre browsing, search and song radio.
- Persistent favourites — star songs with `f` and revisit them from the
  favourites view (`F`), saved to `~/.local/share/ame/favourites.json`.
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
| `R` | start a song radio from the highlighted track |
| `f` | add / remove the highlighted (or playing) track from favourites |
| `F` | open the favourites view |
| `d` | remove the highlighted track from the queue or favourites |
| `S` / `c` | shuffle / clear the queue |
| `/` | focus search |
| `t` / `r` / `m` / `s` / `F` / `q` | jump to trending / radio / moods / search / favourites / queue |
| `v` | show / hide the spectrum visualizer |
| `esc` | back or close overlay |
| `T` | re-sync the Omarchy theme |
| `?` | toggle the keybinding overlay |
| `Q` | quit (stops audio and cleans up sockets) |

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
git clone https://github.com/m7sh/ame.git
cd ame
python -m venv .venv
.venv/bin/pip install -r requirements.txt
./ame
```

`ame` is a small launcher that uses `.venv` when present and falls
back to the system `python3`. Symlink it into `~/.local/bin` to run it from
anywhere:

```bash
ln -sf "$PWD/ame" ~/.local/bin/ame
```

---

## Project layout

```
ame/
├── app.py            # Textual app: layout, bindings, workers, theme sync
├── api.py            # Guest-mode ytmusicapi client + yt-dlp stream resolver
├── player.py         # Headless mpv IPC client, queue state machine
├── favourites.py     # JSON-backed persistent favourites store
├── spectrum.py       # cava subprocess bridge for the spectrum visualizer
├── theme.py          # Omarchy palette detection -> native Textual Theme
├── styles.tcss       # Static stylesheet driven by theme CSS variables
├── requirements.txt
├── ame               # Launcher script
└── ui/
    ├── widgets.py    # TopBar, SectionHeader, SeekBar, PlayerBar, Spectrum
    └── views.py      # Trending, Radio, Moods, Playlist, Search, Favourites, Queue
```

---

## How theme sync works

At startup `theme.py` reads
`~/.local/state/omarchy/current/theme/colors.toml`, normalizes the palette
and registers it as a Textual `Theme` with custom variables (`$ame-accent`,
`$ame-muted`, …). `styles.tcss` only ever refers to those variables. When the
file changes, the app registers a fresh theme and reassigns it — Textual
re-applies every variable live, so nothing on disk is touched and the
interface never re-parses a stylesheet mid-session.

## License

MIT
