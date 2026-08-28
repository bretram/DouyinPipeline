#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能自动剪辑 v3
- 场景检测 → 保留最精彩片段
- 节拍检测 → 音频能量峰值分析，卡点剪辑（无需额外库）
- 随机特效 → 变速 / 滤镜 / 推拉等 8 种
- xfade 转场 → 10 种花式切换
- 一键成片 → mega 模式：以上全部随机组合，真·全自动
"""
import sys, io, os, random, subprocess, json, time, shutil
import numpy as np
from pathlib import Path
# Windows CMD 兼容
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = sys.stdout
except: pass
from pathlib import Path

# 日志由 linkserver 统一捕获到 task_*.log，只打印到 stdout
def log(msg):
    try: print(f"  {msg}", flush=True)
    except: pass

ROOT = Path(os.environ.get("DOUYIN_PIPELINE_ROOT", r"F:\DouyinPipeline"))
FFMPEG = os.environ.get("FFMPEG_BIN", str(ROOT / ".venv" / "ffmpeg.exe"))
FFPROBE = os.environ.get("FFPROBE_BIN", str(ROOT / ".venv" / "ffprobe.exe"))
SCENE = os.environ.get("SCENEDETECT_BIN", str(ROOT / ".venv" / "Scripts" / "scenedetect.exe"))
VIDEOS = ROOT / "videos"
OUTPUT = ROOT / "output"
TEMP = ROOT / ".temp"
TARGET_SEC = 20       # 每段视频截取长度（秒）
TRANSITION = 0.5      # 转场时长（秒）
TARGET_W, TARGET_H = 540, 960   # 竖屏

def run(cmd, timeout=300):
    cname = os.path.basename(cmd[0]) if cmd else "?"
    log(f"  ffmpeg: {cname} {' '.join(str(x) for x in cmd[1:4])}... ({timeout}s timeout)")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"超时 ({timeout}s)")
    if r.returncode != 0:
        raise RuntimeError(f"cmd failed ({r.returncode}): {r.stderr[-200:]}")
    return r.stdout

def get_duration(path):
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30
        )
        dur = float(r.stdout.strip())
        return dur
    except:
        return 0.0

def get_resolution(path):
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30
    )
    d = json.loads(r.stdout)
    s = d.get("streams", [{}])[0]
    return s.get("width", 540), s.get("height", 960)

def detect_scenes(path):
    """用 scenedetect 检测场景变化，返回精彩片段时间点"""
    try:
        run([
            SCENE, "-i", str(path),
            "detect-content", "-t", "30",
            "list-scenes", "-o", str(TEMP), "-f", "csv"
        ], timeout=120)
    except:
        log(f"     场景检测跳过（视频太短或检测失败）")
        return []
    # 解析CSV（scenedetect 输出固定名 csv.csv）
    csv_path = TEMP / "csv.csv"
    scenes = []
    if csv_path.exists():
        with open(csv_path) as f:
            lines = f.readlines()
        start_header = False
        for line in lines:
            line = line.strip()
            if line.startswith("Scene Number"):
                start_header = True
                continue
            if not start_header or not line:
                continue
            parts = line.split(",")
            if len(parts) >= 7:
                try:
                    start_s = parts[3].strip()
                    end_s = parts[6].strip()
                    scenes.append((float(start_s), float(end_s)))
                except: pass
        csv_path.unlink(missing_ok=True)
    return scenes

def prepare_clip(src, seg_idx, duration=TARGET_SEC):
    """准备单个视频片段：裁剪+缩放+统一格式"""
    dur = get_duration(src)
    w, h = get_resolution(src)
    
    log(f"  [{seg_idx}] {src.name} ({dur:.0f}s, {w}x{h})")
    
    # 场景检测：找到最活跃的片段
    scenes = detect_scenes(src)
    log(f"     场景数: {len(scenes)}")
    
    if scenes and len(scenes) > 1:
        # 选中间几个场景（跳过开头结尾），拼成一段
        mid = scenes[len(scenes)//3 : -len(scenes)//3] if len(scenes) > 3 else scenes
        # 取其中最长的 scene 作为起点
        best = max(mid, key=lambda s: s[1]-s[0])
        start = max(0, best[0])
        clip_dur = min(duration, dur - start)
    else:
        # 没有场景检测结果，取中间段
        start = max(0, dur / 2 - duration / 2)
        clip_dur = min(duration, dur - start)
    
    out = TEMP / f"clip_{seg_idx:03d}.mp4"
    
    # 缩放适配竖屏
    scale_filter = f"scale=w={TARGET_W}:h={TARGET_H}:force_original_aspect_ratio=decrease,pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2"
    
    run([
        FFMPEG, "-y", "-ss", str(start), "-i", str(src),
        "-t", str(clip_dur),
        "-vf", f"{scale_filter},fps=30",
        "-c:v", "libx264", "-preset", "fast", "-crf", "21",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "44100",  # 保留原声
        str(out)
    ], timeout=300)
    
    actual_dur = get_duration(out)
    log(f"     截取: {start:.0f}s ~ {start+actual_dur:.0f}s ({actual_dur:.0f}s)")
    return out

def create_concat_with_transitions(clips, batch_id=""):
    """
    用 concat demuxer 拼接 + 首尾淡入淡出 + BGM配乐
    """
    # concat 清单
    concat_file = TEMP / "concat.txt"
    with open(concat_file, "w", encoding="utf-8") as f:
        for c in clips:
            f.write(f"file '{c}'\n")
    
    # 先拼接（视频+音频都copy）
    concat_video = TEMP / "concat_video.mp4"
    run([
        FFMPEG, "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy",  # 视频音频都直接 copy，不重新编码
        str(concat_video)
    ], timeout=300)
    
    concat_dur = get_duration(concat_video)
    
    final = OUTPUT / f"{batch_id or 'final'}_{time.strftime('%H%M%S')}.mp4"
    
    # 淡入淡出
    fade_in = min(1.0, concat_dur / 4)
    fade_out = min(1.0, concat_dur / 4)
    
    # 重新编码，加上淡入淡出（保留每段视频自己的原声）
    run([
        FFMPEG, "-y",
        "-i", str(concat_video),
        "-filter:v", f"fade=t=in:st=0:d={fade_in},fade=t=out:st={concat_dur-fade_out}:d={fade_out}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "21",
        "-c:a", "aac", "-b:a", "128k",  # 保留并转码原声
        str(final)
    ], timeout=600)
    
    return final

# ══════════════════════════════════════════════════════════════
#  节拍检测 ・ 特效 ・ xfade 转场 ・ 一键成片 Mega 模式
# ══════════════════════════════════════════════════════════════

def detect_beats(src):
    """从音频 PCM 用 numpy 检测能量峰值 → 节拍时间点列表（零额外依赖）"""
    SR = 22050; HOP = 512
    r = subprocess.run([FFMPEG, "-v", "error", "-i", str(src),
                        "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                       capture_output=True, timeout=60)
    if r.returncode != 0 or len(r.stdout) < 200:
        return []
    samples = np.frombuffer(r.stdout, dtype=np.float32)
    dur = len(samples) / SR
    if dur < 1.0:
        return [0.0, dur] if dur > 0.5 else [0.0]
    n_frames = len(samples) // HOP
    energy = np.array([np.sqrt(np.mean(samples[i * HOP:(i + 1) * HOP] ** 2))
                       for i in range(n_frames)])
    energy = energy / max(energy.max(), 1e-10)
    diff = np.diff(energy, prepend=0)
    diff[diff < 0] = 0
    thresh = max(np.mean(diff) + 0.7 * np.std(diff), 0.03)
    peaks = []
    refr = max(1, int(0.22 * SR / HOP))
    for i in range(1, len(diff) - 1):
        if diff[i] > thresh and diff[i] >= diff[i - 1] and diff[i] >= diff[i + 1]:
            if not peaks or i - peaks[-1] > refr:
                peaks.append(i)
    bt = sorted(p * HOP / SR for p in peaks)
    return bt if bt else [0.0]

EFFECTS = [
    ("原速",     ""),
    ("加速1.3x", "setpts=0.75*PTS"),
    ("慢放0.7x", "setpts=1.4*PTS"),
    ("暖色调",   "colorbalance=rs=0.08:gs=0:bs=-0.08"),
    ("冷色调",   "colorbalance=rs=-0.08:gs=0:bs=0.08"),
    ("鲜艳",     "eq=saturation=1.4:contrast=1.15"),
    ("复古",     "colorbalance=rs=0.1:gs=-0.05:bs=-0.1,hue=s=0.7"),
    ("黑白",     "hue=s=0"),
]
XFADES = ["fade", "fadeblack", "dissolve", "wipeleft", "wiperight",
           "slideleft", "slideright", "pixelize", "radial", "distance"]

def apply_effect(clip_path, clip_idx, temp_dir):
    """给一个片段随机应用视频特效，返回路径（可能是原路径）"""
    name, vf = random.choice(EFFECTS)
    if not vf: return clip_path
    log(f"    特效: {name}")
    out = temp_dir / f"fx_{clip_idx:03d}.mp4"
    if "setpts" in vf:
        atempo = "1.33" if "0.75" in vf else "0.71"
        run([FFMPEG, "-y", "-i", str(clip_path), "-filter_complex",
             f"[0:v]{vf}[v];[0:a]atempo={atempo}[a]",
             "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-preset", "fast", "-crf", "21",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
             str(out)], timeout=180)
    else:
        run([FFMPEG, "-y", "-i", str(clip_path), "-vf", vf,
             "-c:v", "libx264", "-preset", "fast", "-crf", "21",
             "-c:a", "copy", "-pix_fmt", "yuv420p", str(out)], timeout=180)
    return out

def chain_xfade(clips, temp_dir):
    """把多个片段用随机 xfade 转场一次链完"""
    if len(clips) <= 1: return clips[0] if clips else None
    durations = [get_duration(c) for c in clips]
    xd, total_dur = 0.5, 0.0
    filter_parts, last_v = [], "0:v"
    for i in range(1, len(clips)):
        offset = max(0, total_dur + durations[i - 1] - xd)
        t = XFADES[i - 1] if i - 1 < len(XFADES) else "fade"
        ol = f"v{i}" if i < len(clips) - 1 else "v"
        filter_parts.append(
            f"[{last_v}][{i}:v]xfade=transition={t}:duration={xd}:offset={offset}[{ol}]")
        last_v = ol; total_dur += durations[i - 1] - xd
    merged = temp_dir / "xfade_merged.mp4"
    try:
        cmd = [FFMPEG, "-y"]
        for c in clips: cmd += ["-i", str(c)]
        cmd += ["-filter_complex", ";".join(filter_parts),
                "-map", f"[{last_v}]", "-an",
                "-c:v", "libx264", "-preset", "fast", "-crf", "21",
                "-pix_fmt", "yuv420p", str(merged)]
        run(cmd, timeout=600)
    except Exception as e:
        log(f"    xfade chain 失败: {e}，降级为纯拼接")
        cf = temp_dir / "fallback_concat.txt"
        cf.write_text("\n".join(f"file '{c}'" for c in clips), encoding="utf-8")
        run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(cf),
             "-c", "copy", str(merged)], timeout=300)
    return merged

def mega_oneshot(mp4s, batch_id=""):
    """
    一键成片 Mega 模式：
      节拍对齐切片段 → 随机特效 → xfade 转场链 → 随机 BGM
    """
    log(f"\n{'='*50}")
    log(f"  MEGA 一键成片")
    log(f"{'='*50}")
    me = TEMP / "mega"; me.mkdir(parents=True, exist_ok=True)

    # 1) beat-aligned clips
    raw = []
    for v in mp4s:
        d = get_duration(v)
        if d < 1: continue
        beats = detect_beats(v)
        if len(beats) >= 2:
            bi = random.randint(0, len(beats) - 1)
            start = beats[bi]
            seg = min(d - start, beats[bi + 1] - start if bi + 1 < len(beats) else 4)
            seg = max(1.5, min(seg, 5))
        else:
            start = random.uniform(0, max(0, d - 4))
            seg = max(1.5, min(4, d - start))
        start, seg = max(0, start), min(seg, d - start, 5)
        cut = me / f"cut_{len(raw):03d}.mp4"
        scale = (f"scale=w={TARGET_W}:h={TARGET_H}:force_original_aspect_ratio=decrease,"
                 f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30")
        run([FFMPEG, "-y", "-ss", str(start), "-i", str(v), "-t", str(seg),
             "-vf", scale, "-c:v", "libx264", "-preset", "fast", "-crf", "21",
             "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p",
             str(cut)], timeout=180)
        raw.append(cut)
        log(f"  切取: {v.name} @{start:.1f}s x{seg:.1f}s")
    if len(raw) < 2:
        log("  需要 >= 2 个可用视频")
        return None

    # 2) random effects (~70 %)
    random.shuffle(raw)
    fx = []
    for i, c in enumerate(raw):
        if random.random() < 0.7:
            c = apply_effect(c, i, me)
        fx.append(c)
    log(f"  {len(fx)} 片段已准备（节拍对齐+随机特效）")

    # 3) xfade chain
    log("  xfade 转场链...")
    merged = chain_xfade(fx, me)
    if not merged: return None

    # 4) fade + random BGM
    dur, bgm = get_duration(merged), random.choice(mp4s)
    final = OUTPUT / f"{batch_id or 'mega'}_{int(time.time()) % 100000:05d}.mp4"
    run([FFMPEG, "-y",
         "-i", str(merged), "-i", str(bgm),
         "-filter_complex",
         f"[1:a]atrim=0:{dur:.2f},afade=t=in:st=0:d=1,"
         f"afade=t=out:st={max(0.5, dur - 1):.2f}:d=1,volume=1.3[a]",
         "-map", "0:v", "-map", "[a]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
         "-shortest", str(final)], timeout=300)
    log(f"  mega 完成: {len(raw)} clips → {final.name}")
    return final

# ── 智能剪辑模式 ──

def parse_args():
    """解析命令行：python auto_edit.py <batch_id> [--mode MODE]"""
    batch_id = ""
    mode = "auto"
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--mode" and i + 1 < len(args):
            mode = args[i + 1]; i += 2
        elif not a.startswith("--"):
            batch_id = a; i += 1
        else:
            i += 1
    return batch_id, mode

def split_screen(mp4s, batch_id="", direction="h"):
    """分屏拼接：选 2 个随机视频左右/上下拼接，随机选一个视频的音频，~5s"""
    if len(mp4s) < 2:
        log(f"split_{direction} 需要至少 2 个视频")
        return None
    pick = random.sample(mp4s, 2) if len(mp4s) >= 2 else list(mp4s)
    a, b = pick[0], pick[1]
    da, db = get_duration(a), get_duration(b)
    clip = min(da, db, 5.0)
    if clip < 0.5: clip = min(da, db)  # 视频太短用全长
    sa = random.uniform(0, max(0, da - clip))
    sb = random.uniform(0, max(0, db - clip))
    audio_src = random.choice([0, 1])  # 随机选一个视频的音频

    half_w = TARGET_W // 2 if direction == "h" else TARGET_W
    half_h = TARGET_H if direction == "h" else TARGET_H // 2

    final = OUTPUT / f"{batch_id or 'split'}_{int(time.time())%100000:05d}.mp4"

    filter_str = (
        f"[0:v]trim={sa}:{sa+clip},setpts=PTS-STARTPTS,"
        f"scale=w={half_w}:h={half_h}:force_original_aspect_ratio=decrease,"
        f"pad={half_w}:{half_h}:(ow-iw)/2:(oh-ih)/2,setsar=1[v0];"
        f"[1:v]trim={sb}:{sb+clip},setpts=PTS-STARTPTS,"
        f"scale=w={half_w}:h={half_h}:force_original_aspect_ratio=decrease,"
        f"pad={half_w}:{half_h}:(ow-iw)/2:(oh-ih)/2,setsar=1[v1];"
        f"[v0][v1]{'hstack' if direction == 'h' else 'vstack'}=inputs=2[v]"
    )

    run([
        FFMPEG, "-y",
        "-i", str(a), "-i", str(b),
        "-filter_complex", filter_str,
        "-map", "[v]", "-map", f"{audio_src}:a?",
        "-t", str(clip),
        "-c:v", "libx264", "-preset", "fast", "-crf", "21",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        str(final)
    ], timeout=300)

    log(f"  split_{direction}: {a.name} + {b.name} -> {final.name}")
    return final

def shorts_5s(mp4s, batch_id=""):
    """每个视频截取 ~5s 最精彩片段，拼接输出"""
    if len(mp4s) < 2:
        log("shorts 需要至少 2 个视频")
        return None
    log(f"shorts 模式：{len(mp4s)} 个视频，各截 ~5s 精彩片段")
    random.shuffle(mp4s)
    clips = []
    for i, v in enumerate(mp4s):
        c = prepare_clip(v, i, duration=5)
        clips.append(c)
    log(f"合成...")
    final = create_concat_with_transitions(clips, batch_id)
    return final

def main():
    TEMP.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)

    batch_id, mode = parse_args()

    if batch_id:
        mp4s = sorted(VIDEOS.glob(f"{batch_id}_*.mp4"))
    else:
        mp4s = sorted(VIDEOS.glob("*.mp4"))

    if len(mp4s) < 2:
        log("需要至少 2 个视频")
        return

    log(f"模式: {mode}  视频数: {len(mp4s)}")
    for v in mp4s:
        log(f"  {v.name}  ({get_duration(v):.0f}s)")

    final = None

    if mode in ("split_h", "split_v"):
        final = split_screen(mp4s, batch_id, direction=mode[-1])
    elif mode == "shorts":
        final = shorts_5s(mp4s, batch_id)
    elif mode == "mega":
        final = mega_oneshot(mp4s, batch_id)
    else:
        # auto / default: 原有逻辑（随机顺序、场景检测、拼接）
        random.shuffle(mp4s)
        log(f"\n处理顺序:")
        clips = []
        for i, v in enumerate(mp4s):
            c = prepare_clip(v, i)
            clips.append(c)
        log(f"\n合成视频...")
        final = create_concat_with_transitions(clips, batch_id)

    if final:
        dur = get_duration(final)
        size = final.stat().st_size
        log(f"\n{'='*50}")
        log(f"[OK] 完成！")
        log(f"  输出: {final.name}")
        log(f"  时长: {dur:.0f}s")
        log(f"  大小: {size // 1024} KB")
    else:
        log("[ERR] 未能生成成品")

    # 清理临时文件
    import shutil
    try: shutil.rmtree(TEMP)
    except: pass
    try: TEMP.mkdir(parents=True, exist_ok=True)
    except: pass

if __name__ == "__main__":
    main()
