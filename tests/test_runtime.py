"""Regression tests. Run from app: python -B -m unittest discover -s tests -v"""
import asyncio
import concurrent.futures
from contextlib import closing
import importlib.util
import io
import json
import os
import sqlite3
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

import websockets
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
SANDBOX = tempfile.TemporaryDirectory(prefix="ark-runtime-test-")
ROOT = Path(SANDBOX.name)
ENV = patch.dict(os.environ, {
    "QQ_BOT_DATA_DIR": str(ROOT / "data"), "QQ_BOT_USER_LOG_DIR": str(ROOT / "legacy"),
    "QQ_BOT_SETTINGS_FILE": str(ROOT / "settings.json"), "QQ_BOT_API_CONFIG": str(ROOT / "api.json"),
    "QQ_BOT_PROMPT_CONFIG": str(ROOT / "prompts.json"), "QQ_BOT_LOG_FILE": str(ROOT / "bot.log"),
    "QQ_BOT_PLUGIN_DIR": str(APP / "plugins"), "QQ_BOT_WS_TOKEN": "unit-test-gateway",
    "QQ_BOT_QQ": "1234", "QQ_BOT_ADMIN_QQ": "3333",
    "QQ_BOT_SERVER_CHAN_KEY": "",
})
ENV.start()

import part_command
import part_media
import part_plugin
import part_prompt
import part_record
from part_runtime import ScopeScheduler, authenticated, message_event

