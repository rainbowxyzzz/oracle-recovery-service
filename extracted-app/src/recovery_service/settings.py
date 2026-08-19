from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

import yaml
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_service_mode: str = "monolith"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    app_timezone: str = "Asia/Shanghai"
    mysql_session_time_zone: str = "+08:00"
    secret_key: str = "change-me"
    access_token_expire_minutes: int = 60
    default_admin_username: str = "admin"
    default_admin_password: str = "admin123"
    default_admin_display_name: str = "系统管理员"

    # MySQL（支持远程；可用 DATABASE_URL 完全覆盖）
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "recovery"
    mysql_password: str = "recovery"
    mysql_database: str = "oracle_recovery"
    mysql_charset: str = "utf8mb4"
    database_url: str = ""
    database_url_sync: str = ""

    # Redis（默认本机）
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db_broker: int = 0
    redis_db_result: int = 1
    redis_url: str = ""
    celery_broker_url: str = ""
    celery_result_backend: str = ""
    celery_default_queue: str = "celery"
    celery_oracle_queue: str = "oracle_restore"
    celery_sm3_queue: str = "doris_sm3"
    celery_sm4_queue: str = "doris_sm4"
    celery_sql_queue: str = "doris_sql"
    celery_data_sync_queue: str = "data_sync"
    celery_data_platform_queue: str = "data_platform"
    celery_resource_provisioning_queue: str = "resource_provisioning"
    celery_api_orchestration_queue: str = "api_orchestration"
    celery_worker_prefetch_multiplier: int = 1
    celery_visibility_timeout_seconds: int = 259200
    worker_service_mode: str = "monolith"
    worker_queues: str = ""
    sm3_worker_concurrency: int = 2
    sm4_worker_concurrency: int = 2
    sm4_connection_concurrency: int = 1
    sm4_database_concurrency: int = 1
    sm4_recover_active_jobs_on_startup: bool = True
    data_sync_max_table_parallelism: int = 8
    resource_provisioning_max_parallelism: int = 10

    oracle_client_lib_dir: str = ""
    oracle_target_mode: str = "auto"
    oracle_target_host: str = ""
    oracle_image: str = "53661f3d548e"
    oracle_image_prefixes: str = "oracle,oracle19c,liujunel/oracle19c"
    oracle_container_name: str = "oracle-recovery-oracle19c"
    oracle_default_container_name: str = "oracle-recovery-oracle19c"
    oracle_container_prefixes: str = "oracle-recovery-oracle,oracle,oracle19c"
    oracle_sid: str = "ORCLCDB"
    oracle_pdb: str = "ORCLPDB1"
    oracle_pwd: str = "ChangeMe_Oracle19c_123"
    oracle_host_port: int = 1521
    oracle_docker_host: str = "host.docker.internal"
    oracle_docker_ssh_port: int = 22
    oracle_docker_ssh_user: str = "root"
    oracle_docker_ssh_password: str = ""
    oracle_docker_sudo_password: str = ""
    oracle_base_host_path: str = "/data/oracle-recovery/oracle19c"
    oracle_dmp_host_path: str = "/data/oracle-recovery/oracle19c/dmp"
    oracle_dmp_container_path: str = "/opt/oracle/recovery_dmp"
    oracle_tablespace_host_path: str = "/data/oracle-recovery/oracle19c/tablespaces"
    oracle_tablespace_container_path: str = "/opt/oracle/recovery_tablespaces"
    oracle_tablespace_bigfile: bool = True
    oracle_tablespace_initial_size: str = "10G"
    oracle_tablespace_next_size: str = "1G"
    oracle_tablespace_max_size: str = "UNLIMITED"
    oracle_tablespace_auto_grow_on_ora_01653: bool = True
    oracle_tablespace_auto_grow_add_datafile_size: str = "10G"
    oracle_home_in_container: str = "/opt/oracle/product/19c/dbhome_1"
    oracle_directory: str = "RECOVERY_DMP_DIR"
    oracle_auto_import_python_bin: str = ""
    sqlserver_target_mode: str = "auto"
    sqlserver_target_host: str = ""
    sqlserver_image: str = "f191949a09a6"
    sqlserver_image_prefixes: str = "mcr.microsoft.com/mssql/server,sqlserver,mssql"
    sqlserver_container_name: str = "sqlserver-recovery-mssql"
    sqlserver_default_container_name: str = "sqlserver-recovery-mssql"
    sqlserver_container_prefixes: str = "sqlserver-recovery,sqlserver,mssql"
    sqlserver_sa_password: str = "ChangeMe_SqlServer_123!"
    sqlserver_host_port: int = 1433
    sqlserver_docker_host: str = "host.docker.internal"
    sqlserver_docker_ssh_port: int = 22
    sqlserver_docker_ssh_user: str = "root"
    sqlserver_docker_ssh_password: str = ""
    sqlserver_docker_sudo_password: str = ""
    sqlserver_base_host_path: str = "/data/sqlserver-recovery"
    sqlserver_file_host_path: str = "/data/sqlserver-recovery/files"
    sqlserver_data_host_path: str = "/data/sqlserver-recovery/data"
    sqlserver_file_container_path: str = "/var/opt/mssql/recovery_files"
    sqlserver_data_container_path: str = "/var/opt/mssql/data"
    mysql_restore_target_mode: str = "auto"
    mysql_restore_target_host: str = ""
    mysql_restore_image: str = "mysql:8.4"
    mysql_restore_image_prefixes: str = "mysql"
    mysql_restore_container_name: str = "mysql-recovery-target"
    mysql_restore_default_container_name: str = "mysql-recovery-target"
    mysql_restore_container_prefixes: str = "mysql-recovery,mysql"
    mysql_restore_root_password: str = "ChangeMe_MySqlRestore_123!"
    mysql_restore_host_port: int = 3307
    mysql_restore_docker_host: str = "host.docker.internal"
    mysql_restore_docker_ssh_port: int = 22
    mysql_restore_docker_ssh_user: str = "root"
    mysql_restore_docker_ssh_password: str = ""
    mysql_restore_docker_sudo_password: str = ""
    mysql_restore_base_host_path: str = "/data/mysql-recovery"
    mysql_restore_backup_host_path: str = "/data/mysql-recovery/backup"
    mysql_restore_data_host_path: str = "/data/mysql-recovery/data"
    mysql_restore_backup_container_path: str = "/recovery_backup"
    mysql_restore_import_timeout_seconds: int = 14400
    staging_dir: str = "/tmp/oracle-recovery-staging"
    impdp_bin: str = "impdp"
    sqlplus_bin: str = "sqlplus"
    task_lock_ttl_seconds: int = 7200
    default_impdp_timeout_seconds: int = 604800
    oracle_import_operation_timeout_seconds: int = 604800
    oracle_metadata_probe_timeout_seconds: int = 7200
    oracle_ssh_check_timeout_seconds: int = 600
    oracle_auto_import_preflight_retries: int = 2
    oracle_auto_import_preflight_retry_delay_seconds: int = 5
    oracle_auto_import_skip_preflight_codes: str = ""
    auto_confirm_import: bool = False
    credential_encryption_key: str = ""
    doris_sm4_udf_jar_dir: str = "/app/data/sm4-jars"
    doris_sm4_udf_public_base_url: str = ""
    doris_sm4_javac_bin: str = ""
    doris_sm4_jar_bin: str = ""
    doris_encryption_replication_allocation: str = "tag.location.default: 1"

    config_dir: Path = Field(default=CONFIG_DIR)

    @model_validator(mode="after")
    def build_connection_urls(self) -> "Settings":
        if not self.database_url:
            pw = quote_plus(self.mysql_password)
            self.database_url = (
                f"mysql+aiomysql://{self.mysql_user}:{pw}"
                f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
                f"?charset={self.mysql_charset}"
            )
        if not self.database_url_sync:
            pw = quote_plus(self.mysql_password)
            self.database_url_sync = (
                f"mysql+pymysql://{self.mysql_user}:{pw}"
                f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
                f"?charset={self.mysql_charset}"
            )
        if not self.redis_url:
            self.redis_url = self._redis_url(self.redis_db_broker)
        if not self.celery_broker_url:
            self.celery_broker_url = self._redis_url(self.redis_db_broker)
        if not self.celery_result_backend:
            self.celery_result_backend = self._redis_url(self.redis_db_result)
        return self

    def _redis_url(self, db: int) -> str:
        if self.redis_password:
            pw = quote_plus(self.redis_password)
            return f"redis://:{pw}@{self.redis_host}:{self.redis_port}/{db}"
        return f"redis://{self.redis_host}:{self.redis_port}/{db}"

    def load_yaml(self, name: str) -> dict:
        path = self.config_dir / name
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}


@lru_cache
def get_settings() -> Settings:
    return Settings()
