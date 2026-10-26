# yt-dlp 控制台工具 (yt-dlp-controller)

基于 [Textual](https://textual.textualize.io/) 的 **yt-dlp 图形化终端界面**。

## 功能

- **新建下载**：粘贴一条或多条链接，解析标题 / 上传者 / 时长；播放列表显式展示条目数与范围（全部 / 仅当前 / 前 N 个），绝不静默合并成一条
- **格式选择**：10 种预设（最佳 / ≤4K / ≤2K / ≤1080p / ≤720p / ≤480p / 仅音频最佳 / MP3 / M4A / 自定义），可按完整格式表选择任意 `format_id`
- **任务队列**：实时进度条、速度、剩余时间、大小；暂停 / 继续（POSIX）、停止、重试、删除、详情与日志；顶栏「⚙ 并发」可直接调整同时下载数（1-8，立即生效）
- **下载历史**：与队列同一张表，按「全部 / 进行中 / 已完成 / 失败」筛选并支持模糊搜索；活跃任务优先排列，已完成 / 失败记录自动沉底
- **持久化**：配置 `config.yaml` + 队列 `queue.json` 原子写；重启后提示是否续传被中断的任务
- **后处理**：合并容器、字幕、嵌入元数据 / 封面、SponsorBlock、限速、cookies、代理、并发
- **网络适配**：自动跟随系统代理（或手动指定 / 强制直连）、使用系统证书库、跳过 HTTPS 证书校验，适配 Steamcommunity302 等中间人代理场景
- **进程管理**：每个任务独立进程组，取消同时结束 ffmpeg 等子进程；退出前清理全部子进程

## 环境要求

- Python >= 3.10
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) 在 `PATH` 中（或在设置里填写路径）
- `ffmpeg` / `ffprobe`（合并音视频、嵌入字幕 / 封面时需要）

## 安装

使用 pip 进行安装:
```bash
pip install yt-dlp-controller
```

## 启动

```bash
ytdlpctl                 # 使用默认配置目录
ytdlpctl -D /tmp/cfg     # 指定配置目录
ytdlpctl --yt-dlp /path/to/yt-dlp
ytdlpctl --version
```

配置目录优先级：`-D/--dir` > 环境变量 `YT_DLP_CONTROLLER_CONFIG_DIR` > `~/.config/yt-dlp-controller`。
首次启动会自动生成 `config.yaml` 与下载目录；配置值非法时回落到默认值并在状态栏提示，不会阻止启动。

## 键位

### 全局

| 键 | 行为 |
|---|---|
| `Ctrl+Q` | 任意界面退出（有未完成任务时先确认；确认框默认选中「继续下载」） |
| `Tab` / `Shift+Tab` | 焦点轮切 |

### 主菜单

| 键 | 行为 |
|---|---|
| `↑` / `↓` | 选择菜单项 |
| `Enter` | 进入 |
| `1` ~ `4` | 直达：新建下载 / 任务队列 / 设置 / 退出 |
| `Esc` / `Ctrl+C` | 退出程序 |

### 功能页（新建下载 / 队列 / 设置）

| 键 | 行为 |
|---|---|
| `Esc` / `Ctrl+C` | 返回上一页（优先关闭浮层与模态） |
| `Ctrl+R` | 刷新（焦点在输入框时让位给控件原生行为；新建下载页无刷新语义） |
| `Home` / `End` / `PgUp` / `PgDn` | 页面滚动（输入框聚焦时让位） |

### 新建下载

| 键 | 行为 |
|---|---|
| `Enter`（链接框内） | 解析链接 |
| `Ctrl+Enter` | 跳过格式选择，用默认预设直接加入队列 |
| `Ctrl+V` | 终端原生粘贴（输入框内） |
| 按钮 | 「解析」「加入队列」「查看全部格式…」「高级选项…」「粘贴并解析」 |

### 任务队列（含下载历史）

| 键 | 行为 |
|---|---|
| `↑` / `↓` | 选择任务 |
| `Enter` / 单击 | 任务详情（信息、日志尾部、复制命令） |
| `Ctrl+N` | 新建下载 |
| `Ctrl+P` | 暂停 / 继续（仅 POSIX 提供） |
| `Ctrl+X` | 停止任务（保留记录，可重试） |
| `Delete` | 删除任务（确认框默认「取消」） |
| `/` | 聚焦搜索框 |
| `Ctrl+R` | 刷新 |

控制行按钮为「暂停 / 停止 / 重试 / 删除任务」，按选中任务的状态自动点亮或置灰：进行中可暂停、停止；已结束（含已中断）可重试；停止后或排队中可删除。

顶部筛选按钮「全部 / 进行中 / 已完成 / 失败」即历史视图入口；任务按活跃程度排序（下载中 → 后处理 → 暂停 → 等待 → 中断 → 失败 → 完成 / 取消），同组内按创建时间倒序。

### 设置

| 键 | 行为 |
|---|---|
| 点击「编辑」 | 打开对应面板的表单（下载 / 网络与认证 / 后处理与环境） |
| `Ctrl+R` | 重新读取配置并刷新版本信息 |
| `PgUp` / `PgDn` | 滚动面板 |

### 模态

| 键 | 行为 |
|---|---|
| `Enter` | 提交表单 / 确认（危险确认框默认聚焦「取消」，需 `Tab` 后确认） |
| `Esc` / `Ctrl+C` | 取消 / 关闭当前层 |
| `Tab` / `Shift+Tab` | 表单字段轮切 |

