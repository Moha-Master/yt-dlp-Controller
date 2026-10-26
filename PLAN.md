# yt-dlp-tui 开发计划（v1）

> 面向执行方（CodeBuddy）的施工图。实施前请先通读 §0~§3，再按 §9 的阶段顺序动手。
> 界面规范基准：`/tmp/opencode/yt-dlp-tui-ref/UI-SPEC.md`（摘自 `aliyun-controller/AGENTS.md` 第一部分，**必须逐条遵守**）。
> 可复用参考实现：`/tmp/opencode/yt-dlp-tui-ref/`（`ui.py` / `widgets.py` / `app.tcss` / `app.py` / `config.py` / `main.py` / `screens/`）。
> **禁止修改 `/webservices/aliyun-controller` 下的任何文件**（甚至不要 cd 进去写入）。

---

## 0. 目标与边界

在 `/webservices/yt-dlp-tui` 从零构建一个 Python + Textual 的 TUI，替代 yt-dlp 命令行完成：

- 粘贴链接 → 解析信息 → 选画质/格式 → 加入队列 → 后台并发下载 → 实时进度 → 完成/失败处理
- 队列支持取消、重试、暂停/继续、详情与日志查看
- 常用 yt-dlp 选项可视化（下载目录、命名模板、容器、仅音频、字幕、cookies、代理、限速、SponsorBlock）
- 配置与队列持久化，重启可恢复

**不做**（v1 明确排除）：账号登录体系、订阅/定时追踪、内置播放器、下载站点搜索、Windows 专属行为（代码可跨平台但不专门做 Windows 暂停）。

**竞争定位**：比命令行直观，比 GUI 轻量。核心体验 = **一条链接到开始下载不超过 3 次按键**。

---

## 1. 关键技术决策（已核实，勿推翻）

| 决策 | 结论 | 理由 |
|---|---|---|
| 调用方式 | **子进程调用 `yt-dlp` CLI**，不用 `import yt_dlp` | 版本解耦（用户可自行 `yt-dlp -U`）、崩溃隔离、无需打包依赖；Python API 会把大依赖与版本锁定塞进本项目 |
| 进度获取 | `--newline --progress-template 'download:PROG %(progress)j'` + `postprocess:PP %(progress)j` | **已实测**：2026.10.21 输出单行 JSON，`status/downloaded_bytes/total_bytes/speed/eta/_percent_str` 齐全，无 ANSI |
| 最终文件路径 | `--print 'after_move:FINAL %(filepath)s' --no-simulate` | **已实测**：必须带 `--no-simulate`，否则 `--print` 只模拟不下载 |
| 颜色 | `--color never`（代码里仍做 ANSI 兜底剥离） | 避免进度/日志污染解析 |
| 用户 yt-dlp 配置 | 默认 `--ignore-config`（设置项可关闭） | 保证 TUI 参数可预测；否则用户 `yt-dlp.conf` 会悄悄改写格式 |
| 进程组 | `start_new_session=True`，取消时 `os.killpg` | 连带杀掉 ffmpeg 等子进程，避免僵尸 |
| 暂停 | POSIX `SIGSTOP` / `SIGCONT` | yt-dlp 无原生暂停；冻结整个进程组最可靠。Windows 下隐藏该按钮 |
| 并发 | `asyncio.Semaphore(max_concurrent_tasks)`，默认 2 | 与 GUI 同类应用一致；过高易被站点限流 |
| 配置格式 | YAML（`config.yaml`）+ JSON（`queue.json`） | 与既有项目一致，人工可读可改 |
| 依赖下限 | `textual>=8.0`（本机已装 8.2.8），`PyYAML` | 复用 `aliyun-controller` 的 8.x 特性（compact Input、`overlay: screen`） |
| Python | `>=3.10`（本机 3.14.4） | 与既有项目一致 |

**环境注意**：本机 pip 全局配置的阿里云镜像已失效（http + 证书不匹配）。建 venv 装包必须显式走官方源：

```bash
python3 -m venv venv
./venv/bin/pip install -i https://pypi.org/simple --upgrade pip
./venv/bin/pip install -i https://pypi.org/simple -e ".[dev]"
```

---

## 2. 目录结构

