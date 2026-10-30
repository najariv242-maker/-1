import asyncio
from collections import deque
import importlib
import logging
import os
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ["ALLOWED_USER_IDS"] = ""
os.environ["PRIVATE_CHAT_ONLY"] = "true"

import bot


class BotV7Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        bot.close_db()
        bot.DB_PATH = os.path.join(self.temp_dir.name, "voice.db")
        bot.reset_runtime_state()
        bot.init_db()

    def tearDown(self):
        bot.close_db()
        self.temp_dir.cleanup()

    def test_cache_returns_copy_and_does_not_leak_mutations(self):
        record_id = bot.save_record(1, 2, "متن اصلی", user_id=7)

        first = bot.get_record(record_id)
        first["transcript"] = "تغییر بیرونی"
        second = bot.get_record(record_id)

        self.assertEqual(second["transcript"], "متن اصلی")

    def test_record_access_requires_same_user_and_chat(self):
        record_id = bot.save_record(10, 20, "متن", user_id=30)
        record = bot.get_record(record_id)

        self.assertTrue(bot.can_access_record(record, chat_id=10, user_id=30))
        self.assertFalse(bot.can_access_record(record, chat_id=10, user_id=31))
        self.assertFalse(bot.can_access_record(record, chat_id=11, user_id=30))

    def test_settings_return_isolation(self):
        first = bot.get_user_model_settings(8)
        first["thinking_level"] = "OFF"
        second = bot.get_user_model_settings(8)

        self.assertEqual(second["thinking_level"], "HIGH")

    def test_reply_change_invalidates_translation_in_cache_and_database(self):
        record_id = bot.save_record(1, 2, "source", human_reply="old", translation="old translation", user_id=1)

        bot.update_record_field(record_id, "human_reply", "new")
        self.assertIsNone(bot.get_record(record_id)["translation"])
        bot._RAM_RECORDS_BY_ID.clear()
        self.assertIsNone(bot.get_record(record_id)["translation"])

    def test_old_bot_message_mapping_does_not_match_after_update(self):
        record_id = bot.save_record(1, 2, "source", bot_msg_id=3, user_id=1)
        bot.update_record_field(record_id, "bot_msg_id", 4)

        self.assertIsNone(bot.get_record_by_msg_id(1, 3))
        self.assertEqual(bot.get_record_by_msg_id(1, 4)["record_id"], record_id)

    def test_precompute_does_not_overwrite_newer_reply(self):
        record_id = bot.save_record(1, 2, "source", human_reply="newer", user_id=1)

        bot.save_precomputed_field(record_id, "human_reply", "older")

        self.assertEqual(bot.get_record(record_id)["human_reply"], "newer")

    def test_hedge_keeps_selected_model(self):
        self.assertEqual(set(bot._candidate_models("specific-model")), {"specific-model"})

    def test_translation_cache_separates_source_and_settings(self):
        record_id = bot.save_record(1, 2, "source", user_id=1)
        first = bot.translation_cache_key(record_id, "source", {"chat_model": "m", "thinking_level": "HIGH"})
        second = bot.translation_cache_key(record_id, "reply", {"chat_model": "m", "thinking_level": "HIGH"})
        third = bot.translation_cache_key(record_id, "source", {"chat_model": "m", "thinking_level": "OFF"})
        bot.save_cached_translation(record_id, first, "translated source")
        self.assertEqual(bot.get_cached_translation(record_id, first), "translated source")
        self.assertIsNone(bot.get_cached_translation(record_id, second))
        self.assertIsNone(bot.get_cached_translation(record_id, third))

    def test_transcription_cache_is_scoped_by_user_file_and_model(self):
        bot.save_cached_transcript(1, "file-id", "model-a", "transcript")
        self.assertEqual(bot.get_cached_transcript(1, "file-id", "model-a"), "transcript")
        self.assertIsNone(bot.get_cached_transcript(2, "file-id", "model-a"))
        self.assertIsNone(bot.get_cached_transcript(1, "other-id", "model-a"))
        self.assertIsNone(bot.get_cached_transcript(1, "file-id", "model-b"))

    def test_singleflight_shares_a_running_request(self):
        async def run():
            calls = 0
            gate = asyncio.Event()
            async def work():
                nonlocal calls
                calls += 1
                await gate.wait()
                return "done"
            active = {}
            first = bot._get_or_start_task(active, 1, work)
            second = bot._get_or_start_task(active, 1, work)
            gate.set()
            results = await asyncio.gather(first, second)
            return calls, results
        calls, results = asyncio.run(run())
        self.assertEqual(calls, 1)
        self.assertEqual(results, ["done", "done"])

    def test_tts_does_not_silently_truncate_long_text(self):
        async def run():
            with patch.object(bot, "_call_single_model", new=AsyncMock(return_value=b"fake")) as model:
                with self.assertRaises(ValueError):
                    await bot.generate_speech_audio("a" * 801)
                model.assert_not_awaited()
        asyncio.run(run())

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_tts_converts_raw_pcm_to_telegram_ogg(self):
        async def run():
            with patch.object(bot, "_call_single_model", new=AsyncMock(return_value=b"\x00\x00" * 2400)):
                return await bot.generate_speech_audio("hello")
        self.assertTrue(asyncio.run(run()).startswith(b"OggS"))

    def test_repeated_voice_reuses_transcription_with_same_model(self):
        async def run():
            user = SimpleNamespace(id=42, is_bot=False)
            chat = SimpleNamespace(id=42, type="private")
            media = SimpleNamespace(file_id="file", file_unique_id="unique", file_size=10, mime_type="audio/ogg")

            def make_update(message_id):
                status = SimpleNamespace(message_id=message_id + 100, edit_text=AsyncMock())
                message = SimpleNamespace(chat_id=42, message_id=message_id, voice=media, audio=None,
                                          video_note=None, reply_text=AsyncMock(return_value=status))
                return SimpleNamespace(effective_message=message, effective_user=user, effective_chat=chat), status

            first, first_status = make_update(1)
            second, second_status = make_update(2)
            with patch.object(bot, "PRECOMPUTE_ENABLED", False), patch.object(
                bot, "_download_media_bytes", new=AsyncMock(return_value=b"audio")
            ) as download, patch.object(bot, "transcribe_audio_bytes", new=AsyncMock(return_value="سلام")) as transcribe:
                await bot.process_single_voice(first, SimpleNamespace(bot=object()))
                await bot.process_single_voice(second, SimpleNamespace(bot=object()))
                self.assertEqual(download.await_count, 1)
                self.assertEqual(transcribe.await_count, 1)
                self.assertIn("سلام", first_status.edit_text.await_args.args[0])
                self.assertIn("سلام", second_status.edit_text.await_args.args[0])
        asyncio.run(run())

    def test_status_failure_cancels_parallel_work(self):
        async def run():
            cancelled = asyncio.Event()
            async def work():
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.set()
            async def status():
                await asyncio.sleep(0)
                raise RuntimeError("telegram unavailable")
            status_task = asyncio.create_task(status())
            work_task = asyncio.create_task(work())
            with self.assertRaises(RuntimeError):
                await bot._await_status(status_task, work_task)
            self.assertTrue(cancelled.is_set())
        asyncio.run(run())

    def test_action_uses_the_text_displayed_on_its_button(self):
        record = {"transcript": "source", "human_reply": "reply"}
        voice = SimpleNamespace(reply_markup=bot.build_voice_action_keyboard(1))
        reply = SimpleNamespace(reply_markup=bot.build_reply_keyboard(1))
        self.assertEqual(bot.displayed_record_text(voice, record), "source")
        self.assertEqual(bot.displayed_record_text(reply, record), "reply")

    def test_duplicate_updates_are_suppressed_until_processing_finishes(self):
        self.assertTrue(bot.claim_message_processing(1, 2))
        self.assertFalse(bot.claim_message_processing(1, 2))
        bot.release_message_processing(1, 2)
        self.assertTrue(bot.claim_message_processing(1, 2))

    def test_log_filter_masks_mapping_arguments(self):
        masking_filter = bot.SecretMaskingFilter(["sensitive-key"])
        record = logging.LogRecord("test", logging.INFO, "file", 1, "value=%(value)s", ({"value": "sensitive-key"},), None)
        masking_filter.filter(record)
        self.assertNotIn("sensitive-key", record.getMessage())

    def test_old_database_is_migrated(self):
        bot.close_db()
        os.remove(bot.DB_PATH)
        conn = sqlite3.connect(bot.DB_PATH)
        conn.execute("CREATE TABLE voice_records (record_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, voice_msg_id INTEGER NOT NULL, transcript TEXT NOT NULL, summary TEXT, human_reply TEXT, created_at REAL NOT NULL)")
        conn.commit()
        conn.close()
        bot.init_db()
        record_id = bot.save_record(1, 2, "migrated", user_id=1)
        self.assertEqual(bot.get_record(record_id)["user_id"], 1)

    def test_legacy_private_record_is_accessible_only_to_its_chat_owner(self):
        record_id = bot.save_record(42, 2, "legacy")
        record = bot.get_record(record_id)
        self.assertTrue(bot.can_access_record(record, 42, 42))
        self.assertFalse(bot.can_access_record(record, 42, 43))

    def test_cache_eviction_removes_old_record_mapping(self):
        with patch.object(bot, "MAX_RAM_CACHE_SIZE", 1):
            first_id = bot.save_record(1, 2, "first", bot_msg_id=3, user_id=1)
            bot.save_record(1, 4, "second", bot_msg_id=5, user_id=1)
            self.assertNotIn(first_id, bot._RAM_RECORDS_BY_ID)
            self.assertNotIn((1, 3), bot._RAM_MSG_TO_RECORD_ID)

    def test_message_chunks_always_fit_telegram_limit_with_prefix_and_suffix(self):
        chunks = bot.split_text_chunks("الف" * 12000, header="📝 متن:\n\n", footer="\n\nپایان")

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= bot.TELEGRAM_MAX_MSG_LENGTH for chunk in chunks))
        self.assertTrue(chunks[0].startswith("📝 متن:"))
        self.assertTrue(chunks[-1].endswith("پایان"))

    def test_large_ogg_is_not_reencoded(self):
        audio = b"audio" * 200000

        async def run():
            return await bot.optimize_media_for_fast_upload(audio, "audio/ogg", False)

        result, mime = asyncio.run(run())

        self.assertIs(result, audio)
        self.assertEqual(mime, "audio/ogg")

    def test_rate_limiter_does_not_keep_expired_timestamps_forever(self):
        bot.RATE_LIMIT_MAX_REQUESTS = 2
        bot.RATE_LIMIT_WINDOW_SECONDS = 60
        bot._user_request_timestamps[100] = deque([0.0, 1.0])

        with patch.object(bot.time, "time", return_value=1000.0):
            self.assertFalse(bot.is_rate_limited(100))

        self.assertLessEqual(len(bot._user_request_timestamps), 1)

    def test_hedged_generator_cancels_slow_loser(self):
        cancelled = asyncio.Event()

        async def fake_call(model_name, *args, **kwargs):
            if model_name == "primary":
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return "backup result"

        async def run():
            with patch.object(bot, "_candidate_models", return_value=["primary", "backup"]), patch.object(
                bot, "_call_single_model", new=AsyncMock(side_effect=fake_call)
            ):
                return await bot._hedged_generate("primary", "input", hedge_delay_sec=0)

        result = asyncio.run(run())

        self.assertEqual(result, "backup result")
        self.assertTrue(cancelled.is_set())


if __name__ == "__main__":
    unittest.main()
