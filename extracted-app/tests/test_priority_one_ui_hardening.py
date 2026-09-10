from pathlib import Path


UI_HTML = Path(__file__).parents[1] / "src" / "recovery_service" / "static" / "ui.html"


def _ui() -> str:
    return UI_HTML.read_text(encoding="utf-8")


def test_cleanup_plan_is_bound_to_current_selection_and_options():
    html = _ui()

    assert "let cleanupCurrentPlanFingerprint = \"\";" in html
    assert "function invalidateCleanupPlan" in html
    assert "cleanupPlanFingerprint(body) !== cleanupCurrentPlanFingerprint" in html
    assert '$(["cleanupDropStorage", "cleanupFiles"])' not in html
    assert '["cleanupDropStorage", "cleanupFiles"].forEach' in html


def test_data_automation_uses_named_reference_selectors_without_changing_payload_fields():
    html = _ui()

    for field_id in (
        "dataAutomationRestoreTemplate",
        "dataAutomationSyncNode",
        "dataAutomationWorkflowVersion",
        "dataAutomationSm4Task",
        "dataAutomationStandardConnection",
    ):
        assert f'<select id="{field_id}">' in html
        assert f'data-reference-summary-for="{field_id}"' in html

    assert "function renderDataAutomationReferenceOptions" in html
    assert "function preserveDataAutomationReference" in html
    assert "restore_template_task_id:optionalId(\"dataAutomationRestoreTemplate\")" in html
    assert "data_sync_node_id:optionalId(\"dataAutomationSyncNode\")" in html
    assert "standard_workflow_version_id:optionalId(\"dataAutomationWorkflowVersion\")" in html
    assert "sm4_task_definition_id:optionalId(\"dataAutomationSm4Task\")" in html


def test_sm4_coverage_contracts_have_structured_editor_and_json_compatibility():
    html = _ui()

    assert 'id="dorisEncryptCoverageContracts" class="structured-config-hidden"' in html
    assert 'id="dorisEncryptCoverageContractModal"' in html
    assert 'id="dorisEncryptCoverageContractRows"' in html
    assert 'id="dorisEncryptCoverageContractAddBtn"' in html
    assert 'id="dorisEncryptCoverageContractSaveBtn"' in html
    assert "function openDorisEncryptCoverageContractEditor" in html
    assert "function saveDorisEncryptCoverageContractEditor" in html
    assert "覆盖合同存在重复的 ODS 来源字段" in html


def test_offline_development_uses_structured_node_config_and_column_mapping_editors():
    html = _ui()

    assert 'id="dataPlatformNodeConfig" class="structured-config-hidden"' in html
    assert 'id="dataPlatformNodeConfigModal"' in html
    assert 'id="dataPlatformNodeConfigRows"' in html
    assert 'id="dataPlatformNodeConfigOpenBtn"' in html
    assert 'id="dataPlatformSyncColumnMapping" class="structured-config-hidden"' in html
    assert 'id="dataPlatformSyncColumnMappingRows"' in html
    assert 'id="dataPlatformSyncColumnMappingAddBtn"' in html
    assert "function saveDataPlatformNodeConfigEditor" in html
    assert "function collectDataPlatformSyncColumnMappings" in html
    assert 'column_mapping: collectDataPlatformSyncColumnMappings()' in html
