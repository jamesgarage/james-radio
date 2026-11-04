# /// script
# requires-python = ">=3.10"
# dependencies = ["sounddevice", "numpy"]
# ///
"""Super Sound Pad: 24 synthesized sounds on glowing toy-style pads, in an app window.

    uv run soundboard.py                          # Windows default output
    uv run soundboard.py --out "Speakers (USB"

Keys 123456 / QWERTY / ASDFGH / ZXCVBN play the pads (the grid's shape on the keyboard); Space stops all.
The page is served on 127.0.0.1 only; closing the window ends the program.
"""
import argparse
import json
import os
import secrets
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import numpy as np
import sounddevice as sd

SR = 48000
rng = np.random.default_rng(7)


def t(sec):
    return np.arange(int(SR * sec)) / SR


def fade(n, attack=0.005, release=0.05):
    """Fade in/out so no sound starts or ends with a click."""
    e = np.ones(n)
    a, r = int(SR * attack), int(SR * release)
    e[:a] = np.linspace(0, 1, a)
    e[-r:] = np.linspace(1, 0, r)
    return e


def place(buf, snd, at):
    i = int(SR * at)
    n = min(len(snd), len(buf) - i)
    buf[i:i + n] += snd[:n]


def tone(f, x):
    """Sine at a frequency that may change over time (f is a scalar or an array the length of x)."""
    return np.sin(2 * np.pi * np.cumsum(np.broadcast_to(f, x.shape)) / SR)


def drum(f0, sec):
    x = t(sec)
    return tone(f0 * (1 + 1.5 * np.exp(-x * 30)), x) * np.exp(-x * 12)


def noise_hit(sec, decay):
    x = t(sec)
    return np.diff(rng.standard_normal(len(x)), prepend=0) * np.exp(-x / decay)


def air_horn():
    x = t(1.4)
    s = sum(2 * ((f * x) % 1) - 1 for f in (233, 294, 349, 466)) / 4
    gate = ((x < 0.18) | ((x > 0.25) & (x < 0.43)) | (x > 0.5)).astype(float)
    return s * np.convolve(gate, np.ones(240) / 240, "same") * fade(len(x))


def rimshot():
    out = np.zeros(int(SR * 1.7))
    place(out, drum(180, 0.3), 0.0)
    place(out, drum(140, 0.3), 0.18)
    place(out, drum(120, 0.3) * 0.5, 0.42)
    place(out, noise_hit(1.2, 0.25) * 0.4, 0.42)
    return out


def bandpass(sig, lo, hi):
    f = np.fft.rfftfreq(len(sig), 1 / SR)
    spec = np.fft.rfft(sig)
    spec[(f < lo) | (f > hi)] = 0
    return np.fft.irfft(spec, len(sig))


def applause():
    """A crowd: ~190 claps a second drawn from 24 differently-pitched hands, over a hiss of distant ones."""
    sec = 3.5
    out = np.zeros(int(SR * sec))
    n = int(SR * 0.03)
    hands = []
    for _ in range(24):
        c = bandpass(rng.standard_normal(n), rng.uniform(700, 1200), rng.uniform(2500, 4500))
        c *= np.exp(-np.arange(n) / (SR * rng.uniform(0.004, 0.009)))
        hands.append(c / np.max(np.abs(c)))
    for at in rng.uniform(0, sec - 0.05, 650):
        place(out, hands[rng.integers(len(hands))] * rng.uniform(0.3, 1), at)
    bed = bandpass(rng.standard_normal(len(out)), 500, 5000)
    out += 0.25 * bed / np.max(np.abs(bed))
    x = t(sec)
    return out * np.minimum(1, x / 0.3) * np.minimum(1, (sec - x) / 1.2)


def drum_roll():
    out = np.zeros(int(SR * 3.2))
    for at in np.arange(0, 1.8, 1 / 28):
        place(out, noise_hit(0.04, 0.008) * (0.15 + 0.6 * at / 1.8), at)
    place(out, drum(60, 0.5), 1.85)
    place(out, noise_hit(1.3, 0.35) * 0.6, 1.85)
    return out


def ding():
    x = t(1.8)
    return sum(a * np.sin(2 * np.pi * f * x) * np.exp(-x * d)
               for f, a, d in ((880, 1, 3), (1760, 0.4, 5), (2640, 0.2, 8))) * fade(len(x), 0.002, 0.1)


def buzzer():
    x = t(0.9)
    return np.sign(np.sin(2 * np.pi * 110 * x)) * 0.5 * fade(len(x), 0.005, 0.03)


def note(f, sec, vibrato=0.0):
    x = t(sec)
    ph = 2 * np.pi * np.cumsum(f * (1 + vibrato * np.sin(2 * np.pi * 5 * x))) / SR
    return sum(np.sin(k * ph) / k for k in range(1, 7)) * fade(len(x), 0.02, 0.08)


