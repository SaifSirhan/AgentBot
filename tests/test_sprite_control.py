"""The assistant-facing sprite API: bus, tool handler, schema and catalog."""
import threading
import unittest

import agent
import native_tools
import prompt_catalog
import sprite_bus
from tool_registry import InputValidationError


class SpriteBusTests(unittest.TestCase):
    """A tool runs on a worker, so the bus is the only safe way across."""

    def tearDown(self):
        sprite_bus.unregister()

    def test_nothing_is_attached_by_default(self):
        self.assertFalse(sprite_bus.is_attached())
        self.assertFalse(sprite_bus.sprite_controller.play("fly_around", {}))

    def test_a_registered_listener_receives_the_action_and_params(self):
        seen = []
        sprite_bus.register(lambda action, params: seen.append((action, params)))
        self.assertTrue(
            sprite_bus.sprite_controller.play("fly_to", {"x": 3, "y": 4}))
        self.assertEqual(seen, [("fly_to", {"x": 3, "y": 4})])

    def test_unregister_only_detaches_the_listener_it_names(self):
        first = lambda action, params: None      # noqa: E731
        second = lambda action, params: None     # noqa: E731
        sprite_bus.register(first)
        sprite_bus.unregister(second)
        self.assertTrue(sprite_bus.is_attached())
        sprite_bus.unregister(first)
        self.assertFalse(sprite_bus.is_attached())

    def test_a_dead_listener_cannot_take_the_tool_turn_down(self):
        def boom(action, params):
            raise RuntimeError("window closed mid-turn")
        sprite_bus.register(boom)
        self.assertFalse(sprite_bus.dispatch("fly_around", {}))

    def test_dispatch_from_a_worker_thread_reaches_the_listener(self):
        seen = []
        done = threading.Event()

        def listener(action, params):
            seen.append((action, params))
            done.set()

        sprite_bus.register(listener)
        worker = threading.Thread(
            target=lambda: sprite_bus.dispatch("pop_out", {}))
        worker.start()
        self.assertTrue(done.wait(2.0), "the worker never reached the listener")
        worker.join(2.0)
        self.assertEqual(seen, [("pop_out", {})])


class SpriteControlToolTests(unittest.TestCase):
    """`sprite_control` forwards the command and answers in plain language."""

    def setUp(self):
        self.seen = []
        sprite_bus.register(
            lambda action, params: self.seen.append((action, params)))

    def tearDown(self):
        sprite_bus.unregister()

    def test_a_valid_action_is_forwarded(self):
        result = agent.sprite_control({"action": "fly_around"})
        self.assertEqual(self.seen, [("fly_around", {})])
        self.assertIn("flying around", result)

    def test_scale_is_forwarded_and_reported_back(self):
        result = agent.sprite_control({"action": "set_scale", "scale": 1.5})
        self.assertEqual(self.seen, [("set_scale", {"scale": 1.5})])
        self.assertIn("1.50", result)

    def test_the_anchor_is_forwarded_without_the_action_key(self):
        agent.sprite_control({"action": "set_position", "anchor": "top_left"})
        self.assertEqual(self.seen, [("set_position", {"anchor": "top_left"})])

    def test_an_unknown_action_is_rejected_without_dispatching(self):
        result = agent.sprite_control({"action": "teleport"})
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("fly_to", result, "the error should list the real actions")
        self.assertEqual(self.seen, [])

    def test_a_missing_window_gets_an_honest_error(self):
        sprite_bus.unregister()
        self.assertEqual(agent.sprite_control({"action": "pop_out"}),
                         "ERROR: the desktop pet is not on screen.")


class SpriteControlSchemaTests(unittest.TestCase):
    """The tool must be advertised, validated, and shaped for native calling."""

    def test_the_tool_is_advertised_everywhere_it_must_be(self):
        self.assertIn("sprite_control", agent.VALID_TOOLS)
        self.assertIn("sprite_control", agent.OBJECT_INPUT_TOOLS)
        self.assertIsNotNone(agent.TOOL_REGISTRY.get("sprite_control"))
        self.assertIn("sprite_control", prompt_catalog.DESCRIPTIONS)
        self.assertIn("sprite_control", prompt_catalog.EXTRA_KEYWORDS)

    def test_the_schema_accepts_every_documented_shape(self):
        spec = agent.TOOL_REGISTRY.get("sprite_control")
        for value in ({"action": "fly_around"},
                      {"action": "fly_to", "x": 10, "y": 20},
                      {"action": "set_scale", "scale": 1.5},
                      {"action": "set_position", "anchor": "top_left"}):
            with self.subTest(value=value):
                spec.validate(value)

    def test_the_schema_rejects_unknown_actions_anchors_and_ranges(self):
        spec = agent.TOOL_REGISTRY.get("sprite_control")
        for value in ({"action": "teleport"},
                      {"action": "set_position", "anchor": "middle"},
                      {"action": "set_scale", "scale": 9.0},
                      {"action": "set_scale", "scale": "big"},
                      {}):
            with self.subTest(value=value):
                with self.assertRaises(InputValidationError):
                    spec.validate(value)

    def test_native_callers_get_an_object_with_a_required_action(self):
        specs = native_tools.build_specs(["sprite_control"],
                                         agent.TOOL_REGISTRY)
        parameters = specs[0]["function"]["parameters"]
        self.assertEqual(parameters["type"], "object")
        self.assertIn("action", parameters["properties"])
        self.assertEqual(parameters["required"], ["action"])
        self.assertFalse(parameters["additionalProperties"])


class SpriteRelayTests(unittest.TestCase):
    """The Qt hop the GUI installs: a worker's command must land on the GUI
    thread, never execute inside the worker."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_a_worker_command_is_queued_to_the_gui_thread(self):
        from agent_gui_qt import _SpriteRelay

        relay = _SpriteRelay()
        seen = []
        called_on = []
        relay.command.connect(
            lambda action, params: (seen.append((action, params)),
                                    called_on.append(threading.current_thread())))
        worker = threading.Thread(
            target=lambda: relay.forward("fly_around", {}))
        worker.start()
        worker.join(2.0)
        self.assertEqual(seen, [], "the worker ran the slot itself")
        self.app.processEvents()
        self.assertEqual(seen, [("fly_around", {})])
        self.assertIs(called_on[0], threading.main_thread())


if __name__ == "__main__":
    unittest.main()