```text
/webservices/yt-dlp-tui/
├── pyproject.toml            # 包元数据、入口脚本、ruff 配置
├── requirements.txt          # 同步 dependencies
├── README.md                 # 功能、安装、键位表、截图位
├── AGENTS.md                 # 第一部分=UI 规范原文；第二部分=本项目约定（阶段 3 写）
├── PLAN.md                   # 本文件
├── venv/                     # 本地虚拟环境（.gitignore）
└── ytdlp_tui/
    ├── __init__.py           # __version__ = "0.1.0"
    ├── main.py               # CLI: -D/--dir、--version、日志降噪、启动 App
    ├── app.py                # App 子类、StatusBar、全局服务 getter、退出保护
    ├── config.py             # 纯数据层：配置目录/加载/校验/保存/默认值
    ├── ytdlp.py              # 服务层：探测/版本/探测链接/构建参数/错误归类（零 Textual）
    ├── queue.py              # 服务层：Task 模型 + QueueManager（asyncio 子进程调度/事件）
    ├── ui.py                 # 复制并裁剪：PageScreen / LOGO_* / home_col_width
    ├── widgets.py            # 复制并裁剪：四模态 / ClickTable / busy / ProgressCover / HintBar …
    ├── app.tcss              # 复制后按本项目微调
    └── screens/
        ├── __init__.py
        ├── home.py           # 主菜单
        ├── new_download.py   # 新建下载（解析 + 预设 + 播放列表 + 高级选项）
        ├── queue.py          # 队列/历史（同一屏，按筛选切换）
        ├── settings.py       # 设置
        └── modals.py         # FormatsModal / TaskDetailModal / PlaylistModal
```

**分层铁律**（UI-SPEC §2）：`ytdlp.py` / `queue.py` / `config.py` **零 Textual 依赖、禁止 `print`**；异常统一向上抛，由 Screen 捕获后 `app.notify_err`。

---

## 3. 数据模型与持久化

### 3.1 配置 `config.yaml`

路径优先级：`-D/--dir` > 环境变量 `YT_DLP_CTL_CONFIG_DIR` > `~/.config/yt-dlp-ctl`。
`main()` 必须在 `import .app` **之前**写入环境变量（同 `aliyun-controller/main.py`）。

```yaml
download_dir: ~/Downloads/yt-dlp
filename_template: "%(title)s [%(id)s].%(ext)s"
default_preset: best            # 见 §4.2
merge_container: auto           # auto|mp4|mkv
max_concurrent_tasks: 2         # 1-8
concurrent_fragments: 4         # -N，0=不指定
rate_limit: ""                  # 如 "5M"，空=不限
retries: 10
cookies_file: ""
cookies_from_browser: ""        # 如 "chrome" / "firefox"
proxy: ""
sub_langs: "zh.*,en.*"
embed_subs: false
embed_metadata: true
embed_thumbnail: false
sponsorblock_remove: ""         # 如 "sponsor"；空=关闭
ytdlp_path: ""                  # 空=自动探测
ffmpeg_path: ""                 # 空=自动探测
ignore_ytdlp_config: true
history_limit: 200
```

校验：非法值回落到默认值并 `notify_warn`，**不要让坏配置阻止启动**（区别于 aliyun 的强制向导）。

### 3.2 任务 `queue.json`

```json
{"version": 1, "tasks": [{
  "id": "a1b2c3d4", "url": "...", "title": "...", "uploader": "...", "duration": 0,
  "preset": "best", "format_id": "", "container": "auto", "playlist_items": "",
  "status": "completed", "percent": 100.0, "speed": 0, "eta": 0,
  "downloaded_bytes": 0, "total_bytes": 0, "filepath": "...",
  "error": "", "error_hint": "", "cmd": ["yt-dlp", "..."],
  "created_at": 0, "started_at": 0, "finished_at": 0,
  "log_tail": ["..."]
}]}
```

- 原子写：写 `queue.json.tmp` → `os.replace`；仅在**状态迁移**和每 5s 节流写盘，不随进度写。
- 启动恢复：`running`/`postprocessing`/`paused` → 标为 `interrupted`，提示「N 个任务上次被中断，是否继续？」（继续=重置为 `queued` 并保留 `.part` 断点续传）。
- `log_tail` 最多 300 行（deque），只存摘要，不存完整日志文件。
- `history_limit` 裁剪最旧的 `completed`/`cancelled` 记录。

### 3.3 `Task` 状态机

