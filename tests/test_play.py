"""PLAY page plugin tests: Animals, Silly voice and Sing-along, with the fake audio from test_plugins. Nothing
makes a sound.

The wiring, the tunes, the chipmunk and the press rules are standard library:  py -3 -m unittest discover -s tests
The synthesized sounds need numpy:  uv run --with numpy --with sounddevice python -m unittest discover -s tests
"""
import importlib.util
import math
import unittest
from unittest import mock

from test_mic import HAVE_NUMPY, needs_numpy
from test_plugins import free, make_radio, no_waiting, plugins, until

if HAVE_NUMPY:
    import numpy as np

SR = 48000
SCREEN = 22   # characters a message can have and still fit the display on a phone ("TOO LOUD, MOVE THE MIC")


def setup(name, mic=True, realtime=False):
    """The plugin's module, registered on a radio with fake audio: (module, its buttons by label, the fake, the
    radio)."""
    spec = importlib.util.spec_from_file_location(f"test_play_{name}", plugins.HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    radio, calls, _ = make_radio(mic, realtime)
    module.register(radio)
    return module, {b.label: b for b in radio._buttons}, calls, radio


def load(name, mic=True):
    return setup(name, mic)[:3]


def frequency(samples):
    """A tone's frequency from its zero crossings."""
    ups = sum(1 for a, b in zip(samples, samples[1:]) if a < 0 <= b)
    return ups * SR / len(samples)


class PlayPage(unittest.TestCase):
    def test_the_shipped_plugins_all_load_and_fill_the_play_page_in_order(self):
        radio, _, lines = make_radio()
        loaded = plugins.load_all(radio, log=lines.append)
        self.assertEqual(loaded, ["animals", "call_in", "silly_voice", "sing_along"])
        self.assertEqual(lines, [])   # without numpy too: they import it only when a pad is pressed
        self.assertEqual([b.label for b in radio._buttons if b.page == "Play"],
                         ["🐄 Cow", "🦆 Duck", "🐱 Cat", "🦉 Owl", "🐿️ Silly voice", "🎶 Sing-along"])
        self.assertEqual([b.label for b in radio._buttons if b.page == "Station"], ["📞 Phone call"])

    def test_every_message_fits_the_display(self):
        animals, _, _ = load("animals")
        sing, _, _ = load("sing_along")
        for text in [a[2] for a in animals.ANIMALS] + [t[0] for t in sing.TUNES] + ["🐿️ That was silly!"]:
            self.assertLessEqual(len(text), SCREEN, text)


class Animals(unittest.TestCase):
    def test_four_animals_and_no_dog(self):
        _, buttons, _ = load("animals")
        self.assertEqual(list(buttons), ["🐄 Cow", "🦆 Duck", "🐱 Cat", "🦉 Owl"])   # no bark sounded like a dog

    def test_a_press_plays_the_animal_and_says_its_word_and_presses_overlap_like_pads(self):
        module, buttons, calls = load("animals")
        with mock.patch.object(module, "make", side_effect=lambda label: [0.5] * len(label)) as make:
            self.assertEqual(buttons["🐄 Cow"].on_press(), "🐄 MOO!")
            self.assertEqual(buttons["🐄 Cow"].on_press(), "🐄 MOO!")
            self.assertEqual(buttons["🦉 Owl"].on_press(), "🦉 HOO HOO!")
        self.assertEqual(calls.log, [("play", 5), ("play", 5), ("play", 5)])
        self.assertEqual([c.args for c in make.call_args_list], [("🐄 Cow",), ("🦉 Owl",)])   # made once, kept


@needs_numpy
class AnimalSounds(unittest.TestCase):
    def test_each_sound_is_short_clean_and_below_4_khz(self):
        module, _, _ = load("animals")
        for label, *_ in module.ANIMALS:
            s = module.make(label)
            self.assertEqual(s.dtype, np.float32, label)
            self.assertTrue(0.5 <= len(s) / SR <= 2.0, label)
            self.assertTrue(np.all(np.isfinite(s)), label)
            self.assertAlmostEqual(float(np.max(np.abs(s))), 1.0, places=5)
            self.assertLess(abs(float(s[0])), 0.01, label)    # no click at either end
            self.assertLess(abs(float(s[-1])), 0.01, label)
            power = np.abs(np.fft.rfft(s)) ** 2
            high = power[np.fft.rfftfreq(len(s), 1 / SR) > 4200].sum() / power.sum()
            self.assertLess(high, 0.01, label)                # nothing piercing

    def test_the_same_sound_every_time(self):
        module, _, _ = load("animals")
        self.assertTrue(np.array_equal(module.make("🐱 Cat"), module.make("🐱 Cat")))


class SillyVoice(unittest.TestCase):
    def test_no_microphone_says_so_and_does_nothing_else(self):
        _, buttons, calls = load("silly_voice", mic=False)
        with no_waiting():
            self.assertEqual(buttons["🐿️ Silly voice"].on_press(), "No microphone")
        self.assertEqual(calls.log, [("record", 0.0)])

    def test_it_beeps_records_three_seconds_then_plays_it_back_after(self):
        _, buttons, calls = load("silly_voice")
        with no_waiting():
            self.assertEqual(buttons["🐿️ Silly voice"].on_press(), "🐿️ That was silly!")
        self.assertEqual(calls.log[0], ("record", 0.0))
        self.assertEqual([c[0] for c in calls.log[1:3]], ["tone", "tone"])   # the "talk now" beep-boop
        self.assertEqual(calls.log[3], ("record", 3.0))
        self.assertEqual(calls.log[4:], [("play", 18)])   # 30 fake samples sped up 1.6x; played after, never live

    def test_stop_while_you_talk_frees_it_at_once_and_plays_nothing(self):
        _, buttons, calls, radio = setup("silly_voice", realtime=True)
        button = buttons["🐿️ Silly voice"]
        self.assertTrue(radio._press(radio._buttons.index(button)))
        self.assertTrue(until(lambda: ("record", 3.0) in calls.log))   # recording, for three real seconds
        radio._stop()
        self.assertTrue(free(button, within=0.5))
        self.assertNotIn("play", [c[0] for c in calls.log])
        self.assertEqual(radio._message, (0, ""))
        with no_waiting():
            self.assertEqual(button.on_press(), "🐿️ That was silly!")   # and the next press is a whole one

    def test_the_chipmunk_is_higher_and_shorter(self):
        module, _, _ = load("silly_voice")
        voice = [math.sin(2 * math.pi * 300 * k / SR) for k in range(SR // 2)]
        squeak = module.chipmunk(voice)
        self.assertAlmostEqual(len(squeak), len(voice) / module.SPEED, delta=2)
        self.assertAlmostEqual(frequency(squeak), 300 * module.SPEED, delta=10)
        self.assertEqual((squeak[0], squeak[-1]), (0.0, 0.0))   # it fades in and out
        self.assertEqual(module.chipmunk([]), [])
        self.assertEqual(module.chipmunk([0.3]), [])


class SingAlong(unittest.TestCase):
    def test_the_four_public_domain_tunes(self):
        module, _, _ = load("sing_along")
        self.assertEqual([t[0] for t in module.TUNES],
                         ["🎵 Twinkle twinkle", "🎵 Old MacDonald", "🎵 Row your boat", "🎵 Happy birthday"])

    def test_each_tune_is_short_and_its_bass_keeps_time_with_it(self):
        module, _, _ = load("sing_along")
        for name, beat, melody, bass in module.TUNES:
            self.assertLessEqual(module.seconds((name, beat, melody, bass)), 15, name)
            self.assertEqual(sum(b for _, b in melody), sum(b for _, b in bass), name)
            for midi, _ in melody + bass:
                if midi is not None:
                    self.assertTrue(36 <= midi <= 96, (name, midi))

    def test_each_press_plays_the_next_tune_and_a_press_while_one_plays_is_ignored(self):
        module, buttons, calls = load("sing_along")
        press, clock = buttons["🎶 Sing-along"].on_press, [1000.0]
        with mock.patch.object(module, "render", side_effect=lambda tune: [0.1] * len(tune[2])), \
                mock.patch("time.monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(press(), "🎵 Twinkle twinkle")
            clock[0] += 2
            self.assertIsNone(press())                       # still playing: ignored, nothing more plays
            self.assertEqual(len(calls.log), 1)
            said = []
            for _ in range(4):
                clock[0] += 15
                said.append(press())
        self.assertEqual(said, ["🎵 Old MacDonald", "🎵 Row your boat", "🎵 Happy birthday", "🎵 Twinkle twinkle"])
        self.assertEqual([c[0] for c in calls.log], ["play"] * 5)   # each tune is ONE sound, so STOP stops it

    def test_after_stop_the_next_press_plays_the_next_tune_at_once(self):
        module, buttons, calls, radio = setup("sing_along")
        press, clock = buttons["🎶 Sing-along"].on_press, [1000.0]
        with mock.patch.object(module, "render", side_effect=lambda tune: [0.1] * len(tune[2])), \
                mock.patch("time.monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(press(), "🎵 Twinkle twinkle")
            clock[0] += 2
            radio._stop()                                   # the mixer has stopped the tune
            self.assertEqual(press(), "🎵 Old MacDonald")   # not ignored for the rest of Twinkle's 8 s
        self.assertEqual([c[0] for c in calls.log], ["play"] * 2)


@needs_numpy
class SingAlongSound(unittest.TestCase):
    def test_a_tune_is_one_clean_sound_as_long_as_it_says(self):
        module, _, _ = load("sing_along")
        for tune in module.TUNES:
            s = module.render(tune)
            self.assertEqual(s.dtype, np.float32)
            self.assertEqual(len(s), int(SR * module.seconds(tune)))
            self.assertTrue(np.all(np.isfinite(s)))
            self.assertAlmostEqual(float(np.max(np.abs(s))), 1.0, places=5)
            self.assertLess(float(np.max(np.abs(s[-SR // 100:]))), 0.05, tune[0])   # it has died away at the end
            power = np.abs(np.fft.rfft(s)) ** 2
            self.assertLess(power[np.fft.rfftfreq(len(s), 1 / SR) > 4200].sum() / power.sum(), 0.001, tune[0])

    def test_the_tune_starts_on_its_first_note(self):
        module, _, _ = load("sing_along")
        s = module.render(module.TUNES[0])[:int(SR * 0.4)]   # Twinkle: middle C's octave above, C5
        spectrum = np.abs(np.fft.rfft(s * np.hanning(len(s))))
        f = np.fft.rfftfreq(len(s), 1 / SR)
        peak = f[np.argmax(spectrum * (f > 300))]
        self.assertAlmostEqual(peak, module.hz(72), delta=5)


@needs_numpy
class ThroughTheRealRadio(unittest.TestCase):
    def test_what_the_plugins_play_reaches_the_mixer_no_louder_than_the_pads(self):
        from test_speakers import radio
        played = []

        class Mixer:
            play = staticmethod(played.append)
            record = staticmethod(lambda seconds: None)

        api = radio.plugin_radio(Mixer, [("Ding!", "x", np.zeros(10, np.float32))])
        plugins.load_all(api, log=self.fail)
        for b in api._buttons:
            if b.page == "Play" and "Silly" not in b.label:
                b.on_press()
        self.assertEqual(len(played), 5)   # four animals and a tune
        for snd in played:
            self.assertLessEqual(float(np.sqrt(np.mean(snd ** 2))), radio.TARGET_RMS * 1.01)
            self.assertLessEqual(float(np.max(np.abs(snd))), 1.0)


if __name__ == "__main__":
    unittest.main()
