"""Same-origin access to the running Morning Brief service; no second collector."""
import re
import aiohttp
from aiohttp import web
import brief_tools
import bridge

async def proxy(request):
    path = request.path
    if not re.fullmatch(r'/api/briefs(?:/\d{8}-\d{6}(?:/audio/[a-z0-9-]+\.mp3)?)?', path):
        return bridge.fail(404, 'No such brief.')
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
            headers = {'Range': request.headers['Range']} if 'Range' in request.headers else {}
            async with session.get(brief_tools.BASE + path, headers=headers) as upstream:
                forwarded = {key: upstream.headers[key] for key in ('Content-Type', 'Content-Range', 'Accept-Ranges', 'Content-Length') if key in upstream.headers}
                response = web.StreamResponse(status=upstream.status, headers={**forwarded, 'Cache-Control': 'no-store'})
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(65536):
                    await response.write(chunk)
                await response.write_eof()
                return response
    except (aiohttp.ClientError, TimeoutError):
        return bridge.fail(502, 'The Morning Brief service is unavailable. Start harness/signal_server.py on port 5194.')


def setup(app):
    app.add_routes([web.get('/api/briefs', proxy), web.get('/api/briefs/{id}', proxy), web.get('/api/briefs/{id}/audio/{file}', proxy)])
