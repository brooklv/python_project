#!/bin/bash

# 手机 MAC 地址
PHONE_MAC="48:4C:86:37:CC:62"
BT_CONF="/etc/bluetooth/main.conf"
BACKUP_CONF="/etc/bluetooth/main.conf.bak"
DROPIN_DIR="/etc/systemd/system/bluetooth.service.d"
DROPIN_CONF="$DROPIN_DIR/10-hid-noinput.conf"
REMOTE_PY="douyin_remote.py"

# BlueZ 会把它作为 PnP Information (UUID 0x1200) 的 SDP 记录发布出去。
# iOS 对 HID 设备的 SDP 记录比 Android 挑剔, 缺 Device ID 有可能配不上。
# 1D6B = Linux Foundation 的 USB VID, 用它表明"我是个 Linux 设备"。
DEVICE_ID="usb:v1D6Bp0246d0517"
AGENT_LOG="/tmp/bt-agent.log"

# 当前选定的 HID 模式 (由 choose_mode 设置)
HID_CLASS=""
HID_MODE_NAME=""
HID_PY_ARG=""

function show_menu() {
    echo "=========================================="
    echo " 树莓派蓝牙模式切换与配对工具 "
    echo "=========================================="
    echo "1. 切换为 [HID 输入设备模式] 并开启自动配对"
    echo "2. 还原为 [默认蓝牙模式] (音频等功能恢复原始状态)"
    echo "3. 查看当前 HID 状态诊断"
    echo "4. 启动 [交互式配对代理] (iPhone/iPad 配对必须用这个)"
    echo "5. 退出"
    echo "=========================================="
    read -p "请选择操作 [1-5]: " choice
    case $choice in
        1) choose_mode && set_hid_mode ;;
        2) restore_default_mode ;;
        3) show_status ;;
        4) start_interactive_agent ;;
        5) exit 0 ;;
        *) echo "无效选择"; show_menu ;;
    esac
}

# 模式决定适配器的 CoD (设备类别) 和 Python 脚本的启动参数, 两边必须一致
function choose_mode() {
    echo ""
    echo "请选择要模拟的 HID 设备类型："
    echo "  1) 键鼠复合      (【Android/iOS 共用, 推荐】键盘+鼠标+多媒体键)"
    echo "  2) 键盘+多媒体键 (只给 Android: 对标罗技 K580, 实测能控制抖音)"
    echo "  3) 纯鼠标        (只给 iOS: 真实鼠标的滚轮/拖拽实测能刷)"
    echo "  4) 纯键盘        (最早那套配置, 原样保留)"
    echo ""
    echo "  平台差异: Android 认多媒体键; iOS 上只有 AC Home 这类系统级 usage 有效,"
    echo "            媒体键被路由给 now-playing 会话而抖音没接, 所以 iOS 得走鼠标。"
    echo "  共用做法: 选 1, 描述符里两套都在; Python 里按 'o' 键切换主操作方案,"
    echo "            接 Android 就切多媒体键, 接 iPhone 就切鼠标拖拽。"
    read -p "请选择 [1-4]: " m
    case $m in
        1) HID_CLASS="0x0005c0"; HID_MODE_NAME="键鼠复合 (Keyboard + Pointing)";          HID_PY_ARG="--composite" ;;
        2) HID_CLASS="0x000540"; HID_MODE_NAME="键盘+多媒体键 (Peripheral + Keyboard)";  HID_PY_ARG="--keyboard-media" ;;
        3) HID_CLASS="0x000580"; HID_MODE_NAME="纯鼠标 (Peripheral + Pointing device)";   HID_PY_ARG="--mouse-only" ;;
        4) HID_CLASS="0x000540"; HID_MODE_NAME="纯键盘 (Peripheral + Keyboard)";          HID_PY_ARG="--keyboard-only" ;;
        *) echo "无效选择"; return 1 ;;
    esac
    echo "已选择: $HID_MODE_NAME  (Class = $HID_CLASS)"
    return 0
}

# 取正在运行的遥控脚本用的是哪个模式参数 (不带参数就是默认的纯鼠标)
function running_py_arg() {
    local cmd a
    cmd=$(pgrep -af "$REMOTE_PY" | head -n1)
    if [ -z "$cmd" ]; then echo ""; return; fi
    for a in --composite --keyboard-media --keyboard-only --mouse-only; do
        if echo "$cmd" | grep -q -- "$a"; then echo "$a"; return; fi
    done
    echo "--mouse-only"
}

