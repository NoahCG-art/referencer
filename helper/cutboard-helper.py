#!/usr/bin/env python3
"""
カットボード ローカルヘルパー

ブラウザは YouTube の映像ピクセルに触れない（クロスオリジン iframe / CORS）。
このヘルパーが動画を手元に落とし、localhost から配信することで、
カットボード側の解析をそのまま使えるようにする。

標準ライブラリのみ。追加インストールは yt-dlp だけ。

起動:  python cutboard-helper.py
停止:  Ctrl+C

API:
  GET  /health            稼働確認 + yt-dlp の有無
  POST /fetch  {url}      ダウンロード開始 -> job
  GET  /jobs              進行中・完了したジョブ一覧
  GET  /library           手元にある動画一覧
  DELETE /library/<id>    動画を削除
  GET  /media/<file>      動画本体（Range 対応）
"""

import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

HOST = "127.0.0.1"
PORT = 8787
ROOT = Path(__file__).resolve().parent
MEDIA = ROOT / "media"
MEDIA.mkdir(exist_ok=True)

# 解析にかけるので、H.264 の mp4 を最優先で取りに行く。
# WebCodecs + MP4Box の精密解析が効くのは mp4 コンテナのため。
FORMAT = (
    "bv*[ext=mp4][vcodec^=avc1][height<=1080]+ba[ext=m4a]/"
    "b[ext=mp4][vcodec^=avc1][height<=1080]/"
    "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
)

JOBS = {}
JOBS_LOCK = threading.Lock()


def find_ytdlp():
    """yt-dlp を探す。PATH / 同じフォルダの exe / python -m yt_dlp の順。"""
    exe = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if exe:
        return [exe]
    for name in ("yt-dlp.exe", "yt-dlp"):
        local = ROOT / name
        if local.exists():
            return [str(local)]
    try:
        r = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True, timeout=20,
        )
        if r.returncode == 0:
            return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        pass
    return None


def ytdlp_version(cmd):
    try:
        r = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=20)
        return r.stdout.strip() or None
    except Exception:
        return None


def safe_name(s, fallback="video"):
    """ファイル名に使えない文字を落とす。"""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s or "")
    s = s.strip(" .") or fallback
    return s[:120]


def sidecar_path(media_file: Path) -> Path:
    return media_file.with_suffix(media_file.suffix + ".json")


