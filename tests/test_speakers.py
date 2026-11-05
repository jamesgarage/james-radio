"""Speaker tests: following Windows' default speaker, short names for the display, and the page's speaker picker
(GET /speakers, POST /speaker/<index|default>), with a fake sounddevice: no real audio device is ever opened.

Standard library only:  py -3 -m unittest discover -s tests
radio.py imports numpy at the top for its synthesizer; when numpy is not installed, a stand-in with the one thing
the import needs is used. Nothing tested here makes a sound.
"""
import importlib
import json
import re
import sys
import threading
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import plugins  # noqa: E402


class FakeStream:
    def __init__(self, sd, device, **settings):
        self.sd, self.device, self.active = sd, device, False

    def start(self):
        self.active = True

    def close(self):
        self.active = False


class FakeSD(types.ModuleType):
    """Just enough of sounddevice for the mixer: two host APIs, a device list and a default that tests can change."""

    PortAudioError = RuntimeError

    def __init__(self):
        super().__init__("sounddevice")
        self.devices = [{"name": "Speakers (Old Driver)", "hostapi": 0, "max_output_channels": 2}]   # MME: ignored
        self.default = -1
        self.scans, self.opened = 0, []

    def add(self, name, outputs=2):
        self.devices.append({"name": name, "hostapi": 1, "max_output_channels": outputs})
        return len(self.devices) - 1

    def query_hostapis(self, i=None):
        apis = [{"name": "MME", "default_output_device": 0},
                {"name": "Windows WASAPI", "default_output_device": self.default}]
        return apis if i is None else apis[i]

    def query_devices(self, i=None):
        return self.devices if i is None else self.devices[i]

    def _terminate(self):
        self.scans += 1

    def _initialize(self):
        pass

    def WasapiSettings(self, **kw):
        return kw

    def OutputStream(self, device, **settings):
        self.opened.append(device)
        return FakeStream(self, device, **settings)


SD = FakeSD()
sys.modules["sounddevice"] = SD
try:
    import numpy  # noqa: F401  (the real one, when the tests run where it is installed)
except ImportError:
    sys.modules["numpy"] = types.SimpleNamespace(random=types.SimpleNamespace(default_rng=lambda seed: None))
radio = importlib.import_module("radio")
radio.sd = SD


class World:
    """A fresh fake PC: its speakers, Windows' default among them, and a mixer on it."""

    def __init__(self, names=("Speakers (SoundWire Speakers)", "Airpod Max (Sam's AirPods Max)"), default=1,
                 out=""):
        SD.devices[1:] = []
        SD.opened.clear()
        self.idx = [SD.add(n) for n in names]
        self.set_default(default)
        self.mixer = radio.Mixer(out, default_id=lambda: self.default_id)
        self.mixer._open()

    def set_default(self, k):
        SD.default = self.idx[k]
        self.default_id = f"endpoint-{k}"

    def playing(self):
        return SD.devices[SD.opened[-1]]["name"] if SD.opened else None


