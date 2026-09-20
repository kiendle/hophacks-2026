"""Saved labels, exact selected times, and opening/delta behavior; no network."""
import math
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import classified_data as data

grade = {'company': 'openai', 'score': 8, 'choice': 'positive', 'probabilities': {'positive': .7, 'negative': .1}}
def event(identifier, kind, t, **extra):
    return dict(id=identifier, postId='one', kind=kind, t=t, text='OpenAI news', grades=[grade], **extra)

events = [event('post', 'post', 0), event('opening', 'like', 1000, opening=True, delta=9),
          event('repeat-opening', 'like', 2000, opening=True, delta=9),
          event('change', 'like', 3000, opening=False, delta=2)]
fixture = dict(start=0, end=4000, events=events, companies=[{'id': 'openai', 'name': 'OpenAI'}], counts={'posts': 1, 'likes': 3})
taxonomy = {'taxonomy': {'categories': [{'id': 'openai', 'label': 'OpenAI', 'products': ['ChatGPT']}]}}
with patch.object(data, 'load', return_value=fixture), patch.object(data, 'manifest', return_value=taxonomy), patch.object(data, 'has_query_store', return_value=False):
    scan = data.scan(['OpenAI'], ['OpenAI'])
    assert scan['dataset']['events'] == events  # never replace or rescore saved grades
    assert scan['kept'] == 1
    summary = data.sentiment(['OpenAI'], '1970-01-01T00:00:00Z', '1970-01-01T00:00:04Z')
    assert summary['companies'][0]['sentiment'] == 8
    assert summary['companies'][0]['posts'] == 1
    historical = data.sentiment(['AI'], '1970-01-01T00:00:02Z', '1970-01-01T00:00:03Z')
    assert historical['companies'][0]['sentiment'] is None  # repeated opening adds no influence
    later = data.sentiment(['ChatGPT'], '1970-01-01T00:00:03Z', '1970-01-01T00:00:04Z')
    assert later['companies'][0]['posts'] == 0 and later['companies'][0]['sentiment'] == 8
    assert data.preview(['AI'])['total'] == 1
    assert data.preview(['AI'], language='en')['error']['code'] == 'language_unavailable'
print('PASS: classified labels preserved, product matching, exact time windows, duplicate openings, no regrading.')
