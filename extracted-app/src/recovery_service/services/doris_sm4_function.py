from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import subprocess
import textwrap
import base64
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from recovery_service.api.schemas.doris_encryption import (
    DorisSm4FunctionDatabaseResult,
    DorisSm4FunctionDeploymentResponse,
    DorisSm4FunctionRefreshResponse,
)
from recovery_service.common.time import app_now
from recovery_service.core.models.task import DatabaseConnectionProfile, DorisSm4FunctionDeployment
from recovery_service.db.session import get_sync_session_factory
from recovery_service.services.doris_encryption import _doris_conn
from recovery_service.services.auth import AuthContext
from recovery_service.services.sm4_key_versions import (
    get_active_sm4_key_seed_for_jar,
    get_sm4_key_seed,
    register_sm4_key_version,
)
from recovery_service.settings import get_settings

_SYSTEM_DATABASES = {
    "__internal_schema",
    "information_schema",
    "mysql",
    "performance_schema",
    "sys",
}
_JAR_RECOVERY_LOCK = threading.Lock()


@dataclass(frozen=True)
class BuiltSm4Jar:
    filename: str
    path: Path
    url: str
    symbol: str
    decrypt_symbol: str
    key_seed: str
    key_fingerprint: str
    verification_plaintext: str
    verification_ciphertext: str


