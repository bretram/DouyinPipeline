#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音视频批量下载器 (Playwright 引擎)
从 links.txt 读取链接 -> 打开页面提取视频直链 -> 下载到 videos/
"""
import sys, io
# Windows CMD 兼容（默认 GBK，会让 emoji 报错）
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = sys.stdout
except: pass

import re, time, json, csv, os
from pathlib import Path
from playwright.sync_api import sync_playwright
import requests

# 日志由 linkserver 统一捕获到 task_*.log，这里只打印到 stdout
def log(msg):
    try: print(msg, flush=True)
    except: pass

BASE = Path(os.environ.get("DOUYIN_PIPELINE_ROOT", r"F:\DouyinPipeline"))
LINKS = BASE / "links.txt"
VIDEOS = BASE / "videos"
META_CSV = BASE / "metadata.csv"
LOGS = BASE / "logs"

def extract_urls(text: str):
    """从文本中提取所有抖音分享链接"""
    urls = []
    # v.douyin.com/xxx 短链
    for m in re.finditer(r'https?://v\.douyin\.com/[a-zA-Z0-9_-]+/?', text):
        urls.append(m.group(0).rstrip('/'))
    # www.douyin.com/video/xxx 长链
    for m in re.finditer(r'https?://www\.douyin\.com/video/\d+', text):
        urls.append(m.group(0))
    return urls

def video_id_from_url(url: str) -> str:
    m = re.search(r'video/(\d+)', url)
    if m:
        return m.group(1)
    m = re.search(r'v\.douyin\.com/([a-zA-Z0-9_-]+)', url)
    if m:
        return "short_" + m.group(1)
    return str(abs(hash(url)))

def load_done_ids():
    """只检查 videos/ 目录已有文件（不要查 metadata.csv，那是历史记录）"""
    done = set()
    for p in VIDEOS.glob("*.mp4"):
        # 去掉批次前缀 b20260721_231759_1_ 再取原始 id
        stem = p.stem
        parts = stem.split("_")
        if len(parts) >= 2 and parts[0].startswith("b") and len(parts[0]) >= 15:
            stem = "_".join(parts[3:])  # 去掉 b20260721_231759_1_
        done.add(stem)
    return done

def download_video(playwright_url: str, dest: Path) -> bool:
    """用 requests 下载视频"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
        'Referer': 'https://www.douyin.com/'
    }
    r = requests.get(playwright_url, headers=headers, timeout=120, stream=True)
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}")
        return False
    total = 0
    with open(dest, 'wb') as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
            total += len(chunk)
    log(f"  下载完成: {total//1024} KB")
    return total > 1024

def process_one(url: str):
    vid = video_id_from_url(url)
    dest = VIDEOS / f"{vid}.mp4"
    
    log(f"\n{'='*50}")
    log(f"处理: {url[:80]}")
    log(f"  ID: {vid}")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
            viewport={'width': 1920, 'height': 1080}
        )
        page = ctx.new_page()
        
        print(f"  打开页面...")
        page.goto(url, wait_until='domcontentloaded', timeout=25000)
        time.sleep(6)
        
        # 从video元素拿真实播放地址
        video_url = page.evaluate('''
            () => {
                const vs = document.querySelectorAll('video');
                for (let v of vs) {
                    let src = v.currentSrc || v.src || '';
                    if (src && (src.includes('douyinvod') || src.includes('douyin'))) return src;
                }
                return null;
            }
        ''')
        
        # 获取页面元数据
        title = page.title() or ''
        desc = page.evaluate('() => { let m=document.querySelector("meta[name=description]"); return m?m.content:""; }') or ''
        
        browser.close()
    
    if not video_url:
        print("  [ERR] 未提取到视频直链")
        return False
    
    log(f"  [OK] 提取到直链 ({len(video_url)} chars)")
    log(f"  标题: {title[:60]}")
    
    log(f"  下载中...")
    ok = download_video(video_url, dest)
    if ok:
        print(f"  [OK] 已保存: {dest.name}")
        # 写metadata
        fields = ['aweme_id','desc','author','source_url','local_path','downloaded_at','filesize','duration']
        write_header = not META_CSV.exists()
        with META_CSV.open('a', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if write_header:
                w.writeheader()
            w.writerow({
                'aweme_id': vid,
                'desc': (desc or title)[:120],
                'author': '',
                'source_url': url,
                'local_path': str(dest),
                'downloaded_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                'filesize': dest.stat().st_size,
                'duration': '',
            })
        return True
    else:
        print("  [ERR] 下载失败")
        return False

def main():
    VIDEOS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    
    if not LINKS.exists():
        print(f"找不到 {LINKS}")
        return
    
    text = LINKS.read_text(encoding="utf-8")
    urls = extract_urls(text)
    # 去重
    seen = set()
    unique_urls = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)
    
    log(f"找到 {len(unique_urls)} 个链接")
    done = load_done_ids()
    
    fail_count = 0
    for i, url in enumerate(unique_urls, 1):
        vid = video_id_from_url(url)
        if vid in done:
            print(f"  [{i}/{len(unique_urls)}] 跳过(已存在): {url[:60]}")
            continue
        print(f"  [{i}/{len(unique_urls)}]", end="")
        if not process_one(url):
            fail_count += 1
    
    log(f"\n{'='*50}")
    log(f"处理完成: 成功 {len(unique_urls)-fail_count}, 失败 {fail_count}")
    ls = list(VIDEOS.glob("*.mp4"))
    log(f"videos/ 现有 {len(ls)} 个文件:")
    for p in sorted(ls):
        print(f"  {p.name}  ({p.stat().st_size//1024} KB)")
    
    if fail_count > 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
