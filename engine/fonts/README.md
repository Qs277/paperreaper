# 字体目录

本仓库**不附带字体文件**（字体版权归原作者所有，请自行确认授权）。请把下面 4 个字体放到本目录：

| 文件 | 用途 |
| --- | --- |
| `xiongdi.ttf` | 字母 / 数字 / 中文主字体 |
| `cicada.ttf` | `α ≥ ∑ √` 等数学符号 |
| `newleaf.otf` | 备用字形变体 |
| `qishan.ttf` | 备用字形变体 |

引擎会按文件名读取：

- `engine/api.py` 里 `FONT = fonts/xiongdi.ttf`、`FALLBACK = fonts/cicada.ttf`
- 想换字体，直接改 `engine/api.py` 顶部的 `FONT` / `FALLBACK` 即可。

缺少字体时，落笔会报错或回退到系统字体，字迹效果会明显变差。
