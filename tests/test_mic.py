"""MIC pad tests: the ring between the mic's stream and the speaker's, the delay, the howl guard and the page's
mic routes, with the fake sounddevice from test_speakers: no real device is opened and nothing makes a sound.

The mixing is numpy, so these need it:  uv run --with numpy python -m unittest discover -s tests
Under plain  py -3 -m unittest discover -s tests  they are skipped when numpy is not installed.
"""
import json
import re
import threading
import time
import unittest
import urllib.error
import urllib.request

from test_speakers import SD, World, plugins, radio

try:
    import numpy as np
    np.zeros   # the stand-in test_speakers uses when numpy is missing has no zeros
    HAVE_NUMPY = True
except (ImportError, AttributeError):
    HAVE_NUMPY = False
needs_numpy = unittest.skipUnless(HAVE_NUMPY, "needs numpy: uv run --with numpy python -m unittest discover -s tests")

SR, BLOCK = 48000, 480
MICS = ["Headset Microphone (Sam’s Earbuds)", "Microphone (3- USB Advanced Audio Device)"]


def run(mixer, seconds, mic=lambda at, n: np.zeros(n), pad=None):
    """Drive both callbacks block by block, the mic's first, the way the two streams take turns; the left channel."""
    stream, out = SD.mics[-1], []
    if pad is not None:
        mixer.voices.append((np.asarray(pad, np.float32), 0))
    for k in range(int(seconds * SR) // BLOCK):
        if stream.active:
            stream.callback(np.asarray(mic(k * BLOCK, BLOCK), np.float32).reshape(-1, 1), BLOCK, None, None)
        block = np.zeros((BLOCK, 2), np.float32)
        mixer._mix(block, BLOCK, None, None)
        out.append(block[:, 0].copy())
    return np.concatenate(out)


def click_at(t):
    return lambda at, n: np.where(np.arange(at, at + n) == t, 1.0, 0.0)


@needs_numpy
class RingBuffer(unittest.TestCase):
    def test_in_order_across_the_wrap(self):
        r = radio.Ring(8)
        r.write([1, 2, 3, 4, 5, 6])
        self.assertEqual(r.read(4).tolist(), [1, 2, 3, 4])
        r.write([7, 8, 9, 10])
        self.assertEqual(r.read(6).tolist(), [5, 6, 7, 8, 9, 10])

    def test_overflow_keeps_the_newest(self):
        r = radio.Ring(8)
        r.write(range(1, 13))
        self.assertEqual(r.read(8).tolist(), list(range(5, 13)))

    def test_running_dry_plays_silence_then_waits_for_a_little_backlog(self):
        r = radio.Ring(64, prime=8)
        r.write([1] * 4)
        self.assertEqual(r.read(4).tolist(), [0] * 4)      # not enough yet: nothing taken
        r.write([2] * 4)
        self.assertEqual(r.read(4).tolist(), [1] * 4)
        self.assertEqual(r.read(8).tolist(), [2] * 4 + [0] * 4)
        r.write([3] * 4)
        self.assertEqual(r.read(4).tolist(), [0] * 4)      # dry again: waits for 8

    def test_a_backlog_is_skipped_so_the_voice_never_lags(self):
        r = radio.Ring(64, slack=4)
        r.write(range(20))
        self.assertEqual(r.read(4).tolist(), [16, 17, 18, 19])

    def test_the_delay_is_silence_in_front(self):
        r = radio.Ring(64, delay=6)
        r.write([5, 5])
        self.assertEqual(r.read(8).tolist(), [0] * 6 + [5, 5])


@needs_numpy
class Delay(unittest.TestCase):
    def live(self, delay_ms):
        w = World(mics=MICS, mic_default=1)
        w.mixer.set_delay(delay_ms)
        self.assertTrue(w.mixer.onair(True))
        return w.mixer

    def landing(self, mixer, t, seconds=1.0):
        out = run(mixer, seconds, click_at(t))
        self.assertGreater(np.max(np.abs(out)), 0.1, "the click never came out")
        return int(np.argmax(np.abs(out)))

    def test_a_click_lands_after_the_delay(self):
        for ms in (350, 0, 1000, 2000):
            with self.subTest(ms=ms):
                mixer, t = self.live(ms), 4800
                self.assertLess(abs(self.landing(mixer, t, ms / 1000 + 0.5) - (t + ms * SR // 1000)), BLOCK)

    def test_the_default_is_350_ms(self):
        w = World(mics=MICS, mic_default=1)
        w.mixer.onair(True)
        self.assertLess(abs(self.landing(w.mixer, 4800) - (4800 + 16800)), BLOCK)

    def test_a_new_delay_takes_effect_at_once(self):
        mixer = self.live(0)
        run(mixer, 0.2)
        mixer.set_delay(100)   # mid-stream
        out = run(mixer, 0.6, click_at(9600))
        self.assertLess(abs(int(np.argmax(np.abs(out))) - (9600 + 4800)), BLOCK)

    def test_out_of_range_delays_are_held_to_0_to_2000_ms(self):
        mixer = self.live(350)
        mixer.set_delay(5000)
        self.assertEqual(mixer.mic_delay, 2.0)
        mixer.set_delay(-20)
        self.assertEqual(mixer.mic_delay, 0.0)


@needs_numpy
class Mixing(unittest.TestCase):
    def test_the_voice_joins_the_pads_in_the_speakers_own_stream(self):
        w = World(mics=MICS, mic_default=1)
        speakers = list(SD.opened)
        w.mixer.set_delay(0)
        w.mixer.onair(True)
        self.assertEqual(SD.opened, speakers, "the mic must not open a second speaker stream")
        self.assertEqual(w.mic()[1], MICS[1])                         # Windows' default mic
        out = run(w.mixer, 0.05, lambda at, n: np.full(n, 0.5), pad=np.full(SR, 0.1))
        np.testing.assert_allclose(out, 0.1 + 0.5 * 0.3, atol=1e-6)   # gain 0.3, below the limiter's knee

    def test_gain(self):
        w = World(mics=MICS, mic_default=1)
        w.mixer.set_delay(0)
        w.mixer.mic_gain = 1.0
        w.mixer.onair(True)
        np.testing.assert_allclose(run(w.mixer, 0.05, lambda at, n: np.full(n, 0.5)), 0.5, atol=1e-6)

    def test_off_closes_the_mic_and_the_voice_stops(self):
        w = World(mics=MICS, mic_default=1)
        w.mixer.set_delay(0)
        w.mixer.onair(True)
        stream = w.mic()[0]
        w.mixer.onair(False)
        self.assertFalse(stream.active)
        self.assertEqual(w.mixer.mic_state(), "off")
        block = np.ones((BLOCK, 2), np.float32)
        w.mixer._mix(block, BLOCK, None, None)
        self.assertEqual(float(np.max(np.abs(block))), 0.0)

    def test_a_rescan_keeps_a_live_mic_live(self):
        w = World(mics=MICS, mic_default=1)
        w.mixer.onair(True)
        first, scans = w.mic()[0], SD.scans
        w.mixer.stream.active = False   # the speaker's stream died: the re-scan closes every stream
        w.mixer._tick()
        self.assertEqual(SD.scans, scans + 1)
        self.assertFalse(first.active)
        self.assertTrue(w.mic()[0].active and w.mic()[0] is not first)
        self.assertEqual(w.mixer.mic_state(), "live")

    def test_a_new_default_speaker_takes_the_mic_along(self):
        w = World(mics=MICS, mic_default=1)
        w.mixer.onair(True)               # always on the air, so speaker changes cannot wait for it to stop
        first = w.mic()[0]
        w.set_default(0)
        w.mixer._tick()
        self.assertEqual(w.playing(), "Speakers (SoundWire Speakers)")
        self.assertTrue(w.mic()[0].active and w.mic()[0] is not first)
        self.assertEqual(w.mixer.mic_state(), "live")

    def test_a_new_default_mic_is_followed(self):
        w = World(mics=MICS, mic_default=1)
        w.mixer.onair(True)
        w.set_mic_default(0)
        w.mixer._tick()
        self.assertEqual((w.mic()[1], w.mixer.mic_state()), (MICS[0], "live"))

    def test_nothing_but_a_press_or_closing_takes_it_off_the_air(self):
        w = World(mics=MICS, mic_default=1)
        w.mixer.onair(True)
        for _ in range(3):
            w.mixer._tick()
        self.assertEqual(w.mixer.mic_state(), "live")
        w.mixer.close()
        self.assertFalse(w.mic()[0].active)
        self.assertEqual(w.mixer.mic_state(), "off")

    def test_a_mic_plugged_in_later_goes_on_the_air(self):
        w = World()
        self.assertEqual(w.mixer.mic_state(), "none")
        self.assertFalse(w.mixer.onair(True))   # what the radio asks for at start
        self.assertEqual(SD.mics, [])
        w.mic_idx = [SD.add(MICS[1], outputs=0, inputs=1)]
        w.set_mic_default(0)
        w.mixer._tick()
        self.assertEqual(w.mixer.mic_state(), "live")

    def test_a_muted_mic_stays_muted(self):
        w = World(mics=MICS, mic_default=1)
        w.mixer.onair(True)
        w.mixer.onair(False)
        w.set_mic_default(0)                    # even when Windows' default mic changes
        w.mixer._tick()
        self.assertEqual(w.mixer.mic_state(), "off")
        self.assertFalse(any(m.active for m in SD.mics))


@needs_numpy
class HowlGuard(unittest.TestCase):
    def live(self, gain=0.3, volume=1.0):
        w = World(mics=MICS, mic_default=1)
        w.mixer.set_delay(0)
        w.mixer.mic_gain, w.mixer.volume = gain, volume
        w.mixer.onair(True)
        return w.mixer

    @staticmethod
    def howl(at, n):
        return np.where(np.arange(at, at + n) % 2, 0.99, -0.99)   # the mic pinned at full scale

    def test_a_howl_mutes_the_mic_for_two_seconds(self):
        mixer = self.live()
        run(mixer, 0.25, self.howl)
        self.assertEqual(mixer.mic_state(), "live")
        run(mixer, 0.1, self.howl)
        self.assertEqual(mixer.mic_state(), "hush")
        muted = run(mixer, 1.9, self.howl)
        self.assertEqual(float(np.max(np.abs(muted))), 0.0)
        self.assertEqual(mixer.mic_state(), "hush")
        back = run(mixer, 0.2, lambda at, n: np.full(n, 0.1))
        self.assertEqual(mixer.mic_state(), "live")
        self.assertAlmostEqual(float(back[-1]), 0.03, places=5)

    def test_the_speaker_flat_out_with_a_loud_mic_counts_too(self):
        mixer = self.live(gain=1.0, volume=1.5)
        run(mixer, 0.4, lambda at, n: np.full(n, 0.8))   # not at full scale, but 1.2 into the limiter
        self.assertEqual(mixer.mic_state(), "hush")

    def test_short_shouts_are_fine(self):
        mixer = self.live()
        for _ in range(3):
            run(mixer, 0.2, self.howl)
            run(mixer, 0.05)
        self.assertEqual(mixer.mic_state(), "live")

    def test_loud_pads_with_a_quiet_mic_are_fine(self):
        mixer = self.live()
        run(mixer, 1.0, lambda at, n: np.full(n, 0.05), pad=np.full(SR, 1.5))
        self.assertEqual(mixer.mic_state(), "live")


@needs_numpy
class AnyThread(unittest.TestCase):
    """WASAPI starts a stream only on a thread that has COM, and PortAudio gives COM only to the thread that scans.
    Page presses arrive on server threads and the 5 s look runs on its own: the mic must still go on the air (it
    did not, live: POST /onair/on answered 409 and the look kept failing)."""

    def setUp(self):
        SD.thread_ok = lambda: threading.get_ident() == SD.scan_thread or getattr(radio._com_threads, "ready", False)
        self.addCleanup(setattr, SD, "thread_ok", lambda: True)

    @staticmethod
    def on_thread(fn):
        box = {}
        t = threading.Thread(target=lambda: box.update(r=fn()))
        t.start()
        t.join()
        return box.get("r")

    def test_on_the_air_from_a_thread_that_did_not_scan(self):
        w = World(mics=MICS, mic_default=1)
        self.assertTrue(self.on_thread(lambda: w.mixer.onair(True)))
        self.assertEqual(w.mixer.mic_state(), "live")
        self.assertTrue(w.mic()[0].active)

    def test_the_5_s_look_retries_without_a_rescan(self):
        w = World(mics=MICS, mic_default=1)
        w.mixer.want_air = True                   # on the air was asked for, and the open at start did not take
        scans = SD.scans
        self.on_thread(w.mixer._tick)             # nothing changed in Windows: no re-scan on the watch thread
        self.assertEqual(SD.scans, scans)
        self.assertEqual(w.mixer.mic_state(), "live")

    def test_a_page_press_over_http(self):
        w = World(mics=MICS, mic_default=1)
        api = plugins.Radio(["Ding!"], lambda s: None, lambda f, s: None, lambda s: None, log=lambda line: None)
        server, url, _ = radio.make_server(w.mixer, [("Ding!", "x", [0.0] * 480)], api)
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with urllib.request.urlopen(url, timeout=5) as r:
            token = re.search(r'TOKEN = "([^"]+)"', r.read().decode("utf-8")).group(1)
        req = urllib.request.Request(f"{url}onair/on?t={token}", method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 204)
        self.assertEqual(w.mixer.mic_state(), "live")


@needs_numpy
class MicRoutes(unittest.TestCase):
    """GET /mics and POST /mic, /micgain, /micdelay, /onair on the real server, over a real local socket."""

    def serve(self, **world):
        self.w = World(**world)
        api = plugins.Radio(["Ding!"], lambda s: None, lambda f, s: None, lambda s: None, log=lambda line: None)
        self.server, self.url, _ = radio.make_server(self.w.mixer, [("Ding!", "x", [0.0] * 480)], api)
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.token = re.search(r'TOKEN = "([^"]+)"', self.call("")[1]).group(1)

    def setUp(self):
        self.serve(mics=MICS, mic_default=1)

    def call(self, path, post=False, token=None):
        if token:
            path += f"?t={token}"
        req = urllib.request.Request(self.url + path, method="POST" if post else "GET", data=b"" if post else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            e.close()
            return e.code, ""

    def post(self, path):
        return self.call(path, post=True, token=self.token)[0]

    def level(self):
        return json.loads(self.call("level")[1])

    def listing(self):
        status, body = self.call("mics", token=self.token)
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_the_list_answers_only_the_page(self):
        self.assertEqual(self.call("mics")[0], 403)
        self.assertEqual(self.call("mics", token="guess")[0], 403)
        self.assertEqual(self.call("onair/on", post=True)[0], 403)

    def test_the_list_has_every_mic_and_the_knobs(self):
        r = self.listing()
        self.assertEqual([m["name"] for m in r["mics"]], MICS)
        self.assertEqual([m["short"] for m in r["mics"]], ["Headset Microphone", "Microphone"])
        self.assertEqual((r["follow"], r["default"], r["missing"], r["gain"], r["delay"]),
                         (True, "Microphone", False, 30, 350))

    def test_on_air_and_muted(self):
        self.assertEqual(self.level()["mic"], "off")
        self.assertEqual(self.post("onair/on"), 204)
        self.assertEqual((self.level()["mic"], self.level()["delay"]), ("live", 350))
        self.assertEqual(self.w.mic()[1], MICS[1])
        self.assertEqual(self.post("onair/off"), 204)
        self.assertEqual(self.level()["mic"], "off")
        self.assertFalse(self.w.mic()[0].active)

    def test_a_pick_holds_and_moves_a_live_mic_at_once(self):
        headset = next(m["index"] for m in self.listing()["mics"] if m["name"] == MICS[0])
        self.post("onair/on")
        self.assertEqual(self.post(f"mic/{headset}"), 204)
        self.assertEqual(self.w.mic()[1], MICS[0])
        self.assertTrue(self.w.mic()[0].active)
        r = self.listing()
        self.assertEqual((r["follow"], [m["short"] for m in r["mics"] if m["picked"]]),
                         (False, ["Headset Microphone"]))
        self.assertEqual(self.post("mic/default"), 204)
        self.assertEqual(self.w.mic()[1], MICS[1])

    def test_the_knobs(self):
        self.assertEqual(self.post("micgain/50"), 204)
        self.assertEqual(self.post("micdelay/2000"), 204)
        self.assertEqual((self.listing()["gain"], self.level()["delay"]), (50, 2000))
        self.assertEqual(self.post("micdelay/5000"), 204)
        self.assertEqual(self.level()["delay"], 2000)
        self.assertEqual(self.post("micgain/loud"), 400)
        self.assertEqual(self.post("onair/maybe"), 404)
        self.assertEqual(self.post("mic/99"), 404)

    def test_no_mic_says_so_and_does_nothing(self):
        self.serve()
        self.assertEqual(self.level()["mic"], "none")
        self.assertEqual(self.post("onair/on"), 409)
        self.assertEqual(SD.mics, [])
        self.assertEqual(self.listing()["mics"], [])


@needs_numpy
class Recording(unittest.TestCase):
    """What a plugin's record() gets: the fake mic hears silence ten times faster than real time."""

    def setUp(self):
        self.w = World(mics=MICS)

    def test_the_whole_length_without_a_halt(self):
        self.assertEqual(len(self.w.mixer.record(1.0)), SR)
        self.assertEqual(len(self.w.mixer.record(0)), 0)

    def test_a_halt_ends_it_early_with_what_it_has(self):
        halt = threading.Event()
        threading.Timer(0.1, halt.set).start()
        started = time.monotonic()
        voice = self.w.mixer.record(30.0, halt)   # three seconds of fake mic
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(0 < len(voice) < 30 * SR)
        self.assertEqual(voice.dtype, np.float32)

    def test_halted_before_it_starts_it_is_empty(self):
        halt = threading.Event()
        halt.set()
        self.assertEqual(len(self.w.mixer.record(5.0, halt)), 0)
        self.assertIsNone(World().mixer.record(5.0, halt))   # and no mic is still None


if __name__ == "__main__":
    unittest.main()
