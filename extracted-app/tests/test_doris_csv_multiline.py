import csv
import io

import pytest

from recovery_service.services import doris_csv_import as service


ROWS = [
    ["2026", "哈喽", "jjj321313215124"],
    ["2026", "DE\nDEDEhalou", "jjj321313215124"],
    ["2025", 'ded"这是一个测\n试文档 ""', "jjj321313215124"],
    ["2025", '详情"你好亚，\n我是谁"', "jjj321313215124"],
    ["2024", "CRLF\r\n第二行", "a,b"],
    ["2023", "单独CR\r第二行", 'a"b'],
    ["2022", '"首尾双引号"', "back\\slash\\n"],
    ["2021", '连续""双引号,逗号\t制表符', "unicode\u2028separator\u2029end"],
    ["2020", '结尾反斜杠\\', '\\"以及"\\'],
    ["2019", "", '"'],
]


def excel_csv(header, *, encoding, delimiter, has_header=True):
    out = io.StringIO(newline="")
    writer = csv.writer(out, delimiter=delimiter, lineterminator="\r\n")
    writer.writerows(([header] if has_header else []) + ROWS)
    return out.getvalue().encode(encoding)


@pytest.mark.parametrize("header", [["year", "detail", "code"], ["月份", "详情", "编码"]])
@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-8", "gb18030"])
@pytest.mark.parametrize("delimiter", [",", ";", "\t"])
@pytest.mark.parametrize("has_header", [True, False])
def test_excel_multiline_preview_and_doris_transport_round_trip(header, encoding, delimiter, has_header):
    source = excel_csv(header, encoding=encoding, delimiter=delimiter, has_header=has_header)
    preview = service._preview_one_file(
        "excel.csv", source, delimiter=delimiter, charset="auto", has_header=has_header,
    )
    columns = [column.name for column in preview.columns]
    assert preview.valid_row_count == len(ROWS)
    assert preview.bad_row_count == 0
    assert [[row[col] for col in columns] for row in preview.sample_rows] == ROWS

    prepared = service._prepare_import_content(
        "excel.csv", source, preview=preview, delimiter=delimiter,
        charset=preview.charset, has_header=has_header,
    )
    headers = service._stream_load_headers(columns, delimiter=delimiter, label="test")
    assert headers["escape"] == "\\"
    assert headers["enclose"] == '"'
    decoded = list(csv.reader(
        io.StringIO(prepared["content"].decode("utf-8"), newline=""),
        delimiter=delimiter, escapechar=headers["escape"], doublequote=False, strict=True,
    ))
    assert decoded == [columns, *ROWS]
    assert prepared["valid_row_count"] == len(ROWS)
    assert prepared["bad_rows"] == []


def test_multiline_transport_preserves_mapping_subset():
    source = excel_csv(["year", "detail", "code"], encoding="utf-8-sig", delimiter=",")
    preview = service._preview_one_file("subset.csv", source, delimiter=",", charset="auto", has_header=True)
    preview.columns[0].name = ""
    preview.columns[1].name = "备注"
    preview.columns[2].name = "目标编码"
    prepared = service._prepare_import_content(
        "subset.csv", source, preview=preview, delimiter=",", charset=preview.charset, has_header=True,
    )
    decoded = list(csv.reader(io.StringIO(prepared["content"].decode(), newline=""), escapechar="\\", doublequote=False))
    assert decoded == [["备注", "目标编码"], *[row[1:] for row in ROWS]]
