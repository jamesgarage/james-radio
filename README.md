# James' Radio

James' Radio is a toy soundboard for Windows: 24 synthesized sounds on big glowing pads, a second page, STATION,
of radio-show pads, and a third, PLAY, of animals, a silly voice and sing-along tunes (below). Every sound is
generated with numpy, so there are no sound files; the station's voice is made once with Windows' own speech. The
only downloads are the ones uv makes on the first run (Python and two libraries, see Requirements).

Tap a pad or press its key. The keys follow the grid's shape on the keyboard and play the page you see:

| Row | Keys | Sounds |
|---|---|---|
| 1 | `1 2 3 4 5 6` | Air horn, Applause, Ba-dum-tss, Drum roll, Ta-da!, Cha-ching |
| 2 | `Q W E R T Y` | Ding!, Buzzer, Sad trombone, Siren, Car horn, Countdown |
| 3 | `A S D F G H` | Fart, Boing, Slide whistle, Bubbles, Crickets, Ghost |
| 4 | `Z X C V B N` | Laser, Explosion, Robot, Power up, Magic, Record scratch |

**→** and **←** turn the page (so do a sideways swipe and the SOUNDS, STATION and PLAY tabs). **M** mutes the
microphone and puts it back on the air (below). **Space** (or Esc) stops all sounds. The page also has a STOP button, a volume fader (0-150%) and a level meter.

Python plays the sound through one audio stream. The radio itself is a small web page served on 127.0.0.1
only (unless you share it with a phone, below), opened in a Microsoft Edge app window (or your default browser
if Edge is missing). Closing the window ends the program. The page fits a laptop screen in one piece and a phone
or tablet in either orientation.

## Requirements

- Windows 10 or 11 (the sound goes through the Windows WASAPI audio API)
- Python 3.10 or newer, run through [uv](https://docs.astral.sh/uv/getting-started/installation/). uv installs
  Python and the two libraries (numpy, sounddevice) by itself on the first run.
- A sound output: speakers or headphones
- For the STATION page's voice: Windows' built-in speech, which comes with Windows. Without it those pads play
  without the voice.

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
- `--mic-off` starts with the microphone muted; the MIC pad puts it on the air.

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

- The phone is a remote control. The sound comes out of the PC, and Phone call records with the PC's microphone.
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
with these:

- **Phone call** (`plugins/call_in.py`, on the STATION page): a phone rings, a beep says "talk now", the
  microphone records five seconds, and the radio plays a short station sting and then puts the caller on the air.
  With no microphone the screen says so and nothing else happens.
- **Animals** (`plugins/animals.py`, on the PLAY page): a cow, a duck, a cat and an owl, each made with math
  from harmonics and mouth shapes, no recordings. There is no dog: no bark made this way sounded like one.
- **Silly voice** (`plugins/silly_voice.py`, on the PLAY page): a beep-boop says "talk now", the microphone
  records three seconds, and the radio plays it back higher and quicker, like a chipmunk. It plays after
  recording, never live. With no microphone the screen says so.
- **Sing-along** (`plugins/sing_along.py`, on the PLAY page): each press plays the next tune on a music box:
  Twinkle Twinkle Little Star, Old MacDonald Had a Farm, Row, Row, Row Your Boat and Happy Birthday. Old
  public domain melodies, the notes only; each is under 15 seconds, and the screen shows its name. A press while
  a tune plays is ignored, and STOP stops it.

To write your own, see [docs/plugins.md](docs/plugins.md). Plugins never touch the network. Tests:
`uv run --with numpy --with sounddevice python -m unittest discover -s tests` (no audio device is used;
plain `py -3 -m unittest discover -s tests` also works and skips the mic tests, which need numpy).

## The STATION page

Swipe the pads sideways for the radio-show page, or tap the STATION tab, or press **→**. Swipe back, tap SOUNDS or
press **←** to return. On a phone or tablet a little hand shows the swipe until you have done it once. The display
shows the page's name, and the MIC, STOP and volume work the same on both pages.

| Key | Pad | What it plays |
|---|---|---|
| `1` | Station ID | a three-note logo, then "You're listening to James' Radio!" |
| `2` | Sponsors | "And now, our sponsors!", then a cheesy jingle that ends with a cash register |
| `3` | Music | "Here's some music!", then 12 seconds of upbeat music that goes round until you press STOP (or the pad again) |
| `4` | Weather | "Here's the weather" over rain and wind, then thunder |
| `Q` | Breaking news | a whoosh and a big hit, then "Breaking news!" over alert beeps |
| `W` | Phone call | the phone rings, the caller talks for five seconds, then goes on the air (Plugins, below) |
| `E` | Applause | the first page's applause, for the caller |
| `R` | Sign off | "That's all for today, goodnight!", then a soft chime |

The voice is Windows' own speech voice. The radio makes its six lines the first time it starts (a second or two)
and keeps them in a `cache/` folder next to `radio.py`; delete the folder to make them again. Making them only
writes files, nothing is played. If Windows speech does not work on the PC, those pads play without the voice, and
the display says "No voice on this PC" the first time.

## The PLAY page

The third page, after STATION. It has no pads of its own: the plugins above fill it, and without them the page
is not there.

| Key | Pad | What it plays |
|---|---|---|
| `1` | Cow | moo |
| `2` | Duck | quack, quack, quack |
| `3` | Cat | a kitten's meow |
| `4` | Owl | hoo, hoo-hoo, hooo |
| `Q` | Silly voice | records three seconds and plays it back as a chipmunk |
| `W` | Sing-along | the next nursery tune on a music box |

Every sound on it is levelled to be no louder than the pads.

## The MIC pad

While the radio runs, the PC's microphone is on the air: you hear yourself out of the same speaker as the pads,
a moment later. The display says ON AIR and shows the delay, and the level meter moves with your voice. The big
MIC pad is the mute: tap or click it (or press **M**) and it says MUTED; do it again and the mic is back on the
air. A phone playing the radio can press it too; it is always the PC's microphone.

- Tap the speaker name under the display to choose the microphone (Windows default first), its loudness
  (default 30%) and the delay from the mic to the speaker (0 to 2000 ms, default 350 ms; 0 is straight
  through).
- If the speaker starts to howl, the radio mutes the mic for 2 seconds and the display says "too loud, move the
  mic". Point the mic away from the speaker, or turn it down.
- With no microphone plugged in, the pad says NO MIC and does nothing. A microphone plugged in later goes on
  the air by itself (unless the pad was muted).
- The microphone is open only while it is on the air, and closing the radio takes it off. A Bluetooth headset's
  mic switches the headset to phone-call sound quality while it is on; that is Bluetooth, not the radio.

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
