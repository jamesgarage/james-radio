# /// script
# requires-python = ">=3.10"
# dependencies = ["sounddevice", "numpy"]
# ///
"""James' Radio: 24 synthesized sounds on glowing toy-style pads, a STATION page of radio drops and a PLAY page the
plugins fill, in an app window.

    uv run radio.py                               # Windows' default output, followed when it changes
    uv run radio.py --out "Speakers (USB"
    uv run radio.py --share                       # phones and tablets on the same Wi-Fi can play it too

Keys 123456 / QWERTY / ASDFGH / ZXCVBN play the pads (the grid's shape on the keyboard) of the page you see; a
sideways swipe, the page tabs or the arrow keys turn to the other page; Space stops all;
the PC's microphone is on the air (through the radio's speaker) while the radio runs, and M or the MIC pad mutes it.
The page is served on 127.0.0.1 only, unless --share asks for the home network too; closing the window ends
the program.
"""
import argparse
import base64
import hashlib
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
import wave
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import numpy as np
import sounddevice as sd

import plugins

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


def loud(s):
    """Peak to 1, then about TARGET_RMS: every pad gets the same loudness, not just the same peak."""
    s = s / np.max(np.abs(s))
    return limit(s * min(TARGET_RMS / np.sqrt(np.mean(s ** 2)), 5))   # sparse sounds (claps, bells, the blast) get pushed up


def render():
    return [(name, picture, loud(make()).astype(np.float32)) for name, picture, make in SOUNDS]


# The STATION page: a radio station's drops. Every sting is synthesized like the pads; the words are said by
# Windows' own speech voice, made once and kept in cache/ (see station_voices).

def hz(midi):
    """A MIDI note's frequency (60 is middle C)."""
    return 440 * 2 ** ((midi - 69) / 12)


def pluck(f, sec, decay=5.0):
    """A struck note (marimba, music box): bright at the strike, then dying away."""
    x = t(sec)
    return (sum(np.sin(2 * np.pi * f * k * x) * np.exp(-x * decay * k) / k ** 1.5 for k in (1, 2, 3, 4))
            * fade(len(x), 0.002, 0.05))


def tune(notes, beat, play):
    """A melody: (MIDI note, or None for a rest; length in beats) in order, each note made by play(freq, seconds)."""
    out = np.zeros(int(SR * (beat * sum(b for _, b in notes) + 1.5)))   # room for the last note to ring
    at = 0.0
    for m, b in notes:
        if m is not None:
            place(out, play(hz(m), b * beat), at)
        at += b * beat
    return out


def station_id():
    """Three notes climbing to the chord they make, with a sparkle on top: the station's logo."""
    out = np.zeros(int(SR * 2.6))
    for k, m in enumerate((72, 76, 79)):
        place(out, pluck(hz(m), 0.9, 4) * 0.6, k * 0.14)
    place(out, chord([hz(m) for m in (72, 76, 79, 84)], 2.0, 0.01) * 0.5, 0.42)
    place(out, bell(hz(96), 1.6, 3) * 0.2, 0.42)
    return out


def sponsors():
    """A cheesy word-from-our-sponsors tune on an oom-pah bass, ending on a bright chord and a cash register."""
    beat = 0.17
    out = tune([(72, 1), (74, 1), (76, 1), (79, 2), (76, 1), (79, 1), (84, 4)], beat,
               lambda f, sec: note(f, sec * 0.9) * 0.35 + pluck(2 * f, sec * 0.9, 6) * 0.25)
    place(out, tune([(48, 1), (55, 1), (48, 1), (55, 1), (53, 1), (55, 1), (48, 4)], beat,
                    lambda f, sec: pluck(f, min(sec, 0.6), 5)) * 0.7, 0)
    place(out, chord([hz(m) for m in (72, 76, 79, 84)], 1.0) * 0.3, 7 * beat)
    place(out, cha_ching() * 0.5, 7 * beat + 0.25)
    return out


def music():
    """Twelve seconds of upbeat chiptune pop (C, G, A minor, F, twice round) made to go round without a seam."""
    beat = 0.375   # 160 beats a minute
    bars = [(36, (60, 64, 67)), (43, (59, 62, 67)), (45, (57, 60, 64)), (41, (57, 60, 65))] * 2   # (bass, chord)
    lead = [76, 79, 84, 79, 74, 79, 83, 79, 72, 76, 81, 76, 72, 77, 81, 84,
            84, 83, 79, 76, 74, 79, 74, 71, 72, 76, 72, 69, 72, 74, 76, 79]
    kick, hat = drum(55, 0.25) * 0.9, bandpass(noise_hit(0.03, 0.006), 6000, 12000) * 0.25
    snare = noise_hit(0.18, 0.05) * 0.45 + drum(190, 0.18) * 0.3
    n = int(SR * beat * 4 * len(bars))
    out = np.zeros(n + 2 * SR)
    for k, (bass, triad) in enumerate(bars):
        bar = k * 4 * beat
        for e in range(8):   # eighth notes: the bass jumping an octave, the hi-hat, the chord on the off-beats
            at = bar + e * beat / 2
            place(out, pluck(hz(bass + 12 * (e % 2)), 0.18, 6) * 0.7, at)
            place(out, hat, at)
            if e % 2:
                place(out, sum(pluck(hz(m), 0.14, 8) for m in triad) / 3 * 0.45, at)
        for q in range(4):
            place(out, kick if q % 2 == 0 else snare, bar + q * beat)
            place(out, note(hz(lead[k * 4 + q]), beat * 0.85, 0.005) * 0.32, bar + q * beat)
    body = out[:n].copy()
    body[:2 * SR] += out[n:]   # what still rings at the end rings on into the start: the join is not heard
    return body


def storm():
    """Rain on the window and the wind, then a roll of thunder."""
    sec = 5.0
    x = t(sec)
    out = bandpass(rng.standard_normal(len(x)), 2000, 9000)
    out = out / np.max(np.abs(out)) * 0.18   # the hiss of rain
    for at in rng.uniform(0, sec - 0.05, 160):   # and its drops
        place(out, bandpass(noise_hit(0.02, 0.003), 1500, 7000) * rng.uniform(0.1, 0.4), at)
    wind = bandpass(rng.standard_normal(len(x)), 250, 900)
    out += wind / np.max(np.abs(wind)) * (0.25 + 0.2 * np.sin(2 * np.pi * 0.35 * x))
    y = t(2.4)
    rumble = lowpass(rng.standard_normal(len(y)), 200)
    place(out, rumble / np.max(np.abs(rumble)) * np.minimum(1, y / 0.05) * np.exp(-y * 1.3) * 1.2, 2.6)
    place(out, noise_hit(0.2, 0.04) * 0.5, 2.6)   # the crack
    return out * fade(len(out), 0.4, 0.8)


def breaking_news():
    """A whoosh rising into a big low hit, then an alert beeping."""
    out = np.zeros(int(SR * 2.8))
    x = t(0.7)
    riser = bandpass(rng.standard_normal(len(x)), 700, 6000)
    place(out, riser / np.max(np.abs(riser)) * (x / 0.7) ** 2 * 0.6, 0)
    place(out, drum(48, 1.2), 0.7)
    place(out, noise_hit(0.8, 0.18) * 0.5, 0.7)
    place(out, chord([hz(m) for m in (45, 52, 57, 60)], 1.4) * 0.7, 0.7)   # A minor, low
    y = t(0.1)
    for k in range(6):
        beep = np.sign(np.sin(2 * np.pi * (988 if k % 2 == 0 else 784) * y)) * fade(len(y), 0.003, 0.01)
        place(out, beep * 0.25, 1.15 + k * 0.2)
    return out


def goodnight_chime():
    """A soft chime: three bells, gently down."""
    out = np.zeros(int(SR * 3.4))
    for k, m in enumerate((76, 72, 67)):   # E, C, G
        place(out, bell(hz(m), 2.2, 1.6) * 0.5, k * 0.45)
    return out


# (name, picture, sound, what the station voice says or None, when the voice comes in: seconds into the sound with
# the sound dipping under it; None: the voice first, then the sound; "loop": the voice first, then the sound goes
# round until STOP). A sound given by name is that pad of the Sounds page.
STATION = [("Station ID", "📻", station_id, "You're listening to James' Radio!", 0.8),
           ("Sponsors", "💸", sponsors, "And now, our sponsors!", None),
           ("Music", "🎵", music, "Here's some music!", "loop"),
           ("Weather", "⛈️", storm, "Here's the weather", 0.4),
           ("Breaking news", "📰", breaking_news, "Breaking news!", 1.05),
           ("Applause", "👏", "Applause", None, None),
           ("Sign off", "🌙", goodnight_chime, "That's all for today, goodnight!", None)]
# The pages, in order: (name, picture, how many pads of the sound list, where on it the plugin buttons that ask for
# it go: None = after its pads). On STATION, Phone call (plugins/call_in.py) comes after Breaking news. PLAY has no
# pads of its own: the plugins fill it (animals, Silly voice, Sing-along), and it is left out when none do.
PAGES = [("Sounds", "🎛️", len(SOUNDS), None), ("Station", "📡", len(STATION), 5), ("Play", "🧸", 0, None)]


