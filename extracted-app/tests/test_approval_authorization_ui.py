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
    assert "创建后自动调用 apiUpdate，以返回的 defaultSchemaName 设置默认库和库限制。" in html


def test_import_permission_step_exposes_raw_department_context():
    html = UI_HTML.read_text(encoding="utf-8")
    assert 'import_permissions: {\n            department_raw: "",' in html
    assert 'approvalAuthContextInput("department_raw", "申请人部门原值（createUserDepartment）", context.department_raw' in html
    assert 'if (step === "import_permissions") return { department_raw: value("department_raw"),' in html
    assert "并包含目录 edit/add 权限；部门原值必填。" in html
