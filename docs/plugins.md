# Writing a plugin for James' Radio

A plugin is one Python file in `plugins/`. When the radio starts, it loads every `plugins/*.py` (files starting
with `_` are skipped) and calls the file's `register(radio)`. The buttons it adds appear in the row under the pads,
or on the page of pads they name. `plugins/call_in.py` (Phone call, on the STATION page) is a complete example;
`plugins/sing_along.py` and `plugins/animals.py` make their own sounds with numpy.

The PLAY page is the place for toys: it has no pads of its own, so `page="Play"` buttons are all it shows (four to
a row while there are eight or fewer), and it only appears when some plugin puts a button there.

## A plugin in ten lines

```python
"""Doorbell: ding, then dong."""
import time


def register(radio):
    def ring():
        radio.play("Ding!")
        time.sleep(0.6)
        radio.tone(330, 0.8)

    radio.add_button("🚪 Doorbell", "#66b8ff", ring)
```

Save it as `plugins/doorbell.py` and start the radio. The console line lists the plugins that loaded.

## The API

`radio` has four methods. That is the whole API; anything starting with `_` belongs to the app.

| Method | What it does |
|---|---|
| `add_button(label, color, on_press, page=None)` | Adds a button. `label` is text (start it with an emoji to give the button a picture); `color` is a hex color like `"#ff6161"`; `on_press()` runs on its own thread each time the button is pressed. If `on_press` returns a string, the radio's screen shows it. A press while the last one is still running is ignored. `page="Station"` puts the button on the STATION page among its pads; without `page` it goes in the row under the pads. |
| `play(sound)` | Starts a sound and returns at once; sounds overlap. `sound` is a pad's name (`"Ding!"`, `"Boing"`, `"Station ID"`, ...) or samples: what `record()` returned, or a sound the plugin made itself (48 kHz mono, a list or a numpy array, up to 60 seconds). Samples are levelled so they are never louder than the pads, and STOP stops them like a pad. A name that does not exist raises `KeyError`. |
| `tone(freq, seconds)` | Starts a soft sine wave and returns at once. The frequency is kept between 40 and 4000 Hz and the length at 5 seconds or less. |
| `record(seconds)` | Records from the Windows default microphone and returns the samples (48 kHz, mono), or `None` when there is no microphone. Blocks while it records; at most 30 seconds. `record(0)` only checks that a microphone is there. |

There is no `say()`: the station voice says only the STATION page's own lines.

## Rules

- **Plugins never touch the network.** No sockets, no web requests, no opening web pages. The radio is a toy that
  works offline, and a test checks the shipped plugins import nothing that could reach the network.
- **No new dependencies.** The standard library and numpy are there; nothing else is. Import numpy inside the
  function that needs it (the first press), not at the top of the file: the plain test run has no numpy, and it
  still loads every shipped plugin.
- **A tune is one sound.** Make the whole thing first and `play()` it once, rather than many `tone()`s in a row:
  STOP stops a sound, but cannot stop a plugin that is still calling `tone()`.
- **Be kind to small ears.** Everything a plugin plays is levelled to be no louder than the pads, and recordings
  are played back after recording, never live, so the speaker cannot feed back into the mic.
- **A broken plugin never breaks the radio.** If `register` raises (or the file does not import), that plugin is
  skipped with one line in the console and any buttons it added are taken away again. If `on_press` raises, the
  console gets one line and the button works again on the next press.

## Testing

```
py -3 -m unittest discover -s tests
```

The tests give the plugin API fake audio and check discovery, the skip-on-error rule, the API surface and every
shipped plugin (`tests/test_plugins.py`, `tests/test_play.py`). The ones that make real sounds need numpy and are
skipped without it: `uv run --with numpy --with sounddevice python -m unittest discover -s tests` runs them all.
