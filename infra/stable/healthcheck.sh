#!/usr/bin/env bash
set -Eeuo pipefail
set +x
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
set -a
source /opt/atlas-src/stable.env
set +a
export PYTHONPATH=".:services/model-gateway${PYTHONPATH:+:$PYTHONPATH}"
.venv/bin/python3 - "${1:-}" <<'PY'
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from packages.profiles import PROFILES

base = int(os.environ.get('ATLAS_PORT_BASE', '100'))
identity = os.environ['ATLAS_HEALTHCHECK_USER']

def check(port, path, headers=None, body=None):
    request = Request('http://127.0.0.1:' + str(port) + path,
                      headers=headers or {},
                      data=json.dumps(body).encode() if body is not None else None)
    try:
        with urlopen(request, timeout=125 if body else 10) as response:
            if response.status != 200:
                raise SystemExit('stable health: unexpected status')
    except (HTTPError, URLError, TimeoutError):
        raise SystemExit('stable health: endpoint not ready') from None

check(3000 + base, '/', {'X-Atlas-User': identity,
                        'X-Atlas-Proxy-Secret': os.environ['ATLAS_PROXY_SECRET']})
check(8020 + base, '/v1/models')
if sys.argv[1] != '--ready':
    check(8020 + base, '/v1/chat/completions',
          {'Content-Type': 'application/json', 'X-OpenWebUI-User-Email': identity},
          {'model': PROFILES[0], 'messages': [{'role': 'user', 'content': 'Reply with OK.'}], 'max_tokens': 4})
PY
