"""Tests for the volume and media-control tools in agent.py.

No real audio device is changed and no media key is pressed.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent  # noqa: E402


class FakeEndpoint:
    def __init__(self, level=0.5, muted=0):
        self.level = level
        self.muted = muted
        self.calls = []

    def GetMasterVolumeLevelScalar(self):
        return self.level

    def GetMute(self):
        return self.muted

    def SetMute(self, muted, context):
        self.muted = muted
        self.calls.append(("mute", muted))

    def SetMasterVolumeLevelScalar(self, level, context):
        self.level = level
        self.calls.append(("level", level))


class ModernDevice:
    def __init__(self, endpoint):
        self.EndpointVolume = endpoint

    def Activate(self, *args):
        raise AssertionError(
            "modern pycaw AudioDevice must use EndpointVolume, not Activate")


class LegacyDevice:
    def Activate(self, iid, context, none):
        return object()


class VolumeInterfaceTests(unittest.TestCase):
    def test_uses_endpoint_volume_property_on_modern_pycaw(self):
        endpoint = FakeEndpoint()
        with mock.patch.object(agent, "AudioUtilities") as utilities:
            utilities.GetSpeakers.return_value = ModernDevice(endpoint)
            self.assertIs(agent._get_volume_interface(), endpoint)

    def test_falls_back_to_activate_on_legacy_pycaw(self):
        sentinel = object()
        with mock.patch.object(agent, "AudioUtilities") as utilities, \
                mock.patch.object(agent, "cast", return_value=sentinel), \
                mock.patch.object(agent, "POINTER", side_effect=lambda _: _):
            utilities.GetSpeakers.return_value = LegacyDevice()
            self.assertIs(agent._get_volume_interface(), sentinel)


class SetVolumeTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = FakeEndpoint()
        self.patches = [
            mock.patch.object(agent, "PYCAW_AVAILABLE", True),
            mock.patch.object(
                agent, "_get_volume_interface", return_value=self.endpoint),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_sets_a_percentage_and_unmutes(self):
        result = agent.set_volume("50")
        self.assertEqual(result, "Volume set to 50%.")
        self.assertEqual(self.endpoint.calls, [("mute", 0), ("level", 0.5)])

    def test_accepts_a_percent_sign(self):
        result = agent.set_volume("25%")
        self.assertEqual(result, "Volume set to 25%.")
        self.assertAlmostEqual(self.endpoint.level, 0.25)

    def test_clamps_to_one_hundred(self):
        result = agent.set_volume("150")
        self.assertEqual(result, "Volume set to 100%.")
        self.assertAlmostEqual(self.endpoint.level, 1.0)

    def test_mutes(self):
        self.assertEqual(agent.set_volume("mute"), "Muted.")
        self.assertEqual(self.endpoint.muted, 1)

    def test_unmutes(self):
        self.endpoint.muted = 1
        self.assertEqual(agent.set_volume("unmute"), "Unmuted.")
        self.assertEqual(self.endpoint.muted, 0)


class MediaControlTests(unittest.TestCase):
    def test_maps_actions_to_media_keys(self):
        expected = {
            "play": "playpause",
            "play_pause": "playpause",
            "next": "nexttrack",
            "prev": "prevtrack",
            "stop": "stop",
        }
        with mock.patch.object(agent.pyautogui, "press") as press:
            for action, key in expected.items():
                self.assertEqual(
                    agent.media_control(action), f"Media: {action}.")
                press.assert_called_with(key)


if __name__ == "__main__":
    unittest.main()
