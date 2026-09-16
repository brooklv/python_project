---
name: uart-cmd-test
description: 通过串口测试 AM 826X/83XX 主机的 UART 协议 —— 跑功能验证、WiFi 老化循环、统计偶发失败率、手发十六进制调协议、往测试集里加新测试、读 JSON/JUnit 报告。当出现 UART、串口、ttyUSB、波特率、hexdump、老化/压测、Tx/Rx 配对、投屏、826X、83XX、uart_stress_test、AM 主机 等字样，或用户要验证串口命令、排查设备不回应时使用。
---

# UART 协议测试

驱动 `uart_stress_test.py`（纯标准库 + pyserial）对被测 AM 主机做功能验证、
老化测试和协议调试。

## 安全红线（先读这一段）

被测设备是真实硬件，有些命令**改不回来**或**会把命令通道弄死**：

- **`--tags danger`** —— 改 SSID/密码、执行 OTA 升级、恢复出厂、重启。
- **`--tags logmode`** —— 打开设备 log 后，**所有 UART 命令永久失效**，
  连"关闭 log"都发不进去，整轮测试就地死掉，只能物理重启设备。

**没有用户明确、逐次的确认，绝对不要跑这两组。** 它们不带任何其它 tag，
所以只有显式 `--tags danger` / `--tags logmode` 才会触发，不会被误选中。

另外两条默认值容易出事：

- **`-c` 默认是 100** —— 不给 `-c 1` 就会跑 100 遍。功能验证一律显式 `-c 1`。
- **`-l` 默认写 `test.log`** —— 会覆盖上一次的日志。多次运行要对比时给不同的 `-l`。

**验证串口通不通，永远先用这条**（只读查询，最安全，不需要 WiFi 凭据）：

```bash
python3 uart_stress_test.py -p /dev/ttyUSB0 --role rx --topology 1to1 --tags query -c 1
```

## 先决条件

工具本身只用标准库 + pyserial，**三个平台都能跑**。串口参数固定
**8N1 / 无流控**，波特率默认 115200。

**不接设备也能做的事**：`--list-tests` 列测试、`python3 -m unittest discover`
跑工具自身的回归测试。改了代码先跑这两个。

### 跨平台：`-p` 给什么，怎么装

**先列出实际存在的串口，不要猜端口名**（pyserial 自带，三平台通用）：

```bash
python3 -m serial.tools.list_ports -v
```

| | Linux / 树莓派 | macOS | Windows |
|---|---|---|---|
| `-p` 写法 | `/dev/ttyUSB0`、`/dev/ttyACM0` | **`/dev/cu.usbserial-XXXX`** | `COM3` |
| 装 pyserial | `sudo apt install python3-serial` | `pip3 install pyserial` | `pip install pyserial` |
| 权限 | 用户加入 `dialout` 组，**要重新登录** | 无需改权限 | 无需改权限 |
| 常见坑 | 插拔后编号会变 | 必须用 `cu.*`，**不能用 `tty.*`** | 缺驱动时设备管理器里是黄色感叹号 |

三条一定要记住的差异：

1. **macOS 必须用 `cu.*` 不能用 `tty.*`**。同一个设备两个节点都在，
   但 `tty.*` 打开时会阻塞等载波检测（DCD），表现是**程序卡死没有任何输出**，
   看起来像设备没回应。
2. **Linux 上别用 `pip install pyserial`** —— 树莓派等发行版会以
   PEP 668（`externally-managed-environment`）拒绝。用 apt，或建 venv。
3. **Windows 上命令是 `python` 或 `py -3`**，通常没有 `python3`。
   本文档里的 `python3 xxx.py` 要相应替换。

其它零碎：

- USB 转串口芯片（CH340 / CP210x / FTDI）在 macOS 和 Windows 上**可能要装驱动**；
  Linux 内核自带。设备根本没出现在 `list_ports` 输出里，先查驱动，别查代码。
- Windows 的 `COM10` 及以上在某些工具里要写成 `\\.\COM10`，**pyserial 会自己处理**，
  直接给 `COM10` 即可。
- 终端配色用 ANSI 转义，且只在 `isatty()` 时启用。Windows 10+ 的
  Terminal / PowerShell 正常；老的 `cmd.exe` 可能显示成乱码，重定向到文件则不受影响。

下面配方里的 `/dev/ttyUSB0` 按上表替换成当前平台的端口名。

## 定位工具

按顺序找 `uart_stress_test.py`：`$UART_TEST` → 本 skill 目录的上一层 → `command -v`。
找不到就问用户要路径，不要猜。

## 非交互驱动（重要）

两个地方会挂住或报错，agent 场景必须避开：

1. **`--role` / `--topology` 不给会提示选择**。管道里读不到输入时不会挂住，
   但会直接报错退出。**每次调用都显式给这两个参数。**
   拿不准就问用户，不要随便填 —— 填错的症状是一堆超时，看起来像设备坏了。
2. **`-i` / `--interactive` 是人用的菜单**，agent 不要用。要单发某条命令，
   用对应 tag 跑 `-c 1`。

## 常用配方

| 目的 | 命令 |
|---|---|
| 列出全部测试（不接设备） | `python3 uart_stress_test.py --list-tests` |
| 工具自身回归（不接设备） | `python3 -m unittest discover` |
| 验证串口通了 | `... -p /dev/ttyUSB0 --role rx --topology 1to1 --tags query -c 1` |
| 跑某一类功能 | `... --tags display -c 1` / `--tags audio,net -c 1` |
| WiFi 老化长跑 | `... --role rx --topology 1to1 -s "SSID" -w "PASS" -c 500 -r 3` |
| 出机器可读报告 | 追加 `--report-json run.json --report-junit run.xml` |
| 设备慢导致超时 | 追加 `-t 45` |
| 留现场排查 | 追加 `--no-restore`（跑完不恢复基线） |

`--tags` 多个取并集，被选中的测试**依赖会自动递归带上**。
不给 `--tags` 时跑 `core`（WiFi 老化主流程）。

`-s` / `-w` 只有**真会连路由**的测试才需要（判断依据是测试的 `needs_creds`
标记，不是 tag）。`--tags query` / `display` 不需要。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 全部成功 |
| 1 | 有测试失败，或被用户中止 |
| 130 | Ctrl+C（**中断也会写报告**，长跑靠 Ctrl+C 收尾是常规操作） |

## 什么时候读哪份参考

- 要跑测试、选 role/topology、读报告、排查失败 →
  [references/running-tests.md](references/running-tests.md)
- 要往 `uart_tests.py` 加测试、选断言工厂、用 `Ctx` →
  [references/test-authoring.md](references/test-authoring.md)

协议本身（包结构、命令码、错误码）见项目里的 `SPEC_extract.md` 和
`SPEC_AM_826X_83XX_uart_protocol_v1.8.xlsx`。
