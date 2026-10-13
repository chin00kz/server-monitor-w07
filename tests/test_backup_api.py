import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


APP_SRC = Path(__file__).resolve().parents[1] / "app" / "src"
sys.path.insert(0, str(APP_SRC))

try:
    import argon2  # noqa: F401
except ImportError:
    argon2_module = types.ModuleType("argon2")
    exceptions_module = types.ModuleType("argon2.exceptions")

    class PasswordHasher:
        pass

    class ArgonError(Exception):
        pass

    argon2_module.PasswordHasher = PasswordHasher
    exceptions_module.InvalidHashError = ArgonError
    exceptions_module.VerifyMismatchError = ArgonError
    sys.modules["argon2"] = argon2_module
    sys.modules["argon2.exceptions"] = exceptions_module

try:
    import fastapi  # noqa: F401
except ImportError:
    fastapi_module = types.ModuleType("fastapi")
    responses_module = types.ModuleType("fastapi.responses")

    class Dependency:
        def __init__(self, call):
            self.call = call

    def Depends(call):
        return Dependency(call)

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            self.status_code = status_code
            self.detail = detail

    class Request:
        pass

    class Response:
        pass

    class Route:
        def __init__(self, path, dependencies):
            self.path = path
            self.dependant = types.SimpleNamespace(dependencies=dependencies)

    class FastAPI:
        def __init__(self, **kwargs):
            self.routes = []

        def _route(self, path, dependencies=None):
            def decorator(function):
                self.routes.append(Route(path, dependencies or []))
                return function
            return decorator

        get = _route
        post = _route

        def middleware(self, _kind):
            return lambda function: function

    fastapi_module.Depends = Depends
    fastapi_module.FastAPI = FastAPI
    fastapi_module.HTTPException = HTTPException
    fastapi_module.Request = Request
    fastapi_module.Response = Response
    responses_module.FileResponse = object
    responses_module.RedirectResponse = object
    sys.modules["fastapi"] = fastapi_module
    sys.modules["fastapi.responses"] = responses_module

try:
    import pydantic  # noqa: F401
except ImportError:
    pydantic_module = types.ModuleType("pydantic")

    class BaseModel:
        pass

    pydantic_module.BaseModel = BaseModel
    sys.modules["pydantic"] = pydantic_module

import main  # noqa: E402


class BackupApiTests(unittest.TestCase):
    def test_routes_keep_login_and_management_dependencies(self):
        routes = {route.path: route for route in main.app.routes}

        self.assertIs(
            routes["/api/backups"].dependant.dependencies[0].call,
            main.require_login,
        )
        self.assertIs(
            routes["/api/backups/run"].dependant.dependencies[0].call,
            main.require_management,
        )

    def test_manual_endpoint_writes_only_fixed_backup_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_path = root / "request.json"
            status_path = root / "status.json"
            with (
                patch.object(main, "BACKUP_CONTROL_REQUEST", request_path),
                patch.object(main, "BACKUP_CONTROL_STATUS", status_path),
                patch.object(main.time, "time", return_value=2_000_000_000),
                patch.object(main.secrets, "token_hex", return_value="a" * 32),
            ):
                result = main.run_backup()

            request = json.loads(request_path.read_text())
            self.assertEqual(request, {
                "request_id": "a" * 32,
                "action": "backup",
                "created_at": 2_000_000_000,
            })
            self.assertEqual(result["status"], "running")
            self.assertNotIn("command", request)
            self.assertNotIn("path", request)


if __name__ == "__main__":
    unittest.main()