def build_sm4_udf_jar(*, sm4_key: str | None, public_base_url: str) -> BuiltSm4Jar:
    key_seed = _normalize_sm4_seed(sm4_key)
    fingerprint = hashlib.sha256(key_seed.encode("utf-8")).hexdigest()[:16]
    symbol = f"CqSm4Encrypt_{fingerprint}"
    decrypt_symbol = f"CqSm4Decrypt_{fingerprint}"
    verification_plaintext = f"oracle-recovery-sm4-verify-{fingerprint}"
    verification_ciphertext = sm4_encrypt_to_base64(verification_plaintext, key_seed)
    jar_dir = Path(get_settings().doris_sm4_udf_jar_dir)
    jar_dir.mkdir(parents=True, exist_ok=True)
    filename = f"cq-sm4-encrypt-{fingerprint}-v6.jar"
    jar_path = jar_dir / filename
    if jar_path.exists():
        return BuiltSm4Jar(
            filename=filename,
            path=jar_path,
            url=f"{public_base_url.rstrip('/')}/{filename}",
            symbol=symbol,
            decrypt_symbol=decrypt_symbol,
            key_seed=key_seed,
            key_fingerprint=fingerprint,
            verification_plaintext=verification_plaintext,
            verification_ciphertext=verification_ciphertext,
        )

    settings = get_settings()
    javac = _find_bin(settings.doris_sm4_javac_bin, "javac")
    jar_bin = _find_bin(settings.doris_sm4_jar_bin, "jar")
    if not javac or not jar_bin:
        raise RuntimeError("当前 API 运行环境缺少 javac 或 jar，无法动态生成 SM4 UDF jar。")

    work_dir = jar_dir / f"build-{fingerprint}"
    src_dir = work_dir / "src"
    classes_dir = work_dir / "classes"
    src_dir.mkdir(parents=True, exist_ok=True)
    classes_dir.mkdir(parents=True, exist_ok=True)
    java_file = src_dir / f"{symbol}.java"
    decrypt_java_file = src_dir / f"{decrypt_symbol}.java"
    java_file.write_text(_java_source(symbol, key_seed, mode="encrypt"), encoding="utf-8")
    decrypt_java_file.write_text(_java_source(decrypt_symbol, key_seed, mode="decrypt"), encoding="utf-8")

    try:
        _compile_java8_udf(
            javac,
            classes_dir=classes_dir,
            java_files=[java_file, decrypt_java_file],
        )
        subprocess.run(
            [jar_bin, "cf", str(jar_path), "-C", str(classes_dir), "."],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(f"生成 SM4 UDF jar 失败：{detail}") from exc
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return BuiltSm4Jar(
        filename=filename,
        path=jar_path,
        url=f"{public_base_url.rstrip('/')}/{filename}",
        symbol=symbol,
        decrypt_symbol=decrypt_symbol,
        key_seed=key_seed,
        key_fingerprint=fingerprint,
        verification_plaintext=verification_plaintext,
        verification_ciphertext=verification_ciphertext,
    )


def refresh_sm4_functions(
    profile: DatabaseConnectionProfile,
    *,
    sm4_key: str | None,
    key_mode: str = "random",
    public_base_url: str,
    function_name: str = "CQ_SM4_ENCRYPT",
    decrypt_function_name: str | None = None,
    include_system_databases: bool = False,
    databases: list[str] | None = None,
    actor: AuthContext | None = None,
) -> DorisSm4FunctionRefreshResponse:
    clean_function = _clean_identifier(function_name, "函数名")
    clean_decrypt_function = _clean_identifier(
        decrypt_function_name or _derive_decrypt_function_name(clean_function),
        "解密函数名",
    )
    jar = build_sm4_udf_jar(sm4_key=sm4_key, public_base_url=public_base_url)
    selected_databases = [_clean_identifier(item, "数据库名") for item in databases or [] if item.strip()]
    with _doris_conn(profile, None) as db:
        with db.cursor() as cur:
            target_databases = selected_databases or _list_databases(cur, include_system_databases)
            results: list[DorisSm4FunctionDatabaseResult] = []
            for database in target_databases:
                result = _refresh_function_in_database(cur, database, clean_function, clean_decrypt_function, jar)
                result.attempted_at = app_now()
                results.append(result)

    success_count = sum(1 for item in results if item.state == "success")
    failed_count = sum(1 for item in results if item.state == "failed")
    if results and failed_count == 0:
        state = "success"
        message = f"已在 {success_count} 个 Doris 库中创建 {clean_function}。"
    elif success_count:
        state = "partial"
        message = f"部分库创建成功：成功 {success_count} 个，失败 {failed_count} 个。"
    else:
        state = "failed"
        message = "未能成功创建 Doris SM4 函数。"
    key_version = None
    if success_count > 0:
        key_version = register_sm4_key_version(
            key_seed=jar.key_seed,
            key_mode=key_mode,
            connection_id=profile.id,
            connection_name=profile.name,
            function_name=clean_function,
            decrypt_function_name=clean_decrypt_function,
            jar_filename=jar.filename,
            actor=actor,
        )
    _record_sm4_function_deployments(
        profile=profile,
        function_name=clean_function,
        decrypt_function_name=clean_decrypt_function,
        jar=jar,
        key_version_id=key_version.key_id if key_version else None,
        results=results,
    )
    return DorisSm4FunctionRefreshResponse(
        state=state,  # type: ignore[arg-type]
        message=message,
        function_name=clean_function,
        decrypt_function_name=clean_decrypt_function,
        key_id=key_version.key_id if key_version else None,
        key_fingerprint=jar.key_fingerprint,
        jar_filename=jar.filename,
        jar_url=jar.url,
        total_databases=len(results),
        success_count=success_count,
        failed_count=failed_count,
        results=results,
    )


def list_sm4_function_deployments(*, connection_id) -> list[DorisSm4FunctionDeploymentResponse]:
    session = get_sync_session_factory()()
    try:
        rows = session.execute(
            select(DorisSm4FunctionDeployment)
            .where(DorisSm4FunctionDeployment.connection_id == connection_id)
            .order_by(DorisSm4FunctionDeployment.database)
        ).scalars().all()
        return [_deployment_response(row) for row in rows]
    finally:
        session.close()


def _record_sm4_function_deployments(
    *,
    profile: DatabaseConnectionProfile,
    function_name: str,
    decrypt_function_name: str,
    jar: BuiltSm4Jar,
    key_version_id,
    results: list[DorisSm4FunctionDatabaseResult],
) -> None:
    session = get_sync_session_factory()()
    try:
        for result in results:
            attempted_at = result.attempted_at or app_now()
            row = session.execute(
                select(DorisSm4FunctionDeployment).where(
                    DorisSm4FunctionDeployment.connection_id == profile.id,
                    DorisSm4FunctionDeployment.database == result.database,
                    DorisSm4FunctionDeployment.function_name == function_name,
                )
            ).scalar_one_or_none()
            if row is None:
                row = DorisSm4FunctionDeployment(
                    connection_id=profile.id,
                    database=result.database,
                    function_name=function_name,
                    attempted_at=attempted_at,
                    state=result.state,
                    created_at=attempted_at,
                )
                session.add(row)
            row.connection_name = profile.name
            row.decrypt_function_name = decrypt_function_name
            row.key_version_id = key_version_id
            row.key_fingerprint = jar.key_fingerprint
            row.jar_filename = jar.filename
            row.state = result.state
            row.message = result.message
            row.verification_state = result.verification_state
            row.verification_message = result.verification_message
            row.attempted_at = attempted_at
            if result.state == "success":
                row.last_success_at = attempted_at
            row.updated_at = attempted_at
        session.commit()
    finally:
        session.close()


def _deployment_response(row: DorisSm4FunctionDeployment) -> DorisSm4FunctionDeploymentResponse:
    return DorisSm4FunctionDeploymentResponse(
        connection_id=row.connection_id,
        connection_name=row.connection_name,
        database=row.database,
        function_name=row.function_name,
        decrypt_function_name=row.decrypt_function_name,
        key_version_id=row.key_version_id,
        key_fingerprint=row.key_fingerprint,
        jar_filename=row.jar_filename,
        state=row.state,  # type: ignore[arg-type]
        message=row.message or "",
        verification_state=row.verification_state,  # type: ignore[arg-type]
        verification_message=row.verification_message,
        attempted_at=row.attempted_at,
        last_success_at=row.last_success_at,
    )


def ensure_sm4_key_version_jar(key_id, expected_filename: str | None = None) -> BuiltSm4Jar:
    key_seed = get_sm4_key_seed(key_id)
    public_base_url = get_settings().doris_sm4_udf_public_base_url.strip() or "http://127.0.0.1"
    jar = build_sm4_udf_jar(sm4_key=key_seed, public_base_url=public_base_url)
    if expected_filename and jar.filename != expected_filename:
        raise RuntimeError(
            f"SM4 密钥版本对应的 JAR 文件名不一致：记录={expected_filename}，计算={jar.filename}。"
        )
    return jar


def sm4_jar_path(filename: str) -> Path:
    clean_name = Path(filename).name
    if clean_name != filename or not clean_name.endswith(".jar"):
        raise ValueError("jar 文件名非法。")
    path = Path(get_settings().doris_sm4_udf_jar_dir) / clean_name
    if not path.exists() or not path.is_file():
        with _JAR_RECOVERY_LOCK:
            if not path.exists() or not path.is_file():
                try:
                    key_seed, _, fingerprint = get_active_sm4_key_seed_for_jar(clean_name)
                    public_base_url = get_settings().doris_sm4_udf_public_base_url.strip() or "http://127.0.0.1"
                    rebuilt = build_sm4_udf_jar(sm4_key=key_seed, public_base_url=public_base_url)
                    if rebuilt.filename != clean_name or rebuilt.key_fingerprint != fingerprint:
                        raise RuntimeError(
                            f"JAR 恢复校验失败：请求={clean_name}，生成={rebuilt.filename}。"
                        )
                    path = rebuilt.path
                except Exception as exc:
                    raise FileNotFoundError(f"SM4 UDF jar 不存在且自动恢复失败：{clean_name}；{exc}") from exc
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"SM4 UDF jar 不存在：{clean_name}")
    return path


