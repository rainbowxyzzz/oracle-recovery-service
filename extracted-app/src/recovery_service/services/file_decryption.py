from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from recovery_service.services.doris_sm4_function import sm4_decrypt_from_base64

FileFormat = Literal["csv", "json"]


@dataclass
class FileDecryptResult:
    filename: str
    format: FileFormat
    content_type: str
    content: str
    row_count: int
    decrypted_count: int
    failed_count: int
    errors: list[dict[str, Any]]
    columns: list[str]


def decrypt_file_content(
    *,
    filename: str,
    content: bytes,
    decrypt_columns: list[str],
    sm4_key: str,
    output_format: str = "preserve",
) -> FileDecryptResult:
    clean_columns = _clean_columns(decrypt_columns)
    if not clean_columns:
        raise ValueError("请至少选择一个需要解密的列。")
    key_seed = (sm4_key or "").strip()
    if not key_seed:
        raise ValueError("请填写 SM4 密钥种子。")
    input_format = _detect_format(filename, content)
    target_format = input_format if output_format in {"", "preserve", None} else _clean_output_format(output_format)

    if input_format == "csv":
        records, columns = _parse_csv(content)
    else:
        records, columns = _parse_json(content)

    decrypted_count = 0
    failed_count = 0
    errors: list[dict[str, Any]] = []
    column_set = set(columns)
    missing = [column for column in clean_columns if column not in column_set]
    for column in missing:
        errors.append({"row": None, "column": column, "message": "文件中不存在该列。"})

    active_columns = [column for column in clean_columns if column in column_set]
    for row_index, record in enumerate(records, start=1):
        for column in active_columns:
            value = record.get(column)
            if value is None or value == "":
                continue
            try:
                record[column] = sm4_decrypt_from_base64(str(value), key_seed)
                decrypted_count += 1
            except Exception as exc:
                failed_count += 1
                errors.append({"row": row_index, "column": column, "message": str(exc)})

    if target_format == "csv":
        output = _write_csv(records, columns)
        content_type = "text/csv; charset=utf-8"
    else:
        output = json.dumps(records, ensure_ascii=False, indent=2)
        content_type = "application/json; charset=utf-8"

    return FileDecryptResult(
        filename=_output_filename(filename, target_format),
        format=target_format,
        content_type=content_type,
        content=output,
        row_count=len(records),
        decrypted_count=decrypted_count,
        failed_count=failed_count,
        errors=errors[:200],
        columns=columns,
    )


def _clean_columns(columns: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in columns:
        clean = str(item or "").strip()
        if clean and clean not in seen:
            result.append(clean)
            seen.add(clean)
    return result


def _detect_format(filename: str, content: bytes) -> FileFormat:
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix == ".json":
        return "json"
    sample = _decode_text(content).lstrip()
    if sample.startswith("[") or sample.startswith("{"):
        return "json"
    return "csv"


def _clean_output_format(value: str) -> FileFormat:
    clean = (value or "preserve").strip().lower()
    if clean in {"csv", "json"}:
        return clean  # type: ignore[return-value]
    raise ValueError("输出格式只支持 preserve、csv 或 json。")


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _parse_csv(content: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    text = _decode_text(content)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV 文件缺少表头。")
    columns = [str(item or "").strip() for item in reader.fieldnames]
    if not all(columns):
        raise ValueError("CSV 表头不能包含空列名。")
    records = [{column: row.get(column, "") for column in columns} for row in reader]
    return records, columns


def _parse_json(content: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        data = json.loads(_decode_text(content))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 文件格式不正确：{exc}") from exc
    if isinstance(data, dict):
        if isinstance(data.get("rows"), list):
            rows = data["rows"]
        else:
            rows = [data]
    elif isinstance(data, list):
        rows = data
    else:
        raise ValueError("JSON 顶层必须是对象、对象数组，或包含 rows 数组的对象。")
    if not all(isinstance(item, dict) for item in rows):
        raise ValueError("JSON 行数据必须是对象。")
    columns: list[str] = []
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    for item in rows:
        record = dict(item)
        records.append(record)
        for key in record:
            if key not in seen:
                columns.append(str(key))
                seen.add(str(key))
    return records, columns


def _write_csv(records: list[dict[str, Any]], columns: list[str]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow({column: record.get(column, "") for column in columns})
    return output.getvalue()


def _output_filename(filename: str, output_format: FileFormat) -> str:
    base = Path(filename or "decrypted").stem or "decrypted"
    return f"{base}_decrypted.{output_format}"
