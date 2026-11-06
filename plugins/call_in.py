"""Phone call: the phone rings, you talk for five seconds, and the radio puts you on the air. STOP hangs up."""
import threading

TALK_SECONDS = 5


def register(radio):
    hung_up = threading.Event()   # STOP: every wait below ends at once and the call is over

    def call_in():
        hung_up.clear()
        if radio.record(0) is None:
            return "No microphone"            # say so and do nothing else
        for _ in range(2):                     # ring ring
            radio.tone(440, 0.4)
            radio.tone(480, 0.4)
            if hung_up.wait(0.7):
                return None
        radio.tone(880, 0.15)                  # the beep that means "talk now"
        if hung_up.wait(0.3):
            return None
        voice = radio.record(TALK_SECONDS)     # STOP ends this early too
        if hung_up.is_set():
            return None
        if voice is None:
            return "No microphone"
        for freq in (523, 659, 784):           # the station's sting...
            radio.tone(freq, 0.16)
            if hung_up.wait(0.13):
                return None
        if hung_up.wait(0.3):
            return None
        radio.play(voice)                      # ...and you are on the air
        if hung_up.wait(TALK_SECONDS):         # one call at a time: the button is busy until playback ends
            return None
        return "You were on the air!"

    radio.add_button("📞 Phone call", "#ff4f8b", call_in, page="Station", on_stop=hung_up.set)
