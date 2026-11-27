#!/usr/bin/env python3
"""
環境診断 — これを実行して、出てきた内容をそのまま貼ってください。

    python check.py

Windows でダブルクリックした場合も動きます（最後に待機します）。
何も変更しません。読むだけです。
"""

import os
import platform
import shutil
import socket
import subprocess
import sys

OK, NG, WARN = "[ OK ]", "[ NG ]", "[ 注意 ]"
notes = []


def line(mark, label, value=""):
    print("%-8s %-22s %s" % (mark, label, value))


def run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else ""
    except Exception:
        return ""


print("=" * 60)
print(" カットボード 環境診断")
print("=" * 60)

# --- Python ---
v = sys.version_info
pyver = "%d.%d.%d" % (v.major, v.minor, v.micro)
if v >= (3, 9):
    line(OK, "Python", pyver)
else:
    line(NG, "Python", pyver + " （3.9 以上が必要）")
    notes.append("Python が古いので入れ直してください。")

line("", "実行ファイル", sys.executable)
line("", "OS", "%s %s (%s)" % (platform.system(), platform.release(), platform.machine()))

# Windows の Microsoft Store 版スタブはトラブルのもとなので警告
if "WindowsApps" in sys.executable:
    line(WARN, "入手元", "Microsoft Store 版の可能性")
    notes.append(
        "Microsoft Store 版の Python は制限が多く、うまく動かないことがあります。\n"
        "   python.org のインストーラで入れ直すのを勧めます"
        "（「Add python.exe to PATH」にチェック）。"
    )

# --- PATH 上の python ---
onpath = shutil.which("python") or shutil.which("python3")
if onpath:
    line(OK, "PATH上のpython", onpath)
else:
    line(WARN, "PATH上のpython", "見つかりません")
    notes.append(
        "PATH に python がありません。このスクリプトは動いていますが、\n"
        "   今後は毎回フルパスで指定する必要があります。\n"
        "   python.org のインストーラを再実行し、Modify から PATH を追加できます。"
    )

# --- pip ---
pip = run([sys.executable, "-m", "pip", "--version"])
if pip:
    line(OK, "pip", pip[:60])
else:
    line(NG, "pip", "使えません")
    notes.append("pip が無いと yt-dlp を入れられません。Python を入れ直してください。")

# --- yt-dlp ---
ydl = ""
how = ""
exe = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yt-dlp.exe")
if exe:
    ydl, how = run([exe, "--version"]), exe
elif os.path.exists(local):
    ydl, how = run([local, "--version"]), local
else:
    ydl = run([sys.executable, "-m", "yt_dlp", "--version"])
    how = "python -m yt_dlp" if ydl else ""

if ydl:
    line(OK, "yt-dlp", "%s  (%s)" % (ydl, how))
else:
    line(NG, "yt-dlp", "見つかりません")
    notes.append('yt-dlp を入れてください:  "%s" -m pip install yt-dlp' % sys.executable)

# --- ffmpeg ---
ff = shutil.which("ffmpeg")
if ff:
    ver = run([ff, "-version"])
    line(OK, "ffmpeg", ver[:60])
else:
    line(WARN, "ffmpeg", "見つかりません")
    notes.append(
        "ffmpeg が無くても最低限は動きますが、映像と音声の結合ができず\n"
        "   画質の選択肢が狭まります。Windows: winget install ffmpeg"
    )

fp = shutil.which("ffprobe")
line(OK if fp else WARN, "ffprobe", fp or "見つかりません")

# --- ポート ---
s = socket.socket()
try:
    s.bind(("127.0.0.1", 8787))
    line(OK, "ポート8787", "空いています")
except OSError:
    line(WARN, "ポート8787", "使用中")
    notes.append(
        "8787 が埋まっています。ヘルパーが既に起動しているなら問題ありません。\n"
        "   別アプリが使っている場合は cutboard-helper.py の PORT を変えてください。"
    )
finally:
    s.close()

# --- 保存先 ---
media = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media")
try:
    os.makedirs(media, exist_ok=True)
    probe = os.path.join(media, ".write-test")
    with open(probe, "w") as f:
        f.write("ok")
    os.remove(probe)
    line(OK, "保存先に書き込み", media)
except Exception as e:
    line(NG, "保存先に書き込み", str(e))
    notes.append("media フォルダに書き込めません。別の場所に置き直してください。")

# --- 判定 ---
print("-" * 60)
ready = bool(ydl) and v >= (3, 9)
if ready:
    print(" 準備OK。次はこれを実行してください:")
    print("     %s cutboard-helper.py" % ("python" if onpath else '"%s"' % sys.executable))
else:
    print(" あと少しです。下の「やること」を順に片付けてください。")

if notes:
    print()
    print(" やること / 注意:")
    for i, n in enumerate(notes, 1):
        print(" %d. %s" % (i, n))
print("=" * 60)

# ダブルクリック起動でも読めるように
if os.name == "nt" and not sys.stdin.closed:
    try:
        input("\nEnter で閉じます ")
    except Exception:
        pass