def _compile_java8_udf(javac: str, *, classes_dir: Path, java_files: list[Path]) -> None:
    common_args = [
        "-encoding",
        "UTF-8",
        "-d",
        str(classes_dir),
        *[str(path) for path in java_files],
    ]
    commands = [
        [javac, "--release", "8", *common_args],
        [javac, "-source", "8", "-target", "8", *common_args],
    ]
    last_error = ""
    for command in commands:
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
            return
        except subprocess.CalledProcessError as exc:
            last_error = (exc.stderr or exc.stdout or str(exc)).strip()
    raise RuntimeError(last_error or "javac failed")


def _find_bin(configured: str, name: str) -> str | None:
    candidates = [configured.strip()] if configured and configured.strip() else []
    found = shutil.which(name)
    if found:
        candidates.append(found)
    candidates.append(f"/opt/jdk-17/bin/{name}")
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _refresh_function_in_database(
    cur,
    database: str,
    function_name: str,
    decrypt_function_name: str,
    jar: BuiltSm4Jar,
) -> DorisSm4FunctionDatabaseResult:
    impl_function = _implementation_function_name(function_name, jar.key_fingerprint)
    decrypt_impl_function = _implementation_function_name(decrypt_function_name, jar.key_fingerprint)
    drop_sql = (
        f"DROP FUNCTION IF EXISTS {function_name}(text); "
        f"DROP FUNCTION IF EXISTS {function_name}(VARCHAR); "
        f"DROP FUNCTION IF EXISTS {function_name}(STRING); "
        f"DROP FUNCTION IF EXISTS {impl_function}(text); "
        f"DROP FUNCTION IF EXISTS {impl_function}(VARCHAR); "
        f"DROP FUNCTION IF EXISTS {impl_function}(STRING); "
        f"DROP FUNCTION IF EXISTS {decrypt_function_name}(text); "
        f"DROP FUNCTION IF EXISTS {decrypt_function_name}(VARCHAR); "
        f"DROP FUNCTION IF EXISTS {decrypt_function_name}(STRING); "
        f"DROP FUNCTION IF EXISTS {decrypt_impl_function}(text); "
        f"DROP FUNCTION IF EXISTS {decrypt_impl_function}(VARCHAR); "
        f"DROP FUNCTION IF EXISTS {decrypt_impl_function}(STRING)"
    )
    verify_sql = f"SELECT {function_name}('{_sql_string(jar.verification_plaintext)}')"
    decrypt_verify_sql = f"SELECT {decrypt_function_name}('{_sql_string(jar.verification_ciphertext)}')"
    create_impl_sql = textwrap.dedent(
        f"""
        CREATE FUNCTION {impl_function}(STRING) RETURNS STRING PROPERTIES (
          "file" = "{jar.url}",
          "symbol" = "{jar.symbol}",
          "always_nullable" = "true",
          "type" = "JAVA_UDF"
        )
        """
    ).strip()
    create_decrypt_impl_sql = textwrap.dedent(
        f"""
        CREATE FUNCTION {decrypt_impl_function}(STRING) RETURNS STRING PROPERTIES (
          "file" = "{jar.url}",
          "symbol" = "{jar.decrypt_symbol}",
          "always_nullable" = "true",
          "type" = "JAVA_UDF"
        )
        """
    ).strip()
    create_alias_sql = f"CREATE ALIAS FUNCTION {function_name}(STRING) WITH PARAMETER(x) AS {impl_function}(x)"
    create_decrypt_alias_sql = f"CREATE ALIAS FUNCTION {decrypt_function_name}(STRING) WITH PARAMETER(x) AS {decrypt_impl_function}(x)"
    create_sql = f"{create_impl_sql};\n{create_alias_sql};\n{create_decrypt_impl_sql};\n{create_decrypt_alias_sql}"
    try:
        cur.execute(f"USE `{database.replace('`', '``')}`")
        cur.execute(f"DROP FUNCTION IF EXISTS {function_name}(text)")
        cur.execute(f"DROP FUNCTION IF EXISTS {function_name}(VARCHAR)")
        cur.execute(f"DROP FUNCTION IF EXISTS {function_name}(STRING)")
        cur.execute(f"DROP FUNCTION IF EXISTS {impl_function}(text)")
        cur.execute(f"DROP FUNCTION IF EXISTS {impl_function}(VARCHAR)")
        cur.execute(f"DROP FUNCTION IF EXISTS {impl_function}(STRING)")
        cur.execute(f"DROP FUNCTION IF EXISTS {decrypt_function_name}(text)")
        cur.execute(f"DROP FUNCTION IF EXISTS {decrypt_function_name}(VARCHAR)")
        cur.execute(f"DROP FUNCTION IF EXISTS {decrypt_function_name}(STRING)")
        cur.execute(f"DROP FUNCTION IF EXISTS {decrypt_impl_function}(text)")
        cur.execute(f"DROP FUNCTION IF EXISTS {decrypt_impl_function}(VARCHAR)")
        cur.execute(f"DROP FUNCTION IF EXISTS {decrypt_impl_function}(STRING)")
        cur.execute(create_impl_sql)
        cur.execute(create_alias_sql)
        cur.execute(create_decrypt_impl_sql)
        cur.execute(create_decrypt_alias_sql)
        cur.execute(verify_sql)
        actual = _first_value(cur.fetchone()).strip()
        if actual != jar.verification_ciphertext:
            return DorisSm4FunctionDatabaseResult(
                database=database,
                state="failed",
                message="函数已创建，但验证结果与新密钥预期不一致，不能视为已生效。",
                drop_sql=drop_sql,
                create_sql=create_sql,
                verification_state="failed",
                verification_message=f"expected={jar.verification_ciphertext}, actual={actual or '-'}",
                verification_sql=verify_sql,
            )
        cur.execute(decrypt_verify_sql)
        decrypted = _first_value(cur.fetchone())
        if decrypted != jar.verification_plaintext:
            return DorisSm4FunctionDatabaseResult(
                database=database,
                state="failed",
                message="加密函数已创建，但解密函数验证失败，不能视为生效。",
                drop_sql=drop_sql,
                create_sql=create_sql,
                verification_state="failed",
                verification_message=f"encrypt={actual or '-'}, decrypt_expected={jar.verification_plaintext}, decrypt_actual={decrypted or '-'}",
                verification_sql=f"{verify_sql}; {decrypt_verify_sql}",
            )
        return DorisSm4FunctionDatabaseResult(
            database=database,
            state="success",
            message="加密/解密函数已创建并通过固定函数名验证。",
            drop_sql=drop_sql,
            create_sql=create_sql,
            verification_state="success",
            verification_message="固定加密函数和固定解密函数均与当前密钥预期一致。",
            verification_sql=f"{verify_sql}; {decrypt_verify_sql}",
        )
    except Exception as exc:
        return DorisSm4FunctionDatabaseResult(
            database=database,
            state="failed",
            message=str(exc),
            drop_sql=drop_sql,
            create_sql=create_sql,
            verification_state="failed",
            verification_message=str(exc),
            verification_sql=verify_sql,
        )