def trim(s, floor=0.01):
    """Without the quiet before and after: a speech file has some at both ends, a tune its last note's room."""
    on = np.flatnonzero(np.abs(s) > floor * np.max(np.abs(s)))
    if not len(on):
        return s
    s = s[max(0, on[0] - SR // 50):on[-1] + SR // 10]
    return s * fade(len(s), 0.005, 0.05)


def on_air(voice):
    """A voice clip ready for the air: a little radio-sounding, and as loud as a pad."""
    return loud(trim(bandpass(voice, 100, 7000)))


def station_pad(sting, voice=None, at=None):
    """A STATION pad's sound: the sting and the voice. at: seconds into the sting that the voice comes in, the sting
    dipping to 30% under it and coming back up after; None: the voice first, then the sting."""
    s = loud(trim(sting))
    if voice is None:
        return s.astype(np.float32)
    v = on_air(voice)
    if at is None:
        return np.concatenate([v, np.zeros(SR // 10), s]).astype(np.float32)
    i, ramp = int(SR * at), SR // 10
    out = np.zeros(max(len(s), i + len(v) + SR // 5))
    k = np.arange(len(s))
    under = np.clip((k - (i - ramp)) / ramp, 0, 1) * (1 - np.clip((k - (i + len(v))) / ramp, 0, 1))
    out[:len(s)] = s * (1 - 0.7 * under)
    out[i:i + len(v)] += v
    return limit(out).astype(np.float32)


class Loop:
    """A sound that goes round until it is stopped: the intro once, then the body again and again (a body made so
    its end runs on into its start, like music())."""

    def __init__(self, intro, body):
        self.intro, self.body = intro, body

    def __len__(self):   # once through
        return len(self.intro) + len(self.body)

    def chunk(self, pos, n):
        """The n samples from pos on; pos counts from the start of the intro."""
        k, a = np.arange(pos, pos + n), len(self.intro)
        body = self.body[(k - a) % len(self.body)]
        return (np.where(k < a, self.intro[np.minimum(k, a - 1)], body) if a else body).astype(np.float32)


def render_station(clips, sounds=()):
    """The STATION page's pads, (name, picture, sound) like render()'s. clips holds the voices (station_voices);
    sounds are the Sounds page's rendered pads, for a STATION pad that is one of them."""
    rendered, makers = {n: s for n, _, s in sounds}, {n: make for n, _, make in SOUNDS}
    out = []
    for name, picture, make, words, at in STATION:
        voice = clips.get(words) if words else None
        if isinstance(make, str):
            snd = rendered[make] if make in rendered else loud(makers[make]()).astype(np.float32)
        elif at == "loop":
            intro = np.zeros(0) if voice is None else np.concatenate([on_air(voice), np.zeros(SR // 5)])
            snd = Loop(intro.astype(np.float32), loud(make()).astype(np.float32))
        else:
            snd = station_pad(make(), voice, at)
        out.append((name, picture, snd))
    return out


CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
SPEECH = "Windows speech, 48 kHz 16-bit mono"   # part of each clip's file name: change it and they are made again


def voice_file(words, folder=CACHE):
    """Where the station voice saying `words` is kept."""
    return os.path.join(folder, "voice-" + hashlib.sha1(f"{SPEECH}|{words}".encode()).hexdigest()[:16] + ".wav")


def read_wav(path):
    """A WAV file's sound as mono float32 at SR; raises if it is missing, damaged, empty or silent."""
    with wave.open(path, "rb") as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width == 2:
        x = np.frombuffer(raw, "<i2") / 32768
    elif width == 1:
        x = (np.frombuffer(raw, np.uint8).astype(float) - 128) / 128
    else:
        raise ValueError(f"{width * 8}-bit WAV")
    x = x[:len(x) // channels * channels].reshape(-1, channels).mean(axis=1)
    if rate != SR:
        x = np.interp(np.arange(int(len(x) * SR / rate)) * rate / SR, np.arange(len(x)), x)
    if not np.any(x):
        raise ValueError("no sound in it")
    return x.astype(np.float32)


def speech_script(jobs):
    """The Windows PowerShell that speaks each (words, path) into its WAV file. Into files only: nothing is played."""
    data = json.dumps([{"text": w, "path": p} for w, p in jobs]).replace("'", "''")   # a '...' string: '' is one '
    return ("$ErrorActionPreference = 'Stop'\n"
            "Add-Type -AssemblyName System.Speech\n"
            f"$jobs = '{data}' | ConvertFrom-Json\n"
            "$format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(48000, "
            "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)\n"
            "$s = [System.Speech.Synthesis.SpeechSynthesizer]::new()\n"
            "try { foreach ($j in $jobs) { $s.SetOutputToWaveFile($j.path, $format); $s.Speak($j.text) } }\n"
            "finally { $s.SetOutputToNull(); $s.Dispose() }\n")


def windows_speech(jobs):
    """Speak each (words, path) into a WAV file with Windows' built-in voice (System.Speech, which needs Windows
    PowerShell 5.1); raises if it cannot. Nothing comes out of a speaker."""
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}   # PowerShell 7's breaks 5.1's modules
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                    base64.b64encode(speech_script(jobs).encode("utf-16-le")).decode()],
                   env=env, check=True, capture_output=True, timeout=120,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def station_voices(words, folder=CACHE, speak=windows_speech, before=None):
    """The station voice saying each of `words`: made with speak() the first time, all in one go, and read back
    from folder after that. None for any it could not make (no speech on this PC): those pads play the sting
    alone. before(n) is told first when n clips have to be made."""
    clips, todo = {}, []
    for w in words:
        try:
            clips[w] = read_wav(voice_file(w, folder))
        except Exception:   # not made yet, or damaged: make it (again)
            todo.append(w)
    if not todo:
        return clips
    if before:
        before(len(todo))
    parts = [(w, voice_file(w, folder) + ".part") for w in todo]   # under another name until it reads back whole,
    try:                                                           # so a run cut short leaves no broken clip
        os.makedirs(folder, exist_ok=True)
        speak(parts)
    except Exception:
        pass
    for w, part in parts:
        try:
            clips[w] = read_wav(part)
            os.replace(part, part[:-len(".part")])
        except Exception:
            clips[w] = None
            try:
                os.remove(part)
            except OSError:
                pass
    return clips


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


_com_threads = threading.local()


def com_ready():
    """Give this thread COM, once. PortAudio's WASAPI streams only start on a thread that has it, and PortAudio sets
    it up only on the thread that scans the devices: a mic opened from a page press (a server thread) or from the
    5 s look (the watch thread) failed with "Unanticipated host error". Never undone; the thread's end cleans up."""
    if not getattr(_com_threads, "ready", False):
        if os.name == "nt":
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0)   # S_OK; S_FALSE or RPC_E_CHANGED_MODE: it had COM already
        _com_threads.ready = True


def _com(obj, slot, *argtypes):
    """Method number `slot` (vtable order) of the COM object `obj`, called with the remaining arguments."""
    import ctypes
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[slot])
    return lambda *args: fn(obj, *args)


def windows_default_speaker():
    return windows_default(0)


def windows_default_mic():
    return windows_default(1)


def windows_default(flow):
    """An id for Windows' default speaker (flow 0) or microphone (flow 1) that changes when someone picks another
    one; None if Windows won't say. Asked of Windows' audio service (Core Audio) directly: PortAudio only learns
    of a new default by re-scanning, and a re-scan closes the streams that are open."""
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
        try:   # GetDefaultAudioEndpoint(eRender or eCapture, eMultimedia): the role PortAudio's WASAPI default uses
            if _com(enum, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(flow, 1, ctypes.byref(dev)) < 0:
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


MIC_GAIN = 0.3        # the live mic's loudness to start with; the sheet's slider goes 0 to 1
MIC_DELAY = 0.35      # seconds from the mic to the speaker, so you hear yourself a moment later; 0 = straight through
MAX_DELAY = 2.0
CEILING = 0.95        # limit() never passes 1.0; from 0.95 on it is flat out
MIC_FULL = 0.98       # the mic itself at full scale
MIC_LOUD = 0.5        # loud enough at the mic to be what pins the speaker
HOWL_SECONDS = 0.3    # pinned this long while the mic is live = feedback (or a shout right into it)
HUSH_SECONDS = 2.0    # ...and the mic is muted this long


class Ring:
    """The live mic's samples, handed from the mic's callback to the speaker's (two threads, each at its own pace)
    and held back `delay` samples on the way. A fixed buffer that never blocks or grows: a write that overflows
    drops the oldest samples; a read that runs dry plays silence and waits for `prime` samples before playing
    again; a backlog more than `slack` past the delay is skipped, so the voice never drifts further behind."""

    def __init__(self, size, prime=0, slack=None, delay=0):
        self.buf, self.size, self.prime, self.slack = np.zeros(size, np.float32), size, prime, slack
        self.lock = threading.Lock()
        self.start = self.count = self.delay = 0
        self.primed = False
        self.set_delay(delay)

    def _drop(self, k):
        self.start, self.count = (self.start + k) % self.size, self.count - k

    def _zero(self, at, k):
        first = min(k, self.size - at)
        self.buf[at:at + first] = 0
        self.buf[:k - first] = 0

    def write(self, x):
        x = np.asarray(x, np.float32).ravel()[-self.size:]
        with self.lock:
            end = (self.start + self.count) % self.size
            first = min(len(x), self.size - end)
            self.buf[end:end + first] = x[:first]
            self.buf[:len(x) - first] = x[first:]
            self.count += len(x)
            if self.count > self.size:   # full: the oldest go
                self._drop(self.count - self.size)

    def read(self, n):
        """The next n samples, oldest first; silence where there are none yet."""
        out = np.zeros(n, np.float32)
        with self.lock:
            if not self.primed and self.count < max(self.prime, n):
                return out
            if self.slack is not None and self.count - n > self.delay + self.slack:   # fell behind: skip ahead
                self._drop(self.count - n - self.delay)
            k = min(n, self.count)
            first = min(k, self.size - self.start)
            out[:first] = self.buf[self.start:self.start + first]
            out[first:k] = self.buf[:k - first]
            self._drop(k)
            self.primed = k == n
        return out

    def set_delay(self, samples):
        """Hold the voice back this many samples from now on: silence goes in front of what is waiting, or the
        oldest of it is skipped."""
        samples = max(0, min(int(samples), self.size - (self.slack or 0) - max(self.prime, 1)))
        with self.lock:
            more = samples - self.delay
            self.delay = samples
            if more > 0:
                more = min(more, self.size - self.count)
                self.start = (self.start - more) % self.size
                self._zero(self.start, more)
                self.count += more
            elif more < 0:
                self._drop(min(-more, self.count))

    def clear(self):
        """Nothing waiting but the delay's silence."""
        with self.lock:
            self.start, self.count, self.primed = 0, self.delay, False
            self._zero(0, self.delay)


def _listing(key, listed, picked, pin, default):
    """A device list for the page's sheet: a short name for each row, the full one underneath."""
    names = [n for _, n in listed]
    return {"follow": pin is None, "missing": bool(pin) and picked is None,
            "pinned": short_name(pin, names + [pin]) if pin else "",
            "default": short_name(default, names) if default else "",
            key: [{"index": i, "name": n, "short": short_name(n, names), "picked": i == picked} for i, n in listed]}


class Mixer:
    """One output stream; overlapping sounds are summed in the callback.

    Which speaker: the one picked on the page, else the one named with --out, else Windows' default, followed
    when it changes. Changes wait for the sounds playing to end. A picked or named speaker that is away is waited
    for on Windows' default.

    The microphone is on the air from the start (main() asks for it) until the MIC pad mutes it; one plugged in
    later goes on the air by itself. It has its own input stream, open only while it is on the air. Its callback
    feeds a ring that the output callback mixes from, so the voice always comes out of the same speaker as the
    pads. The mic is the one picked on the page, else Windows' default."""

    def __init__(self, out_part, default_id=windows_default_speaker, mic_default_id=windows_default_mic):
        self.voices, self.lock, self.volume, self.stream = [], threading.Lock(), 1.0, None
        self.music = None   # (Loop, position) while the music goes round
        self.level, self.open_lock, self.device, self.fallback = 0.0, threading.Lock(), "", False
        self.closed = False
        self.pin, self.pin_exact = out_part or None, False   # stay on this speaker (part of its name); None: follow
        self.default_id, self.seen_default = default_id, None   # Windows' default speaker when the stream opened
        self.outputs = []   # (index, name) of every WASAPI output at the last scan
        self.listed, self.default_name = [], ""   # the outputs as the page's picker last showed them; the default's name
        self.switch = False   # a speaker was picked while sounds played: change over when they end
        self.scanned_at = 0.0
        # the live mic: mic_lock guards its stream (lock order: open_lock, then mic_lock)
        self.mic_lock, self.mic_stream, self.mic_live, self.ring = threading.Lock(), None, False, None
        self.want_air = False   # on the air unless muted: a mic that comes (back) is put on by watch()
        self.mic_pin = None   # the picked mic's exact name; None: Windows' default
        self.mic_default_id, self.seen_mic_default = mic_default_id, None
        self.inputs, self.mics_listed, self.default_in, self.default_mic = [], [], -1, ""
        self.mic_gain, self.mic_delay, self.mic_level = MIC_GAIN, MIC_DELAY, 0.0
        self.hot = self.hush = 0   # the howl guard, in frames: pinned so far, still muted

    def label(self):
        """The speaker's short name, for the display."""
        return short_name(self.device, [n for _, n in self.outputs])

    def _pinned(self, name):
        if not self.pin:
            return False
        return name == self.pin if self.pin_exact else self.pin.lower() in name.lower()

    def _open(self):
        """(Re)open on the speaker we should be on (see the class note). Re-scans the devices first; a re-scan
        closes every stream, so a live mic is closed before it and opened again after."""
        with self.mic_lock:
            self._close_mic()
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            self.device, self.switch = "No speaker", False   # what the page shows until a stream opens
            self.seen_default = self.default_id()   # before the scan: a change after this is caught on the next look
            self.seen_mic_default = self.mic_default_id()
            default = self._scan()
            try:
                wanted = [i for i, name in self.outputs if self._pinned(name)]
                dev = wanted[0] if wanted else default
                if dev < 0:
                    raise LookupError("Windows has no output device")
                self.fallback = bool(self.pin) and not wanted
                ch = min(2, sd.query_devices(dev)["max_output_channels"])
                self.stream = sd.OutputStream(device=dev, samplerate=SR, channels=ch, dtype="float32", latency="low",
                                              callback=self._mix, extra_settings=sd.WasapiSettings(auto_convert=True))
                self.stream.start()
                self.device = sd.query_devices(dev)["name"]
            finally:
                if self.mic_live:
                    try:
                        self._open_mic()
                    except Exception:
                        self.mic_live = False
            return self.device

    def _scan(self):
        """Re-scan the devices (PortAudio only knows the list from when it started); the default output's index."""
        com_ready()
        sd._terminate()
        sd._initialize()
        self.scanned_at = time.monotonic()
        api = next(i for i, a in enumerate(sd.query_hostapis()) if a["name"] == "Windows WASAPI")
        devices = [(i, d) for i, d in enumerate(sd.query_devices()) if d["hostapi"] == api]
        self.outputs = [(i, d["name"]) for i, d in devices if d["max_output_channels"] > 0]
        self.inputs = [(i, d["name"]) for i, d in devices if d["max_input_channels"] > 0]
        info = sd.query_hostapis(api)
        self.default_name = dict(self.outputs).get(info["default_output_device"], "")
        self.default_in = info["default_input_device"]
        self.default_mic = dict(self.inputs).get(self.default_in, "")
        return info["default_output_device"]

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
            if dead or (not self.voices and (self.fallback or self.switch or self._default_moved())):   # never cut a sound short
                try:
                    self._open()   # a live mic comes along (closed for the re-scan, opened again after)
                except Exception:
                    pass
        if self.want_air and not self.mic_live and self.inputs:
            self.onair(True)   # not muted, and there is a mic now (plugged in, or it would not open before)
        return True

    def speakers(self):
        """The page's speaker list. Re-scanned when nothing is playing, so a speaker switched on a moment ago is in
        it (a re-scan reopens the stream, which is why it waits for silence)."""
        with self.open_lock:
            if not self.voices and not self.closed:
                try:
                    self._open()
                except Exception:
                    pass
            self.listed = list(self.outputs)
            picked = next((i for i, n in self.listed if self._pinned(n)), None)
            return _listing("speakers", self.listed, picked, self.pin, self.default_name)

    def mics(self):
        """The page's microphone list, with the mic's loudness (%) and delay (ms). Re-scanned like speakers(),
        unless that has just been done (the sheet asks for both)."""
        with self.open_lock:
            fresh = time.monotonic() - self.scanned_at < 1
            if not self.voices and not self.closed and not fresh:
                try:
                    self._open()
                except Exception:
                    pass
            self.mics_listed = list(self.inputs)
            picked = next((i for i, n in self.mics_listed if n == self.mic_pin), None)
            return dict(_listing("mics", self.mics_listed, picked, self.mic_pin, self.default_mic),
                        gain=round(self.mic_gain * 100), delay=round(self.mic_delay * 1000))

    def choose_mic(self, index):
        """The page's mic pick, like choose(); it also holds for Call in. A live mic moves over at once.
        KeyError: not in the list the page was shown."""
        with self.mic_lock:
            self.mic_pin = None if index is None else dict(self.mics_listed)[index]
            if self.mic_live:
                self._close_mic()
                try:
                    self._open_mic()
                except Exception:
                    self.mic_live = False

    def set_delay(self, ms):
        """Mic to speaker, 0 to 2000 ms; at once, even mid-sentence."""
        self.mic_delay = max(0.0, min(float(ms), MAX_DELAY * 1000)) / 1000
        if self.ring is not None:
            self.ring.set_delay(SR * self.mic_delay)

    def onair(self, on):
        """True = on the air, False = muted (the MIC pad). The choice holds: a mic that comes later follows it.
        Returns False when there is no mic to put on the air."""
        self.want_air = on
        if not on:
            with self.mic_lock:
                self.mic_live = False
                self._close_mic()
            return True
        self._ready()   # the voice comes out of the speaker's stream
        with self.mic_lock:
            if self.mic_live and self.mic_stream is not None:
                return True
            try:
                self._open_mic()
            except Exception:   # LookupError: no mic; PortAudioError: it would not open
                self._close_mic()
                return False
            self.mic_live = True
            return True

    def mic_state(self):
        """For the page: "none" (no mic), "off", "live", or "hush" (muted by the howl guard)."""
        if self.mic_live:
            return "hush" if self.hush > 0 else "live"
        return "off" if self.inputs else "none"

    def _mic_index(self):
        """The picked mic if it is plugged in, else Windows' default; -1 for none."""
        return next((i for i, n in self.inputs if n == self.mic_pin), self.default_in)

    def _open_mic(self):
        """Open the mic's stream the way mic_echo.py does (WASAPI, small blocks). Caller holds mic_lock."""
        dev = self._mic_index()
        if dev < 0:
            raise LookupError("no microphone")
        com_ready()
        if self.ring is None:   # the longest delay, plus room for the two streams' blocks to wobble
            self.ring = Ring(int(SR * MAX_DELAY) + SR // 4, prime=SR // 100, slack=SR * 3 // 100,
                             delay=SR * self.mic_delay)
        self.ring.clear()
        self.hot = self.hush = 0
        self.mic_stream = sd.InputStream(device=dev, samplerate=SR, channels=1, dtype="float32", latency="low",
                                         callback=self._listen, extra_settings=sd.WasapiSettings(auto_convert=True))
        self.mic_stream.start()

    def _close_mic(self):
        """Caller holds mic_lock."""
        if self.mic_stream is not None:
            try:
                self.mic_stream.close()
            except Exception:
                pass
            self.mic_stream = None
        self.mic_level, self.hot, self.hush = 0.0, 0, 0

    def _listen(self, indata, frames, when, status):
        x = np.clip(np.nan_to_num(indata[:, 0], nan=0.0, posinf=0.0, neginf=0.0), -1.0, 1.0)   # hard limit on the way in
        self.ring.write(x)
        self.mic_level = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0   # the page's meter while ON AIR

    def choose(self, index):
        """The page's pick: an index from the list it was shown, or None for Windows' default (followed again).
        It holds for the rest of the run. With sounds playing, the change waits for them to end.
        KeyError: not in that list."""
        with self.open_lock:
            if index is None:
                self.pin, self.pin_exact = None, False
            else:
                self.pin, self.pin_exact = dict(self.listed)[index], True
            if self.voices:
                self.switch = True
                return
            try:
                self._open()
            except Exception:
                pass

    def _default_moved(self):
        """Windows' default speaker or mic is not the one it was at the last scan (only matters while following:
        a new default mic needs a re-scan to be opened, and a first mic plugged in shows up the same way)."""
        def moved(now, seen):
            return now is not None and now != seen
        return ((not self.pin and moved(self.default_id(), self.seen_default))
                or (not self.mic_pin and moved(self.mic_default_id(), self.seen_mic_default)))

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
            if self.music is not None:
                loop, pos = self.music
                mix += loop.chunk(pos, frames)
                self.music = (loop, pos + frames)
        voice = None
        if self.mic_live:
            voice = self.ring.read(frames)
            if self.hush > 0:   # muted by the howl guard: the voice is dropped
                self.hush, voice = self.hush - frames, None
            else:
                mix += voice * self.mic_gain
        out = limit(mix * self.volume)
        if voice is not None:
            self._guard(voice, out)
        self.level = float(np.sqrt(np.mean(out ** 2)))   # drives the page's LED meter
        outdata[:] = out[:, None]

    def _guard(self, voice, out):
        """The howl guard. Feedback (or a shout right into the mic) pins the mic at full scale, or pins the speaker
        at the limiter's ceiling with the mic loud too. Pinned for HOWL_SECONDS on end: the mic is muted for
        HUSH_SECONDS and the page says "too loud, move the mic"."""
        mic = float(np.max(np.abs(voice))) if len(voice) else 0.0
        pinned = mic >= MIC_FULL or (mic >= MIC_LOUD and float(np.max(np.abs(out))) >= CEILING)
        self.hot = self.hot + len(voice) if pinned else 0
        if self.hot >= HOWL_SECONDS * SR:
            self.hot, self.hush = 0, int(HUSH_SECONDS * SR)
            self.ring.clear()   # the howl waiting in the delay goes too

    def _ready(self):
        """Open the speaker if it is not open; False when there is none."""
        if self.stream is None or not self.stream.active:   # a live stream needs no lock (record() may hold it)
            with self.open_lock:   # button presses arrive on several server threads at once
                if self.stream is None or not self.stream.active:
                    try:
                        self._open()
                    except Exception:
                        return False
        return True

    def play(self, snd):
        """Start a sound; sounds overlap. A Loop is the music: it goes round until stop_all() or the next Loop played,
        which stops it instead."""
        if isinstance(snd, Loop):
            with self.lock:
                if self.music is not None:
                    self.music = None
                    return
        if not self._ready():
            return     # no speaker: drop the sound rather than queue a pile-up that all plays later
        with self.lock:
            if isinstance(snd, Loop):
                self.music = (snd, 0)
            else:
                self.voices.append((snd, 0))

    def record(self, seconds, halt=None):
        """Mono float32 samples from the microphone (the one picked on the page, else Windows' default); None when
        there is none. record(0) only checks. A halt (threading.Event) that is set ends it early, with what it has:
        STOP, for a plugin button that listens for it."""
        with self.open_lock:   # held throughout: a device re-scan in _open() would pull the mic from under the read
            if self._mic_index() < 0:
                try:
                    self._open()   # re-scan once: a mic plugged in after start is unknown until then
                except Exception:
                    pass
            mic = self._mic_index()
            if mic < 0:
                return None
            if seconds <= 0:
                return np.zeros(0, np.float32)
            try:
                com_ready()
                with sd.InputStream(device=mic, samplerate=SR, channels=1, dtype="float32",
                                    extra_settings=sd.WasapiSettings(auto_convert=True)) as stream:
                    if halt is None:
                        data, _ = stream.read(int(SR * seconds))
                    else:   # in 50 ms pieces, looking for the halt between them
                        pieces, left = [], int(SR * seconds)
                        while left > 0 and not halt.is_set():
                            piece, _ = stream.read(min(left, SR // 20))
                            pieces.append(piece)
                            left -= len(piece)
                        data = np.concatenate(pieces) if pieces else np.zeros((0, 1), np.float32)
            except sd.PortAudioError:
                return None
            return data[:, 0].copy()

    def stop_all(self):
        with self.lock:
            self.voices, self.music = [], None

    def close(self):
        with self.open_lock:   # not halfway through watch() swapping streams
            self.closed = True
            with self.mic_lock:   # the one thing that takes the mic off the air without a press
                self.want_air = self.mic_live = False
                self._close_mic()
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


class Server(ThreadingHTTPServer):
    """The page server, alone on its port. Python's default SO_REUSEADDR lets a second server on Windows bind a
    port that is taken, so two Wi-Fi launches would share 8481 and a phone would reach either radio."""
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):   # Windows: nobody else may bind it while we have it either
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def pad_config(sounds, pages, buttons=()):
    """The pads as the page draws them. pages lists (name, picture, how many pads, where on it its plugin buttons go
    or None for the end) in order, and each page takes the next pads of sounds; buttons are those plugin buttons,
    (index, page name, label, colour). A page of eight pads or fewer has them four to a row, the others six; the
    keys follow the rows (123456 / QWERTY / ASDFGH / ZXCVBN) and the colours go round the rainbow. A sound pad plays
    its place in sounds (POST /play/<i>), a button pad presses its button (POST /button/<k>)."""
    if sum(count for _, _, count, _ in pages) != len(sounds):
        raise ValueError("the pages do not add up to the sounds")
    cfg, first = [], 0
    for p, (title, _, count, slot) in enumerate(pages):
        pads = [{"i": first + j} for j in range(count)]
        mine = [{"button": k, "label": label, "c": color} for k, page, label, color in buttons
                if page.lower() == title.lower()]
        at = count if slot is None else min(slot, count)
        pads[at:at] = mine
        width = 4 if len(pads) <= 8 else 6
        for j, pad in enumerate(pads):
            row, col = divmod(j, width)
            pad.update({"page": p, "key": KEYS[row * 6 + col] if row < 4 else ""})
            if "i" in pad:
                name, emoji, snd = sounds[pad["i"]]
                top, bottom, skirt, text = RAINBOW[j % 6]
                pad.update({"name": name, "emoji": emoji, "dur": round(len(snd) / SR, 2),
                            "loop": isinstance(snd, Loop), "c": top, "d": bottom, "e": skirt, "t": text})
        cfg += pads
        first += count
    return cfg


def make_server(mixer, sounds, radio, address="127.0.0.1", port=0, pages=None, voiceless=()):
    """pages: see pad_config (default: all the sounds on one page); voiceless: the sounds whose voice could not be
    made, so the page says so (once) the first time one is pressed."""
    token = secrets.token_urlsafe(16)   # only this page may press the buttons
    pages = pages or [(PAGES[0][0], PAGES[0][1], len(sounds), None)]
    named = {b.page.lower() for b in radio._buttons if b.page}
    pages = [p for p in pages if p[2] or p[0].lower() in named]   # a page with no pads of its own needs a plugin
    titles = {title.lower() for title, *_ in pages}
    on_pages = [(k, b.page, b.label, b.color) for k, b in enumerate(radio._buttons)
                if b.page and b.page.lower() in titles]
    cfg = pad_config(sounds, pages, on_pages)
    for pad in cfg:
        if pad.get("i") in set(voiceless):
            pad["novoice"] = True
    paged = {k for k, *_ in on_pages}
    page_list = [{"name": title, "emoji": emoji, "few": sum(pad["page"] == p for pad in cfg) <= 8}
                 for p, (title, emoji, *_) in enumerate(pages)]
    extras = [{"label": b.label, "c": b.color, "k": k} for k, b in enumerate(radio._buttons) if k not in paged]
    as_js = lambda v: json.dumps(v).replace("<", "\\u003c")   # a label can never close the <script>
    page = (PAGE.replace("__CONFIG__", as_js(cfg)).replace("__EXTRAS__", as_js(extras))
            .replace("__PAGES__", as_js(page_list))
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
            u = urlsplit(self.path)
            path = u.path
            if path == "/":
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/level":
                state["last_poll"] = time.monotonic()   # the page's poll doubles as its heartbeat
                if self._from_pc():
                    state["pc_poll"] = state["last_poll"]
                self._send(200, json.dumps({"level": mixer.level, "speaker": mixer.label(),
                                            "volume": round(mixer.volume * 100),
                                            "msg": list(radio._message), "mic": mixer.mic_state(),
                                            "micLevel": round(mixer.mic_level, 4),
                                            "delay": round(mixer.mic_delay * 1000),
                                            "music": mixer.music is not None}).encode(), "application/json")
            elif path == "/speakers":   # token: listing re-scans the devices, so only the page may ask
                if parse_qs(u.query).get("t", [""])[0] != token:
                    return self._send(403)
                self._send(200, json.dumps(mixer.speakers()).encode(), "application/json")
            elif path == "/mics":   # the same for the mics
                if parse_qs(u.query).get("t", [""])[0] != token:
                    return self._send(403)
                self._send(200, json.dumps(mixer.mics()).encode(), "application/json")
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
                    radio._stop()   # the plugins that asked hear about it (sounds first: a slow plugin can't hold them)
                elif what == "volume":
                    mixer.volume = max(0.0, min(float(arg), 150.0)) / 100
                elif what == "speaker":
                    mixer.choose(None if arg == "default" else int(arg))
                elif what == "mic":
                    mixer.choose_mic(None if arg == "default" else int(arg))
                elif what == "micgain":
                    mixer.mic_gain = max(0.0, min(float(arg), 100.0)) / 100
                elif what == "micdelay":
                    mixer.set_delay(float(arg))
                elif what == "onair" and arg in ("on", "off"):
                    if not mixer.onair(arg == "on"):
                        return self._send(409)          # no mic: the pad says so and nothing happens
                elif what == "bye":
                    if self._from_pc():                 # a phone closing its tab does not stop the radio
                        state["bye_at"] = time.monotonic()
                else:
                    return self._send(404)
            except ValueError:
                return self._send(400)
            except KeyError:
                return self._send(404)
            self._send(204)

    try:
        server = Server((address, port), Handler)
    except OSError:
        if not port:
            raise
        server = Server((address, 0), Handler)   # the usual port is taken: any free one
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
    p = argparse.ArgumentParser(description="James' Radio: 24 sounds on glowing toy-style pads, and a STATION page.")
    p.add_argument("--out", default="", help="part of the output device's name (default: Windows default)")
    p.add_argument("--no-window", action="store_true", help="serve the page but don't open a window")
    p.add_argument("--mic-off", action="store_true",
                   help="start with the microphone muted (the MIC pad puts it on the air)")
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
    mixer.onair(not a.mic_off)   # the mic is on the air from the start; the MIC pad mutes it
    sounds = render()
    clips = station_voices([words for *_, words, _ in STATION if words],
                           before=lambda n: print(f"Making the station's voice ({n} clips, the first time only)...",
                                                  flush=True))
    sounds += render_station(clips, sounds)
    voiceless = [len(SOUNDS) + j for j, (*_, words, _) in enumerate(STATION) if words and clips[words] is None]
    if voiceless:
        print("Windows speech is not working on this PC, so the STATION pads play their sound without the voice.",
              flush=True)
    radio = plugin_radio(mixer, sounds)
    loaded = plugins.load_all(radio)
    try:
        server, url, state = make_server(mixer, sounds, radio, address, SHARE_PORT if a.share else 0,
                                         PAGES, voiceless)
    except OSError as e:
        mixer.close()
        sys.exit(f"James' Radio: cannot listen on {address}: {e.strerror or e}")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=mixer.watch, daemon=True).start()
    mic = "on the air" if mixer.mic_live else "muted" if mixer.inputs else "none yet"
    print(f"James' Radio on {url} -> {device}; mic {mic}; "
          f"plugins: {', '.join(loaded) or 'none'}", flush=True)
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
.device { position: relative; width: 96rem; padding: 2.6rem 3.2rem 2.8rem; border-radius: 3.2rem;
  display: grid; grid-template-columns: 1fr auto auto; align-items: center;
  grid-template-areas: "top top top" "tabs tabs tabs" "pads pads pads" "bottom extras grille";
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

.top { grid-area: top; display: flex; align-items: center; gap: 2.4rem; margin-bottom: 1rem; min-width: 0; }
.brand { margin: 0; color: #eef0f3; font-weight: 900; font-size: 3rem; letter-spacing: .04em; line-height: .95;
  text-shadow: 0 .15rem 0 #000, 0 -.08rem 0 rgba(255,255,255,.2); }
.brand b { background: linear-gradient(90deg, #ff6161, #ffad45, #ffe65e, #66e680, #66b8ff, #c896ff);
  -webkit-background-clip: text; background-clip: text; color: transparent; text-shadow: none;
  filter: drop-shadow(0 .15rem 0 #000); }
.brand small { display: block; font-size: max(12px, 1.2rem); font-weight: 700; color: #a3aab4; letter-spacing: .3em; margin-top: .7rem; }
.lcd { flex: 1; min-width: 0; height: max(7.4rem, calc(44px + 1rem)); border-radius: 1.1rem; padding: 0 2.2rem; position: relative; overflow: hidden;
  display: flex; align-items: center; justify-content: space-between; gap: 2rem;
  background: linear-gradient(180deg, #0a1d10, #10301b); border: .5rem solid #0b0c0e;
  box-shadow: inset 0 .5rem 1.2rem rgba(0,0,0,.9), 0 .12rem 0 rgba(255,255,255,.14); }
.lcd::after { content: ""; position: absolute; inset: 0; pointer-events: none;
  background: repeating-linear-gradient(0deg, rgba(0,0,0,.28) 0 2px, transparent 2px 4px),
              linear-gradient(160deg, rgba(255,255,255,.08), transparent 40%); }
#now { min-width: 0; font: 700 2.9rem Consolas, "Courier New", monospace; color: #62ff8f; letter-spacing: .12em;
  text-transform: uppercase; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  text-shadow: 0 0 .9rem rgba(98,255,143,.85); }
#now.long { font-size: 2.1rem; letter-spacing: .06em; }   /* a long message still fits in one line */
/* The speaker line and meter are one button, the display's full height: a tap opens the speaker sheet. */
.right { display: flex; flex-direction: column; align-items: flex-end; justify-content: center; gap: .5rem;
  flex: 0 1 auto; min-width: 0; max-width: 100%; align-self: stretch; margin: 0 -1.2rem 0 0; padding: 0 1.2rem;
  border: 0; border-radius: .8rem; background: none; color: inherit; font: inherit; cursor: pointer; }
.right:active { background: rgba(98, 255, 143, .14); }
.right:focus-visible { outline: .3rem solid #62ff8f; outline-offset: -.3rem; }
.spkline { display: flex; align-items: center; gap: .6rem; min-width: 0; max-width: 100%; }
#spk { min-width: 0; font: 700 max(12px, 1.2rem) Consolas, monospace; color: #4fd179; letter-spacing: .1em;
  text-transform: uppercase; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.chev { flex: none; color: #4fd179; font-size: max(12px, 1.2rem); line-height: 1; }
/* ON AIR: the display turns red and shows how long the mic is held back. */
.lcd.onair #now { color: #ff6b5e; text-shadow: 0 0 .9rem rgba(255, 107, 94, .85); }
.dly { flex: none; font: 700 max(12px, 1.3rem) Consolas, monospace; color: #ff8a80; letter-spacing: .1em;
  white-space: nowrap; }
.meter { display: flex; gap: .45rem; flex: none; }
.meter i { width: .95rem; height: 3.4rem; border-radius: .2rem; background: #163020; }
.meter i:nth-child(n+11) { background: #35300f; }
.meter i:nth-child(n+14) { background: #3a1414; }
.meter i.on { background: #4dff7c; box-shadow: 0 0 .8rem #4dff7c; }
.meter i.on:nth-child(n+11) { background: #ffd84d; box-shadow: 0 0 .8rem #ffd84d; }
.meter i.on:nth-child(n+14) { background: #ff4d4d; box-shadow: 0 0 .8rem #ff4d4d; }

/* Page tabs: latching keys, like a radio's band buttons; the lit one is the page you see. */
.tabs { grid-area: tabs; justify-self: center; display: flex; gap: .8rem; margin: 0 0 1rem; padding: .4rem;
  border-radius: 1.8rem; background: #111317;
  box-shadow: inset 0 .3rem .8rem rgba(0,0,0,.9), 0 .12rem 0 rgba(255,255,255,.12); }
.tabs[hidden] { display: none; }
.tab { display: flex; align-items: center; justify-content: center; gap: 1rem; min-width: 19rem; height: max(46px, 4.3rem);
  padding: 0 2.4rem; border: 0; border-radius: 1.3rem; cursor: pointer; color: #9aa1ab;
  font: 900 max(12px, 1.5rem) "Segoe UI", system-ui, sans-serif; letter-spacing: .24em; text-transform: uppercase;
  background: linear-gradient(180deg, #41454e, #2b2e35);
  box-shadow: 0 .35rem 0 #17191d, 0 .5rem .6rem rgba(0,0,0,.5), inset 0 .1rem 0 rgba(255,255,255,.18);
  transform: translateY(-.3rem); transition: transform .2s, box-shadow .2s, color .2s; }
.tab[aria-selected="true"] { color: #eef0f3; transform: none;
  box-shadow: 0 .05rem 0 #17191d, inset 0 .3rem .6rem rgba(0,0,0,.5), inset 0 .1rem 0 rgba(255,255,255,.08); }
.tab:focus-visible { outline: .3rem solid #62ff8f; outline-offset: .3rem; }
.tname { margin-right: -.24em; }   /* the letter spacing after the last letter would pull the word off centre */
.led { flex: none; width: 1rem; height: 1rem; border-radius: 50%; background: #163020;
  box-shadow: inset 0 .1rem .2rem rgba(0,0,0,.8); }
.tab[aria-selected="true"] .led { background: #4dff7c; box-shadow: 0 0 .8rem #4dff7c; }
/* The pages of pads sit side by side on a track that slides inside the panel; the pages you can't see are inert. */
.pads { grid-area: pads; overflow: hidden; overflow: clip; padding: 1.9rem 1.8rem 1.6rem;
  border-radius: 2rem; background: #111317;
  box-shadow: inset 0 .5rem 1.5rem rgba(0,0,0,.9), 0 .12rem 0 rgba(255,255,255,.12); }
.pads { position: relative; }
.track { --gap: 3.6rem; display: flex; gap: var(--gap); touch-action: pan-y pinch-zoom; container-type: inline-size;
  transition: transform .42s cubic-bezier(.2, .9, .25, 1), height .3s ease; }
.grid { --g: 1.8rem; flex: 0 0 100%; min-width: 0; display: grid; grid-template-columns: repeat(6, 1fr); gap: var(--g);
  align-content: center; }
/* A short page (eight pads or fewer): rows of at most four, centred, each pad as big as on a full page. */
.grid.few { display: flex; flex-wrap: wrap; justify-content: center; align-items: flex-start;
  --pw: calc((100cqi - 5 * var(--g)) / 6 - .1px);
  padding-inline: max(0px, calc((100cqi - 4 * var(--pw) - 3 * var(--g)) / 2)); }
.grid.few > .well { flex: none; width: var(--pw); }
/* The hand hint, on a touch screen until the page has been turned: a hand sweeping the way a swipe goes. */
.hint { display: none; position: absolute; z-index: 3; top: 2.6rem; right: 2.4rem; align-items: center; gap: .6rem;
  padding: .6rem 1.8rem .6rem 3.2rem; border-radius: 99rem; pointer-events: none; color: #eef0f3;
  background: rgba(8, 9, 11, .84); box-shadow: 0 .4rem 1.2rem rgba(0,0,0,.6);
  font: 900 max(12px, 1.4rem) "Segoe UI", system-ui, sans-serif; letter-spacing: .2em; }
.hand { display: inline-block; font-size: max(22px, 2.8rem); line-height: 1;
  font-family: "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", sans-serif;
  animation: sweep 1.8s ease-in-out infinite; }
@keyframes sweep { 0%, 12% { transform: translateX(1.4rem); opacity: 0; } 30% { opacity: 1; }
  75% { transform: translateX(-1.8rem); opacity: 1; } 100% { transform: translateX(-1.8rem); opacity: 0; } }
@media (hover: none) { .hint:not([hidden]) { display: flex; } }
.well { container-type: inline-size; aspect-ratio: 1.14; min-width: 0; border-radius: 2rem; padding: .7rem; background: #08090b;
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

.bottom { grid-area: bottom; display: flex; align-items: center; gap: 3rem; margin-top: 2rem; min-width: 0; }
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
.extras { grid-area: extras; display: flex; gap: 1.6rem; margin: 2rem 0 0 3rem; }
.extras:empty { display: none; }
.extras .well { width: 12rem; height: 10.5rem; aspect-ratio: auto; }
.extras .micwell { width: 15rem; }
/* Sized by the pad's height, not its width: on a phone it is two pads wide and one pad tall. */
.micwell { container-type: size; }
.micwell .emo { font-size: 40cqmin; }
.micwell .lbl { font-size: max(12px, 12cqmin); }
/* The MIC pad: a red ON AIR light while the mic is on the air, grey when muted. */
.well.onair { box-shadow: inset 0 .6rem 1rem rgba(0,0,0,.6), 0 0 3rem .6rem #ff3b30, 0 .1rem 0 rgba(255,255,255,.08);
  animation: onair 1.6s ease-in-out infinite; }
@keyframes onair { 50% { box-shadow: inset 0 .6rem 1rem rgba(0,0,0,.6), 0 0 1.6rem .2rem #ff3b30, 0 .1rem 0 rgba(255,255,255,.08); } }
.pad[aria-disabled="true"] { filter: grayscale(1) brightness(.7); cursor: default; }
.grille { grid-area: grille; width: 12rem; height: 9.5rem; margin: 2rem 0 0 3rem; border-radius: 1.4rem;
  background: radial-gradient(circle, #050506 40%, transparent 46%) 0 0 / 1.2rem 1.2rem, linear-gradient(#2c2f36, #24272d);
  box-shadow: inset 0 .3rem .6rem rgba(0,0,0,.8), 0 .1rem 0 rgba(255,255,255,.1); }

/* Speaker sheet: a card that drops from the display on a big screen, a bottom sheet on a phone.
   Real pixels, not toy units: it is read at arm's length on every screen. */
.sheet { position: fixed; inset: var(--top, 12vh) var(--right, 16px) auto auto; margin: 0; padding: 0; border: 0;
  width: 400px; max-width: calc(100vw - 32px); max-height: min(70vh, 560px); overflow: hidden; border-radius: 18px;
  color: #eef0f3; background: #24272e; transform-origin: top right; user-select: none; -webkit-user-select: none;
  box-shadow: 0 24px 60px rgba(0, 0, 0, .65), inset 0 1px 0 rgba(255, 255, 255, .12);
  font: 16px/1.3 "Segoe UI", system-ui, -apple-system, sans-serif; }
.sheet::backdrop { background: rgba(4, 5, 7, .55); }
.sheet[open] { animation: sheet-drop .3s cubic-bezier(.2, .9, .3, 1.15) both; }
.sheet.closing { animation: sheet-lift .18s ease-in both; }
@keyframes sheet-drop { from { opacity: 0; transform: translateY(-8px) scale(.96); } }
@keyframes sheet-lift { to { opacity: 0; transform: translateY(-8px) scale(.96); } }
@keyframes sheet-rise { from { transform: translateY(100%); } }
@keyframes sheet-sink { to { transform: translateY(100%); } }
.sheet-in { display: flex; flex-direction: column; max-height: inherit; }
.sheet-head { display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 4px 6px 4px 20px; border-bottom: 1px solid rgba(255, 255, 255, .09); }
.sheet h2 { margin: 0; font-size: 17px; font-weight: 700; }
.done { min-width: 64px; min-height: 44px; padding: 0 12px; border: 0; border-radius: 12px; background: none;
  color: #62ff8f; font-family: inherit; font-size: 17px; font-weight: 600; cursor: pointer; }
.done:focus-visible, .row:focus-visible { outline: 3px solid #62ff8f; outline-offset: -3px; }
.list { overflow-y: auto; overscroll-behavior: contain; padding: 4px 0 calc(8px + env(safe-area-inset-bottom)); }
.row { display: flex; align-items: center; gap: 14px; width: 100%; min-height: 56px; margin: 0; padding: 8px 20px;
  border: 0; background: none; color: inherit; font-family: inherit; text-align: left; cursor: pointer; }
.row + .row { border-top: 1px solid rgba(255, 255, 255, .07); }
.row:active { background: rgba(255, 255, 255, .08); }
.row .txt { flex: 1; min-width: 0; }
.row b { display: block; font-size: 17px; font-weight: 600; }
.row small { display: block; margin-top: 2px; color: #a3aab4; font-size: 13px; overflow-wrap: anywhere; }
.row .tick { flex: none; width: 24px; color: #62ff8f; font-size: 20px; font-weight: 800; text-align: center; visibility: hidden; }
.row[aria-checked="true"] .tick { visibility: visible; }
.note { margin: 0; padding: 14px 20px; color: #a3aab4; font-size: 15px; }
.note.warn { color: #ffd84d; }
.group { margin: 0; padding: 16px 20px 4px; color: #a3aab4; font-size: 13px; font-weight: 700; letter-spacing: .08em;
  text-transform: uppercase; }
.knob { padding: 8px 20px 4px; }
.knob .klab { display: flex; justify-content: space-between; font-size: 17px; font-weight: 600; }
.knob output { color: #62ff8f; font-family: Consolas, monospace; }
.sheet input[type=range] { height: 48px; }
.sheet input[type=range]::-webkit-slider-runnable-track { height: 8px; border-radius: 4px; }
.sheet input[type=range]::-webkit-slider-thumb { width: 32px; height: 32px; margin-top: -12px; border-radius: 50%;
  background: #eef0f3; box-shadow: 0 2px 6px rgba(0, 0, 0, .6); }
.sheet input[type=range]::-moz-range-track { height: 8px; border-radius: 4px; }
.sheet input[type=range]::-moz-range-thumb { width: 32px; height: 32px; border-radius: 50%; background: #eef0f3; }

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
  #now.long { font-size: 1.75rem; }
  /* The speaker line gets its own row in the display: the name first, the meter shrinks into what is left. */
  .right { flex: 1 1 100%; flex-direction: row; align-items: center; justify-content: space-between; gap: 1rem;
    min-height: 44px; margin: -.4rem -.8rem; padding: .4rem .8rem; }
  .spkline { flex: 0 0 auto; max-width: calc(100% - 7rem); }
  .sheet { inset: auto 0 0 0; margin: 0 auto; width: 100%; max-width: 560px; max-height: 80vh;
    border-radius: 20px 20px 0 0; transform-origin: bottom center; }
  .sheet[open] { animation-name: sheet-rise; animation-duration: .34s; animation-timing-function: cubic-bezier(.2, .9, .3, 1); }
  .sheet.closing { animation-name: sheet-sink; animation-duration: .22s; animation-timing-function: ease-in; }
  .meter { flex: 1 1 0; min-width: 6rem; max-width: 15rem; gap: .25rem; justify-content: flex-end; }
  .meter i { flex: 1 1 0; width: auto; max-width: .7rem; min-width: 2px; height: 2.4rem; }
  .pads, .extras { margin: 0; gap: 1rem; padding: 1.2rem; border-radius: 1.8rem; background: #111317;
    box-shadow: inset 0 .5rem 1.5rem rgba(0,0,0,.9), 0 .12rem 0 rgba(255,255,255,.12); }
  .grid { --g: 1rem; grid-template-columns: repeat(var(--cols), minmax(0, 1fr)); }
  .grid.few { --pw: calc((100cqi - (var(--cols) - 1) * var(--g)) / var(--cols) - .1px); }
  .hint { top: 1.8rem; right: 1.8rem; }
  .track { --gap: 2.4rem; align-items: flex-start; }   /* each page its own height: the page below follows */
  .tabs { justify-self: auto; display: grid; grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr); margin: 0; }
  .tab { min-width: 0; padding: 0 1rem; }
  /* Three tabs share a phone's width: tighter letters, and the type shrinks with the screen. */
  .tabs:has(> .tab:nth-child(3)) { gap: .5rem; }
  .tabs:has(> .tab:nth-child(3)) .tab { gap: .5rem; padding: 0 .4rem; letter-spacing: .1em;
    font-size: max(11px, min(1.5rem, 3.3vw)); }
  .tabs:has(> .tab:nth-child(3)) .tname { margin-right: -.1em; }
  .extras { display: grid; justify-content: center;   /* fixed-width tracks: a short last row sits centred */
    grid-template-columns: repeat(auto-fit, calc((100% - (var(--cols) - 1) * 1rem) / var(--cols))); }
  .extras .well { width: auto; height: auto; aspect-ratio: 1.1; }
  .extras .micwell { grid-column: span 2; aspect-ratio: 2.25; }
  .extras .micwell .pad { flex-direction: row; gap: 6cqmin; }
  .micwell .emo { font-size: 58cqmin; }
  .micwell .lbl { font-size: max(12px, 20cqmin); }
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
  .extras .micwell { aspect-ratio: 2.95; }
  .top { flex-wrap: nowrap; }
  .brand small { display: none; }
  .lcd { min-height: 5rem; }
  .sheet { max-height: 94vh; }
  .row { min-height: 48px; padding-block: 4px; }
}

/* Only where a mouse is: hover warmth and the keyboard hints. Nothing depends on hover. */
@media (hover: hover) and (pointer: fine) {
  .pad:hover, .stop:hover { filter: brightness(1.08); }
  .right:hover, .row:hover, .done:hover { background: rgba(255, 255, 255, .06); }
  .key { display: block; }
}
@media (prefers-reduced-motion: reduce) {
  .pad, .pad.down, .stop, .stop.down, .emo, .well, .track, .tab { transition: none; }
  .sheet[open], .sheet.closing, .well.onair, .hand { animation: none; }
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
      <span class="dly" id="dly" hidden></span>
      <button class="right" id="spkbtn" type="button" aria-haspopup="dialog" aria-label="Choose the speaker and mic">
        <span class="spkline"><span id="spk"></span><span class="chev" aria-hidden="true">&#9662;</span></span>
        <span class="meter" id="meter" aria-hidden="true"></span></button></div>
  </header>
  <div class="tabs" id="tabs" role="tablist" aria-label="Pages"></div>
  <section class="pads" id="pads" aria-label="Pads"><div class="track" id="track"></div>
    <div class="hint" id="hint" aria-hidden="true" hidden><span class="hand">&#128070;</span>SWIPE</div></section>
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
<dialog class="sheet" id="sheet" aria-labelledby="sheet-title">
  <div class="sheet-in">
    <div class="sheet-head"><h2 id="sheet-title">Speaker and mic</h2><button class="done" id="done" type="button">Done</button></div>
    <div class="list">
      <h3 class="group" id="spk-title">Play the radio on</h3>
      <div id="spklist" role="radiogroup" aria-labelledby="spk-title"></div>
      <h3 class="group" id="mic-title">Microphone</h3>
      <div id="miclist" role="radiogroup" aria-labelledby="mic-title"></div>
      <div id="knobs" hidden>
        <div class="knob"><div class="klab"><label for="gain">Mic loudness</label><output id="gainout" for="gain">30%</output></div>
          <input id="gain" type="range" min="0" max="100" step="5" value="30"></div>
        <div class="knob"><div class="klab"><label for="delay">Delay</label><output id="delayout" for="delay">350 ms</output></div>
          <input id="delay" type="range" min="0" max="2000" step="50" value="350"></div>
      </div>
    </div>
  </div>
</dialog>
<script>
const SOUNDS = __CONFIG__, PAGES = __PAGES__, EXTRAS = __EXTRAS__, TOKEN = "__TOKEN__";
const post = (path) => fetch(`${path}?t=${TOKEN}`, { method: "POST" }).catch(() => {});
const $ = (id) => document.getElementById(id);
const FLOW = matchMedia("(max-width: 899.98px), (max-height: 599.98px)");
const STILL = matchMedia("(prefers-reduced-motion: reduce)");
const now = $("now"), dly = $("dly"), IDLE = "PRESS A BUTTON \u2605", ONAIR = "\u25CF ON AIR";
let idle, live = false, swiping = null;   // swiping: the finger turning the page; the pad it started on stays quiet
function rest() {   // what the display says between messages; the delay readout belongs to ON AIR
  now.textContent = live ? ONAIR : IDLE;
  now.classList.remove("long");
  dly.hidden = !live;
}
function show(text) {
  now.textContent = text;
  now.classList.toggle("long", [...text].length > 16);
  dly.hidden = true;
  clearTimeout(idle);
  idle = setTimeout(rest, 2500);
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
    if (armed === e.pointerId && swiping !== e.pointerId) act();
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
  if (act) pressable(b, act);
  well.append(b);
  parent.append(well);
  return { well, b };
}

// Pages of pads side by side on a track. A sideways swipe, a page tab or the arrow keys turn the page, and the
// keys play the page you see (each page has the keyboard grid to itself). The display, MIC, STOP and volume are
// the same on every page.
const track = $("track"), tabs = $("tabs"), lit = {}, keyed = PAGES.map(() => Object.create(null));
let page = 0, toldNoVoice = false;
const grids = PAGES.map((p, k) => {
  const g = document.createElement("div"), t = document.createElement("button");
  g.className = p.few ? "grid few" : "grid";
  g.id = `page${k}`;
  g.setAttribute("role", "tabpanel");
  g.setAttribute("aria-labelledby", `tab${k}`);
  t.type = "button";
  t.className = "tab";
  t.id = `tab${k}`;
  t.setAttribute("role", "tab");
  t.setAttribute("aria-controls", g.id);
  t.append(span("led", ""), span("tname", p.name));
  t.firstChild.setAttribute("aria-hidden", "true");
  t.addEventListener("click", () => turn(k));
  track.append(g);
  tabs.append(t);
  return g;
});
const tabBtns = [...tabs.children];
tabs.hidden = PAGES.length < 2;
// A plugin's button drawn as a pad: a label that starts with an emoji uses it as the picture.
const PICTURE = /^(\p{Extended_Pictographic}(?:️|‍\p{Extended_Pictographic})*️?)\s*/u;
function look(x) {
  const m = x.label.match(PICTURE);
  return { emoji: m ? m[1] : "", name: (m && x.label.slice(m[0].length)) || x.label, t: "#fff",
    c: x.c, d: `color-mix(in srgb, ${x.c} 76%, #000)`, e: `color-mix(in srgb, ${x.c} 45%, #000)` };
}
SOUNDS.forEach((s, k) => {   // the pads, page by page: sounds, and plugin buttons that asked for a page
  if (s.button !== undefined) Object.assign(s, look(s));
  Object.assign(s, makePad(grids[s.page], s, () => fire(k)));
  if (s.key) keyed[s.page][s.key] = k;
});
let musicOn = false, musicTold = 0;
function fire(k) {
  const s = SOUNDS[k];
  if (s.button !== undefined) { post(`/button/${s.button}`); return show(s.label); }   // its reply comes by the poll
  post(`/play/${s.i}`);
  if (s.loop) {   // the music goes round until STOP, or the next press of its pad
    musicOn = !musicOn;
    musicTold = performance.now();
    s.well.classList.toggle("lit", musicOn);
    if (!musicOn) return show("MUSIC OFF");
  } else {
    s.well.classList.add("lit");
    clearTimeout(lit[k]);
    lit[k] = setTimeout(() => s.well.classList.remove("lit"), s.dur * 1000);
  }
  if (s.novoice && !toldNoVoice) { toldNoVoice = true; show("NO VOICE ON THIS PC"); }   // said once; the sound plays
  else show(`${s.emoji} ${s.name}`);
}
const step = () => track.offsetWidth + (parseFloat(getComputedStyle(track).columnGap) || 0);   // one page over
const at = () => new DOMMatrixReadOnly(getComputedStyle(track).transform).m41;   // where the track is right now
function slide(k, dx = 0) {   // page k's place, moved dx px by a finger
  track.style.transform = `translateX(calc(${-100 * k}% - ${k} * var(--gap) + ${dx}px))`;
}
function fit() {   // a phone's page ends where its pads end; a big screen keeps one height, so nothing moves
  track.style.height = FLOW.matches ? `${grids[page].offsetHeight}px` : "";
}
const hint = $("hint");   // the hand hint (CSS shows it on touch screens only)
try { hint.hidden = PAGES.length < 2 || localStorage.getItem("radio-swiped") === "1"; } catch (e) { hint.hidden = PAGES.length < 2; }
function turn(k, swiped = false) {
  k = Math.max(0, Math.min(PAGES.length - 1, k));
  const moved = k !== page;
  if (moved) hint.hidden = true;   // gone once the page turns; a swipe means it has been learned for good
  if (moved && swiped) try { localStorage.setItem("radio-swiped", "1"); } catch (e) {}
  page = k;
  track.style.transition = "";
  slide(k);
  grids.forEach((g, j) => { g.inert = j !== k; });
  tabBtns.forEach((t, j) => { t.setAttribute("aria-selected", String(j === k)); t.tabIndex = j === k ? 0 : -1; });
  fit();
  if (moved) show(`${PAGES[k].emoji} ${PAGES[k].name}`);
}

// A finger moving sideways turns the page. The pads follow it (a rubber band past the first and last page), and
// letting go settles on the next page once it is a quarter of the way there, or flicked; else back where it was.
// The pad it started on does not play. A mouse turns pages with the tabs and the arrow keys.
let drag = null;
const band = (d, w) => Math.sign(d) * (1 - 1 / (Math.abs(d) * 0.55 / w + 1)) * w;
track.addEventListener("pointerdown", (e) => {
  if (e.pointerType !== "mouse" && !drag && PAGES.length > 1)
    drag = { id: e.pointerId, x: e.clientX, y: e.clientY, on: false, v: 0, px: e.clientX, t: e.timeStamp };
}, true);
track.addEventListener("pointermove", (e) => {
  if (!drag || e.pointerId !== drag.id) return;
  if (!drag.on) {
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.abs(dx) < 10 || Math.abs(dx) < 1.2 * Math.abs(dy)) return;   // not a sideways swipe (yet)
    drag.on = true;
    swiping = drag.id;
    release();   // the pad under the finger comes back up, and its press is off
    drag.x = e.clientX;
    drag.base = at() + page * step();   // caught while settling: carry on from where it is
    track.style.transition = "none";
  }
  const w = step(), last = -(PAGES.length - 1) * w;
  let x = -page * w + drag.base + e.clientX - drag.x;
  x = x > 0 ? band(x, w) : x < last ? last + band(x - last, w) : x;
  slide(page, x + page * w);
  const dt = e.timeStamp - drag.t;
  if (dt > 0) drag.v = 0.7 * ((e.clientX - drag.px) / dt) + 0.3 * drag.v;   // px per ms
  drag.px = e.clientX;
  drag.t = e.timeStamp;
});
function letGo(e) {   // after the pad's own pointerup, which saw `swiping` and kept quiet
  if (!drag || e.pointerId !== drag.id) return;
  const d = drag;
  drag = swiping = null;
  if (!d.on) return;
  if (e.type === "pointercancel") return turn(page);
  const w = step(), moved = at() + page * w, v = e.timeStamp - d.t > 100 ? 0 : d.v;   // held still before letting go
  turn(moved < -w / 4 || (v < -0.3 && moved < 0) ? page + 1 : moved > w / 4 || (v > 0.3 && moved > 0) ? page - 1 : page, true);
}
track.addEventListener("pointerup", letGo);
track.addEventListener("pointercancel", letGo);
turn(0);
const resized = new ResizeObserver(() => fit());
grids.forEach((g) => resized.observe(g));
FLOW.addEventListener("change", fit);

// The MIC pad: the PC's microphone is on the air while the radio runs, out of the radio's own speaker, the
// sheet's delay later. A tap, click, Enter or M mutes it, and the same again puts it back on the air. Any page
// can press it, and every page shows it: the state comes back from the PC with each poll.
const MUTED = { c: "#c3c8cf", d: "#848b95", e: "#41464d", t: "#1d2025" }, AIR = { c: "#ff7a6b", d: "#d8261f", e: "#7d0b0b", t: "#fff" };
const MIKE = "\u{1F399}\uFE0F", HUSHED = "\u{1F507}";
const mic = makePad($("extras"), { key: "m", emoji: MIKE, name: "ON AIR", ...AIR }, () => setMic(!live));
mic.well.classList.add("micwell");
mic.b.classList.add("mic");
const lcd = document.querySelector(".lcd");
let micState = "live", micTold = 0, delayMs = 350;
function paintMic() {
  const on = micState === "live" || micState === "hush", none = micState === "none";
  for (const [k, v] of Object.entries(on ? AIR : MUTED)) mic.b.style.setProperty(`--${k}`, v);
  mic.well.classList.toggle("onair", on);
  mic.b.querySelector(".emo").textContent = on || none ? MIKE : HUSHED;
  mic.b.querySelector(".lbl").textContent = none ? "NO MIC" : on ? "ON AIR" : "MUTED";
  mic.b.setAttribute("aria-label", none ? "No microphone" : on ? "Microphone on the air. Mute" : "Microphone muted. Put it on the air");
  mic.b.setAttribute("aria-disabled", String(none));
  lcd.classList.toggle("onair", on);
  dly.textContent = delayMs ? `DELAY ${delayMs} MS` : "NO DELAY";
  if (on !== live) { live = on; clearTimeout(idle); rest(); }
}
function setMic(on) {
  if (micState === "none" || on === live) return;
  micState = on ? "live" : "off";
  micTold = performance.now();
  post(`/onair/${on ? "on" : "off"}`);
  paintMic();
}
paintMic();

// Plugin buttons (plugins/*.py) that name no page sit in the row under the pads, beside the MIC.
EXTRAS.forEach((x) => makePad($("extras"), look(x), () => { post(`/button/${x.k}`); show(x.label); }));

// A one-word label too wide for its pad (COUNTDOWN on a narrow phone) gets a smaller font until it fits, never
// below 9px; a label that fits keeps the usual size.
function fitLabels() {
  document.querySelectorAll(".well:not(.micwell) .lbl").forEach((l) => {
    l.style.fontSize = "";
    for (let fs = parseFloat(getComputedStyle(l).fontSize); l.scrollWidth > l.clientWidth + 0.5 && fs > 9;) {
      fs = Math.max(9, fs - 0.5);
      l.style.fontSize = `${fs}px`;
    }
  });
}
fitLabels();
document.fonts.ready.then(fitLabels);
addEventListener("resize", fitLabels);

function stopAll() {
  post("/stop");
  SOUNDS.forEach((s, i) => { clearTimeout(lit[i]); s.well.classList.remove("lit"); });
  musicOn = false;
  musicTold = performance.now();
  show("✋ STOP");
}
const stop = $("stop");
pressable(stop, stopAll, true);
const release = () => document.querySelectorAll(".down").forEach((el) => el.classList.remove("down"));
addEventListener("pointerup", (e) => { if (e.pointerType === "mouse") release(); });   // let go somewhere else
addEventListener("blur", release);
addEventListener("contextmenu", (e) => e.preventDefault());   // no long-press menu under small fingers

// The speaker sheet: tap the speaker name under the display to choose where the sound plays.
const sheet = $("sheet"), spklist = $("spklist"), spkbtn = $("spkbtn");
function note(text, warn = false) {
  const p = document.createElement("p");
  p.className = warn ? "note warn" : "note";
  p.textContent = text;
  return p;
}
function choice(list, route, icon, title, sub, checked, pick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "row";
  b.setAttribute("role", "radio");
  b.setAttribute("aria-checked", String(checked));
  const txt = span("txt", "");
  txt.append(Object.assign(document.createElement("b"), { textContent: title }));
  if (sub) txt.append(Object.assign(document.createElement("small"), { textContent: sub }));
  const tick = span("tick", "\u2713");
  tick.setAttribute("aria-hidden", "true");
  b.append(txt, tick);
  b.addEventListener("click", () => {
    list.querySelectorAll(".row").forEach((r) => r.setAttribute("aria-checked", String(r === b)));
    post(`/${route}/${pick}`);
    show(`${icon} ${title}`);
  });
  return b;
}
async function openSheet() {
  if (sheet.open) return;
  if (!FLOW.matches) {   // the card hangs from the speaker line it came from
    const r = spkbtn.getBoundingClientRect();
    sheet.style.setProperty("--top", `${Math.round(r.bottom + 8)}px`);
    sheet.style.setProperty("--right", `${Math.max(16, Math.round(innerWidth - r.right))}px`);
  }
  spklist.replaceChildren(note("Looking for speakers\u2026"));
  miclist.replaceChildren();
  knobs.hidden = true;
  sheet.showModal();
  try {
    const SPK = "\u{1F50A}", MIK = "\u{1F399}\uFE0F";
    const r = await (await fetch(`/speakers?t=${TOKEN}`)).json();
    const rows = [choice(spklist, "speaker", SPK, "Windows default", r.default ? `Now: ${r.default}` : "The speaker Windows uses", r.follow, "default"),
      ...r.speakers.map((s) => choice(spklist, "speaker", SPK, s.short, s.name !== s.short ? s.name : "", s.picked, s.index))];
    spklist.replaceChildren(...rows);
    if (r.missing) spklist.append(note(`${r.pinned} is not on right now. The radio plays on Windows default until it is back.`, true));
    (spklist.querySelector('[aria-checked="true"]') || rows[0]).focus({ preventScroll: true });
    const m = await (await fetch(`/mics?t=${TOKEN}`)).json();
    if (!m.mics.length) return miclist.replaceChildren(note("No microphone found. Plug one in, then open this again.", true));
    miclist.replaceChildren(choice(miclist, "mic", MIK, "Windows default", m.default ? `Now: ${m.default}` : "The mic Windows uses", m.follow, "default"),
      ...m.mics.map((s) => choice(miclist, "mic", MIK, s.short, s.name !== s.short ? s.name : "", s.picked, s.index)));
    if (m.missing) miclist.append(note(`${m.pinned} is not plugged in. The radio uses Windows default until it is back.`, true));
    gain.value = m.gain;
    delayIn.value = m.delay;
    paintKnobs();
    knobs.hidden = false;
  } catch (e) {
    spklist.replaceChildren(note("Can't reach the radio. Is it still open on the PC?", true));
  }
}
function closeSheet() {
  if (!sheet.open || sheet.classList.contains("closing")) return;
  const done = () => { if (sheet.open) sheet.close(); sheet.classList.remove("closing"); };
  if (STILL.matches) return done();
  sheet.classList.add("closing");   // leave the way it came in
  sheet.addEventListener("animationend", done, { once: true });
  setTimeout(done, 400);
}
spkbtn.addEventListener("click", openSheet);
const miclist = $("miclist"), knobs = $("knobs"), gain = $("gain"), delayIn = $("delay");
function paintKnobs() {
  $("gainout").textContent = `${gain.value}%`;
  $("delayout").textContent = `${delayIn.value} ms`;
}
gain.addEventListener("input", () => { paintKnobs(); post(`/micgain/${gain.value}`); });
delayIn.addEventListener("input", () => {
  paintKnobs();
  post(`/micdelay/${delayIn.value}`);
  delayMs = +delayIn.value;
  paintMic();
});
$("done").addEventListener("click", closeSheet);
sheet.addEventListener("cancel", (e) => { e.preventDefault(); closeSheet(); });   // Esc
sheet.addEventListener("click", (e) => { if (e.target === sheet) closeSheet(); });   // a tap outside the sheet

addEventListener("keydown", (e) => {
  if (sheet.open || e.repeat || e.ctrlKey || e.metaKey || e.altKey) return;   // keys belong to the sheet while it is up
  const k = e.key.toLowerCase();
  if (k === " " || k === "escape") { e.preventDefault(); stop.classList.add("down"); stopAll(); return; }
  if (k === "m" && e.target !== vol) { mic.b.classList.add("down"); setMic(!live); return; }   // mute / on the air
  if ((k === "arrowleft" || k === "arrowright") && e.target !== vol) {   // the volume fader keeps its own arrows
    e.preventDefault();
    turn(page + (k === "arrowright" ? 1 : -1));
    if (tabBtns.includes(document.activeElement)) tabBtns[page].focus();
    return;
  }
  const i = keyed[page][k];
  if (i !== undefined && e.target !== vol) { SOUNDS[i].b.classList.add("down"); fire(i); }
});
addEventListener("keyup", (e) => {
  if (sheet.open) return;
  const k = e.key.toLowerCase();
  keyed.forEach((keys) => { if (keys[k] !== undefined) SOUNDS[keys[k]].b.classList.remove("down"); });
  if (k === " " || k === "escape") stop.classList.remove("down");
  if (k === "m") mic.b.classList.remove("down");
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
    const { level, speaker, volume, msg, mic: m, micLevel, delay, music } = await (await fetch("/level")).json();
    if (!synced) { vol.value = volume; volout.textContent = `${volume}%`; synced = true; }   // a reload keeps the app's volume
    if (seen >= 0 && msg[0] !== seen) show(msg[1]);   // a plugin button's reply, e.g. "No microphone"
    seen = msg[0];
    if (speaker !== speakerShown) {   // already short; the sheet has the full names
      speakerShown = speaker;
      spk.textContent = "\u{1F50A} " + speaker;
      spkbtn.setAttribute("aria-label", `Speaker: ${speaker}. Choose the speaker and mic`);
    }
    if (delay !== delayMs) { delayMs = delay; paintMic(); }
    if (music !== musicOn && performance.now() - musicTold > 600) {   // stopped (or started) from another page
      musicOn = music;
      SOUNDS.forEach((s) => { if (s.loop) s.well.classList.toggle("lit", musicOn); });
    }
    if (m !== micState && performance.now() - micTold > 600) {   // a fresh press wins over a poll already on its way
      if (m === "hush" && micState === "live") show("TOO LOUD, MOVE THE MIC");
      micState = m;
      paintMic();
    }
    const lv = live ? Math.max(level, micLevel) : level;   // on the air: the voice moves the meter as well as the pads
    shown = Math.max(Math.round(Math.min(1, Math.sqrt(lv) * 1.6) * segs.length), shown - 1);
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