```
queued ──▶ running ──▶ postprocessing ──▶ completed
  │           │  │            │
  │           │  └────────────┴──▶ failed
  │           ├──▶ paused ──▶ running
  │           └──▶ cancelled
  └──▶ cancelled
（重启后 running/postprocessing/paused → interrupted → 用户确认 → queued）
```

---

## 4. yt-dlp 集成

### 4.1 参数构建（`ytdlp.build_args(task, settings) -> list[str]`）

固定前缀：

```text
yt-dlp --ignore-config --color never --newline --no-warnings
       --progress-template "download:PROG %(progress)j"
       --progress-template "postprocess:PP %(progress)j"
       --print "after_move:FINAL %(filepath)s" --no-simulate
       -P <download_dir> -o <filename_template>
```

> `--ignore-config` 仅在 `settings.ignore_ytdlp_config` 为真时加。

可变部分（严格按此映射，不允许自创）：

| 设置/任务字段 | 参数 |
|---|---|
| 格式预设（见 §4.2） | `-f <FORMAT>` |
| 仅音频类预设 | 追加 `-x --audio-format <fmt> --audio-quality 0` |
| `merge_container=mp4/mkv` | `--merge-output-format mp4|mkv`；`auto` 不加 |
| `concurrent_fragments>0` | `-N <n>` |
| `rate_limit` | `-r <rate>` |
| `retries` | `-R <n>` |
| `cookies_file` | `--cookies <path>` |
| `cookies_from_browser` | `--cookies-from-browser <browser>`（与上一项互斥，文件优先） |
| `proxy` | `--proxy <url>` |
| `sub_langs` + `embed_subs` | `--write-subs --sub-langs <langs>`；嵌入再加 `--embed-subs` |
| `embed_metadata` | `--embed-metadata` |
| `embed_thumbnail` | `--embed-thumbnail` |
| `sponsorblock_remove` | `--sponsorblock-remove <cats>` |
| 任务 `format_id` 非空 | `-f <format_id>`（覆盖预设的 `-f`） |
| 任务 `playlist_items` 非空 | `-I <spec>` |
| 播放列表全部/单个 | `--yes-playlist` / `--no-playlist`（默认按解析结果决定，见 §5.2） |

### 4.2 格式预设

| key | 界面文案 | `-f` | 额外 |
|---|---|---|---|
| `best` | 最佳画质（视频+音频） | `bv*+ba/b` | — |
| `2160` | ≤ 4K | `bv*[height<=2160]+ba/b[height<=2160]` | — |
| `1440` | ≤ 2K | `bv*[height<=1440]+ba/b[height<=1440]` | — |
| `1080` | ≤ 1080p | `bv*[height<=1080]+ba/b[height<=1080]` | — |
| `720` | ≤ 720p | `bv*[height<=720]+ba/b[height<=720]` | — |
| `480` | ≤ 480p | `bv*[height<=480]+ba/b[height<=480]` | — |
| `audio_best` | 仅音频（最佳） | `ba/b` | `-x` |
| `audio_mp3` | 仅音频（MP3） | `ba/b` | `-x --audio-format mp3` |
| `audio_m4a` | 仅音频（M4A） | `ba/b` | `-x --audio-format m4a` |
| `custom` | 自定义格式 ID | 由 FormatsModal 选择 | — |

### 4.3 链接解析（`ytdlp.probe(url, settings) -> ProbeResult`）

```text
yt-dlp -J --flat-playlist --playlist-end 200 --no-warnings --color never [cookies/proxy]
```

- 返回 dict 后归一为 `ProbeResult(kind="video"|"playlist", title, uploader, duration, thumbnail, entries, formats)`。
- 单视频：`formats` 取 `formats` 列表，过滤掉 `vcodec=none and acodec=none`，按 `height`/`tbr` 倒序。
- 播放列表：`entries` 取 `title`/`url`/`id`；总条目数用 `playlist_count`（若有）。
- **网络错误归一**：`DownloadError` 文本 → 给用户看的中文提示（见 §4.5）。
- 超时 60s；`asyncio.wait_for`。

### 4.4 运行与事件（`QueueManager`）

```python
proc = await asyncio.create_subprocess_exec(
    *args,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.STDOUT,
    start_new_session=True,
    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
)
```

逐行读取（`encoding="utf-8", errors="replace"`）：

