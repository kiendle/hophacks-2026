"""Verify the duration of real encoded audio without calling a paid service."""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'morning-brief'))
import briefing
import imageio_ffmpeg


def run(*arguments):
    return subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), *arguments], capture_output=True, check=True,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


with tempfile.TemporaryDirectory() as temporary:
    source, output = Path(temporary) / 'source.mp3', Path(temporary) / 'brief.mp3'
    run('-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=100', '-codec:a', 'libmp3lame', str(source))
    briefing.limit_recording(source.read_bytes(), output, 90)
    decoded = run('-i', str(output), '-f', 'null', '-')
    match = re.search(rb'Duration: (\d+):(\d+):(\d+\.\d+)', decoded.stderr)
    assert match, decoded.stderr
    hours, minutes, seconds = map(float, match.groups())
    duration = hours * 3600 + minutes * 60 + seconds
    assert 89 <= duration <= 90, duration
    segments = [{'script': 'A complete sentence. ' * 100}, {'script': 'A second story. ' * 40}]
    briefing.shorten_scripts(segments, 198)
    assert sum(len(segment['script'].split()) for segment in segments) <= 198
    assert segments[0]['script'].endswith('.')
    assert briefing.MAX_SECONDS == 90
    print(f'PASS: generated 100-second recording capped to {duration:.2f}s; script budget respected.')
