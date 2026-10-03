#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""电子元件库存管理 CLI。

设计要点（改动本文件前先读这一段）：

1. 本文件是**单文件**项目，但内部按六层严格分层，依赖方向单向：
   常量表 → 归一化 → 匹配 → 数据 → 渲染 → CLI。
   上层可以依赖下层，下层绝不依赖上层。这样以后要在上面再叠一层 Python
   时，直接 `from inventory import load_inventory, search_components` 即可，
   核心逻辑不会连带拉起 argparse。

2. 这个工具的核心不是增删改查，而是**模糊识别**。电子元件的写法极不统一：
   同一个人不同时候会写 `0.1uF` 和 `100nF`，会写 `51R` 和 `51Ω`，
   会用中文「电容」也可能用代号 `C`。所以参数量归一化（parse_quantity）
   是整个项目的技术核心，其余部分都是围绕它的外围设施。

3. 标签永远原样存盘，归一化结果只在查询期派生。这样归一化规则以后改进
   时（比如新增某种记号支持），不需要做数据迁移，重新加载即可。

用法见 `python inventory.py --help`，自检见 `python inventory.py --selftest`。
"""

import argparse
import errno
import json
import math
import os
import re
import shlex
import sys
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

SCHEMA_VERSION = 1
APP_NAME = "component-inventory"

# 退出码。显式定义是为了让 PowerShell 脚本能可靠判断成败——
# 这是「CLI 工具」和「玩具脚本」的分界线。
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOTFOUND = 3
EXIT_DATA = 4


class AppError(Exception):
    """带退出码的应用级错误。"""

    def __init__(self, message, code=EXIT_ERROR):
        super().__init__(message)
        self.code = code


# =============================================================================
# 第 1 层：常量与表
# =============================================================================

# --- SI 前缀 -----------------------------------------------------------------
# 严格模式大小写有意义，这是必须的：m = 毫，M = 兆，语义相反。
PREFIX_STRICT = {
    "p": -12, "n": -9, "u": -6, "m": -3,
    "k": 3, "K": 3, "M": 6, "G": 9, "T": 12,
}

# 宽容兜底表：仅在严格解析失败时启用，用来救「随手打」的大小写，
# 例如用户写 `100NF` 而不是 `100nF`。
#
# m/M 被刻意排除在外。把 M 宽容成 milli 会让 `1M` 变成 1 毫欧，
# 把 m 宽容成 mega 会让 `1m` 变成 1 兆欧——这在电阻场景是灾难级错误。
# 宁可让 `1M` 在无法判断时报弱匹配，也不能猜错。
PREFIX_LENIENT = {"P": -12, "N": -9, "U": -6}

# --- 单位后缀表 ---------------------------------------------------------------
# 按长度降序排列（下面用代码排序，避免手工维护时出错）。
#
# 长度降序是硬要求：`Hz` 必须排在 `h` 前面，否则 `1kHz` 会被拆成
# `k` + `h`(亨利) + 尾部的 `z`。
_UNIT_SUFFIXES_RAW = [
    ("ohms", "resistance"), ("ohm", "resistance"), ("欧姆", "resistance"),
    ("Ω", "resistance"), ("欧", "resistance"), ("r", "resistance"),
    ("farad", "capacitance"), ("法拉", "capacitance"), ("法", "capacitance"),
    ("f", "capacitance"),
    ("henry", "inductance"), ("亨利", "inductance"), ("亨", "inductance"),
    ("h", "inductance"),
    ("hertz", "frequency"), ("赫兹", "frequency"), ("赫", "frequency"),
    ("hz", "frequency"),
    ("volt", "voltage"), ("伏特", "voltage"), ("伏", "voltage"), ("v", "voltage"),
    ("ampere", "current"), ("amps", "current"), ("amp", "current"),
    ("安培", "current"), ("安", "current"), ("a", "current"),
    ("watt", "power"), ("瓦特", "power"), ("瓦", "power"), ("w", "power"),
]
UNIT_SUFFIXES = sorted(_UNIT_SUFFIXES_RAW, key=lambda p: len(p[0]), reverse=True)

# --- 类型别名 ----------------------------------------------------------------
# 规范码 → 别名集合。匹配时两侧都归约到规范码域再比较，
# 而不是给 `C` 挂一堆别名——这样「标签写 C、搜电容器」和
# 「标签写 电容、搜 C」两个方向都成立。
TYPE_ALIASES = {
    "R": ("电阻", "电阻器", "res", "resistor"),
    "C": ("电容", "电容器", "cap", "capacitor"),
    "L": ("电感", "电感器", "ind", "inductor"),
    "D": ("二极管", "diode", "肖特基", "整流管", "稳压管", "齐纳"),
    "Q": ("三极管", "晶体管", "transistor", "bjt", "mos", "场效应管", "mosfet"),
    "U": ("芯片", "ic", "集成电路", "运放", "mcu", "单片机", "稳压器"),
    "J": ("连接器", "接插件", "connector", "插座", "排母", "排针", "端子", "header"),
    "SW": ("开关", "switch", "按键", "轻触开关", "按钮", "拨动开关"),
    "XTAL": ("晶振", "晶体", "谐振器", "crystal", "resonator"),
    "LED": ("发光二极管", "led", "指示灯", "灯珠"),
    "OPTO": ("光耦", "光电耦合器", "optocoupler"),
    "FUSE": ("保险丝", "熔断器", "fuse", "自恢复保险丝"),
    "POT": ("电位器", "可调电阻", "potentiometer", "trimpot", "微调电阻"),
    "RELAY": ("继电器", "relay"),
    "BZ": ("蜂鸣器", "buzzer", "扬声器", "喇叭"),
    "ANT": ("天线", "antenna"),
    "BAT": ("电池", "电池座", "battery"),
    "TP": ("测试点", "testpoint"),
    "X": ("跳线", "跳帽", "jumper", "短接"),
}

# 描述性词 → 类型码。这些词说的是**工艺或颜色**，不是类型本身，但单独出现时
# 基本能确定类型，所以拿来当证据用。
#
# 刻意不走 TYPE_ALIASES，两边都吃亏：
#   那样它们会变成 explicit 级（强度 3），既能压过单位证据——`红 100nF` 会被
#     判成 LED——又因为 type_source == "explicit" 而**不会被补进标签**（补全
#     的判定是「用户已经显式写了类型就不再补」，而「红」并不算显式写了类型）。
#     实测 `add 直插 5mm 红 20mA` 会存成 `5mm 红 20mA`，LED 不见了。
#   走这张表则是弱证据：能补全，也压不过单位和型号。
#
# 有歧义的属性词仍然不收：`功率`（功率电阻 / 功率电感）、`绕线`（绕线电阻 /
# 绕线电感）、`薄膜`（薄膜电容 / 薄膜电阻，已在介质表里）。
DESCRIPTOR_TYPE = {
    "色环": "R", "金属膜": "R", "碳膜": "R", "水泥": "R",
    "工字": "L", "磁环": "L", "磁珠": "L",
    "轻触": "SW", "拨动": "SW", "船型": "SW", "微动": "SW", "钮子": "SW",
    "牛角": "J", "杜邦": "J", "香蕉": "J",
    "微调": "POT",
    "纽扣": "BAT", "锂电": "BAT", "锂电池": "BAT", "磷酸铁锂": "BAT",
    "红": "LED", "绿": "LED", "蓝": "LED", "黄": "LED", "白": "LED",
    "橙": "LED", "紫": "LED", "暖白": "LED", "冷白": "LED",
    "rgb": "LED", "三色": "LED", "七彩": "LED",
}

# 属性词：不是类型，但常与类型连写（`贴片电容`），分词时需要认识它们。
ATTRIBUTE_WORDS = (
    "贴片", "直插", "插件", "卧式", "立式", "表贴", "smd", "tht",
    "无极性", "有极性", "电解", "钽", "薄膜", "绕线", "功率", "高压",
)

# 已知复合词 → 展开结果。显式表优先于通用分词：
# 可预测、可审查、零意外，覆盖绝大多数真实输入。
COMPOUND_EXPANSIONS = {
    "贴片电容": ("贴片", "C"),
    "贴片电阻": ("贴片", "R"),
    "贴片电感": ("贴片", "L"),
    "贴片二极管": ("贴片", "D"),
    "陶瓷电容": ("陶瓷", "C"),
    "瓷片电容": ("瓷片", "C"),
    "铝电解电容": ("电解", "C"),
    "电解电容": ("电解", "C"),
    "钽电容": ("钽", "C"),
    "薄膜电容": ("薄膜", "C"),
    "肖特基二极管": ("肖特基", "D"),
    "整流二极管": ("整流", "D"),
    "稳压二极管": ("稳压", "D"),
    "发光二极管": ("LED",),
    "轻触开关": ("轻触", "SW"),
    "拨动开关": ("拨动", "SW"),
    "自恢复保险丝": ("自恢复", "FUSE"),
    "绕线电感": ("绕线", "L"),
    "功率电感": ("功率", "L"),
}

# 类型码 → 物理维度。用来从元件的类型标签推出「查询词该按什么维度解释」。
TYPE_TO_DIM = {"R": "resistance", "C": "capacitance", "L": "inductance"}

# 存量粗略等级的中文名，下标即等级值。
COARSE_LABELS = ("无", "极少", "少", "多", "极多")

# --- 封装表 -------------------------------------------------------------------
# 三类必需标签里，封装是唯一靠「查表」识别的一类。用死名单而不是通用正则，
# 是因为名单可审查、零意外；而通用的「字母+数字」正则会猜错——它会把 NE555、
# LM358 这些型号全吞成封装。
PACKAGE_SIZE_CODES = frozenset({
    # 英制（EIA）
    "0201", "0402", "0603", "0805", "1206", "1210", "1806", "1812", "2010", "2512",
    # 公制
    "1005", "1608", "2012", "3216", "3225", "4516", "4532", "5025",
})

# 不带引脚数的封装名（存规范化键，比对时大小写与分隔符都不敏感）。
PACKAGE_NAMES = frozenset({
    "qfn", "dfn", "bga", "lga", "plcc", "melf", "dpak", "d2pak", "toll",
    "sma", "smb", "smc", "ll34", "dip", "soic", "sop", "sot", "sod",
})

# 安装方式词。严格说这不是封装而是安装方式，但 `C 贴片 100nF` 这类写法很常见，
# 算作封装可以让这些元件通过校验。
_PACKAGE_MOUNT_MAP = {
    "贴片": "贴片", "表贴": "贴片", "smd": "贴片",
    "直插": "直插", "插件": "直插", "tht": "直插",
    "卧式": "卧式", "立式": "立式",
}

# 「确实没有封装」的哨兵出口。散装件、拆机件、自制模块、电池座、测试点本来
# 就没有封装，没有这个出口用户会被逼着随便填一个 0805，那比不校验更糟。
# 接受几种写法，但统一存成中文，这样 `search 无封装` 能一次列全。
PACKAGE_NONE = "无封装"
_PACKAGE_NONE_KEYS = frozenset({"无封装", "不适用", "nopackage", "nopkg"})

# 「族名 + 数字」的封装模式。族名必须逐个列举，且后面**必须跟数字**——
# 否则 `TOMATO` 会被 `TO` 前缀吃掉。
_PACKAGE_FAMILY_RE = re.compile(
    r"^(?P<fam>PDIP|DIP|SOIC|SOP|SSOP|TSSOP|MSOP|QSOP|QFN|DFN|LQFP|TQFP|QFP|"
    r"PLCC|BGA|LGA|SOT|SOD|TO|DO|SC)"
    r"[-_ ]?(?P<num>\d[A-Za-z0-9\-]*)$",
    re.IGNORECASE,
)

_PACKAGE_EXACT = {}
for _code in PACKAGE_SIZE_CODES:
    _PACKAGE_EXACT[_code] = _code
for _name in PACKAGE_NAMES:
    _PACKAGE_EXACT[_name] = _name.upper()
_PACKAGE_EXACT.update(_PACKAGE_MOUNT_MAP)
for _key in _PACKAGE_NONE_KEYS:
    _PACKAGE_EXACT[_key] = PACKAGE_NONE
del _code, _name, _key

# --- 机械尺寸 -----------------------------------------------------------------
# 有一大类元件根本没有半导体封装，它们用尺寸、脚距、Case 码来描述：钽电容是
# Case A~E，铝电解是 `5x11`，排针是 `2.54`，直插 LED 是 `5mm`，电池是 `18650`。
# 这些和 SOIC / 0805 一样，回答的都是「这个元件长成什么形状」，所以共用封装槽
# ——用户不需要知道这是两套语汇。
#
# 脚距只收英寸换算值。不用 `^\d+\.\d+$` 那种通用小数正则，是因为 `0.5` / `1.5` /
# `2.0` 都是**合法的电阻值**，吞掉它们等于把参数当成了封装。
_PACKAGE_PITCH = frozenset({"1.27", "2.54", "3.81", "5.08", "7.62"})

# 直插 LED 的直径。
_PACKAGE_LED_DIA = frozenset({"3mm", "5mm", "8mm", "10mm"})

# 电池型号本身就是尺寸。
_PACKAGE_BATTERY = frozenset({"18650", "21700", "14500", "10440", "16340", "26650"})

# 钽电容的 Case 码。裸单字母只收 A / B / E——`C` 和 `D` 是类型码，canon_type 在
# classify_tags 里先跑，`C` 永远到不了封装这一层；而且即便有人直接调
# canon_package("D")，返回「Case D」也太容易误导。带字面量的形式认全部五个。
_PACKAGE_CASE_BARE = frozenset({"a", "b", "e"})
_PACKAGE_CASE_RE = re.compile(
    r"^(?:case[-\s_]?(?P<plain>[a-e])|(?P<pre>[a-e])[-\s_]?case|(?P<suffix>[a-e])型)$",
    re.IGNORECASE,
)

# 尺寸码：`5x11` / `6.3x11` / `6x6x5`。分隔符统一成小写 x，结尾的单位可写可不写。
#
# 单位只在**这个分支内**剥掉，不做全局处理：`5mm` 是直插 LED 的直径，走上面那张
# 白名单；全局剥 mm 会把它变成裸 `5`，那张白名单就永远命不中了。而带单位的
# 尺寸写法反而更常见——`5x11mm` 是铝电解规格书上的印法，漏掉它用户就会在
# 被追问封装时重打一遍尺寸，标签里留下两份。
_PACKAGE_DIM_RE = re.compile(
    r"^(?P<dim>\d+(?:\.\d+)?(?:[xX×*]\d+(?:\.\d+)?){1,2})(?:mm)?$",
    re.IGNORECASE,
)


def _canon_mech_package(t):
    """机械尺寸的归约，不是机械尺寸返回 None。

    单独一个函数，是因为这一类的形态都是正则匹配而非查表，和上面那些
    frozenset 的处理方式不一样。
    """
    key = _pkg_key(t)
    if key in _PACKAGE_PITCH or key in _PACKAGE_LED_DIA or key in _PACKAGE_BATTERY:
        return key
    if key in _PACKAGE_CASE_BARE:
        return f"Case {key.upper()}"
    m = _PACKAGE_CASE_RE.match(t)
    if m:
        return f"Case {(m['plain'] or m['pre'] or m['suffix']).upper()}"
    m = _PACKAGE_DIM_RE.match(t)
    if m:
        # 只取 dim 那一段，结尾的 mm 被正则本身吃掉——于是 `5x11mm` 和 `5x11`
        # 归一到同一个封装，搜哪个都能找到对方。
        return re.sub(r"\s*[xX×*]\s*", "x", m["dim"]).lower()
    return None


# --- 安装方式 -----------------------------------------------------------------
# 这是封装的**派生属性**，不存盘。存了就会和封装字段冗余、可能不一致，而且老
# 数据补不上——用户库里现成的元件不会因为改了代码就凭空长出「直插」标签。搜索
# 时现算，反而永远和下面这张判定表保持一致。
_MOUNT_SMD, _MOUNT_THT = "贴片", "直插"

# 贴片族：除 DIP 外的全部半导体封装。
_MOUNT_SMD_PACKAGES = frozenset({
    "qfn", "dfn", "bga", "lga", "plcc", "melf", "dpak", "d2pak", "toll",
    "sma", "smb", "smc", "ll34", "soic", "sop", "ssop", "tssop", "msop",
    "qsop", "lqfp", "tqfp", "qfp", "sot", "sod", "sc",
})
_MOUNT_THT_PACKAGES = frozenset({"dip", "pdip"})

# TO 和 DO 两个族内部直插贴片都有，得按编号分：TO-92 / TO-220 是直插功率管，
# 而 TO-252 / TO-263 就是 DPAK / D2PAK，是贴片的；DO-35 / DO-41 是轴向直插
# 二极管，DO-214 才是贴片（它另有 SMA / SMB / SMC 的名字，不走这条）。
_MOUNT_THT_TO = frozenset({"92", "126", "220", "247", "3P", "18", "39"})
_MOUNT_SMD_TO = frozenset({"252", "263", "268"})
_MOUNT_THT_DO = frozenset({"35", "41", "201"})

# 两数尺寸 `5x11`。圆柱铝电解的标法是「直径 x 高」，所以第二个数更大；
# 贴片铝电解正好相反（`6.3x5.4`，高小于直径）。靠这个天然把两者分开，
# 不用再维护一张例外名单。
_MOUNT_TWO_DIM_RE = re.compile(r"^(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)$")


def package_mount(pkg):
    """封装的安装方式。判不出来返回 None——「不知道」比猜一个强。

    判不出来的要老实说不知道：`6x6x5` 的轻触开关直插贴片都有，`18650` 电池
    两种都有，`无封装` 更是无从谈起。返回 None 只是意味着「搜直插/贴片时
    不会命中它」，不会造成错误结果。
    """
    if not pkg:
        return None
    key = _pkg_key(pkg)

    # 安装方式词自身。
    own = _PACKAGE_MOUNT_MAP.get(key)
    if own in (_MOUNT_SMD, _MOUNT_THT):
        return own

    if pkg in PACKAGE_SIZE_CODES:
        return _MOUNT_SMD
    if pkg.startswith("Case "):
        return _MOUNT_SMD

    if key in _PACKAGE_PITCH or key in _PACKAGE_LED_DIA:
        return _MOUNT_THT
    if key in _PACKAGE_BATTERY:
        return None

    two = _MOUNT_TWO_DIM_RE.match(key)
    if two:
        # 高大于直径才是圆柱铝电解；反过来的标法是贴片电解。
        return _MOUNT_THT if float(two[2]) > float(two[1]) else None
    if _PACKAGE_DIM_RE.match(pkg):
        return None  # 三数尺寸（6x6x5 这类），直插贴片都有

    m = _PACKAGE_FAMILY_RE.match(pkg)
    if m:
        fam, num = m["fam"].upper(), m["num"].upper()
        if fam == "TO":
            if num in _MOUNT_SMD_TO:
                return _MOUNT_SMD
            if num in _MOUNT_THT_TO:
                return _MOUNT_THT
            return None
        if fam == "DO":
            return _MOUNT_THT if num in _MOUNT_THT_DO else None
        # 其余族名查两张名单——DIP 也走这条路（`DIP-8` 带编号时由族名正则
        # 接住，裸 `DIP` 则由函数末尾的 _MOUNT_THT_PACKAGES 接住）。
        fam_low = m["fam"].lower()
        if fam_low in _MOUNT_THT_PACKAGES:
            return _MOUNT_THT
        return _MOUNT_SMD if fam_low in _MOUNT_SMD_PACKAGES else None

    if key in _MOUNT_SMD_PACKAGES:
        return _MOUNT_SMD
    if key in _MOUNT_THT_PACKAGES:
        return _MOUNT_THT
    return None


def _package_rank(value):
    """封装候选的优先级。安装方式最泛，其余同等具体。

    `贴片` / `直插` 严格说不是封装而是安装方式，只是因为 `C 贴片 100nF` 这种
    写法太常见才收进封装表。一旦同时出现真正的封装（`5x11`、`0805`、`DIP-8`），
    就该让它让位。
    """
    return 0 if _pkg_key(value) in _PACKAGE_MOUNT_MAP else 1


# REPL 缺封装时弹的询问语。**问句和输入点分成两条**：问句本身就 80 列宽，和输入点挤在
# 同一行的话，提示符会被推到终端右半边、窄窗口下还要折行；分开以后问句单独占一行，
# 输入点短且位置固定。措辞集中在这里，交互层只管缩进和取值。
PACKAGE_QUESTION = "请输入封装（如 0805 / SOT-23 / 5x11 / 2.54 / 直插；确实没有就输入 NO PACKAGE）"
PACKAGE_PROMPT = "封装 > "

# REPL 缺存量时的询问语，同样拆成问句和输入点（这句 99 列宽，更得拆）。
STOCK_QUESTION = "请输入数量（qty23 = 23 个；或无 / 极少 / 少 / 多 / 极多，英文 none/few/some/many/lots；回车跳过）"
STOCK_PROMPT = "数量 > "

# --- 存量标签 -----------------------------------------------------------------
# 存量可以不写选项、直接作为标签给出。三种写法等价：
#   qty23 / level1               带前缀的数字
#   无 / 极少 / 少 / 多 / 极多    中文等级词，就是 COARSE_LABELS 那五档
#   none / few / some / many / lots  英文等级词，同一架刻度（见下面 STOCK_WORDS）
#
# 不带前缀的裸数字刻意不接受：粗略存量本来就是 0-4，`3` 到底是「多」还是
# 「3 个」说不清，与其靠数值范围猜，不如要求写清楚。
#
# 这条只管**标签位置**。stock 命令的值位置只有一个 token，`3` 在那里没有第二
# 种解释，所以 `stock #7 3` 是精确 3 个——值位置的那套写法在 parse_stock_value
# 里，两处不能合并，理由见那个函数。
#
# 负号也进正则，好让 `qty-3` 走到「不能为负」的报错，而不是静默当成普通标签存进去。
STOCK_TAG_RE = re.compile(r"^(qty|level)(-?\d+)$", re.IGNORECASE)

# 中文等级词 → 档位。正式名从 COARSE_LABELS 派生，避免两处各写一份；
# 口语同义词按**语义强度**归位，不是当成等价词——`很少` 比 `少` 更少所以归极少，
# `很多` 比 `多` 更多所以归极多。这样五档各自都有口语对应，将来真想区分
# 「多」和「很多」还有余地。
#
# 英文同义词按同一架刻度排：none < few < some < many < lots 恰好五档，
# 一格一个。这也不是逐词翻译——few 严格说更贴「很少」——而是让英文自己
# 构成一条单调刻度：用户不必记住某档对应哪个词，只要知道词越大库存越多。
# 英文正好补上了「少」这一档，中文那边 2 档一直没有口语同义词。
#
# `no` 刻意不收：它是封装哨兵 `NO PACKAGE` 的第一个词，命令行标签按空格分词，
# 用户照文档敲 `add C 0805 100nF NO PACKAGE`（不加引号）时 `NO` 会被静默当成
# 存量「无」。`none` 已经覆盖了这个意思，不值得冒这个险。
#
# 词表刻意保守：只收含义没有争议的。`挺多的`、`不多了` 这类带程度修饰的说法
# 不收——它们的边界模糊，收进来只会让「这算哪一档」变成新的猜测点。
STOCK_WORDS = {label: i for i, label in enumerate(COARSE_LABELS)}
STOCK_WORDS.update({
    "没有": 0, "none": 0, "empty": 0, "zero": 0,
    "很少": 1, "一点点": 1, "few": 1, "scarce": 1,
    "some": 2, "little": 2, "low": 2,
    "不少": 3, "many": 3, "several": 3,
    "很多": 4, "大量": 4, "lots": 4, "plenty": 4, "tons": 4,
})

# 查表用的 casefold 索引。normalize_text 刻意不做 casefold（M 与 m 语义相反，
# 见那里的注释），所以大小写不敏感只能在这一层单独做——存量词里没有任何
# 物理量语义，casefold 是安全的，`NONE` / `None` / `none` 都该认。这也和
# `QTY23` / `Level2` 已经大小写不敏感（STOCK_TAG_RE 的 re.IGNORECASE）一致。
#
# 写成一个推导式而不是像 _build_reverse_index 那样逐个登记冲突：这张表的键
# 都是普通词，两个键只差大小写是纯粹的笔误，自检里一条长度断言就能钉住。
_STOCK_WORDS_CI = {w.casefold(): lv for w, lv in STOCK_WORDS.items()}

# --- 介质表 -------------------------------------------------------------------
# 电容的「构造」维度：MLCC / 电解 / 钽 / 薄膜 / 云母。
#
# 它既不是类型也不是封装，而是参数的一种——但必须单独一张表，因为
# `陶瓷` / `瓷片` / `MLCC` 得能互相检索到。否则用户按哪种写法录的，就只能
# 按哪种写法搜：搜 MLCC 找不到写「陶瓷」的，搜「陶瓷电容」也找不到写 MLCC 的。
MEDIUM_ALIASES = {
    "MLCC": ("mlcc", "陶瓷", "瓷片", "陶瓷电容", "瓷片电容", "独石", "片式陶瓷"),
    "电解": ("电解", "铝电解", "电解电容", "铝电解电容", "铝电解电容器"),
    "钽": ("钽", "钽电容", "钽电解"),
    "薄膜": ("薄膜", "薄膜电容", "涤纶", "聚酯", "涤纶电容"),
    "云母": ("云母", "云母电容"),
}

MEDIUM_TO_CANON = {}
_MEDIUM_CONFLICTS = []


def _build_medium_index():
    """构建 介质别名 → 规范词 的反向索引，并登记冲突。

    与 _build_reverse_index 同样处理：同一个别名映射到多个规范词是真实的
    风险（比如「陶瓷」既可以指介质也可以指别的），静默取最后一个会让结果
    随字典顺序漂移。显式登记，由 --selftest 暴露。
    """
    for code, aliases in MEDIUM_ALIASES.items():
        for alias in (code,) + tuple(aliases):
            key = alias.casefold()
            existing = MEDIUM_TO_CANON.get(key)
            if existing is not None and existing != code:
                _MEDIUM_CONFLICTS.append((alias, existing, code))
                continue
            MEDIUM_TO_CANON[key] = code


_build_medium_index()

# --- 型号表 -------------------------------------------------------------------
# 这是拦住 `1N4148` 这类陷阱的唯一手段。实测 parse_quantity("1N4148") 返回
# 1.4148e-9——`_INFIX_RE` 把 N 当成了纳。不拦的话，一个二极管会被惯例推断
# 成**电容**，而猜错的结果是要写进盘的。
#
# 只用逐个族列举的正则，要求字母段大写（`1n0` 是 1.0nF 的中缀写法，不该命中）。
#
# 每条模式都是「字母段 + 数字」的形态，加之前确认过不会撞上单位解析：
# `7805` / `16MHz` / `0805` 各自走自己的路，互不干扰。
PARTNO_PATTERNS = (
    (re.compile(r"^1N\d{2,}$"), "D"),                       # 1N4001..1N5822
    (re.compile(r"^2N\d{3,}$"), "Q"),                       # 2N2222 / 2N3904
    (re.compile(r"^2S[ABCJK]\d+$"), "Q"),                   # 2SC1815 / 2SA1015 / 2SK / 2SJ
    (re.compile(r"^(?:SS|SR|FR|UF|HER|MUR|ES)\d+$"), "D"),  # 肖特基 / 快恢复
    (re.compile(r"^(?:BAT|BAV|BAS|BZX|BZT|1SS)\d+"), "D"),  # BAT54 是二极管
    (re.compile(r"^(?:BC|BD|BSS|BSC)\d+"), "Q"),
    (re.compile(r"^(?:IRF|IRL|IRFP|IRFZ|IRLZ|AO|AON|SI|FQP|FQPF|CJ)\d+"), "Q"),
    (re.compile(r"^(?:MMBT|KSP)\d+"), "Q"),                 # MMBT3904 / KSP2222
    (re.compile(r"^(?:PC|EL|LTV|TLP|MOC|6N|4N)\d{2,}"), "OPTO"),   # PC817 / MOC3021 / 6N137 / 4N25
    (re.compile(r"^(?:NE|LM|TL|OP|AD|LT|MAX|AMS|TPS|MP|XL|MT|CH|RT|SY)\d{2,}"), "U"),
    (re.compile(r"^(?:STM|GD|ESP|RP|ATMEGA|ATTINY|PIC|NRF)"), "U"),
    (re.compile(r"^(?:78|79)(?:L|M|S)?\d{2,}$"), "U"),      # 7805 / 78L05 / 7912
    (re.compile(r"^(?:MPU|ULN)\d{4}"), "U"),                # MPU6050 / ULN2003
    (re.compile(r"^DS\d[A-Z0-9]{3,}$"), "U"),               # DS18B20 / DS1307 / DS3231
    (re.compile(r"^(?:HT|ME|XC|SGM)\d{3,}"), "U"),          # 常见 LDO 厂牌
    (re.compile(r"^(?:TDA|TEA)\d{3,}"), "U"),               # 音频功放
    (re.compile(r"^WS\d{4}"), "LED"),                       # WS2812
    (re.compile(r"^(?:XH|VH|PH|ZH|GH|EH|MX|JST)[\d.]+$"), "J"),   # XH2.54 这类连接器系列
)

# 显式型号表，**优先于上面的模式表**。
#
# 模式表是按前缀猜的，猜错了没法补救；这张表是逐个型号写死的，可以精确
# 修正模式的误判。SS8050 / SS8550 是最典型的例子：`SS` 前缀在模式表里
# 归二极管（SS14 确实是肖特基），但这两位是长电的 NPN / PNP 三极管，
# SOT-23 封装，而且大概是国产零件盒里最常见的三极管。
#
# 键用 normalize_text 后的原样大小写比对，不做 casefold——型号的大小写
# 有意义，小写 `ss14` 不该命中。
EXPLICIT_PARTNO = {
    "SS8050": "Q", "SS8550": "Q",
    "S8050": "Q", "S8550": "Q",
    "S9012": "Q", "S9013": "Q", "S9014": "Q", "S9015": "Q", "S9018": "Q",
}

# --- 惯例表 -------------------------------------------------------------------
# 「裸前缀」惯例：标签没有明确单位时（`10k` 而不是 `10kΩ`），按行业习惯解释。
# 只在标签解析不出明确维度时才用。
#
# 收录：小写 p → C，小写 k → R，大写 M → R。
#
# 刻意不收录：
#   u  —— µF 与 µH 两可。4u7 既可能是 4.7µF 电容也可能是 4.7µH 电感，
#         两者都是常见值，靠数值大小区分会在最常用的 1µ~100µ 区间失效。
#   n  —— 同样的两可，只是没 u 那么显眼：nF 与 nH 都常见，100nH 在 0805
#         封装里是常规值。既然 u 报了歧义，n 就没有理由静默猜成电容。
#   m  —— mΩ / mH / mF 三可，而且 m/M 是本项目最危险的坑。
#   K  —— 大写 K 与 EIA 容差码结构上完全同形：104K 是 100nF±10%，470K 是
#         470kΩ，任何规则都无法区分。既然区分不了就不猜，让用户写小写 k
#         或者显式给类型。中缀位置是安全的（104K 的 K 在末尾，1K22 是
#         1.22kΩ 的标准写法），所以 INFIX 收录 K 而 TERMINAL 不收录。
#   G/T —— GΩ/TΩ 极罕见。
TERMINAL_CONVENTION = {"p": "C", "k": "R", "M": "R"}
INFIX_CONVENTION = {"p": "C", "k": "R", "K": "R", "M": "R"}

# 这几个裸前缀在电容和电感上都有常见值，推不出类型，必须让用户写清。
# p 刻意不在表里——pH 几乎不存在，22p 猜电容是安全的。于是 p 与 n 写法
# 对称而命运不同，这是照「现实中哪个更常见」定的，不是照对称性定的。
_AMBIGUOUS_PREFIX = frozenset({"u", "n", "m"})

# 惯例推断的量级合理性区间。挡住 EIA 码冒充：104M = 1.04e8 超出电阻的合理范围。
PLAUSIBLE = {"R": (1e-3, 1e7), "C": (1e-13, 1e-1), "L": (1e-10, 1e0)}

# 维度 → 类型码（TYPE_TO_DIM 的反转）。
DIM_TO_TYPE = {v: k for k, v in TYPE_TO_DIM.items()}

# 推断来源的强度。取最高强度；同强度内出现两个不同码就报错，绝不按标签顺序任选。
#
# medium 与 convention 同级是刻意的：`薄膜` 既可能指薄膜电容也可能指薄膜电阻，
# 所以它只配当一份弱证据。这样 `薄膜 100k` 会因为 medium→C 与 convention→R
# 同级不同码而报出冲突，而不是静默选一个。
_STRENGTH = {
    "explicit": 3,
    "partno": 2, "dimension": 2,
    "medium": 1, "descriptor": 1, "convention": 1,
}

# 物理维度到类型的**单向**兜底，刻意不进 TYPE_TO_DIM。
#
# TYPE_TO_DIM 是双向用的（dim_hint_for_tags 靠它反查元件类型对应的维度），把
# XTAL → frequency 塞进去会让晶振元件上的裸数字被解释成频率。而这里只用在一个
# 方向：一个频率标签能提示这是晶振，但晶振只是频率的可能来源之一
# （`STM32F103 16MHz` 里的 16MHz 说的是主频）。强度压在 convention 级，任何
# 型号表或显式类型都能压过它。
_DIM_FALLBACK_TYPE = {"frequency": "XTAL"}

# --- 反向索引（模块加载时构建一次） ------------------------------------------
ALIAS_TO_CODE = {}
_ALIAS_CONFLICTS = []


def _build_reverse_index():
    """构建 别名 → 规范码 的反向索引，并登记冲突。

    同一个别名映射到多个规范码是真实存在的风险（比如「排针」既可算 J
    也可算独立类型）。静默取最后一个是**最坏**的选择——那样匹配结果会
    随字典顺序变化而漂移。这里显式登记，由 --selftest 暴露出来。
    """
    for code, aliases in TYPE_ALIASES.items():
        for alias in (code,) + tuple(aliases):
            key = alias.casefold()
            existing = ALIAS_TO_CODE.get(key)
            if existing is not None and existing != code:
                _ALIAS_CONFLICTS.append((alias, existing, code))
                continue
            ALIAS_TO_CODE[key] = code


_build_reverse_index()

# 分词用的词汇表：全部 casefold 存放。CJK 的 casefold 是自身，不受影响。
VOCAB = set(ALIAS_TO_CODE) | {w.casefold() for w in ATTRIBUTE_WORDS}

# 中缀记号：`4R7` / `1k2` / `2M2` / `4u7` / `R47`。
#
# sep 集合刻意不含 `e`/`E`，这样 `1e-7` 不会被误判成中缀记号，
# 而是落到常规分支由 float() 直接吃下科学计数法。
_INFIX_RE = re.compile(
    r"^(?P<a>\d*)(?P<sep>[RrKkMmGgUuNnPp])(?P<b>\d+)(?P<tail>.*)$"
)

# 常规形式：数字（含科学计数法）+ 可选尾部。
_PLAIN_RE = re.compile(
    r"^(?P<num>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(?P<tail>.*)$"
)

_HEX_RE = re.compile(r"^[0-9a-fA-F-]+$")


# =============================================================================
# 第 2 层：归一化
# =============================================================================


@lru_cache(maxsize=None)
def normalize_text(s):
    """文本预处理。必须最先做，且只做一次。

    NFKC 处理全角数字与全角空格；然后统一三个视觉相同但码点不同的字符：
    MICRO SIGN (U+00B5) 和 GREEK MU (U+03BC) 都归到 ASCII `u`，
    OHM SIGN (U+2126) 归到 GREEK CAPITAL OMEGA (U+03A9)。
    元件库里这几种写法都会出现，不合并就会漏匹配。

    注意这里**不做 casefold**。casefold 会把 M 和 m 混掉，
    而这两个在本项目语义相反（兆 vs 毫），是致命错误。
    大小写只在解析时逐段、有策略地处理。
    """
    if not isinstance(s, str):
        s = str(s)
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("µ", "u").replace("μ", "u")
    s = s.replace("Ω", "Ω")
    return s.strip()


@dataclass(frozen=True, slots=True)
class Quantity:
    """一个已归一化的物理量。

    value 已换算到 SI 基本单位（Ω / F / H / V / A / Hz / W）。
    dim 为 None 表示「数值可信但维度未知」（如 `1k`、`2M2`），
    这是**一等的受支持状态**，不是错误。硬猜维度会在搜 `4k7` 时
    把电感和电阻混在一起。
    """

    value: float
    dim: str | None
    raw: str
    confidence: str  # "strong" | "weak"

    def __str__(self):
        return f"{self.value:g}{self.dim or '?'}"


def _split_tail(tail):
    """拆分「单位前面的部分」，返回 (前缀指数, 维度, 是否合法)。

    tail 是数字后面剩下的部分，例如 `100nF` 的 `nF`、`51R` 的 `R`、`1k` 的 `k`。
    """
    if tail == "":
        return 0, None, True

    low = tail.lower()
    for alias, dim in UNIT_SUFFIXES:  # 已按长度降序排列
        if len(tail) < len(alias):
            continue
        if low[-len(alias):] != alias.lower():
            continue
        head = tail[: len(tail) - len(alias)]
        if head == "":
            return 0, dim, True
        if head in PREFIX_STRICT:
            return PREFIX_STRICT[head], dim, True
        if head in PREFIX_LENIENT:
            return PREFIX_LENIENT[head], dim, True
        return 0, None, False  # 前缀非法，整个 tail 作废

    # 没有单位符号：整个 tail 只能是一个裸前缀（`1k`、`100n`）。
    if tail in PREFIX_STRICT:
        return PREFIX_STRICT[tail], None, True
    if tail in PREFIX_LENIENT:
        return PREFIX_LENIENT[tail], None, True
    return 0, None, False


def _hint_eligible(num_s):
    """判断一个裸数字是否允许用元件的类型提示去推断维度。

    前导零的数字被排除，这一条专门用来挡住封装代码：`0402` / `0603` /
    `0805` 都是四位数且带前导零，若允许 hint 推断，在电阻元件上
    `0805` 会被解释成 805Ω，与同元件的标签 `805Ω` 撞上，产生莫名其妙的命中。

    注意 `0.1` 这种小数不受影响（小数点不是前导零）。
    """
    s = num_s.lstrip("+-")
    if len(s) > 1 and s[0] == "0" and s[1] != ".":
        return False
    return True


@lru_cache(maxsize=None)
def parse_quantity(token, hint=None):
    """把标签或查询词解析成 Quantity，解析不出来返回 None。

    返回 None 表示「这不是一个物理量」，调用方应退化为字符串匹配。
    判定分三档：
      strong —— 有明确单位符号，或有中缀字母（`100nF`、`51R`、`4R7`）
      weak   —— 科学计数法，或只有裸前缀（`1e-7`、`1k`、`100n`）
      none   —— 无单位、无前缀、无指数（`51` 裸数字、`0805` 封装、`贴片`）

    裸数字 `51` 只有在给了 hint 时才会被解释成物理量（对电阻即 51Ω）。
    这正是「维度歧义推迟到有上下文时才消解」的实现：查询词层面猜维度
    一定会猜错，而元件的类型标签提供了可靠的上下文。
    """
    t = normalize_text(token)
    if not t:
        return None

    # --- 中缀记号：4R7 / 1k2 / 2M2 / 4u7 / R47 / 0R05 ---
    m = _INFIX_RE.match(t)
    if m:
        a, sep, b, tail = m["a"], m["sep"], m["b"], m["tail"]
        # `4R7` 里的 7 是小数点后一位，`0R05` 里的 05 是小数点后两位，
        # 所以分母是 10 的「b 的位数」次方，而不是 10 的「b 的数值」次方。
        mantissa = (int(a) if a else 0) + int(b) / (10 ** len(b))

        if sep in ("R", "r"):
            # R 的双重身份之一：这里它是小数点，同时顺带确定了维度是电阻。
            # 另一个身份（`51R` 里的欧姆单位）由常规分支处理——
            # 两者靠正则结构天然消歧：只有「字母后面还跟着数字」才走中缀，
            # `51R` 的 R 后面没有数字，所以自动落到常规分支。
            # 不需要额外逻辑，但重构时极易踩坑，故写在这里。
            prefix_exp = 0
            dim_from_sep = "resistance"
        else:
            prefix_exp = PREFIX_STRICT.get(sep)
            if prefix_exp is None:
                prefix_exp = PREFIX_LENIENT.get(sep)
            if prefix_exp is None:
                return None
            dim_from_sep = None  # `1k2` / `4u7` 的维度待定

        # 尾部的裸前缀被忽略：中缀记号自己已经确定了数量级，
        # `1k2Ω` 这种写法里的 Ω 只提供维度，不再叠加前缀。
        _tail_prefix, dim, ok = _split_tail(tail)
        if not ok:
            return None
        if dim is None:
            dim = dim_from_sep or hint
        return Quantity(mantissa * (10.0 ** prefix_exp), dim, token, "strong")

    # --- 常规形式：100nF / 51R / 0.1uF / 1e-7F / 1k / 100n / 51 ---
    m = _PLAIN_RE.match(t)
    if not m:
        return None
    num_s, tail = m["num"], m["tail"]
    try:
        base = float(num_s)
    except ValueError:
        return None

    prefix, dim, ok = _split_tail(tail)
    if not ok:
        return None
    value = base * (10.0 ** prefix)
    has_exp = "e" in num_s.lower()

    if dim is not None:
        return Quantity(value, dim, token, "strong")
    if hint is not None and _hint_eligible(num_s) and (prefix != 0 or has_exp):
        # `1e-7` 在电容元件上就是 1e-7 F
        return Quantity(value, hint, token, "strong")
    if has_exp:
        return Quantity(value, None, token, "weak")
    if prefix != 0:
        return Quantity(value, None, token, "weak")
    if hint is not None and _hint_eligible(num_s):
        # 裸数字 `51` 在电阻元件上就是 51Ω
        return Quantity(value, hint, token, "weak")
    return None


def quantities_equal(a, b, rel_tol=1e-9):
    """判断两个物理量是否等价。

    rel_tol 取 1e-9 的依据：解析路径最多两次乘法，IEEE754 双精度的相对
    误差量级在 1e-16，`0.1e-6` 与 `1e-7` 的实测相对差约 1.4e-16，
    1e-9 有七个数量级余量。上界方面，电阻 E96 系列相邻档最小比值约 1.02，
    任何接近 1e-2 的容差都会让 `1.0k` 与 `1.1k` 判等，那是对库存语义的破坏。
    """
    if a is None or b is None:
        return False
    # 维度闸门：维度都明确且不同，永不相等。这是防误命中的第一道防线。
    if a.dim is not None and b.dim is not None and a.dim != b.dim:
        return False
    # 零值单独处理：math.isclose 的相对容差在任一操作数为 0 时退化为
    # abs_tol 比较，而 0Ω 是合法值（跳线电阻），必须能和自己相等。
    if a.value == 0.0 or b.value == 0.0:
        return a.value == b.value
    return math.isclose(a.value, b.value, rel_tol=rel_tol, abs_tol=0.0)


@lru_cache(maxsize=None)
def expand_compound(token):
    """把可能由多个词粘成的 token 拆开，返回若干片段。

    两级策略：显式表优先（`贴片电容` → `贴片` + `C`），
    未命中时用正向最大匹配兜底。
    """
    t = normalize_text(token)
    if t in COMPOUND_EXPANSIONS:
        return tuple(COMPOUND_EXPANSIONS[t])

    out = []
    residual = False
    i, n = 0, len(t)
    while i < n:
        matched = False
        for j in range(min(n, i + 6), i, -1):  # 最长 5 字窗口
            piece = t[i:j]
            key = piece.casefold()
            if key in VOCAB:
                out.append(ALIAS_TO_CODE.get(key, piece))
                i = j
                matched = True
                break
        if not matched:
            residual = True
            i += 1  # 单字吞掉，不可切

    # 有字符没能被词汇表覆盖时，整段切分不可信。典型反例是 `capacitor`：
    # 最大匹配会贪婪地切出 `cap` + `c` + `r` 三块碎片，而它们拼出的语义
    # 与原文毫无关系，按「各部分都要命中」处理就永远搜不到东西。
    # 这种情况退回原 token，交给别名表和字符串匹配去处理。
    if residual:
        return (t,)

    # 返回 tuple 而不是 list：本函数带 lru_cache，缓存的是同一个对象，
    # 返回可变类型会让调用方有机会污染缓存。
    return tuple(out)


@lru_cache(maxsize=None)
def canon_type(token):
    """把任意写法归约到规范类型码，无法归约返回 None。"""
    t = normalize_text(token)
    if not t:
        return None
    if t in TYPE_ALIASES:  # 已经是规范码，大小写敏感，优先
        return t
    code = ALIAS_TO_CODE.get(t.casefold())
    if code is not None:
        return code
    # 复合词：取展开结果里第一个类型码。
    # 注意 `电阻电容` 这类会展开成 [R, C]，这里返回 R——这是已知的
    # 不精确之处，但比返回 None（完全匹配不上）要好。
    for part in expand_compound(t):
        if part in TYPE_ALIASES:
            return part

    # 子串回退：token 里含有类型别名，例如 `金属膜电阻` 含 `电阻`。
    # 两道限制缺一不可：
    #   长度 >= 2 —— 否则 `X7R` 会因为含一个 `x` 被误判成跳线，
    #                `C0G` 会因含一个 `c` 被误判成电容。
    #   ASCII 要在词边界 —— 否则 `pic16f877` 里的 `ic` 会把它判成芯片。
    low = t.casefold()
    best_alias, best_code = "", None
    for alias, code in ALIAS_TO_CODE.items():
        if len(alias) < 2 or len(alias) <= len(best_alias):
            continue
        if alias not in low:
            continue
        if alias.isascii() and not _boundary_ok(low, alias):
            continue
        best_alias, best_code = alias, code
    return best_code


def dim_hint_for_tags(tags):
    """从元件的标签集合推出物理维度提示（供 parse_quantity 使用）。"""
    for tag in tags:
        code = canon_type(tag)
        if code in TYPE_TO_DIM:
            return TYPE_TO_DIM[code]
    return None


def _pkg_key(s):
    """封装查表用的规范键：去掉空格、连字符、下划线并转小写。

    于是 `SOT-23` / `SOT23` / `sot 23` 是同一个键。
    """
    return re.sub(r"[\s\-_]+", "", s).casefold()


@lru_cache(maxsize=None)
def canon_package(token):
    """把标签归约到规范封装名；不是封装则返回 None。

    与 canon_type 同层：都是「这个标签属于哪个词汇表」的判定，都不做推断。
    """
    t = normalize_text(token)
    if not t:
        return None
    hit = _PACKAGE_EXACT.get(_pkg_key(t))
    if hit is not None:
        return hit
    mech = _canon_mech_package(t)
    if mech is not None:
        return mech
    m = _PACKAGE_FAMILY_RE.match(t)
    if m:
        return f"{m['fam'].upper()}-{m['num']}"
    return None


@lru_cache(maxsize=None)
def canon_medium(token):
    """把标签归约到规范介质词；不是介质则返回 None。

    与 canon_type / canon_package 同层同风格。介质是**参数**的一种，
    不是第四类标签——「三类必需标签」的模型不变。
    """
    t = normalize_text(token)
    if not t:
        return None
    return MEDIUM_TO_CANON.get(t.casefold())


@lru_cache(maxsize=None)
def _partno_type(token):
    """型号 → 类型码。长度少于 3 一律不看，字母段要求原样大写。

    这是拦住 `1N4148` 的唯一手段：不拦的话它会走惯例推断，而
    parse_quantity("1N4148") 给出 1.4148e-9（N 被当成纳），一个二极管就
    被猜成了电容。两字符的 `M7` / `K2` 碰撞概率太高，交给用户显式指定。
    """
    t = normalize_text(token)
    if len(t) < 3:
        return None
    explicit = EXPLICIT_PARTNO.get(t)
    if explicit is not None:
        return explicit
    for pattern, code in PARTNO_PATTERNS:
        if pattern.match(t):
            return code
    return None


@lru_cache(maxsize=None)
def _convention_letter(token):
    """token 是「数字 + 裸 SI 字母」形态时返回 (字母, "infix" 或 "terminal")。

    只在标签解析不出明确维度时才有意义——有单位的话走维度推断，那条路可靠得多。
    """
    t = normalize_text(token)

    m = _INFIX_RE.match(t)
    if m:
        if m["a"] == "":
            # `M7` / `K2` / `N1` 这类整数部分为空的形态一律拒绝：丝印 M7 是
            # 常见的二极管，而 parse_quantity("M7") 会给出 700000（M 当兆）。
            return None
        if m["tail"]:
            return None
        if len(m["b"]) > 2:
            # `1n4148` 这种：中缀部分长达四位。常规中缀最多两位小数（4k7 / 1k22），
            # 四位小数是型号 1N4148 的小写形式，不是惯例写法。
            return None
        return m["sep"], "infix"

    m = _PLAIN_RE.match(t)
    if not m:
        return None
    num_s, tail = m["num"], m["tail"]
    if not tail or "e" in num_s.lower() or "." in num_s:
        return None
    if not _hint_eligible(num_s):
        return None  # 前导零是封装特征（0805 / 0603）
    return tail, "terminal"


def _plausible(value, code):
    """惯例推断出的量级是否落在该类型的合理范围里。"""
    lo, hi = PLAUSIBLE[code]
    return lo <= abs(value) <= hi


# --- 显示槽位 -----------------------------------------------------------------
#
# 标签在显示时按语义分列，列序固定。槽位名同时是分类的产物（TagPlan.slot_of）
# 与渲染层的列序——**两边共用这一份字面量**，不各写一套，否则迟早漂移成两种排法。
#
#   类型 → 主值 → 电气参数 → 介质 → 封装 → 其它
#
# `C` 和 `1uF` 是元件的身份，挨在一起；耐压、功率、容差是附加条件，跟在主值后面
# （`C 1uF 16V MLCC 0805` 读起来是「一个 1 微法、耐压 16 伏的 0805 陶瓷电容」）；
# 介质说明它是哪一类构造，封装收口；认不出语义的一律甩到最后——「共阴绿红」
# 这种描述词插在中间会把前面几列的节奏打断，而它恰恰是最不影响识别的那部分。
SLOT_ORDER = ("type", "main", "elec", "medium", "package", "other")

# 归到「电气参数」而不是「主值」的物理维度。其余维度都算主值，**包括 dim 为
# None 的裸前缀与中缀写法**（`1k` / `4k7` / `3p` / `22p`）——dim 是 None 不等于
# 「不是数值」，它只是维度歧义被刻意留到有上下文时再消解（见 Quantity 的注释），
# 而它在显示上就是元件的那个值本身，放主值列才对。
_ELEC_DIMS = frozenset({"voltage", "power", "current"})

# 容差 `1%` / `5%`。它不是一个物理量，parse_quantity 认不出（`%` 不在单位表里），
# 所以槽位判定里得单独留一条，否则电阻的精度会被甩到「其它」列去。
_TOLERANCE_RE = re.compile(r"^\d+(?:\.\d+)?%$")


@dataclass
class TagPlan:
    """标签分类的结果。

    刻意**永不抛异常**——把所有问题当成数据返回。这样 run_selftest 能直接断言
    issues 的内容，不必去捕获 AppError；用户可见的错误由 render_issues 渲染。
    """

    type_code: str | None = None
    type_source: str | None = None       # explicit / partno / dimension / medium / convention
    type_evidence: tuple = ()            # 导致该类型的那些原始标签
    package: str | None = None
    medium: str | None = None            # 用户写下的介质（规范词）
    params: tuple = ()
    issues: tuple = ()                   # (kind, detail)
    added_type: str | None = None        # 需要补进 tags 的类型码
    added_medium: str | None = None      # 需要补进 tags 的介质词
    absorbed: tuple = ()                 # 被封装吸收掉的冗余标签，由 cmd_add 从 tags 里删掉
    slot_of: tuple = ()                  # 与传入 tags **逐位对齐**，每项是 SLOT_ORDER 之一

    def has(self, kind):
        return any(k == kind for k, _ in self.issues)


def classify_tags(tags, extra_package=None):
    """把标签分成 类型 / 封装 / 参数 三类，给出补全建议与问题清单。

    关键是区分**类型标签**和**类型证据**：`1uF` 的 canon_type 是 None，
    它是参数标签，只是恰好能推断出类型。不分开的话 `1uF 0805 16V` 会被
    误判成「已经有类型了」。

    extra_package 是给交互模式用的：调用方已经就封装问过用户、拿到了明确答复，
    这个值绕过封装表——**用户说是什么就是什么**。命令行没有这条通道，只能靠
    查表，认不出就报错。

    副产品 slot_of 与传入的 tags 逐位对齐，记录每个标签属于哪个**显示槽位**
    （见 SLOT_ORDER）。渲染层分列、以及 canonical_tags 重排，都只认它，不另写
    一套判定——判定顺序只此一份。
    """
    cands = []       # (类型码, 来源, 原始标签)
    packages = []
    media = []
    params = []
    ambiguous = []   # µ 前缀这类真实歧义
    slots = []       # 与 tags 逐位对齐的槽位名，每轮循环恰记一笔

    for tag in tags:
        code = canon_type(tag)
        if code is not None:
            cands.append((code, "explicit", tag))
            slots.append("type")
            continue

        pkg = canon_package(tag)
        if pkg is not None:
            # 封装必须排在数值之前判。今天 parse_quantity 不传 hint，`2.54` 和
            # `18650` 恰好解析不出来，撞不上；一旦将来给数值解析传元件级 hint，
            # 这两个会变成电阻值，与封装双命中。顺序跟着判定的优先级走。
            packages.append(pkg)
            slots.append("package")
            continue

        med = canon_medium(tag)
        if med is not None:
            # 介质算参数：它确实是这条元件的有效规格，所以 `C 0805 MLCC`
            # 满足「至少一个非类型非封装的标签」，而 `C 0805` 不满足。
            media.append(med)
            params.append(tag)
            # 顺带当类型证据。MEDIUM_ALIASES 那五个词全是电容的构造方式，
            # 出现即指向 C——`薄膜 104 100V` 里 104 和 100V 都推不出类型，
            # 靠的就是这一条。强度压在 medium 级，压不过型号表和单位，
            # 所以 `薄膜 100k` 会如实报出冲突而不是静默选一个。
            cands.append(("C", "medium", tag))
            slots.append("medium")
            continue

        desc = DESCRIPTOR_TYPE.get(tag.casefold())
        if desc is not None:
            # 中文描述性词：`色环` / `轻触` / `红` 这些。与介质同为弱证据，
            # 理由见 DESCRIPTOR_TYPE 的定义处。
            #
            # 槽位算「类型」：`黄 led 直插` 里的 `黄` 和 `led` 说的是同一件事的
            # 两个侧面（什么颜色、什么东西），分成两列反而读不成句。注意
            # canon_type 不查这张表，所以这一支必须自己记槽位。
            cands.append((desc, "descriptor", tag))
            params.append(tag)
            slots.append("type")
            continue

        pn = _partno_type(tag)
        if pn is not None:
            # 型号进「主值」列：对 `D 1N4148 SOD-123` 来说 1N4148 就是它的身份，
            # 和 `1uF` 之于电容没有区别。
            cands.append((pn, "partno", tag))
            params.append(tag)
            slots.append("main")
            continue

        q = parse_quantity(tag)
        if q is not None:
            if q.dim in DIM_TO_TYPE:
                cands.append((DIM_TO_TYPE[q.dim], "dimension", tag))
            elif q.dim in _DIM_FALLBACK_TYPE:
                cands.append((_DIM_FALLBACK_TYPE[q.dim], "convention", tag))
            else:
                conv = _convention_letter(tag)
                if conv is not None:
                    letter, position = conv
                    table = INFIX_CONVENTION if position == "infix" else TERMINAL_CONVENTION
                    guess = table.get(letter)
                    if guess is not None and _plausible(q.value, guess):
                        cands.append((guess, "convention", tag))
                    elif letter in _AMBIGUOUS_PREFIX:
                        # 连前缀字母一起记下来，render_issues 才不用回头去猜
                        # 用户写的是 u 还是 n。
                        ambiguous.append((letter, tag))
            params.append(tag)
            # 耐压 / 功率 / 电流是附加条件，其余维度（含 dim 为 None 的裸前缀与
            # 中缀）都是元件的主值。
            slots.append("elec" if q.dim in _ELEC_DIMS else "main")
            continue

        # 落不进任何一类的残差。容差是这里唯一还认得出来的语义（`1%` 不是物理量，
        # parse_quantity 认不出），其余一律进「其它」列，排在最后。
        params.append(tag)
        slots.append("elec" if _TOLERANCE_RE.match(normalize_text(tag)) else "other")

    # 封装是单值字段，而 `C 直插 5x11 100uF` 里两个标签都会命中封装。取最具体的
    # 那个——安装方式太泛，一旦同时有真正的封装就该让位。稳定排序，同级保持书写顺序。
    if packages:
        packages.sort(key=lambda p: -_package_rank(p))

    issues = []
    type_code = None
    type_source = None
    evidence = ()

    if cands:
        top = max(_STRENGTH[s] for (_, s, _) in cands)
        top_codes = {c for (c, s, _) in cands if _STRENGTH[s] == top}
        if len(top_codes) > 1:
            # 同强度出现两个不同的码，说明标签本身自相矛盾（1uF 与 100Ω 并存）。
            # 绝不按标签顺序或字典顺序任选一个——那会让结果随输入顺序漂移。
            issues.append(("type_conflict",
                           tuple((c, s, t) for (c, s, t) in cands if _STRENGTH[s] == top)))
        else:
            type_code = top_codes.pop()
            best = [(s, t) for (c, s, t) in cands if c == type_code]
            type_source = max(best, key=lambda x: _STRENGTH[x[0]])[0]
            evidence = tuple(t for (_, t) in best)
    elif ambiguous:
        issues.append(("micro_ambiguous", tuple(ambiguous)))
    else:
        issues.append(("type_missing", tuple(tags)))

    if not packages and extra_package is None:
        issues.append(("package_missing", tuple(tags)))
    if not params:
        issues.append(("param_missing", tuple(tags)))

    added_type = None
    if type_code is not None and type_source != "explicit":
        added_type = type_code

    # 片式电容自动认作 MLCC。两个条件缺一不可：
    #   「片式尺寸码」是判据的核心——钽电容用 A/B/C/D/E case 码而不是 0805，
    #       铝电解根本用不了这个尺寸，所以片式尺寸码 + 电容在实践中基本等同
    #       陶瓷（少数片式薄膜电容是已知例外）。
    #   「没有任何介质词」是给例外留的门——用户写了 `薄膜` 就不该被硬塞 MLCC。
    # 直插电容不会被自动补：同一个「直插」下可能是陶瓷圆片、电解圆柱、薄膜方块，
    # 推不出来，这类元件要用户自己写 MLCC 或 陶瓷。
    added_medium = None
    if (type_code == "C" and not media
            and any(p in PACKAGE_SIZE_CODES for p in packages)):
        added_medium = "MLCC"

    package = packages[0] if packages else extra_package

    # 冗余的安装方式标签：既然封装已经把它表达出来了，留着只是重复。只在两者的
    # 安装方式**互相印证**时才吸收——`直插 0805` 这种自相矛盾的输入要原样保留，
    # 让用户看见；`直插 18650` 也不吸收，因为 18650 派生不出安装方式，那个标签
    # 还有信息量。
    #
    # 这里只**报告**该吸收谁，真正的删除在 cmd_add 里做：本函数始终只读，自检里
    # 有断言钉着这条。
    mount = package_mount(package) if package else None
    absorbed = ()
    if mount is not None:
        absorbed = tuple(p for p in packages[1:] if package_mount(p) == mount)

    return TagPlan(
        type_code=type_code,
        type_source=type_source,
        type_evidence=evidence,
        package=package,
        medium=media[0] if media else None,
        params=tuple(params),
        issues=tuple(issues),
        added_type=added_type,
        added_medium=added_medium,
        absorbed=absorbed,
        slot_of=tuple(slots),
    )


# 单位维度的规范符号。只用于「数字 + ASCII 单位简写」这一种形态的改写。
_CANON_UNIT = {
    "resistance": "Ω",
    "capacitance": "F",
    "inductance": "H",
    "frequency": "Hz",
    "voltage": "V",
    "current": "A",
    "power": "W",
}


@lru_cache(maxsize=None)
def canon_unit(tag):
    """把一个标签的**单位写法**规整到规范形式，认不出这种形态就原样返回。

    只有「数字 + ASCII 单位简写」这一种形态需要规整，因为它是全项目唯一
    「同一个量有好几种写法」的地方：`51r` / `51R` / `51Ω` 是一回事，
    `0.25w` / `0.25W` 是一回事。别的标签没有可规整的余地——类型词和介质词是
    用户的词汇，描述词是用户的话，封装名在判定时就已经被 canon_package
    规约过一次了（规约结果只用于比较，不改写用户写下的字）。

    四条边界，都是有意的：

    1. **不做浮点往返。** 只对原字符串做后缀替换，数字字面量一个字符都不动。
       所以 `0.1uF` 不会因为「换算成 100nF 更整齐」而变，`1e-7F` 也不会被重排成
       另一种写法。重新格式化数值是另一件事——那会连「我当初写的是多少」一起
       抹掉，而这个库的价值恰恰在于它记的是你写的东西。
    2. **中文单位原样保留**（`1欧` / `1伏` / `1瓦`）。那是同一件事的另一种语言，
       不是同一种写法的两种拼法。ASCII 的简写与全称（`r` / `ohm`）才算写法差异，
       统一归到符号。
    3. **`M` 与 `m` 永不互换**，前缀一律照抄。它俩在本项目语义相反（兆 vs 毫），
       整个 normalize_text 不做 casefold 就是为了这条。
    4. **电阻带非歧义前缀时省略 Ω**：`10kR` → `10k`。裸前缀 `10k` 已经是本项目
       认的电阻惯例（惯例表里 k/M 指向电阻），省掉冗余的 Ω 更接近手写习惯。
       但 `u`/`n`/`m` 是歧义前缀（`1m` 说不清是毫欧还是别的），所以 `1mR` 保留
       Ω 写成 `1mΩ`。判据直接复用 _AMBIGUOUS_PREFIX，不另立一套。
    """
    t = normalize_text(tag)
    m = _PLAIN_RE.match(t)
    if m is None:
        return t
    num, tail = m.group("num"), m.group("tail")
    if not tail:
        return t
    low = tail.lower()
    for alias, dim in UNIT_SUFFIXES:  # 已按长度降序：`Hz` 必须排在 `h` 前面
        if len(tail) < len(alias) or not low.endswith(alias.lower()):
            continue
        if not alias.isascii():
            return t  # 中文单位不归符号域
        head = tail[: len(tail) - len(alias)]
        if head and head not in PREFIX_STRICT and head not in PREFIX_LENIENT:
            # 前缀非法，整个尾巴作废——`1N4148` 的 `N4148` 走这一支，
            # 于是二极管型号不会被改写成 `1N4148F` 之类。
            return t
        if dim == "resistance" and head and head not in _AMBIGUOUS_PREFIX:
            return num + head
        return num + head + _CANON_UNIT[dim]
    return t


def slot_groups(tags):
    """按显示槽位把标签分组，槽内保持原有相对顺序。

    返回 {槽位: [标签…]}，键固定是 SLOT_ORDER 那六个（没用到的槽是空列表）。
    分槽的判据全部来自 classify_tags 的 slot_of，这里只做分组，一个谓词都不重复。
    """
    groups = {s: [] for s in SLOT_ORDER}
    # zip 不会截断出错：slot_of 由 classify_tags 逐位生成，长度必然相等。
    for tag, slot in zip(tags, classify_tags(tags).slot_of):
        groups[slot].append(tag)
    return groups


def canonical_tags(tags):
    """标签的规范形式：先规整写法，再按槽位重排。

    这是**存盘与显示共用的唯一入口**。存盘路径（save_inventory）对每条记录跑
    一遍，于是「规范化」和「迁移」是同一件事——写入一次，库里所有记录的标签
    一起收敛，不需要单独的迁移步骤。这个函数是幂等的，第二遍不会再变。

    写法规整在重排之前做：槽位判定认的是规整后的写法（`10kR` 与 `10k` 都落主值，
    但先归一再判，省得将来两种写法判出两个槽）。
    """
    fixed = [canon_unit(t) for t in tags]
    groups = slot_groups(fixed)
    return [tag for slot in SLOT_ORDER for tag in groups[slot]]


def resolve_package_answer(tags, answer):
    """用户对封装追问的回答 → (追加后的标签, 表外封装名或 None)。

    交互模式问「封装是什么」时，用户答的是一句话，而库里存的是标签。直接把整句
    当一条标签存下来会出事：`直插 2.54` 这条标签带空格，而词表查的是去掉空格后的
    键（`直插2.54`），谁也认不出它，于是「直插」和「2.54」两个词一起失效——本该是
    封装的东西落进了显示层的「其它」列。

    所以分三档，从紧到松：

    1. **整句本身就是一个封装名**（`SOT23` / `Case A` / `NO PACKAGE`）→ 用规范名。
       带空格的真封装名和哨兵靠这一档，所以它们仍然存成一条标签、仍然是规范写法。
       这一档必须排在第二档前面：先拆的话 `Case A` 会变成 `Case` + `A` 两条，
       规范写法就丢了。
    2. **拆成词再和已有标签拼回去重判**——复用命令行那套切分（`tokenize_query`）
       与同一个 `classify_tags`，规则只有一份。判得出封装就按普通标签收下，
       于是 `直插 2.54` 变成两个普通标签，`直插` 还会被 `2.54` 吸收掉（`2.54`
       派生出的安装方式就是直插），跟 `add C 直插 5x11 100uF` 的行为一致。
    3. **还是认不出 → 整句当一条封装名存下来**。这是录入词表之外的封装的唯一途径，
       所以不能拆散：拆成两条谁也不认，`classify_tags` 会报 package_missing，
       整条 add 直接失败。

    第二档**只**要求判得出封装，不额外要求「合并后没有别的问题」：万一用户答的
    东西和已有标签冲突（电容上答 `100kΩ`），让 cmd_add 照常报类型冲突更好，
    加守卫只会退回第三档，把一句有问题的回答静默存成封装名。
    """
    canon = canon_package(answer)
    if canon is not None:
        return tags + [canon], None

    words = tokenize_query(answer)
    if len(words) > 1:
        merged = tags + words
        if classify_tags(merged).package is not None:
            return merged, None

    return tags + [answer], answer


_TYPE_HINT = "C R L D Q U J SW XTAL LED OPTO FUSE POT RELAY BZ ANT BAT TP X"

# 自动补全类型时，用来向用户说明推断依据的短语。
_SOURCE_LABEL = {
    "partno": "这是常见型号",
    "dimension": "单位可以确定",
    "medium": "该介质只能是电容",
    "descriptor": "这是行业里常见的叫法",
    "convention": "按行业惯例",
}


def render_issues(plan, tags):
    """把 TagPlan 里的问题渲染成人能读的文本。一次列出全部问题，不修一个报一个。"""
    shown = " ".join(tags)
    lines = []
    for kind, detail in plan.issues:
        if kind == "type_missing":
            lines.append(
                "无法确定元件类型：标签里既没有类型词，也没有能推断出类型的参数。\n"
                f"  标签：{shown}\n"
                "  补法：在最前面加上类型码，例如  C 0805 100nF\n"
                f"  可用类型码：{_TYPE_HINT}"
            )
        elif kind == "micro_ambiguous":
            # detail 是 (前缀字母, 原始标签) 的列表——前缀是报歧义时一起记下来的，
            # 这样文案能说清是哪个字母含糊，不用回头猜用户写的是 n 还是 u。
            picked = "、".join(f"`{t}`" for _letter, t in detail)
            letters = []
            for letter, _t in detail:
                if letter not in letters:
                    letters.append(letter)
            marks = " / ".join(f"`{l}`" for l in letters)
            sample = detail[0][1]
            lines.append(
                f"无法判断 {picked} 是电容还是电感——裸的 {marks} 前缀两者都可能"
                "（这些量级上 nF 与 nH、µF 与 µH 都是常见值）。\n"
                f"  补法：写出单位或类型，例如  100nF / 4.7uF，或者  C {sample}、L {sample}"
            )
        elif kind == "type_conflict":
            pairs = "、".join(f"{c}（来自 `{t}`）" for (c, _, t) in detail)
            lines.append(
                f"标签里出现了多个类型：{pairs}。一条记录只能有一个类型。\n"
                "  补法：去掉其中一个，或在最前面显式写出你要的类型。"
            )
        elif kind == "package_missing":
            lines.append(
                "没有找到封装标签。\n"
                f"  标签：{shown}\n"
                "  补法：加上封装，例如  C 0805 100nF 50V\n"
                "  常见封装：0402 0603 0805 1206 / SOT-23 SOD-123 / DIP-8 SOIC-14 / 直插 贴片\n"
                f"  若这个元件确实没有封装（散装、模块、自制件、电池座等），写  {PACKAGE_NONE}"
            )
        elif kind == "param_missing":
            lines.append(
                "除了类型和封装，至少还要有一个参数标签。\n"
                f"  标签：{shown}\n"
                "  补法：加上容值 / 耐压 / 型号，例如  C 0805 100nF 50V"
            )
    # 块与块之间空一行，而不是紧挨着。每个块只有标题顶格、续行缩进两格，若用一个 \n
    # 拼起来，第二个问题的标题就贴在上一块的缩进续行正下方，读起来像是那行漏了缩进，
    # 而不是一个新问题。空一行才能看清「顶格的那句 = 新问题的标题」。
    return "\n\n".join(lines)


def _is_word_char(c):
    """字符是否算「词字符」，用于子串匹配的边界检查。

    只有 ASCII 字母数字算。CJK 刻意不算——中文没有词边界概念，
    若把 CJK 也算作词字符，`电容` 就无法子串命中 `贴片电容`，
    而那是完全合法且期望的匹配。
    """
    return c.isascii() and c.isalnum()


def _boundary_ok(haystack, needle):
    """needle 在 haystack 中是否存在「两侧都是词边界」的出现位置。"""
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx < 0:
            return False
        before_ok = idx == 0 or not _is_word_char(haystack[idx - 1])
        after = idx + len(needle)
        after_ok = after == len(haystack) or not _is_word_char(haystack[after])
        if before_ok and after_ok:
            return True
        start = idx + 1


# =============================================================================
# 第 3 层：匹配
# =============================================================================


def tokenize_query(query):
    """把查询串切成词。

    用 shlex 是为了支持 `"100 nF"` 这种带空格的单个词。必须 posix=False，
    否则 Windows 路径里的反斜杠会被当成转义符吃掉。
    逗号刻意不切分：`1,000Ω` 里的逗号是千分位，切开会造成灾难。
    """
    try:
        parts = shlex.split(query, posix=False)
    except ValueError:
        parts = query.split()
    out = []
    for p in parts:
        p = p.strip()
        if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
            p = p[1:-1]
        if p:
            out.append(p)
    return out


def _substring_score(q, tag):
    """子串匹配打分，带边界检查。返回 0 表示不匹配。

    边界检查是防误命中的关键：`100nF` 作为查询词若不加限制，
    会子串命中 `1100nF`。要求匹配位置前后是非词字符即可挡住绝大多数
    数值型误命中，代价极低。
    """
    ql, tl = q.casefold(), tag.casefold()
    if ql == tl:
        return 0.0  # 全等由上一层处理
    if ql in tl:
        if not _boundary_ok(tl, ql):
            return 0.0
        short, long_ = len(q), len(tag)
    elif tl in ql:
        if not _boundary_ok(ql, tl):
            return 0.0
        short, long_ = len(tag), len(q)
    else:
        return 0.0
    if short < 2:
        # 单字符子串匹配噪声太大：查 `C` 会命中所有含 c 的标签。
        # 单字符的类型查询由「类型归约」那一层负责，那里是精确的。
        return 0.0
    return 0.4 + 0.3 * (short / long_)


def match_token(q, comp):
    """一个查询词对一个元件的最佳匹配。返回 (分数, 命中原因)。

    分两层调度：句柄（#序号 / id 前缀）直接判定；复合词（`贴片电容`）
    展开后要求**每一部分都命中**，取最弱一环的分数。单层判定本身在
    _match_tags 里——包含判定直接用它，绕开上面这两层。
    """
    qn = normalize_text(q)
    if not qn:
        return 0.0, None

    # --- 第 0 层：句柄 ---
    if qn.startswith("#") and qn[1:].isdigit():
        if comp.seq == int(qn[1:]):
            return 1.0, f"#{comp.seq}"
        return 0.0, None
    if len(qn) >= 4 and _HEX_RE.match(qn):
        cat = comp.id.lower().replace("-", "")
        if cat.startswith(qn.lower().replace("-", "")):
            return 1.0, f"id:{qn}"

    # --- 复合词：修饰语和主词都要满足 ---
    # `贴片电容` 展开成 `贴片` + `C`，两者都得命中才算数。
    # 若只取 `C` 而不要求 `贴片`，一个直插电容也会被搜出来；
    # 若退化到子串匹配，`贴片` 会去撞标签 `贴片` 而放过主词，
    # 结果就是把贴片**电阻**也列出来——两种都是错的。
    #
    # 已知限制：`电阻电容` 这种把一个类型词并列的写法会被展开成 [R, C]，
    # 要求元件同时是两者，因而永远搜不到。这种输入用 -A 或分开搜。
    parts = expand_compound(qn)
    if len(parts) > 1:
        scores, reasons = [], []
        for part in parts:
            s, r = _match_tags(part, comp.tags)
            if s <= 0:
                return 0.0, None
            scores.append(s)
            reasons.append(r)
        return min(scores), "  ".join(reasons)

    return _match_tags(qn, comp.tags)


def _match_tags(qn, tags):
    """单个（已展开的）词对**一组标签**的分层匹配，首个成功即返回。

    首个成功即返回是刻意的：这样高分的精确匹配不会被低分的降级路径覆盖。

    只吃标签列表而不是整个元件，是为了让包含判定（find_containment）也用得上
    这份分层。它问的是「这两组标签是不是同一批东西」，不该经过 match_token
    外面那两层**只服务于查询**的调度：句柄层（`#7` 是一条标签时不该去命中序号）
    和复合词展开层（一个标签 `贴片电容` 不该被当成「同时有 贴片 和 C」）。

    qn 假定已过 normalize_text，hint 一律从 tags 自身的类型标签推。
    """
    hint = dim_hint_for_tags(tags)
    qq = parse_quantity(qn, hint=hint)

    # --- 第 1 层：物理量等价 ---
    if qq is not None:
        best = None
        for tag in tags:
            tq = parse_quantity(tag, hint=hint)
            if tq is None or not quantities_equal(qq, tq):
                continue
            # 双方维度都明确才算强匹配；靠 hint 补出来的算弱匹配，
            # 分数低一档，排序时自然靠后。
            strong = qq.dim is not None and tq.dim is not None
            score = 1.0 if strong else 0.7
            if best is None or score > best[0]:
                best = (score, f"{qn} ≈ {tag}")
        if best:
            return best
        # 查询词是一个明确的物理量，但库里没有任何标签与它数值等价。
        # 到此为止，**不再降级**到类型/字符串层。
        #
        # 这一条挡住的是一个很隐蔽的误命中：`0.1uF` 与 `1uF` 相差十倍，
        # 但子串匹配会因为 `.` 不是词字符而放行（`1uF` 确实是 `0.1uF` 的
        # 子串，且起点前是 `.`）。把「数值确定不等」降级成一次字符串巧合，
        # 是模糊匹配里最伤信任的一类错误——搜不到只是麻烦，搜错了会让人
        # 再也无法判断结果可不可信。
        return 0.0, None

    # --- 第 2 层：类型归约 ---
    qc = canon_type(qn)
    if qc is not None:
        for tag in tags:
            if canon_type(tag) != qc:
                continue
            if normalize_text(tag) == qn:
                return 1.0, tag
            return 0.9, f"{qn} → {tag}"

    # --- 第 2.5 层：介质归约 ---
    # 让 `陶瓷` / `瓷片` / `MLCC` 互相检索得到，也让 `钽` 这种单字词不必依赖
    # 子串匹配——单字被子串层的防护挡着（那是为了防止 `C` 匹配一堆无关标签），
    # 而放宽那条例外会牵动 ASCII 单字符的行为，不值得。
    qm = canon_medium(qn)
    if qm is not None:
        for tag in tags:
            if canon_medium(tag) != qm:
                continue
            if normalize_text(tag) == qn:
                return 1.0, tag
            return 0.9, f"{qn} → {tag}"

    # --- 第 2.6 层：封装归约 ---
    # 与介质归约同构。加机械尺寸之后，同一件事有了多种写法（`A` 与 `Case A`、
    # `SOT23` 与 `SOT-23`），没有这一层它们就互相搜不到——而那正是这个项目
    # 承诺要解决的事。
    qp = canon_package(qn)
    if qp is not None:
        for tag in tags:
            if canon_package(tag) != qp:
                continue
            if normalize_text(tag) == qn:
                return 1.0, tag
            return 0.9, f"{qn} → {tag}"

    # --- 第 3 层：字符串全等 ---
    qkey = qn.casefold()
    for tag in tags:
        if normalize_text(tag).casefold() == qkey:
            return 1.0, tag

    # --- 第 3.5 层：安装方式归约 ---
    # `直插` / `贴片` 往往不是任何一个标签，而是封装的派生属性：用户只写了
    # `5x11`（圆柱铝电解的尺寸），但那就是直插的。排在字符串层**之后**，
    # 这样标签里真写了「直插」的元件仍拿满分，靠封装推出来的拿 0.9。
    #
    # 那个 `in` 守卫不能省：_PACKAGE_MOUNT_MAP 里还有 `卧式` / `立式`，它们
    # 不是安装方式、package_mount 也永远不会返回它们；不挡住的话，搜「卧式」
    # 会被这一层当成安装方式查询，把原有的字符串匹配行为打坏。
    qmt = _PACKAGE_MOUNT_MAP.get(_pkg_key(qn))
    if qmt in (_MOUNT_SMD, _MOUNT_THT):
        for tag in tags:
            pkg = canon_package(tag)
            if pkg is not None and package_mount(pkg) == qmt:
                return 0.9, f"{pkg}（{qmt}）"

    # --- 第 4 层：子串 ---
    best = None
    for tag in tags:
        s = _substring_score(qn, normalize_text(tag))
        if s > 0 and (best is None or s > best[0]):
            # 用 `~` 而不是数学的 ⊂：后者 GBK 编不出来，在代码页 936 的
            # 控制台上会直接抛 UnicodeEncodeError。
            best = (s, f"{qn} ~ {tag}")
    if best:
        return best

    return 0.0, None


@dataclass
class SearchHit:
    component: object
    score: float
    per_token: list  # [(分数, 原因, 词), ...]


def search_components(components, tokens, any_mode=False, limit=None):
    """AND 检索：元件必须为每个查询词都提供至少一个命中。

    any_mode 打开时改为 OR，并按命中词数归一化分数。
    """
    if not tokens:
        return []

    results = []
    for comp in components:
        per = []
        failed = False
        for q in tokens:
            s, reason = match_token(q, comp)
            if s <= 0 and not any_mode:
                failed = True
                break
            per.append((s, reason, q))
        if failed:
            continue

        hits = [p for p in per if p[0] > 0]
        if not hits:
            continue

        scores = [p[0] for p in hits]
        # 均值体现整体命中质量，最小值体现「最弱一环」。混合两者可以
        # 避免某个词的高分掩盖另一个词的勉强命中。
        total = 0.7 * (sum(scores) / len(scores)) + 0.3 * min(scores)
        if any_mode:
            total *= len(hits) / len(tokens)
        results.append(SearchHit(comp, total, per))

    results.sort(key=lambda h: (-h.score, h.component.seq))
    if limit is not None:
        results = results[:limit]
    return results


# 包含判定的得分门槛。匹配层里 ≥0.9 的来源只有这么几种：字符串全等（1.0）、
# 双方维度都明确的物理量等价（1.0）、类型/介质/封装/安装方式四种归约（0.9）。
# 它们共同的含义是「同一条信息的另一种写法」，正是集合包含要的语义。
#
# 子串层（0.4~0.657）刻意不算：它是为检索的召回服务的，而且双向对称——
# `共阴` 与 `共阴绿红` 会互相子串命中，于是「新元件更宽泛」被算成「等价」，
# 方向还会翻。检索要召回，包含要精确，两者共用分层内核但不能共用门槛。
_CONTAINMENT_MIN = 0.9

# 关系 → 展示措辞。只在这里出现一次：命令行的报错正文和交互模式的追问
# 上下文共用它，同一条规则不允许有两种说法。
_CONTAINMENT_LABEL = {
    "same": "等价",
    "subset": "新元件更宽泛",
    "superset": "新元件更具体",
}


def _tag_covered(tag, sup_tags):
    """sup_tags 里是否有一条能代表 tag → (是否, 分数, 依据)。

    字面相同要先单独判一遍，不走分层：裸前缀是没有维度的（parse_quantity('3p')
    得到的 dim 是 None），而当 sup_tags 里没有类型标签、给不出 hint 时，
    两条一模一样的 `3p` 只能拿到 0.70，被门槛挡在门外——`排母 3p 2.54`
    录重的那一对正是这么漏掉的。

    依据只保留非字面命中（`10k ≈ 10kΩ`、`瓷片 → MLCC`）：字面相同不需要解释。
    """
    nt = normalize_text(tag)
    for s in sup_tags:
        if normalize_text(s) == nt:
            return True, 1.0, ""
    score, reason = _match_tags(nt, sup_tags)
    if score >= _CONTAINMENT_MIN:
        return True, score, reason or ""
    return False, score, ""


def _covered(sub_tags, sup_tags):
    """sub_tags 是否整体被 sup_tags 覆盖 → (是否, 最弱一环的依据)。"""
    if not sub_tags:
        return False, ""
    weakest = (2.0, "")
    for t in sub_tags:
        ok, score, why = _tag_covered(t, sup_tags)
        if not ok:
            return False, ""
        if why and score < weakest[0]:
            weakest = (score, why)
    return True, weakest[1]


def find_containment(components, tags):
    """新标签与库中元件的包含关系 → [(元件, 关系, 依据)]，按序号排序。

    关系是 "same" / "subset" / "superset"，主语一律是**新元件**：subset 表示
    新标签集合是已有元件的子集（信息更少、更宽泛），superset 则相反。

    判定与检索共用同一套分层内核（_match_tags），但两条都刻意不走：句柄层
    （`#7` 在这里是一条字面标签，不该去命中序号；`0805` 这类封装码本身是合法
    十六进制串，会平白去撞 id 前缀）和复合词展开层（一个标签 `贴片电容` 会被
    展开成 `[贴片, C]`，把「元件里有这个标签」偷换成「元件里同时有 贴片 和 C」）。

    tags 为空必须显式挡掉：空集合的 all() 为真，会命中库里一切。
    """
    if not tags:
        return []
    out = []
    for c in components:
        new_in_c, why_fwd = _covered(tags, c.tags)
        c_in_new, why_rev = _covered(c.tags, tags)
        if new_in_c and c_in_new:
            # 这是「等价意义下的相等」，不是逐字相等：归约层会把 10k 与 10kΩ
            # 判成同一条。措辞上不能宣称「完全重复」，否则用户看到两条不一样
            # 的标签被叫做重复，就再也不信这个提示了。
            out.append((c, "same", why_fwd or why_rev))
        elif new_in_c:
            out.append((c, "subset", why_fwd))
        elif c_in_new:
            out.append((c, "superset", why_rev))
    # 按序号排：文件顺序可以被手工编辑 JSON 打乱，序号不会。
    out.sort(key=lambda r: r[0].seq)
    return out


# =============================================================================
# 第 4 层：数据模型与持久化
# =============================================================================


@dataclass
class Stock:
    """存量。两种模式互斥，靠结构而非约定保证。

    存成判别式对象 `{"mode": ..., "level"/"count": ...}` 而不是并列字段
    `{"coarse": 3, "accurate": null}`，是因为前者在结构层面就排除了
    「两个同时有值」和「两个同时为空」两种非法状态。设新值时整个对象被
    替换，天然满足「设置新的就顶掉旧的」，不需要记得去清空另一个字段。
    """

    mode: str  # "coarse" | "accurate"
    level: int | None = None
    count: int | None = None

    def to_dict(self):
        if self.mode == "accurate":
            return {"mode": "accurate", "count": self.count}
        return {"mode": "coarse", "level": self.level}

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            raise AppError(f"stock 应为对象，实际是 {type(d).__name__}", EXIT_DATA)
        mode = d.get("mode")
        if mode == "accurate":
            c = d.get("count")
            if not isinstance(c, int) or isinstance(c, bool) or c < 0:
                raise AppError(f"stock.count 非法：{c!r}（应为非负整数）", EXIT_DATA)
            return cls("accurate", count=c)
        if mode == "coarse":
            lv = d.get("level")
            if not isinstance(lv, int) or isinstance(lv, bool) or not 0 <= lv <= 4:
                raise AppError(f"stock.level 非法：{lv!r}（应为 0-4）", EXIT_DATA)
            return cls("coarse", level=lv)
        raise AppError(f"stock.mode 非法：{mode!r}（应为 coarse 或 accurate）", EXIT_DATA)

    def label(self):
        if self.mode == "accurate":
            return f"精确 {self.count}"
        return f"{COARSE_LABELS[self.level]}({self.level})"

    def is_low(self):
        if self.mode == "accurate":
            return self.count <= 5
        return self.level <= 1


def extract_stock_tags(tags):
    """从标签里摘出存量写法，返回 (剩余标签, Stock 或 None, 问题列表)。

    存量是独立字段、不是元件的属性，所以命中的标签会被**摘掉**而不是留在 tags 里。
    这在标签已经会被规范化（见 canonical_tags）之后依然成立——存量根本不在标签里，
    没有可规范化的写法。

    放在数据层而不是归一化层，是因为它产出 Stock 对象，而归一化层不该反向
    依赖数据层。不抛异常、把问题作为数据返回，与 classify_tags 同风格，
    便于自检直接断言。

    这里是**标签位置**的写法。stock 命令的值位置还认裸数字与相对增减
    （`stock #7 3`、`stock #7 +5`），那两笔刻意留在这里之外——理由见
    parse_stock_value。两处别为了「统一」而合并。

    匹配是整个标签的精确比对，不是子串——所以 `多圈电位器` 不会被误摘，
    `无封装`（封装的哨兵）也不会被当成存量的 `无`。比对大小写不敏感
    （`NONE` / `Level2` 都认），但这只作用于英文与前缀写法，仍然是整标签
    精确比对，不是子串。
    """
    rest, stock, issues = [], None, []
    for tag in tags:
        t = normalize_text(tag)
        cand = None
        err = None

        m = STOCK_TAG_RE.match(t)
        if m:
            kind, num = m.group(1).lower(), int(m.group(2))
            if kind == "qty":
                if num < 0:
                    err = f"存量写法 `{tag}` 的个数不能是负数"
                else:
                    cand = Stock("accurate", count=num)
            elif not 0 <= num <= 4:
                err = f"存量写法 `{tag}` 的等级应该在 0-4 之间"
            else:
                cand = Stock("coarse", level=num)
        else:
            lv = _STOCK_WORDS_CI.get(t.casefold())
            if lv is not None:
                cand = Stock("coarse", level=lv)

        if err is not None:
            issues.append((tag, err))
            continue
        if cand is None:
            rest.append(tag)
            continue
        if stock is not None:
            # 即便两次写的是同一档也报错：判断「是否一致」要额外一套逻辑，
            # 而写两次存量本身就是笔误。
            issues.append((tag, f"存量写了两次（`{tag}`）—— 一条记录只能有一个存量"))
            continue
        stock = cand

    return rest, stock, issues


@dataclass
class Component:
    id: str
    seq: int
    tags: list
    stock: Stock
    note: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self):
        return {
            "id": self.id,
            "seq": self.seq,
            "tags": list(self.tags),
            "stock": self.stock.to_dict(),
            "note": self.note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d, index=0):
        where = f"第 {index + 1} 个元件"
        if not isinstance(d, dict):
            raise AppError(f"{where} 不是对象", EXIT_DATA)
        cid = d.get("id")
        try:
            uuid.UUID(str(cid))
        except (ValueError, TypeError, AttributeError):
            raise AppError(f"{where} 的 id 不是合法 UUID：{cid!r}", EXIT_DATA)
        seq = d.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise AppError(f"{where} 的 seq 非法：{seq!r}", EXIT_DATA)
        tags = d.get("tags")
        if not isinstance(tags, list) or not tags:
            raise AppError(f"{where} 的 tags 应为非空列表", EXIT_DATA)
        for t in tags:
            if not isinstance(t, str) or not t.strip():
                raise AppError(f"{where} 的 tags 里有非法项：{t!r}", EXIT_DATA)
        return cls(
            id=str(cid),
            seq=seq,
            tags=list(tags),
            stock=Stock.from_dict(d.get("stock")),
            note=d.get("note") or "",
            created_at=d.get("created_at") or "",
            updated_at=d.get("updated_at") or "",
        )


class Inventory:
    """库存容器。这是给未来那一层 Python 用的主要接口之一。"""

    def __init__(self, path):
        self.path = Path(path)
        self.components = []
        self.next_seq = 1
        self.created_at = _now()
        self.updated_at = _now()

    def to_dict(self):
        return {
            "schema_version": SCHEMA_VERSION,
            "app": APP_NAME,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "next_seq": self.next_seq,
            "components": [c.to_dict() for c in self.components],
        }

    def find_by_seq(self, seq):
        return [c for c in self.components if c.seq == seq]


def _now():
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def default_data_path():
    """数据文件默认位置：脚本同目录。可用 --file 或环境变量覆盖。"""
    env = os.environ.get("COMPONENT_INVENTORY_FILE")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent / "inventory.json"


def load_inventory(path):
    """从磁盘加载库存。文件不存在时返回空库存。

    读取用 utf-8-sig：无 BOM 时与 utf-8 完全等价，有 BOM 时透明剥离。
    用记事本编辑过 JSON 会留下 BOM，这是 Windows 上最省事的兼容方式。
    """
    p = Path(path)
    if not p.exists():
        if p.with_suffix(p.suffix + ".bak").exists():
            raise AppError(
                f"{p} 不存在，但发现备份 {p}.bak。若确认要用备份恢复，"
                f"手动把它改名为 {p.name} 即可。",
                EXIT_DATA,
            )
        return Inventory(p)

    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise AppError(f"无法读取 {p}：{e}", EXIT_ERROR)

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise AppError(
            f"{p} 不是合法 JSON（第 {e.lineno} 行第 {e.colno} 列）：{e.msg}", EXIT_DATA
        )

    if not isinstance(raw, dict):
        raise AppError(f"{p} 的顶层应为对象", EXIT_DATA)

    version = raw.get("schema_version")
    if version is None:
        raise AppError(
            f"{p} 缺少 schema_version 字段，可能是手工损坏或非本程序生成的文件。",
            EXIT_DATA,
        )
    if not isinstance(version, int) or version > SCHEMA_VERSION:
        raise AppError(
            f"{p} 的 schema_version 是 {version!r}，高于本程序支持的 {SCHEMA_VERSION}，请升级程序。",
            EXIT_DATA,
        )

    comps_raw = raw.get("components")
    if not isinstance(comps_raw, list):
        raise AppError(f"{p} 的 components 应为列表", EXIT_DATA)

    inv = Inventory(p)
    inv.created_at = raw.get("created_at") or inv.created_at
    inv.updated_at = raw.get("updated_at") or inv.updated_at

    seen_seq = {}
    for i, d in enumerate(comps_raw):
        comp = Component.from_dict(d, i)
        if comp.seq in seen_seq:
            raise AppError(
                f"{p} 中 seq={comp.seq} 重复出现（第 {seen_seq[comp.seq] + 1} 个和第 {i + 1} 个元件）",
                EXIT_DATA,
            )
        seen_seq[comp.seq] = i
        inv.components.append(comp)

    ns = raw.get("next_seq")
    if not isinstance(ns, int) or ns < 1:
        ns = (max(seen_seq) + 1) if seen_seq else 1
    # next_seq 只增不减、删除后不回收。否则用户记下的 `#7` 会指向另一个东西。
    inv.next_seq = max(ns, (max(seen_seq) + 1) if seen_seq else 1)
    return inv


# os.replace 在 Windows 上会因杀软实时扫描、文件索引器、编辑器短暂占用而
# 抛 PermissionError（WinError 5）或 OSError（WinError 32）。这在 Linux 上
# 几乎遇不到，所以必须显式处理。
_RETRYABLE_WINERRORS = {5, 32, 33}


def _is_retryable(e):
    if getattr(e, "winerror", None) in _RETRYABLE_WINERRORS:
        return True
    return e.errno in (errno.EACCES, errno.EPERM)


def _replace_with_retry(src, dst, attempts=5):
    last = None
    for i in range(attempts):
        try:
            os.replace(src, dst)  # Win32: MoveFileEx(MOVEFILE_REPLACE_EXISTING)
            return
        except OSError as e:
            if not _is_retryable(e):
                raise
            last = e
            if i < attempts - 1:
                time.sleep(0.05 * (2 ** i))
    raise AppError(
        f"无法写入 {dst}：文件被其他程序占用（{last}）。"
        f"数据未丢失，备份在 {dst}.bak 和临时文件里；请关闭可能占用该文件的程序后重试。",
        EXIT_ERROR,
    )


def save_inventory(inv):
    """原子写入。

    流程：同目录建临时文件 → 写 → fsync → 旧文件改名为 .bak → 原子替换。
    临时文件必须与目标同目录（同卷），否则 os.replace 会退化成复制+删除，
    不再具备原子性。

    绝不就地 open(path, "w")：写到一半断电会留下截断的 JSON。
    也绝不因为重试失败而退回直接覆盖写——那正是要避免的写坏路径。

    **写盘前把每条记录的标签走一遍 canonical_tags。** 于是「规范化」和「迁移」
    是同一件事：写入一次，全库的标签一起收敛到规范写法与槽位顺序，不需要单独的
    迁移命令。这是本函数唯一的副作用，它是有意的，代价是**你改一条记录，全库
    的标签都会被顺带规范化**（幂等，跑第二遍不会再变）——换来的是零迁移成本。
    迁移前的那一版会原封不动落在 .bak 里。

    这里改的是内存里的对象（而不是只在 payload 上改），因为调用方在这之后还会
    用同一批组件算回显与提示，两边必须是同一个样子。

    **每条记录的 updated_at 不动。** 规范化改的是标签的写法和顺序，不是元件的
    内容；把全库 17 条都标成「今天更新」会污染「什么时候加的」这个信号——那是
    created_at 之外的唯一线索。文件级的 updated_at 照旧由本函数更新。
    """
    for comp in inv.components:
        comp.tags = canonical_tags(comp.tags)

    inv.updated_at = _now()
    payload = json.dumps(inv.to_dict(), ensure_ascii=False, indent=2) + "\n"

    path = str(inv.path)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)

    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".inv-", suffix=".tmp")
    try:
        # newline="\n" 防止文本模式把 \n 翻译成 \r\n，否则文件一旦进 git
        # 就会在不同平台编辑时产生整文件 diff。
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())  # Windows 上映射到 FlushFileBuffers
        if os.path.exists(path):
            try:
                os.replace(path, path + ".bak")
            except OSError:
                pass  # 备份失败不应阻断写入
        _replace_with_retry(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# 并发写未加锁：单用户手动使用时可以接受，但如果你以后写脚本批量调用，
# 两次同时写会互相覆盖（后写的赢，丢掉先写的增量）。届时需要加锁文件。


# =============================================================================
# 第 5 层：渲染
# =============================================================================


def display_width(s):
    """字符串在终端里占的列数。

    len("电容") 是 2，但终端里占 4 列。不做这个换算，中文表格必然错位。
    组合字符和 emoji 是边界情况（east_asian_width 对它们返回 'A'），
    本项目可忽略。
    """
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def pad(s, width, align="<"):
    gap = max(0, width - display_width(s))
    if align == ">":
        return " " * gap + s
    return s + " " * gap


# 单个槽位列的宽度上限。和原来的 min(w, 60) 一个意思，只是分槽之后每列天然更短，
# 上限跟着调到「一个语义字段不会超过这么多列」的量级（`共阴绿红` 是 8 列，
# `SOD-123` 是 7 列，16 有一倍余量）。
#
# 它是**软**上限：超了只是不再补空格，不截断。截断会丢字符，与「显示层不改写
# 任何字符」冲突；代价是一条啰嗦的记录仍会把它那行撑开，与改动前一致。
SLOT_CAP = 16


def format_slot_table(rows, indent="", gap="  ", cap=SLOT_CAP):
    """把一排元件排成按槽位分列、彼此对齐的表，返回行文本（不打印）。

    rows 的每一项是 (前导列, 标签, 尾列)：前导列与尾列都是字符串元组，分别放
    `[i]` / `#序号` 和 `存量: …` 这类；中间那段标签由本函数按 SLOT_ORDER 分列。

    空槽的列不出现——结果集里没有任何一行用到的槽整列不占位，所以搜电阻时
    不会平白多出「电气参数」「介质」两段空白。某一行缺的槽由 pad("", w) 补齐，
    列仍然是对齐的。

    这个函数是三个表格渲染点的公共实现（search 的命中表、list 表、包含关系说明
    里的表格）。之前它们各自抄了一遍「算宽度 + 封顶 + 补空格」，抄出三份不同的
    列间距和两种标签排法。
    """
    rows = list(rows)
    if not rows:
        return []

    cells = []
    for _lead, tags, _tail in rows:
        g = slot_groups(tags)
        cells.append([" ".join(g[s]) for s in SLOT_ORDER])

    used = [i for i in range(len(SLOT_ORDER)) if any(c[i] for c in cells)]
    wslot = {i: min(max(display_width(c[i]) for c in cells), cap) for i in used}

    nlead, ntail = len(rows[0][0]), len(rows[0][2])
    wlead = [max(display_width(r[0][j]) for r in rows) for j in range(nlead)]
    wtail = [max(display_width(r[2][j]) for r in rows) for j in range(ntail)]

    lines = []
    for (lead, _tags, tail), c in zip(rows, cells):
        parts = [pad(lead[j], wlead[j]) for j in range(nlead)]
        parts += [pad(c[i], wslot[i]) for i in used]
        parts += [pad(tail[j], wtail[j]) for j in range(ntail)]
        lines.append((indent + gap.join(parts)).rstrip())
    return lines


def format_tag_line(tags):
    """单行显示用的标签文本：按槽位重排，单空格连接，不做列对齐。

    用在只有一条元件的场合（add / stock / remove 的回显）——没有并排的
    行就没有对齐这回事，那里剩下的诉求只有「和别处顺序一致」。
    """
    g = slot_groups(tags)
    return " ".join(" ".join(g[s]) for s in SLOT_ORDER if g[s])


def attach_notes(lines, components, indent="  "):
    """把备注挂到对应元件那一行的下面，返回新的行列表。

    备注是自由文本、长度不定，不能当作一列参与 format_slot_table 的列宽计算，
    否则一条长备注会把整张表撑变形。所以它另起一行、缩进两格——和删除确认里
    那一行同一套写法。

    两个表格渲染点（search 的命中表、list 表）共用这一份排版：备注挂在哪、
    缩进多少，只有这一处说了算。

    也不用行尾的 `←` 尾注放它：那个记号在 search 里已经是「命中原因」，
    一个记号不该有两种含义。
    """
    out = []
    for line, c in zip(lines, components):
        out.append(line)
        if c.note:
            out.append(f"{indent}备注: {c.note}")
    return out


def render_hits(hits, tokens, show_reason=True):
    if not hits:
        print("没有匹配的元件。")
        print("提示：用 -A/--any 放宽为「任一命中」；或检查标签的写法。")
        return

    rows = [
        ((f"[{i}]", f"#{c.seq}"), tuple(c.tags), (f"存量: {c.stock.label()}",))
        for i, c in enumerate((h.component for h in hits), 1)
    ]
    # 命中原因追加在对齐之后，不参与列宽计算——它是行尾的注解，不是一列数据。
    lines = format_slot_table(rows)
    for i, h in enumerate(hits):
        reasons = "  ".join(r for (_, r, _) in h.per_token if r) if show_reason else ""
        if reasons:
            lines[i] += f"  ← {reasons}"
    for line in attach_notes(lines, [h.component for h in hits]):
        print(line)
    print(f"\n共 {len(hits)} 条")


def render_components(components, title=None):
    if not components:
        print("库存为空。")
        return
    rows = [
        ((f"#{c.seq}",), tuple(c.tags), (f"存量: {c.stock.label()}",))
        for c in components
    ]
    for line in attach_notes(format_slot_table(rows), components):
        print(line)
    print(f"\n共 {len(components)} 条")


def render_containment(relations, tags):
    """「新元件与已有元件存在包含关系」的说明块，返回多行文本（不打印）。

    命令行把它当 AppError 的消息，交互模式逐行 sub_print 之后再追问——同一条
    规则不允许有两种说法，所以整块渲染只有这一份。这里刻意不加缩进参数：
    命令行要的是「标题顶格、表格缩进两格」，交互模式在它之上再整体 +2，
    两种排版都从这一份文本派生。
    """
    lines = [f"新元件 {format_tag_line(tags)} 与库中 {len(relations)} 个元件存在包含关系："]
    rows = [
        ((f"[{i}]", f"#{c.seq}"), tuple(c.tags),
         (_CONTAINMENT_LABEL[kind], f"存量: {c.stock.label()}"))
        for i, (c, kind, _why) in enumerate(relations, 1)
    ]
    lines.extend(format_slot_table(rows, indent="  "))
    # 依据行只在有非字面命中时出现。字面相同的重复录入，标签本身就是解释。
    for c, _kind, why in relations:
        if why:
            lines.append(f"  #{c.seq}：{why}")
    return "\n".join(lines)


def component_to_json(c, score=None, per_token=None):
    # note 是记录字段，和 tags 同级，所以无条件带上（没有备注时是空串）；score 与
    # matches 是查询期的注解，才该按需出现。JSON 消费者不该去猜键在不在。
    #
    # 创建与更新时间刻意不在这里：它们是工具自己写的时间戳，不是用户录入的内容。
    d = {"seq": c.seq, "id": c.id, "tags": list(c.tags),
         "stock": c.stock.to_dict(), "note": c.note}
    if score is not None:
        d["score"] = round(score, 4)
    if per_token is not None:
        d["matches"] = [
            {"token": q, "score": round(s, 4), "reason": r}
            for (s, r, q) in per_token
        ]
    return d


# =============================================================================
# 第 6 层：CLI
# =============================================================================

SUBCOMMANDS = ("search", "add", "stock", "list", "remove")

# 顶层 flag 语法糖 → 子命令。这样 `--search 51R` 和 `search 51R` 走的是
# 同一份实现，不存在行为分叉。
ARG_ALIASES = {
    "--search": "search", "-s": "search",
    "--add": "add", "-a": "add",
    "--list": "list", "-l": "list",
    "--stock": "stock", "--set-stock": "stock",
    "--remove": "remove", "--rm": "remove",
}

_GLOBAL_OPTS_WITH_VALUE = {"--file", "-f"}


def normalize_argv(argv):
    """把 `--search X` 重写成 `search X`，让两种写法共用同一份实现。

    只从左扫到第一个非选项 token 为止。这条限制是必需的：否则
    `add C 0805 --search` 这种标签里恰好等于 `--search` 的词会被误重写。
    """
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in SUBCOMMANDS:
            return list(argv)
        if tok in ARG_ALIASES:
            return list(argv[:i]) + [ARG_ALIASES[tok]] + list(argv[i + 1:])
        if tok in _GLOBAL_OPTS_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    return list(argv)


def _add_stock_options(parser, required=False):
    g = parser.add_mutually_exclusive_group(required=required)
    g.add_argument(
        "--level", type=int, choices=range(0, 5),
        help="粗略存量：0 无 / 1 极少 / 2 少 / 3 多 / 4 极多",
    )
    g.add_argument("--qty", type=int, metavar="N", help="精确存量个数")
    return g


def build_parser():
    # 子命令共用的 --file 模板。default=SUPPRESS 是关键：它让子命令在用户
    # 没显式给 --file 时**不写入** namespace，从而不会覆盖顶层已经解析出的值。
    # 这样 `--file x.json search 51R` 和 `search 51R --file x.json` 都能用。
    # 顶层不能用这个模板（顶层需要 default=None），否则 option 冲突。
    sub_common = argparse.ArgumentParser(add_help=False)
    sub_common.add_argument("--file", "-f", default=argparse.SUPPRESS, metavar="PATH",
                            help="数据文件路径")

    p = argparse.ArgumentParser(
        prog="inventory.py",
        description="电子元件库存管理。支持模糊识别：0.1uF 能匹配 100nF，"
                    "51R 能匹配 51Ω，电容 能匹配 C。",
        epilog="也可以用顶层写法，例如 `inventory.py --search 51R` 等价于 `inventory.py search 51R`。",
    )
    p.add_argument("--file", "-f", default=None, metavar="PATH",
                   help="数据文件路径（默认脚本同目录的 inventory.json）")
    p.add_argument("--selftest", action="store_true", help="运行内置自检并退出")
    sub = p.add_subparsers(dest="command")

    ps = sub.add_parser("search", parents=[sub_common], help="检索元件")
    ps.add_argument("query", nargs="+", help="查询词，多个词之间是「全部命中」")
    ps.add_argument("-A", "--any", action="store_true", help="放宽为「任一命中」")
    ps.add_argument("-n", "--limit", type=int, metavar="N", help="最多显示 N 条")
    ps.add_argument("--json", action="store_true", help="输出 JSON")

    pa = sub.add_parser("add", parents=[sub_common], help="添加元件")
    pa.add_argument("tags", nargs="+", help="标签，例如 C 0805 贴片 100nF 50V")
    _add_stock_options(pa)
    pa.add_argument("--note", default="", help="备注")
    pa.add_argument("--force", action="store_true",
                    help="与库中元件存在包含关系时仍然添加，不报错退出")

    pst = sub.add_parser("stock", parents=[sub_common], help="更改存量")
    # target 与 value 都是单值：目标只认 `#编号`，值是一个 token。
    #
    # 值里的 `-5` / `-` 能落到位置参数上，靠的是 argparse 的一条隐含前提——
    # 本 parser 没有任何短选项长得像负数，`_negative_number_matcher` 才会把
    # `-5` 让给位置参数（`-` 更是因为长度为 1 直接被放过）。将来若给这里加
    # `-1` 这类短选项，`stock #7 -5` 会被重新解释成选项，相对增减当场失效。
    pst.add_argument("target", help="元件编号，只写 #编号，如 #7")
    pst.add_argument("value", nargs="?",
                     help="新存量：档位词（多 / plenty）/ level3 / qty23 / 23 / +5 / -2 / 加5 / 用5 / add5")
    _add_stock_options(pst)

    pl = sub.add_parser("list", parents=[sub_common], help="列出全部元件")
    pl.add_argument("-n", "--limit", type=int, metavar="N", help="最多显示 N 条")
    pl.add_argument("--low", action="store_true", help="只列出存量偏低的")
    pl.add_argument("--json", action="store_true", help="输出 JSON")

    pr = sub.add_parser("remove", parents=[sub_common], help="删除元件")
    pr.add_argument("target", help="元件编号，只写 #编号，如 #7")

    return p


def make_stock(args, default_coarse=False):
    """从 --level / --qty 造一个 Stock。选项形态的唯一入口。

    范围校验收在这里而不是各调用点：argparse 的 choices 只管命令行，REPL 那边
    原来自带一份 check_stock_opts，两处各挡各的——绕过任何一处就能把非法的
    level 写进盘，要等下一次 Stock.from_dict 才报错。收拢之后 CLI 与 REPL 共用
    同一份判定和同一份措辞。

    两个都给时仍然由 qty 静默胜出：命令行靠 argparse 的互斥组挡住，REPL 的
    do_add 自带一段检查，两处都还在。这是既有行为，本次不动 add 那一路。
    """
    qty = getattr(args, "qty", None)
    level = getattr(args, "level", None)
    if qty is not None:
        if qty < 0:
            raise AppError("--qty 不能为负数", EXIT_USAGE)
        return Stock("accurate", count=qty)
    if level is not None:
        if not 0 <= level <= 4:
            raise AppError(f"--level 应该在 0-4 之间，实际是 {level}", EXIT_USAGE)
        return Stock("coarse", level=level)
    if default_coarse:
        return Stock("coarse", level=0)
    raise AppError("必须指定 --level 或 --qty", EXIT_USAGE)


# 值位置独有的相对增减写法：`+N` / `-N` / `加N` / `用N` / `add5`，数字省略就是
# 1，中文动词后面可以多一个「掉」（`用掉5` ≡ `用5`）。
#
# 只有这里认。标签路径（extract_stock_tags）刻意不认裸数字与增减号，因为标签
# 是按空格分词的，`0805`、`1000` 这类封装码满屏都是，认了就会把它们吃成个数；
# 值位置只有一个 token，没有这个风险。
#
# 多 / 少 / low 刻意不进这张表：它们已经是存量词（粗略 3 / 2 / 2）。同一个词在
# 同一个位置有两种解释是最坏的一种歧义，比少一个写法糟得多——`多5` 落到「看不懂」
# 的报错里，报错文案会点明它们是档位词。
#
# 到 / 满 / 空 也不收：语义双关，「到5」既可能是「到货 5 个」也可能是「达到 5」，
# 后者是设值不是增减。英文的 up / down / in / out 同理太短，方向靠猜。
_DELTA_RE = re.compile(
    r"^(?:(?P<sign>[+-])"
    r"|(?P<up>加|增|添|补|进|入|add|plus|inc|gain)"
    r"|(?P<down>减|用|耗|去|出|丢|损|坏|sub|minus|dec|use|take|lost))"
    r"掉?(?P<num>\d*)$",
    re.IGNORECASE,
)


def parse_stock_value(token):
    """解析 stock 值位置上的一个 token → ("set", Stock) 或 ("delta", 增量)。

    绝对写法整段委托给 extract_stock_tags——档位词与 levelN / qtyN 只有那一份
    实现，这里不再抄一遍。裸数字与增减词是值位置独有、标签路径刻意不认的写法
    （理由见 _DELTA_RE 上面），所以只在本函数里补。

    不认就直接报错，不返回 None：值位置没有「这也许是个别的什么」的余地。
    """
    raw = normalize_text(token)
    if not raw:
        raise AppError("stock 的值不能是空的。", EXIT_USAGE)

    m = _DELTA_RE.match(raw)
    if m:
        num = int(m.group("num")) if m.group("num") else 1
        if m.group("sign"):
            return "delta", -num if m.group("sign") == "-" else num
        return "delta", -num if m.group("down") else num

    if raw.isdigit():
        return "set", Stock("accurate", count=int(raw))

    _rest, stock, issues = extract_stock_tags([raw])
    if issues:
        raise AppError(issues[0][1], EXIT_USAGE)
    if stock is not None:
        return "set", stock

    raise AppError(
        f"看不懂存量写法 `{token}`。可以写：\n"
        f"  档位词  无 / 极少 / 少 / 多 / 极多（英文 none/few/some/many/lots 等，整套都认）\n"
        f"  带前缀  level0-4（档位）/ qty23（精确 23 个）\n"
        f"  裸数字  23 —— 精确 23 个\n"
        f"  进 / 出  +5 / -5 / 加5 / 加掉5 / 用5 / add5 / sub5，省略数字就是 1\n"
        f"（多 / 少 / low 是档位词，不是增减词）",
        EXIT_USAGE,
    )


def apply_stock_value(current, token, seq):
    """把值 token 作用到当前存量上，返回新的 Stock。seq 只用来写报错里的例子。

    相对增减只在当前是「精确」时有意义——粗略档位没有可比的数量基准，替用户
    猜一个基准不如要求他先写成精确数。新加的元件默认是粗略的「无(0)」，所以
    刚 add 完就想 `+1` 的人一定会撞上这条，报错必须把下一步写清楚。
    """
    kind, payload = parse_stock_value(token)
    if kind == "set":
        return payload

    if current.mode != "accurate":
        raise AppError(
            f"当前存量是「{current.label()}」，粗略档位没法做相对增减。\n"
            f"先写成精确数（例如 `stock #{seq} 20`）再用 `+N` / `-N`，"
            f"或者直接用档位词覆盖（例如 `stock #{seq} 少`）。",
            EXIT_USAGE,
        )
    new = current.count + payload
    if new < 0:
        raise AppError(
            f"`{token}` 会让存量从 {current.count} 变成 {new}，不能为负。\n"
            f"要清零就写 `stock #{seq} 0`。",
            EXIT_USAGE,
        )
    return Stock("accurate", count=new)


# 改库存与删元件的目标只能是一个 `#编号`。
#
# 曾经这里是三层解析（编号 → id 前缀 → 完整检索）。收窄到编号，是因为这两个
# 命令都得先定位到**一条**记录再动手：三层解析会给出多个候选，于是命令行报了错
# 还要再列一张候选表、交互模式还要再让用户挑一次，同一条挑选规则写两遍。
# 编号是永久的、每个命令的输出里都有；要按标签找元件，第一步本来就该是 search。
#
# 不认裸数字：编号在本项目里始终写作 `#7`，多一种写法就多一条要解释的规则。
#
# id 前缀在这一层不认，但在 search 里认——match_token 的句柄层另有一份独立实现。
# 找要召回，改要精确。
_HANDLE_RE = re.compile(r"^#(\d+)$")


def parse_handle(text):
    """把目标文本解析成序号。只认 `#编号`，其余一律报错。"""
    m = _HANDLE_RE.match(normalize_text(text))
    if not m:
        raise AppError(
            f"目标只能写成 #编号（如 #7），实际是 {text!r}。\n"
            f"编号是永久的：先用 `search 关键词` 或 `list` 查到它。",
            EXIT_USAGE,
        )
    return int(m.group(1))


def _require_handle(inv, text, verb):
    """定位到 `#编号` 指的那一个元件，否则抛错。"""
    seq = parse_handle(text)
    found = inv.find_by_seq(seq)
    if not found:
        raise AppError(
            f"没有 #{seq} 这个编号，没法{verb}。用 `list` 看全部元件的编号。",
            EXIT_NOTFOUND,
        )
    return found[0]


def cmd_search(args, path):
    inv = load_inventory(path)
    tokens = []
    for q in args.query:
        tokens.extend(tokenize_query(q))
    if not tokens:
        raise AppError("查询为空", EXIT_USAGE)

    hits = search_components(inv.components, tokens, any_mode=args.any, limit=args.limit)

    if args.json:
        payload = {
            "count": len(hits),
            "query": tokens,
            "results": [component_to_json(h.component, h.score, h.per_token) for h in hits],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        render_hits(hits, tokens)
    return EXIT_OK if hits else EXIT_NOTFOUND


def cmd_add(args, path, extra_package=None, confirm=None):
    """添加元件。

    extra_package 只由交互模式传入（见 classify_tags）：命令行入口不传，
    于是封装只能靠查表识别，认不出就报错。

    confirm 是第二个只由交互模式传入的通道：命中包含关系时由它决定加不加，
    返回 True 继续、False 取消。命令行不传（None），改为直接报错退出。

    检测放在本函数而不是 WarehouseKeeper，一是两个入口只能有一份判定和一份
    措辞，二是包含判定必须看到 classify_tags 定稿后的标签——`add 0805 100nF
    50V` 补出来的那个 C 要是赶不上，跟库里任何一条电容都比不出关系来。
    """
    inv = load_inventory(path)
    tags = []
    for t in args.tags:
        nt = normalize_text(t)
        if nt:
            tags.append(nt)
    if not tags:
        raise AppError("至少需要一个标签", EXIT_USAGE)

    # 先把存量标签摘出来。必须排在三类标签校验**之前**——存量不是元件的属性，
    # 不该参与「参数」的判定，否则 `add C 0805 qty23` 会因为那个 qty23 而被
    # 误判成「有参数」。
    tags, tag_stock, stock_issues = extract_stock_tags(tags)
    if stock_issues:
        raise AppError("\n".join(msg for _, msg in stock_issues), EXIT_USAGE)
    opt_stock = getattr(args, "level", None) is not None or getattr(args, "qty", None) is not None
    if tag_stock is not None and opt_stock:
        raise AppError(
            "存量给了两次：标签里写了，同时又给了 --level / --qty。二选一。", EXIT_USAGE)

    # 三类必需标签的校验与类型补全。放在这里（而不是 load/save 层）是为了让
    # 命令行和 WarehouseKeeper 的 do_add 自动同享——后者复用本函数。
    plan = classify_tags(tags, extra_package=extra_package)
    if plan.issues:
        # 一次报出全部问题，不修一个报一个：命令行下每次重试都要重启进程。
        raise AppError(render_issues(plan, tags), EXIT_USAGE)
    if plan.added_type:
        # 补在索引 0，贴合 `C 0805 贴片 100nF 50V` 的书写习惯。顺序的最终裁决权
        # 在 canonical_tags（存盘与显示都过它），这里只是让内存里的列表当场就能
        # 读——本函数后面还有分支要用 tags 算提示，不必先跑一遍规范化。
        tags.insert(0, plan.added_type)
    if plan.added_medium:
        # 介质紧挨着类型放，读起来是 `C MLCC 0805 100nF`。理由同上。
        pos = next((i for i, t in enumerate(tags) if canon_type(t) is not None), 0)
        tags.insert(pos + 1, plan.added_medium)
    if plan.absorbed:
        # 全项目唯一一处会删掉用户输入的地方。它只在标签与封装**互相印证**时
        # 发生（判定规则见 classify_tags），所以删掉的是重复信息本身，不是信息。
        tags = [t for t in tags if t not in plan.absorbed]

    stock = tag_stock if tag_stock is not None else make_stock(args, default_coarse=True)

    # 包含检查必须排在存盘之前：命令行报错、交互模式取消，两种拒绝都不能落盘。
    # 用的也是定稿后的 tags——类型补过、冗余删过，才对得上真正要写进去的东西。
    # force 只在这一个地方读，两个入口不必各自分叉。
    relations = find_containment(inv.components, tags)
    if relations and not getattr(args, "force", False):
        if confirm is None:
            raise AppError(
                render_containment(relations, tags)
                + "\n\n（未写入。确认不是重复录入的话，加 --force 再执行一次。）",
                EXIT_USAGE)
        if not confirm(relations, tags):
            return EXIT_OK

    comp = Component(
        id=str(uuid.uuid4()),
        seq=inv.next_seq,
        tags=tags,
        stock=stock,
        note=args.note or "",
        created_at=_now(),
        updated_at=_now(),
    )
    inv.next_seq += 1
    inv.components.append(comp)
    save_inventory(inv)

    # 回显用的是 comp.tags，不是上面那个局部 tags：save_inventory 刚把存盘的标签
    # 规范化过（写法规整 + 槽位重排），结果写回了 comp.tags，而局部 tags 还停在
    # 规范化之前。回显必须和用户打开 JSON 看到的那一份一致。
    print(f"已添加 #{comp.seq}  {format_tag_line(comp.tags)}   存量: {stock.label()}")
    # 自动补全是在改用户的数据，比匹配更需要解释自己。项目里反复强调的
    # 「模糊匹配必须能解释自己」在这里同样适用，而且这里的要求更高。
    if plan.added_type:
        ev = "、".join(f"`{t}`" for t in plan.type_evidence)
        print(f"  （自动补全了类型标签 {plan.added_type}，依据：{ev}，{_SOURCE_LABEL.get(plan.type_source, '')}）")
    if plan.added_medium:
        print(f"  （自动补全了介质标签 {plan.added_medium}，依据：片式尺寸码的电容就是陶瓷的）")
    if plan.absorbed:
        merged = "、".join(f"`{t}`" for t in plan.absorbed)
        print(f"  （{merged} 已并入封装 {plan.package}：这个封装本身就说明了安装方式）")
    if tag_stock is None and not opt_stock:
        print("提示：未指定存量，已设为 0（无）。用 --level 0-4 / --qty N，"
              "或在标签里写 多 / 很少 / qty23（英文 none / few / some / many / lots 同样认）。")

    # --force 的意思是「我看过了，照加」，但命令行那条路上用户可能一上来就带着
    # 它，从没见过相关元件是谁。留一行痕迹。交互模式不重复打印：它刚把同一张
    # 表摊开问过，再说一遍是噪音。
    if relations and confirm is None:
        seqs = "、".join(f"#{c.seq}" for c, _k, _w in relations)
        print(f"注意：与库中 {seqs} 存在包含关系（已按 --force 写入）。")
    return EXIT_OK


def cmd_stock(args, path):
    inv = load_inventory(path)
    comp = _require_handle(inv, args.target, "修改")

    # 三个来源只能给一个，缺一个都不行。逐条查而不是用互斥组，是因为值这条路
    # 走位置参数，argparse 那个 group 管不到它。
    value = getattr(args, "value", None)
    level = getattr(args, "level", None)
    qty = getattr(args, "qty", None)
    if sum(x is not None for x in (value, level, qty)) > 1:
        raise AppError("新存量给多了：值、--level、--qty 只能给一个。", EXIT_USAGE)
    if value is None and level is None and qty is None:
        raise AppError(
            "stock 需要给出新存量，例如：stock #7 plenty / stock #7 23 / "
            "stock #7 +1，或 stock #7 --level 2 / stock #7 --qty 100。",
            EXIT_USAGE,
        )

    old = comp.stock.label()
    # 选项形态复用 make_stock：--qty 负数、--level 越界只有那一份措辞。
    comp.stock = (apply_stock_value(comp.stock, value, comp.seq) if value is not None
                  else make_stock(args))
    comp.updated_at = _now()
    save_inventory(inv)
    print(f"#{comp.seq}  {format_tag_line(comp.tags)}   存量: {old} → {comp.stock.label()}")
    return EXIT_OK


def cmd_list(args, path):
    inv = load_inventory(path)
    comps = sorted(inv.components, key=lambda c: c.seq)
    if args.low:
        comps = [c for c in comps if c.stock.is_low()]
    if args.limit is not None:
        comps = comps[: args.limit]

    if args.json:
        payload = {"count": len(comps), "results": [component_to_json(c) for c in comps]}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        render_components(comps)
    return EXIT_OK


def cmd_remove(args, path, confirm=None):
    """删除元件。

    confirm 只由交互模式传入，与 cmd_add 的 confirm 同一个套路：命令行不传，
    删之前不再问；交互模式传入一个回调，由它把详情摊开再确认。删除本身与
    「next_seq 不回收」的理由因此只有一份，不再被 REPL 抄第二遍。
    """
    inv = load_inventory(path)
    comp = _require_handle(inv, args.target, "删除")
    if confirm is not None and not confirm(comp, path):
        return EXIT_OK
    inv.components.remove(comp)
    # next_seq 不回收：删除后 #7 不会再被分配给新元件，
    # 否则用户记下的 #7 会悄悄指向另一个东西。
    save_inventory(inv)
    print(f"已删除 #{comp.seq}  {format_tag_line(comp.tags)}")
    return EXIT_OK


# =============================================================================
# 自检
# =============================================================================


def run_selftest():
    """归一化引擎的回归测试。

    这不是锦上添花。parse_quantity 的规则密度极高且全是边界情况，
    而它一旦出错是**静默错**——不报错，只是匹配不到或匹配错，
    用户会逐渐失去对检索结果的信任。没有这批断言，规则会很快腐化。
    """
    failures = []
    checks = [0]

    def ok(cond, label):
        checks[0] += 1
        if not cond:
            failures.append(label)

    def eq(actual, expected, label):
        checks[0] += 1
        if actual != expected:
            failures.append(f"{label}：期望 {expected!r}，实际 {actual!r}")

    def close(a, b, label, tol=1e-9):
        checks[0] += 1
        if a is None:
            failures.append(f"{label}：解析结果为空")
        elif not math.isclose(a, b, rel_tol=tol):
            failures.append(f"{label}：期望 {b!r}，实际 {a!r}")

    def same(t1, t2, hint=None, label=None):
        checks[0] += 1
        a = parse_quantity(t1, hint=hint)
        b = parse_quantity(t2, hint=hint)
        if not quantities_equal(a, b):
            failures.append(f"{label or f'{t1} ≡ {t2}'}：{a} vs {b}")

    def val(token, hint=None):
        q = parse_quantity(token, hint=hint)
        return None if q is None else q.value

    def dim(token, hint=None):
        q = parse_quantity(token, hint=hint)
        return None if q is None else q.dim

    # --- 别名表健康度 ---
    ok(not _ALIAS_CONFLICTS, f"类型别名表存在冲突：{_ALIAS_CONFLICTS}")

    # --- 电容等价：用户给的核心需求 ---
    for t in ("100nF", "0.1uF", "1e-7F", "100NF", "100nf", "100n"):
        same("0.1uF", t)

    # --- 电阻等价 ---
    for t in ("51Ω", "51ohm", "51ohms", "51欧", "51欧姆", "51R"):
        same("51R", t)

    # --- 裸数字靠元件上下文推断 ---
    same("51", "51Ω", hint="resistance", label="裸数字 51 在电阻元件上应为 51Ω")

    # --- 中缀记号 ---
    close(val("4R7"), 4.7, "4R7 = 4.7Ω")
    close(val("R47"), 0.47, "R47 = 0.47Ω")
    close(val("0R05"), 0.05, "0R05 = 0.05Ω")
    close(val("1k2"), 1200.0, "1k2 = 1200")
    close(val("2M2"), 2.2e6, "2M2 = 2.2M")
    close(val("4u7"), 4.7e-6, "4u7 = 4.7µ")
    close(val("1k2Ω"), 1200.0, "1k2Ω = 1200Ω")

    # --- 大小写敏感：m 与 M 语义相反，是本项目最危险的坑 ---
    ok(not quantities_equal(parse_quantity("1M"), parse_quantity("1m")),
       "1M（兆）不应等于 1m（毫）")
    close(val("1M"), 1e6, "1M = 1e6")
    close(val("1m"), 1e-3, "1m = 1e-3")

    # --- Hz 必须排在 h（亨利）前面，否则 1kHz 会被拆成 k + H + z ---
    eq(dim("1kHz"), "frequency", "1kHz 应为频率而非电感")
    close(val("1kHz"), 1000.0, "1kHz = 1000 Hz")

    # --- 封装代码绝不能被当成物理量 ---
    for t in ("0805", "0603", "0402", "1206", "100", "51"):
        ok(parse_quantity(t) is None, f"{t} 在无上下文时不应被解析为物理量")
    ok(parse_quantity("0805", hint="resistance") is None,
       "0805 即便在电阻元件上也不应被解析（前导零是封装代码的特征）")

    # --- 维度闸门 ---
    ok(not quantities_equal(parse_quantity("100nF"), parse_quantity("50V")),
       "100nF 与 50V 维度不同，不应相等")
    ok(not quantities_equal(parse_quantity("100nF"), parse_quantity("100")),
       "100nF 与裸 100 不应相等")

    # --- 零值路径 ---
    same("0R", "0Ω", label="0Ω 应与自身相等")

    # --- 非物理量文本 ---
    for t in ("贴片", "金属膜", "-40~85C", "X7R", ""):
        ok(parse_quantity(t) is None, f"{t!r} 不应被解析为物理量")

    # --- Unicode 归一化 ---
    same("100nF", "100\xb5F".replace("\xb5", "n"), label="µ 与 u 应等价")
    same("1uF", "1µF", label="MICRO SIGN 应归一到 u")
    same("1uF", "1μF", label="GREEK MU 应归一到 u")
    same("51Ω", "51Ω", label="OHM SIGN 与 OMEGA 应等价")
    same("1uF", "１uF", label="全角数字应经 NFKC 归一")

    # --- 类型归约 ---
    eq(canon_type("电容"), "C", "电容 → C")
    eq(canon_type("电容器"), "C", "电容器 → C")
    eq(canon_type("cap"), "C", "cap → C")
    eq(canon_type("CAPACITOR"), "C", "大小写无关")
    eq(canon_type("C"), "C", "规范码自身")
    eq(canon_type("电阻"), "R", "电阻 → R")
    eq(canon_type("贴片电容"), "C", "复合词 贴片电容 → C")
    eq(canon_type("发光二极管"), "LED", "复合词 发光二极管 → LED")
    eq(canon_type("0805"), None, "封装代码不是类型")
    eq(canon_type("金属膜电阻"), "R", "子串回退：金属膜电阻 → R")
    eq(canon_type("x7r"), None, "X7R 不应因含字母 x 被判成跳线")
    eq(canon_type("C0G"), None, "C0G 不应因含字母 c 被判成电容")

    # --- 切分残余必须退回原 token ---
    # `capacitor` 会被最大匹配切出 cap + c + r 三块碎片，拼出的语义
    # 与原文毫不相干；若不退回，按「各部分都要命中」处理就永远搜不到。
    eq(expand_compound("capacitor"), ("capacitor",), "有残余的切分应退回原 token")
    eq(expand_compound("贴片电容"), ("贴片", "C"), "显式表展开")
    eq(expand_compound("钽电容"), ("钽", "C"), "无残余的最大匹配展开")

    # --- 子串边界检查 ---
    ok(_substring_score("100nF", "1100nF") == 0.0, "100nF 不应子串命中 1100nF")
    ok(_substring_score("100nF", "100nF/50V") > 0, "100nF 应子串命中 100nF/50V")
    ok(_substring_score("电容", "贴片电容") > 0, "中文子串应命中（CJK 不算词字符）")

    # --- 端到端匹配 ---
    comp = Component(
        id="9f1c4e2a-7b3d-4f88-a1c2-0e5d6b7a8c90",
        seq=1,
        tags=["C", "0805", "贴片", "100nF", "50V"],
        stock=Stock("coarse", level=3),
    )
    for q in ("0.1uF", "100nF", "1e-7", "电容", "C", "capacitor", "#1"):
        s, _ = match_token(q, comp)
        ok(s > 0, f"查询 {q!r} 应命中电容元件")
    for q in ("51R", "电阻", "100pF", "#2"):
        s, _ = match_token(q, comp)
        ok(s == 0, f"查询 {q!r} 不应命中电容元件")

    # 物理量数值不等价时不得降级成字符串子串匹配：
    # `0.1uF` 与 `1uF` 差十倍，但 `1uF` 确实是 `0.1uF` 的子串。
    near = Component(
        id="11111111-2222-3333-4444-555555555555",
        seq=2,
        tags=["C", "0603", "1uF"],
        stock=Stock("coarse", level=1),
    )
    s, why = match_token("0.1uF", near)
    ok(s == 0, f"0.1uF 不应命中 1uF（数值差十倍），实际 {s} / {why}")
    s, _ = match_token("1uF", near)
    ok(s > 0, "1uF 应命中 1uF")

    # --- 多词 AND ---
    hits = search_components([comp], ["电容", "100nF"])
    eq(len(hits), 1, "AND 检索：电容 + 100nF 应命中 1 条")
    hits = search_components([comp], ["电容", "51R"])
    eq(len(hits), 0, "AND 检索：电容 + 51R 应命中 0 条")

    # --- 存量模型 ---
    eq(Stock.from_dict({"mode": "coarse", "level": 3}).label(), "多(3)", "粗略存量显示")
    eq(Stock.from_dict({"mode": "accurate", "count": 42}).label(), "精确 42", "精确存量显示")
    ok(Stock("coarse", level=3).to_dict() == {"mode": "coarse", "level": 3}, "存量序列化")
    for bad in ({"mode": "coarse", "level": 9}, {"mode": "accurate", "count": -1}, {"mode": "x"}):
        try:
            Stock.from_dict(bad)
            failures.append(f"非法存量 {bad} 应被拒绝")
        except AppError:
            pass
        checks[0] += 1

    # --- 显示宽度 ---
    eq(display_width("电容"), 4, "中文按 2 列宽计算")
    eq(display_width("0805"), 4, "ASCII 按 1 列宽计算")

    # --- 单位写法规范化 ---
    #
    # 这一组钉的是「什么会被改写、什么绝不改写」。改写只发生在
    # 「数字 + ASCII 单位简写」这一种形态上——它是全项目唯一同一个量有好几种
    # 写法的地方。用户的词汇（类型词、介质词、描述词、封装名）不是拼写错误的
    # 来源，一个字符都不碰。
    for raw, want in (("51r", "51Ω"), ("51R", "51Ω"), ("51Ω", "51Ω"),
                      ("220R", "220Ω"), ("150R", "150Ω"), ("10kR", "10k"),
                      ("1MR", "1M"), ("16v", "16V"), ("50v", "50V"),
                      ("25v", "25V"), ("0.25w", "0.25W"), ("1ohm", "1Ω"),
                      ("35V", "35V"), ("2.2uF", "2.2uF")):
        eq(canon_unit(raw), want, f"单位写法规范化 {raw!r}")
    for raw in ("4k7", "1k2", "2R2", "0R05", "1e-7F", "0.1uF", "100n", "1k", "10k",
                "0805", "0402", "2.54", "5x11", "5mm", "1%", "1N4148", "1000",
                "X7R", "16MHz", "1kHz", "10uH", "1M", "1m", "1欧", "1伏", "1瓦",
                "直插", "贴片", "led", "排母", "MLCC", "瓷片", "铝电解", "共阴绿红",
                "黄", "Case A", "无封装", "NO PACKAGE"):
        eq(canon_unit(raw), raw, f"不该改写的标签 {raw!r}")
    eq(canon_unit("1mR"), "1mΩ", "m 是歧义前缀：1mR 保留 Ω，裸写 1m 分不清兆毫")
    eq(canon_unit("10KR"), "10K", "前缀照抄不换大小写，只省掉冗余的 Ω")

    # --- 显示槽位与规范顺序 ---
    #
    # `1k` / `4k7` / `3p` 这一组是回归防线：parse_quantity 对裸前缀与中缀写法
    # 刻意返回 dim=None（维度歧义留到有上下文时再消解），谁要是按「dim 属于某几个
    # 维度」来判主值，它们就会掉进「其它」列，`R 10k 直插` 会排成 `R 直插 10k`。
    for tags, want in (
        (["C", "0805", "100nF", "50V", "MLCC"], ["C", "100nF", "50V", "MLCC", "0805"]),
        (["R", "10k", "直插"], ["R", "10k", "直插"]),
        (["R", "直插", "10k"], ["R", "10k", "直插"]),
        (["R", "0.25w", "10kR", "直插"], ["R", "10k", "0.25W", "直插"]),
        (["R", "0603", "51r", "1%"], ["R", "51Ω", "1%", "0603"]),
        (["3p", "排母", "2.54"], ["排母", "3p", "2.54"]),
        (["D", "SOD-123", "1N4148"], ["D", "1N4148", "SOD-123"]),
        (["共阴绿红", "led", "直插"], ["led", "直插", "共阴绿红"]),
        (["C", "100nF", "无封装"], ["C", "100nF", "无封装"]),
        # 同一槽里的多个标签保持原相对顺序（`R` 和 `色环` 都在类型槽，
        # 它们在渲染时合成一个单元格，但 canonical_tags 只重排、不合并 token）。
        (["R", "色环", "4k7", "直插"], ["R", "色环", "4k7", "直插"]),
        # 槽内**稳定**排序：`直插` 和 `0805` 同在封装槽，输入里谁在前谁就留在前
        # （实际录入时 `0805` 会把冗余的 `直插` 吸收掉，这里只钉排序规则本身）。
        (["直插", "MLCC", "0805", "50V", "100nF", "C"],
         ["C", "100nF", "50V", "MLCC", "直插", "0805"]),
    ):
        eq(canonical_tags(tags), want, f"规范顺序 {tags}")
        eq(canonical_tags(want), want, f"规范化必须幂等 {tags}")

    # 分槽的判据只有 classify_tags 一处，渲染与重排都读它的 slot_of。
    eq(len(classify_tags(["C", "0805", "100nF"]).slot_of), 3, "slot_of 与 tags 逐位对齐")
    eq(set(classify_tags(["C", "0805", "100nF"]).slot_of), {"type", "package", "main"},
       "slot_of 只产出 SLOT_ORDER 里的名字")

    # --- 单行回显与表格对齐 ---
    eq(format_tag_line(["0805", "100nF", "C"]), "C 100nF 0805", "单行回显按槽位重排")
    eq(format_slot_table([]), [], "空表返回空列表")
    _rows = [
        (("#1",), ("C", "100nF", "50V", "0805"), ("存量: 多(3)",)),
        (("#2",), ("C", "1uF", "16V", "5x11"), ("存量: 多(3)",)),
    ]
    eq(format_slot_table(_rows),
       ["#1  C  100nF  50V  0805  存量: 多(3)",
        "#2  C  1uF    16V  5x11  存量: 多(3)"],
       "槽位列各自补空格对齐")
    # 某一行缺的槽由空格占位，尾列（存量）仍在同一列；一行都没有用到的槽整列不出现。
    _rows.append((("#3",), ("R", "10k", "直插"), ("存量: 多(3)",)))
    _lines = format_slot_table(_rows)
    eq(len({display_width(l[: l.index("存量:")]) for l in _lines}), 1,
       "缺槽的行也要把尾列推到同一列")
    ok("MLCC" not in "".join(_lines), "一行都没有用到的槽整列不出现")
    eq(_lines[2], "#3  R  10k         直插  存量: 多(3)", "中间的空槽按宽度留白")

    # --- 备注挂在哪：元件行下面，缩进两格 ---
    #
    # show 删掉之后，备注就只剩这里能读了（再就是删除确认那一刻）。它不能当表格的
    # 一列——自由文本、长度不定，会把列宽撑变形——所以单独占一行。两个渲染点共用
    # attach_notes，这一组钉的就是那份排版。
    _note_c1 = Component(id="33333333-3333-3333-3333-333333333333", seq=1,
                         tags=["C", "1uF", "0805"], stock=Stock("coarse", level=0),
                         note="只在副本里")
    _note_c2 = Component(id="44444444-4444-4444-4444-444444444444", seq=2,
                         tags=["R", "10k", "0805"], stock=Stock("coarse", level=0))
    eq(attach_notes(["A", "B"], [_note_c1, _note_c2]),
       ["A", "  备注: 只在副本里", "B"], "备注补在对应元件行的下面，缩进两格")
    eq(attach_notes(["A", "B"], [_note_c2, _note_c1]),
       ["A", "B", "  备注: 只在副本里"], "备注跟着它自己那一行，不会串位")
    eq(attach_notes(["A"], [_note_c2]), ["A"], "没有备注就一行都不多占")
    eq(attach_notes([], []), [], "空输入返回空列表")
    eq(component_to_json(_note_c1)["note"], "只在副本里", "--json 结果带 note")
    eq(component_to_json(_note_c2)["note"], "", "空备注也输出空串，schema 稳定")

    # --- 封装追问的回答怎么变成标签 ---
    #
    # 用户在「封装 > 」处答的是一句话，库里存的却是标签。整句原样存下来会造出
    # `直插 2.54` 这种带空格的标签，而词表查的是去掉空格后的键（`直插2.54`），
    # 谁也认不出它——本该是封装的东西于是落进显示层的「其它」列，两个词一起失效。
    # 这一组钉三档收法。
    eq(resolve_package_answer(["led", "共阴绿红"], "直插 2.54"),
       (["led", "共阴绿红", "直插", "2.54"], None),
       "多词回答拆成普通标签")
    eq(resolve_package_answer(["C", "100nF"], "0805"), (["C", "100nF", "0805"], None),
       "单词回答查表命中")
    eq(resolve_package_answer(["C", "100nF"], "NO PACKAGE"),
       (["C", "100nF", "无封装"], None),
       "哨兵：整句就是一个封装名，存规范写法")
    eq(resolve_package_answer(["C", "100nF"], "TO 220"),
       (["C", "100nF", "TO-220"], None),
       "带空格的真封装名不能被拆成两条")
    eq(resolve_package_answer(["C", "10uF"], "Case A"), (["C", "10uF", "Case A"], None),
       "Case 码同理")
    eq(resolve_package_answer(["C", "100nF"], "MY-PKG"),
       (["C", "100nF", "MY-PKG"], "MY-PKG"),
       "表外的自定义封装名整句收下，并作为 extra_package 交出去")
    eq(resolve_package_answer(["C", "100nF"], "My Pkg"),
       (["C", "100nF", "My Pkg"], "My Pkg"),
       "拆开也认不出封装的就不拆：拆成两条谁也不认，整条 add 会失败")
    _tags, _extra = resolve_package_answer(["C", "100nF"], "MY-PKG")
    ok(not classify_tags(_tags, extra_package=_extra).has("package_missing"),
       "表外封装名必须能过封装校验，这是它作为 extra_package 交出去的理由")
    # 第二档只要求判得出封装，不管别的问题：真冲突时让 cmd_add 当场报错，
    # 好过退回第三档把一句有问题的回答静默存成封装名。
    _tags, _extra = resolve_package_answer(["100nF"], "0805 100kΩ")
    ok(_extra is None and _tags == ["100nF", "0805", "100kΩ"],
       "回答里混进冲突的标签也照收")
    ok(classify_tags(_tags).has("type_conflict"),
       "冲突交给 cmd_add 报，不在这一层静默吞掉")
    # 吸收：`2.54` 派生出的安装方式就是直插，和 add C 直插 5x11 100uF 一个行为。
    _tags, _ = resolve_package_answer(["led", "共阴绿红"], "直插 2.54")
    eq(classify_tags(_tags).absorbed, ("直插",), "多词回答里的直插仍会被封装吸收")

    # --- 包含判定：重复录入该不该拦 ---
    #
    # 判定复用检索的分层内核，但门槛取 0.9、另加一条字面相同的快速通道。这两个
    # 取舍各有一个真实反例钉着，理由见 _CONTAINMENT_MIN 与 _tag_covered 的注释。
    _cap = Component(id="cccccccc-1111-2222-3333-444444444444", seq=1,
                     tags=["C", "100nF", "50V", "MLCC", "0805"],
                     stock=Stock("coarse", level=3))

    def _contain_kinds(comps, ts):
        return [(kind, c.seq) for (c, kind, _w) in find_containment(comps, ts)]

    eq(_contain_kinds([_cap], ["C", "0805", "100nF", "50V", "MLCC"]), [("same", 1)],
       "标签乱序的同义写法算等价——判定与列序无关")
    eq(_contain_kinds([_cap], ["C", "0805", "0.1uF", "50V", "MLCC"]), [("same", 1)],
       "0.1uF 与 100nF 在包含判定里必须等价")
    eq(_contain_kinds([_cap], ["C", "0805", "100nF"]), [("subset", 1)],
       "新元件信息更少，是已有元件的子集")
    eq(_contain_kinds([_cap], ["C", "0805", "100nF", "50V", "MLCC", "1%"]), [("superset", 1)],
       "新元件更具体，反过来是超集")
    eq(_contain_kinds([_cap], ["R", "0805", "10k"]), [], "无关元件不产生关系")
    eq(find_containment([_cap], []), [],
       "空标签集合必须显式挡掉：all([]) 为真，会命中库里一切")

    _tenk = Component(id="dddddddd-1111-2222-3333-444444444444", seq=2,
                      tags=["R", "10kΩ", "0805"], stock=Stock("coarse", level=0))
    eq(_contain_kinds([_tenk], ["R", "10k", "0805"]), [("same", 2)], "10k 与 10kΩ 必须等价")

    # 裸前缀是没有维度的（parse_quantity('3p') 得到的 dim 是 None），库里又没有
    # 类型标签给得出 hint，两条一模一样的 3p 在分层里只能拿 0.70，会被门槛挡在
    # 门外。字面相同的快速通道就是为这条存在的——它是用户真录重过的那一对。
    _hdr = Component(id="eeeeeeee-1111-2222-3333-444444444444", seq=3,
                     tags=["排母", "3p", "2.54"], stock=Stock("coarse", level=3))
    eq(_contain_kinds([_hdr], ["排母", "3p", "2.54"]), [("same", 3)],
       "裸前缀的量拿不到高分，字面相同必须直接算同一条")
    eq(_contain_kinds([_hdr], ["排母", "3p"]), [("subset", 3)], "少一个封装的排母更宽泛")

    # 子串层双向对称，必须整体排除，否则前缀型真子集会被判成「等价」、方向还翻。
    _led = Component(id="ffffffff-1111-2222-3333-444444444444", seq=4,
                     tags=["led", "2.54", "共阴绿红"], stock=Stock("coarse", level=0))
    ok(all(kind != "same" for (kind, _s) in _contain_kinds([_led], ["led", "2.54", "共阴"])),
       "共阴 子串命中 共阴绿红、反向也中，绝不能因此报成等价")

    _clines = render_containment(find_containment([_hdr], ["排母", "3p", "2.54"]),
                                 ["排母", "3p", "2.54"]).splitlines()
    ok(_clines[0] == "新元件 排母 3p 2.54 与库中 1 个元件存在包含关系：",
       "说明块首行顶格，命令行和交互模式各自在它之上加自己的前缀")
    ok("等价" in _clines[1] and "#3" in _clines[1], "表格里要同时给出序号和关系")

    # --- argv 重写 ---
    eq(normalize_argv(["--search", "51R"]), ["search", "51R"], "--search 应被重写")
    eq(normalize_argv(["--file", "x.json", "--search", "51R"]),
       ["--file", "x.json", "search", "51R"], "--file 的值不应被误判为查询词")
    eq(normalize_argv(["add", "C", "--search"]), ["add", "C", "--search"],
       "子命令之后的 --search 是标签，不应被重写")

    # --- show 已下线 ---
    #
    # 三处命令面必须同去：SUBCOMMANDS、ARG_ALIASES、argparse 的子解析器。少改一处，
    # normalize_argv 就会放行一个 argparse 不认识的名字，报错变成一句莫名的 usage。
    ok("show" not in SUBCOMMANDS, "SUBCOMMANDS 不再包含 show")
    ok("--show" not in ARG_ALIASES, "ARG_ALIASES 不再包含 --show")
    eq(normalize_argv(["--show", "#7"]), ["--show", "#7"], "--show 不再是子命令别名")

    # --- 句柄层独立于已删的 resolve_target ---
    #
    # search 的 #序号 与 id 前缀匹配是 match_token 内联实现的另一份，和 show 用过的
    # resolve_target 无关。show 一走，这两条路径在自检里就再没有别的断言摸到过，
    # 而它们正是「找要召回」那一半，所以补在这里。
    _handle_c = Component(id="11111111-abcd-2222-3333-444444444444", seq=7,
                          tags=["R", "4k7", "0805"], stock=Stock("coarse", level=0))
    eq(match_token("#7", _handle_c)[0], 1.0, "search #7 仍走句柄层")
    eq(match_token("11111111", _handle_c)[0], 1.0, "search 的 id 前缀仍走句柄层")
    eq(match_token("#8", _handle_c)[0], 0.0, "别的序号不命中")

    # --- 封装识别：正例 ---
    for raw, want in (("0805", "0805"), ("0603", "0603"), ("0402", "0402"),
                      ("1206", "1206"), ("2512", "2512"), ("1005", "1005"),
                      ("SOT-23", "SOT-23"), ("SOT23", "SOT-23"), ("sot 23", "SOT-23"),
                      ("DIP-8", "DIP-8"), ("dip8", "DIP-8"), ("PDIP-8", "PDIP-8"),
                      ("SOIC-14", "SOIC-14"), ("TSSOP-16", "TSSOP-16"),
                      ("QFN-32", "QFN-32"), ("LQFP-48", "LQFP-48"),
                      ("TO-220", "TO-220"), ("DO-41", "DO-41"),
                      ("SOD-123", "SOD-123"), ("SC-70", "SC-70"),
                      ("SOT-23-5", "SOT-23-5"), ("QFN", "QFN"), ("DPAK", "DPAK"),
                      ("贴片", "贴片"), ("SMD", "贴片"), ("直插", "直插"), ("THT", "直插"),
                      ("NO PACKAGE", "无封装"), ("no package", "无封装"), ("无封装", "无封装")):
        eq(canon_package(raw), want, f"封装识别 {raw!r}")

    # --- 机械尺寸：正例 ---
    # 这一类和上面靠查表的不同，形态是正则匹配，所以单列一组。
    for raw, want in (("5x11", "5x11"), ("5X11", "5x11"), ("6.3x11", "6.3x11"),
                      ("6.3X11", "6.3x11"), ("6x6x5", "6x6x5"), ("5x20", "5x20"),
                      ("1x40", "1x40"), ("6x6", "6x6"),
                      ("2.54", "2.54"), ("5.08", "5.08"), ("7.62", "7.62"),
                      ("5mm", "5mm"), ("3mm", "3mm"),
                      ("18650", "18650"), ("21700", "21700"),
                      ("A", "Case A"), ("a", "Case A"), ("E", "Case E"),
                      ("Case C", "Case C"), ("case c", "Case C"), ("C型", "Case C"),
                      ("D-Case", "Case D")):
        eq(canon_package(raw), want, f"机械尺寸识别 {raw!r}")
    # --- 机械尺寸：带单位 ---
    # `5x11mm` 是铝电解规格书上的印法，比裸 `5x11` 更常见。这一组必须归一到
    # 去掉单位的同一个封装，否则同一颗电容写两次会存成两个不同的封装，
    # 搜哪个都只能找到一半。
    for raw, want in (("5x11mm", "5x11"), ("5X11MM", "5x11"), ("5x11MM", "5x11"),
                      ("6.3x11mm", "6.3x11"), ("6x6x5mm", "6x6x5"),
                      ("8x12mm", "8x12"), ("10x20mm", "10x20"),
                      ("5X11mm", "5x11")):
        eq(canon_package(raw), want, f"带单位的机械尺寸 {raw!r} 应归一到 {want!r}")
    # 单位只在这一个分支里剥。`5mm` 是直插 LED 的直径，走上面那张白名单——
    # 若改成全局剥 mm，它会被剥成裸 `5`，白名单就永远命不中了。
    eq(canon_package("5mm"), "5mm", "5mm 是 LED 直径，不该被当成「剥掉单位的尺寸」")
    eq(canon_package("3mm"), "3mm", "3mm 同理")
    eq(canon_package("25mm"), None, "25mm 不在 LED 直径白名单里，也不该被当成尺寸码")
    # 裸 C / D 刻意不收：它们是类型码，canon_type 在 classify_tags 里先跑，
    # `C` 永远到不了封装这一层；而且即便有人直接调 canon_package("D")，返回
    # 「Case D」也太容易误导。要表达钽电容的 Case C / Case D 得写 Case C 或 C型。
    ok(canon_package("C") is None, "裸 C 不应被当成钽电容的 Case C")
    ok(canon_package("D") is None, "裸 D 不应被当成钽电容的 Case D")

    # --- 封装识别：负例（误判防线）---
    for raw in ("1000", "2200", "4700", "104", "473", "1%", "X7R", "C0G",
                "NE555", "LM358", "1N4148", "100nF", "51Ω", "16V",
                "0.5", "1.5", "2.0"):   # 通用小数值是合法电阻，不能被吞成封装
        ok(canon_package(raw) is None, f"{raw!r} 不应被识别为封装")
    # `TOMATO` 必须躲开 `TO` 前缀——这就是族名后面强制要求数字的原因
    ok(canon_package("TOMATO") is None, "TOMATO 不应被 TO 前缀吃掉")

    # --- 类型推断：正例 ---
    def tcode(tags):
        return classify_tags(tags).type_code

    for tags, want in ((["1uF"], "C"), (["100nF"], "C"), (["0.1uF"], "C"), (["22pF"], "C"),
                       (["51Ω"], "R"), (["51R"], "R"), (["0R"], "R"),
                       (["4u7H"], "L"), (["10uH"], "L"), (["1mH"], "L"),
                       (["10k"], "R"), (["1M"], "R"), (["4k7"], "R"), (["1K22"], "R"),
                       (["100p"], "C"),
                       (["1N4148"], "D"), (["2N2222"], "Q"), (["SS34"], "D"),
                       (["PC817"], "OPTO"), (["NE555"], "U"), (["STM32F103"], "U")):
        eq(tcode(tags), want, f"类型推断 {tags}")

    # --- 类型推断：负例（这一组是误判防线，最重要）---
    # `1N4148` 之所以能安全地落在 D，全靠型号表排在惯例之前：不拦的话
    # parse_quantity("1N4148") 会给出 1.4148e-9（N 被当成纳），一个二极管
    # 就被惯例推成电容了。
    eq(tcode(["1N4148"]), "D", "1N4148 必须走型号表，不能掉进 N 前缀陷阱")
    for tags in (["M7"], ["K2"], ["N1"],          # 整数部分为空（M7 会被解析成 700000）
                 ["104K"], ["470K"], ["104M"],     # 大写 K 与 EIA 码同形 / 量级越界
                 ["4u7"], ["2u2"], ["1u"], ["10u"],  # µ 前缀真实歧义
                 ["100n"], ["4n7"],                 # n 前缀同样两可：nF 与 nH 都常见
                 ["1m"],                            # mΩ / mH / mF 三可
                 ["16V"], ["50V"], ["2W"]):         # 维度指向不了唯一类型
        eq(tcode(tags), None, f"{tags} 不应推断出类型")
    eq(tcode(["470k"]), "R", "小写 k 应正常推断为 R")
    ok(classify_tags(["4u7", "0603"]).has("micro_ambiguous"), "4u7 应报 µ 歧义")

    # --- 认错修复：显式型号表必须压过模式表 ---
    # SS8050 / SS8550 是长电的 NPN / PNP 三极管，而 SS 前缀在模式表里归二极管
    # （SS14 确实是肖特基）。猜错的代价是错误被静默写进盘，比认不出严重得多。
    eq(tcode(["SOT-23", "SS8050"]), "Q", "SS8050 是三极管，不能被 SS 前缀判成二极管")
    eq(tcode(["SS8550"]), "Q", "SS8550 同理")
    eq(tcode(["S8050"]), "Q", "S8050 应经显式表认出")
    eq(tcode(["SS14"]), "D", "SS14 仍是肖特基，不能被误伤")
    eq(tcode(["SOT-23", "SS34"]), "D", "SS34 同理")

    # --- n 前缀改报歧义 ---
    # 100nH 是 0805 电感的常规值，和 100nF 一样常见，所以 n 与 u 同等处理。
    _p = classify_tags(["0805", "100n"])
    eq(_p.type_code, None, "0805 100n 不应推断出类型")
    ok(_p.has("micro_ambiguous"), "0805 100n 应报歧义")
    # 但只在候选集为空时才报——有别的类型证据时它不该拦路。这三条守住那条边界。
    eq(tcode(["0805", "100nF", "50V"]), "C", "有单位就不该报歧义")
    eq(tcode(["L", "0805", "100n"]), "L", "有显式类型就不该报歧义")
    eq(tcode(["0805", "MLCC", "100n"]), "C", "有介质指向就不该报歧义")
    eq(tcode(["0805", "22p"]), "C", "p 前缀不报歧义（pH 几乎不存在）")

    # --- 型号表补漏 ---
    for tag, want in (("IRLZ44N", "Q"), ("IRFZ44N", "Q"), ("2SK170", "Q"),
                      ("MMBT3904", "Q"), ("7805", "U"), ("78L05", "U"),
                      ("7912", "U"), ("MPU6050", "U"), ("DS18B20", "U"),
                      ("ULN2003", "U"), ("XC6206", "U"), ("TDA2030", "U"),
                      ("MOC3021", "OPTO"), ("6N137", "OPTO"), ("4N25", "OPTO"),
                      ("WS2812", "LED"), ("XH2.54", "J"), ("VH3.96", "J")):
        eq(tcode([tag]), want, f"型号表 {tag}")

    # --- 中文描述性词 ---
    eq(tcode(["直插", "5mm", "红"]), "LED", "颜色词应指向 LED")
    eq(tcode(["轻触", "6x6x5"]), "SW", "轻触应指向开关")
    for tag, want in (("色环", "R"), ("金属膜", "R"), ("碳膜", "R"), ("水泥", "R"),
                      ("工字", "L"), ("磁环", "L"), ("磁珠", "L"),
                      ("船型", "SW"), ("微动", "SW"), ("牛角", "J"), ("杜邦", "J"),
                      ("微调", "POT"), ("纽扣", "BAT"), ("锂电", "BAT")):
        eq(tcode([tag]), want, f"中文描述词 {tag}")

    # 描述性词是**弱**证据，不是显式类型。这条区分很要紧：用户写「红」是在描述
    # 颜色，不是在声明类型，所以它必须能被补进标签；而作为弱证据，它也压不过
    # 单位——`红 100nF` 是电容不是 LED。放进 TYPE_ALIASES 会两条都做错。
    _p = classify_tags(["直插", "5mm", "红", "20mA"])
    eq(_p.type_code, "LED", "颜色词应推出 LED")
    eq(_p.added_type, "LED", "描述性词推出来的类型要能补进标签")
    eq(_p.type_source, "descriptor", "来源应标成描述词，不能混进「介质」")
    eq(tcode(["红", "100nF", "0805"]), "C", "单位证据应压过描述词")
    _p2 = classify_tags(["色环", "直插", "10k"])
    eq(_p2.type_code, "R", "色环应推出电阻")
    eq(_p2.added_type, "R", "色环同理要能补进标签")

    # --- 介质参与类型推断 ---
    eq(tcode(["薄膜", "104", "100V"]), "C", "介质词应能推出电容")
    eq(tcode(["钽", "A", "10uF"]), "C", "钽 + Case 码")
    ok(classify_tags(["云母"]).has("package_missing"), "云母电容仍缺封装")

    # --- 频率兜底 ---
    eq(tcode(["3225", "16MHz"]), "XTAL", "频率应兜底推断为晶振")
    eq(tcode(["LQFP-48", "STM32F103C8T6", "16MHz"]), "U", "型号表应压过频率兜底")
    eq(tcode(["U", "16MHz"]), "U", "显式类型应压过频率兜底")

    # --- 封装候选的优先级 ---
    eq(classify_tags(["C", "直插", "5x11", "100uF"]).package, "5x11", "机械尺寸应压过安装方式")
    eq(classify_tags(["C", "贴片", "0805", "100nF"]).package, "0805", "尺寸码应压过安装方式")
    eq(classify_tags(["C", "贴片", "100nF", "50V"]).package, "贴片", "只有安装方式时照常采用")
    # 两个标签都被封装槽吃掉了，参数仍然是空的——多写一个封装不等于有了参数。
    ok(classify_tags(["C", "直插", "5x11"]).has("param_missing"), "C 直插 5x11 仍缺参数")

    # --- 吸收冗余的安装方式标签 ---
    eq(classify_tags(["C", "直插", "5x11", "100uF"]).absorbed, ("直插",), "直插应被 5x11 吸收")
    eq(classify_tags(["C", "贴片", "0805", "100nF"]).absorbed, ("贴片",), "贴片应被 0805 吸收")
    eq(classify_tags(["C", "直插", "DIP-8"]).absorbed, ("直插",), "直插应被 DIP-8 吸收")
    # 反例：自相矛盾的要留下痕迹，派生不出安装方式的也不能删。
    eq(classify_tags(["C", "直插", "0805", "100nF"]).absorbed, (), "直插 + 0805 自相矛盾，不吸收")
    eq(classify_tags(["BAT", "直插", "18650"]).absorbed, (), "18650 派生不出安装方式，不吸收")
    eq(classify_tags(["C", "贴片", "100nF", "50V"]).absorbed, (), "没有落选者就没有可吸收的")
    # 吸收只报告、不执行——classify_tags 必须是纯读的，删除动作在 cmd_add 里。
    _ts = ["C", "直插", "5x11"]
    classify_tags(_ts)
    eq(_ts, ["C", "直插", "5x11"], "classify_tags 不应改动传入的 tags")

    # --- 安装方式判定 ---
    for pkg, want in (("0805", "贴片"), ("0603", "贴片"), ("DIP-8", "直插"),
                      ("PDIP-8", "直插"), ("SOT-23", "贴片"), ("SOIC-14", "贴片"),
                      ("TO-220", "直插"), ("TO-92", "直插"), ("TO-252", "贴片"),
                      ("DO-41", "直插"), ("DO-35", "直插"), ("Case A", "贴片"),
                      ("2.54", "直插"), ("5.08", "直插"), ("5mm", "直插"),
                      ("5x11", "直插"), ("6.3x11", "直插"), ("1x40", "直插"),
                      ("贴片", "贴片"), ("直插", "直插")):
        eq(package_mount(pkg), want, f"安装方式 {pkg}")
    # 判不出来的一律 None——「不知道」比猜一个强。
    # 6.3x5.4 是关键反例：贴片铝电解的标法是「直径 x 高」但高小于直径，
    # 正好被「第二个数更大」这条判据排除，不用另建例外名单。
    for pkg in ("6x6x5", "6x6", "6.3x5.4", "18650", "无封装", "TOMATO"):
        eq(package_mount(pkg), None, f"{pkg} 的安装方式应判不出来")

    # --- 搜索能穿透封装（用户要求的核心场景）---
    # 标签里**没有**「直插」两个字，只有一个 5x11，搜「直插」仍要命中。
    _c1 = Component(id="x", seq=1, tags=["C", "5x11", "100uF", "25V"],
                    stock=Stock("coarse", level=0))
    ok(match_token("直插", _c1)[0] > 0, "搜直插应命中只写了 5x11 的元件")
    eq(match_token("贴片", _c1)[0], 0.0, "该元件不该被搜贴片命中")
    ok(match_token("5x11", _c1)[0] > 0, "搜 5x11 本身也要能命中")
    # 守卫：卧式 / 立式 不是安装方式，不能走归约层，否则原有字符串匹配会被打坏。
    _c2 = Component(id="y", seq=2, tags=["R", "卧式", "10k"],
                    stock=Stock("coarse", level=0))
    ok(match_token("卧式", _c2)[0] > 0, "搜卧式应仍走字符串层")
    eq(match_token("直插", _c2)[0], 0.0, "卧式的电阻不该被搜直插命中")

    # --- 封装归约：同一件事的多种写法要能互相搜到 ---
    # 机械尺寸引入了 `A` / `Case A` 这类同义写法，没有这一层它们就各搜各的。
    _c3 = Component(id="z", seq=3, tags=["C", "钽", "A", "10uF"],
                    stock=Stock("coarse", level=0))
    ok(match_token("Case A", _c3)[0] > 0, "搜 Case A 应命中写成 A 的标签")
    ok(match_token("A", _c3)[0] > 0, "搜 A 本身也要命中")
    _c4 = Component(id="w", seq=4, tags=["U", "SOT-23", "NE555"],
                    stock=Stock("coarse", level=0))
    ok(match_token("SOT23", _c4)[0] > 0, "搜 SOT23 应命中 SOT-23")
    ok(match_token("sot 23", _c4)[0] > 0, "带空格的分隔写法同样要能命中")

    # --- 带单位的机械尺寸：两种写法要互相搜到 ---
    # 这是真实数据里踩到的坑：`5x11mm` 漏认 → 被追问封装 → 标签里留下两份尺寸。
    _c5 = Component(id="v", seq=5, tags=["C", "10uF", "铝电解", "5x11mm", "25V"],
                    stock=Stock("coarse", level=0))
    _p5 = classify_tags(_c5.tags)
    eq(_p5.package, "5x11", "只写了 5x11mm 也要拿到封装")
    eq(package_mount(_p5.package), "直插", "5x11mm 推出的安装方式仍是直插")
    ok(match_token("5x11", _c5)[0] > 0, "搜 5x11 应命中只写了 5x11mm 的元件")
    ok(match_token("5x11mm", _c5)[0] > 0, "搜 5x11mm 本身也要命中")
    ok(match_token("直插", _c5)[0] > 0, "搜直插应命中只写了 5x11mm 的元件")
    eq(match_token("贴片", _c5)[0], 0.0, "该元件不该被搜贴片命中")

    # --- 冲突与三类校验 ---
    ok(classify_tags(["1uF", "100Ω", "0805"]).has("type_conflict"), "容值与阻值并存应判冲突")
    ok(classify_tags(["10k", "100p", "0603"]).has("type_conflict"), "两个惯例推出不同码应判冲突")
    ok(classify_tags(["薄膜", "100k", "0805"]).has("type_conflict"), "介质指向 C 与惯例指向 R 应判冲突")
    ok(classify_tags(["C", "0805"]).has("param_missing"), "C 0805 缺参数")
    ok(classify_tags(["C", "100nF"]).has("package_missing"), "C 100nF 缺封装")
    ok(not classify_tags(["C", "100nF"]).has("type_missing"), "C 100nF 不缺类型")
    _p = classify_tags(["104", "473"])
    ok(_p.has("type_missing") and _p.has("package_missing"), "104 473 同时缺类型与封装")
    ok(not _p.has("param_missing"), "104 473 不缺参数")
    for good in (["J", "直插", "2.54", "40P"],
                 ["C", "1uF", "16V", "无封装"],
                 ["C", "0805", "贴片", "100nF", "50V"],
                 ["D", "1N4148", "SOD-123"]):
        ok(not classify_tags(good).issues, f"{good} 应当完整通过三类校验")

    # --- 补全与幂等 ---
    eq(classify_tags(["1uF", "0805"]).added_type, "C", "1uF 0805 应补类型 C")
    eq(classify_tags(["C", "1uF", "0805"]).added_type, None, "已有显式类型不应重复补")
    eq(classify_tags(["电容", "0805", "100nF"]).added_type, None, "用中文别名写的类型同样不重复补")
    eq(classify_tags(["电容", "0805", "100nF"]).type_code, "C", "电容 应归约为 C")

    # --- 介质表 ---
    ok(not _MEDIUM_CONFLICTS, f"介质别名表存在冲突：{_MEDIUM_CONFLICTS}")
    for raw, want in (("MLCC", "MLCC"), ("mlcc", "MLCC"), ("陶瓷", "MLCC"),
                      ("瓷片", "MLCC"), ("陶瓷电容", "MLCC"), ("瓷片电容", "MLCC"),
                      ("电解", "电解"), ("铝电解", "电解"), ("电解电容", "电解"),
                      ("钽", "钽"), ("钽电容", "钽"),
                      ("薄膜", "薄膜"), ("涤纶", "薄膜"), ("云母", "云母")):
        eq(canon_medium(raw), want, f"介质归约 {raw!r}")
    for raw in ("C", "R", "0805", "100nF", "1N4148", "贴片", "直插"):
        ok(canon_medium(raw) is None, f"{raw!r} 不应被识别为介质")
    # 三张词汇表两两无交集：一个词同时属于两张表会让分类结果取决于检查顺序
    for code in TYPE_ALIASES:
        ok(canon_medium(code) is None, f"类型码 {code} 不应同时是介质")
    for name in PACKAGE_NAMES:
        ok(canon_medium(name) is None, f"封装名 {name} 不应同时是介质")

    # --- classify 的介质字段 ---
    eq(classify_tags(["C", "0805", "100nF"]).medium, None, "没写介质时 medium 为空")
    eq(classify_tags(["C", "0805", "100nF", "陶瓷"]).medium, "MLCC", "陶瓷 归约为 MLCC")
    eq(classify_tags(["C", "0805", "100nF", "薄膜"]).medium, "薄膜", "薄膜 归约为 薄膜")

    # --- 自动补 MLCC ---
    eq(classify_tags(["C", "0805", "100nF"]).added_medium, "MLCC", "片式电容应补 MLCC")
    eq(classify_tags(["C", "0603", "100nF"]).added_medium, "MLCC", "0603 同样要补")
    eq(classify_tags(["C", "0805", "100nF", "薄膜"]).added_medium, None,
       "用户写了介质就不该再补 MLCC")
    eq(classify_tags(["C", "直插", "5.08mm", "100nF"]).added_medium, None,
       "直插推不出陶瓷，不该补")
    eq(classify_tags(["R", "0805", "10k"]).added_medium, None, "电阻不补 MLCC")
    eq(classify_tags(["C", "SOT-23", "100nF"]).added_medium, None,
       "非尺寸码的封装不补 MLCC")

    # --- 介质归约的匹配回归 ---
    _mlcc = Component(id="cccccccc-1111-2222-3333-444444444444", seq=10,
                      tags=["C", "MLCC", "0805", "100nF"], stock=Stock("coarse", level=1))
    for q in ("MLCC", "mlcc", "陶瓷", "瓷片", "陶瓷电容", "瓷片电容"):
        ok(match_token(q, _mlcc)[0] > 0, f"搜 {q!r} 应命中补了 MLCC 的元件")
    _elec = Component(id="dddddddd-1111-2222-3333-444444444444", seq=11,
                      tags=["C", "直插", "1000uF", "电解"], stock=Stock("coarse", level=1))
    ok(match_token("电解电容", _elec)[0] > 0, "电解电容应命中标了电解的元件")
    ok(match_token("MLCC", _elec)[0] == 0, "电解电容不该被 MLCC 搜到")
    ok(match_token("陶瓷", _elec)[0] == 0, "电解电容不该被 陶瓷 搜到")
    _tan = Component(id="eeeeeeee-1111-2222-3333-444444444444", seq=12,
                     tags=["C", "钽电容", "A", "10uF"], stock=Stock("coarse", level=1))
    ok(match_token("钽电容", _tan)[0] > 0, "钽电容应命中——单字不该被防护挡掉")
    ok(match_token("钽", _tan)[0] > 0, "单字 钽 也应命中同一元件")

    # --- 存量标签：带前缀的数字 ---
    for raw, want in (("qty23", {"mode": "accurate", "count": 23}),
                      ("QTY23", {"mode": "accurate", "count": 23}),
                      ("level1", {"mode": "coarse", "level": 1}),
                      ("Level2", {"mode": "coarse", "level": 2}),
                      ("level0", {"mode": "coarse", "level": 0}),
                      ("level4", {"mode": "coarse", "level": 4})):
        _rest, _s, _iss = extract_stock_tags(["C", "0805", raw])
        eq(_s.to_dict() if _s else None, want, f"存量标签 {raw!r}")
        eq(tuple(_rest), ("C", "0805"), f"存量标签 {raw!r} 应从标签里摘掉")
        eq(tuple(_iss), (), f"存量标签 {raw!r} 不该有问题")

    # --- 存量标签：中文等级词 ---
    # `很少` → 1 和 `很多` → 4 是按**语义强度**归位的结果，不是等价词。
    # 这两条要钉死：后来人很容易顺手把它们改成 2 和 3，那样五档就退化成三档了。
    for raw, level in (("无", 0), ("没有", 0),
                       ("极少", 1), ("很少", 1), ("一点点", 1),
                       ("少", 2),
                       ("多", 3), ("不少", 3),
                       ("极多", 4), ("很多", 4), ("大量", 4)):
        _rest, _s, _iss = extract_stock_tags([raw])
        eq(_s.to_dict() if _s else None, {"mode": "coarse", "level": level},
           f"等级词 {raw!r} → level {level}")
        eq(tuple(_rest), (), f"等级词 {raw!r} 应被摘掉")
        eq(tuple(_iss), (), f"等级词 {raw!r} 不该有问题")

    # --- 存量标签：英文等级词 ---
    # 英文自成一条单调刻度，五档各占一个词：none < few < some < many < lots。
    # 这不是逐词翻译而是刻意对齐——few 严格说更贴「很少」——所以下面那条
    # 单调性断言是这个设计的**核心不变量**：将来有人顺手把 few 挪到 2 档、
    # some 挪到 3 档，刻度就断了。和中文那边「很少必须归极少」是同一个道理。
    _ladder = (("none", 0), ("few", 1), ("some", 2), ("many", 3), ("lots", 4))
    for raw, level in _ladder:
        _rest, _s, _iss = extract_stock_tags([raw])
        eq(_s.to_dict() if _s else None, {"mode": "coarse", "level": level},
           f"英文等级词 {raw!r} → level {level}")
        eq(tuple(_rest), (), f"英文等级词 {raw!r} 应被摘掉")
        eq(tuple(_iss), (), f"英文等级词 {raw!r} 不该有问题")
    _ladder_levels = [lv for _w, lv in _ladder]
    ok(_ladder_levels == list(range(5)),
       "英文阶梯必须严格单调并铺满五档——刻度一断，「词越大库存越多」这条唯一的记忆线索就没了")

    # --- 存量标签：英文同义词 ---
    for raw, level in (("empty", 0), ("zero", 0),
                       ("scarce", 1),
                       ("little", 2), ("low", 2),
                       ("several", 3),
                       ("plenty", 4), ("tons", 4)):
        _rest, _s, _iss = extract_stock_tags([raw])
        eq(_s.to_dict() if _s else None, {"mode": "coarse", "level": level},
           f"英文同义词 {raw!r} → level {level}")
        eq(tuple(_rest), (), f"英文同义词 {raw!r} 应被摘掉")
        eq(tuple(_iss), (), f"英文同义词 {raw!r} 不该有问题")

    # --- 存量标签：英文大小写不敏感 ---
    # normalize_text 刻意不做 casefold（M 与 m 语义相反），所以这一层是单独加的。
    for raw, level in (("NONE", 0), ("None", 0), ("FEW", 1), ("MANY", 3), ("Lots", 4)):
        _rest, _s, _iss = extract_stock_tags([raw])
        eq(_s.to_dict() if _s else None, {"mode": "coarse", "level": level},
           f"英文等级词大小写：{raw!r} → level {level}")
        eq(tuple(_rest), (), f"英文等级词大小写：{raw!r} 应被摘掉")

    # 两个键只差大小写会让 casefold 索引少一项，静默丢掉其中一个。这条断言
    # 是那个风险的唯一探测器。
    eq(len(_STOCK_WORDS_CI), len(STOCK_WORDS), "存量词表 casefold 之后不应有键冲突")

    # `no` 刻意不进词表：它是封装哨兵 `NO PACKAGE` 的第一个词，命令行标签按
    # 空格分词，`add C 0805 100nF NO PACKAGE`（不加引号）里的裸 `NO` 会被
    # 静默当成存量「无」。`none` 已经覆盖了这个意思，不值得冒这个险。
    for raw in ("no", "NO", "No"):
        _rest, _s, _iss = extract_stock_tags([raw])
        eq(_s, None, f"裸 {raw!r} 不该被当成存量——它是 NO PACKAGE 的第一个词")
        eq(tuple(_rest), (raw,), f"裸 {raw!r} 应原样留下")

    # --- 存量标签：负例与不误伤 ---
    for raw in ("qty", "level", "qty23x", "xqty23", "挺多的", "不多了", "3", "23", ""):
        _rest, _s, _iss = extract_stock_tags([raw])
        eq(_s, None, f"{raw!r} 不是存量标签")
        eq(tuple(_rest), (raw,), f"{raw!r} 应原样留下")
        eq(tuple(_iss), (), f"{raw!r} 不该报错")
    # `无封装` 是封装的哨兵，`无` 是存量的词——精确匹配，互不干扰
    eq(extract_stock_tags(["无封装"])[1], None, "无封装 不该被当成存量")
    eq(extract_stock_tags(["无封装"])[0], ["无封装"], "无封装 应原样留下")
    # 含「多」的类型名同理：匹配是整标签精确比对，不是子串
    eq(extract_stock_tags(["多圈电位器"])[1], None, "多圈电位器 不该被误摘")

    # --- 存量标签：错误 ---
    ok(tuple(extract_stock_tags(["level5"])[2]), "level5 应报越界")
    ok(tuple(extract_stock_tags(["qty-3"])[2]), "qty-3 应报负数")
    ok(tuple(extract_stock_tags(["qty23", "level1"])[2]), "两个存量标签应报冲突")
    ok(tuple(extract_stock_tags(["多", "少"])[2]), "两个中文等级词同样报冲突")
    ok(tuple(extract_stock_tags(["none", "lots"])[2]), "两个英文等级词同样报冲突")

    # --- 存量标签不参与「参数」判定 ---
    # 这就是必须在校验三类标签**之前**摘存量的原因。
    _rest, _s, _ = extract_stock_tags(["C", "0805", "qty23"])
    ok(_s is not None, "qty23 应被摘成存量")
    ok(classify_tags(_rest).has("param_missing"),
       "C 0805 qty23 摘掉存量后仍然缺参数")
    _rest, _s, _ = extract_stock_tags(["C", "0805", "100nF", "lots"])
    ok(_s is not None, "lots 应被摘成存量")
    eq(tuple(_rest), ("C", "0805", "100nF"), "英文等级词也不该算进参数")

    # --- stock 命令的值语法 ---
    #
    # 值位置与标签位置是两套写法，这里把差别钉死：档位词与 levelN/qtyN 两边共用，
    # 裸数字与增减词只在值位置认。改动任何一边都要过这组断言。

    def _raises(fn, label):
        """断言 fn 抛 AppError，返回那条错误好顺带查退出码。"""
        checks[0] += 1
        try:
            fn()
        except AppError as e:
            return e
        failures.append(f"{label}：应当报错，实际通过了")
        return None

    # 词表整张都认——「档位词表就不能全收吗」这句就是这条断言。
    for _w, _lv in STOCK_WORDS.items():
        eq(parse_stock_value(_w), ("set", Stock("coarse", level=_lv)),
           f"stock 的值要认 {_w!r}")

    for _raw, _want in (("level0", Stock("coarse", level=0)),
                        ("Level2", Stock("coarse", level=2)),
                        ("qty23", Stock("accurate", count=23)),
                        ("QTY23", Stock("accurate", count=23)),
                        ("qty0", Stock("accurate", count=0))):
        eq(parse_stock_value(_raw), ("set", _want), f"stock 的值要认 {_raw!r}")

    # 裸数字就是个数：用户定的「如果只有数字就设成这个数字」。
    for _raw, _n in (("0", 0), ("23", 23), ("0805", 805)):
        eq(parse_stock_value(_raw), ("set", Stock("accurate", count=_n)),
           f"值位置的裸数字 {_raw} 是精确个数")

    # 而同样的裸数字**不能**进标签路径，否则 add C 0805 会变成 805 个。
    for _raw in ("3", "23", "0805"):
        ok(extract_stock_tags([_raw])[1] is None,
           f"裸数字 {_raw} 不是存量标签——标签路径认了它，add C 0805 就成了 805 个")

    # 增减词表：进一组、出一组，中英各半。每种都过三遍——带数字、带「掉」、省略数字。
    _up_cn = ("加", "增", "添", "补", "进", "入")
    _down_cn = ("减", "用", "耗", "去", "出", "丢", "损", "坏")
    for _v in _up_cn:
        eq(parse_stock_value(_v + "5"), ("delta", 5), f"进：{_v}5")
        eq(parse_stock_value(_v + "掉5"), ("delta", 5), f"进：{_v}掉5 与 {_v}5 等价")
        eq(parse_stock_value(_v), ("delta", 1), f"进：{_v} 省略数字就是 1")
    for _v in _down_cn:
        eq(parse_stock_value(_v + "5"), ("delta", -5), f"出：{_v}5")
        eq(parse_stock_value(_v + "掉5"), ("delta", -5), f"出：{_v}掉5")
        eq(parse_stock_value(_v), ("delta", -1), f"出：{_v} 省略数字就是 1")
    for _v in ("add", "plus", "inc", "gain"):
        eq(parse_stock_value(_v + "5"), ("delta", 5), f"进：{_v}5")
        eq(parse_stock_value(_v), ("delta", 1), f"进：{_v} 省略数字就是 1")
    for _v in ("sub", "minus", "dec", "use", "take", "lost"):
        eq(parse_stock_value(_v + "5"), ("delta", -5), f"出：{_v}5")
        eq(parse_stock_value(_v), ("delta", -1), f"出：{_v} 省略数字就是 1")

    # 符号写法。全角 ＋ 由 normalize_text 归一，英文动词不分大小写。
    for _raw, _want in (("+5", 5), ("-5", -5), ("+", 1), ("-", -1), ("+0", 0),
                        ("＋5", 5), ("ADD5", 5), ("Add5", 5), ("用掉5", -5)):
        eq(parse_stock_value(_raw), ("delta", _want), f"增减写法 {_raw!r}")

    # 歧义护栏：多 / 少 / low 是档位词，不是增减词，加了数字也不认。
    for _w in ("多", "少", "low"):
        eq(parse_stock_value(_w)[0], "set", f"{_w} 必须解成档位而不是增减")
    for _raw in ("多5", "少5", "low5", "挺多的", "够用", "51R", "无封装", "foo", ""):
        _raises(lambda r=_raw: parse_stock_value(r), f"看不懂的值 {_raw!r}")

    # --- 相对增减只在精确存量上生效 ---
    _acc42 = Stock("accurate", count=42)
    eq(apply_stock_value(_acc42, "+3", 7), Stock("accurate", count=45), "精确 42 加 3")
    eq(apply_stock_value(_acc42, "用掉2", 7), Stock("accurate", count=40), "精确 42 出 2")
    eq(apply_stock_value(Stock("coarse", level=3), "10", 7),
       Stock("accurate", count=10), "粗略档可以被精确数覆盖")
    eq(apply_stock_value(_acc42, "多", 7), Stock("coarse", level=3), "精确档可以被档位词覆盖")
    for _st, _tok, _label in ((Stock("coarse", level=3), "+1", "粗略档不能相对增减"),
                              (Stock("coarse", level=0), "-", "粗略档不能相对增减"),
                              (Stock("accurate", count=0), "-1", "精确 0 不能再减"),
                              (Stock("accurate", count=2), "减5", "减成负数要报错")):
        _raises(lambda s=_st, t=_tok: apply_stock_value(s, t, 7), _label)

    # --- 目标只认 #编号 ---
    for _raw, _want in (("#7", 7), ("#07", 7), ("#123", 123)):
        eq(parse_handle(_raw), _want, f"编号 {_raw}")
    for _raw in ("7", "51R", "a1b2c3", "#", "#x", ""):
        _raises(lambda r=_raw: parse_handle(r), f"目标 {_raw!r} 应当被拒")

    _inv = Inventory("x")
    _inv.components = [
        Component(id="11111111-1111-1111-1111-111111111111", seq=3,
                  tags=["R", "10k", "0805"], stock=Stock("coarse", level=0)),
        Component(id="22222222-2222-2222-2222-222222222222", seq=9,
                  tags=["C", "1uF", "0805"], stock=Stock("coarse", level=0)),
    ]
    eq(_require_handle(_inv, "#9", "修改").seq, 9, "编号 9 定位")
    eq(_raises(lambda: _require_handle(_inv, "#99", "修改"), "不存在的编号").code,
       EXIT_NOTFOUND, "不存在的编号是 NOTFOUND——它和「写法不对」是两回事")

    # --- make_stock 的范围校验 ---
    eq(make_stock(argparse.Namespace(level=2, qty=None)), Stock("coarse", level=2), "--level 2")
    eq(make_stock(argparse.Namespace(level=None, qty=5)), Stock("accurate", count=5), "--qty 5")
    eq(make_stock(argparse.Namespace(level=None, qty=None), default_coarse=True),
       Stock("coarse", level=0), "add 不给存量时默认「无(0)」")
    _raises(lambda: make_stock(argparse.Namespace(level=None, qty=None)),
            "stock 没给存量时不能默默建一个——它和 add 不同")
    for _ns, _label in ((argparse.Namespace(level=5, qty=None), "--level 5 越界"),
                        (argparse.Namespace(level=-1, qty=None), "--level 越下界"),
                        (argparse.Namespace(level=None, qty=-3), "--qty 负数")):
        _raises(lambda n=_ns: make_stock(n), _label)

    # --- 匹配回归：这次改动修的就是这条 ---
    _fixed = Component(id="aaaaaaaa-1111-2222-3333-444444444444", seq=1,
                       tags=["C", "0805", "1uF", "16V"], stock=Stock("coarse", level=0))
    ok(match_token("C", _fixed)[0] > 0, "补上类型后 search C 必须能命中")
    ok(match_token("电容", _fixed)[0] > 0, "补上类型后 search 电容 必须能命中")
    _broken = Component(id="bbbbbbbb-1111-2222-3333-444444444444", seq=2,
                        tags=["1uF", "16v", "0805"], stock=Stock("coarse", level=0))
    ok(match_token("C", _broken)[0] == 0, "没补类型的旧记录搜 C 命中不了——这就是当初的 bug 现场")

    # --- 补类型会激活 hint，把裸数字标签「唤醒」---
    eq(dim_hint_for_tags(["C", "0805", "100"]), "capacitance", "有 C 标签时 hint 是容值")
    eq(dim_hint_for_tags(["0805", "100"]), None, "没有类型标签时 hint 为空")

    # --- 输出 ---
    total = checks[0]
    if failures:
        print(f"自检失败：{len(failures)} / {total} 项未通过\n")
        for f in failures:
            print(f"  [x] {f}")
        return EXIT_ERROR
    print(f"自检通过：{total} 项断言全部成立。")
    return EXIT_OK


# =============================================================================
# 入口
# =============================================================================


def _stream_encoding(stream):
    """流的编码名，规范化后便于比较（utf-8 / UTF8 / cp65001 视为同一个）。"""
    return (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")


def _setup_console_encoding():
    """把 stdout/stderr 切到 UTF-8——**但已经是 UTF-8 的流一律不碰**。

    为什么必须做这件事：这台机器上 Python 在管道和重定向下的默认 stdout 编码是
    gbk，而 **gbk 连 µ (U+00B5) 都编不出来**，会抛 UnicodeEncodeError 让整个命令
    崩掉。这不是「良好实践」，是功能正确性的前提。

    为什么"已经是 UTF-8 就跳过"这条判据同时护住了真控制台：Python 给真 Windows
    控制台（cmd / PowerShell / Windows Terminal）建的流，编码本来就是 utf-8——它把
    字符串按 UTF-8 桥接到 WriteConsoleW，走的是 UTF-16 的控制台接口，**完全不经过
    控制台代码页**。中文在那个流上本来就是对的，动它只会坏。

    **这里曾经有过一个「跟随控制台代码页」的分支，它是错的，已删除。** 当时的写法是
    检测到 isatty 且 GetConsoleOutputCP() 不是 65001 时，把编码设成 cp{代码页}——
    在中文 Windows 上就是 cp936。它基于一个错误的心智模型：以为 Python 会把 UTF-8
    字节丢给控制台、由代码页去解释。实际上 Python 层若先按 cp936 把中文编成 GBK
    字节，桥接层会再按 UTF-8 去解这些字节，于是 cmd 和 PowerShell 里满屏乱码。

    这个错误是从 Git Bash 的管道里观察到 encoding 是 gbk 反推出来的。但管道和真
    控制台是两回事：管道下的 gbk 只说明下游按 GBK 读，**不说明控制台要 GBK**。
    """
    for stream in (sys.stdout, sys.stderr):
        if _stream_encoding(stream) in ("utf8", "cp65001"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError, LookupError):
            # 拿不到可用的 UTF-8 时退而求其次，只改错误策略：个别字符打不出来
            # 就显示 `?`，绝不因为一个字符中断整个命令。
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


def main(argv=None):
    _setup_console_encoding()
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--selftest" in argv:
        return run_selftest()

    argv = normalize_argv(argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    command = getattr(args, "command", None)
    if not command:
        parser.print_help()
        return EXIT_OK

    path = args.file if getattr(args, "file", None) else default_data_path()

    handlers = {
        "search": cmd_search,
        "add": cmd_add,
        "stock": cmd_stock,
        "list": cmd_list,
        "remove": cmd_remove,
    }

    try:
        return handlers[command](args, path)
    except AppError as e:
        print(f"错误：{e}", file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return EXIT_ERROR
    except BrokenPipeError:
        # 输出被 head 之类的命令提前关闭，不是错误。
        try:
            sys.stdout.close()
        except Exception:
            pass
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
