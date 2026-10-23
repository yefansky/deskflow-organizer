"""Local stdio MCP bridge for AI-assisted DESKFLOW desktop organization."""
from __future__ import annotations
import argparse
import json
import logging
import re
import sys
import subprocess
import os
import time
from app_runtime import command, watch_update, RESOURCE_ROOT
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:" + str(int(os.environ.get("DESKFLOW_API_PORT", "8765")))
TOOLS = [
 {"name":"get_desktop_catalog","description":"高效读取桌面分类目录与项目摘要、标签、人工归类状态。优先使用此工具，不要为普通整理读取庞大的完整布局配置。可按分类或关键词过滤。", "inputSchema":{"type":"object","properties":{"category":{"type":"string","description":"可选分类 id"},"query":{"type":"string","description":"按名称、摘要或标签过滤"},"include_items":{"type":"boolean","default":True},"include_path":{"type":"boolean","default":False},"limit":{"type":"integer","minimum":1,"maximum":500,"default":300}}}},
 {"name":"inspect_desktop_items","description":"按文件名深查 1–50 个桌面项目，返回路径、快捷方式目标、用途摘要、标签和归类来源；文件夹额外返回有界的两层目录画像。同名项目会逐项返回并带 duplicate_name。refresh=true 会重新生成安全的本地摘要，但保留人工摘要。", "inputSchema":{"type":"object","properties":{"names":{"type":"array","items":{"type":"string"},"minItems":1,"maxItems":50},"refresh":{"type":"boolean","default":False}},"required":["names"]}},
 {"name":"organize_desktop_items","description":"一次原子调用批量归类并可同时写入每个项目的人工摘要和标签。只更新 DESKFLOW 虚拟分类与本地元数据，不移动、改名或删除真实文件；dry_run=true 仅返回计划。reset_category=true 恢复自动规则。", "inputSchema":{"type":"object","properties":{"operations":{"type":"array","minItems":1,"maxItems":200,"items":{"type":"object","properties":{"name":{"type":"string"},"category":{"type":"string","description":"分类 id；省略则只改元数据"},"reset_category":{"type":"boolean"},"summary":{"type":"string","maxLength":240,"description":"一句用途摘要；空字符串清除人工摘要"},"tags":{"type":"array","maxItems":12,"items":{"type":"string","maxLength":32},"description":"要写入、合并或移除的标签"},"tag_mode":{"type":"string","enum":["replace","merge","remove"],"default":"replace"}},"required":["name"]}},"dry_run":{"type":"boolean","default":False}},"required":["operations"]}},
 {"name":"manage_desktop_categories","description":"批量创建、重命名或删除分类块。删除仅允许空的自定义分类；dry_run=true 只预览。创建后返回可用于归类的 category id。", "inputSchema":{"type":"object","properties":{"actions":{"type":"array","minItems":1,"maxItems":30,"items":{"type":"object","properties":{"action":{"type":"string","enum":["create","rename","delete"]},"category":{"type":"string","description":"重命名或删除时的分类 id"},"title":{"type":"string","maxLength":40},"color":{"type":"string","pattern":"^#[0-9a-fA-F]{6}$"}},"required":["action"]}},"dry_run":{"type":"boolean","default":False}},"required":["actions"]}},
 {"name":"desktop_layout","description":"低层诊断：读取完整分类、项目和全部布局配置。普通 AI 整理应使用 get_desktop_catalog，避免浪费上下文。", "inputSchema":{"type":"object","properties":{}}},
 {"name":"rescan_desktop","description":"重新扫描桌面；不会移动、删除或改名文件。", "inputSchema":{"type":"object","properties":{}}},
 {"name":"assign_desktop_item","description":"将一个桌面项目人工固定到分类；此习惯不会被后续 AI 整理覆盖。分类可使用内置分类或 GUI 新建的自定义块。", "inputSchema":{"type":"object","properties":{"name":{"type":"string"},"category":{"type":"string"}},"required":["name","category"]}},
 {"name":"update_desktop_ui","description":"调整桌面整理器的显示参数。alpha 只改变玻璃底板，文字和图标保持完全不透明。", "inputSchema":{"type":"object","properties":{"font_size":{"type":"integer","minimum":8,"maximum":15},"icon_size":{"type":"integer","minimum":20,"maximum":34},"background":{"type":"string"},"alpha":{"type":"number","minimum":0.08,"maximum":0.70},"auto_contrast":{"type":"boolean"}}}},
 {"name":"update_desktop_panel_style","description":"单独调整一个分类块的玻璃底色和底板透明度；图标与文字不随之变淡。reset=true 恢复跟随全局外观。", "inputSchema":{"type":"object","properties":{"category":{"type":"string"},"background":{"type":"string","description":"#RRGGBB"},"alpha":{"type":"number","minimum":0.08,"maximum":0.70},"reset":{"type":"boolean"}},"required":["category"]}},
 {"name":"set_desktop_panel_mode","description":"切换一个分类块的列表、紧凑双列或卡片模式；只重绘该块。", "inputSchema":{"type":"object","properties":{"category":{"type":"string"},"mode":{"type":"string","enum":["list","compact","cards"]}},"required":["category","mode"]}},
 {"name":"set_desktop_sorting","description":"设置一个块内排序：smart 会按真实类型分组，custom 保留人工拖动顺序。", "inputSchema":{"type":"object","properties":{"category":{"type":"string"},"mode":{"type":"string","enum":["smart","name","modified","custom"]}},"required":["category","mode"]}},
 {"name":"reorder_desktop_item","description":"把块内项目拖放式地排到另一项之前，并保存为手动顺序。", "inputSchema":{"type":"object","properties":{"category":{"type":"string"},"name":{"type":"string"},"before":{"type":"string"}},"required":["category","name"]}},
 {"name":"show_native_desktop","description":"关闭整理覆盖层并恢复 Windows 原生桌面图标。", "inputSchema":{"type":"object","properties":{}}},
 {"name":"arrange_desktop_top_right","description":"将所有整理块放到所选屏幕的右上角两列布局。", "inputSchema":{"type":"object","properties":{}}},
 {"name":"move_desktop_panel","description":"移动一个分类块到所选屏幕的指定坐标；该位置会按当前分辨率保存。", "inputSchema":{"type":"object","properties":{"category":{"type":"string"},"x":{"type":"integer"},"y":{"type":"integer"}},"required":["category","x","y"]}},
 {"name":"open_desktop_manager","description":"打开 DESKFLOW 产品化管理器，可调整偏好、开机启动和生命周期。", "inputSchema":{"type":"object","properties":{}}},
 {"name":"install_desktop_organizer","description":"安装本机 DESKFLOW 入口并启动整理器；不会移动或删除桌面文件。", "inputSchema":{"type":"object","properties":{}}},
]
# Keep existing names and routing stable for previously configured clients.
INSTRUCTIONS = "先调用 get_desktop_catalog 看分类/摘要/manual；get_desktop_arrangement 感知块位置和排布，必要时 inspect_desktop_items 深查。organize_desktop_items 先 dry_run=true 预演再一次原子提交并复核；默认保护 manual=true，只有用户明确要求覆盖时才设置 allow_manual=true。分类只更新虚拟映射，不移动真实文件。"
PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
LOG = logging.getLogger("deskflow.mcp")
GUIDE_URI = "deskflow://guide/organizing"
RESOURCES = [{"uri": GUIDE_URI, "name": "deskflow-organizing-guide", "description": "接入后的分类、人工习惯、布局和预演工作流", "mimeType": "text/plain"}]
PROMPTS = [{"name": "organize-desktop", "description": "读取本机桌面分类与排布，保留人工习惯，先预演后批量整理。"}]
READ_ONLY = {"get_desktop_catalog", "desktop_layout", "get_desktop_arrangement"}
def close_object_schemas(schema):
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
        for child in schema.get("properties", {}).values():
            close_object_schemas(child)
    if "items" in schema:
        close_object_schemas(schema["items"])


