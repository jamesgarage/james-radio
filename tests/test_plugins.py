"""Plugin system tests: discovery, skip-on-error, the API surface, and the shipped Phone call plugin.

Standard library only:  py -3 -m unittest discover -s tests
"""
import ast
import inspect
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import plugins  # noqa: E402

NETWORK_MODULES = {"socket", "ssl", "urllib", "http", "ftplib", "smtplib", "poplib", "imaplib", "telnetlib",
                   "xmlrpc", "asyncio", "requests", "httpx", "aiohttp", "websocket", "websockets", "webbrowser"}


class Calls:
    """Fake audio for plugins.Radio that writes down every call. With realtime, a recording takes as long as a real
    one, or until the STOP halt it was handed."""

    def __init__(self, mic=True, realtime=False):
        self.log, self.halts, self.mic, self.realtime = [], [], mic, realtime

    def play(self, sound):
        self.log.append(("play", sound if isinstance(sound, str) else len(sound)))

    def tone(self, freq, seconds):
        self.log.append(("tone", freq, seconds))

    def record(self, seconds, *halt):
        self.log.append(("record", seconds))
        self.halts.append(halt)   # () when the radio asked exactly as it always has
        if self.realtime and seconds:
            (halt[0] if halt else threading.Event()).wait(seconds)
        return [0.1] * int(10 * seconds) if self.mic else None


def make_radio(mic=True, realtime=False):
    calls, lines = Calls(mic, realtime), []
    radio = plugins.Radio(["Ding!", "Boing"], calls.play, calls.tone, calls.record, log=lines.append)
    return radio, calls, lines


def no_waiting():
    """A plugin's waits return at once, but still say whether STOP was pressed: a whole call in no time."""
    return mock.patch("threading.Event.wait", lambda self, timeout=None: self.is_set())


def free(button, within=5.0):
    """True once the button's press has finished (it takes presses again), waiting up to `within` seconds."""
    end = time.monotonic() + within
    while True:
        if button.busy.acquire(blocking=False):
            button.busy.release()
            return True
        if time.monotonic() > end:
            return False
        time.sleep(0.005)


def until(check, within=5.0):
    """Wait for check() to come true; True when it did."""
    end = time.monotonic() + within
    while not check():
        if time.monotonic() > end:
            return False
        time.sleep(0.005)
    return True


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
        self.assertEqual(public, {"add_button", "play", "tone", "record"})   # no say(): only the station speaks

    def test_signatures(self):
        sig = lambda name: list(inspect.signature(getattr(plugins.Radio, name)).parameters)[1:]
        self.assertEqual(sig("add_button"), ["label", "color", "on_press", "page", "on_stop"])
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


