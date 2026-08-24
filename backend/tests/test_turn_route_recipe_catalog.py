from __future__ import annotations

import pytest

from deskpilot.application.capability_catalog import create_builtin_capability_catalog
from deskpilot.application.route_recipe_catalog import (
    RouteRecipeCatalog,
    RouteRecipeError,
)

_LEGACY_DIGESTS = {
    "research_to_html": "17ddd84e1fe614cac1e5c2dc078861e4a83f81bec3dd9934e618d50039f42745",
    "knowledge_lookup": "0b39f9ae7b0c196d07f02a1c9a10a7e8015c7ef7d28a197d3367dad159def29d",
    "mcp_text_metrics": "5ce4550db5180bc886a9eae43a40728943dfe30d0d9679a25e9c09b86905392f",
    "workspace_file_read": "3f72b4f25660f25c6e04a23fb9ca739881bd3ce917d9f16cff2c0cf2920ed41c",
    "workspace_file_replace": "e8a3ec8612c751da9119fcd0f621bde757c6e435ff4d7bafb849642a948f0bd1",
    "workspace_patch_bundle": "75b9d016a835be4e3bcc7cc9b2a44af1b66acb4665c15bfc571b1cb76980d756",
    "workspace_agent_patch_test": (
        "76212c5a5d25272fd34ab609cf0993ee31710455c538159f654714f645d7850d"
    ),
    "workspace_dynamic_patch_test": (
        "995e7476d2d48b64fff4ba1fca2eaa272264fa30ebcc981aa709356099dfe82f"
    ),
    "workspace_file_create": "90b16750fa82a923afafe45952ebe32e92a39d95b275ca51cbe0550712b48468",
    "workspace_file_rename": "a825d180481b9786664e01d7f302e714d560e25a125ab3df1b246383d71e0079",
    "workspace_directory_list": "ad9feaa02e0d2f1b17643ac0f7b618db7c61dce74de53b7b439cee14d49ed8c0",
    "workspace_directory_analyze": (
        "2713da3cacc42c99089d5a0783acbe78ef40b7a55830e61d7383bcf686f529bf"
    ),
    "workspace_snapshot_check": "ca53da2a53f15c007e097a5fc9e10a8849afc172cbc1087e03716688e9ebc2ee",
    "workspace_python_test": "ddc01d8713f24af406acc8a7e215a012d8fa346569d9b60186441efec9de9886",
    "workspace_node_test": "5b562956bb793ba5cf6c9a54f05066ade2c407091d1c89051a78f2028b59c32e",
}


def test_legacy_route_recipe_digests_are_immutable() -> None:
    assert {
        route_id: RouteRecipeCatalog.digest(route_id, "1")
        for route_id in RouteRecipeCatalog.route_ids()
    } == _LEGACY_DIGESTS


def test_v2_records_the_complete_directory_analysis_capability_surface() -> None:
    legacy = RouteRecipeCatalog.manifest("workspace_directory_analyze", "1")
    current = RouteRecipeCatalog.manifest("workspace_directory_analyze", "2")

    assert legacy["capabilities"] == (
        "workspace.directory.read.v1",
        "workspace.file.read.v1",
    )
    assert current["capabilities"] == (
        "workspace.directory.read.v1",
        "workspace.file.read.v1",
        "workspace.python.test.v1",
        "workspace.node.test.v1",
    )
    assert RouteRecipeCatalog.digest("workspace_directory_analyze", "1") != (
        RouteRecipeCatalog.digest("workspace_directory_analyze", "2")
    )


