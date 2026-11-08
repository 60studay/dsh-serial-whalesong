# 主题/外观定制接口

> 页面上那个「外观…」弹窗里的几项（背景图 / 透明度 / 主色 / 侧栏）**只是我们给的官方选择题**。
> 这份文档写的是**给别人的接口**：想怎么改都行，不用改我们的代码，也不用重新打包。
>
> 三条路，从简单到彻底：
> 1. 弹窗里的官方选项（够用就别往下看）
> 2. `POST /theme {"extra": {"--变量": "值"}}` —— 直接改任意 CSS 变量
> 3. `<运行期目录>/theme/custom.css` —— **任意 CSS**，页面最后加载，优先级最高

---

## 一、它存在哪

| 东西 | 位置 | 进仓库吗 |
|---|---|---|
| 外观设置 | `<runtime_root>/ui-theme.json` | ❌ 运行期状态 |
| 本地背景图 | `<runtime_root>/theme/*.png|jpg|webp|gif` | ❌ |
| 自定义 CSS | `<runtime_root>/theme/custom.css` | ❌ |

`runtime_root` 的解析顺序：`SERIAL_BRIDGE_RUNTIME_DIR` → `config.json` 的 `runtime_root` → `<log_root>/.runtime`。

**为什么不放进 `config.json`**：那是 git 跟踪的文件，改一次外观就弄脏工作区；而且它管的是"硬件怎么连"，
不该混进"长什么样"。

---

## 二、HTTP 接口

### `GET /theme`

```json
{
  "ok": true,
  "theme": {
    "bg_image": "", "bg_alpha": 1.0, "accent": "",
    "panel_alpha": 1.0, "side_image": "", "side_color": "", "side_alpha": 1.0,
    "fg_color": "",
    "timeline_color": "", "timeline_alpha": "",
    "console_color": "", "console_alpha": "",
    "composer_color": "", "composer_alpha": "",
    "extra": {}
  },
  "tokens": ["--bg", "--panel", "--fg", "..."]
}
```

`tokens` 是**公开的变量清单**（见第四节），写皮肤的照着改就行，不用猜名字。

### `POST /theme`

只传要改的键，其余保持不变：

```json
{ "accent": "#ff3366", "panel_alpha": 0.8,
  "extra": { "--tag-ai-bg": "rgba(255,51,102,.3)" } }
```

校验是**服务端**做的，不合格直接 400（带 `hint` 说怎么改）：

| 键 | 接受 | 拒绝 |
|---|---|---|
| `bg_image` / `side_image` | `http(s)://…`、`data:image/…`、`/theme/image/…` | `javascript:` 等一切别的前缀 |
| `bg_alpha` / `panel_alpha` / `side_alpha` | `0~1`，也收 `0~100`（当百分数） | 非数字、越界 |
| `accent` / `side_color` | `#rgb` / `#rrggbb`，或空串（回默认） | 其它写法 |
| `fg_color` | 文字颜色 `#rgb/#rrggbb`，或空串（回默认） | 其它写法 |
| `timeline_color` / `console_color` / `composer_color` | 同上，**空串 = 跟随面板统一色** | 其它写法 |
| `timeline_alpha` / `console_alpha` / `composer_alpha` | `0~1` 或 `0~100`，**空串 = 跟随 `panel_alpha`** | 非数字、越界 |
| `extra` | `--kebab-case` 变量名 → 字符串值；≤64 个，值 ≤300 字符 | 名字不像变量、值里有花括号/换行 |

### `POST /theme/image?name=你的文件名.png`

**请求体就是图片原始字节**（`Content-Type: application/octet-stream`），不是表单、不是 JSON。

```json
{ "ok": true, "url": "/theme/image/aeb410803c675b24.png" }
```

- 按内容 sha256 命名，同一张图不会存两份
- 只收 `png / jpg / jpeg / webp / gif`，上限 8 MB
- 磁盘只留**最近 8 张**
- 返回的 `url` 直接填给 `bg_image` / `side_image` 即可

### `GET /theme/image/<名字>`

只有 `^[0-9a-f]{16}\.(png|jpg|jpeg|webp|gif)$` 这种名字能取到；拼路径（`../`）一律 404。

### `GET /theme/custom.css`

返回 `<runtime_root>/theme/custom.css` 的原文；文件不存在就是 404（页面会自己摘掉 `<link>`）。

---

## 三、为什么浏览器不能直接读 `C:\...\bg.jpg`

浏览器安全模型禁止 http 页面加载 `file:///`（否则任何网页都能偷看你硬盘）。
所以"填个本地路径"必然失败——**上传**那条路（`POST /theme/image`）就是为了解决它。

---

## 四、可以改的变量

改这些就够了；改名等于破坏兼容，别改。

### 基础色

