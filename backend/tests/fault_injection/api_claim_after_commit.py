"""Claim one DAG node, report the committed fence, then wait to be killed."""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from deskpilot.application.task_service import TaskService
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.postgresql_verification import (
    PostgreSQLVerificationConfigurationError,
    load_postgresql_verification_url,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--graph-owner-id", required=True)
    parser.add_argument("--node-owner-id", required=True)
    parser.add_argument("--ttl-seconds", required=True, type=float)
    return parser


async def _run(args: argparse.Namespace) -> int:
    database: Database | None = None
    try:
        database_url = load_postgresql_verification_url(os.environ)
        if database_url is None:
            raise PostgreSQLVerificationConfigurationError(
                "PostgreSQL verification URL is not configured"
            )
        database = Database(database_url)
        service = TaskService(database, "/api/v1")
        lease = await service.acquire_effect_graph_lease(
            args.task_id,
            owner_id=args.graph_owner_id,
            ttl_seconds=args.ttl_seconds,
        )
        ready = await service.checkpoint_effect_dag_ready_set(
            args.task_id,
            lease_owner_id=args.graph_owner_id,
            fencing_token=lease.fencing_token,
            page_size=1,
        )
        if len(ready.ready_nodes) != 1:
            raise RuntimeError("fault injector expected exactly one ready node")
        claim = (
            await service.claim_effect_dag_nodes(
                args.task_id,
                (ready.ready_nodes[0].node_id,),
                ready_proof_digest=ready.proof_digest,
                claim_owner_id=args.node_owner_id,
                claim_ttl_seconds=args.ttl_seconds,
                lease_owner_id=args.graph_owner_id,
                fencing_token=lease.fencing_token,
            )
        )[0]
        print(
            json.dumps(
                {
                    "checkpoint": "api_claim_after_commit",
                    "process_id": os.getpid(),
                    "parent_process_id": os.getppid(),
                    "task_id": args.task_id,
                    "graph_id": claim.graph_id,
                    "graph_owner_id": args.graph_owner_id,
                    "graph_fencing_token": lease.fencing_token,
                    "graph_expires_at": lease.expires_at.isoformat(),
                    "node_id": claim.node_id,
                    "node_owner_id": claim.owner_id,
                    "node_fencing_token": claim.fencing_token,
                    "node_expires_at": claim.expires_at.isoformat(),
                    "ready_proof_digest": ready.proof_digest,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        await asyncio.Event().wait()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "checkpoint": "fault_injector_error",
                    "error_code": type(exc).__name__,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 2
    finally:
        if database is not None:
            await database.dispose()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parser().parse_args())))


if __name__ == "__main__":
    main()
