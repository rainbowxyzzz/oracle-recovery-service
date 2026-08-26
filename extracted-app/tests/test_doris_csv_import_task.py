import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recovery_service.api.schemas.doris_csv_import import (
    DorisCsvColumnPreview,
    DorisCsvFilePreview,
)
from recovery_service.core.models.task import Base, DatabaseConnectionProfile
from recovery_service.services import doris_csv_import
from recovery_service.services.doris_csv_import import (
    create_csv_parse_task,
    get_csv_parse_task_status_sync,
    request_import_csv_task_sync,
    request_stop_csv_parse_task_sync,
    run_csv_parse_task_sync,
    update_csv_parse_file_preview_sync,
)


def _session_factory(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(doris_csv_import, "get_sync_session_factory", lambda: factory)

    class Settings:
        staging_dir = str(tmp_path)

    monkeypatch.setattr(doris_csv_import, "get_settings", lambda: Settings())
    return factory


def _profile(factory):
    session = factory()
    try:
        profile = DatabaseConnectionProfile(
            id=uuid.uuid4(),
            name="Doris Test",
            engine="doris",
            host="127.0.0.1",
            port=9030,
            username="root",
            password_enc="",
            database=None,
        )
        session.add(profile)
        session.commit()
        session.refresh(profile)
        return profile
    finally:
        session.close()


def test_parse_task_single_table_reuses_first_file_structure(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path, monkeypatch)
    profile = _profile(factory)
    task = create_csv_parse_task(
        profile,
        [
            ("orders_a.csv", "id,name\n1,alpha\n2,beta\n".encode("utf-8")),
            ("orders_b.csv", "3,gamma\n4,delta\n".encode("utf-8")),
        ],
        database=None,
        has_header=True,
        import_mode="single_table",
    )

    run_csv_parse_task_sync(task.task_id)
    status = get_csv_parse_task_status_sync(task.task_id)

    assert status.state == "completed"
    assert status.completed_files == 2
    assert status.failed_files == 0
    assert status.preview is not None
    assert [item.table_name for item in status.preview.files] == ["orders_a", "orders_a"]
    assert status.preview.files[1].has_header is False
    assert status.preview.files[1].valid_row_count == 2
    assert Path(tmp_path, "doris-csv-parse", str(task.task_id)).exists()


def test_parse_task_keeps_chinese_header_names(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path, monkeypatch)
    profile = _profile(factory)
    task = create_csv_parse_task(
        profile,
        [("事件.csv", "统计月份,事件单编码,事件标题\n202401,SJ001,标题1\n".encode("utf-8"))],
        database=None,
        has_header=True,
        import_mode="multiple_tables",
    )

    run_csv_parse_task_sync(task.task_id)
    status = get_csv_parse_task_status_sync(task.task_id)

    assert status.state == "completed"
    assert status.preview is not None
    assert [column.name for column in status.preview.files[0].columns] == ["统计月份", "事件单编码", "事件标题"]
    assert not status.preview.files[0].warnings


def test_parse_task_auto_detects_mixed_file_charsets(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path, monkeypatch)
    profile = _profile(factory)
    task = create_csv_parse_task(
        profile,
        [
            ("utf8.csv", "id,name\n1,alpha\n".encode("utf-8")),
            ("gb18030.csv", "\u7f16\u53f7,\u540d\u79f0\n2,\u8d22\u653f\u4e00\u5904\n".encode("gb18030")),
        ],
        database=None,
        charset="auto",
        has_header=True,
        import_mode="multiple_tables",
    )

    run_csv_parse_task_sync(task.task_id)
    status = get_csv_parse_task_status_sync(task.task_id)

    assert status.state == "completed"
    assert status.preview is not None
    assert [item.charset for item in status.preview.files] == ["utf-8", "gb18030"]
    assert status.preview.files[1].columns[0].name == "\u7f16\u53f7"
    assert status.preview.files[1].sample_rows[0]["\u540d\u79f0"] == "\u8d22\u653f\u4e00\u5904"


def test_stream_load_headers_keep_legacy_columns_for_ascii_mapping():
    headers = doris_csv_import._stream_load_headers(
        ["id", "mapped_name"],
        delimiter=",",
        label="ascii_mapping",
    )

    assert headers["format"] == "csv"
    assert headers["skip_lines"] == "1"
    assert headers["columns"] == "id,mapped_name"
    assert all(value.isascii() for value in headers.values())


def test_stream_load_headers_use_csv_names_for_chinese_mapping():
    headers = doris_csv_import._stream_load_headers(
        ["\u6570\u636e\u4ed3\u5206\u7c7b", "shared_type"],
        delimiter=",",
        label="chinese_mapping",
    )

    assert headers["format"] == "csv_with_names"
    assert "skip_lines" not in headers
    assert "columns" not in headers
    assert all(value.isascii() for value in headers.values())


def test_import_task_accepts_database_after_parse(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path, monkeypatch)
    profile = _profile(factory)
    task = create_csv_parse_task(
        profile,
        [("orders.csv", b"id,name\n1,alpha\n")],
        database=None,
        has_header=True,
        import_mode="multiple_tables",
    )

    run_csv_parse_task_sync(task.task_id)
    status = request_import_csv_task_sync(
        task.task_id,
        create_table=True,
        overwrite=False,
        database="TARGET_DB",
    )

    assert status.database == "TARGET_DB"
    assert status.preview is not None
    assert status.preview.database == "TARGET_DB"


def test_parse_task_stop_marks_waiting_files_stopped(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path, monkeypatch)
    profile = _profile(factory)
    task = create_csv_parse_task(
        profile,
        [("a.csv", b"id\n1\n"), ("b.csv", b"id\n2\n")],
        database=None,
        import_mode="multiple_tables",
    )

    request_stop_csv_parse_task_sync(task.task_id)
    run_csv_parse_task_sync(task.task_id)
    status = get_csv_parse_task_status_sync(task.task_id)

    assert status.state == "stopped"
    assert all(item.state == "stopped" for item in status.files)


def test_custom_column_type_is_normalized_and_unsafe_type_is_rejected():
    column = DorisCsvColumnPreview(
        original_name="mixed_value",
        name="mixed_value",
        type=" varchar ( 65533 ) ",
    )

    assert column.type == "VARCHAR(65533)"

    with pytest.raises(ValidationError, match="不支持的 Doris CSV 字段类型"):
        DorisCsvColumnPreview(
            original_name="mixed_value",
            name="mixed_value",
            type="VARCHAR(20)); DROP TABLE users; --",
        )


def test_parse_task_persists_custom_column_type(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path, monkeypatch)
    profile = _profile(factory)
    task = create_csv_parse_task(
        profile,
        [("mixed.csv", b"mixed_value\n1\n2\n")],
        database=None,
        has_header=True,
        import_mode="multiple_tables",
    )
    run_csv_parse_task_sync(task.task_id)
    status = get_csv_parse_task_status_sync(task.task_id)
    preview_data = status.preview.files[0].model_dump(mode="json")
    preview_data["columns"][0]["type"] = "varchar ( 65533 )"
    preview_data["columns"][0]["max_length"] = 65533
    preview = DorisCsvFilePreview.model_validate(preview_data)

    updated = update_csv_parse_file_preview_sync(
        task.task_id,
        status.files[0].id,
        preview,
    )

    assert updated.preview.files[0].columns[0].type == "VARCHAR(65533)"
    assert updated.preview.files[0].columns[0].max_length == 65533


def test_create_table_uses_custom_types_and_limits_long_varchar_key(monkeypatch):
    executed = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql):
            executed.append(sql)

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def fake_connection(*_args, **_kwargs):
        yield Connection()

    monkeypatch.setattr(doris_csv_import, "_doris_mysql_conn", fake_connection)
    preview = DorisCsvFilePreview(
        filename="mixed.csv",
        table_name="mixed",
        columns=[
            DorisCsvColumnPreview(
                original_name="first_value",
                name="first_value",
                type="VARCHAR(65533)",
                max_length=65533,
            ),
            DorisCsvColumnPreview(
                original_name="second_value",
                name="second_value",
                type="VARCHAR(65533)",
                max_length=65533,
            ),
        ],
    )

    ddl = doris_csv_import._create_table(object(), "TEST_DB", preview, overwrite=False)

    assert "`first_value` VARCHAR(255)" in ddl
    assert "`second_value` VARCHAR(65533)" in ddl
    assert executed == [ddl]
    assert any("VARCHAR(255)" in warning for warning in preview.warnings)


def test_csv_preview_exposes_custom_type_and_bulk_varchar_controls():
    ui = (Path(__file__).parents[1] / "src" / "recovery_service" / "static" / "ui.html").read_text(
        encoding="utf-8"
    )

    assert "data-doris-column-type" in ui
    assert "data-doris-all-varchar" in ui
    assert "全部设为 VARCHAR(65533)" in ui
