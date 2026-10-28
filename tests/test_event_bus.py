import json
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import config
import event_bus


class EventBusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.messages = []
        self.finished = threading.Event()
        self.calls = []
        patcher = mock.patch.object(logging, "FileHandler", return_value=logging.NullHandler())
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(config, "load_config", return_value={
            **config.DEFAULTS, "AGENT_MEMORY_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def bus(self, triggers, sources=False):
        def run(prompt, history, **kwargs):
            self.calls.append((prompt, history, kwargs))
            return ["Action: chat(event) -> Result: AI: summary"]

        def result(text):
            self.messages.append(text)
            self.finished.set()

        bus = event_bus.EventBus({"TRIGGERS": triggers}, run_turn=run, on_result=result)
        if not sources:
            patcher = mock.patch.object(bus, "_start_sources")
            patcher.start()
            self.addCleanup(patcher.stop)
        bus.start()
        self.addCleanup(bus.stop)
        return bus

    def file_trigger(self, suffix=".txt"):
        return {"id": "downloads", "type": "file_watch", "path": str(self.root),
                "suffix": suffix, "prompt": "Read this file and summarize it."}

    def test_empty_config_starts_no_background_threads(self):
        bus = event_bus.EventBus({"TRIGGERS": []})
        thread = mock.Mock()
        with mock.patch.object(event_bus, "threading", SimpleNamespace(Thread=thread)):
            self.assertFalse(bus.start())
        thread.assert_not_called()
        bus.stop()

    def test_matching_file_runs_isolated_turn_with_exact_path_and_text(self):
        path = self.root / "invoice.txt"
        path.write_text("Invoice total 42", encoding="utf-8")
        bus = self.bus([self.file_trigger()])
        self.assertEqual(bus.publish("file_watch", path=str(path)), 1)
        self.assertTrue(self.finished.wait(5))
        self.assertEqual(len(self.calls), 1)
        prompt, history, kwargs = self.calls[0]
        self.assertIn("Invoice total 42", prompt)
        self.assertIn(json.dumps(str(path))[1:-1], prompt)
        self.assertEqual(history, [])
        self.assertIs(kwargs["read_only"], True)
        self.assertIs(kwargs["group_mode"], False)
        self.assertIs(kwargs["stop_event"], bus.stop_event)
        self.assertIn("summary", self.messages[-1])

    def test_file_filter_rejects_wrong_suffix_outside_path_and_directories(self):
        bus = self.bus([self.file_trigger()])
        for path in (self.root / "other.pdf", self.root.parent / "outside.txt", self.root):
            self.assertEqual(bus.publish("file_watch", path=str(path)), 0)
        self.assertEqual(self.calls, [])

    def test_duplicate_file_event_is_not_queued_twice(self):
        path = self.root / "invoice.txt"
        path.write_text("Invoice", encoding="utf-8")
        bus = self.bus([self.file_trigger()])
        self.assertEqual(bus.publish("file_watch", path=str(path)), 1)
        self.assertEqual(bus.publish("file_watch", path=str(path)), 0)
        self.assertTrue(self.finished.wait(5))
        self.assertEqual(len(self.calls), 1)

    def test_telegram_requires_matching_chat_sender_and_text(self):
        bus = self.bus([{"id": "tg", "type": "telegram_listen", "chat_ids": [12],
                         "sender_ids": [34], "contains": "invoice", "prompt": "Summarize message"}])
        base = {"chat_id": 12, "sender_id": 34, "message_id": 5, "text": "New INVOICE"}
        for change in ({"chat_id": 99}, {"sender_id": 99}, {"out": True}, {"text": "hello"}):
            self.assertEqual(bus.publish("telegram_listen", **(base | change)), 0)
        self.assertEqual(bus.publish("telegram_listen", **base), 1)
        self.assertEqual(bus.publish("telegram_listen", **base), 0)
        self.assertTrue(self.finished.wait(2))
        self.assertEqual(len(self.calls), 1)

    def test_battery_triggers_once_on_crossing_and_not_below(self):
        bus = self.bus([{"id": "battery", "type": "battery_watch", "target": 90,
                         "prompt": "Report battery status"}])
        self.assertEqual(bus.publish("battery_watch", percent=89), 0)
        self.assertEqual(bus.publish("battery_watch", percent=91), 1)
        self.assertEqual(bus.publish("battery_watch", percent=100), 0)
        self.assertTrue(self.finished.wait(2))
        self.assertEqual(len(self.calls), 1)

    def test_stop_prevents_new_jobs_and_cancels_turns(self):
        bus = self.bus([{"id": "b", "type": "battery_watch", "target": 100, "prompt": "Report"}])
        bus.stop()
        self.assertTrue(bus.stop_event.is_set())
        self.assertEqual(bus.publish("battery_watch", percent=100), 0)
        self.assertEqual(self.calls, [])

    def test_invalid_configuration_fails_closed(self):
        for trigger in ({"id": "bad", "type": "unknown", "prompt": "x"},
                        {"id": "b", "type": "battery_watch", "target": "nan", "prompt": "x"},
                        {"id": "tg", "type": "telegram_listen", "chat_ids": [], "sender_ids": [1], "prompt": "x"},
                        {"id": "f", "type": "file_watch", "path": str(self.root), "suffix": ".exe", "prompt": "x"}):
            with self.subTest(trigger=trigger), self.assertRaises(ValueError):
                event_bus.EventBus({"TRIGGERS": [trigger]})

    def test_real_folder_observer_emits_created_file(self):
        bus = self.bus([self.file_trigger()], sources=True)
        path = self.root / "fresh.txt"
        path.write_text("Real watchdog event", encoding="utf-8")
        self.assertTrue(self.finished.wait(8), self.messages)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("Real watchdog event", self.calls[0][0])
        bus.stop()

    def test_worker_reports_errors_and_keeps_processing(self):
        bus = self.bus([self.file_trigger()])
        with mock.patch.object(bus, "_file_context", side_effect=ValueError("Malformed PDF")):
            path = self.root / "bad.txt"
            path.write_text("bad", encoding="utf-8")
            bus.publish("file_watch", path=str(path))
            self.assertTrue(self.finished.wait(2))
        self.assertIn("Malformed PDF", self.messages[-1])
        self.assertEqual(self.calls, [])
        self.finished.clear()
        path = self.root / "good.txt"
        path.write_text("good", encoding="utf-8")
        bus.publish("file_watch", path=str(path))
        self.assertTrue(self.finished.wait(5))
        self.assertEqual(len(self.calls), 1)

    def test_pdf_extracts_actual_text_before_dispatch(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 20 100 Td (Invoice total 42) Tj ET")
        page[NameObject("/Contents")] = stream
        path = self.root / "invoice.pdf"
        writer.write(path)
        bus = self.bus([self.file_trigger(".pdf")])
        bus.publish("file_watch", path=str(path))
        self.assertTrue(self.finished.wait(5), self.messages)
        self.assertIn("Invoice total 42", self.calls[0][0])
        self.assertNotIn("%PDF", self.calls[0][0])

    def test_queue_is_bounded_and_rejected_events_are_reported(self):
        bus = event_bus.EventBus({"TRIGGERS": [{"id": "tg", "type": "telegram_listen",
            "chat_ids": [1], "sender_ids": [2], "prompt": "Summarize"}]}, on_result=self.messages.append)
        counts = [bus.publish("telegram_listen", chat_id=1, sender_id=2, message_id=n, text="Hi")
                  for n in range(40)]
        self.assertEqual(sum(counts), 32)
        self.assertTrue(any("queue full" in text for text in self.messages))
        self.assertEqual(len(self.messages), 1, "Overflow must not flood GUI messages")
        bus.stop()

    def test_pending_file_events_are_bounded_and_stop_discards_them(self):
        bus = event_bus.EventBus({"TRIGGERS": [self.file_trigger()]}, on_result=self.messages.append)
        queued = 0
        for index in range(40):
            path = self.root / f"file-{index}.txt"
            path.write_text("content", encoding="utf-8")
            queued += bus.publish("file_watch", path=str(path))
        self.assertEqual(queued, 32)
        self.assertEqual(len(bus._pending_files), 32)
        self.assertEqual(len(self.messages), 1)
        bus.stop()
        self.assertFalse(bus.start())
        self.assertEqual(self.calls, [])

    def test_real_folder_observer_handles_completed_rename(self):
        bus = self.bus([self.file_trigger()], sources=True)
        path = self.root / "download.partial"
        path.write_text("Completed renamed file", encoding="utf-8")
        final = path.with_suffix(".txt")
        path.rename(final)
        self.assertTrue(self.finished.wait(5), self.messages)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("Completed renamed file", self.calls[0][0])
        bus.stop()

    def test_result_callback_failure_is_contained(self):
        bus = event_bus.EventBus({"TRIGGERS": []},
                                 on_result=mock.Mock(side_effect=RuntimeError("GUI closed")))
        with self.assertLogs("event_bus", level="ERROR") as logs:
            bus._emit("summary")
        self.assertIn("GUI closed", "\n".join(logs.output))

    def test_enabled_must_be_boolean(self):
        with self.assertRaises(ValueError):
            event_bus.EventBus({"TRIGGERS": [dict(self.file_trigger(), enabled="false")]})

    def test_telegram_adapter_filters_and_disconnects_without_network(self):
        import asyncio
        import telethon
        settings = {"TRIGGERS": [{"id": "tg", "type": "telegram_listen", "chat_ids": [12],
                    "sender_ids": [34], "prompt": "Summarize"}], "TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "test"}
        bus = event_bus.EventBus(settings)
        client = mock.MagicMock()
        client.connect = mock.AsyncMock()
        client.disconnect = mock.AsyncMock()
        client.is_user_authorized = mock.AsyncMock(return_value=True)

        async def incoming():
            handler = client.add_event_handler.call_args.args[0]
            await handler(SimpleNamespace(chat_id=12, sender_id=34, id=8, raw_text="Hello", out=False))
            await handler(SimpleNamespace(chat_id=99, sender_id=34, id=9, raw_text="Blocked", out=False))

        client.run_until_disconnected = incoming
        with mock.patch.object(telethon, "TelegramClient", return_value=client):
            asyncio.run(bus._listen_telegram())
        self.assertEqual(bus._jobs.qsize(), 1)
        client.disconnect.assert_awaited()
        client.remove_event_handler.assert_called_once()
        bus.stop()

    def test_incomplete_created_file_is_retried_when_writing_finishes(self):
        bus = self.bus([self.file_trigger()], sources=True)
        path = self.root / "download.txt"
        path.write_bytes(b"\xff")
        self.assertTrue(self.finished.wait(5))
        self.assertIn("ERROR:", self.messages[-1])
        self.finished.clear()
        path.write_text("Completed download", encoding="utf-8")
        self.assertTrue(self.finished.wait(5), self.messages)
        self.assertIn("Completed download", self.calls[0][0])
        bus.stop()

    def test_telegram_stop_cancels_pending_connection(self):
        import asyncio
        import telethon
        bus = event_bus.EventBus({"TRIGGERS": [], "TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "test"})
        client = mock.MagicMock()
        client.disconnect = mock.AsyncMock()
        cancelled = []

        async def exercise():
            entered = asyncio.Event()

            async def connect():
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(True)

            client.connect = connect
            task = asyncio.create_task(bus._listen_telegram())
            await asyncio.wait_for(entered.wait(), timeout=1)
            bus.stop_event.set()
            await asyncio.wait_for(task, timeout=1)

        with mock.patch.object(telethon, "TelegramClient", return_value=client):
            asyncio.run(exercise())
        self.assertEqual(cancelled, [True])
        client.disconnect.assert_awaited()
        client.is_user_authorized.assert_not_called()

    def test_battery_source_reads_psutil_and_stops(self):
        sensors = SimpleNamespace(sensors_battery=mock.Mock(return_value=SimpleNamespace(percent=100)))
        with mock.patch.object(event_bus, "psutil", sensors):
            bus = self.bus([{"id": "b", "type": "battery_watch", "target": 100, "prompt": "Report"}], sources=True)
            self.assertTrue(self.finished.wait(2))
            bus.stop()
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
