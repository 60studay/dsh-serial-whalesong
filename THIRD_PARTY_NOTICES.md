# 第三方组件与许可（Third-Party Notices）

本仓库自带（vendored）或引用的第三方组件，各自遵循其原许可。
**它们不改变本项目自身的许可**（本项目自 v2.1.0 起采用 PolyForm Noncommercial 1.0.0，见 `LICENSE`）。

| 组件 | 位置 | 版本 | 许可 | 用途 | 是否修改 |
|---|---|---|---|---|---|
| **pyserial** | `vendor/pyserial-3.5/` | 3.5 | **BSD-3-Clause** | 串口底层读写（免去用户自己 `pip install`） | 否，原样保留 |
| **JetBrains Mono** | `web/fonts/*.woff2` | Regular / Bold | **SIL Open Font License 1.1** | 命令与数字区等宽字体 | 否，仅做 woff2 子集 |
| **Noto / 思源**（可选） | — | — | OFL | 未引入（中文用系统字体栈） | — |

## pyserial（BSD-3-Clause）

```
Copyright (c) 2001-2020 Chris Liechti <cliechti@gmx.net>
All Rights Reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
... （完整条款见 vendor/pyserial-3.5/METADATA.txt 与本目录下各源文件头部）
```

> 说明：`vendor/pyserial-3.5/` 内每个源文件保留原始版权声明，**未做任何改动**。
> 插件运行时把它加入 `sys.path`，不复制、不改写。

## JetBrains Mono（SIL OFL 1.1）

```
Copyright 2020 The JetBrains Mono Project Authors (https://github.com/JetBrains/JetBrainsMono)
Licensed under the SIL Open Font License, Version 1.1.
```

> 完整许可文本见 `web/fonts/LICENSE-JetBrainsMono.txt`。
> 本仓库只把字体**子集化后随网页分发**（两个 woff2，共约 43 KB），未修改字形。

## 图标与图片

- `web/brand.png`（鲸鱼标记）、`icon.png`（插件图标）：**项目作者本人绘制**，
  随本项目一同按上述许可发布。使用 `mask` 方式着色，不单独分发彩色版本。

## 额外依赖（不随仓库分发）

以下由用户的 Python / DSH 环境提供，本仓库不打包：

- Python 3.8+（PSF License）
- DSH / DeepSeek Harness（其自身许可）

---

**如果你发现这里漏了什么，请提 issue。** 我们宁可多写一行，也不想在许可上含糊。
