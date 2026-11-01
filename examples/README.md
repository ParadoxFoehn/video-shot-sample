# 样例产物

来自一条真实素材：127.15 秒动画 MV，1920×1080，65 个切点，30.6 秒处有一个 1 秒的软过渡广告插播。

- `sample-manifest.md`：抽帧清单（31 帧 / 8 张 / ≈2042 token）
- `sample-plan.json`：机器可读的计划

生成命令：

```bash
python scripts/shot_sample.py video.mp4 --out out/ --max 32 --render
```

注意：视频本身不在仓库里（版权与体积原因），这两个文件只用来对照输出格式。
