"""Fixed MCP fault fixture: change the JSON-RPC response identity."""

import json
import sys

json.loads(sys.stdin.readline())
print(
    json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 999,
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
            },
        }
    ),
    flush=True,
)
