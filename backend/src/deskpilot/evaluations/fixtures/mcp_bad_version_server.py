"""Fixed MCP fault fixture: negotiate the wrong protocol version."""

import json
import sys

request = json.loads(sys.stdin.readline())
print(
    json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "protocolVersion": "1900-01-01",
                "capabilities": {"tools": {}},
            },
        }
    ),
    flush=True,
)
