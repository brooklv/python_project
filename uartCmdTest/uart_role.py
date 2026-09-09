"""被测设备的角色，以及由它推出来的 WiFi 行为。

为什么需要这个：**同一条 WiFi 测试对不同角色的设备意义完全不同**。
扫描路由、连接路由、忘记网络这套只有"开热点那一方"才做得到 ——
另一方连的是对端的热点，压根没有连路由这回事。不区分角色就会把
"这台设备本来就不该做这件事"报成失败。

配对关系决定谁开热点：

* **一对一** —— Rx 做 AP，Tx 连它
* **一对多** —— Tx 做 softap，多个 Rx 连它

而开热点的那一方**同时也是 station**：它连路由，给自己和连上它的对端
提供上网和 OTA。所以"是不是 AP"和"要不要连路由"是同一个判断。

    组合            开热点  连路由  说明
    Rx + 一对一     是      是      Tx 连它，它连路由供两边上网/OTA
    Tx + 一对一     否      否      只连 Rx 的热点
    Tx + 一对多     是      是      多个 Rx 连它，它连路由供大家上网/OTA
    Rx + 一对多     否      否      只连 Tx 的热点
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Role(Enum):
    """被测设备是发送端还是接收端。"""

    TX = "tx"
    RX = "rx"

    @property
    def label(self) -> str:
        return "Tx 发送端" if self is Role.TX else "Rx 接收端"


class Topology(Enum):
    """配对拓扑。决定谁开热点 —— 所以它会翻转 Tx / Rx 的 WiFi 行为。"""

    ONE_TO_ONE = "1to1"
    ONE_TO_MANY = "1tomany"

    @property
    def label(self) -> str:
        return "一对一" if self is Topology.ONE_TO_ONE else "一对多"


@dataclass(frozen=True)
class Setup:
    """被测设备的角色 + 拓扑，以及推出来的 WiFi 能力。"""

    role: Role
    topology: Topology

    # ------------------------------------------------------------ 推导
    @property
    def is_ap(self) -> bool:
        """本机是不是开热点的那一方。

        一对一 Rx 开、一对多 Tx 开 —— 即"被连的那一方"开热点。
        """
        if self.topology is Topology.ONE_TO_ONE:
            return self.role is Role.RX
        return self.role is Role.TX

    @property
    def is_sta(self) -> bool:
        """本机要不要作为 station 去连路由。

        和 ``is_ap`` 恒等：开热点的那一方同时也是 station，负责连路由，
        给自己和连上自己的对端提供上网和 OTA。

        两个名字都留着是因为调用点问的是两件不同的事 —— "能不能改热点
        SSID"和"能不能扫路由"。哪天出现只做 station 不开热点的形态，
        这里分开改就行，不用去翻所有调用点。
        """
        return self.is_ap

    @property
    def peer_role(self) -> Role:
        """对端的角色。Remote 指令是发给它的。"""
        return Role.RX if self.role is Role.TX else Role.TX

    @property
    def peer_count(self) -> str:
        if self.topology is Topology.ONE_TO_ONE:
            return "1"
        return "N" if self.is_ap else "1"

    @property
    def label(self) -> str:
        return f"{self.role.label} / {self.topology.label}"

    @property
    def peer_label(self) -> str:
        """对端怎么称呼。一对多时是"多个 RX"，其余是单个。"""
        name = self.peer_role.value.upper()
        if self.topology is Topology.ONE_TO_MANY and self.is_ap:
            return f"多个 {name}"
        return name

    def describe(self) -> str:
        """一行说清本机在这套配对里干什么。开跑前打出来，避免选错了还蒙在鼓里。"""
        if self.is_ap:
            return (f"{self.label}：本机开热点（{self.peer_label} 连它），"
                    f"同时作为 station 连路由，给自己和对端提供上网/OTA")
        return (f"{self.label}：本机连 {self.peer_label} 的热点，"
                f"不连路由（上网/OTA 由对端提供）")


# 四种组合。顺序就是提示菜单里的顺序 —— 把两个"开热点"的放前面，
# 因为它们才是能跑完整 WiFi 流程的。
ALL_SETUPS = [
    Setup(Role.RX, Topology.ONE_TO_ONE),
    Setup(Role.TX, Topology.ONE_TO_MANY),
    Setup(Role.TX, Topology.ONE_TO_ONE),
    Setup(Role.RX, Topology.ONE_TO_MANY),
]


def parse_setup(role: Optional[str],
                topology: Optional[str]) -> Optional[Setup]:
    """从命令行的两个字符串构造 Setup。任一个缺就返回 None（改去提示）。"""
    if not role or not topology:
        return None
    return Setup(Role(role), Topology(topology))


def guidance() -> str:
    """选角色前的说明。**必须解释清楚**，选错了整批 WiFi 测试都会跑偏
    ——而且症状是超时，不是明确的报错。"""
    return """
这个选择决定 WiFi 相关测试怎么跑，选错了整批 WiFi 测试会全部超时。

  配对关系决定谁开热点：
    一对一  ——  Rx 开热点，Tx 连它
    一对多  ——  Tx 开 softap，多个 Rx 连它

  开热点的那一方同时也是 station：它连路由，给自己和连上它的对端
  提供上网和 OTA。另一方只连对端的热点，不连路由。
"""


def menu_lines() -> list:
    """提示菜单的行。序号从 1 开始，和 ALL_SETUPS 一一对应。"""
    out = []
    for i, s in enumerate(ALL_SETUPS, 1):
        flag = "完整 WiFi 流程" if s.is_sta else "不连路由"
        out.append(f"  {i}) {s.label:<16} [{flag}]")
        out.append(f"     {s.describe().split('：', 1)[1]}")
    return out