def sad_trombone():
    return np.concatenate([note(293.7, 0.45), note(277.2, 0.45), note(261.6, 0.45), note(246.9, 1.4, 0.025)])


def siren():
    x = t(2.4)
    return tone(900 + 350 * np.sin(2 * np.pi * 0.8 * x), x) * fade(len(x))


def lowpass(sig, cutoff):
    f = np.fft.rfftfreq(len(sig), 1 / SR)
    return np.fft.irfft(np.fft.rfft(sig) / (1 + (f / cutoff) ** 2), len(sig))


def bell(f, sec, decay):
    """Inharmonic partials (1 : 2.76 : 5.4) are what make it sound struck rather than played."""
    x = t(sec)
    return sum(a * np.sin(2 * np.pi * f * k * x) * np.exp(-x * decay * k)
               for k, a in ((1, 1), (2.76, 0.5), (5.4, 0.25))) * fade(len(x), 0.002, 0.1)


def chord(freqs, sec, vibrato=0.0):
    return sum(note(f, sec, vibrato) for f in freqs) / len(freqs)


def ta_da():
    return np.concatenate([chord((392, 523.3, 659.3), 0.16), np.zeros(int(SR * 0.05)),
                           chord((523.3, 659.3, 784), 1.3, 0.012)])


def cha_ching():
    out = np.zeros(int(SR * 1.8))
    place(out, noise_hit(0.08, 0.015) * 0.6 + drum(90, 0.08) * 0.5, 0.0)
    place(out, bell(2093, 1.6, 2.5) * 0.6, 0.12)
    place(out, bell(2637, 1.6, 2.5) * 0.6, 0.16)
    return out


def crickets():
    x = t(3.5)
    smooth = lambda g: np.convolve(g.astype(float), np.ones(96) / 96, "same")
    a = np.sin(2 * np.pi * 4600 * x) * smooth((np.sin(2 * np.pi * 30 * x) > 0.3) & ((x % 0.55) < 0.12))
    b = np.sin(2 * np.pi * 4300 * x) * smooth((np.sin(2 * np.pi * 27 * x) > 0.3) & (((x + 0.27) % 0.6) < 0.1))
    return (a + 0.6 * b) * fade(len(x), 0.05, 0.3)


def laser():
    out = np.zeros(int(SR * 0.8))
    x = t(0.18)
    shot = tone(1800 * np.exp(-x * 12) + 150, x) * np.exp(-x * 8) * fade(len(x), 0.002, 0.02)
    for at in (0, 0.25, 0.5):
        place(out, shot, at)
    return out


def boing():
    x = t(1.2)
    f = (180 + 120 * (1 - np.exp(-x * 6))) * (1 + 0.25 * np.sin(2 * np.pi * 14 * x) * np.exp(-x * 3))
    return tone(f, x) * np.exp(-x * 2.5) * fade(len(x), 0.002, 0.1)


def countdown():
    out = np.zeros(int(SR * 3.1))
    beep = lambda f, sec: np.sin(2 * np.pi * f * t(sec)) * fade(int(SR * sec), 0.005, 0.02)
    for at in (0, 0.8, 1.6):
        place(out, beep(880, 0.15), at)
    place(out, beep(1760, 0.6), 2.4)
    return out


def explosion():
    x = t(3)
    rumble = lowpass(rng.standard_normal(len(x)), 250)
    out = rumble / np.max(np.abs(rumble)) * np.exp(-x * 1.6) * fade(len(x), 0.003, 0.3)
    place(out, noise_hit(0.12, 0.03) * 0.8, 0)
    place(out, drum(45, 0.8), 0)
    return out


def record_scratch():
    out = np.zeros(int(SR * 0.7))
    x = t(0.25)
    for at, (lo, hi) in ((0, (200, 1200)), (0.3, (1100, 250))):
        f = np.linspace(lo, hi, len(x)) * (1 + 0.3 * np.sin(np.pi * x / 0.25))
        saw = 2 * ((np.cumsum(f) / SR) % 1) - 1
        place(out, (saw + 0.4 * rng.standard_normal(len(x))) * fade(len(x), 0.01, 0.03), at)
    return out


def echo(sig, taps):
    out = np.concatenate([sig, np.zeros(int(SR * max(d for d, _ in taps)))])
    for d, g in taps:
        i = int(SR * d)
        out[i:i + len(sig)] += g * sig
    return out


def fart():
    x = t(1.1)
    wobble = lowpass(rng.standard_normal(len(x)), 6)
    wobble /= np.max(np.abs(wobble))
    buzz = 2 * ((np.cumsum(95 + 25 * wobble - 25 * x) / SR) % 1) - 1
    flutter = 0.6 + 0.4 * np.sin(2 * np.pi * 22 * x + 3 * wobble)
    return lowpass(buzz * flutter + 0.3 * rng.standard_normal(len(x)), 700) * fade(len(x), 0.03, 0.3)


