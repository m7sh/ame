# ame

> A minimal, keyboard-driven YouTube Music player for the terminal.
> Runs entirely in **guest mode** — no login, cookies or API keys.

`ame` (雨, "rain") is built with [Textual](https://textual.textualize.io/), uses
`ytmusicapi` for discovery, direct InnerTube clients (with `yt-dlp` failsafe)
for low-latency stream resolution and 1 MiB byte-range probing, an on-disk LRU
audio cache, and headless `mpv` daemons for gapless playback, equal-power
crossfade, and loudness normalization. The interface is deliberately sparse:
one tab bar, one list, and a slim player — nothing else competing for attention.

> **Disclaimer:** `ame` does **not** host, mirror, or distribute any audio, artwork, or lyrics. It runs purely locally on your device as an unauthenticated client/scraper fetching publicly available internet sources (`YouTube Music`, `yt-dlp`, `LRCLIB`) in a clean, respectful way with bounded local caching.

---

## Design

- **Terminal-native chrome** — no emoji, no boxes-within-boxes. Unicode
  rules, block glyphs and reverse-style selection only.
- **Sidebar & tabbed layout** — collapsible left navigation rail (`b` on terminals `>= 94` cols) paired with a slim top bar: **Home** (`h`/`t`: *for you*, *recently played*, *quick picks*, *charts & mixes*), **Explore** (`e`: *new releases*, *trending*, *moods & genres*), **Radio** (`r`), **Search** (`s`), **Library** (`F`: *liked songs*, *recently played*, *artists*), **Queue** (`q`), **Window** (`w`), **Stats** (`Y`), and **Settings** (`,`).
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
- **Sleek graphic equalizer** — when `cava` is installed, an unobtrusive
  1-row symmetrical block equalizer (`▁▂▃▄▅▆▇█`) animates cleanly above the
  bottom player bar alongside a live EQ indicator on active playback (`v`
  toggles it on/off, omitted automatically if `cava` is absent).

The chrome takes its cues from [cliamp](https://github.com/bjarneo/cliamp):
a letterspaced wordmark, bracketed chips and `▸─ section ──` dividers.

---

## Features

- Zero authentication / guest mode (`YTMusic()` with no credentials).
- Personalized recommendation & mix engine (`recommendations.py`): recency + play-count + favourites taste scoring, audio/video recording deduplication, per-artist diversity caps, time-of-day mood curations, and similar-artist discovery.
- **Explore** (`e`) with live new releases (full album drill-down), trending tracks, and moods & genres playlists.
- Persistent **Library** (`F`) & **Settings** (`,`) — starred songs, local recently played history (`~/.local/share/ame/recent.json`), top artists, and persistent playback preferences (`~/.local/share/ame/settings.json`).
- Infinite radio plus continuous autoplay when the queue runs dry.
- Full queue control: append, play-all, play-next (`i`), reorder (`J`/`K`), shuffle, remove and clear.
- Asynchronous discovery, coalesced stream resolution, 1 MiB pre-playback probing, and mid-playback refusal recovery.
- Bounded 512 MiB on-disk LRU audio cache (`~/.cache/ame/audio`) with chunked prefetching.
- Headless `mpv` playback over JSON IPC with native gapless queue arming, dual-player equal-power crossfade (`x`), loop modes (`l`), playback speed (`C`), sleep timer (`Z`), and YouTube `loudnessDb` normalization (`N`).
- Maximized Now Playing album window (`w`/`m`) with Sixel/TGP/Halfcell cover art, split synced lyrics **open by default** (`L` via LRCLIB / YouTube Music), interactive Up Next side panel (`O`), dynamic album-art accent tinting (`D`), two-row transport controls, and NerdStats.
- Search songs, albums, artists & playlists with album/artist drill-down (`g`/`G`).
- Local-only Replay & listening stats (`Y`) with hourly activity bars, plus MPRIS2 (`playerctl`/Waybar) and Discord Rich Presence.
- Real-time spectrum visualizer via `cava` (optional).

---

## Keyboard

| Key | Action |
| --- | --- |
| `space` | play / pause |
| `n` / `p` | next / previous track |
| `←` / `→` | seek −5s / +5s |
| `+` / `-` | volume up / down |
| `l` | cycle loop mode (`off` / `loop one` / `stop after one`) |
| `x` | cycle crossfade (`off` / `3s` / `6s`) |
| `N` | toggle loudness normalization |
| `C` | cycle playback speed (`1×` / `1.25×` / `1.5×` / `2×` / `0.75×`) |
| `Z` | cycle sleep timer (`off` / `15m` / `30m` / `45m` / `60m`) |
| `j` / `k` | move down / up |
| `J` / `K` | reorder queue item down / up |
| `[` / `]` | switch sub-tab |
| `enter` | play highlighted item, open album / artist / mood, or toggle setting |
| `a` / `i` | append highlighted track or insert as Play Next |
| `A` | queue every track in the current view |
| `P` | play every track in the current view |
| `R` | start a song radio from the highlighted track |
| `g` / `G` | open artist / album page for highlighted or playing track |
| `f` | add / remove the highlighted (or playing) track from favourites |
| `F` | open Library (liked songs, recently played, top artists) |
| `d` | remove the highlighted track from the queue or favourites |
| `S` / `c` | shuffle / clear the queue |
| `/` | focus search |
| `h` / `t` / `e` / `r` / `s` / `F` / `q` / `w` / `Y` / `,` | jump to Home / Explore / Radio / Search / Library / Queue / Window / Stats / Settings |
| `b` | toggle navigation sidebar |
| `w` / `m` | maximize / minimize the Now Playing album window |
| `L` / `O` | toggle synced lyrics / Up Next split panel in Now Playing |
| `D` | toggle dynamic album-art accent tinting |
| `u` | refresh recommendations with fresh picks |
| `U` | check for available updates |
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

## Updating

To update `ame` to the latest version at any time, run:

```bash
ame --update
```

Or from inside the cloned repository:

```bash
./update.sh
```

This will:
- Fetch and fast-forward the latest commits from GitHub
- Automatically install or update dependencies in `.venv` if `requirements.txt` changed
- Safely stash and restore any local modifications

You can also check if updates are available without applying them:

```bash
ame --update --check
```

---

## Uninstallation

To remove `ame` from your system, run the uninstaller directly:

```bash
ame --uninstall
```

Or run the script from the cloned repository:

```bash
./uninstall.sh
```

By default, the uninstaller prompts whether to retain your saved favourites and history (`~/.local/share/ame`), and whether to remove the repository directory.

### Non-interactive flags

```bash
ame --uninstall --purge        # Delete binary symlinks, configs, and all user data
ame --uninstall --keep-data    # Remove binary symlinks but retain favourites and history
ame --uninstall --dry-run      # Preview which files and directories would be removed
ame --uninstall -y             # Proceed without confirmation prompts
```

---

## Project layout

```
ame/
├── app.py              # Textual app: layout, bindings, workers, theme sync
├── api.py              # Guest-mode ytmusicapi client + StreamResolver bridge
├── artwork.py          # Cover art cache, Sixel/Halfcell helpers & accent extraction
├── stream_resolver.py  # InnerTube client walk, 1 MiB probing & yt-dlp failsafe
├── audio_cache.py      # Bounded 512 MiB LRU disk cache with 2 MiB chunked downloads
├── player.py           # Headless mpv IPC daemons: gapless, crossfade, recovery, loudness
├── favourites.py       # JSON-backed persistent favourites store
├── settings.py         # Persistent app preferences & local recently played history
├── lyrics.py           # LRCLIB + YTMusic synced LRC lyrics manager
├── stats.py            # Monthly listening Replay & hourly activity stats store
├── integrations.py     # MPRIS2 & Discord Rich Presence controllers
├── mpris_bridge.py     # System python3 D-Bus MPRIS2 service bridge
├── spectrum.py         # cava subprocess bridge for the spectrum visualizer
├── theme.py            # Omarchy palette detection -> native Textual Theme
├── styles.tcss         # Static stylesheet driven by theme CSS variables
├── requirements.txt
├── ame                 # Launcher script
├── update.sh           # Self-updater script
├── uninstall.sh        # Uninstaller script
└── ui/
    ├── widgets.py      # TopBar, Sidebar, SectionHeader, SeekBar, PlayerBar, NowPlayingView, Spectrum
    └── views.py        # Home, Explore, Mood, Radio, Album/Playlist, Artist, Search, Library, Queue, Stats, Settings
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

## Content & Source Notice

- **No media hosting**: `ame` operates with zero backend servers and never hosts, stores centrally, or redistributes copyrighted audio, images, or lyrics.
- **Public internet scraping**: Discovery, streams, cover artwork, and lyrics are fetched directly on demand from publicly accessible internet endpoints (`ytmusicapi` / YouTube Music InnerTube, `yt-dlp`, and `LRCLIB`) with polite client headers and bounded on-disk caching (`~/.cache/ame`).
- All tracks, metadata, cover art, and lyrics remain the property of their respective copyright holders.

## License

MIT
