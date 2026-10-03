import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.routing import APIRoute

from app import config, main


class LegacyMaintenanceModeTests(unittest.TestCase):
    protected_paths = {
        "/api/admin/rounds/next",
        "/api/admin/rounds/undo",
        "/api/admin/reset",
        "/api/admin/holders",
        "/api/admin/holders/file",
        "/api/admin/notifications/send-all",
        "/api/admin/notifications/cancel",
        "/api/admin/notifications/clear-history",
        "/api/admin/notifications/resolve-unknown",
        "/api/admin/holders/block",
        "/api/admin/holders/unassign",
        "/api/admin/trading/credentials/generate",
        "/api/admin/trading/credentials/reset",
    }

    def test_maintenance_dependency_rejects_writes(self):
        with patch.object(config, "MAINTENANCE_MODE", True):
            with self.assertRaises(HTTPException) as caught:
                main.require_writable()
        self.assertEqual(caught.exception.status_code, 503)

        with patch.object(config, "MAINTENANCE_MODE", False):
            self.assertIsNone(main.require_writable())

    def test_every_legacy_state_write_has_maintenance_dependency(self):
        protected = set()
        for route in main.app.routes:
            if not isinstance(route, APIRoute):
                continue
            dependencies = {
                dependency.call for dependency in route.dependant.dependencies
            }
            if main.require_writable in dependencies:
                protected.add(route.path)

        self.assertEqual(protected, self.protected_paths)


if __name__ == "__main__":
    unittest.main()
