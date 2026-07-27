from pathlib import Path

import pytest

from quantking.common.config import AppSettings, load_settings
from quantking.common.errors import ConfigurationError, InvalidPathError


def _write_defaults(path: Path, *, web_port: int = 8765) -> None:
    path.write_text(
        "\n".join(
            [
                "[paths]",
                f'code_root = "{path.parent.as_posix()}/code"',
                f'data_root = "{path.parent.as_posix()}/data"',
                f'offsite_backup_root = "{path.parent.as_posix()}/offsite"',
                "",
                "[xtquant]",
                f'site_packages_root = "{path.parent.as_posix()}/sdk-site-packages"',
                "",
                "[web]",
                'host = "127.0.0.1"',
                f"port = {web_port}",
                "",
                "[sqlite]",
                "busy_timeout_ms = 250",
                "max_attempts = 7",
                "retry_base_delay_ms = 10",
                "retry_cap_delay_ms = 160",
                "retry_jitter_ratio = 0.25",
                "heartbeat_min_interval_ms = 1000",
                "progress_min_interval_ms = 250",
            ]
        ),
        encoding="utf-8",
    )


def test_load_settings_uses_defaults_and_known_environment_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "default.toml"
    _write_defaults(config_path)

    settings = load_settings(
        config_path,
        environ={
            "QUANTKING_DATA_ROOT": str(tmp_path / "overridden-data"),
            "QUANTKING_WEB_PORT": "9000",
            "QUANTKING_SQLITE_BUSY_TIMEOUT_MS": "7500",
        },
    )

    assert isinstance(settings, AppSettings)
    assert settings.paths.data_root == tmp_path / "overridden-data"
    assert settings.paths.local_backup_root == tmp_path / "overridden-data" / "backups"
    assert settings.xtquant_site_packages_root == tmp_path / "sdk-site-packages"
    assert settings.web_host == "127.0.0.1"
    assert settings.web_port == 9000
    assert settings.sqlite_busy_timeout_ms == 7500
    assert settings.sqlite_max_attempts == 7
    assert settings.sqlite_retry_base_delay_ms == 10
    assert settings.sqlite_retry_cap_delay_ms == 160
    assert settings.sqlite_retry_jitter_ratio == 0.25
    assert settings.job_heartbeat_min_interval_ms == 1000
    assert settings.job_progress_min_interval_ms == 250


def test_load_settings_rejects_unknown_toml_key(tmp_path: Path) -> None:
    config_path = tmp_path / "default.toml"
    _write_defaults(config_path)
    with config_path.open("a", encoding="utf-8") as stream:
        stream.write("\nunknown = true\n")

    with pytest.raises(ConfigurationError, match="unknown"):
        load_settings(config_path, environ={})


def test_load_settings_rejects_unknown_or_empty_prefixed_environment_values(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "default.toml"
    _write_defaults(config_path)

    with pytest.raises(ConfigurationError, match="unknown"):
        load_settings(config_path, environ={"QUANTKING_SECRET_TOKEN": "do-not-read"})
    with pytest.raises(ConfigurationError, match="empty"):
        load_settings(config_path, environ={"QUANTKING_WEB_PORT": "  "})


def test_load_settings_wraps_invalid_path_with_stable_error_chain(tmp_path: Path) -> None:
    config_path = tmp_path / "default.toml"
    _write_defaults(config_path)

    with pytest.raises(ConfigurationError) as caught:
        load_settings(config_path, environ={"QUANTKING_DATA_ROOT": "relative/data"})

    assert isinstance(caught.value.__cause__, InvalidPathError)


@pytest.mark.parametrize("web_port", [0, 65536])
def test_load_settings_rejects_out_of_range_values(tmp_path: Path, web_port: int) -> None:
    config_path = tmp_path / "default.toml"
    _write_defaults(config_path, web_port=web_port)

    with pytest.raises(ConfigurationError, match="web_port"):
        load_settings(config_path, environ={})


@pytest.mark.parametrize(
    "environment",
    [
        {"QUANTKING_SQLITE_MAX_ATTEMPTS": "21"},
        {"QUANTKING_SQLITE_RETRY_BASE_DELAY_MS": "5001"},
        {"QUANTKING_SQLITE_RETRY_CAP_DELAY_MS": "5001"},
        {"QUANTKING_JOB_HEARTBEAT_MIN_INTERVAL_MS": "60001"},
        {"QUANTKING_JOB_PROGRESS_MIN_INTERVAL_MS": "60001"},
    ],
)
def test_load_settings_rejects_retry_values_that_defeat_bounded_waits(
    tmp_path: Path,
    environment: dict[str, str],
) -> None:
    config_path = tmp_path / "default.toml"
    _write_defaults(config_path)

    with pytest.raises(ConfigurationError):
        load_settings(config_path, environ=environment)
