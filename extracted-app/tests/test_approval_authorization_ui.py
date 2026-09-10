from pathlib import Path

UI_HTML = Path(__file__).parents[1] / "src" / "recovery_service" / "static" / "ui.html"


def test_approval_authorization_workspace_keeps_existing_contract_and_structures_all_config():
    html = UI_HTML.read_text(encoding="utf-8")

    for workspace in ("tasks", "testing", "runs"):
        assert f'data-approval-auth-workspace="{workspace}"' in html
        assert f'data-approval-auth-workspace-panel="{workspace}"' in html

    required_fields = (
        "approvalAuthTimeoutSeconds", "approvalAuthDateSuffix", "approvalAuthLoginPath",
        "approvalAuthTodoListPath", "approvalAuthDetailPath", "approvalAuthDataListPath",
        "approvalAuthTodoPage", "approvalAuthTodoRows", "approvalAuthDataListRows",
        "approvalAuthWorkflowTokenPath", "approvalAuthWorkflowTokenHeader",
        "approvalAuthWorkflowTokenPrefix", "approvalAuthTodoItemsPath",
        "approvalAuthDetailDataPath", "approvalAuthDataListResultPath",
        "approvalAuthMappingDatabase", "approvalAuthMappingTable",
        "approvalAuthMappingDepartmentColumn", "approvalAuthMappingDatabaseColumn",
        "approvalAuthAuthInfoDatabase", "approvalAuthAuthInfoTable",
        "approvalAuthYoudataTokenPath", "approvalAuthYoudataTokenType",
        "approvalAuthYoudataTokenResultPath", "approvalAuthApiAddPath",
        "approvalAuthApiAddIdPath", "approvalAuthApiAddPaths",
        "approvalAuthApiAddProjectId", "approvalAuthApiAddType", "approvalAuthApiAddServer",
        "approvalAuthApiAddPort", "approvalAuthApiAddDriver", "approvalAuthApiAddAuthType",
        "approvalAuthApiAddDorisCatalog", "approvalAuthApiAddSkipTest",
        "approvalAuthApiAddNullSafeEqual", "approvalAuthImportPermissionsPath",
        "approvalAuthImportPermissionsRoleIdPath", "approvalAuthImportPermissionPaths",
        "approvalAuthImportProjectId", "approvalAuthImportType", "approvalAuthImportRoleName",
        "approvalAuthImportResourceDataConnection", "approvalAuthAuditStatusUpdatePath",
        "approvalAuthAuditBodyDefaults", "approvalAuthUpdateAuditStatusAfterSuccess",
        "approvalAuthAutoWatchEnabled", "approvalAuthAutoWatchInterval",
        "approvalAuthAutoWatchMaxItems", "approvalAuthAutoWatchSkipStatusUpdated",
        "approvalAuthDebugLogSensitivePayloads",
    )
    for field_id in required_fields:
        assert f'id="{field_id}"' in html

    assert 'id="approvalAuthConfigJson" class="approval-auth-hidden"' in html
    assert 'id="approvalAuthStepContext" class="approval-auth-hidden"' in html
    assert 'data-approval-auth-run-config=' in html
    assert 'data-approval-auth-run=' in html
    assert 'data-approval-auth-flow=' in html
    assert '/api/v1/approval-authorization/configs/${encodeURIComponent(approvalAuthSelectedConfigId)}/test-step' in html
    assert '/api/v1/approval-authorization/configs/${encodeURIComponent(approvalAuthSelectedConfigId)}/runs' in html
    assert '/api/v1/approval-authorization/configs/${encodeURIComponent(approvalAuthSelectedConfigId)}/watch-scan' in html
