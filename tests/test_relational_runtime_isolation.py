import unittest
from pathlib import Path


class RelationalRuntimeIsolationTests(unittest.TestCase):
    def test_relational_runtime_ignores_legacy_store(self):
        root = Path(__file__).resolve().parents[1]
        paths = [root / "app" / "relational_api.py"]
        for directory in ("repositories", "projections", "services"):
            paths.extend(sorted((root / "app" / directory).glob("*.py")))
        forbidden = (
            "draw_state",
            "PostgresStore",
            "SQLiteStore",
            "make_store",
            "Mutation",
        )

        for path in paths:
            source = path.read_text(encoding="utf-8")
            for token in forbidden:
                with self.subTest(path=path.name, token=token):
                    self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