def _list_databases(cur, include_system_databases: bool) -> list[str]:
    cur.execute("SHOW DATABASES")
    rows = cur.fetchall()
    result: list[str] = []
    for row in rows:
        database = _first_value(row)
        if not database:
            continue
        if not include_system_databases and database.lower() in _SYSTEM_DATABASES:
            continue
        result.append(database)
    return result


def _first_value(row: Any) -> str:
    if isinstance(row, dict):
        for value in row.values():
            if value is not None:
                return str(value)
        return ""
    if isinstance(row, (list, tuple)) and row:
        return str(row[0])
    return str(row or "")


def _normalize_sm4_seed(value: str | None) -> str:
    if not value:
        return secrets.token_urlsafe(24)
    clean = value.strip()
    if not clean:
        raise ValueError("手动 SM4 密钥种子不能为空。")
    return clean


def _sql_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "''")


def _implementation_function_name(function_name: str, fingerprint: str) -> str:
    base = f"{function_name}_{fingerprint}"
    if len(base) <= 64:
        return base
    prefix_length = max(1, 64 - len(fingerprint) - 1)
    return f"{function_name[:prefix_length]}_{fingerprint}"


def _derive_decrypt_function_name(function_name: str) -> str:
    if function_name.endswith("_ENCRYPT"):
        return f"{function_name[:-8]}_DECRYPT"
    if function_name.endswith("ENCRYPT"):
        return f"{function_name[:-7]}DECRYPT"
    candidate = f"{function_name}_DECRYPT"
    return candidate if len(candidate) <= 64 else candidate[:64]