| 行 | 处理 |
|---|---|
| `PROG {json}` | 更新 `percent/downloaded_bytes/total_bytes/speed/eta/filename`；`status=finished` 时置 `postprocessing`（若开了后处理）或等待退出码 |
| `PP {json}` | `status=postprocessing`；`postprocessor` 名写入日志 |
| `FINAL <path>` | 记录 `filepath` |
| 其他非空行 | 去 ANSI 后进 `log_tail`；命中 `ERROR:` 时记录 `error` |

退出码：
- `0` → `completed`（若没拿到 `FINAL`，用 `-o` 模板 + 目录扫描兜底，实在没有就留空并警告）
- 非 0 且用户取消过 → `cancelled`
- 非 0 → `failed`，`error_hint = classify_error(log_tail)`

**事件推送**：`QueueManager.events: asyncio.Queue[QueueEvent]`，`QueueEvent(kind, task_id, payload)`。Screen 侧用 `@work(exclusive=True)` 的常驻协程消费，**按 150~250ms 批量 drain**，避免每行进度都刷新 UI。

取消：`SIGTERM` 进程组 → 3s 后 `SIGKILL`；保留 `.part`（yt-dlp 默认断点续传）。
暂停：`SIGSTOP`；继续：`SIGCONT`；两者都作用于 `os.getpgid(pid)`。

### 4.5 错误归类（`classify_error` 返回中文提示，用于详情弹窗）

| 日志特征 | 提示 |
|---|---|
| `Sign in to confirm` / `cookies` | 需要登录：在设置里配置 cookies 文件或浏览器 cookies |
| `HTTP Error 403` / `429` | 被限流或拒绝：降低并发、配置 cookies 或代理后重试 |
| `Requested format is not available` | 该格式不可用：改用「最佳画质」或从格式列表重新选择 |
| `ffmpeg not found` / `ffprobe` | 缺少 ffmpeg：安装后重试（本机已有 `/usr/bin/ffmpeg`） |
| `Unsupported URL` | 链接不受支持：检查链接或更新 yt-dlp |
| `Unable to download webpage` / `timed out` | 网络问题：检查代理/网络后重试 |
| `Video unavailable` / `Private video` | 视频不可用或私有：确认链接与登录状态 |

---

## 5. 界面设计（严格套用 UI-SPEC）

### 5.1 HomeScreen（根屏）

- `Screen(classes="logo-page")` + `#home-main` 居中 + `#home-col`。
- Logo：`YT-DLP` 艺术字（每行 `rstrip()`，`LOGO_WIDTH = max(cell_len(line))`），版本号右对齐，窄屏（`width < LOGO_WIDTH + 6`）降级为 `YT-DLP TUI` 普通文本。
- 菜单（`OptionList` + 描述右对齐 + `MenuMarquee`）：
  1. 新建下载 —— 解析链接、选格式并加入队列
  2. 任务队列 —— 查看进度、暂停、取消、重试；下载历史与队列同屏，按状态筛选查看
  3. 设置 —— 下载目录、格式、网络与后处理
  4. 退出程序 —— 有任务时会先确认
- 键位：`↑↓`、`Enter`、`1-4`、`Esc`/`Ctrl+C` 退出、`Ctrl+Q` 全局退出。
- 底部 `HintBar`：`↑↓ 选择 · 回车 进入 · 1-4 直达 · Esc/Ctrl+C 退出`。

### 5.2 NewDownloadScreen（`PageScreen`）

```
#topbar: ◀ 返回 | 新建下载 / 副标题=当前预设        [粘贴并解析] [菜单 ▾]
#page:
  ┌ .panel ── 链接 ─────────────────────────────────────────┐
  │ 链接  [Input 单行，支持空格/逗号分隔多条]   [解析]        │
  │ 提示  支持视频、播放列表；播放列表会先列出条目            │
  └────────────────────────────────────────────────────────┘
  ┌ .panel ── 视频信息 ─────────────────────────────────────┐
  │ 标题 / 上传者 / 时长 / 条目数（kv-row）                   │
  │ 播放列表：[OptionList 前 20 条] 或 [Static 提示]          │
  └────────────────────────────────────────────────────────┘
  ┌ .panel ── 下载选项 ─────────────────────────────────────┐
  │ 预设 [Select]  容器 [Select]  音频格式 [Select]           │
  │ [查看全部格式…] [高级选项…]                               │
  │ 输出路径预览：/path/to/文件名（根据模板+标题渲染）        │
  └────────────────────────────────────────────────────────┘
  .filter-row: [加入队列 primary] [取消]
```