## 配置

在工作目录下生成 `./config.yaml`，通常无需手动编辑，使用 TUI 内置设置页面即可：

```yaml
download_dir: ~/Downloads/yt-dlp
filename_template: '%(title)s [%(id)s].%(ext)s'
default_preset: best            # best|2160|1440|1080|720|480|audio_best|audio_mp3|audio_m4a（custom 仅逐任务）
merge_container: auto           # auto|mp4|mkv
max_concurrent_tasks: 2         # 1-8
concurrent_fragments: 4         # -N，0=不指定
rate_limit: ''                  # 如 5M，空=不限
retries: 10
cookies_file: ''
cookies_from_browser: ''        # chrome/firefox/edge/brave/...
auto_proxy: true                # 不传 --proxy，由 yt-dlp 自动读取 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY
proxy: ''                       # 仅在 auto_proxy: false 时生效；留空 = --proxy "" 强制直连
use_system_certs: false         # --compat-options no-certifi，改用系统信任库
no_check_certificates: false    # --no-check-certificates，完全不校验 HTTPS 证书
sub_langs: zh.*,en.*
embed_subs: false
embed_metadata: true
embed_thumbnail: false
sponsorblock_remove: ''         # 如 sponsor
ytdlp_path: ''
ffmpeg_path: ''
ignore_ytdlp_config: true       # 加 --ignore-config，避免用户 yt-dlp.conf 干扰
history_limit: 200              # 保留的已完成 / 已停止记录数
open_queue_after_add: true
```

- `cookies_file` 与 `cookies_from_browser` 同时存在时**文件优先**。
- `ignore_ytdlp_config` 默认开启，保证 TUI 参数可预测。
- 保存设置后立即生效，并发数会热更新；正在运行的任务保持原参数。
- `ytdlp_path` / `ffmpeg_path` 留空时，启动会用 `PATH` 探测结果自动填入并保存；配置里的路径失效时会自动回退到 `PATH`。
- 代理：`auto_proxy: true` 时 TUI **不传** `--proxy`，由 yt-dlp 自己读取环境变量（`HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`，含 `NO_PROXY`），设置页会显示检测到的值；关闭后总是传 `--proxy "<值>"`，留空即 `--proxy ""` 强制直连。
- 证书：`use_system_certs` 加 `--compat-options no-certifi`，让 yt-dlp 使用系统证书库；`no_check_certificates` 加 `--no-check-certificates` 完全跳过 SSL 校验。

## 队列与恢复

- 队列文件：`<配置目录>/queue.json`。
- 退出时若有未完成任务，会要求确认；确认退出会终止进程组，并把这些任务标记为 `interrupted`。
- 下次启动检测到 `interrupted` 任务时弹窗询问是否继续（保留 `.part` 断点续传）。
- 停止任务不会删除记录，状态保留为「已停止」，可随时重试。

## 常见问题

**提示「需要登录 / cookies」**
在「设置 → 网络与认证」填写 cookies 文件（推荐 Netscape 格式），或指定浏览器 cookies。

**403 / 429 被限流**
降低「并发任务」，或配置代理 / cookies 后重试。

**没有声音或没有画面**
在新建下载页改用「最佳画质（视频+音频）」，或从「查看全部格式…」里选带音频的格式。合并需要 `ffmpeg`。

**合并或嵌入封面失败**
安装 `ffmpeg` / `ffprobe`，或在设置里指定完整路径。

**HTTPS 证书校验失败（Steamcommunity302 等中间人代理）**
先开「设置 → 网络与认证 → 使用系统证书库」（`--compat-options no-certifi`），让 yt-dlp 读取系统信任的 302 根证书；仍失败再临时开启「跳过证书校验」（`--no-check-certificates`）。注意后者不校验任何证书，仅建议在可信网络下使用。

**设置里的代理不生效？**
「自动检测代理」开启时 TUI 不传 `--proxy`，完全以环境变量为准；要使用手填代理，先关闭该开关。关闭后留空代理表示强制直连，而不是「自动」。

**Windows 没有暂停按钮？**
暂停依赖 POSIX 的 `SIGSTOP` / `SIGCONT`，非 POSIX 平台隐藏该按钮与 `Ctrl+P`。

**下载目录里出现 `.part` 文件**
停止或中断后保留的断点文件，重试 / 继续时 yt-dlp 会自动续传。

**播放列表只下了一部分？**
播放列表默认展示条目数与范围；选择「前 N 个…」会写为 `-I 1-N`，选择「仅当前」会加 `--no-playlist`。

## 开发

目录结构：

```text
yt_dlp_controller/
├── main.py        # CLI：-D / --yt-dlp / --version
├── app.py         # App、状态栏、事件分发、启动恢复、退出保护
├── config.py      # 配置目录 / 默认值 / 校验回落 / 读写
├── ytdlp.py       # 参数构建、probe、行解析、错误归类、格式化
├── queue.py       # Task、QueueManager、进程组、持久化
├── ui.py          # PageScreen、首页 LOGO
├── widgets.py     # 模态、表格、HintBar、加载指示
├── app.tcss       # 全局样式（复用四项目 UI 规范）
└── screens/       # home / new_download / queue / settings / modals
```

界面规范与 Textual 8.2.8 实测依据见 `docs/textual-dev-guide.md`。

## 许可证

MIT
