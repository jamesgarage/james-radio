# 🔊 James Soundboard

**Super Sound Pad** is a toy soundboard for little hands. It has 24 big glowing buttons, including an air horn,
applause, fart, ghost, robot and magic. They sit on a chunky "real" sound pad with screws in the corners, a green LCD,
an LED level meter, a giant red STOP button and a metal volume fader.

![Super Sound Pad](docs/screenshot.png)

Made for James by his dad and AI. Part of [James Garage](https://jamesgarage.github.io/).

## Play

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) once. It needs no admin rights, and uv
   fetches Python and the two libraries the pad needs by itself.
2. Double-click **Play.cmd**. The pad opens in its own window.
3. Close the window to quit.

To play through a particular speaker, such as a Bluetooth one, give part of its name:

```
Play.cmd --out "Bose"
```

If that speaker drops off Bluetooth, the pad keeps playing on the computer's own speakers. It switches back by
itself when the speaker returns. The LCD always shows which speaker is playing.

## Buttons

Tap a pad or press its key. The keys form the same grid on the keyboard. **Space** stops everything.

| | | | | | |
|---|---|---|---|---|---|
| **1** 📯 Air horn | **2** 👏 Applause | **3** 🥁 Ba-dum-tss | **4** 🪘 Drum roll | **5** 🎉 Ta-da! | **6** 💰 Cha-ching |
| **Q** 🔔 Ding! | **W** ❌ Buzzer | **E** 😢 Sad trombone | **R** 🚨 Siren | **T** 🚗 Car horn | **Y** ⏱️ Countdown |
| **A** 💨 Fart | **S** 🦘 Boing | **D** 🎶 Slide whistle | **F** 🫧 Bubbles | **G** 🦗 Crickets | **H** 👻 Ghost |
| **Z** 🛸 Laser | **X** 💥 Explosion | **C** 🤖 Robot | **V** ⭐ Power up | **B** ✨ Magic | **N** 💿 Record scratch |

Pads react the moment a finger lands, not when it lifts, and a long press brings up no menu. The whole pad scales
with the window, so full screen (F11) works well on a touch screen.

## How it works

- **Every sound is made from math. There are no recordings and nothing is downloaded.** For example, the air horn
  is four stacked tones cut into three blasts. The applause is 650 claps from 24 slightly different "hands" over a
  hiss of distant clapping. The bell sounds use out-of-tune overtones, which is what makes a bell sound struck
  rather than played. It all lives in `soundboard.py`.
- **Every sound is equally loud.** Each one is matched on its average loudness, not its loudest instant. Otherwise
  short claps and bells would sound faint next to the air horn. A soft limiter rounds off the loudest moments so
  nothing crackles.
- **Python plays the sound, and a web page draws the toy.** Python (numpy and sounddevice) mixes every pad into one
  audio stream on the chosen speaker. The pad itself is a small page served only to this computer (127.0.0.1) and
  opened in an Edge app window. Only that page can press the buttons.

## Bonus: microphone echo

`mic_echo.py` plays a microphone straight out of a speaker. Talk into the mic and hear yourself on the speaker. It
reconnects by itself if the speaker or the mic drops out.

```
uv run mic_echo.py --list
uv run mic_echo.py --in "USB" --out "Bose"
```

Keep the mic pointed away from the speaker, or it will howl.

## Requirements

Windows 10 or 11 with a speaker. The pad uses Windows audio and opens in Microsoft Edge's app mode (any browser
works if Edge is missing).

## License

[MIT](LICENSE)
