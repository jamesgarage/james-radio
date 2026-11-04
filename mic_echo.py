# /// script
# requires-python = ">=3.10"
# dependencies = ["sounddevice", "numpy"]
# ///
"""Mic echo: play a microphone straight out of an output device, reconnecting when either drops.

    uv run mic_echo.py --list
    uv run mic_echo.py                                  # Windows default mic -> default output
    uv run mic_echo.py --in "USB Advanced" --out "Speakers (Bose" --gain 0.3

Stop with Ctrl+C. Keep the mic away from the speaker, or it howls.
"""
import argparse
import time

import numpy as np
import sounddevice as sd

STALL_SECONDS = 1.5      # no audio callbacks for this long = the stream died (e.g. Bluetooth dropped)
SILENCE_SECONDS = 5      # exact zeros from the mic for this long = its stream died (real mics hiss)


def stamp():
    return time.strftime("%H:%M:%S")


def refresh():
    """Re-scan devices: PortAudio only knows the device list from when it started."""
    sd._terminate()
    sd._initialize()


def api_index(name):
    for i, api in enumerate(sd.query_hostapis()):
        if api["name"].lower() == name.lower():
            return i
    raise LookupError(f"no audio API named {name!r}; try one of {[a['name'] for a in sd.query_hostapis()]}")


def find(part, kind, api):
    """Device index on this API whose name contains `part`; the API's default when `part` is empty."""
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    if not part:
        i = sd.query_hostapis(api)[f"default_{kind}_device"]
        if i < 0:
            raise LookupError(f"Windows has no default {kind} device")
        return i
    hits = [i for i, d in enumerate(sd.query_devices())
            if d["hostapi"] == api and d[key] > 0 and part.lower() in d["name"].lower()]
    if len(hits) != 1:
        raise LookupError(f"{len(hits)} {kind} devices match {part!r}")
    return hits[0]


def open_stream(mic_part, out_part, api_name, gain, state):
    api = api_index(api_name)
    mic, out = find(mic_part, "input", api), find(out_part, "output", api)
    out_ch = min(2, sd.query_devices(out)["max_output_channels"])

    def passthrough(indata, outdata, frames, when, status):
        now = time.monotonic()
        state["last_callback"] = now
        if status:
            state["glitches"] += 1
        if np.any(indata):
            state["last_sound"] = now
        # mono mic to every output channel; tanh soft-clips instead of crackling when loud
        outdata[:] = np.tanh(indata[:, :1] * gain)

    now = time.monotonic()
    state.update(opened=now, last_callback=now, last_sound=now)
    # WASAPI shared mode only opens at the device's own rate unless auto_convert resamples for us
    extra = (sd.WasapiSettings(auto_convert=True),) * 2 if "wasapi" in api_name.lower() else None
    stream = sd.Stream(device=(mic, out), samplerate=48000, channels=(1, out_ch), dtype="float32",
                       latency="low", callback=passthrough, extra_settings=extra)
    stream.start()
    return stream, f"'{sd.query_devices(mic)['name']}' -> '{sd.query_devices(out)['name']}'"


def close(stream):
    if stream is not None:
        try:
            stream.abort()
            stream.close()
        except sd.PortAudioError:
            pass


def main():
    p = argparse.ArgumentParser(description="Play a microphone through an output device.")
    p.add_argument("--list", action="store_true", help="list the microphones and output devices")
    p.add_argument("--in", dest="mic", default="", help="part of the microphone's name (default: Windows default)")
    p.add_argument("--out", default="", help="part of the output device's name (default: Windows default)")
    p.add_argument("--gain", type=float, default=0.3, help="loudness, 0-2 (default 0.3)")
    p.add_argument("--seconds", type=float, default=0, help="stop after this long (default: until Ctrl+C)")
    p.add_argument("--api", default="Windows WASAPI", help="audio API (WASAPI = lowest delay; MME as a fallback)")
    a = p.parse_args()

    if a.list:
        api = api_index(a.api)
        for d in sd.query_devices():
            if d["hostapi"] == api:
                print(f"  {'mic   ' if d['max_input_channels'] else 'output'}  {d['name']}")
        return

    gain = max(0.0, min(a.gain, 2.0))
    state = {"glitches": 0}
    stream, waiting, reconnects = None, False, 0
    silence_limit = SILENCE_SECONDS
    started = time.monotonic()
    try:
        while not a.seconds or time.monotonic() - started < a.seconds:
            if stream is None:
                try:
                    stream, label = open_stream(a.mic, a.out, a.api, gain, state)
                    print(f"{stamp()} Echoing {label} at gain {gain}. Ctrl+C to stop.", flush=True)
                    waiting = False
                except (LookupError, sd.PortAudioError) as e:
                    if not waiting:
                        print(f"{stamp()} Waiting for the devices ({e}); retrying every 2 s.", flush=True)
                        waiting = True
                    time.sleep(2)
                    refresh()
                    continue

            time.sleep(0.2)
            now = time.monotonic()
            if state["last_sound"] > state["opened"]:
                silence_limit = SILENCE_SECONDS      # sound came back; a muted mic backs off below
            reason = None
            if not stream.active:
                reason = "the audio stream stopped"
            elif now - state["last_callback"] > STALL_SECONDS:
                reason = f"no audio for {STALL_SECONDS} s"
            elif now - state["last_sound"] > silence_limit:
                reason = f"the mic sent pure silence for {silence_limit} s (muted?)"
                silence_limit = min(silence_limit * 2, 60)
            if reason:
                print(f"{stamp()} Reconnecting: {reason}.", flush=True)
                close(stream)
                stream = None
                reconnects += 1
                refresh()
    except KeyboardInterrupt:
        pass
    finally:
        close(stream)
    print(f"{stamp()} Stopped. Reconnects: {reconnects}. Audio glitches: {state['glitches']}.")


if __name__ == "__main__":
    main()
