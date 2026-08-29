"""Read the frozen phase 117A Browser Agent policy without launching a browser."""

import argparse
import json
from collections.abc import Sequence

from deskpilot.application.browser_automation_policy import (
    BrowserAutomationPolicyError,
    BrowserAutomationPolicyLoader,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_subparsers(dest="command", required=True).add_parser("manifest")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        bundle = BrowserAutomationPolicyLoader().load()
        policy = bundle.policy
        if arguments.command == "manifest":
            print(
                json.dumps(
                    {
                        "policy_id": policy.policy_id,
                        "policy_digest": bundle.policy_digest,
                        "browser_product": policy.profile.browser_product,
                        "profile_name": policy.profile.profile_name,
                        "default_allowed_origins": policy.profile.default_allowed_origins,
                        "actions": [
                            {
                                "action": item.action.value,
                                "risk_level": item.risk_level.value,
                                "requires_fresh_approval": (
                                    item.requires_fresh_approval
                                ),
                            }
                            for item in policy.actions
                        ],
                        "offline_execution_boundary": (
                            policy.offline_execution_boundary.model_dump(mode="json")
                        ),
                    },
                    sort_keys=True,
                )
            )
            return 0
        return 2
    except BrowserAutomationPolicyError as error:
        print(json.dumps({"code": error.code, "detail": str(error)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
