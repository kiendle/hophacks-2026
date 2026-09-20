"""Offline HTTP checks for fresh briefs, revisions, and exact script recording."""
import asyncio
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "morning-brief"))
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import server


async def check():
    with tempfile.TemporaryDirectory() as temporary, patch.object(server, "BRIEFS", Path(temporary)):
        app = web.Application()
        app["state"] = {"working": None, "task": None, "lock": asyncio.Lock()}
        app["collector"] = SimpleNamespace(store=SimpleNamespace(interests=[{"id": "ai"}]))
        app["http"], app["views"] = None, {}
        app.add_routes([web.post("/api/briefs", server.create_brief), web.get("/api/briefs", server.list_briefs), web.get("/api/briefs/{id}", server.get_brief)])
        async def build(store, http, views, brief, directory):
            brief.update(status="ready")
            (directory / "brief.json").write_text(json.dumps(brief), encoding="utf-8")

        with patch.object(server, "build", side_effect=build), patch.object(server, "list_voices", new_callable=AsyncMock) as voices:
            async with TestClient(TestServer(app)) as client:
                async def create(body, status=200):
                    response = await client.post("/api/briefs", json=body)
                    value = await response.json()
                    assert response.status == status, (response.status, value)
                    if response.status == 200:
                        await app["state"]["task"]
                        return await (await client.get(f'/api/briefs/{value["id"]}')).json()
                    return value

                first = await create({})
                assert first["hours"] == 24 and first["seconds"] == 90
                assert first["source"] == "bluesky" and first["interest_ids"] == ["ai"]
                voices.assert_not_awaited()
                for body in [[], {"hours": True}, {"hours": float("nan")}, {"seconds": False},
                             {"interests": "ai"}, {"interests": ["missing"]}, {"exclude_terms": "topic"},
                             {"exclude_terms": [""]}, {"focus": []}, {"source_context": []},
                             {"source": "custom"}, {"script": ""}, {"script": "word " * 199},
                             {"script": "Hello.", "source": "bluesky"}, {"voice": []},
                             {"previous_brief_id": "../secret"}]:
                    await create(body, 400)
                await create({"previous_brief_id": "20000101-000000"}, 404)
                prior = await create({"hours": 12, "seconds": 60, "focus": "coding tools", "exclude_terms": [" Example ", "example"]})
                prior["selected_post_uris"] = ["at://one/app.bsky.feed.post/a"]
                prior["segments"] = [{"stories": [{"posts": [{"url": "https://bsky.app/profile/two/post/b"}]}]}]
                app["state"]["working"] = prior
                revised = await create({"previous_brief_id": prior["id"]})
                assert (revised["hours"], revised["seconds"], revised["focus"], revised["exclude_terms"]) == (12, 60, "coding tools", ["example"])
                assert revised["script"] is None and revised["source"] == "bluesky"
                assert set(revised["avoid_post_uris"]) == {"at://one/app.bsky.feed.post/a", "at://two/app.bsky.feed.post/b"}
                override = await create({"previous_brief_id": revised["id"], "hours": 24, "seconds": 90, "exclude_terms": []})
                assert override["hours"] == 24 and not override["exclude_terms"]
                app["collector"].store.interests.clear()
                script = "  This is my approved draft.\nKeep these words exactly.  "
                custom = await create({"script": script, "source_context": {"source": "twitter", "start": "2026-09-08"}})
                assert custom["source"] == "custom" and custom["script"] == script and not custom["interest_ids"]
                assert custom["source_context"]["source"] == "twitter"
                await create({"previous_brief_id": custom["id"]}, 400)
                await create({}, 400)
                fixed = datetime(2026, 9, 20, 1, 2, 3)
                with patch.object(server, "datetime") as clock:
                    clock.now.return_value = fixed
                    one = await create({"script": "One."})
                    two = await create({"script": "Two."})
                assert one["id"] != two["id"]
                assert json.loads((server.BRIEFS / one["id"] / "brief.json").read_text())["script"] == "One."
                working = {"id": "20260920-010205", "status": "working"}
                app["state"]["working"] = working
                await create({"script": "Concurrent."}, 409)
                assert app["state"]["working"] is working
                app["state"]["working"] = None
                with patch.object(server, "build", side_effect=server.BriefError("No matching recent posts.")):
                    failed = await create({"script": "Failure fixture."})
                assert failed["status"] == "failed"
                app["state"]["working"] = None
                saved = await (await client.get(f'/api/briefs/{failed["id"]}')).json()
                assert saved["step"] == "No matching recent posts."
    print("PASS: 24h defaults, validated input, inherited preferences, fresh revisions, exact scripts, unique files, busy guard and saved failures.")


if __name__ == "__main__":
    asyncio.run(check())
