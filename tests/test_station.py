"""STATION page tests: the page model (which pads go on which page, their keys and colours, a plugin button placed
on a page) on the real server, the station voice's cache with a fake speech engine (Windows speech never runs
here), the station pads with and without their voice, and the music that goes round until it is stopped.
Nothing makes a sound.

The page-model tests are standard library:  py -3 -m unittest discover -s tests
The sound tests need numpy:  uv run --with numpy --with sounddevice python -m unittest discover -s tests
"""
import json
import math
import os
import re
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import wave

from test_mic import BLOCK, HAVE_NUMPY, SR, needs_numpy
from test_speakers import World, plugins, radio

if HAVE_NUMPY:
    import numpy as np


def fake_sounds(n, prefix):
    return [(f"{prefix}{k}", "x", [0.0] * 480) for k in range(n)]


SOUNDS = fake_sounds(24, "Sound ") + fake_sounds(7, "Station ")
PAGES = [("Sounds", "a", 24, None), ("Station", "b", 7, 5)]
PHONE = (0, "Station", "📞 Phone call", "#ff4f8b")


class PageModel(unittest.TestCase):
    def test_each_page_takes_the_next_pads(self):
        cfg = radio.pad_config(SOUNDS, PAGES)
        self.assertEqual([p["page"] for p in cfg], [0] * 24 + [1] * 7)
        self.assertEqual([p["i"] for p in cfg], list(range(31)))   # what each pad plays: POST /play/<i>
        self.assertEqual(cfg[24]["name"], "Station 0")

    def test_every_page_has_the_keyboard_grid_to_itself(self):
        cfg = radio.pad_config(SOUNDS, PAGES)
        self.assertEqual("".join(p["key"] for p in cfg[:24]), "123456qwertyasdfghzxcvbn")
        self.assertEqual("".join(p["key"] for p in cfg[24:]), "1234qwe")   # a short page: rows of four

    def test_each_page_starts_the_rainbow_again(self):
        cfg = radio.pad_config(SOUNDS, PAGES)
        self.assertEqual([p["c"] for p in cfg[:7]], [c[0] for c in radio.RAINBOW] + [radio.RAINBOW[0][0]])
        self.assertEqual(cfg[24]["c"], radio.RAINBOW[0][0])

    def test_a_plugin_button_goes_where_its_page_keeps_them(self):
        cfg = radio.pad_config(SOUNDS, PAGES, [PHONE])
        station = [p for p in cfg if p["page"] == 1]
        self.assertEqual([p.get("i") for p in station], [24, 25, 26, 27, 28, None, 29, 30])
        self.assertEqual(station[5], {"button": 0, "label": "📞 Phone call", "c": "#ff4f8b", "page": 1, "key": "w"})
        self.assertEqual("".join(p["key"] for p in station), "1234qwer")

    def test_without_a_place_the_buttons_come_last_and_the_page_name_is_any_case(self):
        cfg = radio.pad_config(SOUNDS, [PAGES[0], ("Station", "b", 7, None)], [(3, "STATION", "X", "#000000")])
        self.assertEqual(cfg[-1]["button"], 3)

    def test_the_pages_must_add_up_to_the_sounds(self):
        with self.assertRaises(ValueError):
            radio.pad_config(SOUNDS[:-1], PAGES)

    def test_the_station_is_the_owners_list(self):
        self.assertEqual([s[0] for s in radio.STATION],
                         ["Station ID", "Sponsors", "Music", "Weather", "Breaking news", "Applause", "Sign off"])
        self.assertEqual([p[:3] for p in radio.PAGES], [("Sounds", "🎛️", 24), ("Station", "📡", 7)])
        said = {s[0]: s[3] for s in radio.STATION}
        self.assertEqual(said["Station ID"], "You're listening to James' Radio!")
        self.assertEqual(said["Sign off"], "That's all for today, goodnight!")
        self.assertIsNone(said["Applause"])