def write_sidecar(media_file: Path, info: dict):
    try:
        sidecar_path(media_file).write_text(
            json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception:
        pass


PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")
DEST_RE = re.compile(r"\[(?:download|Merger)\]\s+(?:Destination:|Merging formats into)\s+\"?(.+?)\"?$")


def run_download(job_id, url, cmd):
    """yt-dlp を回し、進捗を JOBS に書き戻す。"""

    def upd(**kw):
        with JOBS_LOCK:
            JOBS[job_id].update(kw)

    upd(status="starting", progress=0.0, message="情報を取得しています")

    out_tpl = str(MEDIA / "%(title).80B-%(id)s.%(ext)s")
    argv = cmd + [
        "--newline",
        "--no-playlist",
        "--no-part",
        "--restrict-filenames",
        "--no-color",
        "--merge-output-format", "mp4",
        "-f", FORMAT,
        "-o", out_tpl,
        "--print", "after_move:FINAL\t%(filepath)s\t%(id)s\t%(title)s\t%(duration)s\t%(webpage_url)s",
        url,
    ]

    final = None
    tail = []
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except Exception as e:
        upd(status="error", message="yt-dlp を起動できません: %s" % e)
        return

    with JOBS_LOCK:
        JOBS[job_id]["_proc"] = proc

    for line in proc.stdout:
        line = line.rstrip("\n")
        tail.append(line)
        if len(tail) > 40:
            tail.pop(0)

        if line.startswith("FINAL\t"):
            final = line.split("\t")
            continue

        m = PROGRESS_RE.search(line)
        if m:
            try:
                upd(status="downloading", progress=float(m.group(1)) / 100.0,
                    message="ダウンロード中")
            except ValueError:
                pass
            continue

        if "[Merger]" in line or "Merging formats" in line:
            upd(status="merging", message="映像と音声を結合しています")
        elif line.startswith("[info]"):
            upd(message="情報を取得しています")
        elif "has already been downloaded" in line:
            upd(message="すでに取得済みのファイルがあります")

    proc.wait()

    if proc.returncode != 0:
        why = "\n".join(l for l in tail if "ERROR" in l or "error" in l)[:500]
        upd(status="error", progress=0.0,
            message=why or "ダウンロードに失敗しました（URL と回線を確認してください）")
        return

    if not final or len(final) < 3:
        upd(status="error", message="ダウンロードは終わりましたが、ファイルを特定できませんでした")
        return

    path = Path(final[1])
    if not path.exists():
        vid = final[2] if len(final) > 2 else ""
        cand = [
            c for c in MEDIA.iterdir()
            if c.is_file() and c.suffix != ".json" and vid and vid in c.name
        ]
        if not cand:
            upd(status="error", message="保存先のファイルが見つかりません")
            return
        path = cand[0]

    try:
        duration = float(final[4]) if len(final) > 4 and final[4] not in ("NA", "", "None") else 0.0
    except ValueError:
        duration = 0.0

    info = {
        "id": final[2] if len(final) > 2 else path.stem,
        "title": final[3] if len(final) > 3 else path.stem,
        "duration": duration,
        "source_url": final[5] if len(final) > 5 else url,
        "file": path.name,
        "size": path.stat().st_size,
        "added": time.time(),
    }
    write_sidecar(path, info)
    upd(status="done", progress=1.0, message="完了", media=library_entry(path))


def library_entry(path: Path):
    info = {}
    sc = sidecar_path(path)
    if sc.exists():
        try:
            info = json.loads(sc.read_text(encoding="utf-8"))
        except Exception:
            info = {}
    st = path.stat()
    return {
        "id": info.get("id") or path.stem,
        "title": info.get("title") or path.stem,
        "file": path.name,
        "url": "http://%s:%d/media/%s" % (HOST, PORT, quote(path.name)),
        "size": st.st_size,
        "duration": info.get("duration") or 0,
        "source_url": info.get("source_url") or "",
        "added": info.get("added") or st.st_mtime,
    }


def library():
    out = []
    for p in sorted(MEDIA.iterdir()):
        if p.is_file() and p.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov", ".m4v"):
            out.append(library_entry(p))
    out.sort(key=lambda e: e["added"], reverse=True)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "CutboardHelper/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/media/" not in (self.path or ""):
            sys.stderr.write("  %s\n" % (fmt % args))

    # ---- helpers ----
    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Expose-Headers", "Content-Range, Content-Length, Accept-Ranges")

    def reply(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- routes ----
    def do_GET(self):
        route = urlparse(self.path).path

        if route == "/health":
            cmd = find_ytdlp()
            return self.reply({
                "ok": True,
                "version": "1.0",
                "ytdlp": bool(cmd),
                "ytdlp_version": ytdlp_version(cmd) if cmd else None,
                "media_dir": str(MEDIA),
                "count": len(library()),
            })

        if route == "/jobs":
            with JOBS_LOCK:
                jobs = [
                    {k: v for k, v in j.items() if not k.startswith("_")}
                    for j in JOBS.values()
                ]
            jobs.sort(key=lambda j: j.get("created", 0), reverse=True)
            return self.reply({"jobs": jobs[:50]})

        if route == "/library":
            return self.reply({"items": library()})

        if route.startswith("/media/"):
            return self.serve_media(unquote(route[len("/media/"):]))

        if route == "/":
            return self.reply({"ok": True, "hint": "カットボードから利用してください"})

        return self.reply({"error": "not found"}, 404)

    def do_POST(self):
        route = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self.reply({"error": "リクエストを解釈できません"}, 400)

        if route == "/fetch":
            url = (payload.get("url") or "").strip()
            if not re.match(r"^https?://", url):
                return self.reply({"error": "http(s) の URL を入れてください"}, 400)

            cmd = find_ytdlp()
            if not cmd:
                return self.reply({
                    "error": "yt-dlp が見つかりません。`pip install yt-dlp` で入れるか、"
                             "yt-dlp.exe をこのヘルパーと同じフォルダに置いてください"
                }, 503)

            job_id = uuid.uuid4().hex[:12]
            with JOBS_LOCK:
                JOBS[job_id] = {
                    "id": job_id, "url": url, "status": "queued",
                    "progress": 0.0, "message": "待機中", "created": time.time(),
                }
            threading.Thread(
                target=run_download, args=(job_id, url, cmd), daemon=True
            ).start()
            return self.reply({"job": job_id})

        return self.reply({"error": "not found"}, 404)

    def do_DELETE(self):
        route = urlparse(self.path).path
        if route.startswith("/library/"):
            name = unquote(route[len("/library/"):])
            target = (MEDIA / name).resolve()
            if MEDIA.resolve() not in target.parents or not target.exists():
                return self.reply({"error": "not found"}, 404)
            try:
                target.unlink()
                sc = sidecar_path(target)
                if sc.exists():
                    sc.unlink()
            except Exception as e:
                return self.reply({"error": str(e)}, 500)
            return self.reply({"ok": True})
        return self.reply({"error": "not found"}, 404)

    # ---- media with Range ----
    def serve_media(self, name):
        """Range 対応。これがないと <video> のシークが効かない。"""
        target = (MEDIA / name).resolve()
        try:
            if MEDIA.resolve() not in target.parents or not target.is_file():
                return self.reply({"error": "not found"}, 404)
        except Exception:
            return self.reply({"error": "not found"}, 404)

        size = target.stat().st_size
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False

        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                g1, g2 = m.group(1), m.group(2)
                if g1 == "" and g2:                 # bytes=-500 (末尾から)
                    start = max(0, size - int(g2))
                else:
                    start = int(g1 or 0)
                    if g2:
                        end = min(int(g2), size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.cors()
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()

        if self.command == "HEAD":
            return
        try:
            with target.open("rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # ブラウザがシークで接続を切るのは正常

    def do_HEAD(self):
        route = urlparse(self.path).path
        if route.startswith("/media/"):
            return self.serve_media(unquote(route[len("/media/"):]))
        self.send_response(200)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    cmd = find_ytdlp()
    print("カットボード ローカルヘルパー")
    print("  保存先 : %s" % MEDIA)
    if cmd:
        print("  yt-dlp : %s (%s)" % (" ".join(cmd), ytdlp_version(cmd) or "?"))
    else:
        print("  yt-dlp : 見つかりません")
        print("           `pip install yt-dlp` で入れるか、yt-dlp.exe をこのフォルダに置いてください")
    print("  待受   : http://%s:%d" % (HOST, PORT))
    print("  終了   : Ctrl+C")
    print()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")
        srv.shutdown()


if __name__ == "__main__":
    main()