class FollowDefault(unittest.TestCase):
    def test_starts_on_windows_default(self):
        w = World(default=1)
        self.assertEqual(w.playing(), "Airpod Max (Sam's AirPods Max)")

    def test_moves_to_the_new_default_between_sounds(self):
        w = World(default=1)
        w.set_default(0)
        self.assertTrue(w.mixer._tick())
        self.assertEqual(w.playing(), "Speakers (SoundWire Speakers)")
        self.assertEqual(w.mixer.device, "Speakers (SoundWire Speakers)")

    def test_never_cuts_a_sound_short(self):
        w = World(default=1)
        w.mixer.voices.append(([0.0] * 10, 0))
        w.set_default(0)
        w.mixer._tick()
        self.assertEqual(w.playing(), "Airpod Max (Sam's AirPods Max)")
        w.mixer.voices.clear()
        w.mixer._tick()
        self.assertEqual(w.playing(), "Speakers (SoundWire Speakers)")

    def test_an_unchanged_default_leaves_the_stream_alone(self):
        w = World(default=1)
        opens, scans = len(SD.opened), SD.scans
        for _ in range(3):
            w.mixer._tick()
        self.assertEqual((len(SD.opened), SD.scans), (opens, scans))

    def test_no_answer_from_windows_changes_nothing(self):
        w = World(default=1)
        w.default_id = None
        SD.default = w.idx[0]
        opens = len(SD.opened)
        w.mixer._tick()
        self.assertEqual(len(SD.opened), opens)

    def test_out_stays_put_when_the_default_moves(self):
        w = World(default=1, out="SoundWire")
        self.assertEqual(w.playing(), "Speakers (SoundWire Speakers)")
        opens = len(SD.opened)
        w.set_default(0)
        w.mixer._tick()
        self.assertEqual(len(SD.opened), opens)

    def test_out_waits_on_the_default_and_returns(self):
        w = World(names=("Airpod Max (Sam's AirPods Max)",), default=0, out="USB Advanced")
        self.assertTrue(w.mixer.fallback)
        self.assertEqual(w.playing(), "Airpod Max (Sam's AirPods Max)")
        SD.add("Speakers (3- USB Advanced Audio Device)")
        w.mixer._tick()
        self.assertEqual(w.playing(), "Speakers (3- USB Advanced Audio Device)")
        self.assertFalse(w.mixer.fallback)

    def test_a_closed_mixer_stops_watching(self):
        w = World()
        w.mixer.close()
        self.assertFalse(w.mixer._tick())


THIS_PC = ["Monitor -2 (2- HD Audio Driver for Display Audio)", "Speakers (SoundWire Speakers)",
           "Airpod Max (Sam’s AirPods Max - Find My)", "Speakers (3- USB Advanced Audio Device)"]


class ShortNames(unittest.TestCase):
    def test_the_bracketed_part_goes(self):
        self.assertEqual(radio.short_name(THIS_PC[2], THIS_PC), "Airpod Max")
        self.assertEqual(radio.short_name(THIS_PC[0], THIS_PC), "Monitor -2")

    def test_two_speakers_with_one_name_keep_a_telling_word(self):
        self.assertEqual(radio.short_name(THIS_PC[1], THIS_PC), "Speakers (SoundWire)")
        self.assertEqual(radio.short_name(THIS_PC[3], THIS_PC), "Speakers (USB)")
        self.assertEqual(radio.short_name("Speakers (Realtek(R) Audio)", ["Speakers (Realtek(R) Audio)", THIS_PC[1]]),
                         "Speakers (Realtek)")

    def test_a_lone_speakers_needs_no_word(self):
        self.assertEqual(radio.short_name(THIS_PC[1], THIS_PC[1:3]), "Speakers")

    def test_at_most_22_characters(self):
        name = "Living Room Sound Bar Left Channel (Bluetooth)"
        short = radio.short_name(name, [name])
        self.assertEqual(len(short), 22)
        self.assertEqual(short, "Living Room Sound Bar…")
        self.assertEqual(radio.short_name("Kitchen", ["Kitchen"]), "Kitchen")
        self.assertEqual(radio.short_name("No speaker"), "No speaker")

    def test_the_display_shows_the_short_name(self):
        w = World(names=THIS_PC, default=2)
        self.assertEqual(w.mixer.label(), "Airpod Max")
        w.set_default(1)
        w.mixer._tick()
        self.assertEqual(w.mixer.label(), "Speakers (SoundWire)")


