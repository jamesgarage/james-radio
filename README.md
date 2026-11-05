# James' Radio

James' Radio is a toy soundboard for Windows: 24 synthesized sounds on big glowing pads. Every sound is
generated with numpy when the program starts, so there are no sound files. The only downloads are the ones uv
makes on the first run (Python and two libraries, see Requirements).

Tap a pad or press its key. The keys follow the grid's shape on the keyboard:

| Row | Keys | Sounds |
|---|---|---|
| 1 | `1 2 3 4 5 6` | Air horn, Applause, Ba-dum-tss, Drum roll, Ta-da!, Cha-ching |
| 2 | `Q W E R T Y` | Ding!, Buzzer, Sad trombone, Siren, Car horn, Countdown |
| 3 | `A S D F G H` | Fart, Boing, Slide whistle, Bubbles, Crickets, Ghost |
| 4 | `Z X C V B N` | Laser, Explosion, Robot, Power up, Magic, Record scratch |

**Space** (or Esc) stops all sounds. The page also has a STOP button, a volume fader (0-150%) and a level meter.

Python plays the sound through one audio stream. The radio itself is a small web page served on 127.0.0.1
only (unless you share it with a phone, below), opened in a Microsoft Edge app window (or your default browser
if Edge is missing). Closing the window ends the program. The page fits a laptop screen in one piece and a phone
or tablet in either orientation.

## Requirements

- Windows 10 or 11 (the sound goes through the Windows WASAPI audio API)
- Python 3.10 or newer, run through [uv](https://docs.astral.sh/uv/getting-started/installation/). uv installs
  Python and the two libraries (numpy, sounddevice) by itself on the first run.
- A sound output: speakers or headphones

## Run

```
uv run radio.py
```

Or double-click `Play.cmd`, which checks that uv is installed and then runs the same command. Any options you
give `Play.cmd` are passed on.

Options:

- `--out "<part of name>"` plays through the output device whose name contains that text, for example
  `uv run radio.py --out "Speakers (USB"`. Without it, the radio plays on the Windows default output and follows
  it: pick another default speaker in Windows while the radio runs, and the sound moves there within 5 seconds,
  between sounds. If the named device is missing, the radio plays on the default output and checks every 5
  seconds for the named one to return.
- `--no-window` serves the page but does not open a window. The program prints the address; open it within
  90 seconds (5 minutes with `--share`), or the program exits.
- `--share` also lets phones and tablets on the same Wi-Fi play the radio; see below.

To find device names, list them with the microphone tool below: `uv run mic_echo.py --list`.

## Choosing the speaker

Tap the speaker name under the display to choose where the sound plays. A list of the PC's speakers opens, with
Windows default first and the one in use ticked; the display shows a short name and the list shows each full
one. A speaker picked there stays picked until the radio closes or you pick Windows default again. If it is
switched off, the radio plays on the Windows default until it is back. A phone playing the radio (below) can
choose too; the sound always comes out of the PC.

## Play from a phone or tablet

Double-click `Play on Wi-Fi.cmd` (or run `uv run radio.py --share`). The radio opens on the PC as usual, and its
console window prints an address like `http://192.168.0.42:8481/`. Open that address in the browser of a phone
or tablet on the same Wi-Fi. On an iPhone or iPad, Share > Add to Home Screen gives it an icon; the address stays
the same as long as the PC keeps its network address.

- The phone is a remote control. The sound comes out of the PC, and Call in records with the PC's microphone.
- It only works while James' Radio is open on the PC. Close the radio's window on the PC and it stops for
  every phone.
- Nothing leaves the home network. The radio listens on the PC's home-network address only, has no accounts and
  sends nothing to the internet. Anyone on the same Wi-Fi who opens the address can press the buttons.
- The first time, Windows may ask whether Python may use the network: allow **Private networks** only. Don't
  share on a public or guest network.
- On a PC with several network adapters the radio may pick the wrong address. Name the right one:
  `uv run radio.py --share 192.168.0.42`. Without `--share`, only the PC itself can open the radio.

## Plugins

Extra buttons come from small Python files in `plugins/`; every one there is loaded at start. The radio ships
with one:

- **Call in** (`plugins/call_in.py`): a phone rings, a beep says "talk now", the microphone records five
  seconds, and the radio plays a short station sting and then puts the caller on the air. With no microphone
  the screen says so and nothing else happens.

To write your own, see [docs/plugins.md](docs/plugins.md). Plugins never touch the network. Tests:
`py -3 -m unittest discover -s tests`.

## Microphone echo (mic_echo.py)

`mic_echo.py` plays a microphone straight out of an output device, so you hear yourself on the speaker. It
reconnects by itself if the mic or the speaker drops out. Stop it with Ctrl+C.

```
uv run mic_echo.py --list
uv run mic_echo.py --in "Microphone (USB" --out "Speakers (USB" --gain 0.3
```

- `--list` lists the microphones and output devices, then exits
- `--in "<part of name>"` picks the microphone (default: Windows default)
- `--out "<part of name>"` picks the output device (default: Windows default)
- `--gain <n>` sets the loudness from 0 to 2 (default 0.3)
- `--seconds <n>` stops after that many seconds (default: run until Ctrl+C)
- `--api "<name>"` picks the audio API (default `Windows WASAPI`, the lowest delay; `MME` is a fallback)

The name given to `--in` or `--out` must match exactly one device. Keep the mic pointed away from the speaker,
or it will howl.

## Screenshot

![James' Radio](docs/screenshot.png)

## Note

Made for James by his dad and AI, shared as is. More from his garage: [jamesgarage.github.io](https://jamesgarage.github.io/).

## License

[MIT](LICENSE)
