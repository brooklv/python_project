# dlna-push skill

把 `dlna_push.py` 的用法和排障经验打包成一个 Claude Code skill，让 Claude
能自己驱动推流、验证拖进度、做老化测试，而不用每次重新解释一遍 DLNA 的坑。

## 这是什么

Claude Code 的 skill 就是一个带 YAML frontmatter 的 `SKILL.md`：
`description` 决定什么时候被触发，正文是 Claude 读到的操作指引。
本目录是一个完整的 skill，**不含可执行代码** —— 它驱动的是上一层已经有测试
覆盖的 `dlna_push.py`，自己不重新实现任何东西。

```
dlna_skill/
├── SKILL.md                      # skill 本体：触发条件 + 操作指引
├── README.md                     # 本文件
└── references/
    └── troubleshooting.md        # 排障手册，按症状组织，SKILL.md 按需引用
```

拆成两层是有意的：`SKILL.md` 保持短，能一眼扫完；细节放
`references/troubleshooting.md`，Claude 遇到对应症状时才去读，不占日常上下文。

## 安装

**安装后的目录名必须等于 frontmatter 里的 `name`**（这里是 `dlna-push`），
否则 Claude Code 不会加载。源目录叫 `dlna_skill`，所以**用软链接改名**最省事 ——
顺带 `SKILL.md` 里"往上一层找 `dlna_push.py`"的逻辑仍然成立，改了源目录也立即生效：

```bash
# 只给当前项目用
mkdir -p .claude/skills
ln -s /media/pie/Disk/workspace/python_project/dlnaPush/dlna_skill .claude/skills/dlna-push

# 或者全局可用
mkdir -p ~/.claude/skills
ln -s /media/pie/Disk/workspace/python_project/dlnaPush/dlna_skill ~/.claude/skills/dlna-push
```

不方便做软链接就整个目录复制过去，但这时必须让 skill 找得到工具：

```bash
export DLNA_PUSH=/media/pie/Disk/workspace/python_project/dlnaPush/dlna_push.py
```

装好后在 Claude Code 里用 `/dlna-push` 显式调用，或者直接描述任务
（"把这个视频推到客厅电视"、"拖进度老是从头开始载"），由 `description` 自动触发。

## 先决条件

- Linux 主机。SSDP 组播和 `SIOCGIFADDR` 查网卡地址都是 Linux 行为，
  Windows 上模块能导入、测试能跑，但 `-i 网卡` 用不了。
- Python 3.8+，无第三方包。`-f ts/mp4/hls` 另需 `ffmpeg`。
- 主机和电视在同一个二层网段。

## 覆盖范围

skill 里沉淀的主要是**判读方法**，不是命令清单：

- 非交互驱动。`dlna_push.py` 推流成功后会进交互控制台，agent 直接跑会挂住；
  正确做法是用管道喂命令（控制台读到 EOF 会干净退出，关 HTTP、清 ffmpeg 产物）。
- 「拖进度从头开始载」的完整定位链路 —— 这类问题九成会被误判成
  「HTTP server 不支持 Range」。真正的分水岭是 `DLNA.ORG_OP`，
  以及 `--http-log` 里起播 `200` / seek 后 `206` 的对比。
- MIME 与 `upnp:class` 对不上导致的静默拒播。
- 搜不到设备时的两条路：换网卡 vs 给 IP 转直连。
- 老化测试的语义和退出码，以及它会真的把设备打崩这件事。

## 维护

改了这些地方要回来同步：

| 改了什么 | 要更新 |
|---|---|
| `dlna_push.py` 的命令行参数 | `SKILL.md` 的「常用配方」 |
| `dlna_shell.py` 的控制台命令 | `SKILL.md` 的「非交互驱动」 |
| `dlna_media.py` 的 `dlna_features()` | `references/troubleshooting.md` 第 4 节里那串 `DLNA.ORG_*` |
| 退出码常量 | `SKILL.md` 的退出码表 |

`dlna_features()` 是 `DLNA.ORG_*` 的唯一出处 —— DIDL 的 `protocolInfo` 第四栏和
HTTP 的 `contentFeatures.dlna.org` 共用它。文档里抄了一份具体的串是为了让人
能直接和 `curl` 的输出对照，所以它改了文档就必须跟着改。