def slide_whistle():
    x = t(1.3)
    f = np.interp(x, [0, 0.6, 1.3], [600, 1800, 500]) * (1 + 0.012 * np.sin(2 * np.pi * 6 * x))
    breath = bandpass(rng.standard_normal(len(x)), 1000, 4000) * 0.05
    return (tone(f, x) + breath) * fade(len(x), 0.03, 0.12)


def bubbles():
    out = np.zeros(int(SR * 1.6))
    for at in np.sort(rng.uniform(0, 1.4, 11)):
        sec = rng.uniform(0.05, 0.1)
        x = t(sec)
        place(out, tone(rng.uniform(450, 1000) * (1 + 1.8 * x / sec), x) * np.exp(-x * 40)
              * fade(len(x), 0.002, 0.01), at)
    return out


def ghost():
    x = t(2.4)
    f = np.interp(x, [0, 0.8, 1.6, 2.4], [260, 520, 380, 220]) * (1 + 0.04 * np.sin(2 * np.pi * 5.5 * x))
    voice = (tone(f, x) + 0.5 * tone(f * 1.505, x)) * fade(len(x), 0.4, 0.8)
    return echo(voice, ((0.13, 0.45), (0.27, 0.25), (0.41, 0.12)))


def robot():
    out = np.zeros(int(SR * 1.2))
    x = t(0.09)
    for k, f in enumerate(rng.choice([330, 440, 554, 660, 880, 990, 1320], 9)):
        blip = np.sign(np.sin(2 * np.pi * f * x)) * (0.6 + 0.4 * np.sign(np.sin(2 * np.pi * 60 * x)))
        place(out, blip * fade(len(x), 0.003, 0.01) * 0.6, k * 0.12)
    return out


def power_up():
    out = np.zeros(int(SR * 0.9))
    x = t(0.09)
    for k, f in enumerate((262, 330, 392, 523, 659, 784)):
        place(out, np.sign(np.sin(2 * np.pi * f * x)) * fade(len(x), 0.002, 0.01) * 0.5, k * 0.07)
    x = t(0.4)
    place(out, np.sign(np.sin(2 * np.pi * 1047 * x)) * np.exp(-x * 6) * fade(len(x), 0.002, 0.05) * 0.5, 0.42)
    return out


def magic():
    out = np.zeros(int(SR * 2.0))
    for k, f in enumerate((1047, 1319, 1568, 2093, 2637, 3136)):
        place(out, bell(f, 1.2, 3) * 0.5, k * 0.07)
    for at in rng.uniform(0.3, 1.2, 14):
        place(out, bell(rng.uniform(2500, 4000), 0.4, 8) * 0.2, at)   # 5.4x partial stays under 24 kHz
    return out


def car_horn():
    out = np.zeros(int(SR * 0.9))
    x = t(0.28)
    honk = lowpass(sum(2 * ((f * x) % 1) - 1 for f in (415, 523)) / 2, 2500) * fade(len(x), 0.01, 0.03)
    place(out, honk, 0)
    place(out, honk, 0.4)
    return out


# (name, picture, sound) in keyboard order; the page colors the six columns as a rainbow
SOUNDS = [("Air horn", "📯", air_horn), ("Applause", "👏", applause), ("Ba-dum-tss", "🥁", rimshot),
          ("Drum roll", "🪘", drum_roll), ("Ta-da!", "🎉", ta_da), ("Cha-ching", "💰", cha_ching),
          ("Ding!", "🔔", ding), ("Buzzer", "❌", buzzer), ("Sad trombone", "😢", sad_trombone),
          ("Siren", "🚨", siren), ("Car horn", "🚗", car_horn), ("Countdown", "⏱️", countdown),
          ("Fart", "💨", fart), ("Boing", "🦘", boing), ("Slide whistle", "🎶", slide_whistle),
          ("Bubbles", "🫧", bubbles), ("Crickets", "🦗", crickets), ("Ghost", "👻", ghost),
          ("Laser", "🛸", laser), ("Explosion", "💥", explosion), ("Robot", "🤖", robot),
          ("Power up", "⭐", power_up), ("Magic", "✨", magic), ("Record scratch", "💿", record_scratch)]
KEYS = "123456qwertyasdfghzxcvbn"   # the grid's shape on the keyboard


TARGET_RMS = 0.3   # every sound gets about the same loudness, not just the same peak


def limit(y):
    """Linear up to 0.8, then eases toward 1.0: loud without crackle."""
    a = np.abs(y)
    return np.where(a < 0.8, y, np.sign(y) * (0.8 + 0.2 * np.tanh((a - 0.8) / 0.2)))