- `Enter`（焦点在链接框）= 解析；`Ctrl+Enter` = 直接以默认预设加入队列（快速路径）。
- 解析中：`spinner` + 禁用「解析/加入队列」；**不铺全屏遮罩**（解析通常 <3s，遮罩会打断输入）。
- 播放列表：
  - 检测到多条目时显示「检测到播放列表：N 个视频」，选项：`全部下载` / `仅当前` / `前 N 个…`（`InputModal` 输入数量，落到 `playlist_items="1-N"`）。
  - **绝不静默合并成单条**（这是 GUI 同类产品最大痛点）。
- 「查看全部格式…」→ `FormatsModal`（表格：ID/格式/分辨率/帧率/编码/大小/码率；`Enter`/单击选择；返回格式 ID 并把预设切到 `custom`）。
- 「高级选项…」→ `FormModal`：仅字幕、嵌入字幕、嵌入元数据、嵌入封面、SponsorBlock、限速、播放列表条目。留空/默认=继承全局设置。
- 加入队列后：`notify_ok("已加入队列")` + 自动跳转队列页（可在设置里关闭，默认开启）。
- 键位：`Esc` 返回、`Ctrl+R` 重置表单?（**不要**：刷新语义留空，避免误清）；`Ctrl+V` 原生粘贴。

### 5.3 QueueScreen（队列 / 历史共用）

```
#topbar: ◀ 返回 | 任务队列 / 副标题="2 进行中 · 1 等待 · 8 已完成"   [⚙并发] [菜单 ▾]
#page:
  ┌ .filter-row ─────────────────────────────────────────────┐
  │ [全部][进行中][已完成][失败]  /搜索  [新建] [暂停/继续]   │
  │ [取消] [重试] [移除] [打开目录]                           │
  └──────────────────────────────────────────────────────────┘
  ┌ .tbl ───────────────────────────────────────────────────┐
  │ #  标题                         状态    进度      速度  剩余  大小 │
  └──────────────────────────────────────────────────────────┘
  底部 HintBar: ↑↓ 选择 · 回车 详情 · Ctrl+N 新建 · Ctrl+P 暂停/继续 · Ctrl+X 取消 · Del 移除 · Ctrl+R 刷新 · / 搜索 · Esc 返回
```

- 表格：`make_table()` → `ClickTable`，`cursor_type="row"`，行 key = `task.id`，列宽 `fit_table_columns([4, 40, 9, 18, 9, 8, 9])`，`on_resize` 重算。
- 进度列：文本进度条 + 百分比，按状态着色（运行=primary、完成=ok、失败=err、暂停/中断=warn）。宽度固定，避免抖动。
- **增量更新**：`table.update_cell(task.id, "progress"|"speed"|"eta"|"size", value)`；仅「任务集合变化」时全量 `load_rows`。
- 状态筛选：按钮切换（不是 TabbedContent）；下载历史不是独立页面，而是队列页的一个筛选视图（`全部 / 进行中 / 已完成 / 失败`）。
- `Ctrl+P` 暂停/继续、`Ctrl+X` 取消（`Input` 聚焦时由控件自身处理剪切，不冲突）、`Delete` 移除（`ConfirmModal`，`default_yes=False`）、`Enter` 详情。
- 有任务运行中退出程序：`ConfirmModal("仍有 N 个任务进行中，退出将中断下载", default_yes=False)`。
- 底部状态栏显示汇总：「↓ 3.2 MiB/s · 2 进行中 · 1 失败」。

### 5.4 TaskDetailModal（sheet 式模态）

- 定宽 `≤104`、`height 88%`；标题栏 + `VerticalScroll` 内容 + docked `.btn-row`。
- 内容：kv 行（链接/状态/输出/大小/耗时/错误提示）+ 完整命令（只读 `Static`，可复制）+ 日志尾部（`TextArea(read_only=True)` 或 `Static`，自动滚到底）。
- 按钮：`[打开目录]` `[复制命令]` `[重试]` `[关闭]`；失败任务额外显示 `[复制错误]`。
- `Esc`/`Ctrl+C` 关闭；`Ctrl+Q` 仍退出程序。

### 5.5 SettingsScreen

