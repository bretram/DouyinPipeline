#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音管线服务器 v2
功能：
  1. 手机浏览器访问 → 粘贴链接 → 提交
  2. 自动触发下载 + 自动剪辑
  3. 完成后页面直接提供视频下载
  4. 支持查看历史成品

用法：
  python scripts/linkserver.py
"""
import sys, io
# Windows CMD 兼容
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = sys.stdout
except: pass

import http.server, urllib.parse, json, os, subprocess, threading, time, re, logging, signal, ctypes
from http.server import ThreadingHTTPServer
from pathlib import Path
from logging.handlers import RotatingFileHandler

BASE = Path(os.environ.get("DOUYIN_PIPELINE_ROOT", r"F:\DouyinPipeline"))
LINKS = BASE / "links.txt"
OUTPUT = BASE / "output"
VIDEOS = BASE / "videos"
SCRIPTS = BASE / "scripts"
LOGS = BASE / "logs"
VENV_PY = BASE / ".venv" / "Scripts" / "python.exe"
PORT = 7890

# ── 日志（每次启动一个独立文件，任务日志单独记录）──
LOGS.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("linkserver")
logger.setLevel(logging.INFO)
server_log_file = LOGS / f"server_{time.strftime('%Y%m%d_%H%M%S')}.log"
fh = logging.FileHandler(server_log_file, encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(fh)
print(f"Log: {server_log_file}")

URL_RE = re.compile(r"https?://v\.douyin\.com/[A-Za-z0-9_-]+/?|https?://www\.douyin\.com/video/\d+")

def extract_urls(text: str):
    """从混合文本中提取所有抖音链接"""
    found = []
    for m in URL_RE.finditer(text):
        url = m.group(0).rstrip("/")
        if url not in found:
            found.append(url)
    return found

# ── cpolar 内网穿透（由本服务直接拉起并解析 stdout，不再依赖外部 bat / 日志文件）──
CPOLAR_EXE = BASE / "cpolar" / "cpolar.exe"
CPOLAR_LOG = LOGS / "cpolar.log"          # cpolar 日志（写入 logs/，日切 logs/cpolar.log.YYYYMMDD）
TUNNEL_URL = ""          # 公网地址
CANCEL_REQUESTED = False # 取消标志
CHILD_PIDS = []          # 子进程 PID（取消时精确杀掉下载/剪辑进程，不误杀服务器）
_cpolar_proc = None

def start_cpolar():
    """启动 cpolar 隧道并实时解析公网地址。

    cpolar 用 -log 写结构化日志（会旋转成 cpolar.log.YYYYMMDD），隧道 URL 就在其中，
    由 _read_cpolar 实时读取最新日志文件解析。
    服务器是 pythonw（无控制台），cpolar 必须拥有自己的控制台才能稳定运行，
    故用 CREATE_NEW_CONSOLE + 隐藏窗口（不弹窗）。
    """
    global _cpolar_proc, TUNNEL_URL
    if not CPOLAR_EXE.exists():
        logger.error("cpolar.exe 未找到，内网穿透不可用")
        return
    try:
        # CREATE_NEW_CONSOLE：给 cpolar 一个独立控制台。
        # 经验证：当 linkserver 由控制台父进程（cmd/双击 Start.bat / bash）拉起时，
        # cpolar 才能稳定常驻；若父进程无控制台（如某些后台启动方式），cpolar 会立即退出。
        si = subprocess.STARTUPINFO()
        si.dwFlags = subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        _cpolar_proc = subprocess.Popen(
            [str(CPOLAR_EXE), "http", str(PORT), "-region=cn", f"-log={CPOLAR_LOG}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            startupinfo=si,
        )
        threading.Thread(target=_read_cpolar, args=(_cpolar_proc,), daemon=True).start()
        logger.info("cpolar 已启动，等待公网地址...")
    except Exception as e:
        logger.error(f"启动 cpolar 失败: {e}")

def _read_cpolar(proc):
    """实时读取 cpolar 最新日志文件（cpolar.log 或 cpolar.log.YYYYMMDD），提取隧道 URL"""
    global TUNNEL_URL
    import re as _re, time as _t, glob as _glob
    last_pos = 0
    cur_file = None
    while True:
        try:
            candidates = _glob.glob(str(CPOLAR_LOG.parent / "cpolar.log*"))
            if candidates:
                newest = max(candidates, key=os.path.getmtime)
                if newest != cur_file:   # 日志发生了旋转，重新从头读
                    cur_file = newest
                    last_pos = 0
                with open(cur_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(last_pos)
                    data = f.read()
                    last_pos = f.tell()
                for line in data.splitlines():
                    m = _re.search(r'https?://[a-zA-Z0-9.-]+\.cpolar\.\w+', line)
                    if m and m.group() != TUNNEL_URL:
                        TUNNEL_URL = m.group()
                        logger.info(f"Tunnel URL 已获取: {TUNNEL_URL}")
        except Exception:
            pass
        if proc.poll() is not None:
            logger.warning("cpolar 进程已退出（隧道断开，重启服务可恢复）")
            break
        _t.sleep(2)

# ── 防睡眠 / 网络就绪 / 隧道健康检查 ──
# 解决“电脑熄屏服务断开”问题：
#   平衡电源计划下，显示器关闭(60min) 与 系统睡眠(60min) 同时触发，
#   睡眠会切断网络 → cpolar 隧道断开。以下三件套保证“熄屏不断服务”。
STOP_EVENT = threading.Event()   # 统一停机信号，供所有守护线程优雅退出

def _keep_awake_loop():
    """周期性调用 SetThreadExecutionState，阻止 Windows 进入睡眠（锁屏/熄屏不睡眠）。
    只阻止【系统睡眠】(ES_SYSTEM_REQUIRED)，允许【显示器照常关闭】以省电。
    进程退出后该状态自动撤销，无需手动清理、无需管理员权限。"""
    if sys.platform != "win32":
        return
    try:
        k32 = ctypes.windll.kernel32
        k32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
        k32.SetThreadExecutionState.restype = ctypes.c_uint32
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)  # 立即生效
        logger.info("防睡眠已激活（SetThreadExecutionState），锁屏/熄屏不睡眠")
        call_count = 0
        while not STOP_EVENT.is_set():
            k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)  # 每30s刷新
            call_count += 1
            if call_count % 10 == 0:   # 每 ~5 分钟输出心跳（确认线程存活）
                logger.info("防睡眠心跳正常 (第 %d 次刷新)", call_count)
            STOP_EVENT.wait(30)
        k32.SetThreadExecutionState(ES_CONTINUOUS)  # 撤销，允许系统再次睡眠
        logger.info("防睡眠状态已撤销")
    except Exception as e:
        logger.warning(f"防睡眠调用失败（不影响主服务）: {e}")

def _wait_for_network(timeout=25):
    """等待外网可达。PC 从睡眠唤醒后网卡可能尚未就绪，先等网络再启动 cpolar，
    避免 cpolar 因连不上而反复退出、陷入快速重启循环。"""
    import socket as _sock
    deadline = time.time() + timeout
    while time.time() < deadline:
        if STOP_EVENT.is_set():
            return False
        try:
            _sock.setdefaulttimeout(3)
            _s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            _s.connect(("www.baidu.com", 443))
            _s.close()
            return True
        except Exception:
            time.sleep(2)
    return False

def cpolar_health_monitor():
    """每 15s 端到端探测隧道是否可达；首次失败后快速重试 3 次（间隔 4s，共 12s），
    确认真的假死后才强制重启 cpolar，避免因网卡瞬时掉线误杀。
    覆盖场景：
    1. 锁屏/熄屏后网卡暂时掉线 → TCP 断开 → cpolar 重新建连
    2. 网络抖动导致隧道「进程还在但不通」的假死状态"""
    import ssl, urllib.request as _urllib
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    while not STOP_EVENT.is_set():
        STOP_EVENT.wait(15)
        if STOP_EVENT.is_set() or not TUNNEL_URL:
            continue
        try:
            _urllib.urlopen(TUNNEL_URL.rstrip("/") + "/health", timeout=6, context=ctx)
        except Exception:
            # 初探失败 → 短间隔快速重试 3 次（共约 12s），容忍瞬时抖动
            failed = True
            for i in range(3):
                if STOP_EVENT.is_set():
                    return
                time.sleep(4)
                try:
                    _urllib.urlopen(TUNNEL_URL.rstrip("/") + "/health", timeout=6, context=ctx)
                    failed = False
                    break
                except Exception:
                    pass
            if failed:
                logger.warning("隧道连续探测失败（假死），强制重启 cpolar...")
                global _cpolar_proc
                if _cpolar_proc and _cpolar_proc.poll() is None:
                    try:
                        _cpolar_proc.terminate()
                    except Exception:
                        pass

# ── 状态管理 ──
STATUS = {"state": "idle", "message": "", "progress": 0, "output_file": ""}
TASK_LOCK = threading.Lock()

def run_script(script_name, *args, timeout=900, prefix=""):
    """运行 venv 下的 python 脚本，流式输出到日志，支持取消。返回 (returncode, output)。"""
    global CHILD_PIDS, CANCEL_REQUESTED
    try:
        proc = subprocess.Popen(
            [str(VENV_PY), str(SCRIPTS / script_name), *args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW, bufsize=1,
        )
    except Exception as e:
        logger.error(f"启动 {script_name} 失败: {e}")
        return -1, str(e)
    CHILD_PIDS.append(proc.pid)
    out_lines = []
    try:
        for line in iter(proc.stdout.readline, ''):
            if not line: break
            s = line.rstrip('\n')
            if s.strip():
                out_lines.append(s)
                logger.info(f"  [{prefix or script_name}] {s}")
            if CANCEL_REQUESTED:
                try: proc.terminate()
                except Exception: pass
                break
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try: proc.kill()
            except Exception: pass
            try: proc.wait()
            except Exception: pass
            logger.error(f"{script_name} 超时 ({timeout}s)")
            return -2, "\n".join(out_lines)
    except Exception as e:
        logger.error(f"{script_name} 运行异常: {e}")
        return -1, str(e)
    return proc.returncode, "\n".join(out_lines)

def set_state(s, msg="", prog=0, out=""):
    STATUS["state"] = s
    STATUS["message"] = msg
    STATUS["progress"] = prog
    if out:
        STATUS["output_file"] = out
    with open(BASE / "status.json", "w") as f:
        json.dump(STATUS, f)

def do_cancel(batch_id, before_files):
    """取消时清理本批次已下载的新视频和输出（不删旧文件）"""
    global CANCEL_REQUESTED
    for p in list(VIDEOS.glob("*.mp4")):
        if p not in before_files:
            try: p.unlink(); logger.info(f"  取消清理: {p.name}")
            except Exception: pass
    for p in list(OUTPUT.glob(f"{batch_id}_*")):
        try: p.unlink(); logger.info(f"  取消清理: {p.name}")
        except Exception: pass
    for p in list(VIDEOS.glob(f"{batch_id}_*")):
        try: p.unlink(); logger.info(f"  取消清理: {p.name}")
        except Exception: pass
    CANCEL_REQUESTED = False
    set_state("idle", "已取消并清理", 0, "")

def run_pipeline(links_text, mode="auto"):
    """后台线程：智能提取→下载→剪辑。TASK_LOCK 防止同时执行两次。
    mode: auto / split_h / split_v / shorts"""
    global CANCEL_REQUESTED, CHILD_PIDS
    if not TASK_LOCK.acquire(blocking=False):
        logger.warning("任务已在执行中，忽略重复提交")
        set_state("error", "已有任务在执行，请等待完成", 0)
        return
    try:
        CANCEL_REQUESTED = False
        CHILD_PIDS.clear()
        urls = extract_urls(links_text)
        if not urls:
            set_state("error", "没有找到抖音链接（v.douyin.com 或 www.douyin.com/video）", 0)
            return
        batch_id = time.strftime("b%Y%m%d_%H%M%S")
        logger.info(f"批次 {batch_id}: 收到 {len(urls)} 条链接")
        
        # 任务日志
        task_fh = logging.FileHandler(LOGS / f"task_{batch_id}.log", encoding="utf-8")
        task_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(task_fh)
        
        # 清空 links.txt
        try: LINKS.write_text("# each batch fresh\n\n", encoding="utf-8")
        except: pass
        
        before_files = set(VIDEOS.iterdir())
        set_state("processing", f"下载 {len(urls)} 个视频... (约 {len(urls)*15}s)", 10)
        with open(LINKS, "a", encoding="utf-8") as f:
            for u in urls: f.write(u + "\n")
        
        # 1) 下载
        logger.info(f"批次 {batch_id}: 下载中...")
        rc, out = run_script("download_videos.py", timeout=600, prefix="下载")
        if CANCEL_REQUESTED:
            do_cancel(batch_id, before_files)
            logger.removeHandler(task_fh); task_fh.close()
            return
        if rc != 0:
            logger.error(f"下载失败: {out[-500:]}")
            for p in list(VIDEOS.glob("*.mp4")):
                if p not in before_files:
                    try: p.unlink(); logger.info(f"  清理: {p.name}")
                    except: pass
            set_state("error", "下载失败：链接无效或视频不存在", 0)
            logger.removeHandler(task_fh); task_fh.close()
            return
        
        # 2) 重命名新视频（加批次前缀）
        after = set(VIDEOS.iterdir())
        new = sorted([v for v in (after - before_files) if v.suffix == ".mp4"], key=lambda p: p.stat().st_mtime)
        logger.info(f"批次 {batch_id}: 下载完成 {len(new)} 个视频")
        new_videos = []
        for i, src in enumerate(new):
            dst = VIDEOS / f"{batch_id}_{i+1}_{src.name}"
            try: src.rename(dst); new_videos.append(dst)
            except: new_videos.append(src)  # rename失败就用原名
        if len(new_videos) < 2:
            logger.warning(f"批次 {batch_id}: 仅 {len(new_videos)} 个新视频，不够拼接")
            set_state("error", f"需要至少2个新视频，当前 {len(new_videos)} 个", 0)
            return
        
        # 3) 剪辑
        eta = int(5 + len(new_videos) * 12)
        set_state("processing", f"剪辑 {len(new_videos)} 个视频... (约 {eta}s)", 60)
        logger.info(f"批次 {batch_id}: 剪辑中...")
        rc, out = run_script("auto_edit.py", batch_id, "--mode", mode, timeout=900, prefix="剪辑")
        if CANCEL_REQUESTED:
            do_cancel(batch_id, before_files)
            logger.removeHandler(task_fh); task_fh.close()
            return
        if rc != 0:
            err_msg = out[-200:].strip() or "未知错误"
            logger.error(f"剪辑失败 rc={rc}: {err_msg}")
            set_state("error", f"剪辑失败: {err_msg}", 0)
            return
        
        # 4) 成品
        finals = sorted(OUTPUT.glob(f"{batch_id}_*.mp4"), key=lambda p: p.stat().st_mtime)
        if finals:
            newest = finals[-1]
            (newest.with_suffix(".json")).write_text(json.dumps({
                "batch_id": batch_id, "output": newest.name,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "videos": [v.name for v in new_videos],
                "links": urls,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                t = LINKS.read_text(encoding="utf-8") if LINKS.exists() else ""
                LINKS.write_text("\n".join(l for l in t.splitlines() if l.startswith("#"))[:500]+"\n", encoding="utf-8")
            except: pass
            size = newest.stat().st_size // 1024
            logger.info(f"批次 {batch_id}: 完成 -> {newest.name} ({size}KB)")
            set_state("complete", f"完成！{newest.name} ({size}KB)", 100, newest.name)
        else:
            logger.error(f"批次 {batch_id}: 未生成成品")
            set_state("error", "未生成成品文件", 0)
        
        logger.removeHandler(task_fh); task_fh.close()
    
    except Exception as e:
        logger.error(f"任务异常 {type(e).__name__}: {e}")
        try:
            for p in list(VIDEOS.glob(f"{batch_id}_*")):
                try: p.unlink()
                except: pass
            logger.removeHandler(task_fh); task_fh.close()
        except: pass
        set_state("error", f"任务异常: {e}", 0)
    finally:
        CHILD_PIDS.clear()
        CANCEL_REQUESTED = False
        TASK_LOCK.release()
# ── Web页面 ──
HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>🎬 抖音自动管线</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#f5f5f7;min-height:100vh}
.header{background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;padding:24px;text-align:center}
.header h1{font-size:22px;margin-bottom:4px}
.header p{font-size:13px;color:#a0a0b0}
.main{max-width:500px;margin:20px auto;padding:0 16px}
.card{background:#fff;border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 2px 12px rgba(0,0,0,.06)}
.card h2{font-size:16px;margin-bottom:12px;color:#1d1d1f}
textarea{width:100%;min-height:200px;border:1px solid #d2d2d7;border-radius:10px;padding:12px;font-size:14px;resize:vertical;line-height:1.5}
button{width:100%;padding:14px;background:#007aff;color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:600;margin-top:12px;cursor:pointer}
button:hover{background:#0062cc}
button:disabled{background:#999;cursor:default}
.status{padding:14px;border-radius:10px;margin-bottom:12px;font-size:14px;display:none}
.status.idle{display:block;background:#f0f0f5;color:#666}
.status.processing{display:block;background:#fff3cd;color:#856404;border:1px solid #ffeeba}
.status.complete{display:block;background:#d4edda;color:#155724;border:1px solid #c3e6cb}
.status.error{display:block;background:#f8d7da;color:#721c24;border:1px solid #f5c6cb}
.bar{width:100%;height:8px;background:#e9ecef;border-radius:4px;margin-top:10px;overflow:hidden}
.bar div{height:100%;background:#28a745;border-radius:4px;transition:width 0.5s}
.download-btn{display:inline-block;padding:10px 24px;background:#28a745;color:#fff;border-radius:8px;text-decoration:none;font-weight:600;margin-top:10px}
.download-btn:hover{background:#218838}
.history{display:flex;align-items:center;padding:8px 10px;border-bottom:1px solid #eee;color:#333;text-decoration:none;font-size:14px;gap:8px}
.history:hover{background:#f5f5f5}
.history-item{padding:10px;border-bottom:1px solid #eee;position:relative}
.history-name{flex:1;color:#333;text-decoration:none;font-size:14px;font-weight:500}
.history-name:hover{color:#007aff}
.del-btn{padding:4px 10px;background:#ff3b30;color:#fff;border:none;border-radius:6px;font-size:12px;cursor:pointer}
.del-btn:hover{background:#d32f2f}
.recent-badge{display:inline-block;background:#ff3b30;color:#fff;font-size:10px;font-weight:700;padding:2px 6px;border-radius:4px;margin-left:6px;vertical-align:middle;animation:pulse 2s infinite}
@keyframes pulse{0%{opacity:1}50%{opacity:0.5}100%{opacity:1}}
.meta{font-size:11px;color:#999;margin-top:4px}
#tunnel-url{padding:6px 10px;background:#1a1a2e;color:#4ade80;text-align:center;font-size:13px;word-break:break-all}
#tunnel-url a{color:#4ade80;text-decoration:none}
#tunnel-url a:hover{text-decoration:underline}
</style></head><body>
<div id="tunnel-url">正在建立公网隧道...</div>
<div class="header"><h1>🎬 抖音自动管线</h1><p>贴链接 → 自动下载 → 智能剪辑 → 直接下载成品</p></div>
<div class="main">
<div class="status" id="status">就绪，等待链接...</div>
<div class="card"><h2>📨 投递抖音链接</h2>
<textarea id="links" placeholder="https://v.douyin.com/xxxx/&#10;https://v.douyin.com/yyyy/&#10;每行一条"></textarea>
<div style="margin-top:10px;font-size:13px;color:#666">
  剪辑模式：
  <select id="edit-mode" style="padding:6px 8px;border-radius:6px;border:1px solid #d2d2d7;font-size:13px">
    <option value="auto">自动拼接（随机排序+转场）</option>
    <option value="split_h">左右分屏（2视频并排5s）</option>
    <option value="split_v">上下分屏（2视频堆叠5s）</option>
    <option value="shorts">5秒快剪（每段截5s拼接）</option>
    <option value="mega">🤖 一键成片（节拍+特效+转场全自动）</option>
  </select>
</div>
<button id="btn" onclick="submitLinks()">开始处理</button>
<button id="cancel-btn" onclick="cancelTask()" style="display:none;width:100%;padding:14px;background:#ff3b30;color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:600;margin-top:0;cursor:pointer">取消任务</button></div>
<div id="output"></div>
<div class="card"><h2>📁 历史成品</h2><div id="history"></div></div>
</div>
<script>
async function submitLinks(){
    const links=document.getElementById('links').value.trim();
    if(!links) return;
    const mode=document.getElementById('edit-mode').value||'auto';
    document.getElementById('btn').disabled=true;
    document.getElementById('btn').textContent='处理中...';
    try {
        const r=await fetch('/submit',{method:'POST',body:new URLSearchParams({links,mode})});
        const d=await r.json();
        if(d.ok) pollStatus();
    } catch(e) { alert('提交失败: '+e.message); document.getElementById('btn').disabled=false; document.getElementById('btn').textContent='开始处理'; }
}
async function pollStatus(){
    try {
        const r=await fetch('/status');
        const s=await r.json();
        const st=document.getElementById('status');
        const out=document.getElementById('output');
        st.className='status '+s.state;
        if(s.state==='idle'){st.textContent=s.message||'就绪，等待链接...'; out.innerHTML=''; document.getElementById('btn').disabled=false; document.getElementById('btn').textContent='开始处理'; document.getElementById('cancel-btn').style.display='none';}
        else if(s.state==='processing'){st.textContent=s.message; out.innerHTML='<div class="bar"><div style="width:'+s.progress+'%"></div></div><div style="margin-top:6px;font-size:12px;color:#666">'+s.progress+'%</div>'; document.getElementById('btn').disabled=true; document.getElementById('btn').textContent='处理中...'; document.getElementById('cancel-btn').style.display='block'; setTimeout(pollStatus,2000);}
        else if(s.state==='complete'){st.textContent='[OK] 完成！'; out.innerHTML='<a class="download-btn" href="/download/'+s.output_file+'">[DOWNLOAD] '+s.output_file+'</a> <button class="del-btn" onclick="resetUI()">关闭</button>'; document.getElementById('btn').disabled=false; document.getElementById('btn').textContent='开始处理'; document.getElementById('cancel-btn').style.display='none'; loadHistory();}
        else if(s.state==='error'){st.textContent='[ERR] '+s.message; document.getElementById('btn').disabled=false; document.getElementById('btn').textContent='开始处理'; document.getElementById('cancel-btn').style.display='block';}
    } catch(e) { setTimeout(pollStatus,3000); }
}
async function cancelTask(){
    if(!confirm('取消当前任务？将清理本次全部临时文件和视频')) return;
    document.getElementById('status').textContent='正在取消...';
    document.getElementById('status').className='status idle';
    await fetch('/cancel');
    document.getElementById('status').textContent='[CANCEL] 已取消并清理';
    document.getElementById('status').className='status idle';
    document.getElementById('output').innerHTML='';
    document.getElementById('btn').disabled=false;
    document.getElementById('btn').textContent='开始处理';
    loadHistory();
}
async function resetUI(){
    await fetch('/reset');
    location.reload();
}
async function deleteFile(file){
    if(!confirm('确认删除 ' + file + ' 及其源视频？')) return;
    try {
        const r=await fetch('/delete',{method:'POST',body:new URLSearchParams({file})});
        const d=await r.json();
        if(d.ok) loadHistory();
        else alert('删除失败: ' + (d.error||''));
    } catch(e) { alert('删除失败: '+e.message); }
}
async function loadHistory(retry){
    retry=retry||0;
    const h=document.getElementById('history');
    try {
        const r=await fetch('/list');
        const files=await r.json();
        if(!Array.isArray(files) || files.length===0){
            h.innerHTML='<span style="color:#999;font-size:13px">暂无成品</span>';
            return;
        }
        let html='';
        for(const f of files){
            const safe=String(f.file).replace(/[<>&"]/g, c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'})[c]);
            const vc=f.videos?f.videos.length:0;
            const lc=f.links?f.links.length:0;
            const badge=f.recent?'<span class="recent-badge">NEW</span>':'';
            html+='<div class="history-item" data-file="'+safe+'">'
                +'<a class="history-name" href="/download/'+encodeURIComponent(f.file)+'">[VIDEO] '+safe+'</a>'
                +badge
                +'<button class="del-btn" data-action="delete">[DEL]</button>'
                +'<div class="meta">'+vc+' videos | '+lc+' links</div>'
                +'</div>';
        }
        h.innerHTML=html;
    } catch(e) {
        if(retry<3) setTimeout(function(){loadHistory(retry+1)},2000);
        else h.innerHTML='<span style="color:#999">加载失败，刷新重试</span>';
    }
}

// Event delegation for delete buttons
document.addEventListener('click', function(e){
    if(e.target && e.target.dataset && e.target.dataset.action==='delete'){
        const item=e.target.closest('.history-item');
        if(item) deleteFile(item.dataset.file);
    }
});
// Poll tunnel URL and show at top
async function pollTunnel(){
    try {
        const r=await fetch('/tunnel');
        const d=await r.json();
        if(d.url) document.getElementById('tunnel-url').innerHTML='<a href="'+d.url+'" target="_blank">'+d.url+'</a>';
    } catch(e) {}
}
loadHistory();
pollStatus();
pollTunnel();
setInterval(pollTunnel,10000);
setInterval(loadHistory,30000);
</script></body></html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global CANCEL_REQUESTED, CHILD_PIDS
        if self.path == "/":
            self.send_html(HTML)
        elif self.path == "/status":
            self.send_json(STATUS)
        elif self.path == "/tunnel":
            self.send_json({"url": TUNNEL_URL})
        elif self.path == "/health":
            # 优先用隧道地址判断 cpolar 状态（服务器内 tasklist 在某些环境受限）
            have_cpolar = bool(TUNNEL_URL) or (_cpolar_proc is not None and _cpolar_proc.poll() is None)
            self.send_json({"tunnel": bool(TUNNEL_URL), "cpolar": have_cpolar,
                            "url": TUNNEL_URL or None, "awake": not STOP_EVENT.is_set()})
        elif self.path == "/cancel":
            # 取消任务：仅杀掉下载/剪辑子进程（绝不杀服务器本身），
            # run_pipeline 检测到 CANCEL_REQUESTED 后会自行清理并置 idle
            CANCEL_REQUESTED = True
            import subprocess as _sp
            for pid in list(CHILD_PIDS):
                try: _sp.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                             capture_output=True, timeout=5)
                except Exception: pass
            CHILD_PIDS.clear()
            logger.info("取消请求已发送，正在清理子进程...")
            self.send_json({"ok": True})
        elif self.path == "/reset":
            # 关闭按钮专用：仅清服务端状态，不删文件
            CANCEL_REQUESTED = False
            CHILD_PIDS.clear()
            set_state("idle", "就绪，等待链接...", 0, "")
            STATUS["output_file"] = ""
            try:
                with open(BASE / "status.json", "w") as f:
                    json.dump(STATUS, f)
            except Exception: pass
            self.send_json({"ok": True})
        elif self.path == "/list":
            # 新批次命名 b20260721_233000_...mp4 + 旧命名 final_xxx.mp4
            all_mp4s = sorted(OUTPUT.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
            # 排除临时文件，只取批次或 final 命名的
            finals = []
            for f in all_mp4s:
                if f.stem.startswith(("final_", "b")) and "batch" not in f.stem:
                    finals.append(f)
            items = []
            now = time.time()
            for f in finals[:20]:
                sidecar = f.with_suffix(".json")
                links = []
                videos = []
                if sidecar.exists():
                    try:
                        sc = json.loads(sidecar.read_text(encoding="utf-8"))
                        links = sc.get("links", [])
                        videos = sc.get("videos", [])
                    except: pass
                mtime = f.stat().st_mtime
                recent = (now - mtime) < 300  # 5分钟内
                items.append({"file": f.name, "links": links, "videos": videos, "recent": recent})
            self.send_json(items)
        elif self.path.startswith("/download/"):
            fname = self.path[10:]
            fpath = OUTPUT / fname
            if fpath.exists():
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(fpath.stat().st_size))
                self.end_headers()
                with open(fpath, "rb") as f:
                    while chunk := f.read(65536):
                        self.wfile.write(chunk)
            else:
                self.send_error(404)
        else:
            self.send_error(404)
    
    def do_POST(self):
        if self.path == "/submit":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            params = urllib.parse.parse_qs(body)
            links = params.get("links", [""])[0].strip()
            mode = params.get("mode", ["auto"])[0].strip() or "auto"
            if not links:
                self.send_json({"ok": False, "error": "链接不能为空"}, 400)
                return
            # 启动后台线程
            t = threading.Thread(target=run_pipeline, args=(links, mode), daemon=True)
            t.start()
            self.send_json({"ok": True})
        elif self.path == "/delete":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            params = urllib.parse.parse_qs(body)
            fname = params.get("file", [""])[0].strip()
            if not fname or "/" in fname or ".." in fname:
                self.send_json({"ok": False, "error": "非法文件名"}, 400)
                return
            deleted = []
            errors = []
            fpath = OUTPUT / fname
            sidecar = fpath.with_suffix(".json")
            sc_data = {}
            if sidecar.exists():
                try:
                    sc_data = json.loads(sidecar.read_text(encoding="utf-8"))
                except: pass
            
            batch_id = sc_data.get("batch_id", "")
            
            if batch_id:
                # 按批次前缀删除：该批次的所有视频 + output + sidecar
                for p in list(VIDEOS.glob(f"{batch_id}_*")):
                    try:
                        p.unlink()
                        deleted.append(f"videos/{p.name}")
                    except Exception as e:
                        errors.append(f"videos/{p.name}: {e}")
                for p in list(OUTPUT.glob(f"{batch_id}_*")):
                    try:
                        p.unlink()
                        deleted.append(f"output/{p.name}")
                    except Exception as e:
                        errors.append(f"output/{p.name}: {e}")
            else:
                # 旧版兼容（无 batch_id）：按 sidecar 中记录的路径删
                for v in sc_data.get("videos", []):
                    p = VIDEOS / v
                    if p.exists():
                        try:
                            p.unlink()
                            deleted.append(f"videos/{v}")
                        except Exception as e:
                            errors.append(f"{v}: {e}")
                if sidecar.exists():
                    try:
                        sidecar.unlink()
                        deleted.append(f"output/{sidecar.name}")
                    except Exception as e:
                        errors.append(f"sidecar: {e}")
                if fpath.exists():
                    try:
                        fpath.unlink()
                        deleted.append(f"output/{fname}")
                    except Exception as e:
                        errors.append(f"{fname}: {e}")
            
            logger.info(f"删除 {batch_id or fname} -> {len(deleted)} files, {len(errors)} errors")
            self.send_json({"ok": True, "deleted": deleted, "errors": errors})
        else:
            self.send_json({"ok": False, "error": "not found"}, 404)    
    def send_html(self, html):
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)
    
    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    
    def log_message(self, fmt, *args):
        msg = f"{args[0]} {args[1]} {args[2] if len(args)>2 else ''}".strip()
        logger.info(msg)
        try:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}")
        except: pass

if __name__ == "__main__":
    import socket
    
    # ── 端口冲突检测：强制清理旧进程 ──
    def kill_port_holder(port):
        """查找并杀死占用端口的旧进程"""
        try:
            out = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True,
                errors="replace", timeout=5
            ).stdout
            for line in out.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.split()[-1]
                    if pid != str(os.getpid()):
                        logger.warning(f"发现旧进程 PID={pid} 占用端口 {port}，正在终止...")
                        subprocess.run(["taskkill", "/PID", pid, "/F"],
                                       capture_output=True, timeout=5)
                        time.sleep(1)
        except Exception as e:
            logger.warning(f"端口清理失败: {e}")
    
    kill_port_holder(PORT)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    set_state("idle")

    # 获取本机IP
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except:
        ip = "127.0.0.1"
    finally:
        s.close()
    
    logger.info(f"服务器启动，端口 {PORT}，IP {ip}")
    try:
        banner = f"""
