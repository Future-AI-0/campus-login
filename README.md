# 校园网自动登录

Windows 校园网登录工具，使用 **PySide6 / Qt** 提供界面，通过 **Playwright + Microsoft Edge** 完成网页认证。支持后台检测、自动填写账号、勾选协议、精确匹配运营商和确认连接，认证结束后关闭浏览器。

默认适配 `10.254.241.66` 的校园网门户，包含网关生成的新入口、SSO iframe 和运营商二次确认流程。其他门户需要调整入口、表单定位和认证结果判断。

## 下载与使用

在 [Releases](https://github.com/Future-AI-0/campus-login/releases/latest) 下载 Windows x64 版本；无需安装 Python，电脑需要 Microsoft Edge。

| 文件 | 使用方式 |
| --- | --- |
| `CampusLogin-Slim.zip` | 推荐。解压整个文件夹，运行 `CampusLogin.exe`，保持 `_internal` 在旁边。 |
| `CampusLogin-OneFile.zip` | 单文件版。只需 EXE，启动时先把依赖解压到临时目录，因此启动稍慢。 |
| `SHA256SUMS.txt` | 两个压缩包的 SHA-256 校验值。 |

1. 连接校园网 Wi-Fi 或网线，运行 EXE。
2. 填写账号、密码，从固定列表选择运营商；**自动保存到本机** 默认开启，修改立即写入配置。也可以导入一行 `账号|密码|运营商` 格式的 TXT。
3. **入口可达时静默自动登录** 默认开启，每 15 秒并行检测所有可用的 Wi-Fi 和有线网卡，无需选择网卡。两种连接都能访问校园入口时，固定优先以太网；只有以太网入口不可达才使用 Wi-Fi。
4. 关闭窗口后继续在系统托盘检测。单击托盘图标或右键选择 **打开窗口** 可恢复窗口，右键还可暂停、立即连接或退出。
5. 勾选 **开机启动（登录 Windows 后）**，下次登录 Windows 即可静默进入托盘。取消勾选即可移除启动项，无需管理员权限。

![主界面（示例账号）](docs/images/main-window.png)

程序运行后会在桌面创建或更新“校园网登录”快捷方式，快捷方式默认静默启动；配置不完整时显示设置窗口。开机启动由界面开关控制，新安装默认关闭。移动便携程序后，从新位置手动运行一次即可更新已启用的启动项。手动重复启动会打开已有窗口，开机重复启动保持静默，避免同时认证。

创建快捷方式在后台完成，先显示界面；重复启动先联系已有窗口，不再等待 PowerShell 创建快捷方式。希望启动快时优先使用精简目录版，单文件版仍需要先解压依赖。

## 自动登录行为

- 自动登录始终隐藏浏览器，使用已保存的配置。默认情况下，账号、密码和运营商的修改立即自动保存；取消自动保存时，界面草稿不会用于后台登录。
- 失败按 60、120、240、300 秒的间隔重试，之后最多每 5 分钟尝试一次；入口断开后重新可达时立即再试。
- 每次认证只提交一次账号并等待明确结果。已在线时不再提交账号，也不切换当前运营商。
- 明确的账号密码错误或验证码要求会暂停自动登录；点击 **取消** 也会暂停。
- 并行检查所有活动的 Wi-Fi 和有线物理网卡，逐张判断校园入口可达性；两种连接均可达时固定优先以太网，与系统默认路由、网卡枚举顺序及 Wi-Fi 在线状态无关。以太网已联网时跳过认证，不再登录 Wi-Fi；以太网尚待确认或正在冷却时也不切到可达的 Wi-Fi。以太网入口不可达后自动使用 Wi-Fi。入口不可达的热点不参与认证。
- DNS 查询和 HTTPS 连接同时绑定对应网卡及其 IPv4 地址。读取各网卡自己的 DNS，避免系统 DNS 返回 TUN 假地址；验证百度或腾讯的 TLS 证书和 HTTP 200，不跟随跳转。在线状态只采用选定通道的证据。
- 待机时，每张网卡探测百度／腾讯共享 **1.5 秒**的 DNS 和 HTTPS 总预算；入口探测仍最多等 3 秒，实际登录前后联网验证保留 6 秒预算，认证超时由高级设置独立控制。多张卡并行探测。超时、站点不可达和 DNS 异常均表示“状态待确认”，不表示未登录。已联网后一次探测失败不触发认证，连续两轮未确认才重新检查门户。每张卡独立冷却和重试，状态恢复后清除旧错误提示。
- 门户在线结果和外网连通性分别判断。门户显示在线但外网尚未验证时显示待确认；运营商认证失败仍优先于基础在线状态。HTTP 555 不直接表示已在线，也不单独证明登录失败。
- 遇到 555 时尝试获取新的登录入口，支持网关的 HTTP `Location` 和短 HTML / JavaScript 跳转。Windows 上，这个请求使用通往校园网门户的网卡，避免被热点或 TUN 带走；只接受原校园门户的 `/portal/` 或 `/eportal/index.jsp` 入口。每次认证最多刷新一次，不发送账号到跳转网关，不修改系统路由或代理。
- 获取到有效的新入口后，完整地址及对应网卡地址保存在本机 `settings.json`；仅相同网卡地址可复用客户端参数，跨网卡扫描使用基础入口。不会把地址中的客户端参数写入日志或发布包。
- 网络检测、入口刷新和浏览器认证使用同一个所选网卡及 IPv4 地址。浏览器认证期间使用仅监听回环地址的临时转发，只接受原门户的 HTTP/HTTPS 请求；不修改系统路由、系统代理或其他浏览器。认证或取消结束后关闭监听和连接，HTTPS 保持端到端加密。
- 网卡断开或 IPv4 地址变化后忽略旧检测结果，不再复用旧客户端参数；自动寻找其他能访问入口的网卡。不修改系统的默认网卡或路由。
- SSO 登录后 iframe 被替换时，等待新的页面继续选择运营商，避免将正常跳转当成错误或重复提交密码。
- 托盘菜单使用白底深色文字、蓝底白字选中项。**打开窗口** 支持初始隐藏、关闭到托盘和最小化状态。

![托盘菜单](docs/images/tray-menu.png)

## 配置

只需要三个账号环境变量；也可以在程序旁的 `.env` 中保存：

```dotenv
CAMPUS_USERNAME='你的校园网账号'
CAMPUS_PASSWORD='你的校园网密码'
CAMPUS_OPERATOR='中国移动'
```

GUI 优先读取本机 `.env`，缺少的键才从环境变量读取，确保修改后后台登录使用新配置。CLI 保持进程环境变量优先。密码保留原始内容，包括空格。界面运营商只能选择 **中国移动、中国联通、中国电信、校园网**；导入支持 `移动`、`联通`、`电信` 别名，其他值会提示重新选择。网页选项无法唯一匹配时停止并显示可选项。

**高级设置** 可调整完整入口、等待时间和手动登录时是否显示浏览器。开关和高级设置保存在 EXE 旁的 `settings.json`，账号也始终保存在 EXE 旁，单文件版不会把个人配置写入临时解压目录。

入口对应的客户端地址保存在 `settings.json`，账号仍只需要原来的三个环境变量。桌面界面自动探测所有可用物理网卡，入口都可达时固定优先以太网；以太网入口不可达时自动使用 Wi-Fi，无需手动选择。CLI 的 `--network wifi` 或 `--network ethernet` 可用于诊断指定通道。

**自动保存到本机** 默认开启，账号输入、密码修改、运营商选择和导入都会立即保存；写入完整快照后再替换 `.env`，避免后台读到混合配置。清空字段也会保存，阻止继续使用旧凭据。仍可点击 **保存配置** 手动保存。取消自动保存仅停止当前窗口的自动写入，不删除已有文件。源码仓库和 Release 均不包含个人账号文件、`.env` 或 `settings.json`。

开机启动写入当前用户 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 中本程序的 `CampusLogin` 项，使用 `--background --startup` 参数；开关显示实际 Windows 启动项状态。

默认入口（失效时会尝试从校园网网关重新获取，也可粘贴校园网生成的完整入口）：

```text
http://10.254.241.66/portal/entry/pc/authenticate;flowParams=undefined;from=
```

## 从源码运行

需要 Windows、Python 3.10+ 和 Microsoft Edge。在 PowerShell 中执行：

```powershell
git clone https://github.com/Future-AI-0/campus-login.git
cd campus-login
powershell -ExecutionPolicy Bypass -File .\setup.ps1
.\.venv\Scripts\python.exe .\gui.py
```

也可以双击 `gui.cmd`。从源码运行时，桌面快捷方式会使用该项目的 Python 虚拟环境。

命令行提供单次认证：

```powershell
# 使用项目 .env 或三个环境变量
.\.venv\Scripts\python.exe .\login.py

# 隐藏浏览器
.\.venv\Scripts\python.exe .\login.py --headless

# 明确使用无线校园网，检测与浏览器认证均走 Wi-Fi
.\.venv\Scripts\python.exe .\login.py --headless --network wifi

# 仅填入账号并勾选协议，不提交或选择运营商
.\.venv\Scripts\python.exe .\login.py --dry-run

# 延长等待；需要时替换为该门户新生成的完整入口
.\.venv\Scripts\python.exe .\login.py --timeout 90 --url 'http://10.254.241.66/portal/实际完整入口'
```

程序只在指定门户主机的页面中填写凭据。多个输入框或按钮匹配时停止，使用 Playwright 的定位和可操作性等待，不按屏幕坐标填写账号。

## 打包

安装源码依赖后执行：

```powershell
# 精简目录版：dist\CampusLoginSlim\CampusLogin.exe
powershell -ExecutionPolicy Bypass -File .\build.ps1 -CompactDirectory

# 单文件版：dist\Standalone\CampusLogin.exe
powershell -ExecutionPolicy Bypass -File .\build.ps1 -SingleFile
```

两个版本共用 `CampusLogin-slim.spec` 的依赖裁剪规则，移除未使用的 Qt OpenGL 软件渲染器、多语言资源、额外插件，以及 Playwright 类型声明、开发工具页面和非 Windows 安装脚本。保留 Qt Windows / 托盘 / 本机通信、Python HTTPS 和 Playwright 核心运行时；通过电脑已有的 Edge 操作网页。

构建时保留本机已有配置，但生成 ZIP 时只包含明确列出的发布文件，不打包个人配置。精简目录版约 58.9 MiB，单文件压缩包约 58.3 MiB；具体以 Release 附件为准。

## 验证与已知限制

已通过 96 项测试，覆盖 iframe、协议勾选、延迟出现的运营商选择、已在线、认证失败、超时、Qt 后台线程、静默触发、重试、取消、配置即时保存、输入期间避免提交半截密码、固定运营商、555 与离线状态冲突、网关入口刷新与保存、SSO iframe 替换、后台创建快捷方式、启动项读写与失败回退、托盘菜单配色、窗口恢复和跨进程单实例通信。还覆盖网卡与源地址绑定、TUN 假 DNS、TLS 主机验证、禁止公网跳转、站点回退、未知状态、恢复时清除错误、已联网时跳过浏览器，以及多网卡并行探测、以太网优先与 Wi-Fi 回退、独立重试、短时探测预算、地址变化清除旧入口、转发 POST/Cookie、HTTPS 隧道、限制目的主机和退出时清理监听。

还验证了 Windows 托盘右键入口的初始隐藏、关闭到托盘、最小化恢复，并在独立解压目录检查了发布包的 Qt、Edge、菜单、窗口恢复和配置路径。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

自动化测试使用模拟门户与测试账号，不会提交真实校园网账号。

另在实际校园网以太网连接上完成了从离线到登录的验证：旧入口在线查询返回失败并出现 555，通过校园网网卡获取新入口后完成账号提交、运营商选择和确认，门户在线接口返回 `success`。还通过同一网卡独立查询 DNS、访问公网 HTTPS 并验证服务器证书，确认联网。

已在线场景也进行了实际验证：旧入口仍出现 555，但有效入口查询返回 `success`，校园网网卡 HTTPS 检测通过；新版 GUI 清除旧错误并显示已联网，未开启浏览器或再次认证。

另验证了 Wi-Fi 和以太网同时连接的场景：以太网已经联网而无线仍需认证，旧版默认选择以太网。新版绑定无线网卡获取入口、提交账号、选择运营商并完成认证，随后通过同一无线网卡验证公网 HTTPS 联网成功；并行扫描能分别得到两张卡的结果。

555 在本校门户中是重新跳转信号，可能出现在已在线或离线时。v0.1.1 曾将其判为已在线，v0.1.2 在旧入口缺少参数时可能显示未确认提示；v0.1.3 在双网卡场景只选择默认通道且未绑定浏览器。请升级到 v0.1.4 或更新版本。外网探测失败仍不能排除网站故障、DNS 故障、IPv6 可用或特殊代理配置；此时保留待确认状态。当前网卡选择及绑定覆盖 Windows IPv4；特殊门户若跳转到其他认证主机或非标准端口，需调整受信任主机及端口范围。

依赖文档：[Qt for Python](https://doc.qt.io/qtforpython-6/)、[Playwright 定位](https://playwright.dev/python/docs/locators)、[Playwright 打包](https://playwright.dev/python/docs/library#pyinstaller)、[PyInstaller](https://pyinstaller.org/en/stable/)。Windows 网卡选择依据：[GetBestInterface](https://learn.microsoft.com/en-us/windows/win32/api/iphlpapi/nf-iphlpapi-getbestinterface)、[GetAdaptersAddresses](https://learn.microsoft.com/en-us/windows/win32/api/iphlpapi/nf-iphlpapi-getadaptersaddresses)、[IP_UNICAST_IF](https://learn.microsoft.com/en-us/windows/win32/winsock/ipproto-ip-socket-options)。