# 描述符 (Python 侧) 和 CoD (适配器侧) 必须匹配, 这里给出每个模式该有的 CoD
function cod_for_arg() {
    case "$1" in
        --composite)                     echo "0x0005c0" ;;
        --keyboard-media|--keyboard-only) echo "0x000540" ;;
        --mouse-only)                    echo "0x000580" ;;
        *)                               echo "" ;;
    esac
}

function current_cod() {
    hciconfig -a hci0 2>/dev/null | grep -m1 "Class:" \
        | grep -oE '0x[0-9a-fA-F]{6}' | tr 'A-F' 'a-f'
}

# 找出 bluetoothd 可执行文件路径 (Bookworm 在 /usr/libexec, 旧版在 /usr/lib)
function find_bluetoothd() {
    local p
    p=$(systemctl show -p ExecStart --value bluetooth.service 2>/dev/null \
        | sed -n 's/.*path=\([^ ;]*\).*/\1/p' | head -n1)
    if [ -x "$p" ]; then echo "$p"; return; fi
    for p in /usr/libexec/bluetooth/bluetoothd /usr/lib/bluetooth/bluetoothd; do
        if [ -x "$p" ]; then echo "$p"; return; fi
    done
    echo ""
}

function set_hid_mode() {
    echo -e "\n[1/7] 备份原始蓝牙配置文件 (/etc/bluetooth/main.conf)..."
    if [ ! -f "$BACKUP_CONF" ]; then
        cp "$BT_CONF" "$BACKUP_CONF"
        echo "已保存原始备份到: $BACKUP_CONF"
    else
        echo "已存在原始备份文件，跳过备份步骤。"
    fi

    echo -e "\n[2/7] 修改 main.conf 中的 Class 为 $HID_CLASS ($HID_MODE_NAME)..."
    if grep -q "^Class *=" "$BT_CONF"; then
        sed -i "s/^Class *=.*/Class = $HID_CLASS/" "$BT_CONF"
    elif grep -q "^#Class *=" "$BT_CONF"; then
        sed -i "s/^#Class *=.*/Class = $HID_CLASS/" "$BT_CONF"
    else
        # 如果配置文件中完全没写 Class，则在 [General] 节点下方追加
        sed -i "/^\[General\]/a Class = $HID_CLASS" "$BT_CONF"
    fi

    # 确保 DiscoverableTimeout 和 PairableTimeout 不会超时关闭
    sed -i 's/^#*DiscoverableTimeout *=.*/DiscoverableTimeout = 0/' "$BT_CONF"
    sed -i 's/^#*PairableTimeout *=.*/PairableTimeout = 0/' "$BT_CONF"

    echo -e "\n[3/7] 设置 Device ID (PnP) 为 $DEVICE_ID..."
    if grep -q "^DeviceID *=" "$BT_CONF"; then
        sed -i "s|^DeviceID *=.*|DeviceID = $DEVICE_ID|" "$BT_CONF"
    elif grep -q "^#DeviceID *=" "$BT_CONF"; then
        sed -i "s|^#DeviceID *=.*|DeviceID = $DEVICE_ID|" "$BT_CONF"
    else
        sed -i "/^\[General\]/a DeviceID = $DEVICE_ID" "$BT_CONF"
    fi
    echo "iOS 对 HID 的 SDP 记录比 Android 挑剔, 缺 PnP 记录有可能配不上。"

    # 关键步骤: BlueZ 自带的 input 插件实现的是 HID 主机(Host)角色, 会占用
    # L2CAP PSM 17/19 两个通道。不关掉它, Python 脚本就没法自己 bind 这两个端口,
    # 手机发起的 HID 连接会被 bluetoothd 自己吃掉。
    echo -e "\n[4/7] 禁用 BlueZ 自带 input 插件 (HID 设备角色必需)..."
    BTD_BIN=$(find_bluetoothd)
    if [ -z "$BTD_BIN" ]; then
        echo "[错误] 找不到 bluetoothd 可执行文件，无法禁用 input 插件！"
    else
        mkdir -p "$DROPIN_DIR"
        cat > "$DROPIN_CONF" <<EOF
[Service]
ExecStart=
ExecStart=$BTD_BIN -P input
EOF
        systemctl daemon-reload
        echo "已写入: $DROPIN_CONF  ($BTD_BIN -P input)"
    fi

    echo -e "\n[5/7] 清理旧的代理进程并重启蓝牙服务..."
    pkill -f "bt-agent" > /dev/null 2>&1
    pkill -f "bluetoothctl" > /dev/null 2>&1
    systemctl restart bluetooth
    sleep 2

    echo -e "\n[6/7] 清除手机旧配对缓存并强制设置广播..."
    bluetoothctl remove $PHONE_MAC > /dev/null 2>&1
    bluetoothctl power on > /dev/null 2>&1
    hciconfig hci0 class $HID_CLASS > /dev/null 2>&1
    hciconfig hci0 piscan > /dev/null 2>&1

    echo -e "\n[7/7] 启动后台无密码配对代理 (bt-agent)..."
    ensure_bt_agent
    # 输出别丢进 /dev/null: 代理"启动即退出"时看不见任何提示, 现象和配对失败
    # 一模一样 (曾经就因为传了不支持的 capability 栽在这上面)
    bt-agent -c NoInputNoOutput > "$AGENT_LOG" 2>&1 &
    sleep 1
    if pgrep -f "bt-agent" > /dev/null 2>&1; then
        echo "已启动: bt-agent -c NoInputNoOutput  (日志: $AGENT_LOG)"
    else
        echo "[错误] bt-agent 启动失败！没有配对代理, 配对一定不会成功:"
        sed 's/^/       /' "$AGENT_LOG"
    fi

    # 保持蓝牙可被搜索和允许配对
    bluetoothctl discoverable on > /dev/null 2>&1
    bluetoothctl pairable on > /dev/null 2>&1

    show_status

    local run_arg
    run_arg=$(running_py_arg)
    if [ -n "$run_arg" ] && [ "$run_arg" != "$HID_PY_ARG" ]; then
        echo "【警告】$REMOTE_PY 正以 $run_arg 运行，和刚选的模式 ($HID_PY_ARG) 不一致！"
        echo "        CoD 和 SDP 描述符必须是同一种设备。请先停掉它再按新参数启动："
        echo "          sudo pkill -f $REMOTE_PY"
        echo "          sudo PYTHONIOENCODING=utf-8 python3 $REMOTE_PY $HID_PY_ARG"
        echo "------------------------------------------------------"
    fi

    echo "【配对环境已就绪！请严格按以下顺序操作】"
    echo "0. 重要：HID 报告描述符是在配对时被手机缓存下来的。只要换过模式或改过"
    echo "   描述符，就必须在手机上删除配对再重新配对，否则手机用的还是旧描述符。"
    echo "1. 手机端：在蓝牙列表中找到树莓派，先【取消配对 / 忽略此设备】。"
    echo "2. 树莓派端：另开终端启动 Python 遥控脚本，并【保持它一直运行】："
    echo "   sudo PYTHONIOENCODING=utf-8 python3 $REMOTE_PY $HID_PY_ARG"
    echo "   注意参数要和刚才选的模式一致 ($HID_MODE_NAME)。"
    echo "   HID 的 SDP 记录由该脚本注册，脚本没运行时手机搜不到 HID 服务，"
    echo "   所以必须先启动它，再去手机上配对。"
    echo "3. 手机端：重新搜索并点击树莓派发起配对。"
    echo "4. 配对成功后回到本脚本选 3，确认 Paired/Trusted/Connected 均为 yes。"
    echo ""
    echo "【iPhone / iPad 请注意】"
    echo "如果选的是带鼠标的模式，iOS 默认不启用外接指针设备，必须先打开："
    echo "  设置 -> 辅助功能 -> 触控 -> 辅助触控 (AssistiveTouch)"
    echo "不打开这个开关，鼠标报告发过去不会有任何反应。Android 不需要。"
    echo "iOS 对 HID 设备要求【已认证的配对】(要输配对码)，而上面起的后台代理是"
    echo "NoInputNoOutput (Just Works)，应答不了配对码请求，表现就是反复"
    echo "Connected=True/False 却始终没有 Paired=True。"
    echo "iOS 请改用本脚本的选项 4 启动【交互式配对代理】，它在前台运行，"
    echo "iPhone 上显示的 6 位配对码可以直接输进去 (选 KeyboardOnly；"
    echo "bt-agent 不支持 KeyboardDisplay，需要它就用选项 4 里的 bluetoothctl)。"
    echo "------------------------------------------------------"
}

