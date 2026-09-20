"""Offline tool contract checks. No servers, model requests, audio calls or Telegram sends."""
import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brief_tools
import steps


BRIEF = "20260920-110000"
PREVIOUS = "20260920-100000"
STATUS = {"min_seconds": 45, "max_seconds": 300, "interests": []}


class BriefCustomizationTools(unittest.TestCase):
    def build(self, tool=brief_tools.make_brief, **kwargs):
        with patch.object(brief_tools, "call", side_effect=[STATUS, {"id": BRIEF}]) as request:
            result = tool(**kwargs)
        self.assertEqual(result["brief_id"], BRIEF)
        self.assertEqual(result["status"], "working")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args[:2], ("POST", "/api/briefs"))
        return result, request.call_args.args[2]

    def test_new_brief_defaults_to_last_day_and_ninety_seconds(self):
        result, payload = self.build()
        self.assertEqual(payload, {"hours": 24, "seconds": 90})
        self.assertEqual(result["hours"], 24)
        self.assertEqual(result["seconds"], 90)
        self.assertEqual(result["_card"]["brief_id"], BRIEF)

    def test_preferences_and_specific_interests_are_forwarded(self):
        _, payload = self.build(hours=24, seconds=180, interest_ids=["models"],
                                focus="Model releases and practical research", exclude_terms=["Donald Trump", "elections"],
                                long_length_requested=True)
        self.assertEqual(payload, {"hours": 24, "seconds": 90, "interests": ["models"],
                                  "focus": "Model releases and practical research", "exclude_terms": ["Donald Trump", "elections"]})

    def test_revision_leaves_unspecified_preferences_for_server_inheritance(self):
        _, payload = self.build(previous_brief_id=PREVIOUS)
        self.assertEqual(payload, {"previous_brief_id": PREVIOUS})

    def test_revision_can_explicitly_restore_defaults_and_clear_exclusions(self):
        _, payload = self.build(previous_brief_id=PREVIOUS, hours=24, seconds=90,
                                interest_ids=["models"], focus="", exclude_terms=[])
        self.assertEqual(payload, {"previous_brief_id": PREVIOUS, "hours": 24, "seconds": 90,
                                  "interests": ["models"], "focus": "", "exclude_terms": []})

    def test_revision_activity_does_not_claim_default_window_or_length(self):
        facts = steps.facts("make_brief", {"previous_brief_id": PREVIOUS})
        self.assertEqual([fact["value"] for fact in facts], ["Same as the previous brief"] * 2)
        explicit = steps.facts("make_brief", {"previous_brief_id": PREVIOUS, "hours": 12, "seconds": 60})
        self.assertEqual([fact["value"] for fact in explicit], ["60 seconds", "the last 12 hours"])
        recorded = steps.facts("record_brief", {"previous_brief_id": PREVIOUS})
        self.assertEqual(recorded[0]["value"], "Same as the previous brief")

    def test_recording_preserves_exact_draft_and_chart_evidence(self):
        script = "In the selected interval, one post questioned the release.\n\nLater posts welcomed the changes."
        context = {"source": "X/Twitter classified export", "date_from": "2026-08-30T08:00:00Z",
                   "date_to": "2026-08-30T12:00:00Z", "partial": True, "post_ids": ["one", "two"]}
        _, payload = self.build(brief_tools.record_brief, script=script, title="The selected recovery",
                                source_context=context, focus="Explain the selected recovery", exclude_terms=["elections"])
        self.assertEqual(payload["script"], script)
        self.assertEqual(payload["source"], "custom")
        self.assertEqual(payload["title"], "The selected recovery")
        self.assertEqual(payload["source_context"], context)
        self.assertNotIn("posts_collected", payload)
        self.assertNotIn("coverage", payload)

    def test_make_brief_supports_script_without_separate_wrapper(self):
        _, payload = self.build(script="A sourced draft.", source_context={"source": "user supplied"})
        self.assertEqual(payload["source"], "custom")
        self.assertEqual(payload["script"], "A sourced draft.")

    def test_custom_revision_retains_prior_preferences_without_old_script(self):
        _, payload = self.build(brief_tools.record_brief, script="The replacement draft.", previous_brief_id=PREVIOUS)
        self.assertEqual(payload, {"source": "custom", "script": "The replacement draft.", "previous_brief_id": PREVIOUS})

    def test_invalid_input_never_reaches_server(self):
        cases = [{"exclude_terms": "politics"}, {"exclude_terms": [""]}, {"focus": []},
                 {"script": " "}, {"source": "custom"}, {"script": "Text.", "source": "bluesky"},
                 {"script": "Text.", "source_context": []}, {"hours": float("nan")},
                 {"seconds": "many"}, {"seconds": -1}]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), patch.object(brief_tools, "call") as request:
                self.assertEqual(brief_tools.make_brief(**kwargs)["error"]["code"], "bad_request")
                request.assert_not_called()

    def test_failed_or_busy_status_never_starts_a_build(self):
        for status in ({"error": {"code": "unreachable"}}, {"working": PREVIOUS}):
            with self.subTest(status=status), patch.object(brief_tools, "call", return_value=status) as request:
                self.assertIn("error", brief_tools.make_brief())
                request.assert_called_once_with("GET", "/api/status")

    def test_ready_brief_returns_exact_script_provenance_and_preferences(self):
        script = "The exact approved draft.\n\nWith a second paragraph."
        stored = {"id": BRIEF, "status": "ready", "script": script, "title": "Chart briefing",
                  "source": "custom", "source_context": {"source": "X/Twitter", "post_ids": ["one"]},
                  "hours": 4, "seconds": 60, "focus": "A recovery", "exclude_terms": ["elections"],
                  "interest_ids": [], "previous_brief_id": PREVIOUS, "avoid_post_uris": ["used"],
                  "coverage": {"complete": False}, "window_start": 100, "window_end": 200,
                  "audio": {"full": "brief.mp3"}, "segments": [{"script": script, "topic": "Chart"}]}
        with patch.object(brief_tools, "call", return_value=stored):
            result = brief_tools.get_brief(BRIEF)
        for key in ("script", "source", "source_context", "focus", "exclude_terms", "coverage",
                    "window_start", "window_end", "previous_brief_id", "avoid_post_uris"):
            self.assertEqual(result[key], stored[key])
        self.assertEqual(result["preferences"]["exclude_terms"], ["elections"])
        self.assertEqual(result["segments"][0]["script"], script)
        self.assertIsNone(result["segments"][0]["posts_collected"])
        self.assertEqual(result["audio_url"], f"/api/briefs/{BRIEF}/audio/brief.mp3")
        self.assertIn("ready", steps.outcome("get_brief", result))
        self.assertNotIn("being made", steps.outcome("get_brief", result))

    def test_legacy_scripts_remain_available_but_missing_audio_is_not_ready_audio(self):
        stored = {"id": BRIEF, "status": "ready", "segments": [{"script": "One."}, {"script": "Two."}]}
        with patch.object(brief_tools, "call", return_value=stored):
            result = brief_tools.get_brief(BRIEF)
        self.assertEqual(result["script"], "One.\n\nTwo.")
        self.assertIsNone(result["audio_url"])
        self.assertIn("recording is not available", steps.outcome("get_brief", result))

    def test_generated_brief_with_null_script_returns_the_recorded_segments(self):
        stored = {"id": BRIEF, "status": "ready", "source": "bluesky", "script": None,
                  "segments": [{"script": "The first story."}, {"script": "The second story."}],
                  "audio": {"full": "brief.mp3"}}
        with patch.object(brief_tools, "call", return_value=stored):
            result = brief_tools.get_brief(BRIEF)
        self.assertEqual(result["script"], "The first story.\n\nThe second story.")
        self.assertEqual(result["source"], "bluesky")
        self.assertIsNotNone(result["audio_url"])

    def test_request_confirmation_only_returns_the_human_review_record(self):
        record = {"confirmation_id": "confirm-test", "summary": {"title": "Draft"},
                  "expires_ms": 1000, "kind": "brief_telegram", "brief_id": BRIEF}
        helper = types.ModuleType("brief_confirmation")
        helper.request_confirmation = AsyncMock(return_value=record)
        delivery = types.ModuleType("brief_delivery")
        delivery.deliver = AsyncMock()
        with patch.dict(sys.modules, {"brief_confirmation": helper, "brief_delivery": delivery}):
            result = brief_tools.request_brief_delivery_confirmation(BRIEF)
        self.assertEqual(result, record)
        helper.request_confirmation.assert_awaited_once_with(BRIEF, brief_base=brief_tools.BASE)
        delivery.deliver.assert_not_called()
        self.assertIn("Nothing has been sent", steps.outcome("request_brief_delivery_confirmation", result))

    def test_legacy_send_cannot_send_or_silently_create_approval(self):
        helper = types.ModuleType("brief_confirmation")
        helper.request_confirmation = AsyncMock()
        delivery = types.ModuleType("brief_delivery")
        delivery.deliver = AsyncMock()
        with patch.dict(sys.modules, {"brief_confirmation": helper, "brief_delivery": delivery}), patch.object(brief_tools, "call") as request:
            result = brief_tools.send_brief_to_telegram(BRIEF)
        self.assertEqual(result["error"]["code"], "confirmation_required")
        self.assertIn("request_brief_delivery_confirmation", result["error"]["hint"])
        request.assert_not_called()
        helper.request_confirmation.assert_not_called()
        delivery.deliver.assert_not_called()

    def test_new_tools_are_registered_and_require_a_visible_reason(self):
        server = Mock()
        brief_tools.register(server)
        self.assertEqual(server.tool.call_count, len(brief_tools.TOOLS))
        for tool in (brief_tools.record_brief, brief_tools.request_brief_delivery_confirmation):
            self.assertIn(tool, brief_tools.TOOLS)
            wrapped = brief_tools.with_reason(tool)
            self.assertEqual(next(iter(inspect.signature(wrapped).parameters)), "reason")
            self.assertEqual(wrapped(reason=" ")["error"]["code"], "no_reason")


if __name__ == "__main__":
    unittest.main()