============================================================
  Douyin Pipeline Server
============================================================
  Local:    http://localhost:{PORT}
  Phone:    http://{ip}:{PORT}  (same WiFi)
  Log file: {LOGS / 'server.log'}
  Stop:     Ctrl+C / taskkill pythonw.exe
============================================================
"""
        print(banner)
    except: pass
    
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    # 防睡眠：保持系统唤醒（显示器可关闭），熄屏不断服务
    threading.Thread(target=_keep_awake_loop, daemon=True).start()
    # 内网穿透看门狗：端口已绑定后拉起 cpolar；先等网络就绪，退出后自动重连
    def cpolar_watchdog():
        global TUNNEL_URL
        while not STOP_EVENT.is_set():
            if not _wait_for_network(timeout=20):
                if STOP_EVENT.is_set():
                    return
                continue
            TUNNEL_URL = ""          # 清空旧地址，强制重新获取最新隧道
            start_cpolar()
            p = _cpolar_proc
            if p:
                try: p.wait()
                except Exception: pass
            if STOP_EVENT.is_set():
                return
            logger.warning("cpolar 已退出，5s 后自动重连...")
            time.sleep(5)
    threading.Thread(target=cpolar_watchdog, daemon=True).start()
    # 隧道健康探测：网络抖动导致假死时强制重启
    threading.Thread(target=cpolar_health_monitor, daemon=True).start()
    logger.info("防睡眠 + 隧道看门狗 + 健康检查 已启动（熄屏不再断服务）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOP_EVENT.set()
        srv.server_close()
        logger.info("服务器已停止")
