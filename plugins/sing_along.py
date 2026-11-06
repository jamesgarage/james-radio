"""Sing-along: each press plays the next nursery tune on a music box, with a soft bass under it.

The tunes are old, public domain melodies (notes only, no words): Twinkle Twinkle Little Star, Old MacDonald Had
a Farm, Row, Row, Row Your Boat and Happy Birthday. The whole tune is made first and played as one sound, so STOP
stops it like any pad. A press while a tune is playing is ignored.
"""
import time

SR = 48000
TOP = 4000.0   # no harmonic above this: the same ceiling as the radio's tones
RING = 1.2     # seconds the last note is left to ring

# (what the screen says, seconds per beat, the tune and the bass: (MIDI note or None for a rest, beats) in order;
# 60 is middle C). The bass is as long as the tune.
TUNES = [
    ("🎵 Twinkle twinkle", 0.5,
     [(72, 1), (72, 1), (79, 1), (79, 1), (81, 1), (81, 1), (79, 2),
      (77, 1), (77, 1), (76, 1), (76, 1), (74, 1), (74, 1), (72, 2)],
     [(48, 2), (48, 2), (53, 2), (48, 2), (53, 2), (48, 2), (55, 2), (48, 2)]),
    ("🎵 Old MacDonald", 0.4,
     [(72, 1), (72, 1), (72, 1), (67, 1), (69, 1), (69, 1), (67, 2),
      (76, 1), (76, 1), (74, 1), (74, 1), (72, 3), (67, 1),
      (72, 1), (72, 1), (72, 1), (67, 1), (69, 1), (69, 1), (67, 2),
      (76, 1), (76, 1), (74, 1), (74, 1), (72, 4)],
     [(48, 4), (53, 2), (48, 2), (48, 2), (55, 2), (48, 4)] * 2),
    ("🎵 Row your boat", 0.2,   # a beat here is an eighth note: six to a bar
     [(72, 3), (72, 3), (72, 2), (74, 1), (76, 3), (76, 2), (74, 1), (76, 2), (77, 1), (79, 6),
      (84, 1), (84, 1), (84, 1), (79, 1), (79, 1), (79, 1), (76, 1), (76, 1), (76, 1), (72, 1), (72, 1), (72, 1),
      (79, 2), (77, 1), (76, 2), (74, 1), (72, 6)],
     [(48, 3), (55, 3)] * 6 + [(43, 3), (50, 3), (48, 6)]),
    ("🎵 Happy birthday", 0.45,
     [(67, 0.75), (67, 0.25), (69, 1), (67, 1), (72, 1), (71, 2),
      (67, 0.75), (67, 0.25), (69, 1), (67, 1), (74, 1), (72, 2),
      (67, 0.75), (67, 0.25), (79, 1), (76, 1), (72, 1), (71, 1), (69, 1),
      (77, 0.75), (77, 0.25), (76, 1), (72, 1), (74, 1), (72, 3)],
     [(None, 1), (48, 3), (43, 3), (43, 3), (48, 3), (48, 3), (41, 3), (48, 2), (43, 1), (48, 3)]),
]


def hz(midi):
    return 440 * 2 ** ((midi - 69) / 12)


def seconds(tune):
    """How long a tune plays, the last note's ring included."""
    _, beat, melody, _ = tune
    return beat * sum(b for _, b in melody) + RING


def box(np, freq, sec, decay, partials):
    """One music-box note: struck, then dying away. partials: (harmonic, loudness)."""
    x = np.arange(int(SR * sec)) / SR
    s = sum(a * np.sin(2 * np.pi * k * freq * x) * np.exp(-x * decay * k) for k, a in partials if k * freq < TOP)
    edge = int(SR * 0.004)
    s[:edge] *= np.linspace(0, 1, edge)        # no click at the strike...
    s[-int(SR * 0.03):] *= np.linspace(1, 0, int(SR * 0.03))   # ...or where the note is let go
    return s


def render(tune):
    """The whole tune as 48 kHz mono samples (float32): the melody on a music box and the bass a little softer."""
    import numpy as np   # imported on the first press: the radio always has numpy, a plain test run may not
    _, beat, melody, bass = tune
    out = np.zeros(int(SR * seconds(tune)))
    made = {}   # a tune uses a few notes many times: each is made once
    for notes, loud, decay, partials in ((melody, 1.0, 2.5, ((1, 1.0), (2, 0.3), (3, 0.1))),
                                         (bass, 0.45, 1.8, ((1, 1.0), (2, 0.2)))):
        at = 0.0
        for midi, beats in notes:
            if midi is not None:
                key = (midi, beats, loud)
                if key not in made:   # it rings on into the next note
                    made[key] = box(np, hz(midi), beats * beat + 0.5, decay, partials) * loud
                note = made[key]
                i = int(SR * at)
                n = min(len(note), len(out) - i)
                out[i:i + n] += note[:n]
            at += beats * beat
    return (out / np.max(np.abs(out))).astype(np.float32)


def register(radio):
    state = {"next": 0, "until": 0.0}
    made = {}

    def sing():
        if time.monotonic() < state["until"]:
            return None                      # a tune is playing: ignored
        k = state["next"]
        state["next"] = (k + 1) % len(TUNES)
        if k not in made:
            made[k] = render(TUNES[k])
        state["until"] = time.monotonic() + seconds(TUNES[k]) - RING / 2
        radio.play(made[k])                  # one sound: never louder than the pads, and STOP stops it
        return TUNES[k][0]

    def stopped():
        state["until"] = 0.0                 # STOP stopped the tune: the next press plays the next one at once

    radio.add_button("🎶 Sing-along", "#0e9488", sing, page="Play", on_stop=stopped)