_SBOX = [
    0xD6, 0x90, 0xE9, 0xFE, 0xCC, 0xE1, 0x3D, 0xB7, 0x16, 0xB6, 0x14, 0xC2, 0x28, 0xFB, 0x2C, 0x05,
    0x2B, 0x67, 0x9A, 0x76, 0x2A, 0xBE, 0x04, 0xC3, 0xAA, 0x44, 0x13, 0x26, 0x49, 0x86, 0x06, 0x99,
    0x9C, 0x42, 0x50, 0xF4, 0x91, 0xEF, 0x98, 0x7A, 0x33, 0x54, 0x0B, 0x43, 0xED, 0xCF, 0xAC, 0x62,
    0xE4, 0xB3, 0x1C, 0xA9, 0xC9, 0x08, 0xE8, 0x95, 0x80, 0xDF, 0x94, 0xFA, 0x75, 0x8F, 0x3F, 0xA6,
    0x47, 0x07, 0xA7, 0xFC, 0xF3, 0x73, 0x17, 0xBA, 0x83, 0x59, 0x3C, 0x19, 0xE6, 0x85, 0x4F, 0xA8,
    0x68, 0x6B, 0x81, 0xB2, 0x71, 0x64, 0xDA, 0x8B, 0xF8, 0xEB, 0x0F, 0x4B, 0x70, 0x56, 0x9D, 0x35,
    0x1E, 0x24, 0x0E, 0x5E, 0x63, 0x58, 0xD1, 0xA2, 0x25, 0x22, 0x7C, 0x3B, 0x01, 0x21, 0x78, 0x87,
    0xD4, 0x00, 0x46, 0x57, 0x9F, 0xD3, 0x27, 0x52, 0x4C, 0x36, 0x02, 0xE7, 0xA0, 0xC4, 0xC8, 0x9E,
    0xEA, 0xBF, 0x8A, 0xD2, 0x40, 0xC7, 0x38, 0xB5, 0xA3, 0xF7, 0xF2, 0xCE, 0xF9, 0x61, 0x15, 0xA1,
    0xE0, 0xAE, 0x5D, 0xA4, 0x9B, 0x34, 0x1A, 0x55, 0xAD, 0x93, 0x32, 0x30, 0xF5, 0x8C, 0xB1, 0xE3,
    0x1D, 0xF6, 0xE2, 0x2E, 0x82, 0x66, 0xCA, 0x60, 0xC0, 0x29, 0x23, 0xAB, 0x0D, 0x53, 0x4E, 0x6F,
    0xD5, 0xDB, 0x37, 0x45, 0xDE, 0xFD, 0x8E, 0x2F, 0x03, 0xFF, 0x6A, 0x72, 0x6D, 0x6C, 0x5B, 0x51,
    0x8D, 0x1B, 0xAF, 0x92, 0xBB, 0xDD, 0xBC, 0x7F, 0x11, 0xD9, 0x5C, 0x41, 0x1F, 0x10, 0x5A, 0xD8,
    0x0A, 0xC1, 0x31, 0x88, 0xA5, 0xCD, 0x7B, 0xBD, 0x2D, 0x74, 0xD0, 0x12, 0xB8, 0xE5, 0xB4, 0xB0,
    0x89, 0x69, 0x97, 0x4A, 0x0C, 0x96, 0x77, 0x7E, 0x65, 0xB9, 0xF1, 0x09, 0xC5, 0x6E, 0xC6, 0x84,
    0x18, 0xF0, 0x7D, 0xEC, 0x3A, 0xDC, 0x4D, 0x20, 0x79, 0xEE, 0x5F, 0x3E, 0xD7, 0xCB, 0x39, 0x48,
]
_FK = [0xA3B1BAC6, 0x56AA3350, 0x677D9197, 0xB27022DC]
_CK = [
    0x00070E15, 0x1C232A31, 0x383F464D, 0x545B6269, 0x70777E85, 0x8C939AA1, 0xA8AFB6BD, 0xC4CBD2D9,
    0xE0E7EEF5, 0xFC030A11, 0x181F262D, 0x343B4249, 0x50575E65, 0x6C737A81, 0x888F969D, 0xA4ABB2B9,
    0xC0C7CED5, 0xDCE3EAF1, 0xF8FF060D, 0x141B2229, 0x30373E45, 0x4C535A61, 0x686F767D, 0x848B9299,
    0xA0A7AEB5, 0xBCC3CAD1, 0xD8DFE6ED, 0xF4FB0209, 0x10171E25, 0x2C333A41, 0x484F565D, 0x646B7279,
]


