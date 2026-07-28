"""Formal local Web application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from quantking.common.errors import (
    ErrorCode,
    IdempotencyConflictError,
    JobConflictError,
    JobNotFoundError,
    QuantKingError,
)
from quantking.common.time import Clock
from quantking.data.adapters.warehouse.errors import (
    DatasetVersionNotQueryableError,
    QueryContractError,
)
from quantking.data.center import DataCenterReadIntegrityError, InvalidDataCenterQueryError
from quantking.data.comparison.verifier import DifferenceReportVerificationError
from quantking.web.api import InvalidEodRequestError, InvalidRequestError, create_api_router
from quantking.web.data_center import DataCenterReadPort
from quantking.web.ports import EodCommandPort, JobCommandPort, JobReadPort


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
    )


def _validate_static_root(static_root: Path | None) -> Path | None:
    if static_root is None:
        return None
    if not isinstance(static_root, Path) or not static_root.is_absolute():
        raise ValueError("static root must be an absolute directory")
    try:
        resolved = static_root.resolve(strict=True)
    except OSError as error:
        raise ValueError("static root must exist") from error
    if not resolved.is_dir():
        raise ValueError("static root must be a directory")
    index = resolved / "index.html"
    if not index.is_file() or index.resolve(strict=True).parent != resolved:
        raise ValueError("static root must contain a safe index.html")
    assets = resolved / "assets"
    if assets.is_symlink() or assets.exists():
        try:
            resolved_assets = assets.resolve(strict=True)
        except OSError as error:
            raise ValueError("static root contains an unsafe assets directory") from error
        if not resolved_assets.is_dir() or not resolved_assets.is_relative_to(resolved):
            raise ValueError("static root contains an unsafe assets directory")
    return resolved


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(InvalidRequestError)
    async def invalid_request(_request: Request, _error_value: InvalidRequestError) -> JSONResponse:
        return _error(400, "invalid_request", "事件查询范围无效。")

    @app.exception_handler(InvalidEodRequestError)
    async def invalid_eod_request(
        _request: Request, _error_value: InvalidEodRequestError
    ) -> JSONResponse:
        return _error(400, "invalid_eod_request", "EOD 请求参数无效。")

    @app.exception_handler(InvalidDataCenterQueryError)
    async def invalid_data_query(
        _request: Request, _error_value: InvalidDataCenterQueryError
    ) -> JSONResponse:
        return _error(400, "invalid_data_query", "数据查询参数无效。")

    @app.exception_handler(DataCenterReadIntegrityError)
    async def data_read_integrity(
        _request: Request, _error_value: DataCenterReadIntegrityError
    ) -> JSONResponse:
        return _error(409, "data_lineage_invalid", "数据血缘验证失败。")

    @app.exception_handler(QueryContractError)
    async def query_contract(_request: Request, _error_value: QueryContractError) -> JSONResponse:
        return _error(400, "invalid_data_query", "数据查询参数无效。")

    @app.exception_handler(DatasetVersionNotQueryableError)
    async def version_not_queryable(
        _request: Request, _error_value: DatasetVersionNotQueryableError
    ) -> JSONResponse:
        return _error(409, "version_not_queryable", "数据版本当前不可查询。")

    @app.exception_handler(DifferenceReportVerificationError)
    async def difference_not_verifiable(
        _request: Request, _error_value: DifferenceReportVerificationError
    ) -> JSONResponse:
        return _error(409, "difference_report_invalid", "差异报告验证失败。")

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        _request: Request,
        _error_value: RequestValidationError,
    ) -> JSONResponse:
        return _error(422, "validation_error", "请求参数无效。")

    @app.exception_handler(JobNotFoundError)
    async def missing_job(_request: Request, _error_value: JobNotFoundError) -> JSONResponse:
        return _error(404, ErrorCode.JOB_NOT_FOUND.value, "任务不存在。")

    @app.exception_handler(IdempotencyConflictError)
    async def idempotency_conflict(
        _request: Request, _error_value: IdempotencyConflictError
    ) -> JSONResponse:
        return _error(409, ErrorCode.IDEMPOTENCY_CONFLICT.value, "幂等键与既有请求冲突。")

    @app.exception_handler(JobConflictError)
    async def job_conflict(_request: Request, _error_value: JobConflictError) -> JSONResponse:
        return _error(409, ErrorCode.JOB_CONFLICT.value, "任务状态已变化或当前不可取消。")

    @app.exception_handler(QuantKingError)
    async def quantking_error(_request: Request, error_value: QuantKingError) -> JSONResponse:
        status = 503 if error_value.code is ErrorCode.PERSISTENCE_BUSY else 400
        return _error(status, error_value.code.value, "请求无法完成。")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, error_value: StarletteHTTPException) -> JSONResponse:
        if error_value.status_code == 404:
            return _error(404, "not_found", "资源不存在。")
        return _error(error_value.status_code, "http_error", "HTTP 请求失败。")


def create_app(
    job_port: JobReadPort,
    *,
    eod_command_port: EodCommandPort | None = None,
    job_command_port: JobCommandPort | None = None,
    static_root: Path | None = None,
    clock: Clock | None = None,
    data_center_port: DataCenterReadPort | None = None,
) -> FastAPI:
    """Build an app from injected ports; construction has no persistence side effects."""
    resolved_static = _validate_static_root(static_root)
    app = FastAPI(title="QuantKing 本地控制台", docs_url=None, redoc_url=None)
    _install_error_handlers(app)
    app.include_router(
        create_api_router(
            job_port,
            eod_command_port=eod_command_port,
            job_command_port=job_command_port,
            clock=clock,
            data_center_port=data_center_port,
        )
    )

    @app.api_route(
        "/api/{unmatched:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def unmatched_api(unmatched: str) -> JSONResponse:
        del unmatched
        return _error(404, "not_found", "资源不存在。")

    if resolved_static is not None:
        assets = resolved_static / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets, check_dir=True), name="assets")
        index = resolved_static / "index.html"

        @app.get("/{path:path}", response_class=FileResponse)
        async def spa(path: str) -> FileResponse:
            del path
            return FileResponse(index)

    return app
