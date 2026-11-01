# 排错：每个报错是什么意思，怎么改

> 排查顺序：**先 `--doctor`，再 `--selftest`，最后才怀疑素材**。
> 90% 的问题在前两步就能定位。

---

## 一、环境类

### `找不到 ffmpeg，请先安装 ffmpeg 并确保在 PATH 里`

**意思**：`shutil.which("ffmpeg")` 返回空。

**改法**：

| 系统 | 命令 |
| --- | --- |
| Windows | `winget install ffmpeg` / `scoop install ffmpeg` |
| macOS | `brew install ffmpeg` |
| Ubuntu | `sudo apt install ffmpeg` |

装完**新开一个终端**（PATH 变更对已开终端不生效）。

### `需要 numpy：pip install numpy`

**意思**：`import numpy` 失败。

**改法**：`pip install numpy`。
如果有多个 Python，确认装到了正在用的那个：`python -c "import sys; print(sys.executable)"`。

### `没装 Pillow，跳过出图`

**意思**：`--render` 需要 Pillow，但没装。

**改法**：`pip install pillow`；或者不传 `--render`（只出清单也够分析）。

### Windows 上跑 `python` 弹出应用商店

**意思**：`python` 指向了 Microsoft Store 的占位程序，不是真 Python。

**改法**：用真实路径调用，或改用 `py -3`：

```powershell
py -3 scripts\shot_sample.py video.mp4 --out out\
```

---

## 二、分析类

### `ffprobe 读不了这个文件`

**意思**：容器封装异常，或文件根本不是视频。

**改法**：先 `ffmpeg -i video.mp4` 看能否正常识别；能识别但 ffprobe 失败，
用 `ffmpeg -i in.mkv -c copy out.mp4` 转封装一次。

### `读不出时长` / 时长显示 0

**意思**：`ffprobe` 没拿 `duration`（常见于流式封装、某些 webm）。

**改法**：转封装后重跑；或 `ffmpeg -i x -c copy y.mp4`。

### `解码出来的帧太少`

**意思**：`ffmpeg` 输出到管道的原始帧数 <2。

**改法**：确认文件有视频流（`ffprobe -show_streams video.mp4`）；
如果只有音频，本工具不适用。

### `没选出任何帧`

**意思**：候选集为空——没有切点、也没有超过阈值的直方图变化。

**改法**：

```bash
python scripts/shot_sample.py v.mp4 --out out/ --hist-threshold 0.15
```

还不行就 `--no-cuts` 之外再检查：片子是不是几乎纯静止（比如一张图配长音频）？

### 只选出 1–2 帧

**意思**：`--max` 太小，或片子太静。

**改法**：`--max 24` 起；极静的片子靠 `--max-gap` 补点也有效。

---

## 三、质量类（能跑但结果不对）

| 现象 | 原因 | 改法 |
| --- | --- | --- |
| 漏掉某段画面 | 该段变化不够「剧烈」 | 降 `--hist-threshold` 到 0.15 |
| 漏掉不到 1 秒的插播 | 默认阈值偏高 | 降 `--hist-threshold` 到 0.18，并 `--max 40` 以上 |
| 同场景采了好几张 | 直方图对表情/字幕也敏感 | 提 `--hist-threshold` 到 0.28 |
| 全是黑帧/过渡帧 | 状态点后移不足 | 目前固定 +0.35 秒；特别慢的叠化可改代码里的 `0.35` |
| 快剪吃满预算 | 切点太密 | `--max 48` 或分段跑（见下） |
| 图上的时间戳是方块 | 系统缺中文字体 | 装中文字体；或改 `render_sheets()` 里的字体候选路径 |

### 分段跑（超快剪素材）

```bash
# 把 3 分钟拆成 3 段各跑一次，每段各自给预算
ffmpeg -ss 0   -t 60 -i v.mp4 -c copy p1.mp4
ffmpeg -ss 60  -t 60 -i v.mp4 -c copy p2.mp4
ffmpeg -ss 120 -t 60 -i v.mp4 -c copy p3.mp4
for f in p1 p2 p3; do python scripts/shot_sample.py $f.mp4 --out out_$f --max 16; done
```

---

## 四、性能类

| 现象 | 原因 | 改法 |
| --- | --- | --- |
| 分析很慢（>1 分钟/5 分钟片） | 机器慢或 4K 源解码慢 | 先转 720p 再分析：`ffmpeg -i 4k.mp4 -vf scale=-2:720 -c:v libx264 -crf 23 small.mp4` |
| `--render` 特别慢 | 每帧单独 seek 解码 | 只在需要时加；或先跑分析确认采样点，再单独渲染 |
| 内存占用高 | 全片 4fps 特征常驻内存 | 默认 64×36 很小；4K 长片仍可放心，实在不够就转小分辨率 |

---

## 五、提交 issue 时请附上

```
1. --doctor 的完整输出
2. 命令与完整输出（含「切点 N 个」「保留 N 帧」两行）
3. manifest.md 前 10 行（能看出阈值与两列数值）
4. 素材特征：时长、分辨率、大致类型（口播/动画/实拍/快剪）
5. 你期望采到但没采到的时间点（几秒处，发生了什么）
```

有这五项，基本不用来回猜。
