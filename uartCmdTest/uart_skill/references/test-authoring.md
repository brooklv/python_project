# 往测试集里加测试

测试集在 `uart_tests.py`，**加/删测试只改这个文件**。
执行机制（依赖、筛选、runner）在 `uart_exec.py`，一般不用动。

## 先选断言强度

四种现成工厂，按断言从弱到强。**不要一律用 `simple()`** —— 它发现不了
"设备回 `rc=SUCCESS` 但功能没生效"。

| 工厂 | 断言了什么 | 收发次数 | 什么时候用 |
|---|---|---|---|
| `simple()` | 设备没报错（`rc >= 0`） | 1 | 该命令**没有查询形式**，无法回读 |
| `query_int/str/hex()` | 返回值的范围、格式、长度 | 1 | 只读查询 |
| `set_and_verify()` | **设置真的生效了**（回读比对） | 2 | 该命令支持查询（`DataLen=FF FF`） |
| `set_and_restore()` | 生效了 **+ 跑完恢复原值** | 3~4 | 状态会**外溢**影响其它测试 |

### `simple()` —— 一行

```python
Test("静音 开",   simple(Cmd.AUDIO_MUTE, b"\x01"),       tags=("audio",)),
Test("区域码 CN", simple(Cmd.NET_AP_PARAM, b"\x06\x03"), tags=("net",)),
```

发命令 → 匹配应答 → 检查 `rc >= 0`。**只是冒烟测试。**
静音、区域码、投屏、配对、编码参数、系统命令属于这类（没有查询形式）。

### `set_and_verify()` —— 验证真的生效

```python
Test("旋转 90度",
     set_and_verify(Cmd.ROTATION, b"\x01", 1, names=ROTATION_ANGLE),
     tags=("display", "rotate")),
Test("缩放 120%",
     set_and_verify(Cmd.SET_OVERSCAN, b"\x78", 120, size=2),
     tags=("display", "zoom")),
```

设置 → 用同一条命令的查询形式（空 data）回读 → 断言值对上。
失败信息会带上含义：`✗ 旋转 90度: 设置了 1(90度) 但回读是 0(0度)`。

### `set_and_restore()` —— 跑完恢复

```python
# 切到 P2P 会让所有 WiFi station 测试失败，多轮循环时会毒害下一轮
Test("点对点模式 P2P",
     set_and_restore(Cmd.NET_ROLE, b"\x00", 0, names=NET_ROLE_NAME),
     tags=("net", "role")),
```

读原值 → 设置 → 回读断言 → 恢复原值。断言失败时**也会先恢复**，
不把设备留在半途。代价是每条 3~4 次收发，**只在真会外溢时用**。

目前用在：网络模式、HDMI 关、HDCP 禁用、log 全关、旋转 270 度。

### 查询类

查询应答的布局是 `rc(2) + [填充] + 值`。光检查 `rc` 会把返回值丢掉，
而返回值往往才是这条查询的意义所在。所以查询类要给解析参数：
枚举用 `names=`（`allowed=True` 可直接复用 `names` 的键当合法集合，
枚举不用写两遍）、偏移用 `offset=`、多字节用 `size=`、格式用正则。

结构还没搞清楚的，直接打十六进制留在日志里，比假装解析好。

## 自定义判定 —— 写个函数

```python
def t_scan(ctx):
    pkt = ctx.step(Cmd.NET_STA_CTRL, bytes([STA_SCAN]))
    ctx.scan_count = network_count(pkt)
    ui.log(f"✓ 扫描完成，发现 {ctx.scan_count} 个网络")
    if ctx.scan_count <= 0:
        raise TestFailed("扫描结果为 0 个网络，无法继续连接")

TESTS = [
    Test("扫描网络", t_scan, tags=("wifi", "core")),
]
```

**失败就 `raise TestFailed(原因)`**，原因会直接打进日志和报告。

测试是函数而不是纯数据表，是因为各条的成功判定是**逻辑种类**的差异，
不是参数的差异（切 5G 只看 `rc`；扫描要取个数并断言 > 0；取扫描结果要循环
收包到 `PACKET_DONE` 再拼接；连接要在应答里找 SSID，找不到只告警不失败）。
硬塞进配置表会长出一套比 Python 更难读的自定义语法。

**简单的 80% 用工厂声明式搞定，难的 20% 写代码。**

## `Ctx` 能用什么

| 方法 | 作用 |
|---|---|
| `ctx.step(cmd, data)` | 发命令 → 匹配应答 → 查返回码，失败抛 `TestFailed` |
| `ctx.send(pkt)` | 只发送（自己拼包时用） |
| `ctx.recv(timeout)` | 收一个包 |
| `ctx.recv_reply(want_id)` | 收匹配的应答，自动跳过异步通知 |
| `ctx.wait_optional(want_id, accept, timeout)` | 等可选包，收不到返回 `None` |
| `ctx.warn(说明)` | 打告警并停下来问，选中止抛 `Aborted` |
| `ctx.forget_all(阶段名)` | 忘记所有网络（清理用） |
| `ctx.ssid` / `ctx.password` | 命令行传进来的 |
| `ctx.networks` / `ctx.scan_count` | 测试之间共享的状态 |
| `ctx.setup` | 当前角色/拓扑，测试要按角色分叉时读它 |

## `needs` 与 `tags`

- **`needs`** —— 声明依赖。被选中的测试如果依赖了没被选中的，
  **依赖会自动递归带上**，否则依赖会被判成"未通过"导致整条链全跳过。
- **`tags`** —— 筛选用，多个取并集。分级见
  [running-tests.md](running-tests.md#2-tag-的安全分级)。

加测试时注意：

- **绝对不要**给新测试打 `danger` 或 `logmode` 以外的 tag 却让它改持久配置 ——
  那会让 `--tags display` 之类误触发。
- 会外溢的状态用 `set_and_restore()`，不要指望基线恢复兜底。
- 时延敏感的加 `max_duration`，依据是实测延迟留 3 倍余量
  （常量放 `uart_tests.py` 顶部，如 `MAX_SCAN`）。

## 改完先跑这两条

```bash
python3 uart_stress_test.py --list-tests     # 看依赖和 tag 展开对不对
python3 -m unittest discover                 # 工具自身回归，不需要接设备
```

两条都不接设备。过了再上真机，先 `--tags query -c 1`。
