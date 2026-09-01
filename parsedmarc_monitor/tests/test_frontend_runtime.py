from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from dmarc_monitor import web as web_module


def test_frontend_runtime_contract_executes_with_node() -> None:
    node = shutil.which("node") or shutil.which("nodejs")
    assert node is not None, "Node.js is required for frontend runtime tests"

    static_root = Path(web_module.__file__).with_name("static")
    javascript = static_root / "app.js"
    runtime_test = Path(__file__).with_name("frontend_runtime.test.js")
    environment = os.environ.copy()
    environment["DMARC_APP_JS"] = str(javascript)
    node_args = ["--test"]
    if os.name == "nt":
        node_args.insert(0, "--test-isolation=none")
    result = subprocess.run(
        [node, *node_args, str(runtime_test)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, f"Node frontend tests failed:\n{result.stdout}\n{result.stderr}"
