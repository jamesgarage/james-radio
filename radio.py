# /// script
# requires-python = ">=3.10"
# dependencies = ["sounddevice", "numpy"]
# ///
"""James' Radio: 24 synthesized sounds on glowing toy-style pads, in an app window.

    uv run radio.py                               # Windows' default output, followed when it changes
    uv run radio.py --out "Speakers (USB"
    uv run radio.py --share                       # phones and tablets on the same Wi-Fi can play it too

Keys 123456 / QWERTY / ASDFGH / ZXCVBN play the pads (the grid's shape on the keyboard); Space stops all.
The page is served on 127.0.0.1 only, unless --share asks for the home network too; closing the window ends
the program.
"""
import argparse
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import numpy as np
import sounddevice as sd

import plugins
from mic_echo import api_index, find   # the microphone lookup is mic_echo's, not a second copy

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


def level(samples, most=60):
    """Plugin audio: never louder than the pads; quiet recordings are raised at most 4x. None if silent."""
    s = np.nan_to_num(np.asarray(samples, dtype=np.float32).ravel()[:SR * most], nan=0.0, posinf=0.0, neginf=0.0)
    rms = float(np.sqrt(np.mean(s ** 2))) if len(s) else 0.0
    return limit(s * min(TARGET_RMS / rms, 4.0)).astype(np.float32) if rms > 0 else None


def soft_tone(freq, seconds):
    """A plugin's sine: below the pads' loudness, click-free at both ends. None if too short to hear."""
    if seconds < 0.01:
        return None
    x, edge = t(seconds), min(0.01, seconds / 4)
    return (0.3 * np.sin(2 * np.pi * freq * x) * fade(len(x), edge, edge)).astype(np.float32)


def plugin_radio(mixer, sounds):
    """The plugin API object (plugins/__init__.py), wired to the real mixer and microphone."""
    by_name = {name: snd for name, _, snd in sounds}

    def play(sound):
        snd = by_name[sound] if isinstance(sound, str) else level(sound)
        if snd is not None:
            mixer.play(snd)

    def tone(freq, seconds):
        snd = soft_tone(freq, seconds)
        if snd is not None:
            mixer.play(snd)

    return plugins.Radio(by_name, play, tone, mixer.record)


def short_name(name, names=()):
    """A speaker's name for the little display: Windows' name for it without the bracketed part, at most 22
    characters. When another speaker in `names` has the same first part, one telling word from the brackets stays:
    "Airpod Max (Sam's AirPods Max)" -> "Airpod Max"; "Speakers (SoundWire Speakers)" -> "Speakers (SoundWire)"."""
    base, _, detail = name.partition(" (")
    base = base.strip() or name.strip()
    if detail and sum(n.partition(" (")[0].strip() == base for n in names) > 1:
        words = [w for w in re.sub(r"^\d+- |\((?:R|TM)\)|\)$", "", detail.strip()).split()
                 if w.lower() not in base.lower().split()]
        if words:
            base = f"{base} ({words[0]})"
    return base if len(base) <= 22 else base[:21].rstrip() + "…"


def _com(obj, slot, *argtypes):
    """Method number `slot` (vtable order) of the COM object `obj`, called with the remaining arguments."""
    import ctypes
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[slot])
    return lambda *args: fn(obj, *args)


