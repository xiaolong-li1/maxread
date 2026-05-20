from __future__ import annotations

import importlib
import inspect
import sys
import tempfile
from pathlib import Path


MODULES = [
    "tests.test_admin_server",
    "tests.test_arxiv",
    "tests.test_cli",
    "tests.test_feishu",
    "tests.test_help",
    "tests.test_job_queue",
    "tests.test_db",
    "tests.test_pipeline",
    "tests.test_publishing",
    "tests.test_prompts",
    "tests.test_render",
    "tests.test_sources",
    "tests.test_web_article",
]
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    passed = 0
    failed = 0
    for module_name in MODULES:
        module = importlib.import_module(module_name)
        for name, fn in inspect.getmembers(module, inspect.isfunction):
            if not name.startswith("test_"):
                continue
            try:
                if "tmp_path" in inspect.signature(fn).parameters:
                    with tempfile.TemporaryDirectory() as tmp:
                        fn(Path(tmp))
                else:
                    fn()
                print(f"PASS {module_name}.{name}")
                passed += 1
            except Exception as exc:
                print(f"FAIL {module_name}.{name}: {exc}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
