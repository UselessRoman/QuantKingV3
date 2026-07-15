from types import MappingProxyType

import pytest

from quantking.common.errors import (
    ConfigurationError,
    ErrorCode,
    IdempotencyConflictError,
    InvalidIdError,
    InvalidJobTransitionError,
    InvalidPathError,
    InvalidTimeError,
    JobNotFoundError,
    PersistenceBusyError,
    QuantKingError,
    UnknownJobTypeError,
    UpstreamJobNotReadyError,
)


def test_error_has_stable_code_message_and_read_only_string_context() -> None:
    error = QuantKingError(
        ErrorCode.CONFIGURATION,
        "configuration is invalid",
        context={"field": "web_port"},
    )

    assert error.code is ErrorCode.CONFIGURATION
    assert error.message == "configuration is invalid"
    assert error.context == {"field": "web_port"}
    assert isinstance(error.context, MappingProxyType)
    assert str(error) == "[configuration_error] configuration is invalid (field=web_port)"
    with pytest.raises(TypeError):
        error.context["field"] = "other"  # type: ignore[index]


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        (ConfigurationError, ErrorCode.CONFIGURATION),
        (InvalidIdError, ErrorCode.INVALID_ID),
        (InvalidTimeError, ErrorCode.INVALID_TIME),
        (InvalidPathError, ErrorCode.INVALID_PATH),
        (InvalidJobTransitionError, ErrorCode.INVALID_JOB_TRANSITION),
        (IdempotencyConflictError, ErrorCode.IDEMPOTENCY_CONFLICT),
        (JobNotFoundError, ErrorCode.JOB_NOT_FOUND),
        (PersistenceBusyError, ErrorCode.PERSISTENCE_BUSY),
        (UnknownJobTypeError, ErrorCode.UNKNOWN_JOB_TYPE),
        (UpstreamJobNotReadyError, ErrorCode.UPSTREAM_JOB_NOT_READY),
    ],
)
def test_public_error_subclasses_have_fixed_codes(
    error_type: type[QuantKingError], code: ErrorCode
) -> None:
    assert error_type("invalid").code is code


def test_error_rejects_non_string_context() -> None:
    with pytest.raises(TypeError):
        QuantKingError(
            ErrorCode.CONFIGURATION,
            "invalid",
            context={"attempt": 2},  # type: ignore[dict-item]
        )


def test_python_exception_chaining_is_preserved() -> None:
    cause = ValueError("vendor detail")

    try:
        raise ConfigurationError("stable public message") from cause
    except ConfigurationError as error:
        assert error.__cause__ is cause
