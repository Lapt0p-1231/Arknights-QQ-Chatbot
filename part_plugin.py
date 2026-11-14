import asyncio
import html
import importlib.util
import inspect
import io
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Dict, List, Optional

PLUGIN_API_VERSION = 1
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.environ.get("QQ_BOT_PLUGIN_DIR", os.path.join(BASE_DIR, "plugins"))
DATA_DIR = os.environ.get("QQ_BOT_DATA_DIR", os.path.join(BASE_DIR, "data"))
CONFIG_FILE = os.path.join(DATA_DIR, "plugins_config.json")
RUNTIME_FILE = os.path.join(DATA_DIR, "plugins_runtime.json")
RELOAD_FILE = os.path.join(DATA_DIR, "plugins_reload.json")
PLUGIN_DATA_DIR = os.path.join(DATA_DIR, "plugin_data")
MAX_ZIP_BYTES = 4 * 1024 * 1024
MAX_EXPANDED_BYTES = 12 * 1024 * 1024
MAX_ZIP_FILES = 128
SUPPORTED_HOOKS = {"on_message", "on_prompt", "on_reply", "after_reply"}
_BACKEND_CACHE = {}
_BACKEND_LOCK = None
_current_manager = None

_logger: Optional[Callable[[str], None]] = None


def set_logger(log_func: Callable[[str], None]) -> None:
    global _logger
    _logger = log_func


def _log(text: str) -> None:
    if _logger:
        try:
            _logger(text)
            return
        except Exception:
            pass
    print(text)


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def _load_overrides(path: str = CONFIG_FILE) -> Dict[str, Dict[str, Any]]:
    data = _read_json(path, {})
    if not isinstance(data, dict):
        return {}
    return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}