for tool in TOOLS:
    close_object_schemas(tool["inputSchema"])
    tool["annotations"] = {"readOnlyHint": tool["name"] in READ_ONLY, "destructiveHint": tool["name"] not in READ_ONLY, "openWorldHint": False}
    if tool["name"] == "organize_desktop_items":
        tool["inputSchema"]["properties"]["allow_manual"] = {"type": "boolean", "default": False, "description": "仅用户明确授权覆盖人工分类时设true；默认拒绝跨分类/重置manual项目，整批不写入。"}
TOOLS.append({"name": "get_desktop_arrangement", "description": "只读感知分类块排布：返回保存的各分辨率布局、坐标、大小、列表/双列/卡片模式和排序策略，不返回文件路径和元数据。坐标是保存的配置，不保证等于当前窗口像素位置。分类和当前项目顺序用get_desktop_catalog。", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}})
TOOL_INDEX = {tool["name"]: tool for tool in TOOLS}
ROUTES = {"get_desktop_catalog": "/catalog", "inspect_desktop_items": "/inspect-items", "organize_desktop_items": "/organize-batch", "manage_desktop_categories": "/categories", "desktop_layout": "/layout", "get_desktop_arrangement": "/layout", "rescan_desktop": "/rescan", "assign_desktop_item": "/assign", "update_desktop_ui": "/settings", "update_desktop_panel_style": "/panel-style", "set_desktop_panel_mode": "/panel-mode", "set_desktop_sorting": "/set-sorting", "reorder_desktop_item": "/reorder-item", "show_native_desktop": "/show-native-icons", "arrange_desktop_top_right": "/arrange-top-right", "move_desktop_panel": "/move-panel"}


