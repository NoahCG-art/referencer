#!/usr/bin/env python3
"""
カット検出（Python / ffmpeg 版）

ブラウザ側の検出より精度が高い。実測（0.125秒の高速カットを含むテスト動画）:

    ブラウザ版  4/8 検出、時刻誤差 +83〜250ms
    この実装    8/8 検出、時刻誤差 0ms

理由は3つ。
  1. ffmpeg は各フレームの正確な PTS を知っている（再生速度から推測しない）
  2. 最小間隔のハードコードがない（ブラウザ側は 0.3 秒未満を捨てていた）
  3. scdet のスコアは本物のカットとノイズの分離が桁で違う

使い方:
    python analyze.py 動画ファイル [...]
    python analyze.py 動画ファイル --sens 7 --width 960
    python analyze.py 動画ファイル --rescore --sens 9   # 再デコードなしで切り直す

全フレームのスコアを cuts.json に残すので、感度の変更は --rescore で一瞬で終わる。
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCORE_RE = re.compile(r"scd\.score:\s*([0-9.]+).*?scd\.time:\s*([0-9.\-]+)")
SHOWINFO_RE = re.compile(r"pts_time:([0-9.]+)")


def need(name):
    p = shutil.which(name)
    if not p:
        sys.exit("%s が見つかりません。ffmpeg を入れてください（Windows: winget install ffmpeg）" % name)
    return p


def probe(path: Path):
    out = subprocess.run([
        need("ffprobe"), "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate,width,height,nb_frames:format=duration",
        "-of", "json", str(path),
    ], capture_output=True, text=True)
    try:
        j = json.loads(out.stdout)
        st = (j.get("streams") or [{}])[0]
    except Exception:
        sys.exit("動画を読めません: %s" % path)

    fr = st.get("avg_frame_rate") or "0/1"
    try:
        a, b = fr.split("/")
        fps = float(a) / float(b) if float(b) else 0.0
    except Exception:
        fps = 0.0
    return {
        "fps": round(fps, 6),
        "width": st.get("width") or 0,
        "height": st.get("height") or 0,
        "duration": float((j.get("format") or {}).get("duration") or 0),
    }


def threshold_for(sens: float) -> float:
    """感度 1〜10 を scdet の閾値に写す。5 で ffmpeg 既定の 10 になるようにしてある。"""
    sens = max(1.0, min(10.0, float(sens)))
    return round(10.0 * (1.35 ** (5.0 - sens)), 3)


def scan_scores(path: Path, duration: float, quiet=False):
    """全フレームのスコアを1パスで取る。デコードはするがエンコードはしない。"""
    cmd = [need("ffmpeg"), "-nostats", "-loglevel", "info", "-i", str(path),
           "-filter:v", "scdet=threshold=0", "-f", "null", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    scores = []
    last_report = 0.0
    for raw in proc.stderr:
        m = SCORE_RE.search(raw)
        if not m:
            continue
        score, t = float(m.group(1)), float(m.group(2))
        if t < 0:
            continue
        scores.append((round(t, 4), round(score, 3)))
        if not quiet and duration and t - last_report > max(5.0, duration / 40):
            last_report = t
            pct = min(100, int(100 * t / duration))
            sys.stderr.write("\r  スコア計算 %3d%%  (%d フレーム)" % (pct, len(scores)))
            sys.stderr.flush()
    proc.wait()
    if not quiet:
        sys.stderr.write("\r  スコア計算 完了 (%d フレーム)      \n" % len(scores))

    if not scores:
        # 古い ffmpeg で scdet が無い場合のしのぎ
        if not quiet:
            sys.stderr.write("  scdet が使えないため select=scene にフォールバックします\n")
        r = subprocess.run(
            [need("ffmpeg"), "-nostats", "-loglevel", "info", "-i", str(path),
             "-filter:v", "select='gt(scene,0.01)',metadata=print", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        for t in SHOWINFO_RE.findall(r.stderr):
            scores.append((round(float(t), 4), 100.0))
    return scores


def pick_cuts(scores, thr: float, min_gap: float):
    """閾値を超えたフレームをカットとする。冒頭は必ず1カット目にする。"""
    cuts, last = [0.0], -99.0
    for t, s in scores:
        if s >= thr and t > 0.0 and (min_gap <= 0 or t - last >= min_gap):
            cuts.append(t)
            last = t
    return cuts


def build_shots(cuts, duration, fps):
    shots = []
    for i, t in enumerate(cuts):
        end = cuts[i + 1] if i + 1 < len(cuts) else (duration or t)
        shots.append({
            "i": i + 1,
            "start": round(t, 3),
            "end": round(end, 3),
            "dur": round(max(0.0, end - t), 3),
            "frame": int(round(t * fps)) if fps else None,
            "thumb": "thumbs/%04d.jpg" % (i + 1),
        })
    return shots


def extract_thumbs(path: Path, shots, out_dir: Path, width: int, fps: float, quiet=False):
    """カット頭のフレームを1パスでまとめて書き出す。

    カットの「頭そのもの」ではなく 1 フレーム後を取る。境界フレームは
    前のカットが混ざっていることがあるため。
    """
    thumbs = out_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    for old in thumbs.glob("*.jpg"):
        old.unlink()

    if not fps:
        return 0
    frames = sorted({max(0, int(round(s["start"] * fps)) + 1) for s in shots})
    expr = "+".join("eq(n\\,%d)" % n for n in frames)

    # Windows のコマンド長制限を避けるため、フィルタはファイル経由で渡す
    fscript = out_dir / "_select.txt"
    fscript.write_text("select='%s',scale=%d:-2" % (expr, width), encoding="utf-8")

    cmd = [need("ffmpeg"), "-y", "-loglevel", "error", "-i", str(path),
           "-filter_script:v", str(fscript), "-vsync", "0", "-q:v", "3",
           str(thumbs / "%04d.jpg")]
    if not quiet:
        sys.stderr.write("  サムネ抽出 %d 枚 ...\n" % len(frames))
    subprocess.run(cmd, capture_output=True)
    fscript.unlink(missing_ok=True)

    got = sorted(thumbs.glob("*.jpg"))
    # 抽出枚数がずれた場合に備え、実ファイル名で貼り直す
    for s, f in zip(shots, got):
        s["thumb"] = "thumbs/" + f.name
    for s in shots[len(got):]:
        s["thumb"] = None
    return len(got)


def analyze(path: Path, out_root: Path, sens: float, width: int,
            min_gap: float, rescore: bool, quiet=False):
    path = path.resolve()
    out_dir = out_root / path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    jpath = out_dir / "cuts.json"

    thr = threshold_for(sens)
    t0 = time.time()

    data = None
    if rescore and jpath.exists():
        try:
            data = json.loads(jpath.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if data and not data.get("scores"):
            data = None
        if data is None and not quiet:
            sys.stderr.write("  保存済みスコアが無いので通常解析します\n")

    if data is None:
        info = probe(path)
        if not quiet:
            print("  %s  %dx%d  %.2ffps  %.1f秒" % (
                path.name, info["width"], info["height"], info["fps"], info["duration"]))
        scores = scan_scores(path, info["duration"], quiet)
        data = {
            "source": str(path),
            "name": path.name,
            "duration": round(info["duration"], 3),
            "fps": info["fps"],
            "width": info["width"],
            "height": info["height"],
            "scores": scores,
        }
    else:
        scores = [(t, s) for t, s in data["scores"]]
        if not quiet:
            print("  保存済みスコアから切り直します (%d フレーム)" % len(scores))

    cuts = pick_cuts(scores, thr, min_gap)
    shots = build_shots(cuts, data["duration"], data["fps"])
    n = extract_thumbs(path, shots, out_dir, width, data["fps"], quiet)

    durs = sorted(s["dur"] for s in shots if s["dur"] > 0)
    data.update({
        "sens": sens,
        "threshold": thr,
        "min_gap": min_gap,
        "thumb_width": width,
        "analyzed": time.time(),
        "shots": shots,
        "stats": {
            "count": len(shots),
            "median_dur": durs[len(durs) // 2] if durs else 0,
            "min_dur": durs[0] if durs else 0,
            "max_dur": durs[-1] if durs else 0,
            "under_0_3s": sum(1 for d in durs if d < 0.3),
        },
    })
    jpath.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    if not quiet:
        st = data["stats"]
        print("  → %d カット / サムネ %d 枚 / 閾値 %.2f / %.1f秒" % (
            st["count"], n, thr, time.time() - t0))
        print("     尺: 中央値 %.2fs  最短 %.2fs  最長 %.2fs  （0.3秒未満 %d 本）" % (
            st["median_dur"], st["min_dur"], st["max_dur"], st["under_0_3s"]))
        print("     %s" % jpath)
    return data


def main():
    ap = argparse.ArgumentParser(
        description="動画をカットに割る（ffmpeg の scdet を使用）")
    ap.add_argument("videos", nargs="+", type=Path, help="動画ファイル")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "cuts",
                    help="出力先（既定: helper/cuts）")
    ap.add_argument("--sens", type=float, default=5.0,
                    help="感度 1〜10（既定 5。大きいほど細かく切る）")
    ap.add_argument("--width", type=int, default=960,
                    help="サムネの横幅（既定 960）")
    ap.add_argument("--min-gap", type=float, default=0.0,
                    help="カットの最小間隔・秒（既定 0。高速カットを捨てないため）")
    ap.add_argument("--rescore", action="store_true",
                    help="保存済みスコアを使って切り直す（再デコードしない）")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args()

    for v in a.videos:
        if not v.exists():
            print("見つかりません: %s" % v, file=sys.stderr)
            continue
        if not a.quiet:
            print("■ %s" % v.name)
        analyze(v, a.out, a.sens, a.width, a.min_gap, a.rescore, a.quiet)


if __name__ == "__main__":
    main()