def test_offers_are_precompiled_and_test_kind_is_server_fixed() -> None:
    task_id = f"tsk_{'1' * 32}"
    offers = RouteRecipeCatalog.offers_for(
        task_id=task_id,
        capabilities=create_builtin_capability_catalog(research_runtime_enabled=True),
    )

    assert len(offers) == 26
    assert len({item.variant_key for item in offers}) == len(offers)
    assert len({item.recipe_digest for item in offers}) == len(offers)
    assert all(item.contract.task_id == task_id for item in offers)
    assert all(item.draft.task_id == task_id for item in offers)
    patch_variants = {
        (item.variant_key, item.fixed_parameters.get("test_kind"))
        for item in offers
        if item.route_id == "workspace_agent_patch_test"
    }
    assert patch_variants == {
        ("workspace_agent_patch_test:python", "python"),
        ("workspace_agent_patch_test:node", "node"),
    }
    command_variants = {
        (item.variant_key, item.fixed_parameters.get("command_profile_id"))
        for item in offers
        if item.route_id == "workspace_command_profile"
    }
    assert command_variants == {
        (
            f"workspace_command_profile:{profile_id}",
            profile_id,
        )
        for profile_id in (
            "python.pytest.v1",
            "python.ruff.v1",
            "python.mypy.v1",
            "node.pnpm_test.v1",
            "node.pnpm_typecheck.v1",
            "node.pnpm_build.v1",
        )
    }
    command_intents = {
        RouteRecipeCatalog.intent_description(item)
        for item in offers
        if item.route_id == "workspace_command_profile"
    }
    assert len(command_intents) == 6
    assert all(
        any(profile_id in description for profile_id in (
            "python.pytest.v1",
            "python.ruff.v1",
            "python.mypy.v1",
            "node.pnpm_test.v1",
            "node.pnpm_typecheck.v1",
            "node.pnpm_build.v1",
        ))
        for description in command_intents
    )
    assert {item.route_id for item in offers} - set(RouteRecipeCatalog.route_ids()) == {
        "workspace_project_search",
        "workspace_project_batch_read",
        "workspace_git_inspect",
        "workspace_command_profile",
    }
    assert set(RouteRecipeCatalog.route_ids()) < set(
        RouteRecipeCatalog.planner_route_ids()
    )


def test_offers_respect_server_runtime_route_eligibility() -> None:
    offers = RouteRecipeCatalog.offers_for(
        task_id=f"tsk_{'2' * 32}",
        capabilities=create_builtin_capability_catalog(research_runtime_enabled=True),
        eligible_variant_keys=frozenset({"knowledge_lookup", "workspace_file_read"}),
    )

    assert {item.route_id for item in offers} == {
        "knowledge_lookup",
        "workspace_file_read",
    }


def test_offer_eligibility_filters_test_variants_independently() -> None:
    offers = RouteRecipeCatalog.offers_for(
        task_id=f"tsk_{'3' * 32}",
        capabilities=create_builtin_capability_catalog(research_runtime_enabled=True),
        eligible_variant_keys=frozenset({"workspace_agent_patch_test:python"}),
    )

    assert tuple(item.variant_key for item in offers) == ("workspace_agent_patch_test:python",)


def test_parameter_binding_requires_exact_persisted_message_substrings() -> None:
    message = "请帮我读取项目里的 notes/todo.md，然后说明内容"
    parameters, proof, proof_digest = RouteRecipeCatalog.bind_parameters(
        "workspace_file_read",
        message,
        {"path": "notes/todo.md"},
    )

    assert parameters == {"path": "notes/todo.md"}
    assert proof["message_digest"]
    assert len(proof_digest) == 64
    with pytest.raises(RouteRecipeError):
        RouteRecipeCatalog.bind_parameters(
            "workspace_file_read",
            message,
            {"path": "invented/secrets.txt"},
        )


def test_server_fixed_parameter_cannot_be_overridden_by_model() -> None:
    message = "修复 a.py，项目 root，测试 test_a.py，目标 修正返回值"
    proposed = {
        "path": "a.py",
        "project_path": "root",
        "test_path": "test_a.py",
        "objective": "修正返回值",
    }
    parameters, _, _ = RouteRecipeCatalog.bind_parameters(
        "workspace_agent_patch_test",
        message,
        proposed,
        fixed_parameters={"test_kind": "python"},
    )
    assert parameters["test_kind"] == "python"

    with pytest.raises(RouteRecipeError):
        RouteRecipeCatalog.bind_parameters(
            "workspace_agent_patch_test",
            message,
            {**proposed, "test_kind": "Node"},
            fixed_parameters={"test_kind": "python"},
        )
