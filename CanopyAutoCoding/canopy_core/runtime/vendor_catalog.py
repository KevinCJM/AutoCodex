from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

SCHEMA_VERSION = "1.0"
SCAN_TIMEOUT_SEC = 12.0
VENDOR_ORDER: tuple[str, ...] = ("codex", "claude", "gemini", "opencode")
NORMALIZED_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
NATIVE_REASONING_ORDER: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max")
LEGACY_DEFAULT_MODEL_BY_VENDOR: dict[str, str] = {
    "codex": "gpt-5.4",
    "claude": "sonnet",
    "gemini": "auto",
    "opencode": "default",
}
LEGACY_MODEL_CHOICES_BY_VENDOR: dict[str, tuple[str, ...]] = {
    "codex": ("gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.3-codex-spark", "gpt-5.2"),
    "claude": ("sonnet", "opus", "haiku"),
    "gemini": ("auto", "flash", "pro"),
    "opencode": (),
}
LEGACY_MODEL_ALIASES_BY_VENDOR: dict[str, dict[str, str]] = {
    "codex": {
        "gpt-5": "gpt-5.4",
    },
    "gemini": {
        "default": "auto",
    },
}
UNAVAILABLE_SCAN_STATUS = "unavailable"
OK_SCAN_STATUS = "ok"
DEGRADED_SCAN_STATUS = "degraded"
SOURCE_UNAVAILABLE = "unavailable"
SOURCE_DYNAMIC_CLI = "dynamic_cli"
SOURCE_CONFIG_FILE = "config_file"
SOURCE_PACKAGE_METADATA = "package_metadata"
SOURCE_HELP_TEXT = "help_text"
SOURCE_LEGACY_FALLBACK = "legacy_fallback"
SOURCE_CACHE_FALLBACK = "cache_fallback"
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
REASONING_NATIVE = "native"
REASONING_MAPPED = "mapped"
REASONING_IMPLICIT_DEFAULT = "implicit_default"
REASONING_UNSUPPORTED = "unsupported"
REASONING_MODEL_FAMILY_ROUTING = "model_family_routing"
_CATALOG_LOCK = threading.RLock()
_CATALOG_SNAPSHOT: "CatalogSnapshot | None" = None
_CATALOG_REFRESHED = False


@dataclass(frozen=True)
class ReasoningInventory:
    vendor_id: str
    model_id: str
    source_kind: str
    confidence: str
    reasoning_control_mode: str
    supports_reasoning: bool
    native_reasoning_levels: tuple[str, ...] = ()
    normalized_reasoning_levels: tuple[str, ...] = ()
    default_normalized_effort: str = "high"
    default_native_level: str = ""
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReasoningInventory":
        return cls(
            vendor_id=str(payload.get("vendor_id", "")).strip(),
            model_id=str(payload.get("model_id", "")).strip(),
            source_kind=str(payload.get("source_kind", "")).strip(),
            confidence=str(payload.get("confidence", "")).strip(),
            reasoning_control_mode=str(payload.get("reasoning_control_mode", "")).strip(),
            supports_reasoning=bool(payload.get("supports_reasoning", False)),
            native_reasoning_levels=tuple(str(item).strip() for item in payload.get("native_reasoning_levels", []) if str(item).strip()),
            normalized_reasoning_levels=tuple(
                str(item).strip() for item in payload.get("normalized_reasoning_levels", []) if str(item).strip()
            ),
            default_normalized_effort=str(payload.get("default_normalized_effort", "high")).strip() or "high",
            default_native_level=str(payload.get("default_native_level", "")).strip(),
            notes=tuple(str(item).strip() for item in payload.get("notes", []) if str(item).strip()),
        )


@dataclass(frozen=True)
class ModelInventory:
    vendor_id: str
    model_id: str
    display_name: str
    source_kind: str
    confidence: str
    reasoning: ReasoningInventory
    synthetic: bool = False
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reasoning"] = self.reasoning.to_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ModelInventory":
        return cls(
            vendor_id=str(payload.get("vendor_id", "")).strip(),
            model_id=str(payload.get("model_id", "")).strip(),
            display_name=str(payload.get("display_name", "")).strip(),
            source_kind=str(payload.get("source_kind", "")).strip(),
            confidence=str(payload.get("confidence", "")).strip(),
            reasoning=ReasoningInventory.from_dict(dict(payload.get("reasoning", {}) or {})),
            synthetic=bool(payload.get("synthetic", False)),
            notes=tuple(str(item).strip() for item in payload.get("notes", []) if str(item).strip()),
        )


@dataclass(frozen=True)
class VendorInventory:
    vendor_id: str
    installed: bool
    scan_status: str
    source_kind: str
    confidence: str
    binary_path: str
    models: tuple[ModelInventory, ...] = ()
    default_model: str = ""
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["models"] = [item.to_dict() for item in self.models]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "VendorInventory":
        return cls(
            vendor_id=str(payload.get("vendor_id", "")).strip(),
            installed=bool(payload.get("installed", False)),
            scan_status=str(payload.get("scan_status", "")).strip(),
            source_kind=str(payload.get("source_kind", "")).strip(),
            confidence=str(payload.get("confidence", "")).strip(),
            binary_path=str(payload.get("binary_path", "")).strip(),
            models=tuple(ModelInventory.from_dict(dict(item or {})) for item in payload.get("models", []) if isinstance(item, dict)),
            default_model=str(payload.get("default_model", "")).strip(),
            notes=tuple(str(item).strip() for item in payload.get("notes", []) if str(item).strip()),
        )

    def model_ids(self) -> tuple[str, ...]:
        return tuple(item.model_id for item in self.models)

    def find_model(self, model_id: str) -> ModelInventory | None:
        candidate = str(model_id or "").strip()
        for item in self.models:
            if item.model_id == candidate:
                return item
        return None


