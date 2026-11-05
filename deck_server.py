"""RONIN Deck: an old phone or tablet as a Stream Deck-style panel for this Windows laptop.

Windows port of OldPhoneDeck (github.com/GLAUCOMA82/OldPhoneDeck, MIT). The phone page
(web/index.html) is adapted from the original; this server is written from scratch.

The phone opens http://<laptop-ip>:8805/#k=<token> on the same Wi-Fi. Everything except
the page shell needs the token, which lives in data/token. Local network only.
"""
import concurrent.futures
import datetime as dt
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil

import winapps
from media import Media

PORT = 8805
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
WEB = os.path.join(ROOT, "web", "index.html")
LAUNCHER_FILE = os.path.join(DATA, "launcher.json")
CONFIG_FILE = os.path.join(DATA, "config.json")
APP_SLOTS = 7
CREATE_NO_WINDOW = 0x08000000

os.makedirs(DATA, exist_ok=True)


def load_config():
    """Optional data/config.json (see config.example.json). Every key has a default."""
    cfg = {}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    return {
        "name": cfg.get("name") or "Deck · %s" % os.environ.get("COMPUTERNAME", "PC"),
        "note_path": os.path.expanduser(cfg.get("note_path") or os.path.join(ROOT, "notes.md")),
        "note_header": cfg.get("note_header") or "# Deck notes\n\n",
        "note_open_uri": cfg.get("note_open_uri") or "",
    }


CONFIG = load_config()
INBOX_NOTE = CONFIG["note_path"]

# Under pythonw.exe there is no console: stdout/stderr are None, and speedtest-cli
# crashes calling .fileno() on them. Point them at devnull instead.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")


def log(msg):
    line = "%s %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(os.path.join(ROOT, "deck.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_token():
    path = os.path.join(DATA, "token")
    try:
        t = open(path, encoding="utf-8").read().strip()
        if t:
            return t
    except OSError:
        pass
    t = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(12))
    with open(path, "w", encoding="utf-8") as f:
        f.write(t)
    return t


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.1.1", 1))   # no packet is sent; this only picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "localhost"
    finally:
        s.close()


TOKEN = load_token()
PC_NAME = CONFIG["name"]   # shown top-right on the deck


# ---------- stats: network, speed test, battery ----------

class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.history = []        # Mbps down, one per second, newest last
        self.rx = self.tx = 0.0
        self.speed = {}
        self.testing = False
        self.batteries = []
        threading.Thread(target=self._net_loop, daemon=True, name="net").start()
        threading.Thread(target=self._batt_loop, daemon=True, name="batt").start()
        threading.Timer(5, self.run_speed_test).start()

    def snapshot(self):
        with self.lock:
            sp = dict(self.speed)
            sp["running"] = self.testing
            return {"net": {"rx": self.rx, "tx": self.tx, "history": list(self.history)},
                    "speed": sp, "batteries": list(self.batteries), "mac": PC_NAME}

    def _net_loop(self):
        last = psutil.net_io_counters(pernic=True)
        last_t = time.time()
        while True:
            time.sleep(1)
            now = psutil.net_io_counters(pernic=True)
            t = time.time()
            drx = dtx = 0
            for nic, c in now.items():
                if "loopback" in nic.lower() or nic not in last:
                    continue
                drx += max(0, c.bytes_recv - last[nic].bytes_recv)
                dtx += max(0, c.bytes_sent - last[nic].bytes_sent)
            span = max(0.001, t - last_t)
            with self.lock:
                self.rx, self.tx = drx * 8 / span / 1e6, dtx * 8 / span / 1e6
                self.history.append(self.rx)
                del self.history[:-60]
            last, last_t = now, t

    def _batt_loop(self):
        while True:
            items = []
            try:
                b = psutil.sensors_battery()
                if b is not None:
                    items.append({"name": "Laptop", "kind": "laptop", "level": int(round(b.percent)),
                                  "charging": bool(b.power_plugged)})
            except Exception:
                pass
            with self.lock:
                self.batteries = items
            time.sleep(60)

    def run_speed_test(self):
        with self.lock:
            if self.testing:
                return
            self.testing = True
        threading.Thread(target=self._speed, daemon=True, name="speedtest").start()

    def _speed(self):
        result = {}
        try:
            import speedtest
            st = speedtest.Speedtest(secure=True)
            st.get_best_server()
            st.download()
            st.upload(pre_allocate=False)
            r = st.results
            result = {"down": r.download / 1e6, "up": r.upload / 1e6, "ping": r.ping, "at": time.time() * 1000}
            log("speedtest: %.1f down / %.1f up Mbps, %.0f ms" % (result["down"], result["up"], result["ping"]))
        except Exception as e:
            log("speedtest failed: %s" % e)
        with self.lock:
            self.testing = False
            if result:
                self.speed = result


# ---------- launcher selection ----------

def launcher_selected():
    try:
        with open(LAUNCHER_FILE, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, list) else None
    except (OSError, ValueError):
        return None