def windows_default_speaker():
    """An id for Windows' default speaker that changes when someone picks another one; None if Windows won't say.
    Asked of Windows' audio service (Core Audio) directly: PortAudio only learns of a new default by re-scanning,
    and a re-scan closes the stream that is playing."""
    if os.name != "nt":
        return None
    import ctypes
    ole32 = ctypes.windll.ole32
    started = ole32.CoInitializeEx(None, 0)   # S_OK / S_FALSE: ours to undo; RPC_E_CHANGED_MODE: COM is already up
    try:
        clsid, iid = (ctypes.c_byte * 16)(), (ctypes.c_byte * 16)()
        ole32.CLSIDFromString("{BCDE0395-E52F-467C-8E3D-C4579291692E}", clsid)   # MMDeviceEnumerator
        ole32.CLSIDFromString("{A95664D2-9614-4F35-A746-DE8DB63617E6}", iid)     # IMMDeviceEnumerator
        enum, dev, text = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        if ole32.CoCreateInstance(clsid, None, 1, iid, ctypes.byref(enum)) < 0 or not enum:
            return None
        try:   # GetDefaultAudioEndpoint(eRender, eMultimedia): the role PortAudio's WASAPI default uses
            if _com(enum, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(0, 1, ctypes.byref(dev)) < 0:
                return None
            try:
                if _com(dev, 5, ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(text)) < 0:   # IMMDevice::GetId
                    return None
                try:
                    return ctypes.wstring_at(text.value)
                finally:
                    ole32.CoTaskMemFree(text)
            finally:
                _com(dev, 2)()     # Release
        finally:
            _com(enum, 2)()
    except OSError:
        return None
    finally:
        if started >= 0:
            ole32.CoUninitialize()


class Mixer:
    """One output stream; overlapping sounds are summed in the callback.

    Which speaker: the one named with --out, else Windows' default, followed when it changes (between sounds).
    A named speaker that is away is waited for on Windows' default."""

    def __init__(self, out_part, default_id=windows_default_speaker):
        self.voices, self.lock, self.volume, self.stream = [], threading.Lock(), 1.0, None
        self.level, self.open_lock, self.device, self.fallback = 0.0, threading.Lock(), "", False
        self.closed = False
        self.pin, self.pin_exact = out_part or None, False   # stay on this speaker (part of its name); None: follow
        self.default_id, self.seen_default = default_id, None   # Windows' default speaker when the stream opened
        self.outputs = []   # (index, name) of every WASAPI output at the last scan

    def label(self):
        """The speaker's short name, for the display."""
        return short_name(self.device, [n for _, n in self.outputs])

    def _pinned(self, name):
        if not self.pin:
            return False
        return name == self.pin if self.pin_exact else self.pin.lower() in name.lower()

    def _open(self):
        """(Re)open on the speaker we should be on (see the class note). Re-scans the devices first."""
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        self.device = "No speaker"   # what the page shows until a stream opens
        self.seen_default = self.default_id()   # before the scan: a change after this is caught on the next look
        sd._terminate()              # re-scan devices: PortAudio only knows the list from when it started
        sd._initialize()
        api = next(i for i, a in enumerate(sd.query_hostapis()) if a["name"] == "Windows WASAPI")
        self.outputs = [(i, d["name"]) for i, d in enumerate(sd.query_devices())
                        if d["hostapi"] == api and d["max_output_channels"] > 0]
        wanted = [i for i, name in self.outputs if self._pinned(name)]
        dev = wanted[0] if wanted else sd.query_hostapis(api)["default_output_device"]
        if dev < 0:
            raise LookupError("Windows has no output device")
        self.fallback = bool(self.pin) and not wanted
        ch = min(2, sd.query_devices(dev)["max_output_channels"])
        self.stream = sd.OutputStream(device=dev, samplerate=SR, channels=ch, dtype="float32", latency="low",
                                      callback=self._mix, extra_settings=sd.WasapiSettings(auto_convert=True))
        self.stream.start()
        self.device = sd.query_devices(dev)["name"]
        return self.device

    def watch(self):
        """Every 5 s: reopen a dead stream. Between sounds, also move to Windows' new default speaker, or back to
        a named speaker that has returned."""
        while True:
            time.sleep(5)
            if not self._tick():
                return

    def _tick(self):
        """One look; False once the mixer is closed."""
        with self.open_lock:
            if self.closed:
                return False
            dead = self.stream is None or not self.stream.active
            if dead or (not self.voices and (self.fallback or self._default_moved())):   # never cut a sound short
                try:
                    self._open()
                except Exception:
                    pass
            return True

    def _default_moved(self):
        """Windows' default speaker is not the one it was when the stream opened (only matters while following)."""
        if self.pin:
            return False
        now = self.default_id()
        return now is not None and now != self.seen_default

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
        if self.stream is None or not self.stream.active:   # a live stream needs no lock (record() may hold it)
            with self.open_lock:   # button presses arrive on several server threads at once
                if self.stream is None or not self.stream.active:
                    try:
                        self._open()
                    except Exception:
                        return     # no speaker: drop the sound rather than queue a pile-up that all plays later
        with self.lock:
            self.voices.append((snd, 0))

    def record(self, seconds):
        """Mono float32 samples from the default microphone; None when there is none. record(0) only checks."""
        with self.open_lock:   # held throughout: a device re-scan in _open() would pull the mic from under the read
            try:
                mic = find("", "input", api_index("Windows WASAPI"))
            except LookupError:
                try:
                    self._open()   # re-scan once: a mic plugged in after start is unknown until then
                    mic = find("", "input", api_index("Windows WASAPI"))
                except Exception:
                    return None
            if seconds <= 0:
                return np.zeros(0, np.float32)
            try:
                with sd.InputStream(device=mic, samplerate=SR, channels=1, dtype="float32",
                                    extra_settings=sd.WasapiSettings(auto_convert=True)) as stream:
                    data, _ = stream.read(int(SR * seconds))
            except sd.PortAudioError:
                return None
            return data[:, 0].copy()

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
RAINBOW = [("#ff6161", "#d81e1e", "#8e0f0f", "#fff"), ("#ffad45", "#f07800", "#9a4b00", "#3d1d00"),
           ("#ffe65e", "#f2c200", "#9c7a00", "#4a3500"), ("#66e680", "#12822d", "#0b5a1d", "#fff"),
           ("#66b8ff", "#1268cc", "#0b4185", "#fff"), ("#c896ff", "#8a3ff0", "#561f9e", "#fff")]


SHARE_PORT = 8481   # fixed, so a phone's bookmark keeps working from one run to the next
HOME_NETS = [ipaddress.ip_network(n) for n in ("192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12")]   # best first


def lan_address():
    """This PC's address on the home network, or None. 192.168.x wins over 10.x, and 172.16-31.x comes last
    because that is where VPNs and virtual adapters usually sit."""
    try:
        found = sorted({ipaddress.ip_address(a) for a in socket.gethostbyname_ex(socket.gethostname())[2]})
    except OSError:
        return None
    return next((str(a) for net in HOME_NETS for a in found if a in net), None)


def share_address(asked):
    """The address --share listens on: the one asked for if it is a home-network or loopback IPv4 address,
    else this PC's own; raises ValueError with a sentence to show."""
    if asked == "auto":
        found = lan_address()
        if not found:
            raise ValueError("no home-network address found on this PC. Is it on Wi-Fi? "
                             "If it is, name the address: --share 192.168.0.42")
        return found
    try:
        ip = ipaddress.IPv4Address(asked)
    except ValueError:
        raise ValueError(f"--share wants an address like 192.168.0.42, not {asked!r}") from None
    if not (ip.is_loopback or any(ip in net for net in HOME_NETS)):
        raise ValueError(f"{asked} is not a home-network address (192.168.x.x, 10.x.x.x or 172.16-31.x.x)")
    return asked


def make_server(mixer, sounds, radio, address="127.0.0.1", port=0):
    token = secrets.token_urlsafe(16)   # only this page may press the buttons
    cfg = [{"name": n, "emoji": e, "key": k, "dur": round(len(s) / SR, 2),
            "c": RAINBOW[i % 6][0], "d": RAINBOW[i % 6][1], "e": RAINBOW[i % 6][2], "t": RAINBOW[i % 6][3]}
           for i, ((n, e, s), k) in enumerate(zip(sounds, KEYS))]
    extras = [{"label": b.label, "c": b.color} for b in radio._buttons]
    as_js = lambda v: json.dumps(v).replace("<", "\\u003c")   # a label can never close the <script>
    page = (PAGE.replace("__CONFIG__", as_js(cfg)).replace("__EXTRAS__", as_js(extras))
            .replace("__TOKEN__", token).encode("utf-8"))
    state = {"last_poll": None, "pc_poll": None, "bye_at": None}
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

        def _from_pc(self):
            # the PC's own window; with --share the PC reaches its home-network address from that same address
            return self.client_address[0] in ("127.0.0.1", address)

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
                if self._from_pc():
                    state["pc_poll"] = state["last_poll"]
                self._send(200, json.dumps({"level": mixer.level, "speaker": mixer.label(),
                                            "volume": round(mixer.volume * 100),
                                            "msg": list(radio._message)}).encode(), "application/json")
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
                elif what == "button" and 0 <= int(arg) < len(radio._buttons):
                    radio._press(int(arg))
                elif what == "stop":
                    mixer.stop_all()
                elif what == "volume":
                    mixer.volume = max(0.0, min(float(arg), 150.0)) / 100
                elif what == "bye":
                    if self._from_pc():                 # a phone closing its tab does not stop the radio
                        state["bye_at"] = time.monotonic()
                else:
                    return self._send(404)
            except ValueError:
                return self._send(400)
            self._send(204)

    try:
        server = ThreadingHTTPServer((address, port), Handler)
    except OSError:
        if not port:
            raise
        server = ThreadingHTTPServer((address, 0), Handler)   # the usual port is taken: any free one
    port = server.server_address[1]
    hosts.update({f"127.0.0.1:{port}", f"localhost:{port}", f"{address}:{port}"})
    return server, f"http://{address}:{port}/", state


def open_app_window(url):
    """Edge's app mode = a plain window with no tabs or address bar; any browser if Edge is missing."""
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        edge = os.path.join(base or "", "Microsoft", "Edge", "Application", "msedge.exe")
        if base and os.path.exists(edge):
            subprocess.Popen([edge, f"--app={url}", "--window-size=1400,920"])
            return
    webbrowser.open(url)


def main():
    p = argparse.ArgumentParser(description="James' Radio: 24 sounds on glowing toy-style pads.")
    p.add_argument("--out", default="", help="part of the output device's name (default: Windows default)")
    p.add_argument("--no-window", action="store_true", help="serve the page but don't open a window")
    p.add_argument("--share", nargs="?", const="auto", metavar="ADDRESS",
                   help="also serve the radio to phones and tablets on the home network (this PC's address, "
                        "or the one given); without it only this PC can open it")
    a = p.parse_args()
    address = "127.0.0.1"
    if a.share:
        try:
            address = share_address(a.share)
        except ValueError as e:
            sys.exit(f"James' Radio: {e}")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")   # redirected output is cp1252; a device name may not fit it

    mixer = Mixer(a.out)
    try:
        device = mixer._open()
    except Exception as e:   # no speaker yet: the page still opens and watch() keeps looking every 5 s
        device = f"no speaker yet ({e})"
    sounds = render()
    radio = plugin_radio(mixer, sounds)
    loaded = plugins.load_all(radio)
    try:
        server, url, state = make_server(mixer, sounds, radio, address, SHARE_PORT if a.share else 0)
    except OSError as e:
        mixer.close()
        sys.exit(f"James' Radio: cannot listen on {address}: {e.strerror or e}")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=mixer.watch, daemon=True).start()
    print(f"James' Radio on {url} -> {device}; plugins: {', '.join(loaded) or 'none'}", flush=True)
    if a.share:
        print(f"On a phone or tablet on the same Wi-Fi, open {url}\n"
              "The sound plays on this PC. Closing the radio's window here stops it for everyone.", flush=True)
    if not a.no_window:
        open_app_window(url)

    started = time.monotonic()
    try:
        while True:
            time.sleep(1)
            now, last, pc, bye = time.monotonic(), state["last_poll"], state["pc_poll"], state["bye_at"]
            if bye and (pc is None or pc < bye) and now - bye > 3:
                break                               # the PC's window closed (a reload polls again within 3 s)
            if last and now - last > 150:
                break                               # every page gone without saying bye (minimized pages poll slowly)
            if last is None and now - started > (300 if a.share else 90):
                break                               # page never opened (a phone needs longer to type the address)
    except KeyboardInterrupt:
        pass                                        # Ctrl+C in the console window: stop the same clean way
    server.shutdown()
    server.server_close()
    mixer.close()
    print("Window closed; James' Radio stopped.", flush=True)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>James' Radio</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1d2027">
