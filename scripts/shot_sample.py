#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shot-sample — 按「状态变化」给视频抽帧，产出 4 拼 1 图版 + 清单。

为什么要这样抽
--------------
人看视频不花钱，模型每读一帧都要付 token。所以真正该问的不是"哪一段重要"，
而是"这一帧能不能被两侧的帧推出来"：能推出来的区间一帧都不给，只有状态真的
变了才付一次费。

状态的边界有两路判据（缺一路就有盲区）：
  1) 镜头切换（硬切）——用 ffmpeg scene 检测；
  2) 画风/内容突变（软过渡、插播、闪回）——用 8x8x8 RGB 颜色直方图。
实测：一条 1 秒的淡入插播，像素差 54.89 低于一次镜头晃动的 68.23，只按像素差
永远选不到它；同一点的颜色直方图距离 0.595，是全段最高。

输出
----
  plan.json     机器读：时长、切点、采样点、张数、预算
  manifest.md   人读：帧号 | 时间 | 所属区间 | 像素差 | 直方图差 | 来源
  sheets/*.png  可选（--render）：每张 4 帧，帧下标时间戳

依赖
----
  必需：Python 3.8+、ffmpeg（含 ffprobe）、numpy
  可选：Pillow（只在 --render 时用）

用法
----
  python shot_sample.py video.mp4 --out out/                # 只出计划
  python shot_sample.py video.mp4 --out out/ --render       # 顺带出图
  python shot_sample.py video.mp4 --out out/ --max 32
  python shot_sample.py --doctor                            # 检查环境
  python shot_sample.py --selftest                          # 自测（生成合成片）
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

try:
    import numpy as np
except ImportError:
    sys.exit("需要 numpy：pip install numpy")

TOK_PER_SHEET = 224          # 实测：一屏 4 拼 1 ≈ 224 token
FEAT_W, FEAT_H = 64, 36      # 分析用缩略尺寸
ANALYZE_FPS = 4              # 分析帧率（够抓 0.25 秒级变化）
MIN_SHOT = 0.15              # 短于此的片段忽略

# ---------------------------------------------------------------- 基础工具


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def need(binary):
    if shutil.which(binary) is None:
        sys.exit("找不到 %s，请先安装 ffmpeg 并确保在 PATH 里" % binary)


def probe_media(path):
    """返回 (时长秒, 帧率, 宽, 高)。"""
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate,duration",
             "-show_entries", "format=duration", "-of", "json", path])
    if r.returncode != 0:
        sys.exit("ffprobe 读不了这个文件：%s" % (r.stderr or "").strip()[:200])
    info = json.loads(r.stdout or "{}")
    st = (info.get("streams") or [{}])[0]
    dur = st.get("duration") or (info.get("format") or {}).get("duration") or 0
    try:
        num, den = (st.get("r_frame_rate") or "0/1").split("/")
        fps = float(num) / float(den or 1)
    except Exception:
        fps = 0.0
    return float(dur), fps, int(st.get("width") or 0), int(st.get("height") or 0)


def detect_cuts(path, dur, threshold=0.30):
    """用 ffmpeg 的 scene 分数找硬切；返回时间点列表（秒）。"""
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
             "-filter:v", "select='gt(scene,%s)',metadata=print:file=-" % threshold,
             "-an", "-f", "null", "-"])
    cuts = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        # 形如：frame:12 pts:12000 pts_time:0.4
        if line.startswith("frame:") and "pts_time:" in line:
            for part in line.split():
                if part.startswith("pts_time:"):
                    try:
                        t = float(part.split(":", 1)[1])
                        if 0 < t < dur:
                            cuts.append(round(t, 3))
                    except ValueError:
                        pass
    return sorted(set(cuts))


def decode_features(path, dur, fps=ANALYZE_FPS):
    """一次解码：返回 (luma[n,h,w], hist[n,512])。

    用 rawvideo 管道直接读，避免把每一帧写成 PNG（IO 快一个数量级）。
    """
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
                        "-vf", "fps=%d,scale=%d:%d" % (fps, FEAT_W, FEAT_H),
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True)
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        sys.exit("解码失败：%s" % err[:200])
    raw = r.stdout
    frame_bytes = FEAT_W * FEAT_H * 3
    cnt = len(raw) // frame_bytes
    if cnt < 2:
        sys.exit("解码出来的帧太少（%d），检查输入文件" % cnt)
    arr = np.frombuffer(raw[:cnt * frame_bytes], dtype=np.uint8)
    arr = arr.reshape(cnt, FEAT_H, FEAT_W, 3).astype(np.float32)
    luma = arr.mean(axis=3)
    q = (arr // 32).astype(np.int32)
    idx = q[:, :, :, 0] * 64 + q[:, :, :, 1] * 8 + q[:, :, :, 2]
    flat = idx.reshape(cnt, -1).astype(np.int64)
    hist = np.zeros((cnt, 512), dtype=np.float32)
    np.add.at(hist.reshape(-1), (flat + (np.arange(cnt)[:, None] * 512)).ravel(), 1.0)
    hist /= np.maximum(1.0, hist.sum(axis=1, keepdims=True))
    return luma, hist


# ---------------------------------------------------------------- 选帧算法


def build_plan(luma, hist, cuts, dur, args):
    """返回 (采样时间列表, 明细行, 统计)。"""
    fps = ANALYZE_FPS
    n = len(luma)
    dpx = np.zeros(n)
    dh = np.zeros(n)
    dpx[1:] = np.abs(np.diff(luma, axis=0)).mean(axis=(1, 2))
    dh[1:] = 0.5 * np.abs(np.diff(hist, axis=0)).sum(axis=1)

    # 1) 状态变化点：颜色直方图的峰（软过渡也能抓）
    thr_h = max(args.hist_threshold, float(np.percentile(dh, 90)))
    changes, last = [], -9e9
    for k in range(1, n - 1):
        t = k / fps
        if dh[k] >= thr_h and dh[k] >= dh[k - 1] and dh[k] >= dh[k + 1] and t - last >= 0.6:
            changes.append(round(t, 2))
            last = t

    # 2) 候选：状态变化后 0.35 秒一帧；没有变化点的镜头按 45% 一帧
    bounds = [0.0] + [c for c in cuts if 0 < c < dur] + [dur]
    shots = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
    cand, used = [], set()
    for c in changes:
        pick = round(min(dur - 0.1, c + 0.35), 2)
        if pick not in used:
            used.add(pick)
            cand.append((pick, float(dh[min(n - 1, int(pick * fps))]), (c, pick), True))
    for s, e in shots:
        if e - s < MIN_SHOT or any(s - 0.3 <= c <= e + 0.3 for c in changes):
            continue
        pick = round(s + (e - s) * 0.45, 2)
        if pick in used:
            continue
        used.add(pick)
        cand.append((pick, float(dpx[min(n - 1, int(pick * fps))]), (s, e), False))
    cand.sort()
    if not cand:
        return [], [], {"cuts": len(cuts), "changes": len(changes), "hist_threshold": round(thr_h, 3)}

    # 3) 状态点优先（最多占一半预算，彼此至少 2.5 秒）
    budget = max(2, args.max)
    quota = max(2, budget // 2)
    state = sorted([c for c in cand if c[3]], key=lambda x: -x[1])
    chosen = []
    for t, _v, _seg, _s in state:
        if len(chosen) >= quota:
            break
        if all(abs(t - c) >= 2.5 for c in chosen):
            chosen.append(t)

    # 4) 保首尾：整片的首尾也要有帧，否则补点循环算不出空档
    edges = [round(min(cand[0][0], dur * 0.05) + 0.05, 2),
             round(max(cand[-1][0], dur * 0.95), 2)]
    for edge in edges:
        if 0 < edge < dur and len(chosen) < budget and all(abs(edge - c) >= 0.6 for c in chosen):
            chosen.append(edge)
    if len(chosen) < 2:                      # 极端情况兜底：至少两个端点
        chosen = sorted({round(dur * 0.05 + 0.05, 2), round(dur * 0.95, 2)})
    chosen.sort()

    def biggest_gap():
        best = None
        for x, y in zip(chosen, chosen[1:]):
            if best is None or y - x > best[2]:
                best = (x, y, y - x)
        return best

    def peak_in(t0, t1):
        a0 = max(0, int((t0 + 0.1) * fps))
        b0 = min(n, int((t1 - 0.1) * fps))
        if b0 <= a0:
            return (t0 + t1) / 2, 0.0
        seg = dpx[a0:b0]
        k = int(np.argmax(seg))
        return (a0 + k) / fps, float(seg[k])

    # 5) 覆盖优先：最大的洞先补；峰值太偏则退回中点
    base = float(np.median(dpx)) or 1.0
    remain = budget - len(chosen)
    while remain > 0:
        g = biggest_gap()
        if not g or g[2] <= args.max_gap:
            break
        x, y, span = g
        pt, pv = peak_in(x, y)
        rel = (pt - x) / span if span else 0.5
        if not (0.4 <= rel <= 0.6) and pv < 5.0 * base:
            pt = x + span * 0.5
        pt = round(pt, 2)
        if any(abs(pt - c) < 0.3 for c in chosen):
            break
        chosen.append(pt)
        chosen.sort()
        remain -= 1

    # 6) 价值填满：变化大且离已选帧远
    while remain > 0:
        best = None
        for t, v, _seg, _s in cand:
            if t in chosen:
                continue
            near = min(abs(t - c) for c in chosen)
            score = min(v, 100.0) * (near ** 0.5)
            if best is None or score > best[0]:
                best = (score, t)
        if not best:
            break
        chosen.append(best[1])
        chosen.sort()
        remain -= 1

    # 7) 近邻合并（<0.6 秒视为同一状态）
    merged = []
    for t in sorted(chosen):
        if merged and t - merged[-1] < 0.6:
            continue
        merged.append(round(t, 2))

    rows = []
    for t in merged:
        seg = next((c[2] for c in cand if abs(c[0] - t) < 0.02), None)
        src = "状态变化" if any(abs(t - (c + 0.35)) < 0.02 for c in changes) else "镜头"
        k = min(n - 1, int(t * fps))
        rows.append({"time": t, "seg": seg, "src": src,
                     "dpx": round(float(dpx[k]), 2), "dh": round(float(dh[k]), 3)})
    stats = {"cuts": len(cuts), "changes": len(changes),
             "hist_threshold": round(thr_h, 3)}
    return merged, rows, stats


# ---------------------------------------------------------------- 输出


def write_manifest(path, video, dur, kept, rows, stats):
    sheets = (len(kept) + 3) // 4
    tok = sheets * TOK_PER_SHEET + 250
    lines = [
        "# 抽帧清单 | %s | %.1fs | %d 帧 / %d 张 4拼1 / ≈%d token" % (
            os.path.basename(video), dur, len(kept), sheets, tok),
        "# 切点 %d ｜ 状态变化点 %d ｜ 直方图阈值 %.3f" % (
            stats.get("cuts", 0), stats.get("changes", 0), stats.get("hist_threshold", 0)),
        "# 帧号|时间|所属区间|像素差|直方图差|来源",
    ]
    for i, r in enumerate(rows, 1):
        seg = r["seg"]
        seg_s = "%.1f-%.1fs" % seg if seg else "-"
        lines.append("%d|%.2fs|%s|%.2f|%.3f|%s" % (
            i, r["time"], seg_s, r["dpx"], r["dh"], r["src"]))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def render_sheets(video, times, outdir, thumb_w=470):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("  ! 没装 Pillow，跳过出图（pip install pillow 后可 --render）")
        return []
    os.makedirs(outdir, exist_ok=True)
    font = None
    for cand in ("C:/Windows/Fonts/msyh.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/System/Library/Fonts/PingFang.ttc"):
        if os.path.isfile(cand):
            font = ImageFont.truetype(cand, 20)
            break
    tmp = tempfile.mkdtemp(prefix="shots_")
    paths = []
    try:
        for si in range(0, len(times), 4):
            chunk = times[si:si + 4]
            cell_h = int(thumb_w * 0.5625)
            sheet = Image.new("RGB", (2 * thumb_w + 30, 2 * cell_h + 90), (247, 248, 250))
            d = ImageDraw.Draw(sheet)
            for k, t in enumerate(chunk):
                p = os.path.join(tmp, "s%07.2f.png" % t)
                run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(t),
                     "-i", video, "-frames:v", "1", "-vf", "scale=%d:-1" % thumb_w, p])
                if not os.path.isfile(p):
                    continue
                im = Image.open(p).convert("RGB")
                c, r = k % 2, k // 2
                x, y = 10 + c * (thumb_w + 10), 10 + r * (cell_h + 30)
                sheet.paste(im, (x, y))
                if font:
                    d.text((x, y + im.height + 4), "%.1fs" % t, font=font, fill=(26, 30, 38))
            q = os.path.join(outdir, "sheet_%02d.png" % (si // 4 + 1))
            sheet.save(q)
            paths.append(q)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return paths


# ---------------------------------------------------------------- 环境与自测


def doctor():
    ok = True
    for b in ("ffmpeg", "ffprobe"):
        p = shutil.which(b)
        print("%-8s %s" % (b, p or "缺失"))
        ok = ok and bool(p)
    print("%-8s %s" % ("python", sys.version.split()[0]))
    for mod, hint in (("numpy", "pip install numpy"),
                      ("PIL", "pip install pillow（仅 --render 需要）")):
        try:
            __import__(mod)
            print("%-8s OK" % mod)
        except ImportError:
            print("%-8s 缺失（%s）" % (mod, hint))
            if mod == "numpy":
                ok = False
    print("\n结论：" + ("环境可用" if ok else "缺必需依赖，先补齐上面的项"))
    return 0 if ok else 1


def selftest():
    """生成一段合成片，跑完整流程，不依赖外部素材。"""
    need("ffmpeg")
    tmp = tempfile.mkdtemp(prefix="shot_selftest_")
    clip = os.path.join(tmp, "synthetic.mp4")
    print("① 生成合成片（12 秒）")
    r = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=12",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", clip])
    if r.returncode != 0:
        print("生成失败：" + (r.stderr or "").strip()[:200])
        return 1
    print("② 跑采样（--max 8）")
    main_run([clip, "--out", tmp, "--max", "8", "--quiet"])
    plan = json.load(open(os.path.join(tmp, "plan.json"), encoding="utf-8"))
    print("③ 结果：%d 帧 / %d 张 / ≈%d token" % (
        len(plan["samples"]), plan["sheets"], plan["token_est"]))
    ok = 3 <= len(plan["samples"]) <= 8 and plan["duration"] > 10
    print("自测" + ("通过" if ok else "失败（结果不合预期）"))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


# ---------------------------------------------------------------- 入口


def main_run(argv):
    ap = argparse.ArgumentParser(
        description="按状态变化抽帧，产出 4 拼 1 图版与清单（省 token 的视频理解前端）")
    ap.add_argument("video", nargs="?", help="视频文件")
    ap.add_argument("--out", help="输出目录")
    ap.add_argument("--max", type=int, default=36, help="总帧数上限（默认 36）")
    ap.add_argument("--max-gap", type=float, default=8.0, help="相邻采样点最大间隔秒数")
    ap.add_argument("--hist-threshold", type=float, default=0.22,
                    help="颜色直方图突变阈值（0-1，默认 0.22；漏插播就调小）")
    ap.add_argument("--cut-threshold", type=float, default=0.30,
                    help="scene 切点阈值（0-1，默认 0.30；切点漏检就调小）")
    ap.add_argument("--no-cuts", action="store_true", help="不做切点检测，只靠直方图")
    ap.add_argument("--render", action="store_true", help="出 4 拼 1 图（需要 Pillow）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    if not args.video or not args.out:
        ap.error("需要 video 与 --out")

    need("ffmpeg")
    need("ffprobe")
    dur, fps, w, h = probe_media(args.video)
    if dur <= 0:
        sys.exit("读不出时长：%s" % args.video)
    say = (lambda *a: None) if args.quiet else print
    say("时长 %.1fs ｜ %.0fx%.0f ｜ %.0f fps" % (dur, w, h, fps))

    cuts = [] if args.no_cuts else detect_cuts(args.video, dur, args.cut_threshold)
    say("切点 %d 个" % len(cuts))

    luma, hist = decode_features(args.video, dur)
    kept, rows, stats = build_plan(luma, hist, cuts, dur, args)
    if not kept:
        sys.exit("没选出任何帧：调小 --hist-threshold 或去掉 --no-cuts")

    os.makedirs(args.out, exist_ok=True)
    sheets = (len(kept) + 3) // 4
    tok = sheets * TOK_PER_SHEET + 250
    say("保留 %d 帧 → %d 张 4拼1 ｜ 预计 ≈%d token" % (len(kept), sheets, tok))
    say("采样点：" + ", ".join("%.1f" % t for t in kept))

    write_manifest(os.path.join(args.out, "manifest.md"), args.video, dur, kept, rows, stats)
    with open(os.path.join(args.out, "plan.json"), "w", encoding="utf-8") as f:
        json.dump({"video": os.path.basename(args.video), "duration": round(dur, 2),
                   "cuts": len(cuts), "changes": stats.get("changes", 0),
                   "samples": kept, "sheets": sheets, "token_est": tok},
                  f, ensure_ascii=False, indent=1)

    if args.render:
        for p in render_sheets(args.video, kept, os.path.join(args.out, "sheets")):
            say("  " + p)
    say("完成，产物在 " + os.path.abspath(args.out))
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--doctor":
        return doctor()
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        return selftest()
    return main_run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
