<!-- 由 tools_parse_spec.py 从 SPEC_AM_826X_83XX_uart_protocol_v1.8.xlsx 生成，勿手改 -->

# UART 协议 spec 提取（v1.8）

从 `SPEC_AM_826X_83XX_uart_protocol_v1.8.xlsx` 机械提取，便于和代码里的命令表对照。


## system（16 条）

### 0x861d Projector Name `WR` TAG=GT len=21

    Data structure: String
    BYTE 0 ~ 19 : 20 characters (ASCII code);
    BYTE 20 : MUST BE 0x00

> Used 0x00 to represent the end of string

### 0x8625 Language `WR` TAG=GT len=1

    Data structure: <1Byte: Index>
    0 : English;
    1 : Czech;
    2 : Danish;
    3 : German;
    4 : Spanish;
    5 : French;
    6 : Italian;
    7 : Hungarian;
    8 : Dutch;
    9 : Polish;
    10 : Portuguese;
    11 : Swedish;
    12 : Turkish;
    13 : Russian;
    14 : Thai;
    15 : Chinese;
    16 : Big5;
    17 : Japanese;
    18 : Korean;
    19 : Vietnamese
    20 : Arabic;
    21 : Croatian;
    22 : Romanian;
    23 : Norwegian;
    24 : Bulgarian;
    25 : Finnish;
    26 : Indonesia;
    27 : Hindi;
    28 : Greek;
    29 : Persian

### 0x8627 Source Status `R` TAG=GT len=2

    Data structure: <2Byte: BitField>
    Byte.bit 0 : HDMI1;
    Byte.bit 1 : HDMI2;
    Byte.bit 2 : VGA1;
    Byte.bit 3 : VGA2;
    Byte.bit 4 : CVBS;
    Byte.bit 5 : None;
    Byte.bit 6 : Media;
    Byte.bit 7 : EZCast;(Network Display)
    Byte.bit 8 : MiraCast;
    Byte.bit 9 : EZWire;(Mobile USB Display)
    Byte.bit 10 : PC USB Display (Type B)
    Byte.bit 11: ui on off
    Byte.bit 12~15 : Reserved
    test:0x3:0x00

> test[HDMI1/2]=3:0

### 0x8628 Power Status `WR` TAG=GT len=2

    Data structure: <2Byte: Value>
    BYTE 0 : Index of Status;
    0 : Power Off;
    1 : Power On;
    2 : Standby;
    3 : Warm Up;
    4 : Cooling Down;
    5 : Standby audio
    BYTE 1 : Percentage

> percentage is 0 ~ 100;
> test[power off, 100]=00:100
> test[power on, 00]=01:0
> test[standby, 100]=02:100

### 0x8629 Reset All Settings `W` TAG=GT len=1

    Data structure: <1Byte: Index>
    Index of Reset All Settings
    0 : User mode;
    1 : Factory mode;
    2 : Vga mode;

> User mode : reset network setting and system setting(w/o adc calibration)
> factory mode : as user mode and include adc calibration.
> Vga mode : reset vga/component mode setting only. (for Foxconn Sharp project only)
> ※ vga mode only for Foxconn Sharp project and no need reboot.
> ※After execute reset command, AM will automatic reboot.

### 0x8632 Firmware Upgrade `WR` TAG=GT len=2

    Data structure: <2Byte: Value>
    Byte 0 : The index of Data path
    0 : OTA
    1 : USB;
    Byte 1 : Upgrade Status;(signed)

> Status is 0 for excuting firmware Upgrade
> Status is 1 ~ 99 for Upgrade progress
> Status is 100 for finishing upgrade
> Status is -1 for same version
> status is -2 for fail case
> test[OTA, start]=0:0
> test[USB, start]=1:0

### 0x863d FW version of AM `R` TAG=GT len=N

    Data structure: String
    BYTE 0 ~ N-1 : characters (ASCII code);
    BYTE N : MUST BE 0x00

> Used 0x00 to represent the end of string

### 0x8644 Native Resolution `WR` TAG=GT len=N

    Data structure: <4Byte: Value>
    BYTE 0~N: resolution string , eg:FMT_1920x1080_60P

> Unit is Pixel
> test[1080P]=0x80:0x07:0x38:0x04
> test[XGA]=0x00:0x04:0x00:0x03
> test[WXGA]=0x00:0x05:0x20:0x03

