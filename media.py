"""Now playing + transport controls through Windows' media controls (SMTC).

Works for any player that shows up in the Windows volume flyout: Spotify, browsers
(YouTube), the Media Player app, etc. All WinRT calls run on one asyncio loop thread.
"""
import asyncio
import datetime as dt
import threading

from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions

PLAYING = 4   # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING


class Media:
    def __init__(self, app_name):
        self.app_name = app_name          # callable: AppUserModelID -> friendly name
        self.lock = threading.Lock()
        self.current = {}
        self.art = None                   # (bytes, content_type)
        self.art_id = ""
        self.loop = asyncio.new_event_loop()
        self.manager = None
        threading.Thread(target=self._run, daemon=True, name="media").start()

    # ---------- public (called from HTTP threads) ----------
    def snapshot(self):
        with self.lock:
            return dict(self.current)

    def artwork(self):
        with self.lock:
            return self.art

    def control(self, cmd):
        asyncio.run_coroutine_threadsafe(self._control(cmd), self.loop)

    def source_app(self):
        with self.lock:
            return self.current.get("aumid", "")

    # ---------- loop ----------
    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._poll())

    async def _poll(self):
        while True:
            try:
                if self.manager is None:
                    self.manager = await Manager.request_async()
                await self._refresh()
            except Exception:
                self.manager = None
                with self.lock:
                    self.current = {}
            await asyncio.sleep(2)

    async def _refresh(self):
        s = self.manager.get_current_session()
        if s is None:
            with self.lock:
                self.current = {}
            return
        props = await s.try_get_media_properties_async()
        playing = s.get_playback_info().playback_status == PLAYING
        tl = s.get_timeline_properties()
        duration = (tl.end_time - tl.start_time).total_seconds() if tl.end_time else 0
        position = tl.position.total_seconds() if tl.position else 0
        # Players only update the timeline now and then; extrapolate from its timestamp.
        try:
            if playing and tl.last_updated_time:
                age = (dt.datetime.now(dt.timezone.utc) - tl.last_updated_time).total_seconds()
                if 0 < age < 3600:
                    position += age
        except Exception:
            pass
        if duration and position > duration:
            position = duration
        aumid = s.source_app_user_model_id or ""
        title, artist = props.title or "", props.artist or ""
        tid = "%s|%s|%s" % (aumid, title, artist)
        info = {"app": self.app_name(aumid), "aumid": aumid, "playing": playing, "title": title, "artist": artist,
                "album": props.album_title or "", "duration": duration, "position": position, "id": str(abs(hash(tid)))}
        new_track = tid != self.art_id
        if new_track:
            self.art_id = tid
            art = await self._read_thumb(props.thumbnail)
            with self.lock:
                self.art = art
        with self.lock:
            info["hasArt"] = self.art is not None
            self.current = info

    async def _read_thumb(self, ref):
        if ref is None:
            return None
        try:
            stream = await ref.open_read_async()
            size = stream.size
            if not size or size > 5_000_000:
                return None
            buf = Buffer(size)
            await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
            data = bytearray(buf.length)
            DataReader.from_buffer(buf).read_bytes(data)
            return bytes(data), (stream.content_type or "image/png")
        except Exception:
            return None

    async def _control(self, cmd):
        try:
            if self.manager is None:
                self.manager = await Manager.request_async()
            s = self.manager.get_current_session()
            if s is None:
                return
            if cmd == "playpause":
                await s.try_toggle_play_pause_async()
            elif cmd == "next":
                await s.try_skip_next_async()
            elif cmd == "previous":
                await s.try_skip_previous_async()
            await asyncio.sleep(0.3)
            await self._refresh()
        except Exception:
            pass