# iOS 对 HID 设备要求已认证配对: 它会显示一个 6 位配对码, 等设备把码"敲"回去。
# 后台跑的 bt-agent 且输出全丢到 /dev/null, 根本没法应答这个请求 —— 这正是
# iPhone 反复 Connected=True/False 却配不上的原因。所以这里在前台跑。
#
# 注意: bluez-tools 的 bt-agent 只认 BlueZ 早期的四种 capability, 没有
# KeyboardDisplay (传它会直接报 "Invalid capability" 退出, 于是一个代理都没注册,
# 表现和"配对失败"一模一样)。所以这里先探测本机支持哪些, 只列出可用的;
# 想用 KeyboardDisplay 就走 bluetoothctl 那条。
function ensure_bt_agent() {
    if ! command -v bt-agent &> /dev/null; then
        echo "正在自动安装 bluez-tools 依赖包..."
        apt-get update -qq && apt-get install bluez-tools -y -qq
    fi
}

function detect_agent_caps() {
    bt-agent --help 2>&1 \
        | grep -oE 'KeyboardDisplay|KeyboardOnly|DisplayYesNo|DisplayOnly|NoInputNoOutput' \
        | sort -u | tr '\n' ' '
}

function start_interactive_agent() {
    ensure_bt_agent
    local caps
    caps=$(detect_agent_caps)
    echo ""
    echo "本机 bt-agent 支持的 capability: ${caps:-探测失败}"
    echo ""
    echo "请选择配对方式："
    echo "  0) 【推荐】用 Python 脚本自带的代理 —— 不用本菜单, 直接这样启动脚本:"
    echo "       sudo PYTHONIOENCODING=utf-8 python3 $REMOTE_PY <模式参数> --agent"
    echo "     配对码提示就在那个终端里, 退格正常, 只需一个终端。"
    echo "  1) bt-agent  KeyboardOnly    (iOS 显示配对码, 在本终端输入)"
    echo "  2) bt-agent  DisplayYesNo    (数字比较, 两边各确认一次)"
    echo "  3) bt-agent  NoInputNoOutput (Just Works, Android 用这个就够)"
    echo "  4) bluetoothctl              (前台交互, 支持 KeyboardDisplay)"
    read -p "请选择 [0-4]: " c

    local cap=""
    case $c in
        0) echo "请按上面第 0 项的命令重启 Python 脚本 (记得先 sudo pkill -f bt-agent)。"
           pkill -f "bt-agent" > /dev/null 2>&1
           echo "已顺手停掉后台 bt-agent, 免得和自带代理抢默认代理。"
           return 0 ;;
        1) cap="KeyboardOnly" ;;
        2) cap="DisplayYesNo" ;;
        3) cap="NoInputNoOutput" ;;
        4) cap="" ;;
        *) echo "无效选择"; return 1 ;;
    esac

    # 先杀掉后台那个, 否则两个代理会抢着注册
    pkill -f "bt-agent" > /dev/null 2>&1
    sleep 1
    bluetoothctl discoverable on > /dev/null 2>&1
    bluetoothctl pairable on > /dev/null 2>&1

    echo "------------------------------------------------------"
    echo "【操作步骤】"
    echo "1. 确认 $REMOTE_PY 已经在另一个终端运行着 (HID SDP 记录由它注册)"
    echo "2. 手机端先【取消配对 / 忽略此设备】，再重新搜索并点击树莓派"
    echo "3. 手机弹出 6 位配对码后，把数字输入到【本终端】并回车"
    echo "4. 按 Ctrl+C 退出代理"
    echo "------------------------------------------------------"

    if [ -n "$cap" ]; then
        # 校验一下本机到底支不支持, 免得又出现"启动即退出、一个代理都没有"
        if [ -n "$caps" ] && ! echo "$caps" | grep -qw "$cap"; then
            echo "[错误] 本机 bt-agent 不支持 $cap (支持的是: $caps)"
            echo "       请改选其它选项，或用选项 4 的 bluetoothctl。"
            return 1
        fi
        echo "前台启动: bt-agent -c $cap"
        echo "------------------------------------------------------"
        bt-agent -c "$cap"
    else
        # 注意: 千万不要用 { echo ...; cat; } | bluetoothctl 这种管道写法把命令喂进去。
        # 那样 bluetoothctl 的 stdin 不是 TTY, readline 行编辑失效, 退格会变成字面量
        # ^H 混进配对码里, 配对必然失败 (现象: 反复 Enter passkey)。
        # 所以这里让 bluetoothctl 直接接管终端, 两条命令由使用者自己敲。
        echo "前台启动 bluetoothctl。进去之后【手动】依次输入这两行:"
        echo "    agent KeyboardDisplay"
        echo "    default-agent"
        echo "然后在手机上发起配对, 配对码提示会出现在 bluetoothctl 里 (退格可用)。"
        echo "输 quit 退出。"
        echo "------------------------------------------------------"
        bluetoothctl
    fi
}

