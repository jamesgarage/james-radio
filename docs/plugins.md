# Writing a plugin for James' Radio

A plugin is one Python file in `plugins/`. When the radio starts, it loads every `plugins/*.py` (files starting
with `_` are skipped) and calls the file's `register(radio)`. Whatever buttons it adds appear on the radio after
the 24 pads. `plugins/call_in.py` is a complete example.

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
| `add_button(label, color, on_press)` | Adds a button. `label` is text (start it with an emoji to give the button a picture); `color` is a hex color like `"#ff6161"`; `on_press()` runs on its own thread each time the button is pressed. If `on_press` returns a string, the radio's screen shows it. A press while the last one is still running is ignored. |
| `play(sound)` | Starts a sound and returns at once; sounds overlap. `sound` is a pad's name (`"Ding!"`, `"Boing"`, ...) or samples that `record()` returned. A name that does not exist raises `KeyError`. |
| `tone(freq, seconds)` | Starts a soft sine wave and returns at once. The frequency is kept between 40 and 4000 Hz and the length at 5 seconds or less. |
| `record(seconds)` | Records from the Windows default microphone and returns the samples (48 kHz, mono), or `None` when there is no microphone. Blocks while it records; at most 30 seconds. `record(0)` only checks that a microphone is there. |

There is no `say()`: the radio has no speech voice.

## Rules

- **Plugins never touch the network.** No sockets, no web requests, no opening web pages. The radio is a toy that
  works offline, and a test checks the shipped plugins import nothing that could reach the network.
- **No new dependencies.** The standard library and numpy are there; nothing else is.
- **Be kind to small ears.** Everything a plugin plays is levelled to be no louder than the pads, and recordings
  are played back after recording, never live, so the speaker cannot feed back into the mic.
- **A broken plugin never breaks the radio.** If `register` raises (or the file does not import), that plugin is
  skipped with one line in the console and any buttons it added are taken away again. If `on_press` raises, the
  console gets one line and the button works again on the next press.

## Testing

```
py -3 -m unittest discover -s tests
```

The tests use only the standard library: they give the plugin API fake audio and check discovery, the
skip-on-error rule, the API surface and the Call in plugin.