三个 `.panel`（下载 / 网络与认证 / 后处理与环境），每个面板 `kv-row` 展示当前值 + 右下角「编辑」按钮 → `FormModal`（字段 5~8 个，符合「表单不拆多步」约定）：

- 下载：下载目录、文件名模板、默认预设（choice）、容器（choice）、并发任务（1-8）、片段并发、限速
- 网络：cookies 文件、浏览器 cookies、代理、重试次数
- 后处理与环境：字幕语言、嵌入字幕（switch）、嵌入元数据（switch）、嵌入封面（switch）、SponsorBlock、yt-dlp 路径、ffmpeg 路径、忽略用户 yt-dlp 配置（switch）
- 额外一行：`yt-dlp 版本 2026.10.21 · ffmpeg 8.0.1`（异步探测，失败显示「未找到」+ 提示）
- 面板底部按钮：`[检查 yt-dlp 更新]`（`yt-dlp -U`，`busy(busy=bar)`，失败只提示不崩）
- 保存后立即生效：`app.reload_config()` + `QueueManager.apply_settings()`（并发数热更新）。

### 5.6 键位总表（必须与 HintBar 一致）

| 场景 | 键 | 行为 |
|---|---|---|
| 全局 | `Ctrl+Q` | 任意界面退出（priority） |
| 全局 | `Tab`/`Shift+Tab` | 焦点轮切 |
| 主菜单 | `↑↓` `Enter` `1-4` | 选择/进入/直达 |
| 主菜单 | `Esc`/`Ctrl+C` | 退出程序 |
| 功能页 | `Esc`/`Ctrl+C` | 返回（浮层>模态>页面） |
| 功能页 | `Ctrl+R` | 刷新（编辑态禁用） |
| 功能页 | `Home/End/PgUp/PgDn` | 页面滚动（Input 聚焦让位） |
| 新建下载 | `Enter` / `Ctrl+Enter` | 解析 / 用默认预设加入队列 |
| 队列 | `Ctrl+N` | 新建下载 |
| 队列 | `Ctrl+P` | 暂停/继续 |
| 队列 | `Ctrl+X` | 取消任务 |
| 队列 | `Delete` | 移除任务 |
| 队列 | `Enter` | 任务详情 |
| 队列 | `/` | 聚焦搜索 |
| 模态 | `Enter` / `Esc`/`Ctrl+C` | 提交 / 取消 |

**禁止新增**：`q` 退出、`Ctrl+E/D` 列表操作（会被搜索框抢占）、单字母快捷键（有输入框的页面）。

---

## 6. 复用与改造清单

| 来源 | 处理 |
|---|---|
| `ref/ui.py` | 复制 `PageScreen`（含菜单浮层、滚动、编辑态让位、`set_title/set_subtitle`）+ `LOGO_*`/`logo_text`/`home_col_width`。**删除** `MonthPicker`、`QuotaBar`（本项目无账期场景） |
| `ref/widgets.py` | **整份复制**：`ConfirmModal` / `InputModal` / `FormModal` / `OutputModal` / `BusyOverlay`+`busy()` / `ProgressCover` / `ClickTable` / `make_table` / `load_rows` / `fit_table_columns` / `shorten` / `rcell` / `filter_fuzzy` / `HintBar` / `MenuMarquee` / `mask_secret` / `HINT_FORM*`。若保留 `ProgressCover` 必须保留 `App.get_loading_widget()` |
| `ref/app.tcss` | 整份复制，改主题色/logo 尺寸 / 新增 `.progress-cell` 等少量类。**不要**在 widget 内部再写 `DEFAULT_CSS` 覆盖全局 Button |
| `ref/app.py` | 保留 `StatusBar`、`status/spinner/notify_*`、`ENABLE_COMMAND_PALETTE=False`、`Binding("ctrl+q", ..., priority=True)`；替换为 `get_queue()` 单例 + `get_settings()` |
| `ref/config.py` | 改字段与默认值；增加「坏值回落」而非抛错 |
| `ref/main.py` | 复制参数模式（`-D/--dir`、`--version`、日志降噪）；`--yt-dlp PATH` 可选覆盖 |
| `ref/screens/settings.py` | 只借鉴「kv 面板 + 编辑弹窗」结构 |
| `ref/screens/dns.py` | 只借鉴表格重建/列宽/搜索/按钮模式，**不要**照抄业务 |

---