function show_status() {
    echo "------------------------------------------------------"
    echo "【HID 状态诊断】"

    # input 插件是否已被禁用
    if pgrep -a bluetoothd | grep -qE '\-P +input|--noplugin=input'; then
        echo "  input 插件 : 已禁用 (OK, HID 设备角色可用)"
    else
        echo "  input 插件 : 仍在加载 (HID 设备角色不可用; 若已还原为默认模式则属正常)"
    fi

    # 适配器 Class / 可发现状态 (hciconfig 短格式不含 Class, 必须用 -a)
    local cls
    cls=$(hciconfig -a hci0 2>/dev/null | grep -m1 "Class:")
    if [ -n "$cls" ]; then
        echo "  适配器 CoD :${cls#*Class:}"
        echo "               (0x000580 纯鼠标 / 0x000540 纯键盘 / 0x0005c0 键鼠复合)"
    else
        echo "  适配器 CoD : $(bluetoothctl show 2>/dev/null | grep -m1 'Class' || echo '读取失败')"
    fi
    echo "  可发现/可配对: $(bluetoothctl show 2>/dev/null | grep -E 'Discoverable:|Pairable:' | tr '\n' ' ')"

    # 手机侧配对状态。注意: 未知设备时 bluetoothctl 会返回
    # "Device <MAC> not available", 这句里同样含有 MAC, 所以必须用 Paired: 字段判断
    local info
    info=$(bluetoothctl info $PHONE_MAC 2>/dev/null)
    if echo "$info" | grep -q "Paired:"; then
        echo "  手机 $PHONE_MAC:"
        echo "$info" | grep -E "Paired:|Trusted:|Connected:" | sed 's/^/    /'
    else
        echo "  手机 $PHONE_MAC: 尚未配对 (需要从手机端主动发起连接)"
    fi

    # HID 的 SDP 记录是 Python 脚本注册的, 脚本不在跑手机就看不到 HID 服务
    local py
    py=$(pgrep -af "$REMOTE_PY" | head -n1)
    if [ -n "$py" ]; then
        echo "  遥控脚本   : 运行中 (HID SDP 记录已注册)"
        echo "               $py"
    else
        echo "  遥控脚本   : 未运行 (手机此时搜不到 HID 服务, 配对前必须先启动它)"
    fi

    # 一致性检查: 脚本发布的描述符/子类 和 适配器 CoD 必须是同一种设备,
    # 否则手机看到的"我是什么设备"和"我的报告长什么样"是矛盾的
    local arg want got
    arg=$(running_py_arg)
    if [ -n "$arg" ]; then
        want=$(cod_for_arg "$arg")
        got=$(current_cod)
        if [ -n "$want" ] && [ -n "$got" ] && [ "$want" != "$got" ]; then
            echo "  [不一致]   脚本按 $arg 运行 (需要 CoD $want), 但适配器 CoD 是 $got"
            echo "             两边必须一致! 二选一:"
            echo "               a) sudo pkill -f $REMOTE_PY  然后按正确参数重启脚本"
            echo "               b) 用菜单选项 1 把 CoD 换成 $want 对应的模式"
        else
            echo "  一致性     : OK (脚本 $arg <-> CoD $got)"
        fi
    fi

    # Device ID (PnP), iOS 比较在意
    if grep -q "^DeviceID *=" "$BT_CONF"; then
        echo "  Device ID  : $(grep -m1 '^DeviceID *=' "$BT_CONF" | sed 's/.*= *//')"
    else
        echo "  Device ID  : 未设置 (iOS 可能因此配不上)"
    fi

    # 配对代理: 顺带把 capability 打出来, iOS 需要的是能输配对码的那种
    local agent
    agent=$(pgrep -af bt-agent | head -n1)
    if [ -n "$agent" ]; then
        echo "  bt-agent   : 运行中"
        echo "               $agent"
        if echo "$agent" | grep -q "NoInputNoOutput"; then
            echo "               (Just Works; iPhone/iPad 配对请改用菜单选项 4)"
        fi
    else
        echo "  bt-agent   : 未运行 (配对会失败)"
    fi
    echo "------------------------------------------------------"
}