def render():
    out = []
    for name, picture, make in SOUNDS:
        s = make()
        s = s / np.max(np.abs(s))
        gain = min(TARGET_RMS / np.sqrt(np.mean(s ** 2)), 5)   # sparse sounds (claps, bells, the blast) get pushed up
        out.append((name, picture, limit(s * gain).astype(np.float32)))
    return out


class Mixer:
    """One output stream; overlapping sounds are summed in the callback."""

    def __init__(self, out_part):
        self.out_part, self.voices, self.lock, self.volume, self.stream = out_part, [], threading.Lock(), 1.0, None
        self.level, self.open_lock, self.device, self.fallback = 0.0, threading.Lock(), "", False
        self.closed = False

    def _open(self):
        """(Re)open on the asked-for speaker, or on Windows' default output while that one is away."""
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        self.device = "No speaker"   # what the page shows until a stream opens
        sd._terminate()              # re-scan devices: PortAudio only knows the list from when it started
        sd._initialize()
        api = next(i for i, a in enumerate(sd.query_hostapis()) if a["name"] == "Windows WASAPI")
        wanted = [i for i, d in enumerate(sd.query_devices()) if self.out_part and d["hostapi"] == api
                  and d["max_output_channels"] > 0 and self.out_part.lower() in d["name"].lower()]
        dev = wanted[0] if wanted else sd.query_hostapis(api)["default_output_device"]
        if dev < 0:
            raise LookupError("Windows has no output device")
        self.fallback = bool(self.out_part) and not wanted
        ch = min(2, sd.query_devices(dev)["max_output_channels"])
        self.stream = sd.OutputStream(device=dev, samplerate=SR, channels=ch, dtype="float32", latency="low",
                                      callback=self._mix, extra_settings=sd.WasapiSettings(auto_convert=True))
        self.stream.start()
        self.device = sd.query_devices(dev)["name"]
        return self.device

    def watch(self):
        """Every 5 s: reopen a dead stream, and while on the fallback speaker, look for the real one again."""
        while True:
            time.sleep(5)
            with self.open_lock:
                if self.closed:
                    return
                dead = self.stream is None or not self.stream.active
                if dead or (self.fallback and not self.voices):   # only switch back between sounds
                    try:
                        self._open()
                    except Exception:
                        pass

    def _mix(self, outdata, frames, when, status):
        mix = np.zeros(frames, dtype=np.float32)
        with self.lock:
            still = []
            for snd, pos in self.voices:
                chunk = snd[pos:pos + frames]
                mix[:len(chunk)] += chunk
                if pos + frames < len(snd):
                    still.append((snd, pos + frames))
            self.voices = still
        out = limit(mix * self.volume)
        self.level = float(np.sqrt(np.mean(out ** 2)))   # drives the page's LED meter
        outdata[:] = out[:, None]

    def play(self, snd):
        with self.open_lock:   # button presses arrive on several server threads at once
            if self.stream is None or not self.stream.active:
                try:
                    self._open()
                except Exception:
                    return     # no speaker: drop the sound rather than queue a pile-up that all plays at once later
        with self.lock:
            self.voices.append((snd, 0))

    def stop_all(self):
        with self.lock:
            self.voices = []

    def close(self):
        with self.open_lock:   # not halfway through watch() swapping streams
            self.closed = True
            if self.stream is not None:
                self.stream.close()
                self.stream = None


# Pad colors per column: (top, bottom, side skirt, label text)
RAINBOW = [("#ff6161", "#d81e1e", "#8e0f0f", "#fff"), ("#ffad45", "#f07800", "#9a4b00", "#fff"),
           ("#ffe65e", "#f2c200", "#9c7a00", "#4a3500"), ("#66e680", "#1fae3f", "#146b28", "#fff"),
           ("#66b8ff", "#1677e6", "#0d4a91", "#fff"), ("#c896ff", "#8a3ff0", "#561f9e", "#fff")]