@dataclass(frozen=True)
class CatalogSnapshot:
    schema_version: str
    generated_at: str
    cache_path: str
    vendors: tuple[VendorInventory, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "cache_path": self.cache_path,
            "vendors": [item.to_dict() for item in self.vendors],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CatalogSnapshot":
        return cls(
            schema_version=str(payload.get("schema_version", SCHEMA_VERSION)).strip() or SCHEMA_VERSION,
            generated_at=str(payload.get("generated_at", "")).strip() or _now_iso(),
            cache_path=str(payload.get("cache_path", "")).strip(),
            vendors=tuple(VendorInventory.from_dict(dict(item or {})) for item in payload.get("vendors", []) if isinstance(item, dict)),
        )

    def vendor(self, vendor_id: str) -> VendorInventory:
        normalized_vendor = normalize_vendor_id(vendor_id)
        for item in self.vendors:
            if item.vendor_id == normalized_vendor:
                return item
        return _unavailable_vendor(normalized_vendor, "")


@dataclass(frozen=True)
class LaunchResolution:
    vendor_id: str
    requested_model: str
    resolved_model: str
    requested_effort: str
    normalized_effort: str
    native_reasoning_level: str
    resolved_variant: str
    reasoning_control_mode: str
    supports_reasoning: bool
    catalog_source_kind: str
    confidence: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProbeResult:
    argv: tuple[str, ...]
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _cache_root() -> Path:
    xdg_cache_home = str(os.environ.get("XDG_CACHE_HOME", "")).strip()
    if xdg_cache_home:
        return Path(xdg_cache_home).expanduser().resolve() / "canopy_auto_coding"
    if os.name == "nt":
        local_app_data = str(os.environ.get("LOCALAPPDATA", "")).strip()
        if local_app_data:
            return Path(local_app_data).expanduser().resolve() / "canopy_auto_coding"
    return Path.home().expanduser().resolve() / ".cache" / "canopy_auto_coding"


def catalog_cache_path() -> Path:
    return _cache_root() / "vendor_catalog.json"


def normalize_vendor_id(value: str) -> str:
    text = str(value or "").strip().lower()
    if text not in VENDOR_ORDER:
        raise ValueError(f"unsupported vendor: {value}")
    return text


def normalize_effort(value: str | None) -> str:
    text = str(value or "high").strip().lower()
    if text not in NORMALIZED_EFFORT_LEVELS:
        raise ValueError(f"unsupported reasoning effort: {value}")
    return text


def _command_probe(argv: list[str], *, timeout_sec: float = SCAN_TIMEOUT_SEC) -> ProbeResult:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as error:
        return ProbeResult(
            argv=tuple(argv),
            ok=False,
            returncode=-1,
            stdout=str(getattr(error, "stdout", "") or ""),
            stderr=str(getattr(error, "stderr", "") or ""),
            timed_out=True,
        )
    return ProbeResult(
        argv=tuple(argv),
        ok=completed.returncode == 0,
        returncode=completed.returncode,
        stdout=str(completed.stdout or ""),
        stderr=str(completed.stderr or ""),
        timed_out=False,
    )


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _load_cached_snapshot() -> CatalogSnapshot | None:
    cache_path = catalog_cache_path()
    if not cache_path.exists() or not cache_path.is_file():
        return None
    payload = _read_json_file(cache_path)
    if not payload:
        return None
    snapshot = CatalogSnapshot.from_dict(payload)
    return CatalogSnapshot(
        schema_version=snapshot.schema_version,
        generated_at=snapshot.generated_at,
        cache_path=str(cache_path),
        vendors=snapshot.vendors,
    )


def _save_cached_snapshot(snapshot: CatalogSnapshot) -> None:
    cache_path = catalog_cache_path()
    payload = snapshot.to_dict()
    payload["cache_path"] = str(cache_path)
    _write_json_atomic(cache_path, payload)


def reset_catalog_cache_for_tests() -> None:
    global _CATALOG_SNAPSHOT, _CATALOG_REFRESHED
    with _CATALOG_LOCK:
        _CATALOG_SNAPSHOT = None
        _CATALOG_REFRESHED = False


def _resolved_binary_path(binary_name: str) -> str:
    candidate = shutil.which(binary_name)
    if not candidate:
        return ""
    return str(Path(candidate).expanduser().resolve())


def _unique_models(items: list[ModelInventory]) -> tuple[ModelInventory, ...]:
    seen: set[str] = set()
    ordered: list[ModelInventory] = []
    for item in items:
        if item.model_id in seen:
            continue
        seen.add(item.model_id)
        ordered.append(item)
    return tuple(ordered)


def _resolve_default_model(models: Sequence[ModelInventory], preferred: str = "", fallback_vendor: str = "") -> str:
    candidate_ids = {item.model_id for item in models}
    if preferred and preferred in candidate_ids:
        return preferred
    if fallback_vendor:
        legacy = LEGACY_DEFAULT_MODEL_BY_VENDOR.get(fallback_vendor, "")
        if legacy and legacy in candidate_ids:
            return legacy
    for item in models:
        if item.confidence in {CONFIDENCE_HIGH, CONFIDENCE_MEDIUM}:
            return item.model_id
    return models[0].model_id if models else ""


def _prioritize_default_model(models: Sequence[ModelInventory], preferred: str) -> tuple[ModelInventory, ...]:
    preferred_model = str(preferred or "").strip()
    if not preferred_model:
        return tuple(models)
    prioritized = [item for item in models if item.model_id == preferred_model]
    remainder = [item for item in models if item.model_id != preferred_model]
    return tuple([*prioritized, *remainder])


def _full_normalized_levels() -> tuple[str, ...]:
    return NORMALIZED_EFFORT_LEVELS


def _unsupported_reasoning(vendor_id: str, model_id: str, *, source_kind: str, confidence: str, note: str = "") -> ReasoningInventory:
    notes = (note,) if note else ()
    return ReasoningInventory(
        vendor_id=vendor_id,
        model_id=model_id,
        source_kind=source_kind,
        confidence=confidence,
        reasoning_control_mode=REASONING_UNSUPPORTED,
        supports_reasoning=False,
        native_reasoning_levels=(),
        normalized_reasoning_levels=("high",),
        default_normalized_effort="high",
        default_native_level="",
        notes=notes,
    )


def _build_model(
    vendor_id: str,
    model_id: str,
    *,
    display_name: str = "",
    source_kind: str,
    confidence: str,
    reasoning: ReasoningInventory,
    synthetic: bool = False,
    notes: Sequence[str] = (),
) -> ModelInventory:
    normalized_model_id = str(model_id or "").strip()
    return ModelInventory(
        vendor_id=vendor_id,
        model_id=normalized_model_id,
        display_name=str(display_name or normalized_model_id).strip() or normalized_model_id,
        source_kind=source_kind,
        confidence=confidence,
        reasoning=reasoning,
        synthetic=synthetic,
        notes=tuple(str(item).strip() for item in notes if str(item).strip()),
    )


def _fallback_reasoning_for_vendor(vendor_id: str, model_id: str, *, source_kind: str, confidence: str) -> ReasoningInventory:
    if vendor_id == "codex":
        native = ("low", "medium", "high", "xhigh")
        return ReasoningInventory(
            vendor_id=vendor_id,
            model_id=model_id,
            source_kind=source_kind,
            confidence=confidence,
            reasoning_control_mode=REASONING_NATIVE,
            supports_reasoning=True,
            native_reasoning_levels=native,
            normalized_reasoning_levels=_full_normalized_levels(),
            default_normalized_effort="high",
            default_native_level="medium",
            notes=("legacy_fallback",),
        )
    if vendor_id == "claude":
        native = ("low", "medium", "high", "max")
        return ReasoningInventory(
            vendor_id=vendor_id,
            model_id=model_id,
            source_kind=source_kind,
            confidence=confidence,
            reasoning_control_mode=REASONING_NATIVE,
            supports_reasoning=True,
            native_reasoning_levels=native,
            normalized_reasoning_levels=_full_normalized_levels(),
            default_normalized_effort="high",
            default_native_level="high",
            notes=("legacy_fallback",),
        )
    if vendor_id == "gemini":
        if model_id in {"auto", "pro", "flash"}:
            return ReasoningInventory(
                vendor_id=vendor_id,
                model_id=model_id,
                source_kind=source_kind,
                confidence=confidence,
                reasoning_control_mode=REASONING_MODEL_FAMILY_ROUTING,
                supports_reasoning=True,
                native_reasoning_levels=(),
                normalized_reasoning_levels=_full_normalized_levels(),
                default_normalized_effort="high",
                default_native_level="",
                notes=("legacy_synthetic_family_alias",),
            )
        return _unsupported_reasoning(vendor_id, model_id, source_kind=source_kind, confidence=confidence, note="legacy_fallback")
    return _unsupported_reasoning(vendor_id, model_id, source_kind=source_kind, confidence=confidence, note="legacy_fallback")


def _legacy_models(vendor_id: str) -> tuple[ModelInventory, ...]:
    models = [
        _build_model(
            vendor_id,
            model_id,
            source_kind=SOURCE_LEGACY_FALLBACK,
            confidence=CONFIDENCE_LOW,
            reasoning=_fallback_reasoning_for_vendor(
                vendor_id,
                model_id,
                source_kind=SOURCE_LEGACY_FALLBACK,
                confidence=CONFIDENCE_LOW,
            ),
            synthetic=vendor_id == "gemini" and model_id in {"auto", "pro", "flash"},
            notes=("legacy_fallback",),
        )
        for model_id in LEGACY_MODEL_CHOICES_BY_VENDOR.get(vendor_id, ())
    ]
    return tuple(models)


def _unavailable_vendor(vendor_id: str, binary_path: str) -> VendorInventory:
    return VendorInventory(
        vendor_id=vendor_id,
        installed=False,
        scan_status=UNAVAILABLE_SCAN_STATUS,
        source_kind=SOURCE_UNAVAILABLE,
        confidence=CONFIDENCE_HIGH,
        binary_path=binary_path,
        models=(),
        default_model="",
        notes=("binary_not_found",),
    )


def _fallback_vendor(vendor_id: str, *, binary_path: str, note: str, models: Sequence[ModelInventory] | None = None) -> VendorInventory:
    fallback_models = tuple(models) if models is not None else _legacy_models(vendor_id)
    return VendorInventory(
        vendor_id=vendor_id,
        installed=bool(binary_path),
        scan_status=DEGRADED_SCAN_STATUS,
        source_kind=SOURCE_LEGACY_FALLBACK,
        confidence=CONFIDENCE_LOW,
        binary_path=binary_path,
        models=fallback_models,
        default_model=_resolve_default_model(fallback_models, fallback_vendor=vendor_id),
        notes=tuple(item for item in ("legacy_fallback", note) if item),
    )


def _cached_degraded_vendor(vendor_id: str, prior_vendor: VendorInventory, *, binary_path: str, note: str) -> VendorInventory:
    return VendorInventory(
        vendor_id=vendor_id,
        installed=bool(binary_path),
        scan_status=DEGRADED_SCAN_STATUS,
        source_kind=SOURCE_CACHE_FALLBACK,
        confidence=prior_vendor.confidence or CONFIDENCE_LOW,
        binary_path=binary_path,
        models=prior_vendor.models,
        default_model=prior_vendor.default_model,
        notes=tuple(dict.fromkeys([*prior_vendor.notes, "cache_retained", note])),
    )


def _load_json_probe(probe: ProbeResult) -> Any:
    try:
        return json.loads(probe.stdout)
    except Exception:
        return {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _resolve_node_package_root(binary_path: str, package_name: str) -> Path | None:
    resolved = Path(binary_path).expanduser().resolve()
    for parent in resolved.parents:
        if parent.name == package_name:
            return parent
    return None


def _extract_quoted_union(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(re.findall(r'"([^"]+)"', text)))


def _extract_claude_models(binary_path: str) -> tuple[str, ...]:
    package_root = _resolve_node_package_root(binary_path, "claude-code")
    if package_root is None:
        return ()
    schema_text = _read_text(package_root / "sdk-tools.d.ts")
    if not schema_text:
        return ()
    match = re.search(r"model\?:\s*([^;]+);", schema_text)
    if not match:
        return ()
    return _extract_quoted_union(match.group(1))


def _extract_claude_efforts(help_text: str) -> tuple[str, ...]:
    candidates = tuple(dict.fromkeys(re.findall(r"\b(?:low|medium|high|xhigh|max)\b", help_text)))
    return candidates or ("low", "medium", "high", "xhigh", "max")


def _extract_gemini_models(binary_path: str) -> tuple[str, ...]:
    package_root = _resolve_node_package_root(binary_path, "gemini-cli")
    if package_root is None:
        return ()
    matches: set[str] = set()
    for relative_path in ("README.md", "bundle"):
        target = package_root / relative_path
        if target.is_file():
            candidates = re.findall(r"\bgemini-[a-z0-9][a-z0-9.-]*[a-z0-9]\b", _read_text(target), re.IGNORECASE)
            matches.update(candidate.lower() for candidate in candidates)
        elif target.is_dir():
            for child in sorted(target.glob("*.js")):
                candidates = re.findall(r"\bgemini-[a-z0-9][a-z0-9.-]*[a-z0-9]\b", _read_text(child), re.IGNORECASE)
                for candidate in candidates:
                    normalized = candidate.lower()
                    if normalized.endswith(".js") or normalized.endswith("-"):
                        continue
                    if "9001-super-duper" in normalized:
                        continue
                    matches.add(normalized)
    return tuple(sorted(matches))


def parse_codex_models_output(stdout: str) -> tuple[dict[str, Any], ...]:
    try:
        payload = json.loads(stdout)
    except Exception:
        return ()
    if isinstance(payload, dict):
        items = payload.get("models") or payload.get("data") or []
    else:
        items = payload
    if not isinstance(items, list):
        return ()
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        visibility = str(item.get("visibility", "")).strip().lower()
        if visibility and visibility != "list":
            continue
        normalized.append(item)
    normalized.sort(key=lambda item: int(item.get("priority", 0)), reverse=True)
    return tuple(normalized)


def parse_opencode_verbose_output(stdout: str) -> tuple[dict[str, Any], ...]:
    lines = stdout.splitlines()
    items: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line.startswith("{"):
            index += 1
            continue
        if "/" not in line:
            index += 1
            continue
        full_model_id = line
        index += 1
        while index < len(lines) and not lines[index].lstrip().startswith("{"):
            index += 1
        if index >= len(lines):
            break
        brace_depth = 0
        json_lines: list[str] = []
        while index < len(lines):
            current = lines[index]
            json_lines.append(current)
            brace_depth += current.count("{")
            brace_depth -= current.count("}")
            index += 1
            if brace_depth <= 0:
                break
        try:
            payload = json.loads("\n".join(json_lines))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        payload = dict(payload)
        payload["full_model_id"] = full_model_id
        items.append(payload)
    return tuple(items)


def parse_opencode_debug_config_output(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _build_codex_models(items: Sequence[dict[str, Any]]) -> tuple[ModelInventory, ...]:
    models: list[ModelInventory] = []
    for item in items:
        model_id = str(item.get("slug") or item.get("id") or item.get("display_name") or "").strip()
        if not model_id:
            continue
        native_levels = tuple(
            str(level.get("effort", "")).strip()
            for level in item.get("supported_reasoning_levels", [])
            if isinstance(level, dict) and str(level.get("effort", "")).strip()
        )
        reasoning = ReasoningInventory(
            vendor_id="codex",
            model_id=model_id,
            source_kind=SOURCE_DYNAMIC_CLI,
            confidence=CONFIDENCE_HIGH,
            reasoning_control_mode=REASONING_NATIVE,
            supports_reasoning=bool(native_levels),
            native_reasoning_levels=native_levels,
            normalized_reasoning_levels=_full_normalized_levels(),
            default_normalized_effort="high",
            default_native_level=str(item.get("default_reasoning_level", "medium")).strip() or "medium",
            notes=(),
        )
        models.append(
            _build_model(
                "codex",
                model_id,
                display_name=str(item.get("display_name") or model_id),
                source_kind=SOURCE_DYNAMIC_CLI,
                confidence=CONFIDENCE_HIGH,
                reasoning=reasoning,
                notes=(),
            )
        )
    return _unique_models(models)


def _build_opencode_models(items: Sequence[dict[str, Any]]) -> tuple[ModelInventory, ...]:
    models: list[ModelInventory] = []
    for item in items:
        provider_id = str(item.get("providerID") or "").strip()
        model_id = str(item.get("id") or "").strip()
        full_model_id = str(item.get("full_model_id") or "").strip() or (
            f"{provider_id}/{model_id}" if provider_id and model_id else model_id
        )
        if not full_model_id:
            continue
        capabilities = item.get("capabilities", {})
        variants = item.get("variants", {})
        native_levels = []
        if isinstance(variants, dict):
            for variant in variants.values():
                if not isinstance(variant, dict):
                    continue
                reasoning_effort = str(variant.get("reasoningEffort", "")).strip()
                if reasoning_effort:
                    native_levels.append(reasoning_effort)
        supports_reasoning = bool((capabilities or {}).get("reasoning", False))
        mode = REASONING_MAPPED if native_levels else (REASONING_IMPLICIT_DEFAULT if supports_reasoning else REASONING_UNSUPPORTED)
        notes: list[str] = []
        if supports_reasoning and not native_levels:
            notes.append("reasoning_supported_without_explicit_variants")
        reasoning = ReasoningInventory(
            vendor_id="opencode",
            model_id=full_model_id,
            source_kind=SOURCE_DYNAMIC_CLI,
            confidence=CONFIDENCE_HIGH,
            reasoning_control_mode=mode,
            supports_reasoning=supports_reasoning,
            native_reasoning_levels=tuple(dict.fromkeys(native_levels)),
            normalized_reasoning_levels=_full_normalized_levels() if supports_reasoning else ("high",),
            default_normalized_effort="high",
            default_native_level="",
            notes=tuple(notes),
        )
        models.append(
            _build_model(
                "opencode",
                full_model_id,
                display_name=str(item.get("name") or full_model_id),
                source_kind=SOURCE_DYNAMIC_CLI,
                confidence=CONFIDENCE_HIGH,
                reasoning=reasoning,
                notes=(),
            )
        )
    return _unique_models(models)


def _build_opencode_config_models(payload: dict[str, Any]) -> tuple[ModelInventory, ...]:
    provider_payload = payload.get("provider", {})
    if not isinstance(provider_payload, dict):
        return ()
    models: list[ModelInventory] = []
    for provider_id, provider_entry in provider_payload.items():
        if not isinstance(provider_entry, dict):
            continue
        provider_models = provider_entry.get("models", {})
        if not isinstance(provider_models, dict):
            continue
        for model_id, model_entry in provider_models.items():
            full_model_id = f"{provider_id}/{model_id}"
            display_name = full_model_id
            if isinstance(model_entry, dict):
                display_name = str(model_entry.get("name") or full_model_id).strip() or full_model_id
            models.append(
                _build_model(
                    "opencode",
                    full_model_id,
                    display_name=display_name,
                    source_kind=SOURCE_CONFIG_FILE,
                    confidence=CONFIDENCE_MEDIUM,
                    reasoning=ReasoningInventory(
                        vendor_id="opencode",
                        model_id=full_model_id,
                        source_kind=SOURCE_CONFIG_FILE,
                        confidence=CONFIDENCE_MEDIUM,
                        reasoning_control_mode=REASONING_IMPLICIT_DEFAULT,
                        supports_reasoning=True,
                        native_reasoning_levels=(),
                        normalized_reasoning_levels=_full_normalized_levels(),
                        default_normalized_effort="high",
                        default_native_level="",
                        notes=("config_model_without_reasoning_metadata",),
                    ),
                    notes=("config_file_model",),
                )
            )
    return _unique_models(models)


def _build_claude_models(model_ids: Sequence[str], effort_levels: Sequence[str]) -> tuple[ModelInventory, ...]:
    native_levels = tuple(dict.fromkeys(level for level in effort_levels if level))
    models = [
        _build_model(
            "claude",
            model_id,
            source_kind=SOURCE_PACKAGE_METADATA,
            confidence=CONFIDENCE_MEDIUM,
            reasoning=ReasoningInventory(
                vendor_id="claude",
                model_id=model_id,
                source_kind=SOURCE_PACKAGE_METADATA,
                confidence=CONFIDENCE_MEDIUM,
                reasoning_control_mode=REASONING_NATIVE,
                supports_reasoning=True,
                native_reasoning_levels=native_levels,
                normalized_reasoning_levels=_full_normalized_levels(),
                default_normalized_effort="high",
                default_native_level="high",
                notes=("package_metadata",),
            ),
            notes=("package_metadata",),
        )
        for model_id in model_ids
    ]
    return _unique_models(models)


def _build_gemini_models(model_ids: Sequence[str]) -> tuple[ModelInventory, ...]:
    models: list[ModelInventory] = []
    for synthetic_model in ("auto", "flash", "pro"):
        models.append(
            _build_model(
                "gemini",
                synthetic_model,
                source_kind=SOURCE_PACKAGE_METADATA,
                confidence=CONFIDENCE_MEDIUM,
                reasoning=ReasoningInventory(
                    vendor_id="gemini",
                    model_id=synthetic_model,
                    source_kind=SOURCE_PACKAGE_METADATA,
                    confidence=CONFIDENCE_MEDIUM,
                    reasoning_control_mode=REASONING_MODEL_FAMILY_ROUTING,
                    supports_reasoning=True,
                    native_reasoning_levels=(),
                    normalized_reasoning_levels=_full_normalized_levels(),
                    default_normalized_effort="high",
                    default_native_level="",
                    notes=("synthetic_family_alias",),
                ),
                synthetic=True,
                notes=("synthetic_family_alias",),
            )
        )
    if os.environ.get("CANOPY_GEMINI_EXPERIMENTAL_MODELS") == "1":
        for model_id in model_ids:
            if model_id in {"auto", "flash", "pro"}:
                continue
            models.append(
                _build_model(
                    "gemini",
                    model_id,
                    source_kind=SOURCE_PACKAGE_METADATA,
                    confidence=CONFIDENCE_MEDIUM,
                    reasoning=_unsupported_reasoning(
                        "gemini",
                        model_id,
                        source_kind=SOURCE_PACKAGE_METADATA,
                        confidence=CONFIDENCE_MEDIUM,
                        note="explicit_model_has_no_native_effort_catalog",
                    ),
                    notes=("package_metadata", "experimental_model"),
                )
            )
    return _unique_models(models)


def _scan_codex_vendor(binary_path: str) -> VendorInventory:
    probe = _command_probe(["codex", "debug", "models"])
    items = parse_codex_models_output(probe.stdout) if probe.ok else ()
    if not items:
        return _fallback_vendor("codex", binary_path=binary_path, note="codex_debug_models_failed")
    models = _build_codex_models(items)
    default_model = _resolve_default_model(models, fallback_vendor="codex")
    return VendorInventory(
        vendor_id="codex",
        installed=True,
        scan_status=OK_SCAN_STATUS,
        source_kind=SOURCE_DYNAMIC_CLI,
        confidence=CONFIDENCE_HIGH,
        binary_path=binary_path,
        models=_prioritize_default_model(models, default_model),
        default_model=default_model,
        notes=(),
    )


def _scan_opencode_vendor(binary_path: str) -> VendorInventory:
    models_probe = _command_probe(["opencode", "models", "--verbose"], timeout_sec=15.0)
    config_probe = _command_probe(["opencode", "debug", "config"])
    dynamic_models = _build_opencode_models(parse_opencode_verbose_output(models_probe.stdout)) if models_probe.ok else ()
    config_payload = parse_opencode_debug_config_output(config_probe.stdout) if config_probe.ok else {}
    config_models = _build_opencode_config_models(config_payload)
    models = _unique_models([*dynamic_models, *config_models])
    default_model = str(config_payload.get("model", "")).strip()
    if not models and default_model:
        models = _unique_models(
            [
                _build_model(
                    "opencode",
                    default_model,
                    source_kind=SOURCE_CONFIG_FILE,
                    confidence=CONFIDENCE_MEDIUM,
                    reasoning=ReasoningInventory(
                        vendor_id="opencode",
                        model_id=default_model,
                        source_kind=SOURCE_CONFIG_FILE,
                        confidence=CONFIDENCE_MEDIUM,
                        reasoning_control_mode=REASONING_IMPLICIT_DEFAULT,
                        supports_reasoning=True,
                        native_reasoning_levels=(),
                        normalized_reasoning_levels=_full_normalized_levels(),
                        default_normalized_effort="high",
                        default_native_level="",
                        notes=("default_model_without_reasoning_metadata",),
                    ),
                    notes=("config_default_model",),
                )
            ]
        )
    if not models:
        return _fallback_vendor("opencode", binary_path=binary_path, note="opencode_catalog_probe_failed", models=())
    notes = []
    if default_model:
        notes.append(f"default_model={default_model}")
    default_model = _resolve_default_model(models, preferred=default_model)
    return VendorInventory(
        vendor_id="opencode",
        installed=True,
        scan_status=OK_SCAN_STATUS,
        source_kind=SOURCE_DYNAMIC_CLI if dynamic_models else SOURCE_CONFIG_FILE,
        confidence=CONFIDENCE_HIGH if dynamic_models else CONFIDENCE_MEDIUM,
        binary_path=binary_path,
        models=_prioritize_default_model(models, default_model),
        default_model=default_model,
        notes=tuple(notes),
    )


def _scan_claude_vendor(binary_path: str) -> VendorInventory:
    help_probe = _command_probe(["claude", "--help"])
    model_ids = _extract_claude_models(binary_path)
    effort_levels = _extract_claude_efforts(help_probe.stdout or help_probe.stderr)
    if not model_ids:
        return _fallback_vendor("claude", binary_path=binary_path, note="claude_package_metadata_missing")
    models = _build_claude_models(model_ids, effort_levels)
    default_model = _resolve_default_model(models, fallback_vendor="claude")
    return VendorInventory(
        vendor_id="claude",
        installed=True,
        scan_status=OK_SCAN_STATUS,
        source_kind=SOURCE_PACKAGE_METADATA,
        confidence=CONFIDENCE_MEDIUM,
        binary_path=binary_path,
        models=_prioritize_default_model(models, default_model),
        default_model=default_model,
        notes=("help_text_effort_levels" if help_probe.ok else "package_only",),
    )


def _scan_gemini_vendor(binary_path: str) -> VendorInventory:
    models = _build_gemini_models(_extract_gemini_models(binary_path))
    if not models:
        return _fallback_vendor("gemini", binary_path=binary_path, note="gemini_package_metadata_missing")
    default_model = _resolve_default_model(models, fallback_vendor="gemini")
    return VendorInventory(
        vendor_id="gemini",
        installed=True,
        scan_status=OK_SCAN_STATUS,
        source_kind=SOURCE_PACKAGE_METADATA,
        confidence=CONFIDENCE_MEDIUM,
        binary_path=binary_path,
        models=_prioritize_default_model(models, default_model),
        default_model=default_model,
        notes=("synthetic_family_aliases_included",),
    )


_SCANNERS: dict[str, Callable[[str], VendorInventory]] = {
    "codex": _scan_codex_vendor,
    "claude": _scan_claude_vendor,
    "gemini": _scan_gemini_vendor,
    "opencode": _scan_opencode_vendor,
}


def refresh_catalog_snapshot(*, prior_snapshot: CatalogSnapshot | None = None) -> CatalogSnapshot:
    cache_path = catalog_cache_path()
    prior_by_vendor = {item.vendor_id: item for item in prior_snapshot.vendors} if prior_snapshot else {}
    vendors: list[VendorInventory] = []
    for vendor_id in VENDOR_ORDER:
        binary_path = _resolved_binary_path(vendor_id)
        if not binary_path:
            vendors.append(_unavailable_vendor(vendor_id, ""))
            continue
        scanner = _SCANNERS[vendor_id]
        try:
            inventory = scanner(binary_path)
        except Exception as error:  # noqa: BLE001
            prior_vendor = prior_by_vendor.get(vendor_id)
            note = f"scan_error={type(error).__name__}"
            if prior_vendor is not None and prior_vendor.models:
                inventory = _cached_degraded_vendor(vendor_id, prior_vendor, binary_path=binary_path, note=note)
            else:
                inventory = _fallback_vendor(vendor_id, binary_path=binary_path, note=note)
        vendors.append(inventory)
    snapshot = CatalogSnapshot(
        schema_version=SCHEMA_VERSION,
        generated_at=_now_iso(),
        cache_path=str(cache_path),
        vendors=tuple(vendors),
    )
    _save_cached_snapshot(snapshot)
    return snapshot


def get_catalog_snapshot(*, force_refresh: bool = False) -> CatalogSnapshot:
    global _CATALOG_SNAPSHOT, _CATALOG_REFRESHED
    with _CATALOG_LOCK:
        if _CATALOG_SNAPSHOT is None:
            _CATALOG_SNAPSHOT = _load_cached_snapshot()
        if force_refresh or not _CATALOG_REFRESHED:
            _CATALOG_SNAPSHOT = refresh_catalog_snapshot(prior_snapshot=_CATALOG_SNAPSHOT)
            _CATALOG_REFRESHED = True
        if _CATALOG_SNAPSHOT is None:
            _CATALOG_SNAPSHOT = CatalogSnapshot(
                schema_version=SCHEMA_VERSION,
                generated_at=_now_iso(),
                cache_path=str(catalog_cache_path()),
                vendors=tuple(_unavailable_vendor(vendor_id, "") for vendor_id in VENDOR_ORDER),
            )
        return _CATALOG_SNAPSHOT


def get_vendor_inventory(vendor_id: str, *, catalog: CatalogSnapshot | None = None) -> VendorInventory:
    snapshot = catalog or get_catalog_snapshot()
    return snapshot.vendor(vendor_id)


def get_default_model_for_vendor(vendor_id: str, *, catalog: CatalogSnapshot | None = None) -> str:
    inventory = get_vendor_inventory(vendor_id, catalog=catalog)
    if inventory.default_model:
        return inventory.default_model
    normalized_vendor = normalize_vendor_id(vendor_id)
    return LEGACY_DEFAULT_MODEL_BY_VENDOR[normalized_vendor]


def get_model_choices(vendor_id: str, *, catalog: CatalogSnapshot | None = None) -> tuple[ModelInventory, ...]:
    inventory = get_vendor_inventory(vendor_id, catalog=catalog)
    return inventory.models


def _map_native_effort(vendor_id: str, normalized_effort: str, native_levels: Sequence[str]) -> str:
    normalized_effort = normalize_effort(normalized_effort)
    available = [level for level in NATIVE_REASONING_ORDER if level in native_levels]
    if not available:
        return ""
    if vendor_id == "codex":
        mapping = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}
        return mapping.get(normalized_effort, available[-1] if available else "")
    if vendor_id == "claude":
        mapping = {"low": "low", "medium": "medium", "high": "high", "xhigh": "max", "max": "max"}
        return mapping.get(normalized_effort, available[-1] if available else "")
    if normalized_effort in native_levels:
        return normalized_effort
    if normalized_effort == "low" and "minimal" in native_levels:
        return "minimal"
    target_rank = NORMALIZED_EFFORT_LEVELS.index(normalized_effort)
    ranked = sorted((NATIVE_REASONING_ORDER.index(level), level) for level in available)
    closest = min(ranked, key=lambda item: (abs(item[0] - target_rank), -item[0]))
    return closest[1]


def _resolve_model_choice(vendor_id: str, requested_model: str, inventory: VendorInventory) -> ModelInventory:
    model_text = str(requested_model or "").strip()
    if not model_text:
        model_text = inventory.default_model or LEGACY_DEFAULT_MODEL_BY_VENDOR[vendor_id]
    if model_text == "default":
        model_text = inventory.default_model or ""
    alias_target = LEGACY_MODEL_ALIASES_BY_VENDOR.get(vendor_id, {}).get(model_text, "")
    if alias_target:
        model_text = alias_target
    if not inventory.installed:
        raise ValueError(f"{vendor_id} is not installed on this machine")
    model = inventory.find_model(model_text)
    if model is not None:
        return model
    available = ", ".join(inventory.model_ids()[:12])
    raise ValueError(f"{vendor_id} model unavailable in scanned catalog: {model_text}; available={available or 'none'}")


def get_normalized_effort_choices(vendor_id: str, model_id: str, *, catalog: CatalogSnapshot | None = None) -> tuple[str, ...]:
    inventory = get_vendor_inventory(vendor_id, catalog=catalog)
    model = _resolve_model_choice(normalize_vendor_id(vendor_id), model_id, inventory)
    levels = model.reasoning.normalized_reasoning_levels
    return levels or ("high",)


def resolve_launch(
    vendor_id: str,
    requested_model: str,
    requested_effort: str,
    *,
    catalog: CatalogSnapshot | None = None,
) -> LaunchResolution:
    normalized_vendor = normalize_vendor_id(vendor_id)
    snapshot = catalog or get_catalog_snapshot()
    inventory = snapshot.vendor(normalized_vendor)
    model = _resolve_model_choice(normalized_vendor, requested_model, inventory)
    normalized_effort = normalize_effort(requested_effort or model.reasoning.default_normalized_effort)
    allowed_efforts = model.reasoning.normalized_reasoning_levels or ("high",)
    if normalized_effort not in allowed_efforts:
        available = "/".join(allowed_efforts)
        raise ValueError(f"{model.model_id} does not support normalized effort {normalized_effort}; allowed={available}")

    resolved_model = model.model_id
    native_reasoning_level = ""
    resolved_variant = ""
    notes = list(model.notes) + list(model.reasoning.notes)
    mode = model.reasoning.reasoning_control_mode

    if mode == REASONING_NATIVE:
        native_reasoning_level = _map_native_effort(normalized_vendor, normalized_effort, model.reasoning.native_reasoning_levels)
    elif mode == REASONING_MAPPED:
        resolved_variant = _map_native_effort(normalized_vendor, normalized_effort, model.reasoning.native_reasoning_levels)
        native_reasoning_level = resolved_variant
    elif mode == REASONING_MODEL_FAMILY_ROUTING:
        if model.model_id == "auto":
            resolved_model = "flash" if normalized_effort in {"low", "medium"} else "pro"
            notes.append(f"auto_family_resolved={resolved_model}")
        else:
            resolved_model = model.model_id
    elif mode in {REASONING_IMPLICIT_DEFAULT, REASONING_UNSUPPORTED}:
        native_reasoning_level = ""

    return LaunchResolution(
        vendor_id=normalized_vendor,
        requested_model=str(requested_model or "").strip(),
        resolved_model=resolved_model,
        requested_effort=str(requested_effort or "").strip(),
        normalized_effort=normalized_effort,
        native_reasoning_level=native_reasoning_level,
        resolved_variant=resolved_variant,
        reasoning_control_mode=mode,
        supports_reasoning=model.reasoning.supports_reasoning,
        catalog_source_kind=model.source_kind or inventory.source_kind,
        confidence=model.confidence or inventory.confidence,
        notes=tuple(dict.fromkeys(item for item in notes if item)),
    )