### 0x864b HDCP Key Status `R` TAG=GT len=1

    Data structure: <1Byte: Index>
    bit0:
    0 : Disable(Off);
    1 : Enable(On);
    bit1: hdmi hdcp key existance
    bit2: miracast hdcp key existance

### 0x864d Factory Test Functions `WR` TAG=GT len=4~N

    Data structure: <4Byte: value>
    Bit 0: Factory Test Functions on/off
    Bit 1: ESD test enable.(1: enable, 0 disable
    Bit 2:ftest_method | eg: string "TX_test" or "Dongle_test" defined int actui_test.h header file
    bit 3:ftest_version | eg: string not use
    bit 4:iperf_test | eg: bool true or false
    bit 5:iperf_threshold_5g | eg: int 1~100
    bit 6:iperf_threshold_2.4g | eg: int 1~100
    bit 7:hdcp_test_tx_device_rx | eg: bool true or false
    bit 8:miracast_hdcp_key_disable | eg: bool true or false
    bit 9:edid_test_device_rx | eg: bool true or false
    bit 10:fw_version_rx | eg: string "21589035"
    bit 11:fw_vendor_rx | eg: string "am_8268D_ezcast-rx_8731bu"
    bit 12:rx_key_test | eg: bool true or false
    bit 13:rx_video_test | eg: bool true or false
    bit 14:rx_video_file | eg: string file name
    bit 15:rx_video_test_num | eg: int 1~100
    bit 16:rx_default_setting_test | eg: bool true or false
    bit 17:rx_default_region | eg: string region "US"
    bit 18:rx_default_language | eg: string language "ja"
    bit 19:rx_default_brand | eg: string brand "Mate"
    bit 20:rx_default_model | eg: string model "01"
    bit 21:iperf_wifi_channel | eg: string "149:12&36&153:10&157&44:22&161&165:20"
    bit 22~30:reserved
    bit 31:get factory test result eg: true or false
    byte4~N: value

> ESD test on 8269D (ES6000) if scalar enable ESD test, when 8269D cannot reach external router, 8269D will reboot, and re-power on Wi Fi module.
> Usage: scalar or MCU send 4 bytes to AM's device/module, AM check bit0 first, if bit0 is 0, discard the remain 31 bits and disable ALL factory test functions, if bit1 is 1, the slave should check the remain 31 bits for each factory test function.

### 0x8651 Log Status `WR` TAG=GT len=1

    Data structure: <2Byte: value>
    bit0 : actui log on/off , when change reboot
    bit1 : kernel log on/off , do not reboot

### 0x8652 TX version `WR` TAG=GT len=N

    Data structure: <2Byte: value>
    BYTE0~N: version value

### 0x8653 HDMI Status `WR` TAG=GT len=1

    Data structure: <2Byte: value>
    BYTE0: 0 plug out
    1 plug in

### 0x8654 uart ready `WR` TAG=GT len=2

    Data structure: <2Byte: value>
    BYTE0~1:  1 uart ready

### 0x8655 Remote AM status `WR` TAG=GT len=17*N

    Data structure: <2Byte: value>
    BYTE0~15: Remote IP, 0 default, IP Format xx.xx.xx.xx.xx.xx
    BYTE16: status
    0:disconnect
    1: connected
    2: casting start
    4: casting stop
    [BYTE7: Remote index ,
    ….]

### 0x8656 Firmware Upgreade Control `WR` TAG=GT

    Data structure: <2Byte: value>
    BYTE0: Major Action Check or Confirm
    0: Upgrade Mode Switch
    1: check , the ack value: local_Version:Server_Version
    2: confirm
    BYTE1: Low 4 bits Sub Action Local or Remote
    0: Local
    1: Remote
    High 4 bits for Remote Device index
    BYTE2: Min Action Cancel ,Ok, version result, upgrade percent
    0: Cancel
    1: Ok
    2: version result
    3: upgrade percent
    4: upgrade status
    BYTE3~N: value String, eg: version result , upgrade percent


## Display（14 条）

### 0x8823 Output Rotation `WR` TAG=GT len=2

    Data structure: <2Byte: Index>
    Byte 0:
    degree:
    0 : 0 degree;
    1 : 90 degree counter clockwised;
    2 : 180 degree, counter clockwised ;
    3 : 270 degree, counter clockwised ;
    4:  keep current
    Byte 1:
    mode:
    0 : keep current
    1 : crop black.
    2 : don't black.

> for 8269D/8275 uart cmd

### 0x8824 USB wire mode `WR` TAG=GT len=1

    Data structure: <1Byte: Index>
    byte 0:
    0: device mode
    1:host mode

### 0x8825 switch wifi channel `WR` TAG=GT len=1

    Data structure: <1Byte: Index>
    byte 0:
    1:2.4G
    2:5G
    3:2.4G->5G, 5G->2.4G

### 0x8829 set overscan `WR` TAG=GT len=2

    Data structure: <2Byte: value>
    value:
    100:100%
    110:110%
    120:120%
    130:130%

### 0x8830 set portrait `WR` TAG=GT len=4

    Data structure: <4Byte: value>
    byte0~1:
    0:degree0
    90:degree90
    270:degree270
    byte2~3:
    100: over scan 100%
    110: over scan 110%
    120: over scan 120%
    130: over scan 130%

### 0x8831 UI control `WR` TAG=GT len=14

    Data structure: <14Byte: value>
    BYTE0~1:GUI id
    BYTE2~3:GUI Position x
    BYTE4~5:GUI Position y
    BYTE6~7:GUI width
    BYTE8~9: GUI height
    BYTE10~11: control
    0: unhide
    1: hide
    2: add
    3: remove
    BYTE12~13: reserved

### 0x8832 HDMI Enable `WR` TAG=GT len=1

    Data structure: <14Byte: value>
    BYTE0: HDMI Enable or disable
    0: disable HDMI output
    1: enable HDMI output

### 0x8833 Set Portrait Zoom `WR` TAG=GT len=4

    Data structure: <14Byte: value>
    BYTE0~1: portrait degree
    0: degree0
    90:degree90
    270:degree270
    BYTE2~3: zoom mode
    0:mode 0
    1:mode 1
    2:mode 2

### 0x8834 EDID Passthrough `WR` TAG=GT len=N

    Data structure: <14Byte: value>
    BYTE 0~N:EDID raw data

### 0x8835 Mirror Rotation Enable `WR` TAG=GT len=1

    Data structure: <14Byte: value>
    BYTE 0: Rotation Enable
    0 : disable
    1 : enable

### 0x8836 Camera Portrait Enable `WR` TAG=GT len=1

    Data structure: <14Byte: value>
    BYTE 0: camera portrait angle
    0 : rotate 90
    1 : rotate 270

### 0x8837 Transfer String `W` TAG=GT len=N

    Data structure: <NByte: value>
    BYTE 0~1: axis X
    BYTE 2~3: axis Y
    BYTE 4: priorty, the value is higher means priority is higher, 0=minor
    BYTE 5~N: string

### 0x8838 Cast Status `WR` TAG=GT len=1

    Data structure: <1Byte: value>
    BYTE 0: cast on or off
    0 : cast off
    1 : cast on

### 0x8839 Encoder Param `WR` TAG=GT len=1~N

    Data structure: <1Byte: value>
    BYTE 0: encode param type
    0 : resolution_w_h,  follow 2byte width, 2 byte height
    1 : fps, follow 2 byte fps value
    2: bitrate, follow 4byte
    3: sample rate, follow 4byte
    BYTE 1~N: param value


## Network（9 条）

### 0x820a WiFi AP SSID `WR` TAG=GT len=21

    Data structure: String(abbr)
    BYTE 0~20 : SSID;(ASCII code);

> Used 0x00 to represent the end of string
> test[1111]
> test[2222]

### 0x820b WiFi AP KEY `WR` TAG=GT len=64

    Data structure: <64Byte: String>
    BYTE 0~63 : Key;(ASCII code)

> Key is Hexadecimal
> test[11111111]
> test[22222222]

### 0x820c WiFi STA SSID `WR` TAG=GT len=21

    Data structure: String(abbr)
    BYTE 0~20 : SSID;(ASCII code);

> Used 0x00 to represent the end of string
> test[00:26:5A:A3:B7:3E]
> test[ askawu@actions-mi...]
> test[ Actions_TPE_14F]

### 0x8210 WiFi MAC Address `R` TAG=GT len=6

    Data structure: <6Bytes: MAC format>
    BYTE 0~5 : MAC;(MAC format)

> test[11:22:33:44:55:66]=11:22:33:44:55:66

### 0x821d Network Custom `R` TAG=GT len=67

    Data structure:  <Byte : Value>
    BYTE 0~3 : LAN IP address;(Address format)
    BYTE 4~7 : LAN Subnet mask;(Address format)
    BYTE 8~11 : LAN  Gateway;(Address format)
    BYTE 12~15 : LAN DNS server;(Address format)
    BYTE 16~21 : LAN MAC Address;(MAC format)
    BYTE 22~25 : WLAN IP address;(Address format)
    BYTE 26~29 : WLAN Subnet mask;(Address format)
    BYTE 30~33 : WLAN Gateway;(Address format)
    BYTE 34~37 : WLAN DNS server;(Address format)
    BYTE 38~43 : WLAN MAC Address;(MAC format)
    BYTE 44~68 : AP SSID;(ASCII code)

> test[1]=0xc0:0xa8:0x00:0x0a:0xff:0xff:0xff:0x00:0xc0:0xa8:0x00:0x01:0xc0:0xa8:0x01:0x00:0x00:0x00:0x00:0x00:0x00:0x00:0xc0:0xa8:0x64:0x01:0xff:0xff:0xff:0x00:0xc0:0xa8:0x64:0x01:0xc0:0xa8:0x00:0x01:0x00:0x00:0x00:0x00:0x00:0x00:0x45:0x5a:0x70:0x72:0x6f:0x6a:0x65:0x63:0x74:0x6f:0x5f:0x36:0x36:0x37:0x37:0x38:0x38:0x00:0x00

### 0x8220 WiFi Role switch `WR` TAG=GT len=2

    Data structure: <Byte: value>
    BYTE 0: P2P
    BYTE 1: AP & STA

### 0x8222 WiFi mode enable `WR` TAG=GT len=2

    Data structure: <Byte: value>
    BYTE 0: 0=sta, 1=ap, 4=p2p
    BYTE 1: 0=off, 1=off

### 0x8223 WiFi station control `WR` TAG=GT len=1~N

    Data structure: <Byte: value>
    BYTE 0: 0=scan
    1=get scan_result
    2=connect
    3=disconnect
    4=channel (current wifi channel)
    5=signal_level (current connected wifi signal level)
    6=connect_status (0:disconnnect 1: connected)
    BYTE 1~N: ssid or password

### 0x8224 WiFi AP parameter `WR` TAG=GT len=1~N

    Data structure: <Byte: value>
    BYTE 0: 0=set channel
    1=set ssid
    2=set psk
    3=set security
    4=set hidden
    5=set bandwidth
    6=set region ID
    7=set Max Client
    8=set range
    9=signal level (not support)
    10=connected clients number
    BYTE 1~N: value


## audio（2 条）

### 0x8101 Mute `WR` TAG=GT len=1

    Data structure: <1Byte: Index>
    0 : Disable(Un-Mute);
    1 : Enable(Mute);

### 0x8102 Volume `WR` TAG=GT len=2

    Data structure: <2Byte: Integer>
    Byte 0~1: Value
    use signed integer to cast it

> test[7]=7:0


## misc（9 条）

### 0x8910 PTP `WR` TAG=GT len=N

    Data structure: <NByte: Index>
    BYTE0~1:PTP command
    0:status (value1byte: 0=not ready, 1= ready)
    1:capture (value1byte: 1=start capture,)
    2:video (unused)
    3:param_iso(unused)
    4: send requeset (value: PTP container data)
    5: send data(value: PTP container data)
    6: get response (value: PTP response data)
    7: get data(value: PTP data)
    BYTE2~N:value

### 0x890c Pairing Control `WR` TAG=GT len=1

    Data structure: <2Byte: Index>
    BYTE0: pair mode
    0:WPS
    1:MDNS
    2:QRCode
    BYTE1: pair on/off
    0: stop pairing
    1: start pairing

### 0x890d system call `WR` TAG=GT len=n

    Data structure:  String
    System Call

### 0x890e EZAir Mode `WR` TAG=GT len=1

    Data structure:  <1Byte: value>
    BYTE0:
    0: mirror+streaming
    1: mirror only
    10:auto mode

### 0x890f Wall paper `WR` TAG=GT len=4~N

    Data structure:  <4~N Byte: value>
    BYTE0~1: paper type
    0: wall paper
    1: word paper
    BYTE2~3: control
    0: off
    1: on
    BYTE4~5: content
    string for word paper content
    string for wallpaper keyword

### 0x8940 Camera Set Control `W` TAG=GT len=8

    Data structure:  <8 Byte: value>
    BYTE0~3: Control ID (Little Endian, uint32_t)
    BYTE4~7: Control Value (Little Endian, int32_t)

> Control ID from lunux kernel folder in include/uapi/linux/v4l2-controls.h
> Also can use "v4l2-ctl --all" to get Control ID and Control Value
> Example:
> zoom_absolute 0x009a090d
> pan_absolute 0x009a0908
> tilt_absolute 0x009a0909

### 0x8941 Camera Get Control `R` TAG=GT len=4

    Data structure:  <4 Byte: value>
    BYTE0~3: Control ID (Little Endian, uint32_t)

> Control ID from lunux kernel folder in include/uapi/linux/v4l2-controls.h
> Also can use "v4l2-ctl --all" to get Control ID and Control Value
> Example:
> zoom_absolute 0x009a090d
> pan_absolute 0x009a0908
> tilt_absolute 0x009a0909

### 0x8942 Camera Get All Control `R` TAG=GT len=0

    Data structure:  None
    None

> get camera control table data struct
> Refer to:
> https://teamwork.iezvu.com/T14755#342990
> Camera Control over UART – Programming Guide.docx
> Visual Studio 2022 Project Sample Code: UART_Camera_Control.zip

### 0x8911 Low Power Mode On/Off Control `WR` TAG=GT len=0

    Data structure:  <1 Byte: value>
    Byte 0:
    0: disable low power mode (HDMI on and WiFi on)
    1: enable low power mode (HDMI off and WiFi off)
    2: set HDMI output off
    3: set HDMI output on
    4: set WiFi off
    5: set WiFi on

> 發送命令範例:
> 47 54 00 00 11 89 01 00 00 // disable low power mode
> 47 54 00 00 11 89 01 00 01 // enable low power mode
> 47 54 00 00 11 89 01 00 02 // set HDMI output off
> 47 54 00 00 11 89 01 00 03 // set HDMI output on
> 47 54 00 00 11 89 01 00 04 // set WiFi off
> 47 54 00 00 11 89 01 00 05 // set WiFi on
> Rx 回傳值範例: (紅色為讀回的 low power 狀態)
> 47 54 F8 00 11 49 03 00 00 00 00 //  low powe disable
> 47 54 F8 00 11 49 03 00 00 00 01 //  HDMI output offlow powe off
> 47 54 F8 00 11 49 03 00 00 00 02 //  WiFi function off
> 47 54 F8 00 11 49 03 00 00 00 03 //  low power mode enable


## customer（2 条）

### 0x870b Null Console `WR` TAG=GT len=1

    Data structure: String
    BYTE 0 :
    0 = enable console.
    1 = switch debug console to NULL device.
    2= redirect all log to /dev/ttyS0
    3= redirect all log to /dev/NULL

> This command is used in 8269D because there's only one uart port, uart comm and debug console couldn't work simultaniously, so we need a mechanism to swith the functions of the only one uart port.
> 1: To disable debug console. Leave uart for communication. (this sould not work because when console is enabled, uart comm would not work.) to enable uart comm, just restart uart.app -i & in console.
> 0: To enable debug console. Uart comm is disabled after 1 sec.

### 0x870c Product_number `WR` TAG=GT len=5

    Data structure: String
    BYTE 0~1 : Major Number
    BYTE 2~3 : Suber Number
    BYTE 4 : append Number


## keyCode（2 条）

### 0x8401 Keycode `WR` TAG=GT len=4

    Data structure: <4Byte: Value>(Key Code)
    Byte 0: Index of Source
    0 : IR
    1 : Keypad
    2 : keyboard
    3 : lanctrl/Web (only for send key event to DDP)
    Byte 1: Index of Action
    0 : once
    1 : key down
    2 : key up
    3 : key hold
    4 : key long
    Byte 2~3: Index of Key
    0 : Setup
    1 : Enter
    2 : Up
    3 : Down
    4 : Left
    5 : Right
    6 : Back
    7 : Auto
    8 : Blank
    9 : input
    10 : Power
    11 : Freeze
    12 : Mic Volume Up
    13 : Mic Volume Down
    14 : Vol Up
    15 : Vol Down
    16 : Digital Zoom In
    17 : Digital Zoom Out
    18 : HDMI1
    19 : HDMI2
    20 : VGA1
    21 : VGA2
    22 : CVBS
    23 : CVBS2
    24 : SOURCE NONE
    25 : RESERVE2
    26 : Multi_Media
    27 : EzCast
    28 : Miracast
    29 : EzWire
    30 : USB_Connection
    31 : Page Up
    32 : Page Down
    33 : Aspect
    34 : Mute
    35 : Digital Zoom-Panning Up
    36 : Digital Zoom-Panning Down
    37 : Digital Zoom-Panning Left
    38 : Digital Zoom-Panning Right
    39 : Resync
    40 : Test Pattern
    41 : Capture
    42 : Picture
    43 : Smart Eco
    44 : Teaching Templat
    45 : 3D Sync Invert
    46 : Power On
    47 : Power Off
    48 : Menu Hide
    49 : Menu Display
    50 : Quick Install
    51 : Skip_prev
    52 : Play_pause
    53 : Skip_next
    54 : Rewind
    55 : Stop
    56 : Fast_forward
    57 : Network setting
    58 : VK INC
    59 : VK DEC
    60 : HK INC
    61 : HK DEC
    62 : KS_RESET
    63 : DEL
    64 : MODE
    65 : TIME
    66 : Color Level Up (saturation)
    67 : Color Level Down (saturation)
    68 : Brightness Level Up
    69 : Brightness Level Down
    70 : Contrast Level Up
    71 : Contrast Level Down
    72 : Sharpness Level Up
    73 : Sharpness Level Down
    74 : Mute On
    75 : Mute Off
    76 : Image Mute On
    77 : Image Mute Off
    78 : AV Mute On
    79 : AV Mute Off

> Index 66 ~ 79 is for Lanctrl/Web

### 0x8402 Touch Status `WR` TAG=GT len=1

    Data structure: <1Byte: Index>
    0~N: value


## 命令 ID 汇总（54 个）

- `0x8101` audio/Mute
- `0x8102` audio/Volume
- `0x820a` Network/WiFi AP SSID
- `0x820b` Network/WiFi AP KEY
- `0x820c` Network/WiFi STA SSID
- `0x8210` Network/WiFi MAC Address
- `0x821d` Network/Network Custom
- `0x8220` Network/WiFi Role switch
- `0x8222` Network/WiFi mode enable
- `0x8223` Network/WiFi station control
- `0x8224` Network/WiFi AP parameter
- `0x8401` keyCode/Keycode
- `0x8402` keyCode/Touch Status
- `0x861d` system/Projector Name
- `0x8625` system/Language
- `0x8627` system/Source Status
- `0x8628` system/Power Status
- `0x8629` system/Reset All Settings
- `0x8632` system/Firmware Upgrade
- `0x863d` system/FW version of AM
- `0x8644` system/Native Resolution
- `0x864b` system/HDCP Key Status
- `0x864d` system/Factory Test Functions
- `0x8651` system/Log Status
- `0x8652` system/TX version
- `0x8653` system/HDMI Status
- `0x8654` system/uart ready
- `0x8655` system/Remote AM status
- `0x8656` system/Firmware Upgreade Control
- `0x870b` customer/Null Console
- `0x870c` customer/Product_number
- `0x8823` Display/Output Rotation
- `0x8824` Display/USB wire mode
- `0x8825` Display/switch wifi channel
- `0x8829` Display/set overscan
- `0x8830` Display/set portrait
- `0x8831` Display/UI control
- `0x8832` Display/HDMI Enable
- `0x8833` Display/Set Portrait Zoom
- `0x8834` Display/EDID Passthrough
- `0x8835` Display/Mirror Rotation Enable
- `0x8836` Display/Camera Portrait Enable
- `0x8837` Display/Transfer String
- `0x8838` Display/Cast Status
- `0x8839` Display/Encoder Param
- `0x890c` misc/Pairing Control
- `0x890d` misc/system call
- `0x890e` misc/EZAir Mode
- `0x890f` misc/Wall paper
- `0x8910` misc/PTP
- `0x8911` misc/Low Power Mode On/Off Control
- `0x8940` misc/Camera Set Control
- `0x8941` misc/Camera Get Control
- `0x8942` misc/Camera Get All Control
