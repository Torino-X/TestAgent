"""CE-02 整改一：静态架构边界测试。

验证：
- app.context_engine 不导入 app.agent_runtime；
- context_engine domain 不导入 LangGraph；
- invoker 不位于 context_engine core；
- composer 不导入 Repository/Provider/Parser；
- validator 不做 I/O。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_CONTEXT_ENGINE = _BACKEND_ROOT / "app" / "context_engine"
_AGENT_RUNTIME = _BACKEND_ROOT / "app" / "agent_runtime"


def _py_files(dirpath: Path) -> list[Path]:
    return sorted(p for p in dirpath.rglob("*.py") if "__pycache__" not in str(p))


def _imports_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", _py_files(_CONTEXT_ENGINE), ids=lambda p: str(p.relative_to(_BACKEND_ROOT)))
def test_context_engine_does_not_import_agent_runtime(path):
    """app.context_engine 不导入 app.agent_runtime（Core 不依赖 Agent Runtime）。"""
    content = _imports_of(path)
    # 允许 TYPE_CHECKING 注释引用（不产生运行时导入）与文档字符串
    for match in re.finditer(r"^\s*(?:from|import)\s+app\.agent_runtime", content, re.MULTILINE):
        line = content[: match.start()].count("\n") + 1
        pytest.fail(f"{path.relative_to(_BACKEND_ROOT)}:{line} 导入 app.agent_runtime")


def test_context_engine_domain_does_not_import_langgraph():
    """context_engine domain（models/）不导入 LangGraph。"""
    models_dir = _CONTEXT_ENGINE / "models"
    for path in _py_files(models_dir):
        content = _imports_of(path)
        assert "langgraph" not in content, f"{path} 导入 LangGraph"


def test_invoker_not_in_context_engine_core():
    """ContextAwareLLMInvoker 不在 context_engine 核心层。"""
    # 旧路径已删除；新路径在 agent_runtime/context/
    old_invoker = _CONTEXT_ENGINE / "invoker"
    assert not old_invoker.exists(), "context_engine/invoker/ 应已删除"
    new_invoker = _AGENT_RUNTIME / "context"
    assert (new_invoker / "llm_invoker.py").is_file()
    assert (new_invoker / "provider_error_mapper.py").is_file()
    assert (new_invoker / "retry_policy.py").is_file()


def test_composer_does_not_import_repository_provider_parser():
    """composer 不导入 Repository/Provider/Parser。"""
    composer_dir = _CONTEXT_ENGINE / "composer"
    for path in _py_files(composer_dir):
        content = _imports_of(path)
        assert "repositories" not in content, f"{path} 导入 Repository"
        assert "provider" not in content.lower(), f"{path} 导入 Provider"
        assert "parser" not in content.lower(), f"{path} 导入 Parser"


def test_validator_does_no_io():
    """validator 不做 I/O（无 session/文件/HTTP import）。"""
    validator = _CONTEXT_ENGINE / "composer" / "validator.py"
    content = _imports_of(validator)
    assert "repositories" not in content
    assert "sqlalchemy" not in content
    assert "httpx" not in content
    assert "open(" not in content
    assert "session_factory" not in content


def test_agent_runtime_context_imports_engine_protocol():
    """Agent Runtime 可依赖 context_engine（单向：Agent Runtime → Context Engine Core）。"""
    content = (_AGENT_RUNTIME / "context" / "llm_invoker.py").read_text(encoding="utf-8")
    # Invoker 依赖 context_engine 的 models/errors（允许的单向依赖）
    assert "app.context_engine.models" in content
    assert "app.context_engine.errors" in content


def test_no_reverse_cycle_agent_runtime_into_context_engine_core():
    """agent_runtime/context 只导入 context_engine 的 models/errors/runtime.protocols，不导入 context_engine 内部实现层。"""
    context_dir = _AGENT_RUNTIME / "context"
    allowed = ("app.context_engine.models", "app.context_engine.errors", "app.context_engine.runtime.protocols")
    for path in _py_files(context_dir):
        content = _imports_of(path)
        for line in content.splitlines():
            if "import app.context_engine" not in line and "from app.context_engine" not in line:
                continue
            if any(line.startswith(f"from {a}") or f"from {a}" in line for a in allowed):
                continue
            # 顶层 __init__ 引用模型允许（不构成循环）
            if "agent_runtime.context" in path.name and "__init__" in path.name:
                continue
            pytest.fail(f"{path.relative_to(_BACKEND_ROOT)} 反向导入: {line.strip()}")