def make_server(mixer, sounds):
    token = secrets.token_urlsafe(16)   # only this page may press the buttons
    cfg = [{"name": n, "emoji": e, "key": k, "dur": round(len(s) / SR, 2),
            "c": RAINBOW[i % 6][0], "d": RAINBOW[i % 6][1], "e": RAINBOW[i % 6][2], "t": RAINBOW[i % 6][3]}
           for i, ((n, e, s), k) in enumerate(zip(sounds, KEYS))]
    page = PAGE.replace("__CONFIG__", json.dumps(cfg)).replace("__TOKEN__", token).encode("utf-8")
    state = {"last_poll": None, "bye_at": None}
    hosts = set()   # the only Host headers we answer, filled in once the port is known

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def parse_request(self):
            # a web page that re-points its own domain at 127.0.0.1 (DNS rebinding) arrives with a foreign Host
            if not super().parse_request():
                return False
            if self.headers.get("Host", "") not in hosts:
                self._send(403)
                return False
            return True

        def _send(self, code, body=b"", ctype="text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/level":
                state["last_poll"] = time.monotonic()   # the page's poll doubles as its heartbeat
                self._send(200, json.dumps({"level": mixer.level, "speaker": mixer.device,
                                            "volume": round(mixer.volume * 100)}).encode(), "application/json")
            else:
                self._send(404)

        def do_POST(self):
            u = urlsplit(self.path)
            if parse_qs(u.query).get("t", [""])[0] != token:
                return self._send(403)
            what, _, arg = u.path.strip("/").partition("/")
            try:
                if what == "play" and 0 <= int(arg) < len(sounds):
                    mixer.play(sounds[int(arg)][2])
                elif what == "stop":
                    mixer.stop_all()
                elif what == "volume":
                    mixer.volume = max(0.0, min(float(arg), 150.0)) / 100
                elif what == "bye":
                    state["bye_at"] = time.monotonic()
                else:
                    return self._send(404)
            except ValueError:
                return self._send(400)
            self._send(204)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    hosts.update({f"127.0.0.1:{port}", f"localhost:{port}"})
    return server, f"http://127.0.0.1:{port}/", state


def open_app_window(url):
    """Edge's app mode = a plain window with no tabs or address bar; any browser if Edge is missing."""
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        edge = os.path.join(base or "", "Microsoft", "Edge", "Application", "msedge.exe")
        if base and os.path.exists(edge):
            subprocess.Popen([edge, f"--app={url}", "--window-size=1400,920"])
            return
    webbrowser.open(url)


def main():
    p = argparse.ArgumentParser(description="Super Sound Pad: 24 sounds on glowing toy-style pads.")
    p.add_argument("--out", default="", help="part of the output device's name (default: Windows default)")
    p.add_argument("--no-window", action="store_true", help="serve the page but don't open a window")
    a = p.parse_args()

    mixer = Mixer(a.out)
    try:
        device = mixer._open()
    except Exception as e:   # no speaker yet: the page still opens and watch() keeps looking every 5 s
        device = f"no speaker yet ({e})"
    server, url, state = make_server(mixer, render())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=mixer.watch, daemon=True).start()
    print(f"Super Sound Pad on {url} -> {device}", flush=True)
    if not a.no_window:
        open_app_window(url)

    started = time.monotonic()
    try:
        while True:
            time.sleep(1)
            now, last, bye = time.monotonic(), state["last_poll"], state["bye_at"]
            if bye and (last is None or last < bye) and now - bye > 3:
                break                               # window closed (a reload polls again within 3 s)
            if last and now - last > 150:
                break                               # page gone without saying bye (minimized pages poll slowly)
            if last is None and now - started > 90:
                break                               # page never opened
    except KeyboardInterrupt:
        pass                                        # Ctrl+C in the console window: stop the same clean way
    server.shutdown()
    server.server_close()
    mixer.close()
    print("Window closed; Super Sound Pad stopped.", flush=True)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Super Sound Pad</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
