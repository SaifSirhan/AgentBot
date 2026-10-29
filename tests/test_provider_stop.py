"""Offline provider/normal-turn cancellation regressions with event-gated workers."""
from contextlib import ExitStack
import os
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_event_turn import ROOT, load_source


class ProviderStopTests(unittest.TestCase):
    def setUp(self):
        # Same isolated source-loader harness as test_event_turn: no user config,
        # integrations, memory initialization, network, or desktop operations.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "APPDATA": temp.name, "LOCALAPPDATA": temp.name,
            "AGENT_MEMORY_DIR": temp.name, "AGENTBOT_MOOD_FILE": "",
            "META_AI_API_KEY": "", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_USER_ID": "",
        }))
        config = load_source("provider_stop_config", "config.py")
        config.load_config = lambda: {"AGENT_MEMORY_DIR": temp.name}
        config.save_config = mock.Mock(side_effect=AssertionError("config write"))
        names = (
            "requests winsound weather_news bs4 PIL PIL.ImageGrab selenium "
            "selenium.webdriver selenium.webdriver.common selenium.webdriver.common.by "
            "selenium.webdriver.common.keys selenium.webdriver.chrome "
            "selenium.webdriver.chrome.options selenium.webdriver.chrome.service "
            "webdriver_manager webdriver_manager.chrome pyautogui watcher gmail_tool "
            "scheduler memory screen security_tools air_quality telegram_user "
            "pyperclip psutil pycaw pycaw.pycaw comtypes pygetwindow winotify yt_dlp "
            "evals rag_tool experiential_memory"
        ).split()
        modules = {name: mock.MagicMock(name=name) for name in names}
        modules["config"] = config
        self.stack.enter_context(mock.patch.dict(sys.modules, modules))
        self.stack.enter_context(mock.patch.object(sys, "path", [str(ROOT)] + sys.path))
        self.a = load_source("provider_stop_agent", "agent.py")
        self.stack.enter_context(mock.patch.dict(sys.modules, {"agent": self.a}))
        self.a.BRAIN_PRIORITY = ["first", "second"]
        self.a.LAST_BRAIN = self.a._LAST_PROVIDER = "previous"
        self.a._LAST_FAILURE_REASON = "previous failure"
        self.a.note_tool_result = mock.Mock()
        self.a._generate_skill_from_turn = mock.Mock(return_value="")
        self.a.save_mood = mock.Mock(side_effect=AssertionError("mood write"))
        modules["rag_tool"].search_documents.return_value = ""
        self.workers, self.callers, self.releases, self.stops = [], [], [], []
        real_thread = threading.Thread

        def make_worker(*args, **kwargs):
            worker = real_thread(*args, **kwargs)
            self.workers.append(worker)
            return worker

        self.stack.enter_context(mock.patch.object(self.a, "_threading", SimpleNamespace(
            Thread=make_worker, Event=threading.Event, Lock=threading.Lock,
            RLock=threading.RLock)))
        self.addCleanup(self.cleanup_threads)

    def cleanup_threads(self):
        for event in self.stops + self.releases:
            event.set()
        for thread in self.callers + self.workers:
            if thread.ident is not None:
                thread.join(3)
                self.assertFalse(thread.is_alive(), "test thread did not exit")

    def start_call(self, stop, fn=None):
        result = SimpleNamespace(values=[], errors=[], entered=threading.Event())
        self.stops.append(stop)

        def run():
            result.entered.set()
            try:
                result.values.append((fn or (lambda: self.a._call_llm(
                    "hello", stop_event=stop)))())
            except BaseException as exc:
                result.errors.append(exc)

        result.thread = threading.Thread(target=run, daemon=True)
        self.callers.append(result.thread)
        result.thread.start()
        self.assertTrue(result.entered.wait(1))
        return result

    def assert_cancelled(self, call):
        call.thread.join(1)
        self.assertFalse(call.thread.is_alive(), "cancelled caller still blocked")
        self.assertEqual(call.values, [])
        self.assertEqual(len(call.errors), 1)
        self.assertIsInstance(call.errors[0], InterruptedError)

    def assert_brain_unchanged(self):
        self.assertEqual(self.a.LAST_BRAIN, "previous")
        self.assertEqual(self.a._LAST_PROVIDER, "previous")
        self.assertEqual(self.a._LAST_FAILURE_REASON, "previous failure")
        self.assertFalse(any(s["dead"] or s["cooldown_until"] for s in self.a._brain_state.values()))

    def test_preset_explicit_and_threadlocal_stop_never_dispatch(self):
        stop = threading.Event()
        stop.set()
        with mock.patch.object(self.a, "_dispatch_call") as dispatch:
            for explicit in (False, True):
                self.a._set_stop_event(stop)
                kwargs = {"stop_event": stop} if explicit else {}
                with self.assertRaises(InterruptedError):
                    self.a._call_llm("hello", **kwargs)
            dispatch.assert_not_called()
        self.assert_brain_unchanged()
        self.assertEqual(self.workers, [])

    def test_stop_after_provider_success_error_empty_or_exception_never_fails_over(self):
        for outcome in ("late", "Error: HTTP 503", "", RuntimeError("offline")):
            with self.subTest(outcome=outcome):
                stop = threading.Event()

                def dispatch(*_args):
                    stop.set()
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome

                with mock.patch.object(self.a, "_dispatch_call", side_effect=dispatch) as provider:
                    with self.assertRaises(InterruptedError):
                        self.a._call_llm("hello", stop_event=stop)
                    provider.assert_called_once()
                self.assert_brain_unchanged()

    def test_cancelled_http_remains_single_inflight_and_late_result_is_discarded(self):
        entered, release, stop = threading.Event(), threading.Event(), threading.Event()
        self.releases.append(release)
        seen, contexts, cleared = [], [], []
        clear_stop = self.a._clear_stop_event
        self.a._ROUTED.allowed = {"private_turn_tool"}

        def dispatch(name, *_args):
            seen.append(name)
            contexts.append((self.a._current_stop_event(), dict(self.a._ROUTED.__dict__)))
            entered.set()
            if not release.wait(5):
                raise AssertionError("fake HTTP was not released")
            return "Error: HTTP 503"

        def clear():
            clear_stop()
            cleared.append(self.a._current_stop_event())

        with mock.patch.object(self.a, "_dispatch_call", side_effect=dispatch), \
                mock.patch.object(self.a, "_clear_stop_event", side_effect=clear):
            call = self.start_call(stop)
            self.assertTrue(entered.wait(1), "provider never entered")
            stop.set()
            self.assert_cancelled(call)
            self.assertEqual(len(self.workers), 1)
            self.assertTrue(self.workers[0].is_alive())
            self.assertTrue(self.workers[0].daemon)
            self.assertEqual(contexts, [(stop, {})])
            for _ in range(3):
                retry_stop = threading.Event()
                retry = self.start_call(retry_stop)
                retry_stop.set()
                self.assert_cancelled(retry)
                self.assertEqual(len(self.workers), 1, "stops leaked provider threads")
            self.assertEqual(seen, ["first"])
            release.set()
            self.workers[0].join(2)
            self.assertFalse(self.workers[0].is_alive())
        self.assertEqual(cleared, [None])
        self.assert_brain_unchanged()
        self.assertEqual(self.a._ROUTED.allowed, {"private_turn_tool"})
        with mock.patch.object(self.a, "_dispatch_call", return_value="new result"):
            self.assertEqual(self.a._call_llm("new", stop_event=threading.Event()), "new result")

    def test_success_and_failover_work_with_and_without_stop_event(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                caller = threading.get_ident()
                threads, names = [], []

                def dispatch(name, *_args):
                    names.append(name)
                    threads.append(threading.get_ident())
                    return "" if name == "first" else "answer"

                with mock.patch.object(self.a, "_dispatch_call", side_effect=dispatch):
                    stop = threading.Event() if asynchronous else None
                    self.assertEqual(self.a._call_llm("hello", stop_event=stop), "answer")
                self.assertEqual(names, ["first", "second"])
                self.assertEqual(all(t == caller for t in threads), not asynchronous)
                self.assertEqual(self.a._LAST_PROVIDER, "second")
                self.assertIsNone(self.a._LAST_FAILURE_REASON)

    def test_explicit_stop_overrides_stale_threadlocal_stop_and_direct_wrapper_forwards(self):
        stale, active = threading.Event(), threading.Event()
        stale.set()
        self.a._set_stop_event(stale)
        with mock.patch.object(self.a, "_dispatch_call", return_value="ok"):
            self.assertEqual(self.a.ask_llm_direct("hello", stop_event=active), "ok")
        active.set()
        with self.assertRaises(InterruptedError):
            self.a.ask_llm_direct("hello", stop_event=active)

    def test_interrupted_provider_exception_is_not_converted_to_failover(self):
        for stop in (None, threading.Event()):
            with mock.patch.object(self.a, "_dispatch_call", side_effect=InterruptedError("stop")) as dispatch:
                with self.assertRaises(InterruptedError):
                    self.a._call_llm("hello", stop_event=stop)
                dispatch.assert_called_once()

    def test_openrouter_request_preserves_payload_and_http_timeout(self):
        self.a.OPENROUTER_API_KEY = "test-key"
        post = self.a.requests.post
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"choices": [{"message": {"content": "answer"}}]}
        self.a._ROUTED.allowed = {"search_web"}
        self.assertEqual(self.a._call_llm("catalog and goal", force_json=True, max_tokens=42,
                                         force_provider="openrouter", stop_event=threading.Event()), "answer")
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["json"]["messages"], [{"role": "user", "content": "catalog and goal"}])
        self.assertEqual(kwargs["json"]["response_format"], {"type": "json_object"})
        self.assertEqual(kwargs["json"]["max_tokens"], 42)
        self.assertEqual(kwargs["timeout"], self.a.OLLAMA_TIMEOUT)

    def test_action_retry_stops_before_retry_after_parser_cancellation(self):
        stop = threading.Event()
        self.a._set_stop_event(stop)

        def parse(_text):
            stop.set()
            return None, "invalid"

        with mock.patch.object(self.a, "ask_ai_for_plan", return_value="invalid") as plan, \
                mock.patch.object(self.a, "parse_action", side_effect=parse):
            with self.assertRaises(InterruptedError):
                self.a.get_valid_action("hello", "")
            plan.assert_called_once()

    def test_action_retry_discards_a_plan_returned_after_stop(self):
        stop = threading.Event()
        self.a._set_stop_event(stop)

        def plan(*_args, **_kwargs):
            stop.set()
            return "late answer"

        with mock.patch.object(self.a, "ask_ai_for_plan", side_effect=plan), \
                mock.patch.object(self.a, "parse_action") as parse:
            with self.assertRaises(InterruptedError):
                self.a.get_valid_action("hello", "")
            parse.assert_not_called()

    def test_normal_turn_verification_fault_fails_closed(self):
        with mock.patch.object(self.a, "get_valid_action", return_value=({"tool": "chat", "input": "I saved the file."}, None)), \
                mock.patch.object(self.a, "execute_tool", return_value="AI: I saved the file."), \
                mock.patch.object(self.a.verification_adapter, "build_engine", side_effect=RuntimeError("broken")):
            log = self.a._run_agent_turn_impl("hello", [])
        self.assertNotIn("I saved the file.", "\n".join(log))
        self.assertIn(self.a.verification_adapter.ERROR_REPLY, "\n".join(log))

    def test_verification_build_and_reply_receive_shared_stop(self):
        stop = threading.Event()
        engine = object()
        with mock.patch.object(self.a, "get_valid_action", return_value=({"tool": "chat", "input": "hello"}, None)), \
                mock.patch.object(self.a, "execute_tool", return_value="AI: hello"), \
                mock.patch.object(self.a.verification_adapter, "build_engine", return_value=engine) as build, \
                mock.patch.object(self.a.verification_adapter, "reply_is_supported", return_value=True) as reply:
            self.a._run_agent_turn_impl("hello", [], stop_event=stop)
        build.assert_called_once_with([], stop_event=stop)
        reply.assert_called_once_with(engine, "hello", stop_event=stop)

    def test_normal_turn_stop_propagates_without_fallback_or_skill_generation(self):
        for phase in ("preset", "planner", "tool", "verification", "verification_error", "queued"):
            with self.subTest(phase=phase):
                stop = threading.Event()
                if phase == "preset":
                    stop.set()

                def plan(*_args, **_kwargs):
                    if phase == "planner":
                        stop.set()
                    tool = "chat" if phase.startswith("verification") else "read_file"
                    action = {"tool": tool, "input": "notes.txt"}
                    if phase == "queued":
                        action["_queued_actions"] = [{"tool": "read_file", "input": "second"}]
                    return action, None

                def execute(action):
                    if phase == "tool" or action["input"] == "second":
                        stop.set()
                    return "data"

                def build(*_args, **_kwargs):
                    stop.set()
                    if phase == "verification_error":
                        raise RuntimeError("error racing cancellation")
                    raise InterruptedError("stop")

                with mock.patch.object(self.a, "get_valid_action", side_effect=plan), \
                        mock.patch.object(self.a, "execute_tool", side_effect=execute), \
                        mock.patch.object(self.a, "ask_llm_direct") as direct, \
                        mock.patch.object(self.a.verification_adapter, "build_engine", side_effect=build):
                    with self.assertRaises(InterruptedError):
                        self.a._run_agent_turn_impl("hello", [], stop_event=stop)
                    direct.assert_not_called()
                self.a._generate_skill_from_turn.assert_not_called()

    def test_request_tool_refunds_work_step_and_next_prompt_resumes_original_goal(self):
        plans = iter([
            ({"tool": "request_tool", "input": "type_text"}, None),
            ({"tool": "read_file", "input": "notes.txt"}, None),
            ({"tool": "chat", "input": "Here are the notes."}, None),
        ])
        prompts, executed = [], []

        def plan(prompt, context):
            prompts.append((prompt, context, self.a._ROUTED.prompt))
            return next(plans)

        def execute(action):
            executed.append(action["tool"])
            return "AI: " + action["input"] if action["tool"] == "chat" else "notes"

        with mock.patch.object(self.a, "MAX_STEPS_PER_TURN", 2), \
                mock.patch.object(self.a, "get_valid_action", side_effect=plan), \
                mock.patch.object(self.a, "execute_tool", side_effect=execute), \
                mock.patch.object(self.a, "ask_llm_direct", return_value="unwanted fallback") as direct:
            log = self.a._run_agent_turn_impl("watch a movie", [])
        self.assertEqual(executed, ["read_file", "chat"])
        self.assertEqual(len(prompts), 3)
        self.assertIn("Original request: 'watch a movie'", prompts[1][0])
        self.assertIn("continue", prompts[1][0].lower())
        self.assertIn("- type_text:", prompts[1][2])
        self.assertIn("Here are the notes.", "\n".join(log))
        direct.assert_not_called()

    def test_repeated_grants_are_bounded_by_total_decisions(self):
        with mock.patch.object(self.a, "MAX_STEPS_PER_TURN", 2), \
                mock.patch.object(self.a, "get_valid_action", return_value=({"tool": "request_tool", "input": "type_text"}, None)) as plan, \
                mock.patch.object(self.a, "execute_tool") as execute, \
                mock.patch.object(self.a, "ask_llm_direct", return_value="No work completed."):
            log = self.a._run_agent_turn_impl("watch a movie", [])
        self.assertGreater(plan.call_count, 2, "grants still consumed the work budget")
        self.assertLessEqual(plan.call_count, 6, "grant decisions were not bounded")
        execute.assert_not_called()
        self.assertIn("decision", "\n".join(log).lower())

    def test_checkpoint_protects_compaction_and_both_history_windows(self):
        protected = "User: /task build a report"
        history = [protected, "tool evidence", "User: continue"]
        checkpoint = {"status": "paused", "protected_user_message": protected}
        with mock.patch.object(self.a, "_latest_task_checkpoint", return_value=checkpoint, create=True), \
                mock.patch("context_compactor.compact_history", return_value=history) as compact, \
                mock.patch.object(self.a, "_history_window", return_value=history) as window, \
                mock.patch.object(self.a, "get_valid_action", return_value=(None, "no plan")), \
                mock.patch.object(self.a, "ask_llm_direct", return_value="No work completed."):
            self.a._run_agent_turn_impl("continue", history)
        compact.assert_called_once_with(history, task_active=True, protected_user_message=protected)
        self.assertEqual(window.call_args_list, [mock.call(history, protected_message=protected)] * 2)


if __name__ == "__main__":
    unittest.main()
