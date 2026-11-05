"""Page server tests: a second radio never shares the first one's port (--share uses a fixed one).

Standard library only:  py -3 -m unittest discover -s tests
Uses the fake sounddevice from test_speakers, so no audio device is opened.
"""
import socket
import unittest

from test_speakers import World, plugins, radio


class Port(unittest.TestCase):
    def test_a_taken_port_is_never_shared(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        api = plugins.Radio(["Ding!"], lambda s: None, lambda f, s: None, lambda s: None, log=lambda line: None)
        mixer, sounds = World().mixer, [("Ding!", "x", [0.0] * 480)]
        first = radio.make_server(mixer, sounds, api, "127.0.0.1", port)[0]
        try:
            second = radio.make_server(mixer, sounds, api, "127.0.0.1", port)[0]   # a second Wi-Fi launch
            try:
                self.assertEqual(first.server_address[1], port)
                self.assertNotEqual(second.server_address[1], port, "two radios answering on one port")
            finally:
                second.server_close()
        finally:
            first.server_close()


if __name__ == "__main__":
    unittest.main()
