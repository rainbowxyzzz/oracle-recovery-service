from types import SimpleNamespace

import pytest

from recovery_service.api.v1.tasks import _is_platform_managed_oracle_target
from recovery_service.core.domain import RemoteHost, TargetDatabase
from recovery_service.core.exceptions import RemoteAccessError
from recovery_service.domain.import_job import (
    ImportJobConfig,
    OracleDockerTargetConfig,
    SourceServerConfig,
)
from recovery_service.orchestrator.professional_pipeline import _route_managed_oracle_target


def _job() -> ImportJobConfig:
    host = RemoteHost("oracle-host", username="root", password="secret")
    return ImportJobConfig(
        source=SourceServerConfig(host=host, directory="/dmp"),
        oracle_docker=OracleDockerTargetConfig(
            host=host,
            container="oracle-recovery-oracle21c-ee",
            dmp_host_path="/data/oracle-recovery/oracle21c/dmp",
            dmp_container_path="/opt/oracle/recovery_dmp",
            tablespace_container_path="/opt/oracle/recovery_tablespaces",
        ),
        target=TargetDatabase(
            connection_string="oracle-recovery-oracle21c-ee:1521/ORCLPDB1",
            admin_user="SYSTEM",
            admin_password="secret",
        ),
        generated_user_password="secret",
    )


ROUTING = {
    "enabled": True,
    "primary_character_set": "ZHS16GBK",
    "utf8": {
        "container": "oracle-recovery-oracle21c-utf8",
        "connection": "oracle-recovery-oracle21c-utf8:1521/ORCLPDBUTF8",
        "character_set": "AL32UTF8",
    },
}


def test_saved_profile_for_managed_container_keeps_routing_enabled():
    settings = SimpleNamespace(
        oracle_container_name="oracle-recovery-oracle19c",
        oracle_charset_primary_container_name="oracle-recovery-oracle21c-ee",
    )
    managed_profile = SimpleNamespace(
        container_name="oracle-recovery-oracle21c-ee",
        host="oracle-recovery-oracle21c-ee",
    )
    external_profile = SimpleNamespace(container_name=None, host="10.20.30.40")

    assert _is_platform_managed_oracle_target(managed_profile, settings) is True
    assert _is_platform_managed_oracle_target(external_profile, settings) is False


def test_al32utf8_export_log_routes_before_probe():
    job, decision = _route_managed_oracle_target(_job(), ROUTING, "AL32UTF8")

    assert job.oracle_docker.container == "oracle-recovery-oracle21c-utf8"
    assert job.target.connection_string.endswith("/ORCLPDBUTF8")
    assert decision["reason"] == "source_character_set"


def test_zhs16gbk_keeps_primary_target():
    job, decision = _route_managed_oracle_target(_job(), ROUTING, "ZHS16GBK")

    assert job.oracle_docker.container == "oracle-recovery-oracle21c-ee"
    assert decision["selected_character_set"] == "ZHS16GBK"


def test_probe_charset_failure_forces_utf8_target():
    job, decision = _route_managed_oracle_target(_job(), ROUTING, "", force_utf8=True)

    assert job.oracle_docker.container == "oracle-recovery-oracle21c-utf8"
    assert decision["reason"] == "probe_character_set_incompatible"
    assert decision["rerouted"] is True


def test_disabled_routing_never_overrides_explicit_target():
    job, decision = _route_managed_oracle_target(
        _job(),
        {**ROUTING, "enabled": False},
        "AL32UTF8",
    )

    assert job.oracle_docker.container == "oracle-recovery-oracle21c-ee"
    assert decision["reason"] == "routing_disabled"


def test_utf8_target_missing_blocks_instead_of_falling_back():
    with pytest.raises(RemoteAccessError, match="禁止回退"):
        _route_managed_oracle_target(_job(), {"enabled": True, "utf8": {}}, "AL32UTF8")
