"""Speaker tests: following Windows' default speaker, with a fake sounddevice (no real audio is ever opened).

Standard library only:  py -3 -m unittest discover -s tests
radio.py imports numpy at the top for its synthesizer; when numpy is not installed, a stand-in with the one thing
the import needs is used. Nothing tested here makes a sound.
"""
import importlib
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


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


if __name__ == "__main__":
    unittest.main()