def sm4_encrypt_to_base64(value: str, key_seed: str) -> str:
    key = _derive_sm4_key(key_seed)
    data = _pkcs7_pad(value.encode("utf-8"))
    rk = _round_keys(key)
    out = bytearray()
    for index in range(0, len(data), 16):
        out.extend(_encrypt_block(data[index : index + 16], rk))
    return base64.b64encode(bytes(out)).decode("ascii")


def sm4_decrypt_from_base64(value: str, key_seed: str) -> str:
    key = _derive_sm4_key(key_seed)
    data = base64.b64decode(value.encode("ascii"), validate=True)
    if not data or len(data) % 16:
        raise ValueError("SM4 ciphertext must be non-empty and aligned to 16-byte blocks.")
    rk = list(reversed(_round_keys(key)))
    out = bytearray()
    for index in range(0, len(data), 16):
        out.extend(_encrypt_block(data[index : index + 16], rk))
    return _pkcs7_unpad(bytes(out)).decode("utf-8")


def _derive_sm4_key(key_seed: str) -> bytes:
    return hashlib.md5(key_seed.encode("utf-8")).hexdigest()[:16].encode("utf-8")


def _pkcs7_pad(data: bytes) -> bytes:
    pad = 16 - (len(data) % 16)
    return data + bytes([pad]) * pad


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise ValueError("SM4 plaintext is empty after decrypt.")
    pad = data[-1]
    if pad < 1 or pad > 16 or len(data) < pad or data[-pad:] != bytes([pad]) * pad:
        raise ValueError("Invalid SM4 PKCS7 padding.")
    return data[:-pad]


def _round_keys(key: bytes) -> list[int]:
    mk = [_to_int(key[index * 4 : index * 4 + 4]) for index in range(4)]
    k = [0] * 36
    rk = [0] * 32
    for index in range(4):
        k[index] = (mk[index] ^ _FK[index]) & 0xFFFFFFFF
    for index in range(32):
        k[index + 4] = (k[index] ^ _l_prime(_tau(k[index + 1] ^ k[index + 2] ^ k[index + 3] ^ _CK[index]))) & 0xFFFFFFFF
        rk[index] = k[index + 4]
    return rk


def _encrypt_block(block: bytes, rk: list[int]) -> bytes:
    x = [0] * 36
    for index in range(4):
        x[index] = _to_int(block[index * 4 : index * 4 + 4])
    for index in range(32):
        x[index + 4] = (x[index] ^ _l(_tau(x[index + 1] ^ x[index + 2] ^ x[index + 3] ^ rk[index]))) & 0xFFFFFFFF
    result = bytearray()
    for index in range(4):
        result.extend(_from_int(x[35 - index]))
    return bytes(result)


def _tau(value: int) -> int:
    value &= 0xFFFFFFFF
    return (
        (_SBOX[(value >> 24) & 0xFF] << 24)
        | (_SBOX[(value >> 16) & 0xFF] << 16)
        | (_SBOX[(value >> 8) & 0xFF] << 8)
        | _SBOX[value & 0xFF]
    ) & 0xFFFFFFFF


def _l(value: int) -> int:
    return (value ^ _rotate_left(value, 2) ^ _rotate_left(value, 10) ^ _rotate_left(value, 18) ^ _rotate_left(value, 24)) & 0xFFFFFFFF


def _l_prime(value: int) -> int:
    return (value ^ _rotate_left(value, 13) ^ _rotate_left(value, 23)) & 0xFFFFFFFF


def _rotate_left(value: int, bits: int) -> int:
    value &= 0xFFFFFFFF
    return ((value << bits) | (value >> (32 - bits))) & 0xFFFFFFFF


def _to_int(data: bytes) -> int:
    return int.from_bytes(data, byteorder="big", signed=False)


def _from_int(value: int) -> bytes:
    return (value & 0xFFFFFFFF).to_bytes(4, byteorder="big", signed=False)


def _clean_identifier(value: str, label: str) -> str:
    clean = (value or "").strip()
    if not clean:
        raise ValueError(f"{label}不能为空。")
    if not all(ch.isalnum() or ch == "_" or "\u4e00" <= ch <= "\u9fff" for ch in clean):
        raise ValueError(f"{label}只能包含中文、字母、数字和下划线。")
    return clean


