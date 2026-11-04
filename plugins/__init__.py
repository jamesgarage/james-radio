"""Plugins for James' Radio.

Every plugins/*.py file (names starting with "_" excepted) that has a register(radio) function is loaded when
the app starts. How to write one, the API and the rules: docs/plugins.md. Plugins never touch the network.

Standard library only: radio.py hands Radio the real audio, the tests hand it fakes.
"""
import importlib.util
import re
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
COLOR = re.compile(r"#[0-9a-fA-F]{6}")
LOWEST_HZ, HIGHEST_HZ = 40.0, 4000.0          # plugin tones stay out of the piercing top of a child's hearing
LONGEST_TONE, LONGEST_RECORDING = 5.0, 30.0   # seconds


def _print(text):
    print(text, flush=True)


class _Button:
    def __init__(self, label, color, on_press):
        self.label, self.color, self.on_press, self.busy = label, color, on_press, threading.Lock()


class Radio:
    """Everything a plugin may do: add_button, play, tone, record. Underscored members belong to the app."""

    def __init__(self, sound_names, play, tone, record, log=_print):
        self._sound_names = list(sound_names)
        self._play, self._tone, self._record, self._log = play, tone, record, log
        self._buttons = []        # drawn on the page after the 24 pads, in load order
        self._message = (0, "")   # (count, text): the latest on_press reply, shown on the screen
        self._lock = threading.Lock()

    def add_button(self, label, color, on_press):
        """A button on the radio. on_press() runs on its own thread; a string it returns is shown on the screen."""
        if not isinstance(label, str) or not label.strip():
            raise ValueError("label must be some text")
        if not (isinstance(color, str) and COLOR.fullmatch(color)):
            raise ValueError(f"color must look like '#ff6161', not {color!r}")
        if not callable(on_press):
            raise TypeError("on_press must be a function")
        self._buttons.append(_Button(label.strip(), color, on_press))

    def play(self, sound):
        """Start a built-in sound by name ("Ding!") or samples from record(); returns at once, sounds overlap."""
        if isinstance(sound, str) and sound not in self._sound_names:
            raise KeyError(f"no sound named {sound!r}; the names are on the pads")
        self._play(sound)

    def tone(self, freq, seconds):
        """Start a soft sine wave (40-4000 Hz, up to 5 s); returns at once."""
        self._tone(min(max(float(freq), LOWEST_HZ), HIGHEST_HZ), min(max(float(seconds), 0.0), LONGEST_TONE))

    def record(self, seconds):
        """Record up to 30 s from the default microphone: 48 kHz mono samples, or None when there is no microphone.

        Blocks while recording. record(0) only checks that a microphone is there."""
        return self._record(min(max(float(seconds), 0.0), LONGEST_RECORDING))

    def _press(self, i):
        """Run button i's on_press on its own thread. A press while the last one is still running is ignored."""
        button = self._buttons[i]
        if not button.busy.acquire(blocking=False):
            return False

        def run():
            try:
                reply = button.on_press()
                if isinstance(reply, str) and reply:
                    with self._lock:
                        self._message = (self._message[0] + 1, reply)
            except Exception as e:
                self._log(f"Plugin button {button.label!r} failed: {_one_line(e)}")
            finally:
                button.busy.release()

        threading.Thread(target=run, daemon=True).start()
        return True


def _one_line(e):
    text = (str(e).splitlines() or [""])[0]
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def load_all(radio, folder=HERE, log=_print):
    """Import every <folder>/*.py and call its register(radio). One that fails is skipped with one log line."""
    loaded = []
    for path in sorted(Path(folder).glob("*.py")):
        if path.name.startswith("_"):
            continue
        name = f"radio_plugin_{path.stem}"
        before = len(radio._buttons)
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module   # what dataclasses and pickling expect of an imported module
            spec.loader.exec_module(module)
            module.register(radio)
        except (Exception, SystemExit) as e:
            sys.modules.pop(name, None)
            del radio._buttons[before:]   # a half-registered plugin leaves no buttons behind
            log(f"Plugin {path.name} skipped: {_one_line(e)}")
            continue
        loaded.append(path.stem)
    return loaded
