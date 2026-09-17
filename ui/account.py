"""
ui/account.py - Google sign-in overlay for ytmusic-tui.

Drives the OAuth device-code flow with the user's own Google Cloud client and
persists the token so the app can load the library, liked songs and playlists.
"""

import time
import webbrowser

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

import auth
from ui.widgets import _palette


INSTRUCTIONS = """\
Sign in with your own Google OAuth client (Google requires this for
unofficial clients). One-time setup:

  1. Enable the YouTube Data API v3
  2. Create an OAuth client of type "Desktop app"
  3. Paste the Client ID and Client Secret below

console.cloud.google.com/apis/credentials"""


class AccountScreen(ModalScreen):
    """Sign in / sign out of Google."""

    BINDINGS = [Binding("escape", "dismiss", "Close", show=False)]

    class SignedIn(Message):
        """Posted after a successful sign-in."""

    class SignedOut(Message):
        """Posted after signing out."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._signed_in = auth.is_signed_in()

    def compose(self) -> ComposeResult:
        t = _palette(self)
        with Container(id="account_panel"):
            yield Static(f"[bold {t.accent}]ACCOUNT[/]", classes="help_title")

            if self._signed_in:
                yield Static(f"[{t.foreground}]Google account connected[/]", id="account_body")
                yield Static(
                    f"[{t.muted}]Library, liked songs and playlists are synced.[/]",
                    id="account_note",
                )
                with Horizontal(id="account_buttons"):
                    yield Button("Sign out", id="account_signout", variant="error")
                    yield Button("Close", id="account_close")
            else:
                yield Static(f"[{t.foreground}]{INSTRUCTIONS}[/]", id="account_body")
                yield Input(placeholder="OAuth Client ID", id="account_client_id")
                yield Input(
                    placeholder="OAuth Client Secret",
                    password=True,
                    id="account_client_secret",
                )
                with Horizontal(id="account_buttons"):
                    yield Button("Sign in", id="account_signin", variant="primary")
                    yield Button("Close", id="account_close")
                yield Static("", id="account_status")

    def on_mount(self) -> None:
        saved = auth.load_client()
        if saved and not self._signed_in:
            try:
                self.query_one("#account_client_id", Input).value = saved[0]
                self.query_one("#account_client_secret", Input).value = saved[1]
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Buttons
    # ------------------------------------------------------------------ #
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "account_close":
            self.dismiss()
        elif event.button.id == "account_signout":
            auth.sign_out()
            self.post_message(self.SignedOut())
            self.dismiss()
        elif event.button.id == "account_signin":
            self._begin_signin()

    def _begin_signin(self) -> None:
        try:
            client_id = self.query_one("#account_client_id", Input).value.strip()
            client_secret = self.query_one("#account_client_secret", Input).value.strip()
        except Exception:
            return

        if not client_id or not client_secret:
            self._status("enter both the Client ID and Client Secret")
            return

        auth.save_client(client_id, client_secret)
        self._status("contacting Google…")
        self._run_device_flow(client_id, client_secret)

    # ------------------------------------------------------------------ #
    # Device flow (background thread)
    # ------------------------------------------------------------------ #
    @work(exclusive=True, thread=True)
    def _run_device_flow(self, client_id: str, client_secret: str) -> None:
        try:
            credentials, code = auth.begin_device_flow(client_id, client_secret)
        except Exception as exc:
            self._call(self._status, f"could not start sign-in: {exc}")
            return

        url = code.get("verification_url", "https://www.google.com/device")
        user_code = code.get("user_code", "")
        device_code = code.get("device_code", "")
        interval = max(2, int(code.get("interval", 5)))

        self._call(self._show_code, url, user_code)
        try:
            webbrowser.open(f"{url}?user_code={user_code}")
        except Exception:
            pass

        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(interval)
            try:
                raw = auth.poll_device_token(credentials, device_code)
            except Exception as exc:
                self._call(self._status, f"sign-in failed: {exc}")
                return
            if raw:
                try:
                    auth.save_token(credentials, raw)
                except Exception as exc:
                    self._call(self._status, f"could not save token: {exc}")
                    return
                self._call(self._signed_in)
                return

        self._call(self._status, "timed out — press Sign in to try again")

    def _call(self, callback, *args) -> None:
        try:
            self.app.call_from_thread(callback, *args)
        except Exception:
            pass

    def _show_code(self, url: str, user_code: str) -> None:
        self._status(f"go to {url}  ·  enter code  {user_code}")

    def _signed_in(self) -> None:
        self.post_message(self.SignedIn())
        self.dismiss()

    def _status(self, text: str) -> None:
        try:
            t = _palette(self)
            self.query_one("#account_status", Static).update(f"[{t.muted}]{text}[/]")
        except Exception:
            pass