## 7. 关键陷阱（除了 UI-SPEC §12 通用坑，本项目额外注意）

1. **`--print` 必带 `--no-simulate`**，否则不下载（已实测踩过）。
2. 进度 JSON 行自带 `PROG ` 前缀；某些行可能被缓冲合并，解析时用 `line.startswith("PROG ")` 并 `json.loads(rest)`，`try/except` 丢弃坏行。
3. 子进程输出必须 `errors="replace"`，否则非 UTF-8 文件名会让读取协程崩掉。
4. 取消后**不要立即删除任务行**；保留 `cancelled` 状态可见，用户可重试。
5. `SIGSTOP` 会把 ffmpeg 一起冻结，恢复时注意进度时间跳变（用 `elapsed` 判断，不要用墙钟算速度）。
6. `DataTable.update_cell` 的 key 必须是加入时的 row key（任务 id），不要在刷新时重建表格导致 key 失效。
7. 进度刷新必须节流（150~250ms/批），否则宽终端下 CPU 飙升、Textual 队列积压。
8. `Table` 中富文本 `Text` 对象每次更新都新建，避免复用同一个可变对象。
9. 关闭程序：先 `QueueManager.shutdown()`（终止所有进程组）再 `app.exit()`，否则会留孤儿 yt-dlp。
10. 文件名模板预览只做字符串替换演示，**不要**自己实现 yt-dlp 模板引擎。
11. 打开目录用 `subprocess.Popen(["xdg-open", dir])`，macOS 用 `open`，Windows 用 `os.startfile`；失败只 `notify_warn`。
12. 配置写入前 `mkdir(parents=True, exist_ok=True)`；`download_dir` 展开 `~`。
13. 不要在 `on_mount` 里用 `self.is_mounted` 守卫 UI 写入（UI-SPEC §12 历史事故）。
14. 多 URL 输入先按 `[\s,]+` 拆分并去重，再逐条 `probe`；v1 只把**第一条**的解析结果用于选项面板，其余按同一预设入队（在提示里说明）。

---

## 8. 非功能要求

- 应用代码零 `print`（`ruff` 规则 + 人工复查）；调试走 `app.status()`。
- 所有阻塞调用（subprocess 探测、版本查询、`-U`）走 `asyncio.to_thread` 或 `asyncio.create_subprocess_exec`，包在 `@work` 里。
- 长任务不阻塞 UI：队列下载在 `QueueManager` 自己的 asyncio 任务中，不占 Screen worker 的 `exclusive` 名额。
- 退出时清理：timer、子进程、事件队列消费协程。
- 终端尺寸：120×40 与 80×28 都必须可用（窄屏时隐藏次要列：速度/剩余合并进详情）。
- 所有用户可见文案中文；代码注释中文；标识符英文。

---

## 9. 实施阶段（按顺序，每阶段可运行）

### 阶段 0：骨架跑起来（约 1/5 工作量）
1. `pyproject.toml`（name `ytdlp-tui`, 命令名 `yt-dlp-ctl`, module `ytdlp_tui`, entry `yt-dlp-ctl = ytdlp_tui.main:main`, deps `textual>=8.0`, `PyYAML`, Python `>=3.10`, ruff 同规范）。
2. `venv` + 安装（用 §1 的官方源命令）。
3. 复制 `ui.py` / `widgets.py` / `app.tcss` 并裁剪。
4. `config.py`、`main.py`、`app.py`（StatusBar + 全局 getter + 退出保护占位）。
5. `HomeScreen` + 两个占位页（队列/设置）。
6. **验收**：`./venv/bin/yt-dlp-ctl` 能启动、首页菜单可进出、`Ctrl+Q` 可退出、ruff 零告警。

### 阶段 1：能下载（MVP，约 2/5）
1. `ytdlp.py`：`find_ytdlp()` / `version()` / `build_args()` / `runner 行解析` 单元级函数。
2. `queue.py`：`Task`、`QueueManager.add/start/cancel/retry/shutdown`、信号量并发、事件队列、`queue.json` 持久化。
3. `QueueScreen`：表格 + 实时进度 + 取消/重试/移除 + 详情弹窗（先只放日志与命令）。
4. `NewDownloadScreen`：**单 URL + 默认预设 + 加入队列**（格式选择可先只有预设下拉）。
5. **端到端验收**（必须做，不依赖外网）：
   ```bash
   mkdir -p /tmp/opencode/ytdlp-e2e && cd /tmp/opencode/ytdlp-e2e
   head -c 3000000 /dev/urandom > sample.bin
   python3 -m http.server 8733 &
   # TUI 里或 headless 测试里加入 http://127.0.0.1:8733/sample.bin
   ```
   断言：任务从 `queued → running → completed`、进度递增、文件落盘、`queue.json` 有记录、取消能立即停止。