def _java_source(class_name: str, key_seed: str, *, mode: str) -> str:
    if mode not in {"encrypt", "decrypt"}:
        raise ValueError("SM4 Java UDF mode must be encrypt or decrypt.")
    key_seed_literal = json.dumps(key_seed, ensure_ascii=False)
    codec_field = "    private static final Base64.Encoder BASE64_ENCODER = Base64.getEncoder();" if mode == "encrypt" else "    private static final Base64.Decoder BASE64_DECODER = Base64.getDecoder();"
    evaluate_method = (
        """
    public String evaluate(String input) {
        if (input == null) return null;
        byte[] plain = pad(input.getBytes(StandardCharsets.UTF_8));
        int[] rk = roundKeys(KEY);
        byte[] out = new byte[plain.length];
        for (int i = 0; i < plain.length; i += 16) {
            cryptBlock(plain, i, out, i, rk);
        }
        return BASE64_ENCODER.encodeToString(out);
    }
"""
        if mode == "encrypt"
        else """
    public String evaluate(String input) {
        if (input == null) return null;
        byte[] cipher = BASE64_DECODER.decode(input);
        if (cipher.length == 0 || cipher.length % 16 != 0) {
            throw new IllegalArgumentException("SM4 ciphertext must be non-empty and aligned to 16-byte blocks.");
        }
        int[] rk = reverse(roundKeys(KEY));
        byte[] out = new byte[cipher.length];
        for (int i = 0; i < cipher.length; i += 16) {
            cryptBlock(cipher, i, out, i, rk);
        }
        return new String(unpad(out), StandardCharsets.UTF_8);
    }
"""
    )
    return f"""
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.nio.charset.StandardCharsets;
import java.util.Base64;

public class {class_name} {{
    private static final String KEY_SEED = {key_seed_literal};
    private static final byte[] KEY = leftMd5Hex(KEY_SEED, 16).getBytes(StandardCharsets.UTF_8);
{codec_field}
    private static final int[] SBOX = new int[] {{
        0xd6,0x90,0xe9,0xfe,0xcc,0xe1,0x3d,0xb7,0x16,0xb6,0x14,0xc2,0x28,0xfb,0x2c,0x05,
        0x2b,0x67,0x9a,0x76,0x2a,0xbe,0x04,0xc3,0xaa,0x44,0x13,0x26,0x49,0x86,0x06,0x99,
        0x9c,0x42,0x50,0xf4,0x91,0xef,0x98,0x7a,0x33,0x54,0x0b,0x43,0xed,0xcf,0xac,0x62,
        0xe4,0xb3,0x1c,0xa9,0xc9,0x08,0xe8,0x95,0x80,0xdf,0x94,0xfa,0x75,0x8f,0x3f,0xa6,
        0x47,0x07,0xa7,0xfc,0xf3,0x73,0x17,0xba,0x83,0x59,0x3c,0x19,0xe6,0x85,0x4f,0xa8,
        0x68,0x6b,0x81,0xb2,0x71,0x64,0xda,0x8b,0xf8,0xeb,0x0f,0x4b,0x70,0x56,0x9d,0x35,
        0x1e,0x24,0x0e,0x5e,0x63,0x58,0xd1,0xa2,0x25,0x22,0x7c,0x3b,0x01,0x21,0x78,0x87,
        0xd4,0x00,0x46,0x57,0x9f,0xd3,0x27,0x52,0x4c,0x36,0x02,0xe7,0xa0,0xc4,0xc8,0x9e,
        0xea,0xbf,0x8a,0xd2,0x40,0xc7,0x38,0xb5,0xa3,0xf7,0xf2,0xce,0xf9,0x61,0x15,0xa1,
        0xe0,0xae,0x5d,0xa4,0x9b,0x34,0x1a,0x55,0xad,0x93,0x32,0x30,0xf5,0x8c,0xb1,0xe3,
        0x1d,0xf6,0xe2,0x2e,0x82,0x66,0xca,0x60,0xc0,0x29,0x23,0xab,0x0d,0x53,0x4e,0x6f,
        0xd5,0xdb,0x37,0x45,0xde,0xfd,0x8e,0x2f,0x03,0xff,0x6a,0x72,0x6d,0x6c,0x5b,0x51,
        0x8d,0x1b,0xaf,0x92,0xbb,0xdd,0xbc,0x7f,0x11,0xd9,0x5c,0x41,0x1f,0x10,0x5a,0xd8,
        0x0a,0xc1,0x31,0x88,0xa5,0xcd,0x7b,0xbd,0x2d,0x74,0xd0,0x12,0xb8,0xe5,0xb4,0xb0,
        0x89,0x69,0x97,0x4a,0x0c,0x96,0x77,0x7e,0x65,0xb9,0xf1,0x09,0xc5,0x6e,0xc6,0x84,
        0x18,0xf0,0x7d,0xec,0x3a,0xdc,0x4d,0x20,0x79,0xee,0x5f,0x3e,0xd7,0xcb,0x39,0x48
    }};
    private static final int[] FK = new int[] {{ 0xa3b1bac6, 0x56aa3350, 0x677d9197, 0xb27022dc }};
    private static final int[] CK = new int[] {{
        0x00070e15,0x1c232a31,0x383f464d,0x545b6269,0x70777e85,0x8c939aa1,0xa8afb6bd,0xc4cbd2d9,
        0xe0e7eef5,0xfc030a11,0x181f262d,0x343b4249,0x50575e65,0x6c737a81,0x888f969d,0xa4abb2b9,
        0xc0c7ced5,0xdce3eaf1,0xf8ff060d,0x141b2229,0x30373e45,0x4c535a61,0x686f767d,0x848b9299,
        0xa0a7aeb5,0xbcc3cad1,0xd8dfe6ed,0xf4fb0209,0x10171e25,0x2c333a41,0x484f565d,0x646b7279
    }};

{evaluate_method}

    private static byte[] pad(byte[] input) {{
        int pad = 16 - (input.length % 16);
        byte[] out = new byte[input.length + pad];
        System.arraycopy(input, 0, out, 0, input.length);
        for (int i = input.length; i < out.length; i++) out[i] = (byte) pad;
        return out;
    }}

    private static byte[] unpad(byte[] input) {{
        if (input.length == 0) throw new IllegalArgumentException("SM4 plaintext is empty after decrypt.");
        int pad = input[input.length - 1] & 0xff;
        if (pad < 1 || pad > 16 || input.length < pad) {{
            throw new IllegalArgumentException("Invalid SM4 PKCS7 padding.");
        }}
        for (int i = input.length - pad; i < input.length; i++) {{
            if ((input[i] & 0xff) != pad) throw new IllegalArgumentException("Invalid SM4 PKCS7 padding.");
        }}
        byte[] out = new byte[input.length - pad];
        System.arraycopy(input, 0, out, 0, out.length);
        return out;
    }}

    private static int[] reverse(int[] input) {{
        int[] out = new int[input.length];
        for (int i = 0; i < input.length; i++) out[i] = input[input.length - 1 - i];
        return out;
    }}

    private static void cryptBlock(byte[] in, int inOff, byte[] out, int outOff, int[] rk) {{
        int[] x = new int[36];
        for (int i = 0; i < 4; i++) x[i] = toInt(in, inOff + i * 4);
        for (int i = 0; i < 32; i++) x[i + 4] = x[i] ^ l(tau(x[i + 1] ^ x[i + 2] ^ x[i + 3] ^ rk[i]));
        for (int i = 0; i < 4; i++) putInt(x[35 - i], out, outOff + i * 4);
    }}

    private static int[] roundKeys(byte[] key) {{
        int[] mk = new int[4];
        int[] k = new int[36];
        int[] rk = new int[32];
        for (int i = 0; i < 4; i++) mk[i] = toInt(key, i * 4);
        for (int i = 0; i < 4; i++) k[i] = mk[i] ^ FK[i];
        for (int i = 0; i < 32; i++) {{
            k[i + 4] = k[i] ^ lPrime(tau(k[i + 1] ^ k[i + 2] ^ k[i + 3] ^ CK[i]));
            rk[i] = k[i + 4];
        }}
        return rk;
    }}

    private static int tau(int a) {{
        return (SBOX[(a >>> 24) & 0xff] << 24) | (SBOX[(a >>> 16) & 0xff] << 16)
            | (SBOX[(a >>> 8) & 0xff] << 8) | SBOX[a & 0xff];
    }}

    private static int l(int b) {{
        return b ^ Integer.rotateLeft(b, 2) ^ Integer.rotateLeft(b, 10) ^ Integer.rotateLeft(b, 18) ^ Integer.rotateLeft(b, 24);
    }}

    private static int lPrime(int b) {{
        return b ^ Integer.rotateLeft(b, 13) ^ Integer.rotateLeft(b, 23);
    }}

    private static int toInt(byte[] b, int off) {{
        return ((b[off] & 0xff) << 24) | ((b[off + 1] & 0xff) << 16) | ((b[off + 2] & 0xff) << 8) | (b[off + 3] & 0xff);
    }}

    private static void putInt(int n, byte[] b, int off) {{
        b[off] = (byte) (n >>> 24);
        b[off + 1] = (byte) (n >>> 16);
        b[off + 2] = (byte) (n >>> 8);
        b[off + 3] = (byte) n;
    }}

    private static String leftMd5Hex(String value, int length) {{
        try {{
            MessageDigest md = MessageDigest.getInstance("MD5");
            byte[] digest = md.digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder(digest.length * 2);
            for (byte b : digest) {{
                String item = Integer.toHexString(b & 0xff);
                if (item.length() == 1) hex.append('0');
                hex.append(item);
            }}
            return hex.substring(0, length);
        }} catch (NoSuchAlgorithmException e) {{
            throw new IllegalStateException("MD5 digest is not available.", e);
        }}
    }}
}}
"""
