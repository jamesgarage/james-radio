"""Animals: a pad for each animal on the PLAY page, each sound made with math (no recordings).

Every sound is a voice built from harmonics: a pitch that moves the way the animal's does, and the mouth shapes
(formants) a moo or a meow goes through. Each is made the first time its pad is pressed and kept after that.
There is no dog: no bark made this way sounded like one.
"""
SR = 48000
TOP = 4000.0   # no harmonic above this: the same ceiling as the radio's tones


def _np():
    import numpy   # imported on the first press: the radio always has numpy, a plain test run may not
    return numpy


def curve(np, t, points):
    """A value that moves through (seconds, value) points in straight lines."""
    at, values = zip(*points)
    return np.interp(t, at, values)


def voiced(np, f0, formants, tilt=1.0):
    """Harmonics of the pitch f0 (Hz, one per sample) made louder near each formant (centre Hz, width Hz, gain);
    centres may move (one per sample). tilt: how fast the harmonics fade, higher is darker."""
    n = len(f0)
    phase = 2 * np.pi * np.cumsum(f0) / SR
    slow = np.arange(0, n + 47, 48).clip(max=n - 1)   # the mouth moves slowly: its shape is worked out each ms
    f0s, where = f0[slow], np.arange(n)
    formants = [(c[slow] if np.ndim(c) else c, w, g) for c, w, g in formants]
    out = np.zeros(n)
    for k in range(1, int(TOP / max(float(np.min(f0)), 40.0)) + 1):
        fk = k * f0s
        shape = sum(g / np.sqrt(1 + ((fk - c) / (w / 2)) ** 2) for c, w, g in formants)
        out += np.interp(where, slow, np.where(fk < TOP, shape, 0.0)) / k ** tilt * np.sin(k * phase)
    return out


def noise(np, n, lo, hi, seed):
    """Breath: white noise kept between lo and hi Hz."""
    spec = np.fft.rfft(np.random.default_rng(seed).standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / SR)
    spec[(f < lo) | (f > hi)] = 0
    s = np.fft.irfft(spec, n)
    return s / (np.max(np.abs(s)) or 1.0)


def breath(np, f1, f2, seed):
    """Breath through the mouth: noise shaped by the same two moving formants as the voice, 20 ms at a time."""
    n, hop = len(f1), 480
    s, out, win = np.random.default_rng(seed).standard_normal(n), np.zeros(n), np.hanning(2 * hop)
    f = np.fft.rfftfreq(2 * hop, 1 / SR)
    for i in range(0, n - 2 * hop, hop):
        g = 1 / np.sqrt(1 + ((f - f1[i + hop]) / 150) ** 2) + 0.7 / np.sqrt(1 + ((f - f2[i + hop]) / 200) ** 2)
        out[i:i + 2 * hop] += np.fft.irfft(np.fft.rfft(s[i:i + 2 * hop] * win) * g * (f < TOP), 2 * hop)
    return out / (np.max(np.abs(out)) or 1.0)


def wobble(np, t, amount, seed, rate=25):
    """A slow random wobble around 1: a real voice is never perfectly smooth (a smooth one sounds like a whistle)."""
    k = np.random.default_rng(seed).standard_normal(int(len(t) / SR * rate) + 2)
    return 1 + amount * np.interp(t * rate, np.arange(len(k)), k)


def place(out, snd, at):
    i = int(SR * at)
    n = min(len(snd), len(out) - i)
    out[i:i + n] += snd[:n]


def times(np, seconds):
    return np.arange(int(SR * seconds)) / SR


def cow(np):
    """Mmm-OOO: a low voice that opens from a hum into a long moo and sinks at the end."""
    t = times(np, 1.6)
    f0 = curve(np, t, [(0, 92), (0.25, 118), (0.9, 112), (1.35, 100), (1.6, 82)])
    f0 = f0 * (1 + 0.012 * np.sin(2 * np.pi * 5.5 * t))   # a slow wobble in the voice
    f1 = curve(np, t, [(0, 250), (0.2, 420), (0.5, 480), (1.6, 380)])
    f2 = curve(np, t, [(0, 900), (0.2, 850), (1.6, 800)])
    s = voiced(np, f0, [(f1, 160, 1.0), (f2, 200, 0.5), (2500, 400, 0.08)])
    return s * curve(np, t, [(0, 0), (0.15, 0.35), (0.35, 1), (1.2, 0.9), (1.6, 0)])


