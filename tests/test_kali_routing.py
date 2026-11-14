"""Kali tools appear only on security turns.

Run: python -m unittest tests.test_kali_routing -v
"""
import importlib.util
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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

    def test_security_turn_states_kali_task_is_the_shell(self):
        """The model must not claim it has no shell for security work.

        "Run an nmap scan on 127.0.0.1" once drew "no shell tool for it this
        turn" while kali_task was exposed and policy-allowed: the tool list was
        never the blocker, the wording was. The rule names the binaries so the
        mapping from a tool request to kali_task is not left to inference.
        """
        with mock.patch.object(
                agent.memory, "get_user_model_block", return_value=""):
            prompt = agent._build_plan_prompt(
                "run nmap on 192.168.56.10", "")
        self.assertIn("MUST use kali_task", prompt)
        self.assertIn("never say you lack a shell tool", prompt)
        for binary in ("nmap", "masscan", "ffuf", "gobuster", "sqlmap"):
            self.assertIn(binary, prompt)

    def test_normal_turn_never_mentions_the_kali_shell(self):
        """The mandate belongs to the security route, not the static prompt."""
        with mock.patch.object(
                agent.memory, "get_user_model_block", return_value=""):
            prompt = agent._build_plan_prompt("hello there", "")
        self.assertNotIn("MUST use kali_task", prompt)
        self.assertNotIn("lack a shell tool", prompt)

    def test_security_prompt_mentions_the_task_tool(self):
        with mock.patch.object(
                agent.memory, "get_user_model_block", return_value=""):
            prompt = agent._build_plan_prompt(
                "run nmap on 192.168.56.10", "")
        self.assertIn("kali_task", prompt)
        self.assertIn("kali_scope", prompt)

    def test_security_turn_renders_the_step_cap_it_enforces(self):
        # The schema's nested maxItems is a wire detail the model is not told
        # to read; the prose is where it learns how long a plan may be. Without
        # it the budget could only be met by hiding a limit that is enforced.
        schema = agent.TOOL_REGISTRY.get("kali_task").input_schema
        with mock.patch.object(
                agent.memory, "get_user_model_block", return_value=""):
            prompt = agent._build_plan_prompt(
                "run nmap on 192.168.56.10", "")
        self.assertIn("up to %d steps" % schema["properties"]["steps"]["maxItems"],
                      prompt)

    def test_security_turn_cost_fits_the_static_prompt_budget(self):
        """The 4,100 promise is about the static prompt; a security turn may add
        only what the tools cost, so the delta is what is bound here."""
        with mock.patch.object(
                agent.memory, "get_user_model_block", return_value=""):
            normal = agent._build_plan_prompt("hello", "")
            security = agent._build_plan_prompt(
                "run nmap on 192.168.56.10", "")
        delta = (len(security) - len(normal)) / 4
        self.assertLessEqual(delta, 512,
                             "security turn adds ~%.0f tokens" % delta)
        self.assertIn("kali_task", security)


if __name__ == "__main__":
    unittest.main()


class PromptToolDriftTests(unittest.TestCase):
    """Every binary the prompt text advertises must be one the wrapper permits.

    On 2026-11-15 the security rule named `sqlmap` and `hydra` while
    `classify()` had no entry for either, so the model was instructed to reach
    for a tool it could not get and answered "no shell tool for it this turn" --
    the exact failure the rule exists to prevent. The enumeration is matched by
    its shape and the test requires it to find tools, so a reworded rule fails
    instead of quietly checking nothing.
    """

    def load_wrapper(self):
        # Test-only load by path: production code never imports the Kali-side
        # wrapper, which is deployed on the VM, not on this machine.
        spec = importlib.util.spec_from_file_location(
            "kestrel_run_drift_guard",
            ROOT / "kali" / "kestrel_run.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        return module

    def advertised_binaries(self, wrapper):
        text = " ".join((prompt_router.RULES["security"],
                         prompt_catalog.DESCRIPTIONS["kali_task"]))
        found = []
        for run in re.findall(
                r"([a-z][a-z0-9_-]+(?:\s*,\s*[a-z][a-z0-9_-]+){2,})"
                r"\s+or any other Kali binary", text):
            found.extend(token.strip() for token in run.split(","))
        self.assertGreaterEqual(len(found), 8,
                                "the security rule no longer enumerates tools")
        return found

    def test_every_advertised_binary_is_permitted_not_denied(self):
        wrapper = self.load_wrapper()
        permitted = (wrapper.RECON | wrapper.INTRUSIVE | wrapper.CREDENTIAL
                     | wrapper.LOCAL_SAFE)
        for binary in self.advertised_binaries(wrapper):
            with self.subTest(binary=binary):
                self.assertIn(binary, permitted | wrapper.DENIED_BINARIES)

    def test_advertised_binaries_reach_a_real_tier(self):
        # Naming a tool the policy refuses is how the model ends up apologising
        # about shells, so refusal is not an acceptable answer for the rule text.
        wrapper = self.load_wrapper()
        permitted = wrapper.RECON | wrapper.INTRUSIVE | wrapper.CREDENTIAL
        for binary in self.advertised_binaries(wrapper):
            with self.subTest(binary=binary):
                self.assertIn(binary, permitted)