class RPCError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def validate(value, schema, location="arguments"):
    """Validate the bounded JSON Schema subset published by this bridge."""
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int}
    valid = (type(value) in (int, float)) if kind == "number" else (type(value) is types[kind] if kind in types else True)
    if not valid:
        raise RPCError(-32602, f"{location}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise RPCError(-32602, f"{location}: unsupported value")
    if kind == "object":
        for required in schema.get("required", []):
            if required not in value:
                raise RPCError(-32602, f"{location}.{required}: required")
        properties = schema.get("properties", {})
        for key, child in value.items():
            if key not in properties:
                raise RPCError(-32602, f"{location}.{key}: unknown argument")
            validate(child, properties[key], f"{location}.{key}")
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", float("inf")):
            raise RPCError(-32602, f"{location}: invalid item count")
        for i, child in enumerate(value):
            validate(child, schema.get("items", {}), f"{location}[{i}]")
    elif kind == "string":
        if len(value) > schema.get("maxLength", float("inf")) or ("pattern" in schema and not re.fullmatch(schema["pattern"], value)):
            raise RPCError(-32602, f"{location}: invalid string")
    elif kind in ("integer", "number"):
        if not schema.get("minimum", float("-inf")) <= value <= schema.get("maximum", float("inf")):
            raise RPCError(-32602, f"{location}: outside range")


def launch_manager(install=False):
    from diagnostics import logging_arguments
    subprocess.Popen(command("manager", "--install" if install else "--start", *logging_arguments()), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "message": "DESKFLOW 管理器已打开。"}


def request(path, payload=None):
    try:
        data = None if payload is None else json.dumps(payload).encode()
        req = Request(BASE + path, data=data, headers={"Content-Type": "application/json"}, method="POST" if data is not None else "GET")
        with urlopen(req, timeout=8) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail", str(exc))
        except (ValueError, AttributeError):
            detail = str(exc)
        raise RuntimeError(f"DESKFLOW 请求无效：{detail}") from exc
    except URLError as exc:
        raise RuntimeError("桌面整理器未运行：请调用 open_desktop_manager 或启动 DESKFLOW。") from exc


def arrangement(layout):
    config = layout.get("config", {})
    ui = config.get("ui", {})
    return {"coordinate_source": "saved_configuration", "target_monitor": ui.get("target_monitor", "primary"), "preferred_monitor": ui.get("preferred_monitor"), "layout_profiles": ui.get("layout_profiles", {}), "panel_positions": ui.get("panel_positions", {}), "panel_sizes": ui.get("panel_sizes", {}), "category_modes": config.get("category_modes", {}), "category_sorting": config.get("category_sorting", {}), "category_names": ui.get("category_names", {}), "item_orders": config.get("item_orders", {})}


def tool_result(value, error=False):
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": error}


def dispatch(method, params):
    if method == "initialize":
        requested = params.get("protocolVersion", PROTOCOLS[0])
        version_path = RESOURCE_ROOT / "VERSION"
        version = version_path.read_text(encoding="utf-8").strip() if version_path.is_file() else "0.3.0"
        return {"protocolVersion": requested if requested in PROTOCOLS else PROTOCOLS[-1], "capabilities": {"tools": {}, "resources": {}, "prompts": {}}, "serverInfo": {"name": "smart-desktop-organizer", "version": version}, "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "resources/list":
        return {"resources": RESOURCES}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "resources/read":
        if params.get("uri") != GUIDE_URI:
            raise RPCError(-32602, "Unknown resource URI")
        return {"contents": [{"uri": GUIDE_URI, "mimeType": "text/plain", "text": INSTRUCTIONS}]}
    if method == "prompts/list":
        return {"prompts": PROMPTS}
    if method == "prompts/get":
        if params.get("name") != "organize-desktop":
            raise RPCError(-32602, "Unknown prompt")
        return {"description": "DESKFLOW 整理工作流", "messages": [{"role": "user", "content": {"type": "text", "text": INSTRUCTIONS}}]}
    if method != "tools/call":
        raise RPCError(-32601, "Method not found: " + method)
    name = params.get("name")
    if not isinstance(name, str) or name not in TOOL_INDEX:
        raise RPCError(-32602, "Unknown tool")
    args = params.get("arguments", {})
    validate(args, TOOL_INDEX[name]["inputSchema"])
    started = time.perf_counter()
    LOG.debug("MCP tool started name=%s", name)
    try:
        if name in {"open_desktop_manager", "install_desktop_organizer"}:
            value = launch_manager(name == "install_desktop_organizer")
        else:
            value = request(ROUTES[name], None if name in {"desktop_layout", "get_desktop_arrangement"} else args)
            if name == "get_desktop_arrangement":
                value = arrangement(value)
        return tool_result(value)
    except Exception as exc:
        LOG.exception("MCP tool failed name=%s", name)
        return tool_result({"ok": False, "error": {"type": "tool_execution_error", "message": str(exc)}}, error=True)
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        LOG.log(logging.WARNING if elapsed > 50 else logging.DEBUG, "MCP tool completed name=%s elapsed_ms=%.1f", name, elapsed)


def process_line(line):
    try:
        msg = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except ValueError:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    # JSON-RPC notifications never receive a response, including unknown methods.
    if "id" not in msg:
        return None
    ident = msg["id"]
    if ident is None or type(ident) not in (str, int):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request id"}}
    body = {"jsonrpc": "2.0", "id": ident}
    try:
        params = msg.get("params", {})
        if not isinstance(params, dict):
            raise RPCError(-32602, "params must be an object")
        body["result"] = dispatch(msg["method"], params)
    except RPCError as exc:
        LOG.warning("MCP request rejected method=%s code=%s", msg["method"], exc.code)
        body["error"] = {"code": exc.code, "message": exc.message}
    except Exception:
        LOG.exception("MCP protocol processing failed method=%s", msg["method"])
        body["error"] = {"code": -32603, "message": "Internal error; inspect diagnostic logs"}
    return body


def main(argv=None):
    parser = argparse.ArgumentParser(description="DESKFLOW local stdio MCP server")
    parser.add_argument("--print-config", action="store_true", help="Print MCP JSON configuration using this executable or Python source entry")
    parser.add_argument("--log-level", choices=("debug", "info", "warn", "error"))
    options = parser.parse_args(argv)
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if options.print_config:
        entry = command("mcp")
        print(json.dumps({"mcpServers": {"deskflow": {"command": entry[0], "args": entry[1:]}}}, ensure_ascii=False, indent=2))
        return 0
    from diagnostics import configure_logging
    configure_logging(options.log_level, role="mcp")
    watch_update(lambda: os._exit(0))
    for line in sys.stdin:
        result = process_line(line)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
