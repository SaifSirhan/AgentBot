"""Kali tools appear only on security turns.

Run: python -m unittest tests.test_kali_routing -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent
import prompt_catalog
import prompt_router


class RoutingTests(unittest.TestCase):
    def setUp(self):
        agent._clear_turn_route()

    def test_security_tools_are_advertised(self):
        for name in ("kali_task", "kali_scope"):
            self.assertIn(name, prompt_catalog.DESCRIPTIONS)
            self.assertIn(name, prompt_catalog.EXTRA_KEYWORDS)

    def test_security_tools_are_in_the_catalog(self):
        tools = prompt_catalog.build_tools(frozenset(agent.VALID_TOOLS))
        by_name = {tool.name: tool for tool in tools}
        self.assertIn("security", by_name["kali_task"].rule_groups)
        self.assertIn("security", by_name["kali_scope"].rule_groups)

    def test_security_intent_matches_kali_terms(self):
        self.assertIn("security",
                      prompt_router.matching_intents("run nmap on the lab"))
        self.assertIn("security",
                      prompt_router.matching_intents("kali scan the target"))
        self.assertNotIn("security",
                         prompt_router.matching_intents("hello there"))

    def test_security_turns_expose_kali_tools(self):
        prompt, selected = agent._route_prompt(
            "run nmap on 192.168.56.10")
        self.assertIn("kali_task", {tool.name for tool in selected})
        self.assertIn("kali_task", prompt)

    def test_natural_security_phrasings_reach_the_kali_tools(self):
        """A user asks for the work, not for the binary.

        "Run nmap" routed but "scan my lab for open ports" did not, so the
        tools were unreachable for the most natural way to ask.
        """
        for text in ("scan my lab for open ports",
                     "port scan the lab host",
                     "scan 192.168.56.10",
                     "run a vulnerability assessment",
                     "enumerate services on the VM"):
            with self.subTest(text=text):
                _, selected = agent._route_prompt(text)
                self.assertIn("kali_task",
                              {tool.name for tool in selected})

    def test_ordinary_scans_do_not_reach_the_kali_tools(self):
        """The wider security pattern must not attach to everyday scanning."""
        for text in ("scan this document for typos",
                     "scan the QR code",
                     "scan my receipts",
                     "scan the network for my printer"):
            with self.subTest(text=text):
                _, selected = agent._route_prompt(text)
                self.assertNotIn("kali_task",
                                 {tool.name for tool in selected})
    def test_normal_turns_hide_kali_tools(self):
        prompt, selected = agent._route_prompt("hello there")
        self.assertNotIn("kali_task", {tool.name for tool in selected})
        self.assertNotIn("kali_task", prompt)

    def test_static_prompt_stays_in_budget_and_hides_kali(self):
        with mock.patch.object(
                agent.memory, "get_user_model_block", return_value=""):
            prompt = agent._build_plan_prompt("hello", "")
        self.assertNotIn("kali_task", prompt)
        self.assertNotIn("kali_task", agent.TOOL_DESCRIPTIONS)
        self.assertLess(len(prompt) / 4, 4100)

    def test_security_prompt_mentions_the_task_tool(self):
        with mock.patch.object(
                agent.memory, "get_user_model_block", return_value=""):
            prompt = agent._build_plan_prompt(
                "run nmap on 192.168.56.10", "")
        self.assertIn("kali_task", prompt)
        self.assertIn("kali_scope", prompt)


if __name__ == "__main__":
    unittest.main()