def duck(np):
    """Quack, quack, quack: short, buzzy and through the nose, each a little lower."""
    out = np.zeros(int(SR * 1.0))
    for at, drop in ((0, 1.0), (0.32, 0.97), (0.62, 0.92)):
        t = times(np, 0.22)
        f0 = curve(np, t, [(0, 210), (0.04, 250), (0.15, 230), (0.22, 180)]) * drop
        f1 = curve(np, t, [(0, 500), (0.04, 900), (0.22, 700)])
        f2 = curve(np, t, [(0, 1100), (0.04, 1500), (0.22, 1300)])
        s = voiced(np, f0, [(f1, 120, 1.0), (f2, 150, 0.9), (2600, 200, 0.5)], tilt=0.3)
        place(out, s * curve(np, t, [(0, 0), (0.02, 1), (0.15, 0.9), (0.22, 0)]), at)
    return out


def cat(np):
    """Mi-ew: a kitten's meow. The pitch climbs and falls, the mouth opens and closes, and there is breath in it."""
    t = times(np, 0.6)
    f0 = curve(np, t, [(0, 650), (0.08, 850), (0.2, 1000), (0.42, 850), (0.6, 620)]) * wobble(np, t, 0.02, 11)
    f1 = curve(np, t, [(0, 300), (0.06, 650), (0.2, 1050), (0.42, 800), (0.6, 450)])
    f2 = curve(np, t, [(0, 1700), (0.06, 2400), (0.2, 1900), (0.42, 1300), (0.6, 950)])
    s = voiced(np, f0, [(f1, 260, 1.0), (f2, 300, 0.9), (3400, 400, 0.4)], tilt=0.2)
    s = s / np.max(np.abs(s)) + 0.4 * breath(np, f1, f2, 12)
    return s * curve(np, t, [(0, 0), (0.04, 0.35), (0.1, 1), (0.42, 0.75), (0.6, 0)])


def owl(np):
    """Hoo, hoo-hoo, hooo: soft round notes, nearly pure, with a little breath."""
    out = np.zeros(int(SR * 1.7))
    for at, sec, f, end in ((0, 0.32, 400, 380), (0.55, 0.16, 410, 400), (0.78, 0.16, 410, 400), (1.0, 0.55, 405, 360)):
        t = times(np, sec)
        f0 = curve(np, t, [(0, f * 0.96), (sec * 0.2, f), (sec, end)])
        phase = 2 * np.pi * np.cumsum(f0) / SR
        s = np.sin(phase) + 0.12 * np.sin(2 * phase) + 0.04 * noise(np, len(t), 200, 1200, seed=7)
        place(out, s * curve(np, t, [(0, 0), (0.05, 1), (sec - 0.12, 0.85), (sec, 0)]), at)
    return out


# (label for the pad, colour, what the screen says, the sound)
ANIMALS = [("🐄 Cow", "#2e9e4f", "🐄 MOO!", cow),
           ("🦆 Duck", "#1f86d6", "🦆 QUACK!", duck),
           ("🐱 Cat", "#e0572e", "🐱 MEOW!", cat),
           ("🦉 Owl", "#6b4fd8", "🦉 HOO HOO!", owl)]


def make(name):
    """An animal's sound by its pad label, as 48 kHz mono samples (float32)."""
    np = _np()
    [sound] = [s for label, _, _, s in ANIMALS if label == name]
    s = sound(np)
    edge = int(SR * 0.005)   # no click at either end
    s[:edge] *= np.linspace(0, 1, edge)
    s[-edge:] *= np.linspace(1, 0, edge)
    return (s / np.max(np.abs(s))).astype(np.float32)


def register(radio):
    made = {}

    def pad(label, reply):
        def press():
            if label not in made:
                made[label] = make(label)
            radio.play(made[label])   # never louder than the pads; presses overlap like the pads'
            return reply
        return press

    for label, color, reply, _ in ANIMALS:
        radio.add_button(label, color, pad(label, reply), page="Play")
