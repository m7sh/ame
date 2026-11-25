#!/usr/bin/env python3
"""
mpris_bridge.py - Lightweight D-Bus MPRIS2 service subprocess for ame.

Runs under the system python3 (which provides dbus-python + PyGObject on
Arch/Omarchy) and communicates over newline-delimited JSON on stdin/stdout.
"""

from __future__ import annotations

import json
import sys
import threading

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

BUS_NAME = "org.mpris.MediaPlayer2.ame"
OBJ_PATH = "/org/mpris/MediaPlayer2"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"


def _emit(cmd: str, **kwargs) -> None:
    msg = {"cmd": cmd, **kwargs}
    try:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


class AmeMPRIS(dbus.service.Object):
    def __init__(self, bus: dbus.SessionBus) -> None:
        self._bus_name = dbus.service.BusName(BUS_NAME, bus=bus)
        super().__init__(bus, OBJ_PATH)
        self._props = {
            "PlaybackStatus": "Stopped",
            "LoopStatus": "None",
            "Rate": dbus.Double(1.0),
            "Shuffle": dbus.Boolean(False),
            "Metadata": dbus.Dictionary({}, signature="sv"),
            "Volume": dbus.Double(0.8),
            "Position": dbus.Int64(0),
            "MinimumRate": dbus.Double(0.5),
            "MaximumRate": dbus.Double(2.5),
            "CanGoNext": dbus.Boolean(True),
            "CanGoPrevious": dbus.Boolean(True),
            "CanPlay": dbus.Boolean(True),
            "CanPause": dbus.Boolean(True),
            "CanSeek": dbus.Boolean(True),
            "CanControl": dbus.Boolean(True),
        }
        self._root_props = {
            "CanQuit": dbus.Boolean(True),
            "CanRaise": dbus.Boolean(False),
            "HasTrackList": dbus.Boolean(False),
            "Identity": dbus.String("ame"),
            "DesktopEntry": dbus.String("ame"),
            "SupportedUriSchemes": dbus.Array([], signature="s"),
            "SupportedMimeTypes": dbus.Array([], signature="s"),
        }

    # --- MediaPlayer2 ---
    @dbus.service.method(ROOT_IFACE)
    def Raise(self) -> None:
        pass

    @dbus.service.method(ROOT_IFACE)
    def Quit(self) -> None:
        _emit("quit")

    # --- MediaPlayer2.Player ---
    @dbus.service.method(PLAYER_IFACE)
    def Next(self) -> None:
        _emit("next")

    @dbus.service.method(PLAYER_IFACE)
    def Previous(self) -> None:
        _emit("previous")

    @dbus.service.method(PLAYER_IFACE)
    def Pause(self) -> None:
        _emit("pause")

    @dbus.service.method(PLAYER_IFACE)
    def PlayPause(self) -> None:
        _emit("play_pause")

    @dbus.service.method(PLAYER_IFACE)
    def Stop(self) -> None:
        _emit("stop")

    @dbus.service.method(PLAYER_IFACE)
    def Play(self) -> None:
        _emit("play")

    @dbus.service.method(PLAYER_IFACE, in_signature="x")
    def Seek(self, offset_us: int) -> None:
        _emit("seek_rel", seconds=float(offset_us) / 1_000_000.0)

    @dbus.service.method(PLAYER_IFACE, in_signature="ox")
    def SetPosition(self, _track_id: str, position_us: int) -> None:
        _emit("seek_abs", seconds=max(0.0, float(position_us) / 1_000_000.0))

    @dbus.service.signal(PLAYER_IFACE, signature="x")
    def Seeked(self, position_us: int) -> None:
        pass

    # --- Properties ---
    @dbus.service.method(PROPS_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface_name: str, property_name: str):
        if interface_name == ROOT_IFACE:
            return self._root_props[property_name]
        if interface_name == PLAYER_IFACE:
            return self._props[property_name]
        raise dbus.exceptions.DBusException(f"Unknown interface {interface_name}")

    @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface_name: str):
        if interface_name == ROOT_IFACE:
            return dbus.Dictionary(self._root_props, signature="sv")
        if interface_name == PLAYER_IFACE:
            return dbus.Dictionary(self._props, signature="sv")
        return dbus.Dictionary({}, signature="sv")

    @dbus.service.method(PROPS_IFACE, in_signature="ssv")
    def Set(self, interface_name: str, property_name: str, new_value) -> None:
        if interface_name != PLAYER_IFACE:
            return
        if property_name == "Volume":
            _emit("volume", value=int(round(float(new_value) * 100.0)))
        elif property_name == "Rate":
            _emit("speed", value=float(new_value))

    @dbus.service.signal(PROPS_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, interface_name: str, changed: dict, invalidated: list) -> None:
        pass

    def apply_state(self, state: dict) -> None:
        changed = {}
        if "status" in state:
            self._props["PlaybackStatus"] = dbus.String(state["status"])
            changed["PlaybackStatus"] = self._props["PlaybackStatus"]
        if "loop" in state:
            self._props["LoopStatus"] = dbus.String(state["loop"])
            changed["LoopStatus"] = self._props["LoopStatus"]
        if "volume" in state:
            self._props["Volume"] = dbus.Double(max(0.0, min(1.0, float(state["volume"]) / 100.0)))
            changed["Volume"] = self._props["Volume"]
        if "speed" in state:
            self._props["Rate"] = dbus.Double(float(state["speed"]))
            changed["Rate"] = self._props["Rate"]
        if "position_us" in state:
            self._props["Position"] = dbus.Int64(max(0, int(state["position_us"])))
        if "track" in state:
            tr = state["track"]
            if tr is None:
                meta = dbus.Dictionary({}, signature="sv")
            else:
                tid = str(tr.get("id") or "0")
                safe_tid = "".join(c if c.isalnum() else "_" for c in tid) or "0"
                meta = dbus.Dictionary(
                    {
                        "mpris:trackid": dbus.ObjectPath(f"/com/ame/track/{safe_tid}"),
                        "xesam:title": dbus.String(str(tr.get("title") or "")),
                        "xesam:artist": dbus.Array([dbus.String(str(tr.get("artist") or ""))], signature="s"),
                    },
                    signature="sv",
                )
                if tr.get("album"):
                    meta["xesam:album"] = dbus.String(str(tr["album"]))
                if tr.get("art_url"):
                    meta["mpris:artUrl"] = dbus.String(str(tr["art_url"]))
                dur_us = int(max(0, float(tr.get("duration_s") or 0)) * 1_000_000)
                if dur_us > 0:
                    meta["mpris:length"] = dbus.Int64(dur_us)
            self._props["Metadata"] = meta
            changed["Metadata"] = meta

        if changed:
            self.PropertiesChanged(PLAYER_IFACE, dbus.Dictionary(changed, signature="sv"), dbus.Array([], signature="s"))


def main() -> int:
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    service = AmeMPRIS(bus)
    loop = GLib.MainLoop()

    def _reader() -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            GLib.idle_add(lambda m=msg: (service.apply_state(m), False)[1])
        GLib.idle_add(loop.quit)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    loop.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
