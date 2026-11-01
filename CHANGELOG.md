# 变更记录

## 1.0.0 — 2026-11-01

首个公开版本。

- 两路判据：镜头切点（ffmpeg scene）+ 8×8×8 RGB 颜色直方图
- 选帧五步：状态点优先 → 保首尾 → 覆盖优先 → 价值填满 → 近邻合并
- 特征一次解码（rawvideo 管道），不再逐帧写 PNG
- --doctor 环境自检、--selftest 无需素材的自测
- 输出 plan.json / manifest.md / 可选 sheets/*.png
- 实测：127 秒 MV → 31 帧 / 8 张 / ≈2042 token，含 1 秒软过渡插播
