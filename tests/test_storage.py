import threading
import unittest

from ai_governance_foundation.storage import Database


class StorageTest(unittest.TestCase):
    def test_transaction_rolls_back_on_error(self):
        database = Database()
        with self.assertRaises(RuntimeError):
            with database.transaction():
                database.connection.execute(
                    "INSERT INTO organizations(organization_id,name,created_at) VALUES('o1','科研机构','now')"
                )
                raise RuntimeError("停止事务")
        count = database.connection.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]
        self.assertEqual(0, count)
        database.close()

    def test_schema_enables_foreign_keys(self):
        database = Database()
        enabled = database.connection.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(1, enabled)
        database.close()

    def test_transactions_are_serialized_across_threads(self):
        database = Database()
        errors = []

        def commit_organization(index):
            try:
                with database.transaction(immediate=True):
                    database.connection.execute(
                        "INSERT INTO organizations(organization_id,name,created_at) VALUES(?,?,?)",
                        (f"o{index}", "组织", "now"),
                    )
            except Exception as exc:  # pragma: no cover - 串行化后不应发生
                errors.append(repr(exc))

        threads = [threading.Thread(target=commit_organization, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        count = database.connection.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]
        self.assertEqual(20, count)
        database.close()


if __name__ == "__main__":
    unittest.main()
