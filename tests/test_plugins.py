"""Plugin system tests: discovery, skip-on-error, the API surface, and the shipped Call in plugin.

Standard library only:  py -3 -m unittest discover -s tests
"""
import ast
import inspect
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import plugins  # noqa: E402

NETWORK_MODULES = {"socket", "ssl", "urllib", "http", "ftplib", "smtplib", "poplib", "imaplib", "telnetlib",
                   "xmlrpc", "asyncio", "requests", "httpx", "aiohttp", "websocket", "websockets", "webbrowser"}


class Calls:
    """Fake audio for plugins.Radio that writes down every call."""

    def __init__(self, mic=True):
        self.log, self.mic = [], mic

    def play(self, sound):
        self.log.append(("play", sound if isinstance(sound, str) else len(sound)))

    def tone(self, freq, seconds):
        self.log.append(("tone", freq, seconds))

    def record(self, seconds):
        self.log.append(("record", seconds))
        return [0.1] * int(10 * seconds) if self.mic else None


def make_radio(mic=True):
    calls, lines = Calls(mic), []
    radio = plugins.Radio(["Ding!", "Boing"], calls.play, calls.tone, calls.record, log=lines.append)
    return radio, calls, lines


def write_plugins(folder, files):
    for name, body in files.items():
        Path(folder, name).write_text(body, encoding="utf-8")


class Discovery(unittest.TestCase):
    def test_every_plugin_file_is_registered_in_name_order(self):
        with tempfile.TemporaryDirectory() as d:
            write_plugins(d, {
                "b_second.py": "def register(radio):\n    radio.add_button('B', '#00ff00', lambda: None)\n",
                "a_first.py": "def register(radio):\n    radio.add_button('A', '#ff0000', lambda: None)\n",
                "_helper.py": "raise RuntimeError('underscore files are not plugins')\n",
                "notes.txt": "not python",
            })
            radio, _, lines = make_radio()
            loaded = plugins.load_all(radio, d, log=lines.append)
        self.assertEqual(loaded, ["a_first", "b_second"])
        self.assertEqual([b.label for b in radio._buttons], ["A", "B"])
        self.assertEqual(lines, [])

    def test_the_shipped_folder_loads_call_in(self):
        radio, _, lines = make_radio()
        self.assertIn("call_in", plugins.load_all(radio, log=lines.append))
        self.assertEqual(lines, [])


class SkipOnError(unittest.TestCase):
    def test_a_broken_plugin_is_skipped_with_one_log_line_and_the_rest_still_load(self):
        with tempfile.TemporaryDirectory() as d:
            write_plugins(d, {
                "bad_import.py": "import no_such_module_anywhere\n",
                "bad_register.py": ("def register(radio):\n"
                                    "    radio.add_button('Half', '#123456', lambda: None)\n"
                                    "    raise ValueError('broke halfway\\nsecond line')\n"),
                "bad_color.py": "def register(radio):\n    radio.add_button('Red', 'red', lambda: None)\n",
                "exits.py": "import sys\ndef register(radio):\n    sys.exit(3)\n",
                "no_register.py": "X = 1\n",
                "syntax.py": "def register(radio)\n",
                "zz_good.py": "def register(radio):\n    radio.add_button('Good', '#abcdef', lambda: None)\n",
            })
            radio, _, lines = make_radio()
            loaded = plugins.load_all(radio, d, log=lines.append)
        self.assertEqual(loaded, ["zz_good"])
        self.assertEqual([b.label for b in radio._buttons], ["Good"])   # "Half" was rolled back
        self.assertEqual(len(lines), 6)
        for line, name in zip(lines, ["bad_color", "bad_import", "bad_register", "exits", "no_register", "syntax"]):
            self.assertTrue(line.startswith(f"Plugin {name}.py skipped: "), line)
            self.assertNotIn("\n", line)
        self.assertNotIn("radio_plugin_bad_import", sys.modules)

    def test_a_failing_button_logs_one_line_and_frees_the_button(self):
        radio, _, lines = make_radio()
        done = threading.Event()

        def boom():
            done.set()
            raise RuntimeError("no luck")

        radio.add_button("Boom", "#ff0000", boom)
        self.assertTrue(radio._press(0))
        done.wait(5)
        for _ in range(500):
            if radio._buttons[0].busy.acquire(blocking=False):
                radio._buttons[0].busy.release()
                break
            threading.Event().wait(0.01)
        self.assertEqual(lines, ["Plugin button 'Boom' failed: RuntimeError: no luck"])


