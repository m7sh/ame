# ⚡ YouTube Music TUI Player

> **Modern, Standalone, and Beautiful Terminal Music Player for YouTube Music.**
> Requires **ZERO authentication or login** (operates in 100% Guest Mode).

---

## ✨ Features & Architecture

- 🛡️ **Zero Login / Guest Mode**: Initialized via `YTMusic()` with no Google credentials, cookies, or auth JSON required.
- 🎨 **Dark Cyberpunk / Dracula Aesthetic**:
  - Background: `#121212`, Surfaces: `#1e1e24`, Accents: Cyan `#00e5ff` & Purple `#bd93f9`, Zinc borders `#27272a`.
- ⚡ **Asynchronous & 60fps Responsive**: All discovery searches, playlist retrievals, and stream extractions run in dedicated background worker threads (`@work(exclusive=True, thread=True)`).
- 🎵 **High-Fidelity Audio Playback**:
  - Stream URLs extracted on the fly via `yt-dlp` (`bestaudio`, Opus / AAC) with in-memory caching.
  - Headless background `mpv` worker daemon communicating through JSON UNIX domain sockets (`--no-video --idle --input-ipc-server`).
- 📻 **Infinite Radio & Continuous Autoplay**:
  - Start an algorithmic radio station from any highlighted song (`get_watch_playlist`).
  - Continuous Autoplay keeps your music streaming indefinitely by automatically fetching recommendations based on the last played song when your queue ends.
- 📂 **Moods & Curated Playlists**:
  - Explore mood categories (Chill, Workout, Focus, Gaming, Romance, Energize) and musical genres (Dance & Electronic, Rock, Pop, Hip-Hop, Classical).
  - Inspect any playlist to view track count, creator metadata, full track list, with **Play All** or **Queue All**.
- 🔍 **Instant Search**:
  - Full-text search across songs and public community playlists with instant tabbed results.
- 📋 **Full Queue Control**:
  - Append (`a`), Play Next, Shuffle Queue (`s`), Clear Queue (`c`), and Jump to any queued track.

---

## 🕹️ Keyboard Shortcuts (Vim-Inspired)

| Key | Action |
| --- | --- |
| `/` | Focus Search Input |
| `Space` | Toggle Play / Pause |
| `n` | Next Track in Queue |
| `p` | Previous Track (or restart current if > 3s) |
| `j` / `Down` | Navigate down through lists and tables |
| `k` / `Up` | Navigate up through lists and tables |
| `Enter` | Play highlighted track immediately / Open playlist |
| `a` | Append highlighted track or playlist to queue |
| `r` | Start algorithmic song radio from highlighted track |
| `Tab` | Switch focus between Sidebar and Main Content |
| `+` / `=` | Increase volume (+5%) |
| `-` / `_` | Decrease volume (-5%) |
| `←` / `→` | Seek backward / forward 5 seconds |
| `s` | Shuffle remaining queue |
| `c` | Clear active queue |
| `Esc` | Return to previous view or defocus search bar |
| `1` – `5` | Quick switch view (1: Trending, 2: Radio, 3: Moods, 4: Search, 5: Queue) |
| `t` | Sync / reload theme from active Omarchy desktop theme |
| `q` | Safely stop audio, clean IPC sockets/processes, and exit |

---

## 📁 Project Structure

```
ytmusic-tui/
├── app.py              # Main Textual App class, event handlers, and keybindings
├── api.py              # Async-wrapped ytmusicapi guest client and yt-dlp stream resolver
├── player.py           # Headless MPV IPC client, audio state machine, and queue manager
├── styles.tcss         # Modern Cyberpunk/Dracula stylesheet (layouts, themes, colors)
├── requirements.txt    # Project dependencies (textual, ytmusicapi, yt-dlp, requests)
├── ytmusic-tui         # Direct executable launcher script
└── ui/
    ├── __init__.py     # UI package module
    ├── views.py        # Views for Trending, Radio, Moods, Playlist Detail, Search, Queue
    └── widgets.py      # Sticky bottom player bar, Unicode seek bar, header, and sidebar
```

---

## 🚀 Installation & Running

### Dependencies
Ensure `mpv` and `yt-dlp` are installed on your Linux system:
```bash
sudo pacman -S mpv yt-dlp   # Arch Linux / Omarchy
# or
sudo apt install mpv yt-dlp # Ubuntu / Debian
```

### Quick Run
Since `ytmusic-tui` is linked to your `~/.local/bin`, you can launch it from any terminal:
```bash
ytmusic-tui
```

Or run directly from this directory:
```bash
source .venv/bin/activate
python app.py
```
