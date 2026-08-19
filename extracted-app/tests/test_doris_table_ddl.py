import unittest

from recovery_service.services.doris_table_ddl import (
    normalize_replication_allocation,
    rewrite_table_replication_allocation,
)


class DorisTableDdlTests(unittest.TestCase):
    def test_rewrites_existing_replication_allocation(self) -> None:
        ddl = '''CREATE TABLE `demo` (`id` bigint) PROPERTIES (
  "replication_allocation" = "tag.location.zone_a: 3"
)'''

        updated = rewrite_table_replication_allocation(ddl, "tag.location.default: 1")

        self.assertIn('"replication_allocation" = "tag.location.default: 1"', updated)
        self.assertNotIn("zone_a", updated)

    def test_converts_replication_num_to_allocation(self) -> None:
        ddl = 'CREATE TABLE `demo` (`id` bigint) PROPERTIES ("replication_num" = "3")'

        updated = rewrite_table_replication_allocation(ddl, "tag.location.default: 1")

        self.assertTrue(updated.endswith('PROPERTIES ("replication_allocation" = "tag.location.default: 1")'))
        self.assertNotIn("replication_num", updated)

    def test_adds_allocation_to_existing_properties(self) -> None:
        ddl = 'CREATE TABLE `demo` (`id` bigint) PROPERTIES ("in_memory" = "false")'

        updated = rewrite_table_replication_allocation(ddl, "tag.location.default: 1")

        self.assertIn('"replication_allocation" = "tag.location.default: 1",', updated)
        self.assertIn('"in_memory" = "false"', updated)

    def test_adds_properties_block_when_missing(self) -> None:
        ddl = "CREATE TABLE `demo` (`id` bigint);"

        updated = rewrite_table_replication_allocation(ddl, "tag.location.default: 1")

        self.assertTrue(updated.endswith(
            'PROPERTIES (\n  "replication_allocation" = "tag.location.default: 1"\n);'
        ))

    def test_rejects_unsafe_allocation(self) -> None:
        with self.assertRaises(ValueError):
            normalize_replication_allocation('tag.location.default: 1"; DROP TABLE demo; --')


if __name__ == "__main__":
    unittest.main()