class StationServer(unittest.TestCase):
    """GET / and POST /play, /button on the real server, over a real local socket, with the real page list."""

    def setUp(self):
        self.w = World()
        self.played = []
        self.w.mixer.play = self.played.append   # what a pad hands the mixer
        self.api = plugins.Radio([n for n, *_ in SOUNDS], lambda s: None, lambda f, s: None, lambda s: None,
                                 log=lambda line: None)
        self.api.add_button("📞 Phone call", "#ff4f8b", lambda: "Hello caller", page="Station")
        self.api.add_button("🚪 Doorbell", "#66b8ff", lambda: None)
        self.server, self.url, _ = radio.make_server(self.w.mixer, SOUNDS, self.api, pages=PAGES, voiceless=[24])
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        with urllib.request.urlopen(self.url, timeout=5) as r:
            self.html = r.read().decode("utf-8")
        self.token = re.search(r'TOKEN = "([^"]+)"', self.html).group(1)

    def config(self, name):
        return json.loads(re.search(rf"\b{name} = (\[.*?\])(?:, [A-Z]+ = |;)", self.html).group(1))

    def post(self, path):
        req = urllib.request.Request(f"{self.url}{path}?t={self.token}", method="POST", data=b"")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def test_the_page_gets_both_pages_and_where_each_button_goes(self):
        self.assertEqual(self.config("PAGES"), [{"name": "Sounds", "emoji": "a", "few": False},
                                                {"name": "Station", "emoji": "b", "few": True}])
        pads = self.config("SOUNDS")
        self.assertEqual(len(pads), 32)
        self.assertEqual([p.get("button") for p in pads if p["page"] == 1][5], 0)        # Phone call: on STATION
        self.assertEqual(self.config("EXTRAS"), [{"label": "🚪 Doorbell", "c": "#66b8ff", "k": 1}])   # the rest: below

    def test_a_station_pad_plays_its_own_sound(self):
        self.assertEqual(self.post("play/24"), 204)
        self.assertIs(self.played[-1], SOUNDS[24][2])

    def test_the_phone_call_pad_presses_its_button(self):
        self.assertEqual(self.post("button/0"), 204)
        for _ in range(200):
            if self.api._message[0]:
                break
            threading.Event().wait(0.01)
        self.assertEqual(self.api._message, (1, "Hello caller"))

    def test_only_the_pads_without_a_voice_say_so(self):
        self.assertEqual([p["i"] for p in self.config("SOUNDS") if p.get("novoice")], [24])

    def test_one_page_when_no_pages_are_given(self):
        server, url, _ = radio.make_server(self.w.mixer, SOUNDS[:24], self.api)
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with urllib.request.urlopen(url, timeout=5) as r:
            self.html = r.read().decode("utf-8")
        self.assertEqual([p["name"] for p in self.config("PAGES")], ["Sounds"])
        self.assertEqual([x["k"] for x in self.config("EXTRAS")], [0, 1])   # no STATION page: both buttons below


class SpeechScript(unittest.TestCase):
    """The PowerShell that makes the voice: checked as text, never run."""

    JOBS = [("You're listening to James' Radio!", "cache\\voice-1.wav.part"), ("Breaking news!", "cache\\voice-2.wav.part")]

    def test_it_only_ever_speaks_into_files(self):
        script = radio.speech_script(self.JOBS)
        self.assertIn("SetOutputToWaveFile", script)
        self.assertLess(script.index("SetOutputToWaveFile"), script.index("$s.Speak("))
        self.assertNotIn("SetOutputToDefaultAudioDevice", script)
        self.assertIn("SetOutputToNull", script)

    def test_the_words_and_paths_arrive_whole(self):
        script = radio.speech_script(self.JOBS)
        quoted = re.search(r"\$jobs = '(.*)' \| ConvertFrom-Json", script).group(1)
        self.assertEqual(json.loads(quoted.replace("''", "'")), [{"text": w, "path": p} for w, p in self.JOBS])
        self.assertNotRegex(quoted.replace("''", ""), "'")   # every ' is doubled: the string cannot be closed early


def write_wav(path, seconds=0.5, rate=22050, channels=1, freq=440.0):
    frames = b"".join(struct.pack("<" + "h" * channels, *[int(8000 * math.sin(2 * math.pi * freq * k / rate))] * channels)
                      for k in range(int(rate * seconds)))
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)


