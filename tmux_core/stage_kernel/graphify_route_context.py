"""Stage-owned AI Hermes routing hints for Graphify turns.

Graphify's runtime adapter deliberately does not read routing JSON.  Stage
code uses this module to select an AI Hermes route, reuse the bundled route
materializer, and pass only safe project-relative source paths and explicit
code symbols into ``GraphifyTurnContext``.

All routing failures are fail-soft: Graphify can still fall back to its prompt
keyword strategy, while the business stage continues unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
from types import ModuleType
from typing import Iterable, Mapping, Sequence

from tmux_core.runtime.graphify import (
    GraphifyQueryIntent,
    GraphifyTurnContext,
)


_MAX_ROUTE_TEXT_CHARS = 128_000
_MAX_ROUTED_PATHS = 32
_MAX_SYMBOLS = 24
_INLINE_CODE_RE = re.compile(r"`([^`\r\n]{1,240})`")
_CODE_SYMBOL_RE = re.compile(
    r"[A-Za-z_$][A-Za-z0-9_$]*(?:(?:::|\.|#)[A-Za-z_$][A-Za-z0-9_$]*)*"
)
_LINE_SUFFIX_RE = re.compile(r"(?::L?\d+(?::\d+)?)$")
_SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".clj",
        ".cljs",
        ".cpp",
        ".cs",
        ".dart",
        ".ex",
        ".exs",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".lua",
        ".m",
        ".mm",
        ".php",
        ".pl",
        ".pm",
        ".py",
        ".pyi",
        ".r",
        ".rb",
        ".rs",
        ".scala",
        ".sh",
        ".sol",
        ".swift",
        ".ts",
        ".tsx",
        ".vue",
    }
)
_SOURCE_BASENAMES = frozenset({"dockerfile", "makefile", "rakefile"})
_NON_SYMBOL_WORDS = frozenset(
    {
        "false",
        "json",
        "markdown",
        "none",
        "null",
        "python",
        "true",
        "typescript",
        "unknown",
    }
)


@dataclass(frozen=True)
class StageGraphifyRouteHints:
    """Safe route evidence resolved by the stage layer."""

    route_id: str = ""
    routed_paths: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()


def _stable_unique(values: Iterable[str], *, limit: int) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for raw_value in values:
        value = str(raw_value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return tuple(result)


def _read_text(path: Path, *, remaining_chars: int) -> str:
    if remaining_chars <= 0:
        return ""
    try:
        if not path.exists() or not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")[:remaining_chars]
    except (OSError, UnicodeError):
        return ""


def _load_json_mapping(path: Path) -> Mapping[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


@lru_cache(maxsize=1)
def _load_route_task_module() -> ModuleType | None:
    project_root = Path(__file__).resolve().parents[2]
    script_path = (
        project_root
        / "skills"
        / "ai-hermes-self-evolve"
        / "scripts"
        / "route_task.py"
    )
    if not script_path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location(
            "_tmux_coding_team_ai_hermes_route_task",
            script_path,
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 - routing hints must remain fail-soft.
        return None


def _select_route_id(task_routes: Mapping[str, object], task_text: str) -> str:
    routes = task_routes.get("routes", ())
    if not isinstance(routes, list):
        return ""
    normalized_text = task_text.casefold()
    candidates: list[tuple[int, int, int, str]] = []
    for index, route in enumerate(routes):
        if not isinstance(route, Mapping):
            continue
        route_id = str(route.get("id", "") or "").strip()
        if not route_id:
            continue
        match_keywords = tuple(
            str(value).strip().casefold()
            for value in route.get("match_keywords", ())
            if isinstance(value, str) and value.strip()
        ) if isinstance(route.get("match_keywords"), list) else ()
        negative_keywords = tuple(
            str(value).strip().casefold()
            for value in route.get("negative_keywords", ())
            if isinstance(value, str) and value.strip()
        ) if isinstance(route.get("negative_keywords"), list) else ()
        matched_count = sum(1 for keyword in match_keywords if keyword in normalized_text)
        if matched_count == 0 or any(keyword in normalized_text for keyword in negative_keywords):
            continue
        try:
            priority = int(route.get("route_priority", 0) or 0)
        except (TypeError, ValueError):
            priority = 0
        # Highest configured priority wins. More direct keyword matches break
        # ties; source order is the final deterministic tie-breaker.
        candidates.append((-priority, -matched_count, index, route_id))
    if not candidates:
        return ""
    candidates.sort()
    return candidates[0][3]


def _safe_source_path(project_root: Path, raw_value: object) -> str:
    raw_path = str(raw_value or "").strip().replace("\\", "/")
    if not raw_path:
        return ""
    raw_path = _LINE_SUFFIX_RE.sub("", raw_path)
    pure = PurePosixPath(raw_path)
    if pure.is_absolute() or ".." in pure.parts:
        return ""
    normalized = pure.as_posix()
    if not normalized:
        return ""
    candidate = (project_root / normalized).resolve()
    try:
        candidate.relative_to(project_root)
    except ValueError:
        return ""
    try:
        if not candidate.is_file():
            return ""
    except OSError:
        return ""
    if candidate.suffix.casefold() not in _SOURCE_SUFFIXES and candidate.name.casefold() not in _SOURCE_BASENAMES:
        return ""
    return candidate.relative_to(project_root).as_posix()


def _safe_symbol(raw_value: object) -> str:
    value = str(raw_value or "").strip()
    if not value or len(value) > 160 or not _CODE_SYMBOL_RE.fullmatch(value):
        return ""
    if value.casefold() in _NON_SYMBOL_WORDS:
        return ""
    if Path(value).suffix.casefold() in _SOURCE_SUFFIXES:
        return ""
    return value


def _explicit_refs_from_text(project_root: Path, text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    paths: list[str] = []
    symbols: list[str] = []
    for match in _INLINE_CODE_RE.finditer(text):
        value = match.group(1).strip()
        path = _safe_source_path(project_root, value)
        if path:
            paths.append(path)
            continue
        symbol = _safe_symbol(value)
        if symbol:
            symbols.append(symbol)
    return (
        _stable_unique(paths, limit=_MAX_ROUTED_PATHS),
        _stable_unique(symbols, limit=_MAX_SYMBOLS),
    )


def _module_symbols(
    project_root: Path,
    resolved: Mapping[str, object],
) -> tuple[str, ...]:
    repo_map = _load_json_mapping(project_root / "docs" / "repo_map.json")
    if repo_map is None:
        return ()
    modules = repo_map.get("modules", ())
    selected_payload = resolved.get("modules", {})
    selected_ids = (
        selected_payload.get("selected", ())
        if isinstance(selected_payload, Mapping)
        else ()
    )
    selected = {
        str(value).strip()
        for value in selected_ids
        if isinstance(value, str) and value.strip()
    }
    if not selected or not isinstance(modules, list):
        return ()
    symbols: list[str] = []
    for module in modules:
        if not isinstance(module, Mapping) or str(module.get("id", "")) not in selected:
            continue
        key_symbols = module.get("key_symbols", ())
        if not isinstance(key_symbols, list):
            continue
        symbols.extend(
            symbol
            for value in key_symbols
            if (symbol := _safe_symbol(value))
        )
    return _stable_unique(symbols, limit=_MAX_SYMBOLS)


def resolve_stage_graphify_route_hints(
    project_dir: str | Path,
    *,
    task_text: str,
    business_artifact_paths: Sequence[str | Path] = (),
    explicit_paths: Sequence[str | Path] = (),
    explicit_symbols: Sequence[str] = (),
) -> StageGraphifyRouteHints:
    """Resolve stage-level Graphify seeds without making routing authoritative."""

    try:
        project_root = Path(project_dir).expanduser().resolve()
    except (OSError, RuntimeError):
        return StageGraphifyRouteHints()
    task_routes = _load_json_mapping(project_root / "docs" / "task_routes.json")
    if task_routes is None:
        return StageGraphifyRouteHints()

    combined_parts = [str(task_text or "")]
    remaining = _MAX_ROUTE_TEXT_CHARS - len(combined_parts[0])
    for raw_path in business_artifact_paths:
        try:
            candidate = Path(raw_path).expanduser().resolve()
            candidate.relative_to(project_root)
        except (OSError, RuntimeError, ValueError):
            continue
        content = _read_text(candidate, remaining_chars=remaining)
        if content:
            combined_parts.append(content)
            remaining -= len(content)
        if remaining <= 0:
            break
    combined_text = "\n".join(combined_parts)[:_MAX_ROUTE_TEXT_CHARS]
    route_id = _select_route_id(task_routes, combined_text)
    if not route_id:
        return StageGraphifyRouteHints()

    resolver_module = _load_route_task_module()
    resolver = getattr(resolver_module, "resolve_route", None) if resolver_module else None
    if not callable(resolver):
        return StageGraphifyRouteHints()
    try:
        resolved = resolver(
            project_root=project_root,
            route_id=route_id,
            expand="conditional",
        )
    except Exception:  # noqa: BLE001 - stage behavior must not depend on hints.
        return StageGraphifyRouteHints()
    if not isinstance(resolved, Mapping) or resolved.get("status") != "ok":
        return StageGraphifyRouteHints()

    path_values: list[object] = list(explicit_paths)
    files = resolved.get("files", {})
    if isinstance(files, Mapping):
        for field in ("first_read", "then_check"):
            values = files.get(field, ())
            if isinstance(values, list):
                path_values.extend(values)
    for field in ("tests", "configs"):
        values = resolved.get(field, ())
        if isinstance(values, list):
            path_values.extend(values)
    explicit_text_paths, explicit_text_symbols = _explicit_refs_from_text(
        project_root,
        combined_text,
    )
    path_values.extend(explicit_text_paths)
    routed_paths = _stable_unique(
        (
            normalized
            for value in path_values
            if (normalized := _safe_source_path(project_root, value))
        ),
        limit=_MAX_ROUTED_PATHS,
    )

    symbols = _stable_unique(
        (
            normalized
            for value in (
                *explicit_symbols,
                *explicit_text_symbols,
                *_module_symbols(project_root, resolved),
            )
            if (normalized := _safe_symbol(value))
        ),
        limit=_MAX_SYMBOLS,
    )
    return StageGraphifyRouteHints(
        route_id=route_id,
        routed_paths=routed_paths,
        symbols=symbols,
    )


def build_stage_graphify_turn_context(
    project_dir: str | Path,
    *,
    stage_key: str,
    phase: str,
    role: str,
    intent: GraphifyQueryIntent,
    requirement_name: str = "",
    task_name: str = "",
    query_seeds: Sequence[str] = (),
    business_artifact_paths: Sequence[str | Path] = (),
    explicit_paths: Sequence[str | Path] = (),
    explicit_symbols: Sequence[str] = (),
    resolve_route_hints: bool = True,
) -> GraphifyTurnContext:
    """Build a Graphify context enriched by stage-resolved routing hints."""

    task_text = "\n".join(
        value
        for value in (
            str(requirement_name or "").strip(),
            str(task_name or "").strip(),
            str(stage_key or "").strip(),
            str(phase or "").strip(),
            *(str(value or "").strip() for value in query_seeds),
        )
        if value
    )
    hints = (
        resolve_stage_graphify_route_hints(
            project_dir,
            task_text=task_text,
            business_artifact_paths=business_artifact_paths,
            explicit_paths=explicit_paths,
            explicit_symbols=explicit_symbols,
        )
        if resolve_route_hints
        else StageGraphifyRouteHints()
    )
    return GraphifyTurnContext(
        stage_key=stage_key,
        phase=phase,
        role=role,
        intent=intent,
        requirement_name=requirement_name,
        task_name=task_name,
        routed_paths=hints.routed_paths,
        symbols=hints.symbols,
        query_seeds=tuple(str(value) for value in query_seeds if str(value or "").strip()),
    )


def worker_graphify_scope(worker: object) -> tuple[Path, str]:
    """Return worker project/requirement metadata without runtime coupling."""

    try:
        project_root = Path(getattr(worker, "work_dir", ".")).expanduser().resolve()
    except (OSError, RuntimeError):
        project_root = Path.cwd().resolve()
    requirement_name = ""
    runtime_metadata = getattr(worker, "runtime_metadata", None)
    try:
        payload = runtime_metadata() if callable(runtime_metadata) else runtime_metadata
        if isinstance(payload, Mapping):
            requirement_name = str(payload.get("requirement_name", "") or "").strip()
    except Exception:  # noqa: BLE001 - metadata is advisory.
        requirement_name = ""
    return project_root, requirement_name


def worker_graphify_route_hints_enabled(worker: object) -> bool:
    """Skip route reads only when the worker explicitly configured Graphify Off."""

    config = getattr(worker, "config", None)
    mode = getattr(config, "graphify_mode", None)
    if mode is None:
        return True
    normalized = str(getattr(mode, "value", mode) or "").strip().casefold()
    return normalized != "off"


__all__ = [
    "StageGraphifyRouteHints",
    "build_stage_graphify_turn_context",
    "resolve_stage_graphify_route_hints",
    "worker_graphify_route_hints_enabled",
    "worker_graphify_scope",
]
