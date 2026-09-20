# 内嵌字体说明

Dashboard 是**自包含单文件**：它不引用任何外部字体 URL，而是把下面两个**开源字体子集**以 `data:font/woff2;base64` 的形式内嵌进 `output/dashboard.html`（由 `analyze.py` 的 `font_face_css()` 读取本目录文件生成）。这样线上 Demo 与本地离线打开看到的是同一套字形。

| 文件 | 字体 | 许可 | 用途 |
| --- | --- | --- | --- |
| `NotoSerifSC-subset.woff2` | Noto Serif SC（可变字重 100–900） | SIL Open Font License 1.1 | 中文/拉丁正文与标题（代替 kami 的商用字体 TsangerJinKai02） |
| `JetBrainsMono-subset.woff2` | JetBrains Mono | SIL Open Font License 1.1 | eyebrow、徽章、工单号等小标签 |
| `charset.txt` / `ascii.txt` | — | — | 子集化的字符集输入（本页用到的 646 个字符 + ASCII 可打印区） |

## 为什么不用 kami 的 TsangerJinKai02

kami 设计规范的中文主字体是 **TsangerJinKai02（仓耳今楷，商业授权）**，不能随公开仓库分发。字体栈因此按「授权字体优先 → 内嵌开源子集 → 系统衬线」排列：

```css
--serif: "TsangerJinKai02", "Kami Serif", Charter, Georgia,
         "Source Han Serif SC", "Noto Serif CJK SC", "Noto Serif SC",
         "Songti SC", "STSong", serif;
```

- `Kami Serif` = 本目录内嵌的 Noto Serif SC 子集（**kami 规范自己列的降级字体**，观感与仓耳今楷同属中文衬线）；
- 如果本机装了正版 TsangerJinKai02，浏览器会优先用它，视觉上就是 kami 的原始字体；
- 子集只包含本页出现的字符（646 个），所以文件只有 243KB；换数据/换文案后需重新子集化。

## 重新生成子集

```bash
# 1) 生成字符集（从已渲染的 dashboard.html 里提取所有可见文字）
python - <<'PY'
import re, pathlib
html = pathlib.Path("output/dashboard.html").read_text(encoding="utf-8")
chars = set("".join(re.findall(r">([^<>]+)<", html))) | set(chr(c) for c in range(32, 127))
pathlib.Path("assets/fonts/charset.txt").write_text("".join(sorted(chars)), encoding="utf-8")
PY

# 2) 子集化（需要 fonttools + brotli；用 uv 临时安装即可，不写入项目依赖）
uv run --no-project --with fonttools --with brotli python -m fontTools.subset \
  "C:/Windows/Fonts/NotoSerifSC-VF.ttf" --text-file="assets/fonts/charset.txt" \
  --flavor=woff2 --layout-features="*" --output-file="assets/fonts/NotoSerifSC-subset.woff2"

uv run --no-project --with fonttools --with brotli python -m fontTools.subset \
  "<kami>/assets/fonts/JetBrainsMono.woff2" --text-file="assets/fonts/ascii.txt" \
  --flavor=woff2 --layout-features="*" --output-file="assets/fonts/JetBrainsMono-subset.woff2"
```

字体文件缺失时 `analyze.py` 会自动跳过内嵌（回退到系统衬线），因此 CI 在任意环境都能跑通。

## 许可声明

Noto Serif SC 与 JetBrains Mono 均以 **SIL Open Font License 1.1** 发布，允许随文档/软件嵌入与再分发（要求保留版权与许可声明，且不得单独售卖字体）。原始项目：

- Noto Serif SC — <https://github.com/notofonts/noto-cjk>
- JetBrains Mono — <https://github.com/JetBrains/JetBrainsMono>
