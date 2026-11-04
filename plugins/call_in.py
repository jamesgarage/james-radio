"""Call in: the phone rings, you talk for five seconds, and the radio puts you on the air."""
import time

TALK_SECONDS = 5


def register(radio):
    def call_in():
        if radio.record(0) is None:
            return "No microphone"            # say so and do nothing else
        for _ in range(2):                     # ring ring
            radio.tone(440, 0.4)
            radio.tone(480, 0.4)
            time.sleep(0.7)
        radio.tone(880, 0.15)                  # the beep that means "talk now"
        time.sleep(0.3)
        voice = radio.record(TALK_SECONDS)
        if voice is None:
            return "No microphone"
        for freq in (523, 659, 784):           # the station's sting...
            radio.tone(freq, 0.16)
            time.sleep(0.13)
        time.sleep(0.3)
        radio.play(voice)                      # ...and you are on the air
        time.sleep(TALK_SECONDS)               # one call at a time: the button is busy until playback ends
        return "You were on the air!"

    radio.add_button("📞 Call in", "#ff4f8b", call_in)
