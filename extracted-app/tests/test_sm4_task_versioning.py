import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from recovery_service.core.models.task import (
    Base,
    DataPlatformWorkflow,
    DataPlatformWorkflowVersion,
    DorisSm4TaskDefinition,
    DorisSm4TaskDefinitionRevision,
)
from recovery_service.services.doris_encryption import (
    freeze_sm4_task_nodes,
    update_sm4_task_definition,
)


class Sm4TaskVersioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.task_id = uuid.uuid4()
        self.workflow_id = uuid.uuid4()
        self.version_id = uuid.uuid4()
        with self.factory() as session:
            session.add(
                DorisSm4TaskDefinition(
                    id=self.task_id,
                    name="客户表加密",
                    revision=1,
                    connection_id=uuid.uuid4(),
                    connection_name="测试 Doris",
                    database="ODS",
                    tables=[{"table_name": "customer", "columns": ["name"]}],
                    table_strategy="drop_recreate",
                    target_suffix="sm4",
                )
            )
            session.add(DataPlatformWorkflow(id=self.workflow_id, name="客户数据离线流程"))
            session.add(
                DataPlatformWorkflowVersion(
                    id=self.version_id,
                    workflow_id=self.workflow_id,
                    version_no=1,
                    channel="prod",
                    status="online",
                    nodes=[
                        {
                            "key": "sm4-1",
                            "name": "客户表加密",
                            "node_type": "sm4_batch",
                            "config": {
                                "task_definition_id": str(self.task_id),
                                "task_definition_name": "客户表加密",
                            },
                        }
                    ],
                    edges=[],
                    schedule_enabled=True,
                )
            )
            session.commit()

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_update_creates_revision_and_freezes_existing_production_version(self) -> None:
        with patch(
            "recovery_service.services.doris_encryption.get_sync_session_factory",
            return_value=self.factory,
        ):
            updated = update_sm4_task_definition(
                self.task_id,
                updates={
                    "name": "客户表加密新版",
                    "tables": [{"table_name": "customer", "columns": ["name", "phone"]}],
                },
            )

        self.assertEqual(updated.revision, 2)
        with self.factory() as session:
            version = session.get(DataPlatformWorkflowVersion, self.version_id)
            config = version.nodes[0]["config"]
            self.assertEqual(config["task_definition_revision"], 1)
            self.assertEqual(config["task_definition_snapshot"]["name"], "客户表加密")
            self.assertEqual(config["task_definition_snapshot"]["tables"][0]["columns"], ["name"])
            revisions = session.scalars(
                select(DorisSm4TaskDefinitionRevision)
                .where(DorisSm4TaskDefinitionRevision.task_definition_id == self.task_id)
                .order_by(DorisSm4TaskDefinitionRevision.revision)
            ).all()
            self.assertEqual([item.revision for item in revisions], [1, 2])

    def test_new_production_submission_snapshot_uses_current_revision(self) -> None:
        with self.factory() as session:
            task = session.get(DorisSm4TaskDefinition, self.task_id)
            task.revision = 3
            task.name = "客户表加密 V3"
            task.tables = [{"table_name": "customer", "columns": ["name", "phone", "address"]}]
            session.commit()
            nodes = freeze_sm4_task_nodes(
                session,
                [
                    {
                        "key": "sm4-1",
                        "name": "旧显示名称",
                        "node_type": "sm4_batch",
                        "config": {"task_definition_id": str(self.task_id)},
                    }
                ],
            )

        config = nodes[0]["config"]
        self.assertEqual(nodes[0]["name"], "客户表加密 V3")
        self.assertEqual(config["task_definition_revision"], 3)
        self.assertEqual(config["task_definition_snapshot"]["tables"][0]["columns"][-1], "address")


if __name__ == "__main__":
    unittest.main()
