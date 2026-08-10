"""Authenticated Provider read and administration endpoints."""

import re
from collections.abc import Awaitable
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response

from deskpilot.api.dependencies import (
    get_model_gateway,
    get_provider_catalog,
    get_provider_management,
)
from deskpilot.api.problem_details import ProblemException
from deskpilot.application.credential_resolver import CredentialResolutionError
from deskpilot.application.model_gateway import (
    DisabledModelProviderError,
    ModelGateway,
    UnknownModelProviderError,
)
from deskpilot.application.provider_catalog import ProviderCatalogService
from deskpilot.application.provider_catalog_store import (
    ProviderCatalogVersionConflictError,
)
from deskpilot.application.provider_management_service import (
    ProviderManagementService,
)
from deskpilot.application.provider_management_store import (
    ProviderAlreadyExistsError,
    ProviderIdempotencyConflictError,
    ProviderManagementConflictError,
    ProviderManagementNotFoundError,
)
from deskpilot.application.provider_runtime_store import (
    ProviderRuntimeConfigProtectionError,
    ProviderRuntimeConfigProtectionUnavailableError,
)
from deskpilot.domain.model_contracts import PROVIDER_ID_PATTERN
from deskpilot.domain.model_routing import ModelGatewayRoutingSnapshot
from deskpilot.domain.provider_admin import (
    ProviderConfigAuditPage,
    ProviderMutationResult,
)
from deskpilot.domain.provider_config import ProviderConfig
from deskpilot.domain.provider_management import (
    ProviderCatalogSnapshot,
    ProviderHealthSnapshot,
)

router = APIRouter(prefix="/model-providers", tags=["model-providers"])

ProviderCatalogDependency = Annotated[
    ProviderCatalogService,
    Depends(get_provider_catalog),
]
ProviderManagementDependency = Annotated[
    ProviderManagementService,
    Depends(get_provider_management),
]
ModelGatewayDependency = Annotated[ModelGateway, Depends(get_model_gateway)]
IfMatchHeader = Annotated[str | None, Header(alias="If-Match")]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]

_ETAG_PATTERN = re.compile(r'^"provider-catalog-v([1-9][0-9]*)"$')
_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


def provider_catalog_etag(version: int) -> str:
    return f'"provider-catalog-v{version}"'


@router.get("", response_model=ProviderCatalogSnapshot)
async def list_model_providers(
    response: Response,
    catalog: ProviderCatalogDependency,
) -> ProviderCatalogSnapshot:
    snapshot = await catalog.snapshot()
    response.headers["ETag"] = provider_catalog_etag(snapshot.catalog_version)
    response.headers["Cache-Control"] = "no-store"
    return snapshot


@router.get("/audit", response_model=ProviderConfigAuditPage)
async def list_model_provider_audit(
    management: ProviderManagementDependency,
    provider_id: Annotated[
        str | None,
        Query(pattern=PROVIDER_ID_PATTERN),
    ] = None,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ProviderConfigAuditPage:
    return await management.audit_page(
        provider_id=provider_id,
        after_sequence=after_sequence,
        limit=limit,
    )


@router.get("/routing", response_model=ModelGatewayRoutingSnapshot)
async def get_model_provider_routing(
    gateway: ModelGatewayDependency,
    response: Response,
) -> ModelGatewayRoutingSnapshot:
    response.headers["Cache-Control"] = "no-store"
    return gateway.routing_snapshot()


@router.post("", response_model=ProviderMutationResult, status_code=201)
async def create_model_provider(
    config: ProviderConfig,
    response: Response,
    management: ProviderManagementDependency,
    if_match: IfMatchHeader = None,
    idempotency_key: IdempotencyHeader = None,
) -> ProviderMutationResult:
    return await _execute_mutation(
        management.create(
            config,
            expected_catalog_version=_expected_version(if_match),
            idempotency_key=_idempotency_key(idempotency_key),
        ),
        response,
    )


@router.put("/{provider_id}", response_model=ProviderMutationResult)
async def update_model_provider(
    provider_id: str,
    config: ProviderConfig,
    response: Response,
    management: ProviderManagementDependency,
    if_match: IfMatchHeader = None,
    idempotency_key: IdempotencyHeader = None,
) -> ProviderMutationResult:
    return await _execute_mutation(
        management.update(
            provider_id,
            config,
            expected_catalog_version=_expected_version(if_match),
            idempotency_key=_idempotency_key(idempotency_key),
        ),
        response,
    )


@router.post("/{provider_id}:enable", response_model=ProviderMutationResult)
async def enable_model_provider(
    provider_id: str,
    response: Response,
    management: ProviderManagementDependency,
    if_match: IfMatchHeader = None,
    idempotency_key: IdempotencyHeader = None,
) -> ProviderMutationResult:
    return await _execute_mutation(
        management.enable(
            provider_id,
            expected_catalog_version=_expected_version(if_match),
            idempotency_key=_idempotency_key(idempotency_key),
        ),
        response,
    )


@router.post("/{provider_id}:disable", response_model=ProviderMutationResult)
async def disable_model_provider(
    provider_id: str,
    response: Response,
    management: ProviderManagementDependency,
    if_match: IfMatchHeader = None,
    idempotency_key: IdempotencyHeader = None,
) -> ProviderMutationResult:
    return await _execute_mutation(
        management.disable(
            provider_id,
            expected_catalog_version=_expected_version(if_match),
            idempotency_key=_idempotency_key(idempotency_key),
        ),
        response,
    )


@router.post("/{provider_id}:make-default", response_model=ProviderMutationResult)
async def make_default_model_provider(
    provider_id: str,
    response: Response,
    management: ProviderManagementDependency,
    if_match: IfMatchHeader = None,
    idempotency_key: IdempotencyHeader = None,
) -> ProviderMutationResult:
    return await _execute_mutation(
        management.make_default(
            provider_id,
            expected_catalog_version=_expected_version(if_match),
            idempotency_key=_idempotency_key(idempotency_key),
        ),
        response,
    )


@router.delete("/{provider_id}", response_model=ProviderMutationResult)
async def delete_model_provider(
    provider_id: str,
    response: Response,
    management: ProviderManagementDependency,
    if_match: IfMatchHeader = None,
    idempotency_key: IdempotencyHeader = None,
) -> ProviderMutationResult:
    return await _execute_mutation(
        management.delete(
            provider_id,
            expected_catalog_version=_expected_version(if_match),
            idempotency_key=_idempotency_key(idempotency_key),
        ),
        response,
    )


@router.get("/{provider_id}/health", response_model=ProviderHealthSnapshot)
async def check_model_provider_health(
    provider_id: str,
    catalog: ProviderCatalogDependency,
) -> ProviderHealthSnapshot:
    try:
        return await catalog.probe(provider_id)
    except UnknownModelProviderError as error:
        raise ProblemException(
            status_code=404,
            code=error.code,
            title="模型服务不存在",
            detail=f"没有找到模型服务 {provider_id}。",
        ) from error
    except DisabledModelProviderError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="模型服务已禁用",
            detail=f"模型服务 {provider_id} 当前已禁用，未执行网络探测。",
        ) from error