<meta name="color-scheme" content="dark">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="James' Radio">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<style>
/* Two layouts, one page.
   Console: a laptop, or a tablet on its side. The whole toy fits the window; 1rem = one toy unit.
   Flow: phones, portrait tablets, short windows. Real-size type, 3 / 4 / 6 columns, the page scrolls down
   (never sideways), and STOP + volume stay pinned at the bottom. */
:root { color-scheme: dark; --cols: 3; }
@media (min-width: 600px) { :root { --cols: 4; } }
@media (min-width: 900px), (orientation: landscape) and (min-width: 640px) { :root { --cols: 6; } }
html { font-size: min(1vw, calc(100vh / 90)); -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
@supports (height: 100dvh) { html { font-size: min(1vw, calc(100dvh / 90)); } }
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
body { margin: 0; height: 100vh; height: 100dvh; display: grid; place-items: center; overflow: hidden;
  user-select: none; -webkit-user-select: none; -webkit-touch-callout: none; touch-action: manipulation;
  font-family: "Segoe UI", system-ui, -apple-system, sans-serif; color: #eef0f3;
  background: #15171c radial-gradient(ellipse at 50% 35%, #4b5160 0%, #1d2027 55%, #0d0e11 100%); }
.device { position: relative; width: 96rem; padding: 3rem 3.2rem 3.2rem; border-radius: 3.2rem;
  display: grid; grid-template-columns: 1fr auto auto; align-items: center;
  grid-template-areas: "top top top" "pads pads pads" "bottom extras grille";
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

.top { grid-area: top; display: flex; align-items: center; gap: 2.4rem; margin-bottom: 2.4rem; min-width: 0; }
.brand { margin: 0; color: #eef0f3; font-weight: 900; font-size: 3rem; letter-spacing: .04em; line-height: .95;
  text-shadow: 0 .15rem 0 #000, 0 -.08rem 0 rgba(255,255,255,.2); }
.brand b { background: linear-gradient(90deg, #ff6161, #ffad45, #ffe65e, #66e680, #66b8ff, #c896ff);
  -webkit-background-clip: text; background-clip: text; color: transparent; text-shadow: none;
  filter: drop-shadow(0 .15rem 0 #000); }
.brand small { display: block; font-size: max(12px, 1.2rem); font-weight: 700; color: #a3aab4; letter-spacing: .3em; margin-top: .7rem; }
.lcd { flex: 1; min-width: 0; height: 7.4rem; border-radius: 1.1rem; padding: 0 2.2rem; position: relative; overflow: hidden;
  display: flex; align-items: center; justify-content: space-between; gap: 2rem;
  background: linear-gradient(180deg, #0a1d10, #10301b); border: .5rem solid #0b0c0e;
  box-shadow: inset 0 .5rem 1.2rem rgba(0,0,0,.9), 0 .12rem 0 rgba(255,255,255,.14); }
.lcd::after { content: ""; position: absolute; inset: 0; pointer-events: none;
  background: repeating-linear-gradient(0deg, rgba(0,0,0,.28) 0 2px, transparent 2px 4px),
              linear-gradient(160deg, rgba(255,255,255,.08), transparent 40%); }
#now { min-width: 0; font: 700 2.9rem Consolas, "Courier New", monospace; color: #62ff8f; letter-spacing: .12em;
  text-transform: uppercase; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  text-shadow: 0 0 .9rem rgba(98,255,143,.85); }
.right { display: flex; flex-direction: column; align-items: flex-end; gap: .5rem; flex: 0 1 auto; min-width: 0; max-width: 100%; }
#spk { max-width: 100%; font: 700 max(12px, 1.2rem) Consolas, monospace; color: #4fd179; letter-spacing: .1em;
  text-transform: uppercase; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.meter { display: flex; gap: .45rem; flex: none; }
.meter i { width: .95rem; height: 3.4rem; border-radius: .2rem; background: #163020; }
.meter i:nth-child(n+11) { background: #35300f; }
.meter i:nth-child(n+14) { background: #3a1414; }
.meter i.on { background: #4dff7c; box-shadow: 0 0 .8rem #4dff7c; }
.meter i.on:nth-child(n+11) { background: #ffd84d; box-shadow: 0 0 .8rem #ffd84d; }
.meter i.on:nth-child(n+14) { background: #ff4d4d; box-shadow: 0 0 .8rem #ff4d4d; }

.pads { grid-area: pads; display: grid; grid-template-columns: repeat(6, 1fr); gap: 1.8rem; padding: 2.2rem 1.8rem 1.8rem;
  border-radius: 2rem; background: #111317;
  box-shadow: inset 0 .5rem 1.5rem rgba(0,0,0,.9), 0 .12rem 0 rgba(255,255,255,.12); }
.well { container-type: inline-size; aspect-ratio: 1.1; min-width: 0; border-radius: 2rem; padding: .7rem; background: #08090b;
  box-shadow: inset 0 .6rem 1rem rgba(0,0,0,.95), 0 .1rem 0 rgba(255,255,255,.08); transition: box-shadow .25s; }
.well.lit { box-shadow: inset 0 .6rem 1rem rgba(0,0,0,.6), 0 0 2.8rem .35rem var(--glow), 0 .1rem 0 rgba(255,255,255,.08); }
/* Pressed state: instant on pointer-down, a short spring back up on release. */
.pad { width: 100%; height: 100%; border: 0; padding: 0 .4rem; margin: 0; cursor: pointer; position: relative;
  border-radius: 1.6rem; color: var(--t); font: inherit;
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 3cqi;
  background: radial-gradient(120% 70% at 50% 0%, rgba(255,255,255,.62), rgba(255,255,255,0) 60%),
              linear-gradient(180deg, var(--c), var(--d));
  box-shadow: 0 1rem 0 var(--e), 0 1.4rem 1.2rem rgba(0,0,0,.55),
              inset 0 .25rem 0 rgba(255,255,255,.5), inset 0 -.6rem 1rem rgba(0,0,0,.18);
  transform: translateY(-1rem);
  transition: transform .38s cubic-bezier(.3, 1.7, .45, 1), box-shadow .38s cubic-bezier(.3, 1.7, .45, 1), filter .2s ease; }
.pad.down { transform: translateY(-.15rem);
  transition: transform .05s ease-out, box-shadow .05s ease-out, filter .2s ease;
  box-shadow: 0 .15rem 0 var(--e), 0 .3rem .5rem rgba(0,0,0,.6),
              inset 0 .25rem 0 rgba(255,255,255,.35), inset 0 .6rem 1.2rem rgba(0,0,0,.25); }
.pad:focus-visible { outline: .35rem solid #fff; outline-offset: .3rem; }
.well.lit .pad { filter: brightness(1.2) saturate(1.15); }
.emo { font-size: 38cqi; line-height: 1; font-family: "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", sans-serif;
  filter: drop-shadow(0 .3rem .25rem rgba(0,0,0,.35)); transition: transform .12s; }
.well.lit .emo { transform: scale(1.12) rotate(-4deg); }
.lbl { max-width: 100%; font-size: max(12px, 11cqi); font-weight: 900; line-height: 1.1; letter-spacing: .04em;
  text-transform: uppercase; text-wrap: balance; text-shadow: 0 .12rem .1rem rgba(0,0,0,.3); }
.key { display: none; position: absolute; top: 6cqi; left: 6cqi; min-width: 2.3rem; padding: .1rem .45rem; border-radius: .5rem;
  font-size: max(12px, 9.5cqi); font-weight: 800; color: rgba(0,0,0,.62); background: rgba(255,255,255,.42);
  box-shadow: inset 0 -.12rem 0 rgba(0,0,0,.15); text-align: center; }

.bottom { grid-area: bottom; display: flex; align-items: center; gap: 3rem; margin-top: 2.6rem; min-width: 0; }
.collar { width: 11rem; height: 11rem; border-radius: 50%; display: grid; place-items: center; flex: none;
  background: conic-gradient(from 200deg, #9aa0a8, #eceef1, #7b818a, #cdd1d6, #5d626a, #9aa0a8);
  box-shadow: 0 .4rem .8rem rgba(0,0,0,.7), inset 0 0 0 .3rem rgba(0,0,0,.25); }
.stop { width: 8.4rem; height: 8.4rem; border-radius: 50%; border: 0; padding: 0; cursor: pointer; color: #fff;
  font: 900 max(12px, 1.6rem) "Segoe UI", system-ui, sans-serif; letter-spacing: .1em; text-shadow: 0 .15rem 0 rgba(0,0,0,.35);
  background: radial-gradient(circle at 42% 30%, #ffa0a0 0, #ff2b2b 35%, #c40f0f 70%, #7d0606 100%);
  box-shadow: 0 .9rem 0 #5a0505, 0 1.2rem 1rem rgba(0,0,0,.6), inset 0 .3rem 0 rgba(255,255,255,.35);
  transform: translateY(-.9rem);
  transition: transform .38s cubic-bezier(.3, 1.7, .45, 1), box-shadow .38s cubic-bezier(.3, 1.7, .45, 1); }
.stop.down { transform: translateY(-.1rem); transition: transform .05s ease-out, box-shadow .05s ease-out;
  box-shadow: 0 .1rem 0 #5a0505, 0 .3rem .5rem rgba(0,0,0,.6), inset 0 .5rem 1rem rgba(0,0,0,.3); }
.stop:focus-visible { outline: .35rem solid #fff; outline-offset: .4rem; }
.stop span { display: block; font-size: 2.8rem; line-height: 1.1; font-family: "Segoe UI Emoji", "Apple Color Emoji", sans-serif; }
.fader { flex: 1; min-width: 0; }
.fader .lab { display: flex; justify-content: space-between; color: #a3aab4; font-weight: 800; font-size: max(12px, 1.3rem);
  letter-spacing: .35em; margin-bottom: .4rem; }
.fader .lab label { cursor: pointer; }
.fader .lab output { font-family: Consolas, monospace; letter-spacing: .05em; color: #62ff8f; }
input[type=range] { -webkit-appearance: none; appearance: none; display: block; width: 100%; height: max(44px, 5.4rem);
  background: transparent; margin: 0; cursor: pointer; }
input[type=range]:focus-visible { outline: .3rem solid #62ff8f; outline-offset: .2rem; border-radius: 1rem; }
input[type=range]::-webkit-slider-runnable-track { height: 1.3rem; border-radius: .65rem; background: #08090b;
  box-shadow: inset 0 .25rem .5rem rgba(0,0,0,.95), 0 .1rem 0 rgba(255,255,255,.12); }
input[type=range]::-webkit-slider-thumb { -webkit-appearance: none; width: 3.6rem; height: 5.4rem; margin-top: -2.05rem;
  border-radius: .7rem;
  background: linear-gradient(180deg, transparent 47%, #26292e 47% 53%, transparent 53%),
              linear-gradient(90deg, #5f646c, #eef0f3 45%, #a7adb5 55%, #4c5057);
  box-shadow: 0 .4rem .7rem rgba(0,0,0,.75), inset 0 .1rem 0 rgba(255,255,255,.6); }
input[type=range]::-moz-range-track { height: 1.3rem; border-radius: .65rem; background: #08090b; }
input[type=range]::-moz-range-thumb { width: 3.6rem; height: 5.4rem; border: 0; border-radius: .7rem;
  background: linear-gradient(90deg, #5f646c, #eef0f3 45%, #a7adb5 55%, #4c5057); }
.ticks { display: flex; justify-content: space-between; padding: 0 1.4rem; color: #9aa1ab;
  font: 700 max(12px, 1.1rem) Consolas, monospace; }
.extras { grid-area: extras; display: flex; gap: 1.6rem; margin: 2.6rem 0 0 3rem; }
.extras:empty { display: none; }
.extras .well { width: 12rem; height: 10.5rem; aspect-ratio: auto; }
.grille { grid-area: grille; width: 17rem; height: 9.5rem; margin: 2.6rem 0 0 3rem; border-radius: 1.4rem;
  background: radial-gradient(circle, #050506 40%, transparent 46%) 0 0 / 1.2rem 1.2rem, linear-gradient(#2c2f36, #24272d);
  box-shadow: inset 0 .3rem .6rem rgba(0,0,0,.8), 0 .1rem 0 rgba(255,255,255,.1); }

/* Flow layout: phones, portrait tablets and short windows. */
@media (max-width: 899.98px), (max-height: 599.98px) {
  html { font-size: clamp(10px, 2.8vw, 13px); }
  body { display: block; place-items: normal; height: auto; overflow: visible;
    padding: max(10px, env(safe-area-inset-top)) max(10px, env(safe-area-inset-right)) 0 max(10px, env(safe-area-inset-left)); }
  .device { display: flex; flex-direction: column; align-items: stretch; gap: 1.2rem; width: auto; max-width: 96rem; margin: 0 auto;
    padding: 1.4rem 1.2rem 0; border-radius: 2.4rem 2.4rem 0 0;
    box-shadow: 0 2rem 4rem rgba(0,0,0,.7), inset 0 .15rem 0 rgba(255,255,255,.25); }
  .screw, .grille { display: none; }
  .top { margin: 0; flex-wrap: wrap; gap: 1rem 1.6rem; }
  .brand { font-size: 2.6rem; }
  .brand br { display: none; }
  .brand small { letter-spacing: .26em; margin-top: .5rem; }
  .lcd { flex: 1 1 26rem; height: auto; min-height: 6.2rem; flex-wrap: wrap; gap: .3rem 1.4rem;
    padding: .8rem 1.4rem; border-width: .4rem; }
  #now { flex: 1 1 auto; font-size: 2.2rem; }
  .right { flex-direction: row; align-items: center; gap: 1rem; margin-left: auto; }
  .meter { gap: .3rem; }
  .meter i { width: .7rem; height: 2.4rem; }
  .pads, .extras { margin: 0; gap: 1rem; padding: 1.2rem; border-radius: 1.8rem; background: #111317;
    box-shadow: inset 0 .5rem 1.5rem rgba(0,0,0,.9), 0 .12rem 0 rgba(255,255,255,.12); }
  .pads { grid-template-columns: repeat(var(--cols), minmax(0, 1fr)); }
  .extras { display: grid; justify-content: center;   /* fixed-width tracks: a short last row sits centred */
    grid-template-columns: repeat(auto-fit, calc((100% - (var(--cols) - 1) * 1rem) / var(--cols))); }
  .extras .well { width: auto; height: auto; aspect-ratio: 1.1; }
  .well { border-radius: 1.6rem; padding: .5rem; }
  .pad { border-radius: 1.3rem; transform: translateY(-.7rem);
    box-shadow: 0 .7rem 0 var(--e), 0 1rem .9rem rgba(0,0,0,.55),
                inset 0 .25rem 0 rgba(255,255,255,.5), inset 0 -.6rem 1rem rgba(0,0,0,.18); }
  .pad.down { transform: translateY(-.1rem); }
  .bottom { position: sticky; bottom: 0; z-index: 2; margin: 0 -1.2rem; gap: 1.6rem;
    padding: 1rem 1.8rem calc(1rem + env(safe-area-inset-bottom));
    background: rgba(30, 33, 39, .84); -webkit-backdrop-filter: blur(18px) saturate(150%); backdrop-filter: blur(18px) saturate(150%);
    border-top: 1px solid rgba(255, 255, 255, .12); }
  .collar { width: 7.6rem; height: 7.6rem; }
  .stop { width: 6rem; height: 6rem; font-size: max(12px, 1.2rem); transform: translateY(-.55rem);
    box-shadow: 0 .55rem 0 #5a0505, 0 .8rem .8rem rgba(0,0,0,.6), inset 0 .25rem 0 rgba(255,255,255,.35); }
  .stop.down { transform: translateY(-.05rem); }
  .stop span { font-size: 2rem; }
  .fader .lab { letter-spacing: .25em; margin: 0; }
  input[type=range] { height: max(44px, 4.4rem); }
  input[type=range]::-webkit-slider-thumb { height: 4.4rem; margin-top: -1.55rem; }
  .ticks { padding: 0 1.2rem; }
}
/* A phone on its side: little height, so shorter pads and a slimmer header. */
@media (orientation: landscape) and (max-height: 500px) {
  html { font-size: clamp(9px, 2.6vh, 12px); }
  .well { aspect-ratio: 1.45; }
  .extras .well { aspect-ratio: 1.45; }
  .top { flex-wrap: nowrap; }
  .brand small { display: none; }
  .lcd { min-height: 5rem; }
}

/* Only where a mouse is: hover warmth and the keyboard hints. Nothing depends on hover. */
@media (hover: hover) and (pointer: fine) {
  .pad:hover, .stop:hover { filter: brightness(1.08); }
  .key { display: block; }
}
@media (prefers-reduced-motion: reduce) {
  .pad, .pad.down, .stop, .stop.down, .emo, .well { transition: none; }
  .well.lit .emo { transform: none; }
}
@media (prefers-reduced-transparency: reduce) {
  .bottom { background: #1e2127; -webkit-backdrop-filter: none; backdrop-filter: none; }
}
</style></head>
<body>
<main class="device">
  <i class="screw s1" aria-hidden="true"></i><i class="screw s2" aria-hidden="true"></i>
  <i class="screw s3" aria-hidden="true"></i><i class="screw s4" aria-hidden="true"></i>
  <header class="top">
    <h1 class="brand">JAMES' <br><b>RADIO</b><small>MAKE SOME NOISE</small></h1>
    <div class="lcd"><div id="now" role="status" aria-live="polite">PRESS A BUTTON &#9733;</div>
      <div class="right" aria-hidden="true"><div id="spk"></div><div class="meter" id="meter"></div></div></div>
  </header>
  <section class="pads" id="pads" aria-label="Sounds"></section>
  <section class="extras" id="extras" aria-label="More buttons"></section>
  <footer class="bottom">
    <div class="collar"><button class="stop" id="stop" type="button"><span aria-hidden="true">&#9995;</span>STOP</button></div>
    <div class="fader">
      <div class="lab"><label for="vol">VOLUME</label><output id="volout" for="vol">100%</output></div>
      <input id="vol" type="range" min="0" max="150" value="100">
      <div class="ticks" aria-hidden="true"><span>0</span><span>50</span><span>100</span><span>150</span></div>
    </div>
  </footer>
  <div class="grille" aria-hidden="true"></div>
</main>
<script>
const SOUNDS = __CONFIG__, EXTRAS = __EXTRAS__, TOKEN = "__TOKEN__";
const post = (path) => fetch(`${path}?t=${TOKEN}`, { method: "POST" }).catch(() => {});
const $ = (id) => document.getElementById(id);
const now = $("now");
let idle;
function show(text) {
  now.textContent = text;
  clearTimeout(idle);
  idle = setTimeout(() => (now.textContent = "PRESS A BUTTON ★"), 2500);
}
function span(cls, text) {
  const s = document.createElement("span");
  s.className = cls;
  s.textContent = text;   // never innerHTML: plugin labels are plain text
  return s;
}

// Feedback on pointer-down. A mouse plays at once; a finger plays when it lifts, so a finger that starts
// scrolling the page (the browser sends pointercancel) plays nothing. STOP acts on touch-down regardless.
function pressable(el, act, immediate = false) {
  let armed = null;
  el.addEventListener("pointerdown", (e) => {
    if (e.button > 0) return;
    e.preventDefault();
    el.classList.add("down");
    if (immediate || e.pointerType === "mouse") act();
    else armed = e.pointerId;
  });
  el.addEventListener("pointerup", (e) => {
    el.classList.remove("down");
    if (armed === e.pointerId) act();
    armed = null;
  });
  el.addEventListener("pointercancel", () => { el.classList.remove("down"); armed = null; });
  el.addEventListener("click", (e) => { if (e.detail === 0) act(); });   // Enter on a focused button
}

function makePad(parent, p, act) {
  const well = document.createElement("div");
  well.className = "well";
  well.style.setProperty("--glow", p.c);
  const b = document.createElement("button");
  b.className = "pad";
  b.type = "button";
  for (const [k, v] of Object.entries({ "--c": p.c, "--d": p.d, "--e": p.e, "--t": p.t })) b.style.setProperty(k, v);
  if (p.key) {
    b.append(span("key", p.key.toUpperCase()));
    b.setAttribute("aria-keyshortcuts", p.key.toUpperCase());
  }
  if (p.emoji) b.append(span("emo", p.emoji));
  b.append(span("lbl", p.name));
  b.querySelectorAll(".key, .emo").forEach((s) => s.setAttribute("aria-hidden", "true"));
  pressable(b, act);
  well.append(b);
  parent.append(well);
  return { well, b };
}

const lit = {}, byKey = {};
SOUNDS.forEach((s, i) => {
  Object.assign(s, makePad($("pads"), s, () => fire(i)));
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

// Plugin buttons (plugins/*.py) are pads too. A label that starts with an emoji uses it as the picture.
const PICTURE = /^(\p{Extended_Pictographic}(?:️|‍\p{Extended_Pictographic})*️?)\s*/u;
EXTRAS.forEach((x, i) => {
  const m = x.label.match(PICTURE);
  makePad($("extras"), {
    emoji: m ? m[1] : "", name: (m && x.label.slice(m[0].length)) || x.label, t: "#fff",
    c: x.c, d: `color-mix(in srgb, ${x.c} 76%, #000)`, e: `color-mix(in srgb, ${x.c} 45%, #000)`,
  }, () => { post(`/button/${i}`); show(x.label); });
});

function stopAll() {
  post("/stop");
  SOUNDS.forEach((s, i) => { clearTimeout(lit[i]); s.well.classList.remove("lit"); });
  show("✋ STOP");
}
const stop = $("stop");
pressable(stop, stopAll, true);
const release = () => document.querySelectorAll(".down").forEach((el) => el.classList.remove("down"));
addEventListener("pointerup", (e) => { if (e.pointerType === "mouse") release(); });   // let go somewhere else
addEventListener("blur", release);
addEventListener("contextmenu", (e) => e.preventDefault());   // no long-press menu under small fingers

addEventListener("keydown", (e) => {
  if (e.repeat || e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === " " || k === "escape") { e.preventDefault(); stop.classList.add("down"); stopAll(); return; }
  if (k in byKey && e.target !== vol) { SOUNDS[byKey[k]].b.classList.add("down"); fire(byKey[k]); }
});
addEventListener("keyup", (e) => {
  const k = e.key.toLowerCase();
  if (k in byKey) SOUNDS[byKey[k]].b.classList.remove("down");
  if (k === " " || k === "escape") stop.classList.remove("down");
});

const vol = $("vol"), volout = $("volout");
vol.addEventListener("input", () => {
  volout.textContent = `${vol.value}%`;
  post(`/volume/${vol.value}`);
  show(`VOLUME ${vol.value}%`);
});

const meter = $("meter"), spk = $("spk");
const segs = Array.from({ length: 16 }, () => meter.appendChild(document.createElement("i")));
let shown = 0, synced = false, seen = -1, speakerShown = "";
async function poll() {
  try {
    const { level, speaker, volume, msg } = await (await fetch("/level")).json();
    if (!synced) { vol.value = volume; volout.textContent = `${volume}%`; synced = true; }   // a reload keeps the app's volume
    if (seen >= 0 && msg[0] !== seen) show(msg[1]);   // a plugin button's reply, e.g. "No microphone"
    seen = msg[0];
    if (speaker !== speakerShown) { speakerShown = speaker; spk.textContent = "\u{1F50A} " + speaker; }   // already short
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
