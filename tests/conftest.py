"""Pytest shared fixtures. Only used by the test suite itself."""
from __future__ import annotations

from pathlib import Path

import pytest


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def csharp_dir() -> Path:
    return FIXTURES_DIR / "csharp"


@pytest.fixture(scope="session")
def cpp_dir() -> Path:
    return FIXTURES_DIR / "cpp"


@pytest.fixture(scope="session")
def python_dir() -> Path:
    return FIXTURES_DIR / "python"


@pytest.fixture(scope="session")
def java_dir() -> Path:
    return FIXTURES_DIR / "java"


@pytest.fixture(scope="session")
def kotlin_dir() -> Path:
    return FIXTURES_DIR / "kotlin"


@pytest.fixture(scope="session")
def scala_dir() -> Path:
    return FIXTURES_DIR / "scala"


@pytest.fixture(scope="session")
def go_dir() -> Path:
    return FIXTURES_DIR / "go"


@pytest.fixture(scope="session")
def rust_dir() -> Path:
    return FIXTURES_DIR / "rust"


@pytest.fixture(scope="session")
def php_dir() -> Path:
    return FIXTURES_DIR / "php"


@pytest.fixture(scope="session")
def ruby_dir() -> Path:
    return FIXTURES_DIR / "ruby"


@pytest.fixture(scope="session")
def md_dir() -> Path:
    return FIXTURES_DIR / "markdown"


@pytest.fixture(scope="session")
def yaml_dir() -> Path:
    return FIXTURES_DIR / "yaml"


@pytest.fixture(scope="session")
def css_dir() -> Path:
    return FIXTURES_DIR / "css"


@pytest.fixture(scope="session")
def scss_dir() -> Path:
    return FIXTURES_DIR / "scss"


@pytest.fixture(scope="session")
def sql_dir() -> Path:
    return FIXTURES_DIR / "sql"


@pytest.fixture(scope="session")
def lua_dir() -> Path:
    return FIXTURES_DIR / "lua"


@pytest.fixture(scope="session")
def swift_dir() -> Path:
    return FIXTURES_DIR / "swift"


@pytest.fixture(scope="session")
def html_dir() -> Path:
    return FIXTURES_DIR / "html"


@pytest.fixture(autouse=True)
def _reset_adapter_lookup_cache():
    """`core._adapter_for_language` is lru_cached at process level; a
    test that monkeypatches `adapters.ADAPTERS` would otherwise see
    stale pre-patch entries in any later `_render_family` /
    `_file_format_suffix` call. Clearing per-test keeps the cache an
    invisible optimization rather than hidden cross-test state."""
    from ast_outline.core import _adapter_for_language

    _adapter_for_language.cache_clear()
    yield
    _adapter_for_language.cache_clear()
