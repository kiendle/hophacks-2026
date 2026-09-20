# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Build a Jev-compatible company categorization request without calling an API.

The post and short relevance rules are shared state. Each company has one
independent Noul question; product recognition and ambiguity rely on Jev.
Request JSON goes to stdout or a new --output file; provenance goes to stderr.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path


PROMPT_VERSION = 'company-relevance-v2'
MODEL = 'jev-1.13.0'


def build_request(text, taxonomy):
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Post text must be a nonempty string')
    categories = taxonomy['categories']
    ids = [category['id'] for category in categories]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError('Company category IDs must be nonempty and unique')
    return {
        'model': MODEL,
        'state': {
            'post': text,
            'relevance_rules': taxonomy['classification']['rules'],
        },
        'questions': {
            category['id']: {
                'type': 'noul',
                'instructions': f"Is post relevant to {category['label']}?",
            }
            for category in categories
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--text', help='Exact post text, preserved without normalization')
    source.add_argument('--text-file', help='UTF-8 text file, or - for stdin')
    parser.add_argument('--taxonomy', type=Path,
        default=Path(__file__).with_name('company-categories.json'))
    parser.add_argument('--output', type=Path, help='New file for API request JSON; defaults to stdout')
    args = parser.parse_args()
    try:
        taxonomy_bytes = args.taxonomy.read_bytes()
        taxonomy = json.loads(taxonomy_bytes)
        text = args.text if args.text is not None else (
            sys.stdin.read() if args.text_file == '-' else Path(args.text_file).read_text(encoding='utf-8')
        )
        request = build_request(text, taxonomy)
        encoded = json.dumps(request, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        if args.output:
            with args.output.open('x', encoding='utf-8') as output:
                output.write(encoded + '\n')
        else:
            print(encoded)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(json.dumps({
        'prompt_version': PROMPT_VERSION,
        'taxonomy_version': taxonomy['version'],
        'taxonomy_sha256': hashlib.sha256(taxonomy_bytes).hexdigest(),
        'content_version': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'request_sha256': hashlib.sha256(encoded.encode('utf-8')).hexdigest(),
        'model': request['model'], 'questions': len(request['questions']),
        'json_characters': len(encoded), 'json_utf8_bytes': len(encoded.encode('utf-8')),
        'token_usage': 'Unmeasured; JSON length is not Jev billable tokens',
        'api_calls': 0,
    }, ensure_ascii=False), file=sys.stderr)


if __name__ == '__main__':
    main()