class PickerRoutes(unittest.TestCase):
    """GET /speakers and POST /speaker/<index|default> on the real server, over a real local socket."""

    def setUp(self):
        self.w = World(names=THIS_PC, default=2)
        api = plugins.Radio(["Ding!"], lambda s: None, lambda f, s: None, lambda s: None, log=lambda line: None)
        self.server, self.url, _ = radio.make_server(self.w.mixer, [("Ding!", "x", [0.0] * 480)], api)
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.token = re.search(r'TOKEN = "([^"]+)"', self.call("")[1]).group(1)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, path, post=False, host=None, token=None):
        if token:
            path += f"?t={token}"
        req = urllib.request.Request(self.url + path, method="POST" if post else "GET", data=b"" if post else None)
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            e.close()
            return e.code, ""

    def listing(self):
        status, body = self.call("speakers", token=self.token)
        self.assertEqual(status, 200)
        return json.loads(body)

    def index_of(self, listing, name):
        return next(s["index"] for s in listing["speakers"] if s["name"] == name)

    def pick(self, what):
        return self.call(f"speaker/{what}", post=True, token=self.token)[0]

    def test_the_list_answers_only_the_page(self):
        self.assertEqual(self.call("speakers")[0], 403)
        self.assertEqual(self.call("speakers", token="guess")[0], 403)
        self.assertEqual(self.call("speakers", token=self.token, host="evil.example")[0], 403)

    def test_the_list_has_every_speaker_with_short_and_full_names(self):
        r = self.listing()
        self.assertEqual([s["name"] for s in r["speakers"]], THIS_PC)   # WASAPI only: the MME copy is not there
        self.assertEqual([s["short"] for s in r["speakers"]],
                         ["Monitor -2", "Speakers (SoundWire)", "Airpod Max", "Speakers (USB)"])
        self.assertEqual((r["follow"], r["default"], r["missing"]), (True, "Airpod Max", False))
        self.assertFalse(any(s["picked"] for s in r["speakers"]))

    def test_a_pick_moves_the_sound_and_holds_when_windows_changes(self):
        usb = self.index_of(self.listing(), THIS_PC[3])
        self.assertEqual(self.pick(usb), 204)
        self.assertEqual(self.w.playing(), THIS_PC[3])
        self.assertEqual(json.loads(self.call("level")[1])["speaker"], "Speakers (USB)")
        r = self.listing()
        self.assertFalse(r["follow"])
        self.assertEqual([s["short"] for s in r["speakers"] if s["picked"]], ["Speakers (USB)"])
        self.w.set_default(1)
        self.w.mixer._tick()
        self.assertEqual(self.w.playing(), THIS_PC[3])

    def test_windows_default_is_followed_again(self):
        self.pick(self.index_of(self.listing(), THIS_PC[3]))
        self.assertEqual(self.pick("default"), 204)
        self.assertEqual(self.w.playing(), THIS_PC[2])
        self.assertTrue(self.listing()["follow"])
        self.w.set_default(1)
        self.w.mixer._tick()
        self.assertEqual(self.w.playing(), THIS_PC[1])

    def test_a_pick_waits_for_the_sound_playing(self):
        usb = self.index_of(self.listing(), THIS_PC[3])
        self.w.mixer.voices.append(([0.0] * 10, 0))
        self.assertEqual(self.pick(usb), 204)
        self.assertEqual(self.w.playing(), THIS_PC[2])
        self.w.mixer.voices.clear()
        self.w.mixer._tick()
        self.assertEqual(self.w.playing(), THIS_PC[3])

    def test_a_picked_speaker_that_goes_away_is_waited_for(self):
        self.pick(self.index_of(self.listing(), THIS_PC[3]))
        SD.devices.pop()                       # switched off
        self.w.mixer.stream.active = False     # its stream dies with it
        self.w.mixer._tick()
        self.assertEqual(self.w.playing(), THIS_PC[2])
        r = self.listing()
        self.assertEqual((r["missing"], r["pinned"]), (True, "Speakers (USB)"))
        SD.add(THIS_PC[3])                     # back on
        self.w.mixer._tick()
        self.assertEqual(self.w.playing(), THIS_PC[3])

    def test_bad_picks_change_nothing(self):
        self.assertEqual(self.pick(1), 404)    # a pick must come from a list the page was shown
        self.listing()
        self.assertEqual(self.pick(99), 404)
        self.assertEqual(self.pick("loud"), 400)
        self.assertEqual(self.call("speaker/1", post=True)[0], 403)
        self.assertEqual(self.call("speaker/1", post=True, token=self.token, host="evil.example")[0], 403)
        self.assertEqual(self.w.playing(), THIS_PC[2])
        self.assertTrue(self.listing()["follow"])


if __name__ == "__main__":
    unittest.main()
