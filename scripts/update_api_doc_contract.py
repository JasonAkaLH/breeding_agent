"""Refresh the API guide's embedded OpenAPI snapshot and TypeScript example.

Run with the project's Python environment: python scripts/update_api_doc_contract.py
This generates route metadata only: no runtime, database, lifespan or network is started.
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def contract() -> dict:
    from src.api.app import create_app

    return create_app(runtime=object()).openapi()


def updated_document(source: str) -> str:
    payload = json.dumps(contract(), ensure_ascii=False, indent=2).replace('<', '\\u003c')
    example = html.escape((ROOT / 'docs/api/frontend-example.ts').read_text())
    for identifier, content, tag in [('api-contract', payload, 'script'), ('frontend-example', example, 'code')]:
        pattern = rf'(<{tag}\b[^>]*\bid="{identifier}"[^>]*>).*?(</{tag}>)'
        source, count = re.subn(pattern, lambda match: match[1] + content + match[2], source, flags=re.S)
        if count != 1:
            raise ValueError(f'Expected one {identifier} block; found {count}')
    return source


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    path = ROOT / 'docs/api/api-doc.html'
    source = path.read_text()
    updated = updated_document(source)
    if args.check:
        if source != updated:
            raise SystemExit('API documentation snapshot/example is stale; run this script without --check.')
        print('API documentation snapshot and example match current code.')
    else:
        path.write_text(updated)
        print('Updated API documentation snapshot and TypeScript example.')


if __name__ == '__main__':
    main()