### 阶段 2：完整交互（约 1.5/5）
1. `probe()` + 信息面板 + 播放列表条目/全部/前 N + 多 URL 批量入队。
2. `FormatsModal` + 自定义格式 ID。
3. 高级选项 `FormModal`、每任务覆盖全局设置。
4. 暂停/继续（非 Windows）、失败错误归类提示。
5. 设置页三面板 + 版本探测 + 检查更新。
6. 历史筛选、重启恢复（`interrupted` 提示续传）。
7. 退出前运行中任务确认。

### 阶段 3：打磨（约 0.5/5）
1. 窄屏适配（隐藏速度/剩余列）、列宽重算、进度列不抖动。
2. `README.md`（功能、安装、键位表、常见问题）+ `AGENTS.md`（第一部分原文 + 本项目第二部分）。
3. `.gitignore`（venv、`__pycache__`、`*.egg-info`）。
4. 全量验证脚本通过、ruff 零告警。

---

## 10. 验证规范（必须自己跑，不许只靠肉眼）

在 `/tmp/opencode/` 写**一次性** headless 脚本（不入库），用 `App.run_test(size=(120,40))` 与 `(80,28)` 各跑一遍：

```python
# /tmp/opencode/ytdlp_tui_check.py 参考骨架
import asyncio, tempfile, os
os.environ["YT_DLP_CTL_CONFIG_DIR"] = tempfile.mkdtemp()
from ytdlp_tui.app import YtdlpTuiApp
from ytdlp_tui.queue import QueueManager

async def main():
    app = YtdlpTuiApp()
    app._queue = FakeQueue()          # 带状态、按脚本推进事件
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.screen.__class__.__name__ == "HomeScreen"
        await pilot.press("2")        # 进队列
        await pilot.pause()
        # 表格列宽和 ≈ region.width - 2；#page 无外层滚动；Esc 层级；Ctrl+P/Ctrl+X/Delete
asyncio.run(main())
```

必测清单：
- [ ] 各页挂载无异常（宽/窄两尺寸）
- [ ] 首页 `1-4` 直达、窄屏 logo 降级
- [ ] `#page.virtual_size.height <= container_size.height`（外层不滚动）
- [ ] 表格列宽和 ≈ `region.width - 2`，无横向滚动条
- [ ] 单击行打开详情，**不重复压栈**
- [ ] 进度更新不重建表格（行 key 稳定）、速度/ETA 文案不抖
- [ ] 取消 → 状态 `cancelled` 且子进程确实退出（`ps` 校验）
- [ ] 暂停/继续仅在 POSIX 显示
- [ ] `Esc` 层级：模态 > 菜单 > 返回；`Ctrl+Q` 处处生效
- [ ] 设置保存 → `config.yaml` 正确 → 并发数热更新
- [ ] 重启恢复：`running` 变 `interrupted` 并给出续传选项
- [ ] `ruff check ytdlp_tui` 零告警
- [ ] 真实本地 HTTP 端到端下载成功

---

## 11. 交付定义（Definition of Done）

1. `./venv/bin/yt-dlp-ctl` 可启动，`--version`、`-D` 可用。
2. 阶段 1 的本地端到端用例跑通，阶段 2 功能全部可见可用。
3. `README.md` 键位表与实际 `BINDINGS` 一致（逐条核对）。
4. `ruff check` 零告警；代码内无 `print`。
5. 不改动 `/webservices/aliyun-controller`；不执行 `git commit`。
6. 在最终回复中列出：改动文件清单、验证命令与结果、已知限制/未做项。

---

## 12. 参考依据

- 同类 GUI 调研结论（功能优先级、播放列表/格式选择痛点）：本计划 §4/§5 已吸收
- UI 规范原文：`/tmp/opencode/yt-dlp-tui-ref/UI-SPEC.md`
- 实测环境：`yt-dlp 2026.10.21`、`ffmpeg 8.0.1`、`textual 8.2.8`、`Python 3.14.4`