def launcher_save(paths):
    known = {a["path"] for a in winapps.list_apps()}
    clean = [p for p in paths if p in known][:APP_SLOTS]
    with open(LAUNCHER_FILE, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2, ensure_ascii=False)
    return len(clean)


def app_name(aumid):
    if not aumid:
        return ""
    for a in winapps.list_apps():
        if a["path"].lower() == aumid.lower():
            return a["name"]
    # e.g. "Spotify.exe" -> "Spotify", "Microsoft.ZuneMusic_8wekyb3d8bbwe!App" -> "ZuneMusic"
    base = aumid.split("!")[0].split("\\")[-1]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base.split("_")[0].split(".")[-1] or base


# ---------- RONIN note (the original's "Take note" key) ----------

_note_lock = threading.Lock()


def save_note(text):
    now = dt.datetime.now()
    with _note_lock:
        new = not os.path.exists(INBOX_NOTE)
        with open(INBOX_NOTE, "a", encoding="utf-8") as f:
            if new:
                f.write(CONFIG["note_header"].replace("{date}", now.strftime("%Y-%m-%d")))
            f.write("- [ ] %s — %s\n" % (now.strftime("%Y-%m-%d %H:%M"), text.replace("\n", " ").strip()))
    log("note saved (%d chars)" % len(text))


def open_note():
    """Holding the note key opens the notes: a custom URI (e.g. obsidian://...) if configured, else the file."""
    if CONFIG["note_open_uri"]:
        os.startfile(CONFIG["note_open_uri"])
    elif os.path.exists(INBOX_NOTE):
        os.startfile(INBOX_NOTE)


# ---------- HTTP ----------

stats = Stats()
media = Media(app_name)
icon_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)   # one COM thread for all icon renders


class Handler(BaseHTTPRequestHandler):
    server_version = "RONINDeck/1.0"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body=b"", ctype="text/plain; charset=utf-8", cache=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj):
        self.send(200, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def parse(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
        params = dict(q)
        if self.command == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            if n > 100_000:
                return u.path, q, None
            body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            params.update(urllib.parse.parse_qsl(body, keep_blank_values=True))
        return u.path, q, params

    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def route(self):
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("error on %s %s: %r" % (self.command, self.path.split("?")[0], e))
            try:
                self.send(500, "error")
            except Exception:
                pass

    def _route(self):
        path, q, p = self.parse()
        if p is None:
            return self.send(413, "too big")
        if path in ("/", "/index.html"):
            with open(WEB, "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if not secrets.compare_digest(q.get("k", ""), TOKEN):
            return self.send(403, "bad token")
        m = self.command

        if m == "GET" and path == "/info":
            return self.send_json({"camera": False})
        if m == "GET" and path == "/stats":
            snap = stats.snapshot()
            mus = media.snapshot()
            mus.pop("aumid", None)
            snap["music"] = mus
            return self.send_json(snap)
        if m == "POST" and path == "/speedtest":
            stats.run_speed_test()
            return self.send(200, "ok")
        if m == "POST" and path == "/media":
            cmd = p.get("cmd", "")
            if cmd == "open":
                aid = media.source_app()
                if aid and winapps.is_known(aid):
                    winapps.launch(aid)
            elif cmd in ("playpause", "next", "previous"):
                media.control(cmd)
            return self.send(200, "ok")
        if m == "GET" and path == "/artwork":
            art = media.artwork()
            if not art:
                return self.send(404, "no artwork")
            return self.send(200, art[0], art[1])
        if m == "GET" and path == "/launcher":
            sel = launcher_selected()
            return self.send_json({"selected": sel, "all": winapps.list_apps()})
        if m == "POST" and path == "/launcher":
            paths = [x for x in (p.get("paths") or "").split("\n") if x]
            return self.send_json({"saved": launcher_save(paths)})
        if m == "GET" and path == "/icon":
            aid = p.get("path", "")
            if not winapps.is_known(aid):
                return self.send(404, "unknown app")
            png = icon_pool.submit(winapps.icon, aid).result(timeout=15)
            if not png:
                return self.send(404, "no icon")
            return self.send(200, png, "image/png", cache=True)
        if m == "POST" and path == "/launch":
            aid = p.get("path", "")
            if not winapps.is_known(aid):
                return self.send(404, "unknown app")
            winapps.launch(aid)
            log("launch %s" % aid)
            return self.send(200, "ok")
        if m == "POST" and path == "/claude":   # the page's note endpoint (name kept from the original)
            text = (p.get("text") or "").strip()
            if text:
                save_note(text[:4000])
            else:
                open_note()
            return self.send(200, "ok")
        return self.send(404, "not found")


def main():
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        log("cannot listen on %d: %s" % (PORT, e))
        sys.exit(1)
    httpd.daemon_threads = True
    link = "http://%s:%d/#k=%s" % (lan_ip(), PORT, TOKEN)
    with open(os.path.join(DATA, "url.txt"), "w", encoding="utf-8") as f:
        f.write(link + "\n")
    log("RONIN Deck running on port %d (link in data/url.txt)" % PORT)
    winapps.list_apps()   # warm the app list so the first page load is quick
    httpd.serve_forever()


if __name__ == "__main__":
    main()
