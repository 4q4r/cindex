"""Regression guard: every aiohttp.ClientSession must honor env proxy.

aiohttp defaults to ``trust_env=False``, which silently ignores
``https_proxy``/``HTTPS_PROXY`` and connects directly to external APIs.
Behind a filtering egress proxy this surfaces as Cloudflare/BunnyCDN 403s
and empty result sets — the exact root cause of the Exa 0-results bug
(#67), which only patched Exa's ``_cs_post_json`` session while the same
defect stayed latent on every other ``aiohttp.ClientSession()`` in the
tree (fixed for the remaining sites in #68).

This invariant test parses every production ``.py`` file under ``apps/``
and fails if any ``aiohttp.ClientSession(...)`` call lacks
``trust_env=True``, so the bug cannot silently recur when a new session is
added or an existing one is edited. The AST scan is robust to multi-line
constructions (``doi_enrichment.py``, the Exa ``_cs_post_json`` site)
where ``trust_env`` sits on a later line.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _prod_python_files(root: Path) -> list[Path]:
    """Return production ``.py`` files under ``root``.

    Excludes tests, migrations, and ``test_*`` modules so fake/stub
    ``ClientSession`` definitions in the test suite are not scanned.
    """
    files: list[Path] = []
    for path in root.rglob("*.py"):
        parts = path.parts
        if "tests" in parts or "migrations" in parts:
            continue
        if path.name.startswith("test_"):
            continue
        files.append(path)
    return files


def _is_aiohttp_client_session_call(node: ast.Call) -> bool:
    """Return ``True`` if ``node`` is an ``aiohttp.ClientSession(...)`` call."""
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "ClientSession"
        and isinstance(func.value, ast.Name)
        and func.value.id == "aiohttp"
    )


def _has_trust_env_true(call: ast.Call) -> bool:
    """Return ``True`` if the call passes ``trust_env=True`` as a keyword."""
    for kw in call.keywords:
        if (
            kw.arg == "trust_env"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
        ):
            return True
    return False


def test_every_aiohttp_session_honors_env_proxy() -> None:
    """All ``aiohttp.ClientSession()`` calls in production pass ``trust_env=True``."""
    apps = REPO_ROOT / "apps"
    assert apps.is_dir(), f"apps dir not found at {apps}"
    files = _prod_python_files(apps)
    assert files, "no production python files found under apps/"

    offenders: list[str] = []
    found = 0
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_aiohttp_client_session_call(node):
                found += 1
                if not _has_trust_env_true(node):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno}",
                    )

    assert found, "expected at least one aiohttp.ClientSession() in production code"
    assert not offenders, (
        "aiohttp.ClientSession() without trust_env=True found (would bypass "
        "https_proxy/HTTPS_PROXY and hit Cloudflare 403s behind an egress proxy): "
        + ", ".join(offenders)
    )