def _valid_plugin_id(value: Any) -> bool:
    return bool(re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", str(value or "")))


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _iter_manifests(plugin_dir: str = PLUGIN_DIR):
    if not os.path.isdir(plugin_dir):
        return
    for folder in sorted(os.listdir(plugin_dir)):
        root = os.path.join(plugin_dir, folder)
        manifest_path = os.path.join(root, "manifest.json")
        if not os.path.isdir(root) or not os.path.isfile(manifest_path):
            continue
        manifest = _read_json(manifest_path, None)
        if not isinstance(manifest, dict):
            yield folder, {}, "manifest.json 格式错误"
            continue
        plugin_id = str(manifest.get("id") or folder)
        manifest = dict(manifest)
        manifest["id"] = plugin_id
        manifest["_folder"] = folder
        if not _valid_plugin_id(plugin_id):
            yield folder, manifest, "插件 id 格式错误"
            continue
        api_version = manifest.get("api_version")
        if not isinstance(api_version, (int, float, str)) or _as_int(api_version, -1) != PLUGIN_API_VERSION:
            yield folder, manifest, f"需要 api_version={PLUGIN_API_VERSION}"
            continue
        yield folder, manifest, ""


def inspect_plugins(plugin_dir: str = PLUGIN_DIR) -> List[Dict[str, Any]]:
    overrides = _load_overrides()
    runtime = _read_json(RUNTIME_FILE, {})
    runtime_items = runtime.get("plugins", {}) if isinstance(runtime, dict) else {}
    result = []
    for folder, manifest, manifest_error in _iter_manifests(plugin_dir) or []:
        plugin_id = str(manifest.get("id") or folder)
        root = Path(plugin_dir) / folder
        override = overrides.get(plugin_id, {})
        enabled = bool(override.get("enabled", manifest.get("enabled", False)))
        state = runtime_items.get(plugin_id, {}) if isinstance(runtime_items, dict) else {}
        panel = _panel_file(root, manifest)
        stylesheet = str(manifest.get("stylesheet") or "")
        style_rel = _safe_relative(stylesheet)
        if not style_rel or not (root / Path(*style_rel.parts)).is_file():
            stylesheet = ""
        raw_config = manifest.get("config")
        config = dict(raw_config) if isinstance(raw_config, dict) else {}
        override_config = override.get("config")
        if isinstance(override_config, dict):
            config.update(override_config)
        raw_permissions = manifest.get("permissions")
        if isinstance(raw_permissions, str):
            permissions = [raw_permissions]
        elif isinstance(raw_permissions, (list, tuple, set)):
            permissions = [str(item) for item in raw_permissions]
        else:
            permissions = []
        result.append({
            "id": plugin_id,
            "folder": folder,
            "type": str(manifest.get("type") or "plugin"),
            "name": str(manifest.get("name") or plugin_id),
            "version": str(manifest.get("version") or "0.0.0"),
            "api_version": _as_int(manifest.get("api_version"), 0),
            "description": str(manifest.get("description") or ""),
            "author": str(manifest.get("author") or ""),
            "enabled": enabled,
            "loaded": bool(state.get("loaded", False)),
            "error": manifest_error or str(state.get("error") or ""),
            "commands": list(state.get("commands") or []) if isinstance(state.get("commands"), list) else [],
            "hooks": list(state.get("hooks") or []) if isinstance(state.get("hooks"), list) else [],
            "permissions": permissions,
            "priority": _as_int(manifest.get("priority"), 100),
            "config": config,
            "panel": panel,
            "has_panel": bool(panel),
            "stylesheet": stylesheet,
            "has_stylesheet": bool(stylesheet),
            "revision": _plugin_revision(root, manifest, panel),
        })
    return result


def _safe_relative(value: Any) -> Optional[PurePosixPath]:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    rel = PurePosixPath(value)
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        return None
    return rel


def _panel_file(root: Path, manifest: Dict[str, Any]) -> str:
    ui = manifest.get("ui")
    panel = ""
    if isinstance(ui, dict):
        panel = str(ui.get("panel") or ui.get("entry") or "")
    if not panel:
        panel = str(manifest.get("panel") or "")
    if not panel and (root / "index.html").is_file():
        panel = "index.html"
    rel = _safe_relative(panel)
    if not rel or not (root / Path(*rel.parts)).is_file():
        return ""
    return str(rel)


def _plugin_revision(root: Path, manifest: Dict[str, Any], panel: str) -> str:
    values = [str(manifest.get("id") or "")]
    for name in ("manifest.json", "plugin.py", "main.py", panel, str(manifest.get("stylesheet") or "")):
        rel = _safe_relative(name)
        if not rel:
            continue
        path = root / Path(*rel.parts)
        try:
            stat = path.stat()
            values.append(f"{rel}:{stat.st_mtime_ns}:{stat.st_size}")
        except OSError:
            pass
    return str(abs(hash("|".join(values))))


def _plugin_root(plugin_id: str, plugin_dir: str = PLUGIN_DIR) -> Optional[Path]:
    if not _valid_plugin_id(plugin_id):
        return None
    for folder, manifest, _ in _iter_manifests(plugin_dir) or []:
        if str(manifest.get("id") or folder) == plugin_id:
            return Path(plugin_dir) / folder
    return None


def is_plugin_enabled(plugin_id: str) -> bool:
    root = _plugin_root(plugin_id)
    if root is None:
        return False
    manifest = _read_json(str(root / "manifest.json"), {})
    if not isinstance(manifest, dict):
        return False
    override = _load_overrides().get(plugin_id, {})
    return bool(override.get("enabled", manifest.get("enabled", False)))


def plugin_panel(plugin_id: str) -> Dict[str, Any]:
    root = _plugin_root(plugin_id)
    if root is None:
        return {}
    manifest = _read_json(str(root / "manifest.json"), {})
    if not isinstance(manifest, dict):
        return {}
    panel = _panel_file(root, manifest)
    stylesheet = str(manifest.get("stylesheet") or "")
    style_rel = _safe_relative(stylesheet)
    if not style_rel or not (root / Path(*style_rel.parts)).is_file():
        stylesheet = ""
    return {
        "id": plugin_id,
        "root": root,
        "manifest": manifest,
        "panel": panel,
        "stylesheet": stylesheet,
        "revision": _plugin_revision(root, manifest, panel),
    }


def plugin_asset_file(plugin_id: str, relative: str) -> Optional[Path]:
    info = plugin_panel(plugin_id)
    if not info:
        return None
    rel = _safe_relative(relative)
    if not rel:
        return None
    path = info["root"] / Path(*rel.parts)
    if not path.is_file():
        return None
    try:
        if not path.resolve().is_relative_to(info["root"].resolve()):
            return None
    except Exception:
        return None
    return path


def _slug(value: str, fallback: str = "plugin") -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower()).strip("-")
    if not slug or not slug[0].isalpha():
        slug = f"{fallback}-{slug}".strip("-")
    if not _valid_plugin_id(slug):
        slug = f"{fallback}-{uuid.uuid4().hex[:8]}"
    return slug


