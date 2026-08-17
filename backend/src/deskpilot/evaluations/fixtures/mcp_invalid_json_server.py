"""Fixed MCP fault fixture: emit a non-JSON frame."""

import json
import sys

json.loads(sys.stdin.readline())
print("{not-json", flush=True)