html { font-size: min(1vw, 1.08vh); }   /* 1rem = one unit; the whole toy scales with the window */
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
body { margin: 0; height: 100vh; display: grid; place-items: center; overflow: hidden; user-select: none;
  touch-action: manipulation; font-family: "Segoe UI", system-ui, sans-serif;
  background: radial-gradient(ellipse at 50% 35%, #4b5160 0%, #1d2027 55%, #0d0e11 100%); }
.device { position: relative; width: 96rem; padding: 3rem 3.2rem 3.2rem; border-radius: 3.2rem;
  background: linear-gradient(180deg, #5a606c 0%, #363a43 3%, #2a2d34 50%, #202328 97%, #121316 100%);
  box-shadow: 0 2.5rem 5rem rgba(0,0,0,.75), 0 .6rem 0 #0e0f12,
              inset 0 .15rem 0 rgba(255,255,255,.25), inset 0 -.2rem .4rem rgba(0,0,0,.6); }
.device::before { content: ""; position: absolute; inset: 0; border-radius: inherit; pointer-events: none;
  background: repeating-linear-gradient(90deg, rgba(255,255,255,.022) 0 1px, rgba(0,0,0,.03) 1px 2px, transparent 2px 4px); }
.screw { position: absolute; width: 1.9rem; height: 1.9rem; border-radius: 50%;
  background: radial-gradient(circle at 35% 30%, #e3e6ea, #848a93 55%, #3d4148);
  box-shadow: 0 .12rem .25rem rgba(0,0,0,.8), inset 0 0 0 .08rem rgba(0,0,0,.35); }
.screw::after { content: ""; position: absolute; left: 18%; right: 18%; top: 44%; height: 12%;
  background: #2a2d33; border-radius: .1rem; transform: rotate(var(--r)); }
.s1 { top: 1.1rem; left: 1.1rem; --r: 30deg } .s2 { top: 1.1rem; right: 1.1rem; --r: -50deg }
.s3 { bottom: 1.1rem; left: 1.1rem; --r: 80deg } .s4 { bottom: 1.1rem; right: 1.1rem; --r: 10deg }

.top { display: flex; align-items: center; gap: 2.4rem; margin-bottom: 2.4rem; }
.brand { color: #eef0f3; font-weight: 900; font-size: 3rem; letter-spacing: .06em; line-height: .95;
  text-shadow: 0 .15rem 0 #000, 0 -.08rem 0 rgba(255,255,255,.2); }
.brand b { background: linear-gradient(90deg, #ff6161, #ffad45, #ffe65e, #66e680, #66b8ff, #c896ff);
  -webkit-background-clip: text; background-clip: text; color: transparent; text-shadow: none;
  filter: drop-shadow(0 .15rem 0 #000); }
.brand small { display: block; font-size: 1.15rem; font-weight: 700; color: #9aa1ab; letter-spacing: .32em; margin-top: .7rem; }
.lcd { flex: 1; height: 7.4rem; border-radius: 1.1rem; padding: 0 2.2rem; position: relative; overflow: hidden;
  display: flex; align-items: center; justify-content: space-between; gap: 2rem;
  background: linear-gradient(180deg, #0a1d10, #10301b); border: .5rem solid #0b0c0e;
  box-shadow: inset 0 .5rem 1.2rem rgba(0,0,0,.9), 0 .12rem 0 rgba(255,255,255,.14); }
.lcd::after { content: ""; position: absolute; inset: 0; pointer-events: none;
  background: repeating-linear-gradient(0deg, rgba(0,0,0,.28) 0 2px, transparent 2px 4px),
              linear-gradient(160deg, rgba(255,255,255,.08), transparent 40%); }
#now { font: 700 2.9rem Consolas, "Courier New", monospace; color: #62ff8f; letter-spacing: .12em;
  text-transform: uppercase; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  text-shadow: 0 0 .9rem rgba(98,255,143,.85); }
.right { display: flex; flex-direction: column; align-items: flex-end; gap: .5rem; flex: none; }
#spk { font: 700 1.15rem Consolas, monospace; color: #3fb866; letter-spacing: .1em; text-transform: uppercase; }
.meter { display: flex; gap: .45rem; flex: none; }
.meter i { width: .95rem; height: 3.4rem; border-radius: .2rem; background: #163020; }
.meter i:nth-child(n+11) { background: #35300f; }
.meter i:nth-child(n+14) { background: #3a1414; }
.meter i.on { background: #4dff7c; box-shadow: 0 0 .8rem #4dff7c; }
.meter i.on:nth-child(n+11) { background: #ffd84d; box-shadow: 0 0 .8rem #ffd84d; }
.meter i.on:nth-child(n+14) { background: #ff4d4d; box-shadow: 0 0 .8rem #ff4d4d; }

.pads { display: grid; grid-template-columns: repeat(6, 1fr); gap: 1.8rem; padding: 2.2rem 1.8rem 1.8rem;
  border-radius: 2rem; background: #111317;
  box-shadow: inset 0 .5rem 1.5rem rgba(0,0,0,.9), 0 .12rem 0 rgba(255,255,255,.12); }
.well { aspect-ratio: 1.1; border-radius: 2rem; padding: .7rem; background: #08090b;
  box-shadow: inset 0 .6rem 1rem rgba(0,0,0,.95), 0 .1rem 0 rgba(255,255,255,.08); transition: box-shadow .25s; }
.well.lit { box-shadow: inset 0 .6rem 1rem rgba(0,0,0,.6), 0 0 2.8rem .35rem var(--glow), 0 .1rem 0 rgba(255,255,255,.08); }
.pad { width: 100%; height: 100%; border: 0; padding: 0; margin: 0; cursor: pointer; position: relative; outline: none;
  border-radius: 1.6rem; color: var(--t); font: inherit;
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: .3rem;
  background: radial-gradient(120% 70% at 50% 0%, rgba(255,255,255,.62), rgba(255,255,255,0) 60%),
              linear-gradient(180deg, var(--c), var(--d));
  box-shadow: 0 1rem 0 var(--e), 0 1.4rem 1.2rem rgba(0,0,0,.55),
              inset 0 .25rem 0 rgba(255,255,255,.5), inset 0 -.6rem 1rem rgba(0,0,0,.18);
  transform: translateY(-1rem); transition: transform .05s ease-out, box-shadow .05s ease-out, filter .2s; }
.pad.down { transform: translateY(-.15rem);
  box-shadow: 0 .15rem 0 var(--e), 0 .3rem .5rem rgba(0,0,0,.6),
              inset 0 .25rem 0 rgba(255,255,255,.35), inset 0 .6rem 1.2rem rgba(0,0,0,.25); }
.well.lit .pad { filter: brightness(1.2) saturate(1.15); }
.emo { font-size: 5.4rem; line-height: 1; font-family: "Segoe UI Emoji", "Apple Color Emoji", sans-serif;
  filter: drop-shadow(0 .3rem .25rem rgba(0,0,0,.35)); transition: transform .12s; }
.well.lit .emo { transform: scale(1.12) rotate(-4deg); }
.lbl { font-size: 1.45rem; font-weight: 900; letter-spacing: .06em; text-transform: uppercase;
  text-shadow: 0 .12rem .1rem rgba(0,0,0,.3); }
.key { position: absolute; top: .8rem; left: .9rem; min-width: 2.3rem; padding: .15rem .45rem; border-radius: .5rem;
  font-size: 1.2rem; font-weight: 800; color: rgba(0,0,0,.55); background: rgba(255,255,255,.38);
  box-shadow: inset 0 -.12rem 0 rgba(0,0,0,.15); text-align: center; }

.bottom { display: flex; align-items: center; gap: 3rem; margin-top: 2.6rem; }
.collar { width: 11rem; height: 11rem; border-radius: 50%; display: grid; place-items: center; flex: none;
  background: conic-gradient(from 200deg, #9aa0a8, #eceef1, #7b818a, #cdd1d6, #5d626a, #9aa0a8);
  box-shadow: 0 .4rem .8rem rgba(0,0,0,.7), inset 0 0 0 .3rem rgba(0,0,0,.25); }
.stop { width: 8.4rem; height: 8.4rem; border-radius: 50%; border: 0; cursor: pointer; outline: none; color: #fff;
  font: 900 1.6rem "Segoe UI", sans-serif; letter-spacing: .1em; text-shadow: 0 .15rem 0 rgba(0,0,0,.35);
  background: radial-gradient(circle at 42% 30%, #ffa0a0 0, #ff2b2b 35%, #c40f0f 70%, #7d0606 100%);
  box-shadow: 0 .9rem 0 #5a0505, 0 1.2rem 1rem rgba(0,0,0,.6), inset 0 .3rem 0 rgba(255,255,255,.35);
  transform: translateY(-.9rem); transition: transform .05s, box-shadow .05s; }
.stop.down { transform: translateY(-.1rem);
  box-shadow: 0 .1rem 0 #5a0505, 0 .3rem .5rem rgba(0,0,0,.6), inset 0 .5rem 1rem rgba(0,0,0,.3); }
.stop span { display: block; font-size: 2.8rem; line-height: 1.1; font-family: "Segoe UI Emoji", sans-serif; }
.fader { flex: 1; }
.fader .lab { display: flex; justify-content: space-between; color: #a3aab4; font-weight: 800; font-size: 1.3rem;
  letter-spacing: .35em; margin-bottom: .4rem; }
.fader .lab output { font-family: Consolas, monospace; letter-spacing: .05em; color: #62ff8f; }
input[type=range] { -webkit-appearance: none; appearance: none; width: 100%; height: 5.4rem; background: transparent; margin: 0; cursor: pointer; }
input[type=range]::-webkit-slider-runnable-track { height: 1.3rem; border-radius: .65rem; background: #08090b;
  box-shadow: inset 0 .25rem .5rem rgba(0,0,0,.95), 0 .1rem 0 rgba(255,255,255,.12); }
input[type=range]::-webkit-slider-thumb { -webkit-appearance: none; width: 3.6rem; height: 5.4rem; margin-top: -2.05rem;
  border-radius: .7rem;
  background: linear-gradient(180deg, transparent 47%, #26292e 47% 53%, transparent 53%),
              linear-gradient(90deg, #5f646c, #eef0f3 45%, #a7adb5 55%, #4c5057);
  box-shadow: 0 .4rem .7rem rgba(0,0,0,.75), inset 0 .1rem 0 rgba(255,255,255,.6); }
.ticks { display: flex; justify-content: space-between; padding: 0 1.4rem; color: #6c727c;
  font: 700 1.1rem Consolas, monospace; }
.grille { width: 17rem; height: 9.5rem; border-radius: 1.4rem; flex: none;
  background: radial-gradient(circle, #050506 40%, transparent 46%) 0 0 / 1.2rem 1.2rem, linear-gradient(#2c2f36, #24272d);
  box-shadow: inset 0 .3rem .6rem rgba(0,0,0,.8), 0 .1rem 0 rgba(255,255,255,.1); }
</style></head>
<body>
<div class="device">
  <i class="screw s1"></i><i class="screw s2"></i><i class="screw s3"></i><i class="screw s4"></i>
  <div class="top">
    <div class="brand">SUPER<br><b>SOUND PAD</b><small>MAKE SOME NOISE</small></div>
    <div class="lcd"><div id="now">PRESS A BUTTON &#9733;</div><div class="right"><div id="spk"></div><div class="meter" id="meter"></div></div></div>
  </div>
  <div class="pads" id="pads"></div>
  <div class="bottom">
    <div class="collar"><button class="stop" id="stop"><span>&#9995;</span>STOP</button></div>
    <div class="fader">
      <div class="lab"><span>VOLUME</span><output id="volout">100%</output></div>
      <input id="vol" type="range" min="0" max="150" value="100">
      <div class="ticks"><span>0</span><span>50</span><span>100</span><span>150</span></div>
    </div>
    <div class="grille"></div>
  </div>
</div>
<script>
const SOUNDS = __CONFIG__, TOKEN = "__TOKEN__";
const post = (path) => fetch(`${path}?t=${TOKEN}`, { method: "POST" }).catch(() => {});
const now = document.getElementById("now"), pads = document.getElementById("pads");
let idle;
function show(text) {
  now.textContent = text;
  clearTimeout(idle);
  idle = setTimeout(() => (now.textContent = "PRESS A BUTTON ★"), 2500);
}

const lit = {}, byKey = {};
SOUNDS.forEach((s, i) => {
  const well = document.createElement("div");
  well.className = "well";
  well.style.setProperty("--glow", s.c);
  const b = document.createElement("button");
  b.className = "pad";
  b.style.cssText = `--c:${s.c};--d:${s.d};--e:${s.e};--t:${s.t}`;
  b.innerHTML = `<span class="key">${s.key.toUpperCase()}</span><span class="emo">${s.emoji}</span><span class="lbl">${s.name}</span>`;
  b.addEventListener("pointerdown", (e) => { e.preventDefault(); b.classList.add("down"); fire(i); });
  well.append(b);
  pads.append(well);
  Object.assign(s, { well, b });
  byKey[s.key] = i;
});

function fire(i) {
  const s = SOUNDS[i];
  post(`/play/${i}`);
  s.well.classList.add("lit");
  clearTimeout(lit[i]);
  lit[i] = setTimeout(() => s.well.classList.remove("lit"), s.dur * 1000);
  show(`${s.emoji} ${s.name}`);
}
function stopAll() {
  post("/stop");
  SOUNDS.forEach((s, i) => { clearTimeout(lit[i]); s.well.classList.remove("lit"); });
  show("✋ STOP");
}
const stop = document.getElementById("stop");
stop.addEventListener("pointerdown", (e) => { e.preventDefault(); stop.classList.add("down"); stopAll(); });
const release = () => document.querySelectorAll(".down").forEach((el) => el.classList.remove("down"));
addEventListener("pointerup", release);
addEventListener("pointercancel", release);
addEventListener("blur", release);
addEventListener("contextmenu", (e) => e.preventDefault());   // no long-press menu under small fingers

addEventListener("keydown", (e) => {
  if (e.repeat) return;
  const k = e.key.toLowerCase();
  if (k === " " || k === "escape") { e.preventDefault(); stop.classList.add("down"); stopAll(); return; }
  if (k in byKey) { SOUNDS[byKey[k]].b.classList.add("down"); fire(byKey[k]); }
});
addEventListener("keyup", (e) => {
  const k = e.key.toLowerCase();
  if (k in byKey) SOUNDS[byKey[k]].b.classList.remove("down");
  if (k === " " || k === "escape") stop.classList.remove("down");
});

const vol = document.getElementById("vol"), volout = document.getElementById("volout");
vol.addEventListener("input", () => {
  volout.textContent = `${vol.value}%`;
  post(`/volume/${vol.value}`);
  show(`VOLUME ${vol.value}%`);
});

const meter = document.getElementById("meter"), spk = document.getElementById("spk");
const segs = Array.from({ length: 16 }, () => meter.appendChild(document.createElement("i")));
let shown = 0, synced = false;
async function poll() {
  try {
    const { level, speaker, volume } = await (await fetch("/level")).json();
    if (!synced) { vol.value = volume; volout.textContent = `${volume}%`; synced = true; }   // a reload keeps the app's volume
    spk.textContent = "\u{1F50A} " + ((speaker.match(/\(([^)]+)\)/) || [, speaker])[1]).replace(/^\d+- /, "");
    shown = Math.max(Math.round(Math.min(1, Math.sqrt(level) * 1.6) * segs.length), shown - 1);
    segs.forEach((g, k) => g.classList.toggle("on", k < shown));
  } catch (e) {}
  setTimeout(poll, 80);
}
poll();
addEventListener("pagehide", () => navigator.sendBeacon(`/bye?t=${TOKEN}`));
</script>
</body></html>
"""


if __name__ == "__main__":
    main()