class FakeSpeech:
    """Writes a short tone for each line, or fails like a PC without speech."""

    def __init__(self, works=True, seconds=0.5):
        self.calls, self.works, self.seconds = [], works, seconds

    def __call__(self, jobs):
        self.calls.append([w for w, _ in jobs])
        if not self.works:
            raise OSError("no speech on this PC")
        for _, path in jobs:
            write_wav(path, self.seconds)


@needs_numpy
class VoiceCache(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = os.path.join(tmp.name, "cache")

    def voices(self, words, speak, before=None):
        return radio.station_voices(words, self.folder, speak, before)

    def test_made_once_in_one_go_then_read_back(self):
        speak, told = FakeSpeech(), []
        first = self.voices(["Breaking news!", "Here's the weather"], speak, told.append)
        self.assertEqual(speak.calls, [["Breaking news!", "Here's the weather"]])
        self.assertEqual(told, [2])
        self.assertEqual(sorted(os.listdir(self.folder)),
                         sorted(os.path.basename(radio.voice_file(w, self.folder)) for w in first))
        again = self.voices(["Breaking news!", "Here's the weather"], FakeSpeech(works=False))   # never asked
        for w in first:
            self.assertTrue(np.array_equal(first[w], again[w]))

    def test_a_new_line_is_made_alone(self):
        self.voices(["Breaking news!"], FakeSpeech())
        speak = FakeSpeech()
        self.voices(["Breaking news!", "Sign off"], speak)
        self.assertEqual(speak.calls, [["Sign off"]])

    def test_read_at_the_radios_rate_in_mono(self):
        os.makedirs(self.folder)
        write_wav(radio.voice_file("a", self.folder), seconds=0.5, rate=22050, channels=2)
        clip = self.voices(["a"], FakeSpeech(works=False))["a"]
        self.assertEqual(clip.dtype, np.float32)
        self.assertAlmostEqual(len(clip) / SR, 0.5, delta=0.001)

    def test_no_speech_no_voice_and_nothing_left_behind(self):
        speak = FakeSpeech(works=False)
        self.assertEqual(self.voices(["a", "b"], speak), {"a": None, "b": None})
        self.assertEqual(os.listdir(self.folder), [])
        self.voices(["a", "b"], speak)
        self.assertEqual(len(speak.calls), 2)   # tried again on the next start

    def test_a_damaged_clip_is_made_again(self):
        os.makedirs(self.folder)
        with open(radio.voice_file("a", self.folder), "wb") as f:
            f.write(b"RIFF\x00\x00")
        speak = FakeSpeech()
        self.assertIsNotNone(self.voices(["a"], speak)["a"])
        self.assertEqual(speak.calls, [["a"]])

    def test_a_silent_clip_is_not_kept(self):
        speak = FakeSpeech(seconds=0)
        self.assertEqual(self.voices(["a"], speak), {"a": None})
        self.assertEqual(os.listdir(self.folder), [])

    def test_each_line_has_its_own_file_in_the_folder(self):
        a, b = radio.voice_file("a", self.folder), radio.voice_file("b", self.folder)
        self.assertNotEqual(a, b)
        self.assertEqual(os.path.dirname(a), self.folder)


def amplitude(x, freq, start, end):
    """How strong one frequency is in x between two times (seconds)."""
    seg = x[int(start * SR):int(end * SR)]
    k = np.arange(len(seg)) / SR
    return 2 * abs(np.sum(seg * np.exp(-2j * np.pi * freq * k))) / len(seg)


@needs_numpy
class StationPads(unittest.TestCase):
    def setUp(self):
        self.sting = np.sin(2 * np.pi * 200 * np.arange(2 * SR) / SR)      # 2 s at 200 Hz
        self.voice = np.sin(2 * np.pi * 3000 * np.arange(int(0.6 * SR)) / SR)   # 0.6 s at 3 kHz

    def test_without_a_voice_the_sting_plays_alone(self):
        alone = radio.station_pad(self.sting)
        self.assertEqual(alone.dtype, np.float32)
        self.assertAlmostEqual(amplitude(alone, 3000, 0.1, 1.9), 0, delta=0.01)

    def test_the_voice_comes_in_on_time_and_the_sting_dips_under_it(self):
        pad = radio.station_pad(self.sting, self.voice, at=0.5)
        before, under, after = (amplitude(pad, 200, a, b) for a, b in ((0.1, 0.3), (0.65, 0.95), (1.3, 1.6)))
        self.assertAlmostEqual(under / before, 0.3, delta=0.03)
        self.assertAlmostEqual(after / before, 1.0, delta=0.03)
        self.assertLess(amplitude(pad, 3000, 0.1, 0.45), 0.01)          # no voice before its time
        self.assertGreater(amplitude(pad, 3000, 0.55, 1.05), 0.3)       # then the voice

    def test_voice_first_then_the_sting(self):
        pad = radio.station_pad(self.sting, self.voice, at=None)
        self.assertGreater(amplitude(pad, 3000, 0.05, 0.5), 0.3)
        self.assertLess(amplitude(pad, 200, 0.05, 0.5), 0.01)
        self.assertGreater(amplitude(pad, 200, 1.0, 2.0), 0.3)
        self.assertLess(amplitude(pad, 3000, 1.0, 2.0), 0.01)

    def test_the_station_without_voices(self):
        sounds = [(n, "x", np.zeros(10, np.float32)) for n, *_ in radio.SOUNDS]
        pads = radio.render_station({}, sounds)
        self.assertEqual([p[0] for p in pads], [s[0] for s in radio.STATION])
        by_name = {n: s for n, _, s in pads}
        self.assertIs(by_name["Applause"], sounds[1][2])                  # the Sounds page's own pad
        self.assertIsInstance(by_name["Music"], radio.Loop)
        self.assertEqual(len(by_name["Music"].intro), 0)                  # no voice: the music alone
        self.assertTrue(all(np.all(np.isfinite(s.body if isinstance(s, radio.Loop) else s)) for s in by_name.values()))


@needs_numpy
class Music(unittest.TestCase):
    def setUp(self):
        self.loop = radio.Loop(np.full(100, 0.5, np.float32), (np.arange(1000) / 1000).astype(np.float32))

    def test_the_intro_once_then_the_body_round_and_round(self):
        self.assertTrue(np.array_equal(self.loop.chunk(0, 150)[100:], self.loop.body[:50]))
        self.assertTrue(np.all(self.loop.chunk(0, 150)[:100] == 0.5))
        wrap = self.loop.chunk(100 + 990 + 3000, 20)
        self.assertTrue(np.array_equal(wrap, np.concatenate([self.loop.body[990:], self.loop.body[:10]])))

    def mix(self, mixer, blocks):
        out = []
        for _ in range(blocks):
            block = np.zeros((BLOCK, 2), np.float32)
            mixer._mix(block, BLOCK, None, None)
            out.append(block[:, 0].copy())
        return np.concatenate(out)

    def test_it_goes_round_until_stop(self):
        w = World()
        w.mixer.play(self.loop)
        heard = self.mix(w.mixer, 30)   # 14,400 samples: the 1,100-sample loop many times over
        self.assertIsNotNone(w.mixer.music)
        self.assertGreater(float(np.max(heard[-BLOCK:])), 0)
        w.mixer.stop_all()
        self.assertIsNone(w.mixer.music)
        self.assertEqual(float(np.max(np.abs(self.mix(w.mixer, 2)))), 0.0)

    def test_its_pad_again_stops_it(self):
        w = World()
        w.mixer.play(self.loop)
        w.mixer.play(self.loop)
        self.assertIsNone(w.mixer.music)

    def test_the_station_music_has_no_seam(self):
        body = radio.music()
        self.assertAlmostEqual(len(body) / SR, 12.0, places=2)
        steps = np.abs(np.diff(body))
        self.assertLessEqual(abs(body[0] - body[-1]), np.percentile(steps, 99.9))   # no click at the join


if __name__ == "__main__":
    unittest.main()
