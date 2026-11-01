---
name: video-shot-sample
description: 给视频抽「状态帧」：按镜头切换 + 颜色直方图突变选帧，产出 4 拼 1 图版与清单，用最少的帧说清整条片子发生了什么。当需要让模型看懂视频、做视频笔记、或嫌均匀抽帧漏内容/太贵时使用。
whenToUse: 用户丢来视频链接或本地文件问「这视频讲了什么 / 帮我总结 / 值不值得看」；或要给多模态模型准备帧序列、做视频笔记、检查自己剪的片子有没有漏段。
---

# video-shot-sample

## 维护

- **谁改**：阈值与默认值可直接改；改选帧策略前先跑 `--selftest` 与一条真实片回归。
- **同步与记录**：改算法同步 `README.md` 的「它是怎么工作的」与 `docs/ALGORITHM.md`；新坑记进 `docs/TROUBLESHOOTING.md`。

## 索引

| 要什么 | 去哪 |
| --- | --- |
| 主脚本 | `scripts/shot_sample.py` |
| 完整说明（给人读） | `README.md` |
| 算法推导 | `docs/ALGORITHM.md` |
| 阈值怎么调 | `docs/TUNING.md` |
| 实测数据 | `docs/MEASURED.md` |
| 排错 | `docs/TROUBLESHOOTING.md` |

---

## 这是什么

按「状态变化」给视频抽帧的工具。判据不是「哪段重要」，而是「**这一帧能不能被两侧的帧推出来**」：

- 镜头之内连续 → 一帧都不给
- 镜头切换 / 画风突变（插播、闪回、字幕卡）→ 给一帧
- 太久没采样 → 补一个点

产出：`plan.json`（机器读）+ `manifest.md`（人读）+ `sheets/*.png`（4 拼 1，帧带时间戳）。

## 怎么用

```bash
python scripts/shot_sample.py <视频> --out <目录> --max 32 --render
python scripts/shot_sample.py --doctor      # 环境检查
python scripts/shot_sample.py --selftest    # 不需要素材的自测
```

## 结果怎么读

1. 先看 `manifest.md` 里「直方图差」与「像素差」两列的分布，判断阈值合不合适
2. 再逐张看 `sheets/*.png`，记下每个关键时刻的时间
3. 需要某一段的过程帧时，对那一段单独密采：
   `ffmpeg -ss <起> -to <止> -i <视频> -vf fps=3.3 q_%03d.jpg`

## 边界（别越界承诺）

- 只给状态帧，不给过程帧
- 快剪素材会吃满预算（`--max 48` 起）
- 不发声音：转写与节拍要另接
- 字幕逐句滚动会被当成状态变化（刻意的，电影里字幕能单独承担信息）
