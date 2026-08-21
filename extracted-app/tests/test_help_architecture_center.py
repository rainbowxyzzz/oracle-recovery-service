from pathlib import Path


UI = Path("src/recovery_service/static/ui.html")


def test_help_center_is_available_to_every_logged_in_user() -> None:
    html = UI.read_text(encoding="utf-8")
    assert 'data-nav-module="helpArchitecture"' in html
    assert 'data-module="helpArchitecture"' in html
    assert 'moduleId === "system" || moduleId === "helpArchitecture"' in html


def test_help_center_contains_four_information_views_and_openapi_discovery() -> None:
    html = UI.read_text(encoding="utf-8")
    for tab in ("overview", "manual", "apis", "coupling"):
        assert f'data-help-tab="{tab}"' in html
    assert 'helpCenterOpenApi = await api("/openapi.json")' in html
    assert "function helpModuleForPath(path)" in html
    assert 'path.replace(/^\\/api\\/v1' in html
    assert "matches.sort((a, b) => b.length - a.length)" in html


def test_help_center_documents_all_primary_business_modules() -> None:
    html = UI.read_text(encoding="utf-8")
    for module_id in (
        "connections", "restore", "dorisCsv", "dorisSqlEtl", "dataSyncCenter",
        "dataChangeTriggerCenter", "batchAuthorization", "approvalAuthorization",
        "resourceProvisioning", "apiOrchestration", "dorisEncrypt",
        "dorisEncryptDispatch", "dorisSm3", "dorisSm3Logs", "dataPlatform", "cleanup",
    ):
        assert f'id:"{module_id}"' in html


def test_help_center_keeps_swagger_as_developer_supplement() -> None:
    html = UI.read_text(encoding="utf-8")
    assert 'href="/docs" target="_blank"' in html
    assert "打开原始 Swagger" in html