function restore_default_mode() {
    echo -e "\n[1/6] 停止遥控脚本 (它占着 L2CAP PSM 17/19 并注册着 HID SDP 记录)..."
    if pgrep -f "$REMOTE_PY" > /dev/null 2>&1; then
        pkill -f "$REMOTE_PY"
        sleep 1
        echo "已停止 $REMOTE_PY"
    else
        echo "$REMOTE_PY 未在运行，跳过。"
    fi

    echo -e "\n[2/6] 清理后台配对代理进程..."
    pkill -f "bt-agent" > /dev/null 2>&1
    pkill -f "bluetoothctl" > /dev/null 2>&1

    echo -e "\n[3/6] 还原配置文件 (/etc/bluetooth/main.conf)..."
    if [ -f "$BACKUP_CONF" ]; then
        cp "$BACKUP_CONF" "$BT_CONF"
        rm -f "$BACKUP_CONF"
        echo "已从备份恢复原始配置，并删除备份文件 (下次切 HID 会重新备份)。"
    else
        # 没有备份就把我们可能写进去的三项删掉/注释回去
        sed -i '/^Class = 0x0005[48c]0/d' "$BT_CONF"
        sed -i "\|^DeviceID = $DEVICE_ID|d" "$BT_CONF"
        echo "未找到备份文件，已移除自定义 Class 和 DeviceID。"
    fi

    echo -e "\n[4/6] 恢复 BlueZ input 插件 (移除 systemd drop-in)..."
    if [ -f "$DROPIN_CONF" ]; then
        rm -f "$DROPIN_CONF"
        rmdir "$DROPIN_DIR" 2>/dev/null
        systemctl daemon-reload
        echo "已移除: $DROPIN_CONF"
    else
        echo "未找到自定义 drop-in，跳过。"
    fi

    echo -e "\n[5/6] 解除与手机的 HID 配对..."
    bluetoothctl remove $PHONE_MAC > /dev/null 2>&1
    echo "已移除 $PHONE_MAC 的配对记录 (手机侧也请手动【取消配对】)。"

    echo -e "\n[6/6] 重启蓝牙服务并复位适配器..."
    systemctl restart bluetooth
    sleep 2
    hciconfig hci0 reset > /dev/null 2>&1
    sleep 1

    echo "------------------------------------------------------"
    echo "【还原结果核对】"
    if pgrep -a bluetoothd | grep -qE '\-P +input|--noplugin=input'; then
        echo "  input 插件 : [异常] 仍被禁用，请检查是否有其它 drop-in"
        systemctl cat bluetooth.service 2>/dev/null | grep -n "ExecStart"
    else
        echo "  input 插件 : 已恢复加载 (可以正常连接蓝牙键鼠等外设)"
    fi
    local cls
    cls=$(hciconfig -a hci0 2>/dev/null | grep -m1 "Class:")
    echo "  适配器 CoD :${cls#*Class:} (已不再是 HID 外设类别)"
    if grep -q "^Class *=" "$BT_CONF"; then
        echo "  main.conf  : $(grep -m1 '^Class *=' "$BT_CONF")"
    else
        echo "  main.conf  : 未设置自定义 Class (原始状态)"
    fi
    echo "------------------------------------------------------"
    echo "[OK] 已还原为初始蓝牙设置，音频 (A2DP) 等功能恢复正常。"
    echo "------------------------------------------------------"
}

if [ "$EUID" -ne 0 ]; then
  echo "请使用 sudo 运行此脚本: sudo ./switch2hid.sh"
  exit 1
fi

show_menu