| 变量 | 作用 |
|---|---|
| `--bg` `--panel` `--fg` `--muted` `--line` `--line-strong` | 底色 / 面板 / 文字 / 次要文字 / 分隔线 |
| `--accent` `--accent-hover` | 主色（按钮、高亮、焦点框） |
| `--human` `--ai` `--paused` `--danger` | 语义色（人 / AI / 暂停 / 错误） |
| `--btn-face` `--btn-highlight` `--btn-shadow` `--hover-bg` | 次级按钮的"面"与悬停 |
| `--tx-fg` `--input-bg` `--overlay-bg` `--shadow-pop` | 命令文字 / 输入框底 / 遮罩 / 浮层阴影 |

### 徽章与胶囊

| 变量 | 作用 |
|---|---|
| `--tag-ai-bg` `--tag-human-bg` `--tag-auto-bg` `--tag-dev-bg` | 徽章底色（AI / 我 / 定时 / 设备） |
| `--tag-rule-bg` `--tag-rule-fg` `--tag-rule-line` | 规则徽章（默认红） |
| `--pill-human-bg` `--pill-ai-bg` `--pill-paused-bg` | 控制权胶囊底色 |

### 透明度与背景

| 变量 | 作用 |
|---|---|
| `--bg-alpha` `--bg-image` | 背景层的不透明度 / 图 |
| `--panel-alpha` | 面板（收发区 / 状态轨道 / 输入框 / 弹窗）不透明度 |
| `--timeline-color` `--timeline-alpha` | 状态轨道单独底色 / 不透明度 |
| `--console-color` `--console-alpha` | 串口收发窗口单独底色 / 不透明度 |
| `--composer-color` `--composer-alpha` | 输入指令窗口单独底色 / 不透明度 |
| `--fg` | 文字颜色 |
| `--side-color` `--side-alpha` `--side-image` | 侧栏单独底色 / 不透明度 / 图 |
| `--logo` | 鲸鱼图颜色（默认 `currentColor`，即跟随文字色） |

### 字体

| 变量 | 作用 |
|---|---|
| `--font-ui` | 界面字体栈 |
| `--font-mono` | 命令 / 回显 / 时间戳 |

---

## 五、例子

### 1) 换个主色 + 徽章配色

```bash
curl -X POST http://127.0.0.1:8898/theme -H "Content-Type: application/json" -d '{
  "accent": "#ff3366",
  "extra": {
    "--tag-ai-bg": "rgba(255,51,102,.25)",
    "--pill-ai-bg": "rgba(255,51,102,.18)"
  }
}'
```

### 2) 只改文字色 + 只把收发区透明

```bash
curl -X POST http://127.0.0.1:8898/theme -H "Content-Type: application/json" -d '{
  "fg_color": "#e6edf3",
  "console_color": "#0d1b2a", "console_alpha": 0.55
}'
```

### 3) 注意：`extra` 与官方项冲突时，官方优先

`extra` 里如果有跟官方项同名的变量（典型：`--accent`、`--panel-alpha`），
**页面上还是以官方项为准**（先套 extra、后设官方项）。
想完全接管就用 `custom.css` —— 它最后加载，没有任何优先级问题。

### 4) 半透明玻璃感

```bash
curl -X POST http://127.0.0.1:8898/theme -H "Content-Type: application/json" -d '{
  "bg_image": "https://example.com/bg.jpg",
  "panel_alpha": 0.55,
  "side_alpha": 0.7
}'
```

### 5) 彻底重画（custom.css）

把文件放到 `<runtime_root>/theme/custom.css`，例如：

```css
/* 直角、细边框、青色系 —— 想怎么写都行，这份文件页面最后加载 */
:root { --accent: #00d1b2; --line-strong: #00d1b2; }
button.ghost { border-radius: 0; }
header { border-bottom: 1px solid var(--accent); }
```

放好之后刷新页面即可；文件不存在页面就当没这回事（不会报错）。

### 6) 从电脑选图

页面里点「选图…」就是在用 `POST /theme/image`；脚本里也可以：

```bash
curl -X POST "http://127.0.0.1:8898/theme/image?name=bg.png" \
     -H "Content-Type: application/octet-stream" --data-binary @bg.png
# => {"ok":true,"url":"/theme/image/xxxxxxxxxxxxxxxx.png"}
```

---

## 六、我们守住的三条边界

1. **背景图只挂在专用背景层**（`body::before`，`z-index:-1`，`pointer-events:none`），
   不许写到 `body` 上再指望页头盖住它。
2. **页头品牌区（鲸鱼 + 「鲸歌」）自带不透明背景**，背景图与透明度**不作用于它**——logo 任何皮肤下都清楚。
3. **面板透明度用 `color-mix()`**：不支持的浏览器只丢那一条声明、退回不透明，
   绝不会出现"字看不见"。

---

## 七、恢复默认

```bash
curl -X POST http://127.0.0.1:8898/theme -H "Content-Type: application/json" -d '{
  "bg_image": "", "bg_alpha": 1, "panel_alpha": 1,
  "side_image": "", "side_color": "", "side_alpha": 1,
  "accent": "", "extra": {}
}'
```

或者页面「外观…」弹窗里点「恢复默认」。