class Stop(unittest.TestCase):
    """STOP on the page: every sound stops (the mixer's job) and each button that gave an on_stop hears about it."""

    def test_stop_tells_the_buttons_that_asked_and_a_failing_on_stop_is_one_log_line(self):
        radio, _, lines = make_radio()
        told = []
        radio.add_button("A", "#ff0000", lambda: None, on_stop=lambda: told.append("A"))
        radio.add_button("B", "#00ff00", lambda: None)
        radio.add_button("C", "#0000ff", lambda: None, on_stop=lambda: 1 / 0)
        radio.add_button("D", "#ffffff", lambda: None, on_stop=lambda: told.append("D"))
        radio._stop()
        self.assertEqual(told, ["A", "D"])   # C failing does not keep D from hearing
        self.assertEqual(lines, ["Plugin button 'C' on_stop failed: ZeroDivisionError: division by zero"])

    def test_on_stop_must_be_a_function(self):
        radio, _, _ = make_radio()
        with self.assertRaises(TypeError):
            radio.add_button("X", "#ffffff", print, on_stop="not callable")
        self.assertEqual(radio._buttons, [])

    def test_a_button_without_on_stop_works_exactly_as_before(self):
        radio, calls, _ = make_radio()
        go, started = threading.Event(), threading.Event()

        def slow():
            radio.record(2)
            started.set()
            go.wait(5)
            return "Done"

        radio.add_button("Slow", "#00ff00", slow)
        self.assertTrue(radio._press(0))
        started.wait(5)
        radio._stop()
        self.assertFalse(radio._press(0))   # STOP does not end it: still busy, as before
        self.assertEqual(calls.halts, [()])  # and it recorded the way it always has
        go.set()
        self.assertTrue(until(lambda: radio._message[0]))
        self.assertEqual(radio._message, (1, "Done"))

    def test_stop_ends_a_stoppable_buttons_recording_and_frees_it_for_the_next_press(self):
        radio, calls, _ = make_radio(realtime=True)
        radio.add_button("Talk", "#ff0000", lambda: radio.record(10), on_stop=lambda: None)
        button = radio._buttons[0]
        self.assertTrue(radio._press(0))
        self.assertTrue(until(lambda: calls.halts))
        radio._stop()
        self.assertTrue(free(button, within=0.5))      # not ten seconds
        [halt] = calls.halts[0]
        self.assertTrue(halt.is_set())
        self.assertTrue(radio._press(0))               # at once
        self.assertTrue(until(lambda: len(calls.halts) == 2))
        self.assertFalse(calls.halts[1][0].is_set())   # a fresh press is not stopped by the last STOP
        radio._stop()
        self.assertTrue(free(button, within=0.5))


class PhoneCall(unittest.TestCase):
    def load(self, mic, realtime=False):
        radio, calls, lines = make_radio(mic, realtime)
        plugins.load_all(radio, log=lines.append)
        [button] = [b for b in radio._buttons if "Phone call" in b.label]
        self.radio, self.index = radio, radio._buttons.index(button)
        return button, calls

    def test_it_is_on_the_station_page(self):
        button, _ = self.load(mic=True)
        self.assertEqual(button.page, "Station")

    def test_no_microphone_says_so_and_does_nothing_else(self):
        button, calls = self.load(mic=False)
        with no_waiting():
            self.assertEqual(button.on_press(), "No microphone")
        self.assertEqual(calls.log, [("record", 0.0)])

    def test_a_call_rings_records_five_seconds_and_plays_it_back(self):
        button, calls = self.load(mic=True)
        with no_waiting():
            self.assertEqual(button.on_press(), "You were on the air!")
        kinds = [c[0] for c in calls.log]
        self.assertEqual(calls.log[0], ("record", 0.0))
        self.assertIn(("record", 5.0), calls.log)
        self.assertLess(kinds.index("tone"), calls.log.index(("record", 5.0)))   # the ring comes first
        self.assertEqual(calls.log[-1], ("play", 50))                            # the recording, last

    def test_stop_hangs_up_at_once_and_the_next_press_calls_again(self):
        button, calls = self.load(mic=True, realtime=True)
        self.assertTrue(self.radio._press(self.index))
        self.assertTrue(until(lambda: ("tone", 440.0, 0.4) in calls.log))   # ringing
        self.radio._stop()
        self.assertTrue(free(button, within=0.5))
        self.assertEqual((calls.log.count(("tone", 440.0, 0.4)), self.radio._message), (1, (0, "")))
        self.assertTrue(self.radio._press(self.index))
        self.assertTrue(until(lambda: calls.log.count(("tone", 440.0, 0.4)) == 2))   # it rings again
        self.radio._stop()
        self.assertTrue(free(button, within=0.5))
        self.assertNotIn("play", [c[0] for c in calls.log])   # nobody was put on the air

    def test_stop_while_you_talk_puts_nothing_on_the_air(self):
        button, calls = self.load(mic=True)
        talk = calls.record

        def record(seconds, *halt):   # STOP is pressed while the caller talks
            if seconds:
                self.radio._stop()
            return talk(seconds, *halt)

        self.radio._record = record
        with no_waiting():
            self.assertIsNone(button.on_press())
        self.assertNotIn("play", [c[0] for c in calls.log])


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
