# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4", "anthropic>=0.75", "imageio-ffmpeg>=0.6"]
# ///
"""Offline coverage of brief preferences, regeneration and approved-script narration.

Run: uv run harness/tests/test_brief_preferences.py
Every writer, engagement and voice request is mocked; no service is started.
"""
import copy
from array import array
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "morning-brief"))
import briefing

NOW = 1_790_000_000_000
HOUR = 3_600_000


def post(key, text, age=1, link=None):
    return {"uri": f"at://did:plc:{key}/app.bsky.feed.post/{key}", "did": f"did:plc:{key}",
            "rkey": key, "topics": ["ai"], "t": NOW - age * HOUR, "text": text,
            "parent": None, "link": link}


def link(key, title="A research update", description="New findings from a research team"):
    return {"url": f"https://news.example/{key}", "title": title, "description": description}


def store_for(*posts):
    return SimpleNamespace(posts={row["uri"]: row for row in posts}, heat={},
                           interests=[{"id": "ai", "name": "AI"}])


def request(**changes):
    return {"id": "20260920-100000", "created": NOW, "hours": 24, "seconds": 90,
            "voice": {"id": "test-voice", "name": "Test voice"}, "interest_ids": ["ai"],
            "notes": [], "audio": None, "usage": {}, "status": "working", **changes}


def answer(payload):
    topic = next(topic for topic in payload["topics"] if topic["posts"])
    selected = topic["posts"][0]
    return {"title": "Research updates", "segments": [{"topic": topic["topic"],
            "headline": selected["text"], "script": selected["text"], "stories": [{
                "title": selected["text"], "summary": selected["text"], "why_it_matters": "A new research result.",
                "mood": "curious", "post_ids": [selected["id"]]}]}]}


class BriefPreferencesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.env = patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.clock = patch.object(briefing, "now_ms", return_value=NOW)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.payloads = []

        async def hydrate(_session, views, uris):
            for uri in uris:
                views[uri] = {"at": NOW, "likes": 10, "reposts": 0, "replies": 0, "quotes": 0,
                              "name": "A researcher", "handle": "research.example"}

        async def writer(_system, content, _schema, _effort):
            payload = json.loads(content)
            self.payloads.append(payload)
            return answer(payload), {"model": "offline-test"}

        self.hydrate_patch = patch.object(briefing, "hydrate", new=AsyncMock(side_effect=hydrate))
        self.hydrate = self.hydrate_patch.start()
        self.addCleanup(self.hydrate_patch.stop)
        self.writer_patch = patch.object(briefing, "write_json", new=AsyncMock(side_effect=writer))
        self.writer = self.writer_patch.start()
        self.addCleanup(self.writer_patch.stop)

    async def build(self, store, brief):
        await briefing.build(store, None, {}, brief, self.directory)
        return brief

    def test_whole_word_phrases_and_common_name_aliases(self):
        pattern = briefing.exclusion_pattern(["Donald Trump", "ACME Labs", "AI"])
        for text in ["Trump's remarks", "DONALD J. TRUMP", "President Trump", "acme\nLabs launched", "ai research"]:
            self.assertTrue(briefing.excluded(pattern, text), text)
        for text in ["A trumpet solo", "Donald Duck", "Said something", "Acme laboratory"]:
            self.assertFalse(briefing.excluded(pattern, text), text)

    async def test_filter_previews_and_real_window_before_hydration_and_writer(self):
        kept = post("keep", "An open research model was released.", age=23)
        store = store_for(kept,
                          post("name", "DONALD J. TRUMP announces a plan."),
                          post("title", "Read this announcement.", link=link("a", title="President Trump announces a plan")),
                          post("description", "New funding news.", link=link("b", description="ACME Labs raised funds")),
                          post("old", "Older findings.", age=25), post("future", "Future findings.", age=-1))
        brief = await self.build(store, request(exclude_terms=["Donald Trump", "Acme Labs"], focus="Focus on open research releases."))
        self.assertEqual(self.hydrate.await_args.args[2], [kept["uri"]])
        payload = self.payloads[0]
        self.assertEqual(payload["window_hours"], 24)
        self.assertEqual(payload["window_start"], NOW - 24 * HOUR)
        self.assertEqual(payload["window_end"], NOW)
        self.assertEqual(payload["focus"], brief["focus"])
        self.assertEqual(payload["exclude_terms"], brief["exclude_terms"])
        self.assertEqual([row["text"] for row in payload["topics"][0]["posts"]], [kept["text"]])
        self.assertEqual(brief["selected_post_uris"], [kept["uri"]])
        self.assertEqual(brief["segments"][0]["stories"][0]["posts"][0]["uri"], kept["uri"])

    async def test_new_request_uses_new_time_and_new_preferences(self):
        old = post("old", "Earlier model research.", age=1)
        fresh = post("fresh", "Fresh robotics findings.", age=-25)
        store = store_for(old, fresh)
        first = await self.build(store, request())
        self.now.return_value = NOW + 26 * HOUR
        second = await self.build(store, request(hours=12, exclude_terms=["Earlier"], focus="Robotics results"))
        self.assertEqual(first["selected_post_uris"], [old["uri"]])
        self.assertEqual(second["selected_post_uris"], [fresh["uri"]])
        self.assertEqual(self.payloads[1]["window_start"], NOW + 14 * HOUR)
        self.assertEqual(self.payloads[1]["window_hours"], 12)
        self.assertEqual(self.payloads[1]["exclude_terms"], ["Earlier"])
        self.assertEqual(self.writer.await_count, 2)

    async def test_regeneration_selects_alternatives_and_excludes_duplicate_sources(self):
        first = post("first", "New model research.", link=link("model"))
        same_link = post("same-link", "Another account shares the model news.", link={**link("model"), "url": "https://news.example/model?utm_source=feed"})
        same_text = post("same-text", first["text"])
        other = post("other", "Robotics lab shares a different finding.")
        store = store_for(first, same_link, same_text, other)
        original = await self.build(store, request())
        refreshed = await self.build(store, request(previous_brief_id=original["id"], avoid_post_uris=original["selected_post_uris"]))
        self.assertEqual(refreshed["selected_post_uris"], [other["uri"]])
        self.assertEqual(self.hydrate.await_args.args[2], [other["uri"]])
        self.assertNotEqual(refreshed["segments"][0]["headline"], original["segments"][0]["headline"])
        with self.assertRaisesRegex(briefing.BriefError, "No new eligible stories"):
            await self.build(store, request(avoid_post_uris=[first["uri"], other["uri"]]))
        self.assertEqual(self.writer.await_count, 2)

    async def test_regeneration_rejects_a_repeated_title_from_different_sources(self):
        store = store_for(post("different-post", "Research teams released a new result."))
        brief = request(avoid_story_titles=["RESEARCH TEAMS RELEASED A NEW RESULT!"])
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-only"}), patch.object(briefing, "voice", new_callable=AsyncMock) as voice:
            with self.assertRaisesRegex(briefing.BriefError, "repeated a story"):
                await self.build(store, brief)
            voice.assert_not_awaited()
        self.assertEqual(self.payloads[0]["avoid_story_titles"], brief["avoid_story_titles"])

    async def test_stories_require_valid_eligible_post_references(self):
        store = store_for(post("research", "Research teams released a new result."))
        for references in [[], ["made-up-id"], ["p1", "made-up-id"]]:
            with self.subTest(references=references):
                written = answer({"topics": [{"topic": "AI", "posts": [{"id": "p1", "text": "A new research result."}]}]})
                written["segments"][0]["stories"][0]["post_ids"] = references
                self.writer.side_effect = None
                self.writer.return_value = (written, {})
                with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-only"}), patch.object(briefing, "voice", new_callable=AsyncMock) as voice:
                    with self.assertRaisesRegex(briefing.BriefError, "without an eligible collected source"):
                        await self.build(store, request())
                    voice.assert_not_awaited()

    async def test_forbidden_writer_output_is_never_recorded_or_saved(self):
        store = store_for(post("research", "Research teams released a new result."))
        safe = answer({"topics": [{"topic": "AI", "posts": [{"id": "p1", "text": "A new research result."}]}]})
        for field in ["title", "headline", "script", "story_title", "summary", "why_it_matters"]:
            with self.subTest(field=field):
                written = copy.deepcopy(safe)
                if field == "title":
                    written[field] = "Trump news"
                elif field in ("headline", "script"):
                    written["segments"][0][field] = "Trump news"
                else:
                    written["segments"][0]["stories"][0]["title" if field == "story_title" else field] = "Trump news"
                self.writer.side_effect = None
                self.writer.return_value = (written, {})
                brief = request(exclude_terms=["Donald Trump"])
                with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-only"}), patch.object(briefing, "voice", new_callable=AsyncMock) as voice:
                    with self.assertRaisesRegex(briefing.BriefError, "excluded subject"):
                        await self.build(store, brief)
                    voice.assert_not_awaited()
                self.assertFalse((self.directory / "brief.json").exists())
                self.assertNotIn("segments", brief)

    async def test_custom_script_reaches_voice_in_full_without_writer_or_hydration(self):
        script = "  The approved opening stays exactly as supplied.\n\n" + "A complete approved sentence. " * 75 + "The approved ending.  "
        brief = request(script=script, source="custom", title="Approved research brief", seconds=45,
                        source_context={"source": "provided notes", "title": "Draft title"})
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-only"}), \
                patch.object(briefing, "speak", new_callable=AsyncMock, return_value=b"mock-audio") as speak, \
                patch.object(briefing, "limit_recording") as limit:
            await self.build(None, brief)
            speak.assert_awaited_once_with(None, script, "", "", "test-voice")
            limit.assert_called_once_with(b"mock-audio", self.directory / "brief.mp3", 45, preserve_script=True)
        self.writer.assert_not_awaited()
        self.hydrate.assert_not_awaited()
        self.assertEqual(brief["segments"][0]["script"], script)
        self.assertEqual(brief["title"], "Approved research brief")
        self.assertEqual(brief["source_context"], {"source": "provided notes", "title": "Draft title"})
        self.assertNotIn("posts_collected", brief["segments"][0])
        self.assertNotIn("window_start", brief)
        self.assertEqual(json.loads((self.directory / "brief.json").read_text(encoding="utf-8"))["segments"][0]["script"], script)

    async def test_custom_script_or_title_cannot_violate_exclusions(self):
        for changes in [{"script": "President Trump spoke."}, {"script": "An approved update.", "title": "Trump update"}]:
            with self.subTest(changes=changes), patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-only"}), \
                    patch.object(briefing, "voice", new_callable=AsyncMock) as voice:
                with self.assertRaisesRegex(briefing.BriefError, "excluded subject"):
                    await self.build(None, request(exclude_terms=["Donald Trump"], **changes))
                voice.assert_not_awaited()
        self.writer.assert_not_awaited()
        self.hydrate.assert_not_awaited()

    def test_shortened_custom_recording_preserves_the_end_of_the_audio(self):
        import imageio_ffmpeg

        executable = imageio_ffmpeg.get_ffmpeg_exe()

        def run(*arguments):
            return subprocess.run([executable, '-hide_banner', '-loglevel', 'error', *arguments],
                                  capture_output=True, check=True, timeout=30,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

        source, output = self.directory / 'source.mp3', self.directory / 'brief.mp3'
        # A distinctive final tone proves that fitting a long recording preserves its ending.
        run('-f', 'lavfi', '-i', 'sine=frequency=440:duration=6',
            '-f', 'lavfi', '-i', 'sine=frequency=1200:duration=2',
            '-filter_complex', '[0:a][1:a]concat=n=2:v=0:a=1[out]', '-map', '[out]',
            '-codec:a', 'libmp3lame', str(source))
        briefing.limit_recording(source.read_bytes(), output, 4, preserve_script=True)
        decoded = run('-i', str(output), '-f', 's16le', '-ar', '8000', '-ac', '1', 'pipe:1').stdout
        samples = array('h', decoded)
        self.assertLessEqual(len(samples) / 8000, 4)
        self.assertGreater(len(samples) / 8000, 3)
        ending = samples[-2800:-400]
        crossings = sum((first < 0) != (second < 0) for first, second in zip(ending, ending[1:]))
        self.assertGreater(crossings, 550, 'The distinctive final tone was cut off instead of sped up.')

    async def test_sparse_results_do_not_widen_window_or_ignore_exclusions(self):
        store = store_for(post("old", "A popular older research result.", age=30),
                          post("blocked", "Trump spoke about AI."))
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-only"}), patch.object(briefing, "voice", new_callable=AsyncMock) as voice:
            with self.assertRaisesRegex(briefing.BriefError, "No eligible posts.*requested window"):
                await self.build(store, request(exclude_terms=["Trump"]))
            voice.assert_not_awaited()
        self.writer.assert_not_awaited()
        self.hydrate.assert_not_awaited()

    async def test_no_writer_preserves_exclusions_and_reports_unavailable_focus(self):
        store = store_for(post("kept", "A lab released its open research model."), post("excluded", "Trump spoke."))
        self.writer.side_effect = briefing.BriefError("The writer is unavailable.")
        brief = await self.build(store, request(exclude_terms=["Trump"]))
        self.assertEqual(brief["selected_post_uris"], [next(iter(store.posts))])
        self.assertNotIn("Trump", brief["segments"][0]["script"])
        with self.assertRaisesRegex(briefing.BriefError, "editorial focus could not be applied"):
            await self.build(store, request(exclude_terms=["Trump"], focus="Focus on research methods"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
