"""Local stdio MCP bridge for AI-assisted DESKFLOW desktop organization."""
from __future__ import annotations
import json, sys, subprocess, os
from app_runtime import command, watch_update
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:" + str(int(os.environ.get("DESKFLOW_API_PORT", "8765")))
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
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
MANAGER = Path(__file__).with_name("deskflow_manager.py")
def launch_manager(install=False):
    subprocess.Popen(command("manager", "--install" if install else "--start"), creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    return {"ok":True,"message":"DESKFLOW 管理器已打开。"}
def request(path, payload=None):
    try:
        data = None if payload is None else json.dumps(payload).encode()
        req = Request(BASE+path, data=data, headers={"Content-Type":"application/json"}, method="POST" if data else "GET")
        with urlopen(req, timeout=8) as response: return json.loads(response.read())
    except HTTPError as exc:
        try: detail = json.loads(exc.read()).get("detail", str(exc))
        except Exception: detail = str(exc)
        raise RuntimeError(f"DESKFLOW 请求无效：{detail}") from exc
    except URLError as exc: raise RuntimeError("桌面整理器未运行：请调用 open_desktop_manager 或启动 DESKFLOW。") from exc
def reply(id, result=None, error=None):
    body={"jsonrpc":"2.0","id":id}
    if error: body["error"]={"code":-32000,"message":error}
    else: body["result"]=result
    print(json.dumps(body,ensure_ascii=False),flush=True)
watch_update(lambda: os._exit(0))
for line in sys.stdin:
    try:
        msg = {}
        msg=json.loads(line); method=msg.get("method"); ident=msg.get("id")
        if method=="initialize": reply(ident,{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"smart-desktop-organizer","version":"0.3.0"},"instructions":"桌面整理优先使用 get_desktop_catalog，再按需 inspect_desktop_items，最后用一次 organize_desktop_items 批量提交并复核；不要覆盖 manual=true 的人工习惯，除非用户明确要求。"})
        elif method=="tools/list": reply(ident,{"tools":TOOLS})
        elif method=="tools/call":
            name=msg["params"]["name"]; args=msg["params"].get("arguments",{})
            if name in {"open_desktop_manager","install_desktop_organizer"}: value=launch_manager(name=="install_desktop_organizer")
            else:
                mapping={"get_desktop_catalog":("/catalog",args),"inspect_desktop_items":("/inspect-items",args),"organize_desktop_items":("/organize-batch",args),"manage_desktop_categories":("/categories",args),"desktop_layout":("/layout",None),"rescan_desktop":("/rescan",{}),"assign_desktop_item":("/assign",args),"update_desktop_ui":("/settings",args),"update_desktop_panel_style":("/panel-style",args),"set_desktop_panel_mode":("/panel-mode",args),"set_desktop_sorting":("/set-sorting",args),"reorder_desktop_item":("/reorder-item",args),"show_native_desktop":("/show-native-icons",{}),"arrange_desktop_top_right":("/arrange-top-right",{}),"move_desktop_panel":("/move-panel",args)}
                path,payload=mapping[name]; value=request(path,payload)
            reply(ident,{"content":[{"type":"text","text":json.dumps(value,ensure_ascii=False,indent=2)}]})
        elif method == "ping": reply(ident,{})
        elif ident is not None: reply(ident,error="不支持的方法：" + str(method))
    except Exception as exc: reply(msg.get("id") if 'msg' in locals() else None,error=str(exc))