def _normalize_import_manifest(raw: Dict[str, Any], fallback_name: str) -> Dict[str, Any]:
    manifest = dict(raw)
    manifest["id"] = str(manifest.get("id") or _slug(fallback_name))
    if not _valid_plugin_id(manifest["id"]):
        manifest["id"] = _slug(fallback_name)
    manifest["name"] = str(manifest.get("name") or fallback_name or manifest["id"])
    manifest["version"] = str(manifest.get("version") or "0.0.0")
    manifest["api_version"] = _as_int(manifest.get("api_version"), 1)
    if not isinstance(manifest.get("permissions"), list):
        manifest["permissions"] = []
    if not isinstance(manifest.get("config"), dict):
        manifest["config"] = {}
    return manifest


def _zip_entries(data: bytes):
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError("ZIP 超过 4 MiB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("不是有效的 ZIP 文件")
    entries = []
    total = 0
    seen = set()
    for info in archive.infolist():
        name = info.filename
        if info.is_dir():
            continue
        if name.startswith("__MACOSX/") or name.endswith(".DS_Store"):
            continue
        rel = _safe_relative(name)
        if rel is None:
            raise ValueError("ZIP 包含非法路径")
        if str(rel) in seen:
            raise ValueError("ZIP 包含重复文件路径")
        seen.add(str(rel))
        if info.flag_bits & 0x1:
            raise ValueError("不支持加密 ZIP")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise ValueError("ZIP 不能包含符号链接")
        total += info.file_size
        if total > MAX_EXPANDED_BYTES:
            raise ValueError("ZIP 解压后超过 12 MiB")
        if info.file_size > 2 * 1024 * 1024:
            raise ValueError("ZIP 单文件超过 2 MiB")
        entries.append((str(rel), info))
        if len(entries) > MAX_ZIP_FILES:
            raise ValueError("ZIP 文件数量超过 128")
    return archive, entries


def install_plugin_zip(data: bytes, filename: str = "", name: str = "") -> Dict[str, Any]:
    archive, entries = _zip_entries(data)
    names = {name for name, _ in entries}
    base = ""
    if "manifest.json" not in names and "plugin.json" not in names:
        tops = {name.split("/", 1)[0] for name, _ in entries if "/" in name}
        for top in sorted(tops):
            if f"{top}/manifest.json" in names or f"{top}/plugin.json" in names:
                base = top + "/"
                break
        if not base:
            for top in sorted(tops):
                if f"{top}/index.html" in names:
                    base = top + "/"
                    break
    selected = []
    for rel, info in entries:
        if base:
            if not rel.startswith(base):
                continue
            rel = rel[len(base):]
        if not rel:
            continue
        selected.append((rel, info))
    if not selected:
        raise ValueError("ZIP 中没有可安装文件")
    raw_manifest = None
    for target in ("manifest.json", "plugin.json"):
        if target in {rel for rel, _ in selected}:
            try:
                raw_manifest = json.loads(archive.read(dict((r, i) for r, i in selected)[target]).decode("utf-8"))
            except Exception:
                raise ValueError("插件清单 JSON 无效")
            break
    if not isinstance(raw_manifest, dict):
        raw_manifest = {
            "id": _slug(name or Path(filename or "plugin").stem),
            "name": name or Path(filename or "plugin").stem or "插件",
            "version": "0.0.0",
            "api_version": 1,
        }
    manifest = _normalize_import_manifest(raw_manifest, name or Path(filename or "plugin").stem)
    if any(rel == "index.html" for rel, _ in selected):
        ui = manifest.setdefault("ui", {})
        if isinstance(ui, dict):
            ui.setdefault("panel", "index.html")
    target_dir = Path(PLUGIN_DIR) / manifest["id"]
    if target_dir.exists():
        raise ValueError(f"插件已存在: {manifest['id']}")
    temp_dir = Path(PLUGIN_DIR) / f".install-{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    try:
        for rel, info in selected:
            path = temp_dir / Path(*PurePosixPath(rel).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        manifest_path = temp_dir / "manifest.json"
        package_manifest = manifest
        if not manifest_path.exists():
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            current = _read_json(str(manifest_path), {})
            if not isinstance(current, dict):
                raise ValueError("插件清单 JSON 无效")
            current = _normalize_import_manifest(current, name or filename)
            current["id"] = manifest["id"]
            if (temp_dir / "index.html").is_file():
                current.setdefault("ui", {}).setdefault("panel", "index.html")
            manifest_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
            package_manifest = current
        style_rel = _safe_relative(package_manifest.get("stylesheet"))
        has_theme_style = (
            str(package_manifest.get("type") or "").lower() == "theme"
            and style_rel is not None
            and (temp_dir / Path(*style_rel.parts)).is_file()
        )
        if not (temp_dir / "plugin.py").is_file() and not (temp_dir / "index.html").is_file() and not has_theme_style:
            raise ValueError("插件至少需要 plugin.py、index.html 或主题 stylesheet")
        os.replace(temp_dir, target_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    overrides = _load_overrides()
    overrides.setdefault(manifest["id"], {})["enabled"] = False
    _write_json(CONFIG_FILE, overrides)
    request_plugin_reload()
    return {
        "id": manifest["id"],
        "name": manifest.get("name") or manifest["id"],
        "enabled": False,
        "has_panel": (target_dir / "index.html").is_file(),
    }


def create_plugin_template(name: str, plugin_id: str = "") -> Dict[str, Any]:
    if not str(name or "").strip():
        raise ValueError("插件名称不能为空")
    pid = plugin_id.strip() if plugin_id.strip() else _slug(name)
    if not _valid_plugin_id(pid):
        raise ValueError("插件 ID 只能使用字母开头的小写字母、数字和连字符")
    target = Path(PLUGIN_DIR) / pid
    if target.exists():
        raise ValueError(f"插件已存在: {pid}")
    target.mkdir(parents=True)
    display = html.escape(str(name))
    manifest = {
        "id": pid,
        "name": str(name),
        "version": "0.1.0",
        "api_version": PLUGIN_API_VERSION,
        "enabled": False,
        "permissions": [],
        "config": {},
        "ui": {"panel": "index.html"},
    }
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (target / "plugin.py").write_text("def setup(ctx):\n    pass\n", encoding="utf-8")
    (target / "main.py").write_text(
        "def handle(method, data, ctx):\n"
        "    return {\"ok\": True, \"method\": method, \"plugin\": ctx.get(\"id\")}\n",
        encoding="utf-8",
    )
    (target / "index.html").write_text(
        "<!doctype html>\n<html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<script src=\"/plugin-bridge.js\"></script></head><body>"
        f"<h2>{display}</h2><p>面板已加载。</p><pre id=\"out\"></pre>"
        "<script>ArkPlugin.onReady(function(cfg){"
        "ArkPlugin.request('ping',{hello:'world'}).then(function(r){"
        "document.getElementById('out').textContent=JSON.stringify(r,null,2);});"
        "ArkPlugin.setHeight(260);});</script></body></html>\n",
        encoding="utf-8",
    )
    overrides = _load_overrides()
    overrides[pid] = {"enabled": False}
    _write_json(CONFIG_FILE, overrides)
    request_plugin_reload()
    return {"id": pid, "name": str(name), "enabled": False, "has_panel": True}


def delete_plugin(plugin_id: str) -> None:
    root = _plugin_root(plugin_id)
    if root is None:
        raise ValueError(f"插件不存在: {plugin_id}")
    if not root.is_dir() or root.parent.resolve() != Path(PLUGIN_DIR).resolve():
        raise ValueError("插件路径无效")
    shutil.rmtree(root)
    _BACKEND_CACHE.pop(plugin_id, None)
    overrides = _load_overrides()
    overrides.pop(plugin_id, None)
    _write_json(CONFIG_FILE, overrides)
    request_plugin_reload()


def _load_backend(plugin_id: str):
    info = plugin_panel(plugin_id)
    if not info:
        return None
    path = info["root"] / "main.py"
    if not path.is_file():
        return None
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    cached = _BACKEND_CACHE.get(plugin_id)
    if cached and cached[0] == mtime:
        return cached[1]
    if cached and cached[1]:
        old_mod = cached[1]
        for mod_k, mod_v in list(sys.modules.items()):
            if mod_v is old_mod:
                sys.modules.pop(mod_k, None)
    module_name = f"_arkbot_panel_{plugin_id}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name, str(path), submodule_search_locations=[str(path.parent)]
    )
    if spec is None or spec.loader is None:
        raise ImportError("无法加载插件后端")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    _BACKEND_CACHE[plugin_id] = (mtime, module)
    return module


async def call_plugin_backend(plugin_id: str, method: str, data: Any) -> Any:
    if not is_plugin_enabled(plugin_id):
        raise PermissionError("插件未启用")
    module = await asyncio.to_thread(_load_backend, plugin_id)
    if module is None:
        return None
    ctx = {
        "id": plugin_id,
        "data_dir": os.path.join(PLUGIN_DATA_DIR, plugin_id),
        "config": plugin_panel(plugin_id).get("manifest", {}).get("config", {}),
    }
    os.makedirs(ctx["data_dir"], exist_ok=True)
    handler = getattr(module, "handle", None)
    if callable(handler):
        args = (method, data, ctx)
    else:
        handlers = getattr(module, "HANDLERS", None)
        if not isinstance(handlers, dict) or not callable(handlers.get(method)):
            raise ValueError(f"插件没有后端方法: {method}")
        handler = handlers[method]
        params = list(inspect.signature(handler).parameters)
        args = (data, ctx) if len(params) >= 2 else (data,)
    if inspect.iscoroutinefunction(handler):
        return await asyncio.wait_for(handler(*args), timeout=15)
    result = await asyncio.wait_for(asyncio.to_thread(handler, *args), timeout=15)
    if inspect.isawaitable(result):
        return await asyncio.wait_for(result, timeout=15)
    return result


def set_plugin_enabled(plugin_id: str, enabled: bool) -> None:
    items = inspect_plugins()
    target = next((item for item in items if item["id"] == plugin_id), None)
    if target is None:
        raise ValueError(f"插件不存在: {plugin_id}")
    overrides = _load_overrides()
    if enabled and target.get("type") == "theme":
        for item in items:
            if item.get("type") == "theme" and item["id"] != plugin_id and item.get("enabled"):
                overrides.setdefault(item["id"], {})["enabled"] = False
    item = overrides.setdefault(plugin_id, {})
    item["enabled"] = bool(enabled)
    _write_json(CONFIG_FILE, overrides)
    request_plugin_reload()


def request_plugin_reload() -> None:
    _write_json(RELOAD_FILE, {"requested_at": time.time()})


@dataclass
class CommandSpec:
    name: str
    handler: Callable[..., Any]
    admin_only: bool = False
    group_allowed: bool = True
    private_allowed: bool = True
    timeout: float = 15.0
    description: str = ""


@dataclass
class HookSpec:
    event: str
    handler: Callable[..., Any]
    priority: int = 100
    timeout: float = 5.0


@dataclass
class PluginRecord:
    manifest: Dict[str, Any]
    enabled: bool = False
    loaded: bool = False
    error: str = ""
    commands: Dict[str, CommandSpec] = field(default_factory=dict)
    hooks: Dict[str, List[HookSpec]] = field(default_factory=dict)
    module_name: str = ""
    module: Any = None

    @property
    def plugin_id(self) -> str:
        return str(self.manifest.get("id") or "")

    @property
    def permissions(self) -> set:
        values = self.manifest.get("permissions") or []
        if isinstance(values, str):
            return {values}
        return set(values) if isinstance(values, (list, tuple, set)) else set()


class PluginLogger:
    def __init__(self, plugin_id: str):
        self.plugin_id = plugin_id

    def info(self, text: Any) -> None:
        _log(f"[插件:{self.plugin_id}] {text}")

    def warning(self, text: Any) -> None:
        _log(f"[插件:{self.plugin_id}] [警告] {text}")

    def error(self, text: Any) -> None:
        _log(f"[插件:{self.plugin_id}] [错误] {text}")


class PluginContext:
    def __init__(self, manager: "PluginManager", record: PluginRecord, event: Optional[Dict[str, Any]] = None):
        self._manager = manager
        self._record = record
        self.event = event
        self.plugin_id = record.plugin_id
        self.logger = PluginLogger(record.plugin_id)
        self.data_dir = os.path.join(PLUGIN_DATA_DIR, record.plugin_id)
        os.makedirs(self.data_dir, exist_ok=True)

    @property
    def config(self) -> Dict[str, Any]:
        base = dict(self._record.manifest.get("config") or {})
        override = _load_overrides(self._manager.config_file).get(self.plugin_id, {})
        base.update(override.get("config") or {})
        return base

    @property
    def settings(self) -> Dict[str, Any]:
        self._manager.require(self._record, "settings.read")
        if not self._manager.settings_provider:
            return {}
        return dict(self._manager.settings_provider())

    def register_command(
        self,
        name: str,
        handler: Callable[..., Any],
        admin_only: bool = False,
        group_allowed: bool = True,
        private_allowed: bool = True,
        timeout: float = 15.0,
        description: str = "",
        **kwargs,
    ) -> None:
        if not description and "desc" in kwargs:
            description = str(kwargs["desc"])
        key = str(name or "").strip().lstrip("/").casefold()
        if not key or any(ch.isspace() for ch in key):
            raise ValueError("指令名不能为空或包含空白字符")
        if not callable(handler):
            raise TypeError("指令 handler 必须可调用")
        if key in self._record.commands:
            raise ValueError(f"插件内重复注册指令: /{key}")
        self._record.commands[key] = CommandSpec(
            key, handler, bool(admin_only), bool(group_allowed),
            bool(private_allowed), max(1.0, min(120.0, float(timeout))),
            str(description or "").strip(),
        )

    def on(self, event: str, handler: Callable[..., Any], priority: int = 100, timeout: float = 5.0) -> None:
        if event not in SUPPORTED_HOOKS:
            raise ValueError(f"不支持的钩子: {event}")
        if not callable(handler):
            raise TypeError("钩子 handler 必须可调用")
        self._record.hooks.setdefault(event, []).append(
            HookSpec(event, handler, int(priority), max(0.5, min(30.0, float(timeout))))
        )

    async def reply(self, text: str, event: Optional[Dict[str, Any]] = None) -> None:
        await self._manager.reply_from_plugin(self._record, event or self.event, text)

    async def send_private(self, user_id: Any, text: str) -> None:
        self._manager.require(self._record, "send_message")
        await self._manager.send_private(user_id, text)

    async def send_group(self, group_id: Any, text: str) -> None:
        self._manager.require(self._record, "send_message")
        await self._manager.send_group(group_id, text)

    async def call_model(self, role: str, messages: List[Dict[str, Any]]) -> str:
        self._manager.require(self._record, "model")
        if not self._manager.model_caller:
            raise RuntimeError("模型服务未配置")
        return await self._manager.model_caller(role, messages)


class PluginManager:
    def __init__(
        self,
        plugin_dir: str = PLUGIN_DIR,
        config_file: str = CONFIG_FILE,
        runtime_file: str = RUNTIME_FILE,
        reload_file: str = RELOAD_FILE,
    ):
        global _current_manager
        self.plugin_dir = plugin_dir
        self.config_file = config_file
        self.runtime_file = runtime_file
        self.reload_file = reload_file
        self.records: Dict[str, PluginRecord] = {}
        self.commands: Dict[str, tuple] = {}
        self.hooks: Dict[str, List[tuple]] = {name: [] for name in SUPPORTED_HOOKS}
        self.send_private_callback: Optional[Callable[..., Awaitable[Any]]] = None
        self.send_group_callback: Optional[Callable[..., Awaitable[Any]]] = None
        self.model_caller: Optional[Callable[..., Awaitable[str]]] = None
        self.settings_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self._signature = None
        self._last_check = 0.0
        self._reload_lock = asyncio.Lock()
        _current_manager = self

    def configure(
        self,
        send_private: Optional[Callable[..., Awaitable[Any]]] = None,
        send_group: Optional[Callable[..., Awaitable[Any]]] = None,
        model_caller: Optional[Callable[..., Awaitable[str]]] = None,
        settings_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        self.send_private_callback = send_private
        self.send_group_callback = send_group
        self.model_caller = model_caller
        self.settings_provider = settings_provider

    @staticmethod
    def require(record: PluginRecord, permission: str) -> None:
        if permission not in record.permissions:
            raise PermissionError(f"插件 {record.plugin_id} 未声明权限: {permission}")

    def _directory_signature(self):
        entries = []
        for folder, manifest, error in _iter_manifests(self.plugin_dir) or []:
            root = os.path.join(self.plugin_dir, folder)
            for filename in ("manifest.json", "plugin.py"):
                path = os.path.join(root, filename)
                try:
                    stat = os.stat(path)
                    entries.append((path, stat.st_mtime_ns, stat.st_size))
                except OSError:
                    entries.append((path, 0, 0))
        for path in (self.config_file, self.reload_file):
            try:
                stat = os.stat(path)
                entries.append((path, stat.st_mtime_ns, stat.st_size))
            except OSError:
                entries.append((path, 0, 0))
        return tuple(sorted(entries))

    async def _unload_one(self, record: PluginRecord) -> None:
        if record.module is not None:
            teardown = getattr(record.module, "teardown", None)
            if callable(teardown):
                try:
                    ctx = PluginContext(self, record)
                    await _invoke_handler(teardown, (ctx,), 5.0)
                except Exception as e:
                    _log(f"[插件] {record.plugin_id} teardown 异常: {e}")
            if record.module_name and record.module_name in sys.modules:
                sys.modules.pop(record.module_name, None)
            record.module = None
            record.module_name = ""
        record.loaded = False
        record.commands.clear()
        record.hooks.clear()

    async def reload(self) -> None:
        async with self._reload_lock:
            for old_record in list(self.records.values()):
                if old_record.loaded:
                    await self._unload_one(old_record)
            overrides = _load_overrides(self.config_file)
            records: Dict[str, PluginRecord] = {}
            for folder, manifest, manifest_error in _iter_manifests(self.plugin_dir) or []:
                plugin_id = str(manifest.get("id") or folder)
                override = overrides.get(plugin_id, {})
                enabled = bool(override.get("enabled", manifest.get("enabled", False)))
                record = PluginRecord(manifest=manifest, enabled=enabled, error=manifest_error)
                records[plugin_id] = record
                package_type = str(manifest.get("type") or "plugin")
                if enabled and not manifest_error and package_type == "plugin":
                    await self._load_one(record)
                elif enabled and not manifest_error and package_type == "theme":
                    record.loaded = True
                if record.error:
                    _log(f"[插件] {plugin_id} 加载失败: {record.error}")

            commands: Dict[str, tuple] = {}
            hooks: Dict[str, List[tuple]] = {name: [] for name in SUPPORTED_HOOKS}
            ordered = sorted(records.values(), key=lambda r: (int(r.manifest.get("priority", 100) or 100), r.plugin_id))
            for record in ordered:
                if not record.loaded:
                    continue
                for name, spec in record.commands.items():
                    if name in commands:
                        other = commands[name][0].plugin_id
                        record.error = f"指令 /{name} 与插件 {other} 冲突，已跳过"
                        _log(f"[插件] {record.plugin_id}: {record.error}")
                        continue
                    commands[name] = (record, spec)
                for event, specs in record.hooks.items():
                    hooks[event].extend((record, spec) for spec in specs)
            for event in hooks:
                hooks[event].sort(key=lambda pair: pair[1].priority)

            self.records = records
            self.commands = commands
            self.hooks = hooks
            self._signature = self._directory_signature()
            self._write_runtime()

    async def _load_one(self, record: PluginRecord) -> None:
        folder_name = str(record.manifest.get("_folder") or record.plugin_id)
        plugin_folder = os.path.join(self.plugin_dir, folder_name)
        path = os.path.join(plugin_folder, "plugin.py")
        if not os.path.isfile(path):
            record.error = "缺少 plugin.py"
            return
        module_name = f"_arkbot_plugin_{record.plugin_id}_{uuid.uuid4().hex}"
        try:
            spec = importlib.util.spec_from_file_location(
                module_name, path, submodule_search_locations=[plugin_folder]
            )
            if spec is None or spec.loader is None:
                raise ImportError("无法创建模块加载器")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            setup = getattr(module, "setup", None)
            if not callable(setup):
                raise TypeError("plugin.py 必须提供 setup(ctx)")
            result = setup(PluginContext(self, record))
            if inspect.isawaitable(result):
                await result
            record.module_name = module_name
            record.module = module
            record.loaded = True
        except Exception as e:
            record.loaded = False
            record.error = f"{type(e).__name__}: {e}"
            if module_name in sys.modules:
                sys.modules.pop(module_name, None)

    def get_commands_help(self, admin: bool = False, in_group: bool = False) -> List[str]:
        lines = []
        ordered = sorted(self.commands.items(), key=lambda x: x[0])
        for name, (record, spec) in ordered:
            if in_group and not spec.group_allowed:
                continue
            if not in_group and not spec.private_allowed:
                continue
            if spec.admin_only and not admin:
                continue
            desc = f" - {spec.description}" if spec.description else ""
            lines.append(f"/{name}{desc} [插件:{record.plugin_id}]")
        return lines

    def _write_runtime(self) -> None:
        data = {
            "updated_at": time.time(),
            "plugins": {
                plugin_id: {
                    "loaded": record.loaded,
                    "error": record.error,
                    "commands": sorted(record.commands),
                    "hooks": sorted(record.hooks),
                }
                for plugin_id, record in self.records.items()
            },
        }
        try:
            _write_json(self.runtime_file, data)
        except Exception as e:
            _log(f"[插件] 写入运行状态失败: {e}")

    async def reload_if_changed(self) -> bool:
        now = time.monotonic()
        if now - self._last_check < 2.0:
            return False
        self._last_check = now
        signature = self._directory_signature()
        if signature == self._signature:
            return False
        try:
            await self.reload()
            _log("[插件] 配置或文件变化，已重新加载")
            return True
        except Exception as e:
            _log(f"[插件] 热重载失败: {type(e).__name__}: {e}")
            return False

    async def send_private(self, user_id: Any, text: str) -> None:
        if not self.send_private_callback:
            raise RuntimeError("私聊发送服务未配置")
        await self.send_private_callback(user_id, text)

    async def send_group(self, group_id: Any, text: str) -> None:
        if not self.send_group_callback:
            raise RuntimeError("群聊发送服务未配置")
        await self.send_group_callback(group_id, text)

    async def reply_from_plugin(self, record: PluginRecord, event: Optional[Dict[str, Any]], text: Any) -> None:
        self.require(record, "send_message")
        if not event:
            raise ValueError("缺少回复目标 event")
        content = str(text or "").strip()
        if not content:
            return
        if event.get("message_type") == "group":
            await self.send_group(event.get("group_id"), content)
        else:
            await self.send_private(event.get("user_id"), content)

    async def _reply_system(self, event: Dict[str, Any], text: str) -> None:
        if event.get("message_type") == "group":
            await self.send_group(event.get("group_id"), text)
        else:
            await self.send_private(event.get("user_id"), text)

    async def handle_command(self, text: str, event: Dict[str, Any], admin: bool) -> bool:
        stripped = str(text or "").strip()
        if not stripped.startswith("/"):
            return False
        await self.reload_if_changed()
        parts = stripped[1:].split(maxsplit=1)
        name = parts[0].casefold() if parts and parts[0] else ""
        if name not in self.commands:
            return False
        record, spec = self.commands[name]
        in_group = event.get("message_type") == "group"
        if (in_group and not spec.group_allowed) or (not in_group and not spec.private_allowed):
            await self._reply_system(event, "[插件] 该指令不支持当前会话")
            return True
        if spec.admin_only and not admin:
            await self._reply_system(event, "[插件] 该指令仅管理员可用")
            return True

        args = parts[1] if len(parts) > 1 else ""
        ctx = PluginContext(self, record, event)
        try:
            result = await _invoke_handler(spec.handler, (args, event, ctx), spec.timeout)
        except asyncio.TimeoutError:
            _log(f"[插件] {record.plugin_id} 指令 /{name} 超时({spec.timeout:g}s)")
            await self._reply_system(event, f"[插件] /{name} 执行超时")
            return True
        except Exception as e:
            _log(f"[插件] {record.plugin_id} 指令 /{name} 异常: {type(e).__name__}: {e}")
            await self._reply_system(event, f"[插件] /{name} 执行失败: {type(e).__name__}")
            return True
        try:
            return await self._handle_command_result(record, event, result)
        except Exception as e:
            _log(f"[插件] {record.plugin_id} 返回结果处理失败: {type(e).__name__}: {e}")
            await self._reply_system(event, f"[插件] /{name} 无法发送结果: {type(e).__name__}")
            return True

    async def _handle_command_result(self, record: PluginRecord, event: Dict[str, Any], result: Any) -> bool:
        if result is None:
            return True
        if isinstance(result, str):
            result = {"action": "reply", "text": result}
        if not isinstance(result, dict):
            _log(f"[插件] {record.plugin_id} 返回了不支持的结果类型: {type(result).__name__}")
            return True
        action = str(result.get("action") or ("reply" if result.get("text") else "stop"))
        if action == "continue":
            return False
        if action in ("reply", "replace"):
            await self.reply_from_plugin(record, event, result.get("text") or "")
        elif action != "stop":
            _log(f"[插件] {record.plugin_id} 未知返回动作: {action}")
        return True

    async def apply_reply_hooks(
        self,
        text: str,
        user_id: str,
        group_id: Optional[Any] = None,
        message: str = "",
    ) -> Optional[str]:
        await self.reload_if_changed()
        current = text
        for record, spec in list(self.hooks.get("on_reply", [])):
            payload = {
                "text": current,
                "user_id": str(user_id),
                "group_id": str(group_id) if group_id is not None else None,
                "message": message,
            }
            ctx = PluginContext(self, record)
            try:
                result = await _invoke_handler(spec.handler, (payload, ctx), spec.timeout)
            except Exception as e:
                _log(f"[插件] {record.plugin_id} on_reply 异常，已跳过: {type(e).__name__}: {e}")
                continue
            if result is None:
                continue
            if isinstance(result, str):
                current = result
                continue
            if not isinstance(result, dict):
                continue
            action = str(result.get("action") or "")
            if action == "stop":
                return None
            if action == "replace":
                current = str(result.get("text") or "")
        return current


async def _invoke_handler(handler: Callable[..., Any], args: tuple, timeout: float) -> Any:
    if inspect.iscoroutinefunction(handler):
        return await asyncio.wait_for(handler(*args), timeout=timeout)
    result = await asyncio.wait_for(asyncio.to_thread(handler, *args), timeout=timeout)
    if inspect.isawaitable(result):
        return await asyncio.wait_for(result, timeout=timeout)
    return result


def get_active_commands_help(admin: bool = False, in_group: bool = False) -> List[str]:
    if _current_manager:
        return _current_manager.get_commands_help(admin=admin, in_group=in_group)
    return []
