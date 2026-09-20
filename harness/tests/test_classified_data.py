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
    # One pattern with a substring shortcut still means whole words, any case, and re.I's odd letters.
    said = lambda text: dict(id=text, postId=text, kind='post', t=0, text=text, grades=[])
    found = data.matcher(['chatbot', 'Sam Altman'])
    long_s, dotted_i, dotless_i = chr(0x17f), chr(0x130), chr(0x131)
    assert [found(said(t)) for t in ('a ChatBot!', 'chatbots', 'my_chatbot', 'SAM ALTMAN said', long_s + 'am altman', 'nothing here')] == [True, False, False, True, True, False]
    assert all(data.matcher(['openai'])(said(t)) for t in ('OPENA' + dotted_i, 'opena' + dotless_i + ' news'))
    assert data.matcher(['nvidia'])(event('graded', 'post', 0)) is False and data.matcher(['chatgpt'])(event('graded', 'post', 0)) is True
    # The same words on the same export are selected once; another export is selected again.
    assert data.scan(['OpenAI'], [])['dataset']['events'] is data.scan(['OpenAI'], [])['dataset']['events']
    assert data.scan(['AI'], [])['dataset']['events'] == events and data.scan(['nvidia'], [])['kept'] == 0
    with patch.object(data, 'load', return_value={**fixture, 'events': events[:1], 'counts': {'posts': 1, 'likes': 0}}):
        assert data.scan(['OpenAI'], [])['dataset']['counts'] == {'posts': 1, 'likes': 0}
print('PASS: classified labels preserved, product matching, exact time windows, duplicate openings, no regrading, fast matching unchanged.')