class ApiSurface(unittest.TestCase):
    def test_the_public_api_is_exactly_four_methods(self):
        public = {n for n, v in vars(plugins.Radio).items() if callable(v) and not n.startswith("_")}
        self.assertEqual(public, {"add_button", "play", "tone", "record"})   # no say(): the app has no speech

    def test_signatures(self):
        sig = lambda name: list(inspect.signature(getattr(plugins.Radio, name)).parameters)[1:]
        self.assertEqual(sig("add_button"), ["label", "color", "on_press"])
        self.assertEqual(sig("play"), ["sound"])
        self.assertEqual(sig("tone"), ["freq", "seconds"])
        self.assertEqual(sig("record"), ["seconds"])

    def test_add_button_checks_its_arguments(self):
        radio, _, _ = make_radio()
        for label, color, on_press in (("", "#ffffff", print), ("X", "white", print), ("X", "#fff", print),
                                       ("X", "#ffffff", "not callable")):
            with self.assertRaises((ValueError, TypeError)):
                radio.add_button(label, color, on_press)
        self.assertEqual(radio._buttons, [])

    def test_play_takes_a_sound_name_or_samples(self):
        radio, calls, _ = make_radio()
        radio.play("Ding!")
        radio.play([0.0, 0.5, -0.5])
        with self.assertRaises(KeyError):
            radio.play("Ding")
        self.assertEqual(calls.log, [("play", "Ding!"), ("play", 3)])

    def test_tone_and_record_are_kept_in_safe_ranges(self):
        radio, calls, _ = make_radio()
        radio.tone(20000, 60)
        radio.tone(5, -1)
        radio.tone(440, 0.5)
        self.assertEqual(calls.log, [("tone", 4000.0, 5.0), ("tone", 40.0, 0.0), ("tone", 440.0, 0.5)])
        calls.log.clear()
        radio.record(600)
        self.assertEqual(calls.log, [("record", 30.0)])

    def test_a_reply_goes_to_the_screen_and_a_busy_button_ignores_presses(self):
        radio, _, _ = make_radio()
        go, started = threading.Event(), threading.Event()

        def slow():
            started.set()
            go.wait(5)
            return "Hello"

        radio.add_button("Slow", "#00ff00", slow)
        self.assertTrue(radio._press(0))
        started.wait(5)
        self.assertFalse(radio._press(0))   # still running: ignored
        go.set()
        for _ in range(500):
            if radio._message[0]:
                break
            threading.Event().wait(0.01)
        self.assertEqual(radio._message, (1, "Hello"))


class CallIn(unittest.TestCase):
    def load(self, mic):
        radio, calls, lines = make_radio(mic)
        plugins.load_all(radio, log=lines.append)
        [button] = [b for b in radio._buttons if "Call in" in b.label]
        return button, calls

    def test_no_microphone_says_so_and_does_nothing_else(self):
        button, calls = self.load(mic=False)
        with mock.patch("time.sleep"):
            self.assertEqual(button.on_press(), "No microphone")
        self.assertEqual(calls.log, [("record", 0.0)])

    def test_a_call_rings_records_five_seconds_and_plays_it_back(self):
        button, calls = self.load(mic=True)
        with mock.patch("time.sleep"):
            self.assertEqual(button.on_press(), "You were on the air!")
        kinds = [c[0] for c in calls.log]
        self.assertEqual(calls.log[0], ("record", 0.0))
        self.assertIn(("record", 5.0), calls.log)
        self.assertLess(kinds.index("tone"), calls.log.index(("record", 5.0)))   # the ring comes first
        self.assertEqual(calls.log[-1], ("play", 50))                            # the recording, last


class NoNetwork(unittest.TestCase):
    def test_shipped_plugins_import_no_network_modules(self):
        for path in plugins.HERE.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                for name in names:
                    self.assertNotIn(name.split(".")[0], NETWORK_MODULES, f"{path.name} imports {name}")


if __name__ == "__main__":
    unittest.main()