spec = importlib.util.spec_from_file_location("ark_runtime_test_bot", APP / "QQ-BOT.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


def tearDownModule():
    ENV.stop()
    SANDBOX.cleanup()


class FakeCollection:
    def __init__(self):
        self.docs = {}
        self.fail_delete = False

    def add(self, *, ids, embeddings, documents, metadatas):
        self.docs.update(zip(ids, documents))

    def delete(self, *, ids):
        if self.fail_delete:
            raise RuntimeError("test unavailable")
        for uid in ids:
            self.docs.pop(uid, None)

    def query(self, **kwargs):
        return {"ids": [list(self.docs)], "documents": [list(self.docs.values())],
                "distances": [[0.01] * len(self.docs)], "metadatas": [[{} for _ in self.docs]]}


class StorageTests(unittest.TestCase):
    def test_schema_upgrade_preserves_legacy_rows(self):
        db = ROOT / "legacy.db"
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE chat_history(user_id TEXT,ts REAL,role TEXT,content TEXT)")
            conn.execute("INSERT INTO chat_history VALUES ('4501',1,'user','old row')")
            conn.execute("CREATE TABLE memory_meta(doc_id TEXT PRIMARY KEY,user_id TEXT,type TEXT,"
                         "valence REAL,arousal REAL,importance REAL,ts REAL)")
            conn.execute("INSERT INTO memory_meta VALUES ('old','4501','dialogue',0,0,0.5,1)")
            conn.commit()
        with patch.object(part_record, "DB_FILE", str(db)):
            part_record._init_db()
            part_record._init_db()
            self.assertEqual(part_record.load_user_memory("4501")["chat_history"], [{"role": "user", "content": "old row"}])
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("SELECT doc_id,turn_id FROM memory_meta").fetchone(), ("old", None))

    def test_concurrent_settings_and_profile_fields(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda item: part_record.patch_settings({item: True}), ("one", "two", "three")))
        self.assertTrue(all(part_record.load_settings()[k] for k in ("one", "two", "three")))
        part_record.set_favorability("3001", 91)
        part_record.set_user_persona("3001", "profile")
        self.assertEqual(part_record.load_user_memory("3001")["favorability"], 91)
        part_record.patch_settings({"default_mode": "聊天"})
        self.assertEqual(part_record.toggle_user_mode("3001"), "扮演")

    def test_alias_stability_and_scope_isolation(self):
        first = part_record.touch_group_archives(102, 3010, "同名")
        other = part_record.touch_group_archives(102, 3011, "同名")
        self.assertNotEqual(first, other)
        self.assertEqual(first, part_record.touch_group_archives(102, 3010, "改名"))
        self.assertEqual(part_record.load_user_memory("group_102_3010")["display_aliases"], ["同名"])
        self.assertNotEqual(first, part_record.touch_group_archives(103, 3010, "改名"))
        self.assertIsNone(part_record.load_user_memory("3010").get("speaker_id"))

    def test_clear_during_embedding_and_orphan_filter(self):
        scope = "3002"
        col = FakeCollection()
        old_event = time.time() - 1
        with patch.object(part_record, "_get_collection", return_value=col), \
             patch.object(part_record, "_embed", return_value=[[1.0]]) as embed:
            def reset(_):
                part_record.clear_user_memory(scope)
                return [[1.0]]
            embed.side_effect = reset
            self.assertIsNone(part_record.add_vector(scope, "old", expected_epoch=part_record.get_epoch(scope)))
            self.assertFalse(col.docs)
            embed.side_effect = None
            uid = part_record.add_vector(scope, "confirmed")
            self.assertIsNotNone(uid)
            col.fail_delete = True
            part_record.clear_user_memory(scope)
            self.assertIn(uid, col.docs)
            self.assertEqual(part_record.recall([scope], "confirmed"), [])
            self.assertEqual(part_record.search_vectors([scope], "confirmed"), [])
            self.assertIsNone(part_record.get_epochs((scope,), received_at=old_event))
            self.assertEqual(part_record.get_epochs((scope,), received_at=time.time() + 1), {scope: 2})

    def test_regeneration_does_not_delete_other_history(self):
        scope, member = "group_103", "group_103_2001"
        epochs = part_record.get_epochs((scope, member))
        part_record.append_history(scope, "user", "A question", 100)
        old, _ = part_record.commit_chat_turn(scope, member, "A question", 100, "A old", 101, epochs)
        part_record.append_history(scope, "user", "B unrelated", 102)
        self.assertIsNone(part_record.latest_confirmed_turn(scope, "group_103_2002"))
        new, _ = part_record.commit_chat_turn(scope, member, "A question", 100, "A new", 103, epochs, old)
        self.assertNotEqual(old, new)
        content = [m["content"] for m in part_record.load_user_memory(scope)["chat_history"]]
        self.assertEqual(content, ["A question", "B unrelated", "A new"])

    def test_character_selection_and_length_inherited(self):
        files = [{"file": "a.txt", "enabled": True, "character": True},
                 {"file": "b.txt", "enabled": False, "character": True}]
        part_prompt.select_character(files, "b.txt")
        with patch.object(part_prompt, "load_config", return_value=files), \
             patch.object(part_prompt, "_read_file", side_effect=str):
            self.assertEqual(part_prompt.load_prompts().strip(), "b.txt")
            self.assertEqual(part_prompt.load_prompts(include_character=False), "")
        text = bot.build_user_prompt("1", {}, settings={"default_mode": "聊天", "max_words": 41})
        self.assertIn("41", text)

    def test_plugin_zip_valid_entries_and_rejections(self):
        self.assertIsNotNone(part_plugin._safe_relative("assets/panel.html"))
        self.assertIsNone(part_plugin._safe_relative("assets/../panel.html"))
        self.assertIsNone(part_plugin._safe_relative("C:panel.html"))
        self.assertIsNone(part_plugin._safe_relative("assets/CON.txt"))
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("PANEL.html", "one")
            archive.writestr("panel.html", "two")
        with self.assertRaises(ValueError):
            part_plugin._zip_entries(data.getvalue())
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("Demo/manifest.json", json.dumps({
                "id": "safe-panel", "name": "test", "api_version": 1, "enabled": True,
            }))
            archive.writestr("Demo/index.html", "<html>ok</html>")
        folder = ROOT / "install"
        with patch.object(part_plugin, "PLUGIN_DIR", str(folder)):
            result = part_plugin.install_plugin_zip(data.getvalue())
        manifest = json.loads((folder / "safe-panel" / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["enabled"])
        self.assertTrue(result["has_panel"])

    @unittest.skipUnless(importlib.util.find_spec("httpx"), "install requirements-dev.txt for WebUI smoke")
    def test_webui_read_auth_and_secret_masking(self):
        import httpx
        import part_web
        part_record.patch_settings({"tavily_api_key": "fake-unit-test-key"})
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=part_web.app), base_url="http://localhost") as client:
                self.assertEqual((await client.get("/api/settings")).status_code, 401)
                login = await client.get("/", auth=("admin", "fake-test-web-token"))
                self.assertEqual(login.status_code, 200)
                settings = (await client.get("/api/settings")).json()
                self.assertEqual(settings["tavily_api_key"], "")
                self.assertTrue(settings["tavily_key_configured"])
                self.assertEqual((await client.post("/api/settings", json={"at_only": False},
                                                    headers={"Origin": "https://other.invalid"})).status_code, 403)
                response = await client.post("/api/settings", json={"max_words": 38})
                self.assertEqual(response.json()["max_words"], 38)
                response = await client.post("/api/plugins/install", content=b"x" * (part_plugin.MAX_ZIP_BYTES + 1))
                self.assertEqual(response.status_code, 413)
        with patch.dict(os.environ, {"QQ_BOT_WEB_TOKEN": "fake-test-web-token"}):
            asyncio.run(check())

    def test_gateway_message_and_media_validation(self):
        event = {"post_type": "message", "message_type": "group", "user_id": "3333",
                 "group_id": "100", "self_id": 1234, "message_id": -100, "message": []}
        self.assertTrue(part_command.is_admin(message_event(event, 1234), [3333]))
        self.assertIsNone(message_event({**event, "self_id": 2}, 1234))
        self.assertIsNone(message_event({**event, "group_id": True}, 1234))
        headers = Headers([("Authorization", "Bearer demo"), ("Authorization", "Bearer duplicate")])
        self.assertFalse(authenticated(type("Request", (), {"headers": headers})(), "demo"))
        with self.assertRaises(ValueError):
            part_media._safe_url("http://127.0.0.1/image.png")
        self.assertEqual(part_media.image_mime(b"\x89PNG\r\n\x1a\n"), "image/png")


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_scope_fifo_and_parallelism(self):
        scheduler = ScopeScheduler(lambda _: None, max_pending=2)
        release = asyncio.Event()
        separate = asyncio.Event()
        seen = []
        async def work(value):
            if value == "a1":
                await release.wait()
            seen.append(value)
            if value == "b1":
                separate.set()
        try:
            self.assertTrue(scheduler.submit("A", work, "a1"))
            self.assertTrue(scheduler.submit("A", work, "a2"))
            self.assertFalse(scheduler.submit("A", work, "a3"))
            self.assertTrue(scheduler.submit("B", work, "b1"))
            await asyncio.wait_for(separate.wait(), 1)
            self.assertEqual(seen, ["b1"])
            release.set()
            await scheduler.run("A", lambda: asyncio.sleep(0))
            self.assertEqual(seen, ["b1", "a1", "a2"])
        finally:
            await scheduler.close()

    async def test_real_handshake_and_ack_transport(self):
        part_command.set_gateway_call(bot.call_onebot)
        async with websockets.serve(bot.handler, "127.0.0.1", 0, process_request=bot._gateway_handshake) as http:
            url = f"ws://127.0.0.1:{http.sockets[0].getsockname()[1]}"
            with self.assertRaises(InvalidStatus) as rejected:
                await websockets.connect(url)
            self.assertEqual(rejected.exception.response.status_code, 401)
            headers = {"Authorization": "Bearer unit-test-gateway"}
            async with websockets.connect(url, additional_headers=headers) as peer:
                with self.assertRaises(InvalidStatus) as second:
                    await websockets.connect(url, additional_headers=headers)
                self.assertEqual(second.exception.response.status_code, 409)
                accepted = asyncio.create_task(part_command.send_private(bot._active_ws, 4500, "[CQ:literal]"))
                request = json.loads(await asyncio.wait_for(peer.recv(), 1))
                self.assertEqual(request["params"]["message"], [{"type": "text", "data": {"text": "[CQ:literal]"}}])
                await peer.send(json.dumps({"echo": request["echo"], "status": "ok", "data": {"message_id": 940}}))
                self.assertEqual(await accepted, 940)
                pending = asyncio.create_task(part_command.send_private(bot._active_ws, 4500, "disconnected"))
                await asyncio.wait_for(peer.recv(), 1)
                await peer.close()
                with self.assertRaises(RuntimeError):
                    await asyncio.wait_for(pending, 1)

    async def test_live_gateway_keeps_two_group_targets_and_replies_separate(self):
        part_record.patch_settings({"sticker_enabled": False, "search_enabled": False, "reply_cooldown_at": 0})
        first_started = asyncio.Event()
        release = asyncio.Event()
        async def generate(uid, message, *args):
            if "--A--" in message:
                first_started.set()
                await release.wait()
                return "answer A"
            return "answer B"

        def event(group, sender, mid, text):
            return {"post_type": "message", "self_id": bot.BOT_QQ, "message_type": "group",
                    "group_id": group, "user_id": sender, "message_id": mid,
                    "sender": {"nickname": "同名"},
                    "message": [{"type": "at", "data": {"qq": bot.BOT_QQ}},
                                {"type": "text", "data": {"text": text}}]}

        async def next_reply(peer, group, text):
            action = json.loads(await asyncio.wait_for(peer.recv(), 3))
            self.assertEqual(action["action"], "send_group_msg")
            self.assertEqual(action["params"]["group_id"], group)
            self.assertEqual(action["params"]["message"][-1]["data"]["text"], text)
            await peer.send(json.dumps({"echo": action["echo"], "status": "ok", "retcode": 0,
                                       "data": {"message_id": group + 20}}))

        part_command.set_gateway_call(bot.call_onebot)
        with patch.object(bot, "call_your_api", side_effect=generate), \
             patch.object(bot, "summarize_memory", AsyncMock()), \
             patch.object(bot.plugin_manager, "apply_message_hooks", AsyncMock(return_value=None)), \
             patch.object(bot.plugin_manager, "emit_hook", AsyncMock()):
            async with websockets.serve(bot.handler, "127.0.0.1", 0, process_request=bot._gateway_handshake) as server:
                url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                async with websockets.connect(url, additional_headers={"Authorization": "Bearer unit-test-gateway"}) as peer:
                    try:
                        await peer.send(json.dumps(event(120, 4101, 541, "--A--")))
                        await asyncio.wait_for(first_started.wait(), 3)
                        await peer.send(json.dumps(event(121, 4102, 542, "--B--")))
                        await next_reply(peer, 121, "answer B")
                        release.set()
                        await next_reply(peer, 120, "answer A")
                        for scope in ("group_120", "group_121"):
                            await bot._conversation_jobs.run(scope, lambda: asyncio.sleep(0))
                            await bot._sticker_jobs.run(scope, lambda: asyncio.sleep(0))
                        for scope in ("group_120", "group_120_4101", "group_121", "group_121_4102"):
                            await bot._summary_jobs.run(scope, lambda: asyncio.sleep(0))
                    finally:
                        release.set()
        a = part_record.latest_confirmed_turn("group_120", "group_120_4101")
        b = part_record.latest_confirmed_turn("group_121", "group_121_4102")
        self.assertEqual((a["input_id"], b["input_id"]), ("541", "542"))

    async def test_active_job_is_cancelled_with_its_caller(self):
        scheduler = ScopeScheduler(lambda _: None)
        entered = asyncio.Event()
        stopped = asyncio.Event()
        async def job():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()
        task = asyncio.create_task(scheduler.run("A", job))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(stopped.wait(), 1)
        await scheduler.close()

    async def test_reply_commit_only_after_ack_and_favor_revision(self):
        scope, ws = "4011", object()
        epoch = part_record.get_epochs((scope,))
        stats = {"sticker_enabled": False}
        part_record.set_favorability(scope, 15)
        with patch.object(bot, "_active_ws", ws), \
             patch.object(bot, "call_your_api", AsyncMock(return_value="answer")), \
             patch.object(bot, "_send_text", AsyncMock(side_effect=RuntimeError("not acked"))):
            with self.assertRaises(RuntimeError):
                await bot.reply_chat(ws, 4011, None, "question", 123, stats, epoch)
        self.assertIsNone(part_record.latest_confirmed_turn(scope))
        with patch.object(bot, "_active_ws", ws), \
             patch.object(bot, "call_your_api", AsyncMock(return_value="answer")), \
             patch.object(bot, "_send_text", AsyncMock(return_value=1001)), \
             patch.object(bot, "summarize_memory", AsyncMock()), \
             patch.object(bot.plugin_manager, "emit_hook", AsyncMock()):
            self.assertTrue(await bot.reply_chat(ws, 4011, None, "question", 123, stats, epoch))
            await bot._summary_jobs.run(scope, lambda: asyncio.sleep(0))
            await bot._sticker_jobs.run("private_4011", lambda: asyncio.sleep(0))
        turn_id = part_record.latest_confirmed_turn(scope)["id"]
        with patch.object(bot, "load_internal_prompt", return_value="prompt"), \
             patch.object(bot, "_post_json", AsyncMock(return_value='{"favorability_delta":3,"importance":0}')):
            part_record.set_favorability(scope, 99)
            await bot.summarize_memory(scope, [{"role": "user", "content": "q"}], "q", epoch[scope], turn_id, True, 1)
            self.assertEqual(part_record.load_user_memory(scope)["favorability"], 99)
            await bot.summarize_memory(scope, [{"role": "user", "content": "q"}], "q", epoch[scope], turn_id, True, 2)
            self.assertEqual(part_record.load_user_memory(scope)["favorability"], 100)

    async def test_existing_plugins_and_panel_rpc(self):
        items = {item["id"]: item for item in part_plugin.inspect_plugins()}
        self.assertIn("theme-arctic", items)
        self.assertEqual(items["theme-arctic"]["type"], "theme")
        self.assertEqual(items["theme-arctic"]["error"], "")
