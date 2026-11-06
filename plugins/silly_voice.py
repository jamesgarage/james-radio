"""Silly voice: a beep-boop says "talk now", the microphone records three seconds, and the radio plays it back
higher and quicker, like a chipmunk. Played after recording, never live. STOP ends it at any point."""
import threading

SR = 48000
TALK_SECONDS = 3
SPEED = 1.6   # played back 1.6 times as fast: about eight notes higher, and over in under two seconds


def chipmunk(samples, speed=SPEED):
    """The recording sped up (higher and shorter), with a short fade at each end so it starts and ends softly."""
    x = [float(v) for v in samples]
    n = int((len(x) - 1) / speed) if len(x) > 1 else 0
    out = []
    for i in range(n):
        p = i * speed
        j = int(p)
        out.append(x[j] + (x[j + 1] - x[j]) * (p - j))
    edge = min(n // 2, SR // 100)
    for k in range(edge):
        out[k] *= k / edge
        out[n - 1 - k] *= k / edge
    return out


def register(radio):
    stopped = threading.Event()   # STOP: every wait below ends at once and nothing more plays

    def silly():
        stopped.clear()
        if radio.record(0) is None:
            return "No microphone"            # say so and do nothing else
        radio.tone(660, 0.12)                 # beep-boop: your turn
        if stopped.wait(0.15):
            return None
        radio.tone(990, 0.15)
        if stopped.wait(0.35):
            return None
        voice = radio.record(TALK_SECONDS)    # STOP ends this early too
        if stopped.is_set():
            return None
        if voice is None:
            return "No microphone"
        squeak = chipmunk(voice)
        if not squeak:
            return "No microphone"
        if stopped.wait(0.2):
            return None
        radio.play(squeak)                    # the radio levels it: never louder than the pads
        if stopped.wait(len(squeak) / SR):    # one at a time: the button is busy until it has played
            return None
        return "🐿️ That was silly!"

    radio.add_button("🐿️ Silly voice", "#d9468a", silly, page="Play", on_stop=stopped.set)