async def _execute_mutation(
    operation: Awaitable[ProviderMutationResult],
    response: Response,
) -> ProviderMutationResult:
    try:
        result = await operation
    except ProviderCatalogVersionConflictError as error:
        raise ProblemException(
            status_code=412,
            code=error.code,
            title="Provider 配置版本冲突",
            detail="Provider 配置已被其他操作修改，请刷新后重试。",
            extensions={
                "expected_version": error.expected_version,
                "actual_version": error.actual_version,
                "current_etag": provider_catalog_etag(error.actual_version),
            },
        ) from error
    except ProviderIdempotencyConflictError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="幂等键冲突",
            detail="该 Idempotency-Key 已用于另一个 Provider 请求。",
        ) from error
    except ProviderAlreadyExistsError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="模型服务已存在",
            detail=f"模型服务 {error.provider_id} 已存在。",
        ) from error
    except ProviderManagementNotFoundError as error:
        raise ProblemException(
            status_code=404,
            code=error.code,
            title="模型服务不存在",
            detail=f"没有找到模型服务 {error.provider_id}。",
        ) from error
    except ProviderManagementConflictError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="Provider 操作冲突",
            detail=str(error),
        ) from error
    except CredentialResolutionError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="Provider 凭据不可用",
            detail="模型服务引用的凭据当前不可用，未修改任何配置。",
        ) from error
    except ProviderRuntimeConfigProtectionUnavailableError as error:
        raise ProblemException(
            status_code=503,
            code=error.code,
            title="运行配置保护不可用",
            detail="当前系统无法安全保存 Provider 运行配置。",
        ) from error
    except ProviderRuntimeConfigProtectionError as error:
        raise ProblemException(
            status_code=503,
            code=error.code,
            title="运行配置保护失败",
            detail="Provider 运行配置未能安全保存，未应用本次修改。",
        ) from error
    response.headers["ETag"] = provider_catalog_etag(result.catalog_version)
    response.headers["Cache-Control"] = "no-store"
    return result


def _expected_version(value: str | None) -> int:
    if value is None:
        raise ProblemException(
            status_code=428,
            code="IF_MATCH_REQUIRED",
            title="缺少并发条件",
            detail="Provider 写请求必须携带最新的 If-Match ETag。",
        )
    matched = _ETAG_PATTERN.fullmatch(value.strip())
    if matched is None:
        raise ProblemException(
            status_code=400,
            code="IF_MATCH_INVALID",
            title="并发条件格式错误",
            detail="If-Match 必须是单个 DeskPilot Provider Catalog ETag。",
        )
    return int(matched.group(1))


def _idempotency_key(value: str | None) -> str:
    if value is None:
        raise ProblemException(
            status_code=400,
            code="IDEMPOTENCY_KEY_REQUIRED",
            title="缺少幂等键",
            detail="Provider 写请求必须携带 Idempotency-Key。",
        )
    if _IDEMPOTENCY_PATTERN.fullmatch(value) is None:
        raise ProblemException(
            status_code=400,
            code="IDEMPOTENCY_KEY_INVALID",
            title="幂等键格式错误",
            detail="Idempotency-Key 必须是 16 到 128 位安全 ASCII 标识符。",
        )
    return value
