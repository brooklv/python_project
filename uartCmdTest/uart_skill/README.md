# uart-cmd-test skill

把 `uart_stress_test.py` 的用法、角色模型、tag 安全分级和测试编写约定打包成一个
Claude Code skill，让 Claude 能自己驱动串口测试，而不用每次重新解释一遍
"为什么这台 Tx 跑连接网络会全超时"。

## 这是什么

Claude Code 的 skill 就是一个带 YAML frontmatter 的 `SKILL.md`：
`description` 决定什么时候被触发，正文是 Claude 读到的操作指引。
本目录是一个完整的 skill，**不含可执行代码** —— 它驱动的是上一层已经有回归
测试覆盖的 `uart_stress_test.py`，自己不重新实现任何东西。

```
uart_skill/
├── SKILL.md                        # skill 本体：安全红线 + 跨平台 + 常用配方
├── README.md                       # 本文件
└── references/
    ├── running-tests.md            # 角色/拓扑、tag 分级、超时、报告、排障
    └── test-authoring.md           # 往 uart_tests.py 加测试
```

拆成两层是有意的：`SKILL.md` 保持短，能一眼扫完；细节放 `references/`，
Claude 遇到对应任务时才去读，不占日常上下文。

## 安装

**安装后的目录名必须等于 frontmatter 里的 `name`**（这里是 `uart-cmd-test`），
否则 Claude Code 不会加载。源目录叫 `uart_skill` 是为了和项目里其它目录看齐，
所以**用软链接改名**最省事，顺带改了源目录立即生效：

```bash
# 只给当前项目用
mkdir -p .claude/skills
ln -s /path/to/python_project/uartCmdTest/uart_skill .claude/skills/uart-cmd-test

# 或者全局可用
mkdir -p ~/.claude/skills
ln -s /path/to/python_project/uartCmdTest/uart_skill ~/.claude/skills/uart-cmd-test
```

Windows 上做软链接要管理员权限或开发者模式，不方便就直接把目录复制成
`%USERPROFILE%\.claude\skills\uart-cmd-test`，并设环境变量指向工具：

```
set UART_TEST=C:\path\to\uartCmdTest\uart_stress_test.py
```

装好后在 Claude Code 里用 `/uart-cmd-test` 显式调用，或者直接描述任务
（"跑一下串口查询测试"、"设备不回应了"），由 `description` 自动触发。

## 先决条件

工具本身只用标准库 + pyserial，Linux / macOS / Windows 都能跑。端口名、
装包方式和权限三平台不同，`SKILL.md` 里有对照表 —— 其中两条最容易出事：

- **macOS 必须用 `/dev/cu.*`，不能用 `/dev/tty.*`**（后者打开时阻塞等 DCD，
  表现是程序卡死没有任何输出）。
- **Linux 上别 `pip install pyserial`**，PEP 668 会拒绝，用 `apt` 或 venv。

## 为什么这个 skill 以安全为主线

被测设备是真实硬件，两组 tag 会造成不可逆或需要物理干预的后果：

- `--tags danger` —— 改 SSID/密码、OTA 升级、恢复出厂、重启。
- `--tags logmode` —— 打开设备 log 后**所有 UART 命令永久失效**，
  连"关闭 log"都发不进去，只能物理重启。

所以 `SKILL.md` 把这两条放在最前面，并且要求 agent 没有用户逐次确认就不要碰。
同样放在前面的还有两个容易出事的默认值：`-c` 默认 **100**（不是 1）、
`-l` 默认覆盖 `test.log`。

## 覆盖范围

沉淀的主要是**判断依据**，不是命令清单：

- 角色/拓扑怎么选。填错不会报参数错误，只会一堆超时 —— 而超时看起来像设备坏了。
  这是排障时的头号误判。
- tag 的安全分级，以及为什么 `danger`/`logmode` 不带其它 tag。
- 加测试时怎么选断言强度。`simple()` 只是冒烟测试，发现不了"回 SUCCESS 但没生效"；
  会外溢的状态必须用 `set_and_restore()`。
- 非交互驱动：`--role`/`--topology` 必须显式给，`-i` 是人用的菜单不要调。
- 跨平台的串口差异，尤其 macOS 的 `cu.*` / `tty.*`。

## 维护

改了这些地方要回来同步：

| 改了什么 | 要更新 |
|---|---|
| `uart_stress_test.py` 的命令行参数或默认值 | `SKILL.md` 的配方表和"安全红线"里的默认值 |
| `uart_tests.py` 的 tag 集合 | `references/running-tests.md` 的 tag 分级表 |
| `uart_exec.py` 的测试工厂 | `references/test-authoring.md` |
| `uart_role.py` 的角色模型 | `references/running-tests.md` 第 1 节 |
| 退出码常量 | `SKILL.md` 的退出码表 |

文档里**没有写死"每组有几条测试"**，那种数字迟早和代码脱节 ——
一律以 `--list-tests` 的实时输出为准。
