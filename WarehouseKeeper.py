#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""元件库存的交互式入口（REPL）。

这是叠在 inventory.py 之上的一层薄壳，**必须作为脚本运行**（`python WarehouseKeeper.py`），
不要 `import WarehouseKeeper`——脚本所在目录要出现在 sys.path[0] 才能 import 到同目录的
inventory.py，而这只在「作为脚本运行」时成立。

分工：

  inventory.py          核心层（归一化 / 匹配 / 存储）与命令行层
  WarehouseKeeper.py    本文件。交互循环、行解析、追问。业务逻辑一律不复制。

相对命令行的两个增量：一是循环，不必每条命令重新起进程；二是**追问**——缺封装、
缺数量、命中包含关系、删除前二次确认，命令行在这些地方要么报错、要么用选项绕过，
交互场景该问就问。

另一个可选增量是 autosync：写库后在后台线程里自动 sync push，默认关闭，只在交互
模式生效（见「自动同步层」）。它不碰库存业务，只搬运文件。

明确不做的事：全屏 TUI（当前是 Git Bash 伪终端，msvcrt 与 Windows 控制台 API 都读不到按键）；
edit 命令（标签写错了 remove 掉重录即可，这是既有决定）。
"""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

from inventory import (
    AppError,
    EXIT_DATA,
    EXIT_ERROR,
    EXIT_NOTFOUND,
    EXIT_OK,
    EXIT_USAGE,
    PACKAGE_EXAMPLES_LINE,
    PACKAGE_NONE_HINT,
    PACKAGE_PROMPT,
    PACKAGE_QUESTION,
    STOCK_PROMPT,
    STOCK_QUESTION,
    apply_limit,
    canonical_tags,
    classify_tags,
    cmd_add,
    cmd_remove,
    cmd_stock,
    default_data_path,
    format_tag_line,
    inference_note,
    load_inventory,
    make_stock,
    render_components,
    render_containment,
    render_hits,
    resolve_package_answer,
    run_selftest,
    search_components,
    tokenize_query,
    # 下面这个 internal 函数（下划线开头）跨模块导入私有名并不漂亮，但它承载着
    # 必须全项目一致的东西，复制一份等于让两套实现各自演化：
    #   _setup_console_encoding —— 管道和重定向下 stdout 的默认编码是 gbk，而 gbk
    #       编不出 µ (U+00B5)，不处理会抛 UnicodeEncodeError；真控制台则本来就是
    #       utf-8，不该碰。两种情况都归它管，是功能正确性的前提。
    # 改动 inventory.py 时请勿破坏这个签名。
    _setup_console_encoding,
)

# 一页显示多少条。超过就截断并**如实报出总数**。
PAGE = 20

PROMPT = "元件库> "

QUIT_WORDS = frozenset({"quit", "exit", ":q", "q"})

# 每个命令认识的选项。值是一个 (namespace 键, 类型) 元组，类型为 None 表示这是个开关。
#
# 这些规格必须与 inventory.py 里 build_parser 的子命令定义保持一致——我们复用
# cmd_add、cmd_stock、cmd_remove，靠 SimpleNamespace 构造形状相同的假 args。
ADD_SPEC = {"--level": ("level", int), "--qty": ("qty", int), "--note": ("note", str),
            "--force": ("force", None)}
STOCK_SPEC = {"--level": ("level", int), "--qty": ("qty", int)}
SEARCH_SPEC = {
    "-A": ("any", None), "--any": ("any", None),
    "-n": ("limit", int), "--limit": ("limit", int),
    "-a": ("all", None), "--all": ("all", None),
}
LIST_SPEC = {
    "-n": ("limit", int), "--limit": ("limit", int),
    "-a": ("all", None), "--all": ("all", None),
    "--low": ("low", None),
}

# git 子进程的超时（秒）。只给只读查询用：git 卡住（仓库锁文件、坏掉的网络、
# 在等交互输入）会把整个 REPL 挂死，而 `-c "sync"` 是无人值守的，更危险。
# 改动/联网的操作（fetch / push）不设超时——它们本来就可能很慢，而且 GCM 的
# 认证窗口可以等你任意久；那两条路靠 Ctrl+C 兜底。
GIT_TIMEOUT = 30

# 自动同步的节流参数。两条都是「推迟推送」，不是「丢弃改动」——后台线程推的
# 永远是数据文件的当前状态，等待期间攒下的改动会被同一次推送带走。
#   合并窗口：连敲几条命令、或者 add 之后补数量造成的二次写盘，只推一次。
#   冷却：两次推送之间至少隔这么久，高频输入时不至于每条命令都打一次网络。
AUTOSYNC_DEBOUNCE = 1.0
AUTOSYNC_COOLDOWN = 15.0
# 退出时最多等后台线程多久。数据已经在盘上，等不到就留给下次。
AUTOSYNC_EXIT_WAIT = 3.0
AUTOSYNC_CONFIG_VERSION = 1
AUTOSYNC_CONFIG_ENV = "WAREHOUSEKEEPER_CONFIG"
# 后台 push 的上限。手动 push 没超时是因为前台能 Ctrl+C；后台线程里一旦挂住，
# 没人看得见，还会一直攥着 git 操作锁，把之后的手动 sync 也堵死。
AUTOSYNC_PUSH_TIMEOUT = 120.0


HELP_TEXT = """\
命令
  search 查询词...               检索（多个词之间是「全部命中」）
  add    标签...                 添加元件（缺封装会先问你；没给数量的先按「无」入库，随后再问一句；
                                 类型能推断的会自动补；与已有元件重复时会确认）
  stock  #编号 [值]              更改存量（值可以直接写：多 / plenty / 23 / +5）
  list                           列出全部（--low 只看存量偏低的；-a 不分页）
  remove #编号                   删除元件（会二次确认）
  sync [pull|push]               与 GitHub 同步（无参＝只看状态，不动任何东西）
  autosync [on|off]              自动同步：写库后在后台自动 push（默认关闭）
  help                           显示本帮助
  selftest                       跑一遍 inventory.py 的内置自检
  quit                           退出

选项
  --level 0-4     粗略存量：0 无 / 1 极少 / 2 少 / 3 多 / 4 极多
                  （0 就是 0，与 --qty 0 是同一个值；1-4 才是真粗略档）
  --qty N         精确存量个数（与 --level 互斥）
  --note 文本     备注（只有 add 有；会显示在 list 与 search 里）
  --force         与库中元件重复或包含、或类型是弱证据推出来的时，仍然添加，不追问（只有 add 有）
  -A, --any       检索时放宽为「任一命中」，默认是全部命中
  -n N            最多显示 N 条（默认 20；须为正整数）
  -a, --all       不分页，列出全部（等价于不给 -n；与 -n 同时给时 -n 被忽略）
                  注意 -A 与 -a 是大小写不同的两个开关：-A 放宽检索，-a 取消分页

目标怎么写
  #7              永久编号为 7 的元件。编号只增不减，删除后不回收
  stock / remove  只认 #编号。写检索词会报错，先去 search 查到编号再动手

stock 的值怎么写
  多 / 很少 / plenty      粗略档位，词表整套都认：无 极少 少 多 极多，英文 none few some many lots
  level2 / qty23          带前缀的写法，分别是档位与精确个数
  23                      裸数字就是精确 23 个
  无                      就是 0 个（同一个值的两种写法，所以也能直接 +5）
  +5 / -3                 在当前存量上增减，裸 + / - 就是加一减一
  加5 / 用掉2 / add5      同一个意思（进：加增添补进入，出：减用耗去出丢损坏）
  只有 1-4 档的粗略存量不能增减，会报错让你先写成精确数

sync：与 GitHub 同步
  sync           只看状态：仓库、分支、提交、有没有未提交的改动
  sync pull      用远端覆盖本地（破坏性，会先问一次）
  sync push      把当前改动 amend 进那个唯一的提交，再强推上去
  只对「把 inventory.json 纳入 git」的同步仓库有意义；开发仓库把数据 .gitignore
  掉了，在那里 sync 会直接拒绝。pull 是覆盖不是合并——本地有未推送的改动时会被
  硬拦下来，不会静默丢掉。推送被拒会以非 0 退出码失败，并告诉你下一步怎么走。
  别用 VS Code 的 Sync Changes：它会 pull，会在这里造出一个合并提交。

autosync：写库后自动推送
  autosync               看开关、配置文件与后台状态
  autosync on            开启（只在交互模式生效；-c 不会自动推送）
  autosync off           关闭
  开启后 add / stock / remove 写盘会自动 sync push，推送跑在后台线程里，
  不会卡住提示符；结果显示在你敲下一条命令之前。连敲几条只推一次：合并 1 秒，
  两次推送之间至少隔 15 秒。退出时若还有没推上去的改动，会再试一次。
  推送失败不影响那条命令本身——数据已经写盘，只是记为「待同步」，下次重试。
  工作区里有 inventory.json 之外的改动时会跳过自动推送，免得把代码或临时文件
  一起 amend 进那个唯一的提交。

输入技巧
  search "100 nF"        标签里带空格时用引号括起来
  search 电容 -A         任一命中
  list -a                不分页，一次看完（search 同理）
  add R 0603 -- -40~85C  标签以 - 开头且与选项同名时，用 -- 转义
  add C 0805 100nF 多    存量可以直接写在标签里：多 / 很少 / 极少 / qty23
  add C 0805 100nF lots  同上，英文按同一架刻度：none/few/some/many/lots
  stock #7 多            改存量：档位词、裸数字、增减号都直接接在编号后面
  add 之后问数量         元件已经入库了，答什么等于 `stock #编号 什么`，裸数字 20 也认；
                         回车或 Ctrl+C 只是不填，元件不会撤回
  每行一条命令，不支持跨行

退出
  quit（或 q / exit）—— 任何环境都能用，推荐
  Windows 控制台：Ctrl+Z 再回车
  Git Bash：Ctrl+D
  Ctrl+C 不会退出，只取消当前输入或当前操作
  （例外：add 的数量追问处，Ctrl+C 只是跳过不填——那时元件已经入库了）
"""


# =============================================================================
# 解析层
# =============================================================================


def scan_options(words, spec):
    """从词列表里摘出选项，返回 (选项字典, 其余的词)。

    选项可以写在标签前面或后面。以 `-` 开头但不匹配任何已知选项名的词会被当成普通标签，
    所以温度范围 `-40~85C` 这类标签是安全的。`--` 之后的一切都按标签处理。
    """
    opts = {}
    positional = []
    i = 0
    while i < len(words):
        w = words[i]
        if w == "--":
            positional.extend(words[i + 1:])
            break
        if w in spec:
            key, typ = spec[w]
            if typ is None:
                opts[key] = True
                i += 1
                continue
            if i + 1 >= len(words):
                raise AppError(f"选项 {w} 后面缺少值", EXIT_USAGE)
            raw = words[i + 1]
            if typ is int:
                try:
                    opts[key] = int(raw)
                except ValueError:
                    raise AppError(f"选项 {w} 的值应该是整数，实际是 {raw!r}", EXIT_USAGE)
            else:
                opts[key] = raw
            i += 2
            continue
        positional.append(w)
        i += 1
    return opts, positional


def split_line(line):
    """把一行输入切成 (命令名, 其余的词)。

    先用 shlex 严格试一次，再交给 tokenize_query。这一步看着冗余，其实必须：
    tokenize_query 内部对 shlex 的 ValueError 有兜底（退化成朴素 split），
    于是 `add C "100 nF` 这种未闭合的引号会被静默切成 ['C', '"100', 'nF']，
    用户看到的是「标签里莫名多了个引号」而不是「引号没闭合」。

    命令行下遇不到这个坑——shell 会先拦下未闭合的引号。只有 REPL 的整行是
    Python 字符串、没有 shell 参与解析，才会把这种输入送进来。
    """
    try:
        shlex.split(line, posix=False)
    except ValueError as e:
        raise AppError(f"命令解析失败：{e}（引号可能没闭合）", EXIT_USAGE)

    words = tokenize_query(line)
    if not words:
        return "", []
    return words[0].lower(), words[1:]


def check_stock_opts(opts):
    """提前校验 --level / --qty，免得用户答完封装与数量才发现选项是错的。

    判定本身不在这里——make_stock 是唯一的实现，这里只是借它提前跑一遍。
    default_zero 让「两个都没给」照旧放过：add 允许不给存量，由追问兜底。
    """
    make_stock(SimpleNamespace(level=opts.get("level"), qty=opts.get("qty")),
               default_zero=True)


# =============================================================================
# 追问层
# =============================================================================

# 追问的缩进。
#
# 一条命令执行期间，程序第二次向用户要输入时，那个输入点和它的上下文一起缩进两格，
# 视觉上挂在命令下方。缩进覆盖「还在等你回答」的那一段：
#
#   问句（长问句单独占一行）→ 重问的提示 → 输入点
#
# 问题一有答案，缩进就结束——「已添加 #3 …」「已取消。」这类结果行回到第一列。
# 于是「顶格的都是结果、缩进的都是在等你」这条规则不用记，扫一眼就能看出来。
#
# 宽度取 2，和 do_remove 的详情行、包含关系表格对齐：追问的输入点正好落在它上面
# 那组详情下面缩进同一级，读起来是同一个块。
SUB_INDENT = "  "


def sub_ask(prompt):
    """追问的输入点。缩进由这里统一加，调用点不再自己拼空格。"""
    return input(SUB_INDENT + prompt)


def sub_print(text):
    """追问块里的一行输出：问句、重问的提示、非法输入的抱怨。"""
    print(SUB_INDENT + text)


def sub_print_block(text):
    """多行文本整块缩进，空行保持空行。

    有几句报错是带内嵌换行的多行文本——「看不懂存量写法」那张四行写法表、
    「粗略档位没法做相对增减」那两行带例子的。sub_print 只在整段前面加一次缩进，
    内嵌的换行会顶到第一列，正好破坏上面那条「缩进即追问」的规则，看起来像是
    追问已经结束、程序自己在报错。

    多行块里的相对层级要保留：那些报错的子项自带两格缩进，整块再加两格之后
    读起来仍然是「主句 + 缩进的附表」。
    """
    for line in text.splitlines():
        sub_print(line) if line else print()


# =============================================================================
# 定位层
# =============================================================================


def confirm(prompt):
    """是非确认。回车、EOF、Ctrl+C 都视为「否」。"""
    try:
        ans = sub_ask(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes")


# =============================================================================
# 同步层（git）
# =============================================================================
#
# 这一层是本项目**唯一**会启动外部进程的地方。它把两个 .cmd 脚本里那套协议搬
# 进了 Python：单提交模型——整个仓库永远只有一个提交，推送时 amend 覆盖它再
# 强推，pull 则用远端整棵工作树覆盖本地。
#
# 判据是「数据文件有没有被 git 跟踪」，这正好是两个仓库的区别：同步仓库把
# inventory.json 纳入版本控制（它就是要搬运的货物），而开发仓库把它 .gitignore
# 掉。同一条命令因此在两边各有正确行为——一边正常跑，一边明确拒绝。
#
# 两条不可让步的性质，改动这一层时先看它们：
#
#   1. **status 与 push 绝不 fetch。** `git push --force-with-lease` 不带参数时，
#      比的是「远端真实 ref」对「本地 refs/remotes/<remote>/<branch>」。一旦在
#      push 前 fetch，lease 的基线就被刷成远端当前值，远端即使被另一台机器推
#      进过也会「匹配通过」，于是强推静默覆盖对方。只有 sync pull 才 fetch。
#
#   2. **只读查询必须显式指定 UTF-8。** 本机 locale 是 cp936，而 git 的提交信息
#      是 UTF-8；`subprocess.run(..., text=True)` 不指定编码会在 subprocess 的读
#      线程里抛 UnicodeDecodeError，那个异常绕过 except AppError，表现为一整条
#      裸回溯。这是继 `µ`(U+00B5) 编不进 gbk 之后同一类陷阱的第二次出现。


_git_cache = {}


def git_path():
    """git 可执行文件的绝对路径。找不到就报错。

    用绝对路径有两个理由：一是把「git 没装」变成一句干净的 AppError，而不是让
    FileNotFoundError 从某个调用点冒出来；二是 Windows 上 CreateProcess 的搜索
    顺序里**包含当前目录**，用绝对路径可以避免 cwd 里被人放一个 git.exe 顶掉。
    """
    if "path" not in _git_cache:
        found = shutil.which("git")
        if not found:
            raise AppError("找不到 git。sync 需要 git 在 PATH 上。", EXIT_ERROR)
        _git_cache["path"] = found
    return _git_cache["path"]


def git_capture(root, *args):
    """跑一条只读 git 查询，返回 CompletedProcess（输出已解码成 str）。

    只读查询才走这里。会改动仓库或联网的操作走 git_inherit——那条路不捕获输出，
    因为认证提示（GCM 弹的窗）和进度条必须让用户看得见，捕获了就变成「看不见
    提示地挂住」。
    """
    cmd = [git_path(), "-C", str(root), *args]
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT,
            # stdin=DEVNULL 不是洁癖。自动同步在后台线程里调用这些查询，而主线程
            # 这时通常正阻塞在 input() 的管道上；子进程一旦继承那个 stdin，git 会
            # 跟着卡到那一行输入结束——实测一次 rev-parse 从 30 ms 变成好几秒。
            # 只读查询本来就不该读 stdin。
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise AppError(
            f"git 超过 {GIT_TIMEOUT} 秒没有返回，已中止：git {' '.join(args)}",
            EXIT_ERROR,
        )
    except OSError as e:
        # FileNotFoundError 也是 OSError 的子类，一并接住。
        raise AppError(f"启动 git 失败：{e}", EXIT_ERROR)


def git_inherit(root, *args):
    """跑一条会改动仓库或联网的 git 操作，返回它的退出码。

    输出**不捕获**：继承 stdin/stdout/stderr，这样进度条、GCM 的认证提示、报错
    都照常显示。捕获 stderr 会让「正在等你在浏览器里认证」变成一个静默的挂起。

    调用前先 flush 自己的 stdout／stderr——Python 的 stdout 在管道下是块缓冲，
    而 git 写的是同一个 fd、无缓冲，不 flush 会让 git 的输出插到我们上一行说明
    之前，读起来像是顺序错乱。
    """
    sys.stdout.flush()
    sys.stderr.flush()
    cmd = [git_path(), "-C", str(root), *args]
    try:
        return subprocess.run(cmd).returncode
    except OSError as e:
        raise AppError(f"启动 git 失败：{e}", EXIT_ERROR)


def git_text(root, *args):
    """跑一条必须成功的只读查询，返回 stdout（已 strip）。失败抛 AppError。"""
    r = git_capture(root, *args)
    if r.returncode != 0:
        raise AppError(
            f"git {' '.join(args)} 失败：{r.stderr.strip() or r.stdout.strip()}",
            EXIT_ERROR,
        )
    return r.stdout.strip()


def git_ok(root, *args):
    """一条只读查询，只关心成败。"""
    return git_capture(root, *args).returncode == 0


def find_repo_root(start_dir):
    """start_dir 所属的仓库根，不在仓库里就返回 None。"""
    r = git_capture(start_dir, "rev-parse", "--show-toplevel")
    if r.returncode != 0:
        return None
    return Path(r.stdout.strip()).resolve()


def require_syncable(path):
    """确认这个数据文件所在的仓库可以同步，返回 (仓库根, 数据文件相对仓库根的路径)。

    守卫就是「数据文件有没有被 git 跟踪」。不适用时给的是 EXIT_USAGE 而不是
    错误——它表达的是「这条命令在这个仓库里没有意义」，不是「操作失败了」。
    文案里不出现任何具体机器上的路径：这个程序会在两台电脑、好几个目录里跑。
    """
    data = Path(path).resolve()
    root = find_repo_root(data.parent)
    if root is None:
        raise AppError(
            f"sync 需要一个 git 仓库，但 {data.parent} 不在任何仓库里。\n"
            "sync 是把库存数据在两台电脑之间搬运，没有仓库就没有可搬运的东西。",
            EXIT_USAGE,
        )
    try:
        rel = data.relative_to(root).as_posix()
    except ValueError:
        raise AppError(
            "数据文件不在仓库里，sync 无从下手：\n"
            f"  数据文件：{data}\n"
            f"  仓库根：  {root}",
            EXIT_USAGE,
        )
    if not git_ok(root, "ls-files", "--error-unmatch", "--", rel):
        raise AppError(
            "这个仓库没有把库存数据纳入 git，sync 在这里没有意义。\n"
            f"  数据文件：{data}\n"
            f"  仓库根：  {root}\n"
            "sync 只对「把 inventory.json 纳入版本控制」的同步仓库有用；\n"
            "开发仓库把数据 .gitignore 掉了，改代码在那里，搬数据才用 sync。",
            EXIT_USAGE,
        )
    return root, rel


def git_branch(root):
    """当前分支名。detached HEAD 时 git 会返回字面的 HEAD，那种状态没法推拉。"""
    branch = git_text(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise AppError("现在是 detached HEAD，不在任何分支上，sync 无从下手。",
                       EXIT_USAGE)
    return branch


def git_remote(root, branch):
    """分支跟踪的远端名，没配就回落 origin。

    分支名与远端名一律从仓库里推导，不写死 main——这个文件要原样复制到两个
    仓库，而它们的默认分支一个是 master、一个是 main。
    """
    r = git_capture(root, "config", f"branch.{branch}.remote")
    remote = r.stdout.strip() if r.returncode == 0 else ""
    return remote or "origin"


def parse_status(root):
    """跑一次 status，返回 (脏文件列表, ahead, behind)。

    一次调用同时拿到「工作区脏不脏」与「相对上次同步点的 ahead/behind」。
    --untracked-files=no：未跟踪文件（.bak、__pycache__）本来就不在版本控制里，
    reset --hard 也不动它们，不该挡住 pull。quotepath=false：否则中文或特殊字符
    的路径会被 git 转义成 \\346\\226\\207 之类，列出来没法看。

    ahead/behind 相对的是**本地已知的**远端 ref，也就是上次 fetch/push 时的样子。
    这个函数不 fetch（见本层开头的第 1 条），所以调用点的措辞一律说「上次同步点」，
    不说「远端」。
    """
    out = git_text(root, "-c", "core.quotepath=false",
                   "status", "--porcelain=v1", "--branch", "--untracked-files=no")
    dirty = []
    ahead = behind = 0
    for line in out.splitlines():
        if line.startswith("## "):
            # 形状：`## main...origin/main [ahead 1, behind 2]`
            tail = line.partition("[")[2]
            if tail:
                for part in tail.rstrip("]").split(","):
                    part = part.strip()
                    if part.startswith("ahead "):
                        ahead = int(part[len("ahead "):])
                    elif part.startswith("behind "):
                        behind = int(part[len("behind "):])
        elif line:
            dirty.append(line)
    return dirty, ahead, behind


def sync_banner_line(path, auto=False):
    """banner 上那行同步状态；没有可说的就返回 None。

    auto=True 表示自动同步开着：那时「记得 sync push」是有害的提示——它马上就
    会自己推，催用户手动推只会造成误解。

    任何不对劲（不在仓库里、数据没跟踪、git 没装、读盘失败）都返回 None——
    banner 绝不能把 REPL 启动搞崩。

    在开发仓库里这一行根本不出现，零成本、零误导。措辞说「上次同步点」而不是
    「远端」：这里不 fetch，ahead/behind 相对的是本地上次看到的远端。
    """
    try:
        root, _rel = require_syncable(path)
        _branch = git_branch(root)
        dirty, ahead, behind = parse_status(root)
    except Exception:
        return None
    dirty_hint = "" if auto else "（收工记得 sync push）"
    ahead_hint = "" if auto else "（记得 sync push）"
    if dirty:
        return f"同步：有 {len(dirty)} 项未提交改动{dirty_hint}"
    if ahead and behind:
        return f"同步：与上次同步点分叉（领先 {ahead}、落后 {behind}）"
    if ahead:
        return f"同步：本地领先上次同步点 {ahead} 个提交{ahead_hint}"
    if behind:
        return f"同步：本地落后上次同步点 {behind} 个提交（记得 sync pull）"
    return "同步：与上次同步点一致"


# =============================================================================
# 自动同步层
# =============================================================================
#
# 自动同步要同时满足三件事，这一层的结构就是从它们推出来的：
#
#   1. **不阻塞提示符。** git push 是秒级的网络操作，不能挂在 REPL 主线程上。
#      真正推送放在一个守护线程里，主线程只负责置事件和取结果。
#
#   2. **不打扰输入。** 后台线程绝不 print——它写到的行会和用户正在敲的那一行
#      搅在一起，而 Windows 控制台没有可靠的办法把输入行重绘回来。所有消息先
#      攒进 _AUTOSYNC_RESULTS，主循环在下一轮 input() 之前取走打印。
#
#   3. **不推不该推的东西。** sync_push 是 git add -A，整棵工作树一起 amend。
#      自动同步只替用户推库存数据：工作区里还有别的改动时跳过并报警，而不是
#      把正在改的 WarehouseKeeper.py 或临时文件也带进那个唯一的提交。
#
# 配置放在用户配置目录而不是仓库里，理由也和第 3 条同源：仓库里任何没被忽略
# 的文件都会被 git add -A 带走；而 --file 可以把数据文件指到任意仓库，代码没法
# 保证每个仓库都忽略某个固定文件名。放用户目录从根上消掉这类事故。
#
# pending 用「代次」表达（_AUTOSYNC_DONE < _AUTOSYNC_REQUESTED），不用布尔量：
# 推送进行中又来了新写盘时，旧推送的成功不能把新改动误标成已同步。

_AUTOSYNC_LOCK = threading.Lock()
_AUTOSYNC_EVENT = threading.Event()   # 有改动待推
_AUTOSYNC_FLUSH = threading.Event()   # 退出前的最后一次推送，跳过所有节流
_AUTOSYNC_BUSY = threading.Event()    # 线程正在跑 git
_AUTOSYNC_THREAD = None
_AUTOSYNC_PATH = None
_AUTOSYNC_REQUESTED = 0
_AUTOSYNC_DONE = 0
_AUTOSYNC_LAST_END = 0.0
_AUTOSYNC_LAST_FAIL = 0.0      # 上一次失败的时刻
_AUTOSYNC_FAIL_GEN = 0         # 那次失败覆盖到的代次
_AUTOSYNC_RESULTS = []

# 后台推送与手动 sync pull / push 之间的互斥。两边都会动 .git/index，
# 同时跑会撞 index.lock；手动操作宁可等一下，也不要报一个莫名其妙的错。
_GIT_OP_LOCK = threading.Lock()


def autosync_config_path():
    """每台机器一份配置，放在用户配置目录，**不放在仓库里**（理由见本节开头）。

    环境变量覆盖是给测试隔离用的：同一台机器上跑沙盒时不必碰真实配置。
    """
    override = os.environ.get(AUTOSYNC_CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "lnwsWarehouse" / "autosync.json"


def autosync_data_key(path):
    """配置里区分数据文件的键：规范化后的绝对路径。

    normcase 在 Windows 上抹平大小写与斜杠差异，同一条路径的不同写法落到同一个
    键上。默认库与 --file 指的库因此各有各的开关。
    """
    return os.path.normcase(str(Path(path).resolve()))


def _read_autosync_config():
    """返回 (配置, 错误)。文件不存在不是错误——那就是默认的关闭状态。

    读不懂时返回 (None, 错误) 而不是悄悄重建一份：配置坏了说明有人手改过或者
    盘出了问题，静默覆盖会把设置连同证据一起抹掉。
    """
    p = autosync_config_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": AUTOSYNC_CONFIG_VERSION, "data_files": {}}, None
    except OSError as e:
        return None, e
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        return None, e
    if not isinstance(data, dict) or not isinstance(data.get("data_files", {}), dict):
        return None, ValueError("配置结构不对：data_files 应该是一个对象")
    if data.get("version") != AUTOSYNC_CONFIG_VERSION:
        return None, ValueError(f"不认识的配置版本：{data.get('version')!r}")
    data.setdefault("data_files", {})
    return data, None


def autosync_is_enabled(path):
    """开关是否打开。配置读不了按关闭处理；错误由 banner 与 autosync 命令显示。"""
    data, _err = _read_autosync_config()
    if data is None:
        return False
    entry = data["data_files"].get(autosync_data_key(path))
    return bool(isinstance(entry, dict) and entry.get("autosync"))


def _write_autosync_config(data):
    """原子写。配置很小，但写到一半断电留下的坏 JSON 会让下次启动读不懂。"""
    p = autosync_config_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".autosync-", suffix=".tmp")
    except OSError as e:
        raise AppError(f"无法创建自动同步配置：{p}（{e}）", EXIT_ERROR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except OSError as e:
        raise AppError(f"无法写入自动同步配置：{p}（{e}）", EXIT_ERROR)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def set_autosync(path, enabled):
    """切换开关。写盘成功才算生效——存不下来的开关等于没改。"""
    data, err = _read_autosync_config()
    if err is not None:
        raise AppError(
            f"自动同步配置读不了：{autosync_config_path()}（{err}）。\n"
            "修好或删除这个文件后再试。",
            EXIT_ERROR,
        )
    data["data_files"][autosync_data_key(path)] = {"autosync": bool(enabled)}
    _write_autosync_config(data)


def inventory_fingerprint(path):
    """数据文件的内容指纹。

    用内容而不是 mtime/size：文件系统的时间精度不保证，而「数据写盘了却没有
    自动推送」是这一层最不能接受的静默失败。二十来 KB 的文件读两次可以忽略。

    三种结果：("sha256", 摘要) / ("missing", "") / None（读失败，无从判断）。
    """
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return ("missing", "")
    except OSError:
        return None
    return ("sha256", hashlib.sha256(data).hexdigest())


def inventory_changed(before, after):
    """两次指纹都拿得到且不同才算变。读失败时宁可不推，也不误推。"""
    return before is not None and after is not None and before != after


def autosync_dirty_paths(root):
    """会被 git add -A 带走的路径：已跟踪的改动 + 未跟踪且未忽略的新文件。

    这里不能用 parse_status——它对 pull 的脏检查用的是 --untracked-files=no，
    而未跟踪文件一样会被 git add -A 带走。已忽略的 .bak / __pycache__ 不会出现在
    这个列表里，所以它们不会挡住自动同步。
    """
    # 不能用 git_text：它对 stdout 做 strip()，而 porcelain 的第一列是状态字符，
    # 未暂存改动那一行的行首恰好是空格（" M path"）。strip 掉之后按固定偏移取
    # 路径会少一个字符，把 inventory.json 读成 nventory.json——安全闸于是把数据
    # 文件自己当成「别的改动」，永远跳过自动推送。
    r = git_capture(root, "-c", "core.quotepath=false",
                    "status", "--porcelain=v1", "--untracked-files=all")
    if r.returncode != 0:
        raise AppError(
            f"git status 失败：{r.stderr.strip() or r.stdout.strip()}",
            EXIT_ERROR,
        )
    paths = []
    for line in r.stdout.splitlines():
        if not line or line.startswith("## "):
            continue
        path = line[3:]
        if " -> " in path:   # 重命名：git 报的是「旧 -> 新」，取新路径
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    return paths


def _autosync_blockers(path):
    """数据文件之外会挡路的改动。自动同步只替用户推库存数据。"""
    root, rel = require_syncable(path)
    return [p for p in autosync_dirty_paths(root) if p != rel]


def _note_autosync_failure(gen):
    """记下这次尝试失败了。退出时据此判断「刚刚才撞过墙」——不必立刻再撞一次。"""
    global _AUTOSYNC_LAST_FAIL, _AUTOSYNC_FAIL_GEN
    with _AUTOSYNC_LOCK:
        _AUTOSYNC_LAST_FAIL = time.monotonic()
        _AUTOSYNC_FAIL_GEN = gen


def _git_quiet(root, *args, timeout=None):
    """后台线程专用的 git 调用：捕获输出、禁止终端认证提示。

    不能用 git_inherit：它把子进程直接接到终端上，后台线程的输出会和用户的输入
    行搅在一起。也不能让 git 在没人看着的地方弹认证提示——GIT_TERMINAL_PROMPT=0
    让它在没有缓存凭据时立刻失败；用户手动 sync push 一次（前台能看到 GCM 的
    窗口）就把凭据缓存好了。timeout 只给 push 用（见 AUTOSYNC_PUSH_TIMEOUT）：
    add / commit 是本地操作，快到不值得再引入一种失败。
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    cmd = [git_path(), "-C", str(root), *args]
    try:
        # stdin=DEVNULL 与 git_capture 同一个理由：后台线程的 git 不能等主线程
        # 正在 input() 的那根管道。后台这条路也不该有任何交互余地。
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env,
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AppError(
            f"git 超过 {timeout:.0f} 秒没有返回，已中止：git {' '.join(args)}",
            EXIT_ERROR,
        )
    except OSError as e:
        raise AppError(f"启动 git 失败：{e}", EXIT_ERROR)


def _autosync_guidance(path):
    """推送失败之后该做什么。这段文字在后台线程里生成，主线程原样打印。

    绝不能建议「先 sync pull」：此时 amend 已经完成、工作区是干净的，pull 的脏
    检查拦不住它，reset --hard 会把刚录的元件连同本地提交一起覆盖掉。
    """
    lines = [
        "本机数据已经写盘，改动也在本地提交里，没有丢——只是还没上传。",
        "已记为「待同步」，下次写库或退出时会再试一次。",
        "如果原因是另一台机器推过东西（--force-with-lease 被拒）：",
        "  不要直接 sync pull：本机改动已经 amend 进那个唯一的提交，pull 会把它覆盖掉。",
        f"  先把 {Path(path).name} 复制到仓库外留底，再 sync pull，对照留底重新录入，最后 sync push。",
    ]
    try:
        root, rel = require_syncable(path)
        branch = git_branch(root)
        remote = git_remote(root, branch)
    except AppError:
        return lines
    lines.append("想先看差异（不覆盖任何东西）：")
    lines.append(f"  git -C {root} fetch")
    lines.append(f"  git -C {root} diff HEAD {remote}/{branch} -- {rel}")
    return lines


def _autosync_sleep(seconds):
    """可以被退出打断的等待。冷却最长 15 秒，退出时不能陪着它干等。"""
    deadline = time.monotonic() + seconds
    while not _AUTOSYNC_FLUSH.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.05))


def _autosync_worker():
    """后台推送线程。全进程只有这一个，daemon 保证它不会拖住退出。"""
    global _AUTOSYNC_DONE, _AUTOSYNC_LAST_END
    while True:
        _AUTOSYNC_EVENT.wait()
        _AUTOSYNC_EVENT.clear()

        if not _AUTOSYNC_FLUSH.is_set():
            # 合并窗口。这期间进来的写盘由下面那次推送一起带走。
            _autosync_sleep(AUTOSYNC_DEBOUNCE)
            _AUTOSYNC_EVENT.clear()
            wait = _AUTOSYNC_LAST_END + AUTOSYNC_COOLDOWN - time.monotonic()
            if wait > 0:
                _autosync_sleep(wait)
                _AUTOSYNC_EVENT.clear()

        # 代次在这里取，才包含等待期间进来的所有改动。
        _GIT_OP_LOCK.acquire()
        try:
            with _AUTOSYNC_LOCK:
                gen = _AUTOSYNC_REQUESTED
                path = _AUTOSYNC_PATH
                needed = gen > _AUTOSYNC_DONE
            if not needed:
                # 等待期间手动 sync push 已经结清，或者 autosync off 把账追平了。
                # 没必要再推一次空车。
                continue
            _AUTOSYNC_BUSY.set()
            lines = []
            try:
                blockers = _autosync_blockers(path)
                if blockers:
                    name = Path(path).name
                    lines.append(f"自动同步已跳过：工作区里还有 {name} 之外的改动。")
                    lines.append("  自动同步只替你推库存数据；先处理这些改动，或手动 sync push。")
                    lines.extend(f"  {b}" for b in blockers)
                    _note_autosync_failure(gen)
                else:
                    sync_push(path, auto=True,
                              emit=lambda line: lines.append(f"自动同步：{line}"))
                    with _AUTOSYNC_LOCK:
                        _AUTOSYNC_DONE = max(_AUTOSYNC_DONE, gen)
            except AppError as e:
                lines.append(f"自动同步失败：{e}")
                lines.extend(_autosync_guidance(path))
                _note_autosync_failure(gen)
            except Exception as e:
                # 后台线程里抛出去的异常没人接，线程会直接死掉，从此再不同步。
                lines.append(f"自动同步失败（内部错误）：{e}")
                _note_autosync_failure(gen)
            finally:
                _AUTOSYNC_BUSY.clear()
                with _AUTOSYNC_LOCK:
                    # 失败也计入冷却。断网时每条命令都去撞一次网络毫无意义。
                    _AUTOSYNC_LAST_END = time.monotonic()
                    _AUTOSYNC_RESULTS.extend(lines)
        finally:
            _GIT_OP_LOCK.release()


def _ensure_autosync_worker(path):
    global _AUTOSYNC_THREAD, _AUTOSYNC_PATH
    with _AUTOSYNC_LOCK:
        _AUTOSYNC_PATH = path
        if _AUTOSYNC_THREAD is None or not _AUTOSYNC_THREAD.is_alive():
            t = threading.Thread(target=_autosync_worker, name="autosync", daemon=True)
            _AUTOSYNC_THREAD = t
            t.start()


def autosync_request(path):
    """主线程：登记一次写盘。开关关闭时什么都不做。"""
    global _AUTOSYNC_REQUESTED
    try:
        if not autosync_is_enabled(path):
            return
        _ensure_autosync_worker(path)
        with _AUTOSYNC_LOCK:
            _AUTOSYNC_REQUESTED += 1
        _AUTOSYNC_EVENT.set()
    except Exception:
        # 自动同步是附加功能，它自己的任何故障都不许影响 REPL。
        pass


def autosync_pending():
    """还有改动没推上去。代次不相等就说明最后一次请求还没被成功覆盖。"""
    with _AUTOSYNC_LOCK:
        return _AUTOSYNC_DONE < _AUTOSYNC_REQUESTED


def autosync_mark_synced():
    """手动 sync push 成功、或关闭自动同步时调用：把账面追平，免得再推一次空车。"""
    global _AUTOSYNC_DONE
    with _AUTOSYNC_LOCK:
        _AUTOSYNC_DONE = _AUTOSYNC_REQUESTED
    _AUTOSYNC_EVENT.clear()


def autosync_drain():
    """把后台线程攒下的消息取出来打印。只在主线程、提示符之前调用。"""
    with _AUTOSYNC_LOCK:
        lines = list(_AUTOSYNC_RESULTS)
        _AUTOSYNC_RESULTS.clear()
    for line in lines:
        print(line)


def autosync_wait_idle():
    """手动 sync pull / push 之前调用，拿住 git 操作锁。调用方负责释放。

    后台推送可能正在跑，也可能还在合并窗口/冷却里睡觉——后者不占锁，手动操作
    可以直接跑；它睡醒后的那次推送大概率是空车，sync_push 自己会认出来。
    """
    if _AUTOSYNC_BUSY.is_set():
        print("等待后台自动同步完成…")
    _GIT_OP_LOCK.acquire()


def autosync_finish(path):
    """退出前的最后一次机会：绕过节流补推一次，最多等 AUTOSYNC_EXIT_WAIT 秒。

    等不到也不慌：数据在盘上，本地提交也在；下次写库或手动 sync push 会带上。
    """
    try:
        autosync_drain()
        if not autosync_is_enabled(path) or not autosync_pending():
            return
        with _AUTOSYNC_LOCK:
            # 刚失败过、且之后没有新改动，立刻再撞一次只会把同一段长警告打第二遍。
            just_failed = (_AUTOSYNC_FAIL_GEN == _AUTOSYNC_REQUESTED
                           and time.monotonic() - _AUTOSYNC_LAST_FAIL < AUTOSYNC_COOLDOWN)
        if just_failed:
            print("自动同步仍未完成（最近一次尝试刚刚失败）；本机改动仍在，"
                  "下次写库或手动 sync push 会再试。")
            return
        _ensure_autosync_worker(path)
        print("正在完成自动同步（最多等 3 秒）…")
        _AUTOSYNC_FLUSH.set()
        _AUTOSYNC_EVENT.set()
        t = _AUTOSYNC_THREAD
        if t is not None:
            t.join(AUTOSYNC_EXIT_WAIT)
        autosync_drain()
        if autosync_pending():
            print("自动同步还没完成；本机改动仍在，下次写库或手动 sync push 会再试。")
    except Exception:
        pass
    finally:
        _AUTOSYNC_FLUSH.clear()


# =============================================================================
# 命令层
# =============================================================================


def do_search(rest, path):
    opts, words = scan_options(rest, SEARCH_SPEC)
    if not words:
        raise AppError("查询为空。用法：search 查询词...", EXIT_USAGE)

    inv = get_inv(path)
    hits = search_components(inv.components, words, any_mode=opts.get("any", False))
    # apply_limit 排在空结果分支之前：`-n 0` 是用法错误，不该被「反正没东西可显示」
    # 提前吞掉，否则这一条与命令行那边的校验时机就不一致了。
    page, _total, note = apply_limit(hits, opts.get("limit", PAGE), "search",
                                     all=opts.get("all", False))
    if not hits:
        print("没有匹配的元件。")
        print("提示：加 -A 放宽为「任一命中」；用 list 看全部；用 #序号 直接定位。")
        return EXIT_NOTFOUND

    render_hits(page)
    if note:
        print(note)
    return EXIT_OK


# 类型本身有问题时不该接着追问封装——那会变成「先让你答一通、再告诉你类型也不对」。
# 这几类问题一律跳过询问，直接交给 cmd_add 一次报清楚。
_TYPE_PROBLEMS = {"type_conflict", "type_missing", "micro_ambiguous"}


def ensure_package(tags):
    """标签里没有封装就问用户要一个。

    返回 (标签列表, extra_package)，取消时返回 (None, None)。

    第二个值是交给 cmd_add 的「用户已明确指定」通道。表认得的值（`0805`、
    `NO PACKAGE`、`sot 23`）会被规范化后加进标签，然后让 cmd_add 自己识别；
    **表外的值必须走这个通道**，否则 cmd_add 的校验会把它当成「缺封装」报错。
    交互模式是录入表外封装的唯一途径——命令行没有这条通道，只能靠表。
    """
    plan = classify_tags(tags)
    # 这个检查必须排在封装检查**之前**。`0805 100n` 是有封装的，单看封装看不出
    # 任何问题，但它的类型是歧义的——先问一轮、答完才被告知类型也不对，最恼人。
    if any(k in _TYPE_PROBLEMS for k, _ in plan.issues):
        return tags, None
    if plan.package is not None:
        return tags, None

    # 问句与示例只打一次，重问时只重出输入点：两行加起来 150 列宽，答错一次就再糊
    # 一遍更难读，而它们要说的（封装有哪些写法）不会因为上一次答错而变。重问时
    # 旁边那句提示足够提醒。
    sub_print(PACKAGE_QUESTION)
    sub_print(PACKAGE_EXAMPLES_LINE)
    while True:
        try:
            ans = sub_ask(PACKAGE_PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return None, None
        if not ans:
            sub_print(f"封装不能为空。确实没有封装的话，{PACKAGE_NONE_HINT}。")
            continue
        # 回答可能是好几个词（`直插 2.54`），不能整句当一条标签塞进库里：
        # 怎么拆、按什么顺序判，全在 resolve_package_answer 里，规则只有一份。
        return resolve_package_answer(tags, ans)


def confirm_containment(relations, tags):
    """把包含关系摊开，再问要不要照加。

    整块渲染复用 render_containment——它在命令行那边是报错正文，在这里是追问的
    上下文，同一句话不允许出现第二份。本函数只管缩进和追问，和 do_remove 先把
    详情摊开再确认是同一个套路。
    """
    sub_print_block(render_containment(relations, tags))
    if confirm("仍然添加？(y/N) > "):
        return True
    # 取消的结果行顶格：追问一有答案，缩进就结束，结果回到第一列。
    print("已取消。")
    return False


def confirm_inference(plan, tags, stock):
    """类型是弱证据推出来的，写入前把「推成什么、依据是什么、最终存成什么」摊开问一句。

    依据那一句复用 inference_note——它也是写入后回显用的同一份措辞，两处必须
    逐字一致（见那个函数的 docstring）。补齐的证明在这里：用户看到的不只是
    「我猜是 J」，还有这条记录长什么样、存量记多少，然后才决定认不认。

    问句用「按此添加？」而不是「仍然添加？」——后者是包含关系那一问的话，两问
    可能连着出现，问句重了会让人以为同一个问题问了两次。
    """
    sub_print(f"  {inference_note(plan)}")
    sub_print(f"  存成：{format_tag_line(canonical_tags(tags))}   存量: {stock.label()}")
    if confirm("按此添加？(y/N) > "):
        return True
    print("已取消。")
    return False


def ask_stock_after_add(comp, path):
    """元件已经落盘之后补问一句数量。答案直接走 stock 的值语法。

    与它取代的 ensure_stock 有三处根本差别：

      1. **发生在写入之后**。这里没有「取消整次添加」这个选项——回车、EOF、
         Ctrl+C 都只意味着「数量不补了」，元件留在库里、存量停在「无」。
         数量是可选信息，不该有否决整条记录的权力。
      2. **认的是值位置那套写法**（裸数字 23、+5、用掉2…），不是标签那套。
         所以规则不在这儿，在 cmd_stock 里——这里一行解析都不写。
      3. **只在值写得看不懂时重问**。写盘失败、数据文件坏了都不重问：那两种
         重问一万次也不会好，只会把用户困在提示符里。

    只读 comp.seq。不要在这里改 comp.stock——cmd_stock 自己 load_inventory，
    手里是另一份对象，改这一份不会落盘。
    """
    # 同 ensure_package：问句只打一次，重问只重出输入点。
    sub_print(STOCK_QUESTION)
    while True:
        try:
            ans = sub_ask(STOCK_PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            # 必须自己吞 EOF：-c 分支和 inventory.py 的 main 都只捕获 AppError，
            # 让它冒上去就是一个裸回溯——而元件其实已经加好了。
            # input() 的提示串没有换行，补一个，和 confirm 一致。
            print()
            return
        if not ans:
            # 回车跳过。存量不是必需信息，每次 add 都被拦住必答会很烦。
            return
        try:
            # 复用 cmd_stock，这样编号解析、值语法、相对增减、原子保存全都是
            # 命令行那份实现，不会漂移；它回显的「#7  <标签>  存量: 无 → 精确 20」
            # 也正是用户手敲同一条命令会看到的那一行。
            cmd_stock(
                SimpleNamespace(target=f"#{comp.seq}", value=ans,
                                level=None, qty=None),
                path,
            )
            return
        except AppError as e:
            # 缩进打印：这还是追问块的一部分，不是命令的结果。
            sub_print_block(str(e))
            if e.code != EXIT_USAGE:
                # 写盘失败（ERROR）或数据文件坏了（DATA）。cmd_stock 的这些失败
                # 都发生在动盘之前或写盘那一刻，答案已经没救了，重问没有意义。
                return
            # EXIT_USAGE = 「值写得看不懂」或「粗略档不能增减」，改一下就能过。
        except OSError as e:
            # save_inventory 对不可重试的 OSError 是裸抛的。不接就会冒到 repl 的
            # 兜底 except Exception 打一整个回溯——而元件其实已经加好了。
            sub_print_block(f"补数量失败：{e}")
            return


def do_add(rest, path):
    opts, tags = scan_options(rest, ADD_SPEC)
    if not tags:
        raise AppError("至少要有一个标签。用法：add 标签... [--level 0-4 | --qty N] [--note 文本] [--force]",
                       EXIT_USAGE)

    # 这一条必须自己挡。make_stock 自己不检查互斥——它先判 qty 后判 level，
    # 两个都给时 qty 静默胜出、level 被丢掉。命令行下 argparse 的互斥组让这种输入
    # 根本构造不出来，而我们在这里是自己解析参数的，就直接暴露在这个行为下。
    if "level" in opts and "qty" in opts:
        raise AppError("--level 与 --qty 互斥，只能给一个", EXIT_USAGE)
    check_stock_opts(opts)

    tags, provided = ensure_package(tags)
    if tags is None:
        return EXIT_OK  # 用户在询问处取消了，不是错误

    # 数量不在这里问了。以前它排在这一行（写入之前）、答案当标签拼回去，代价是
    # 只能写字面量写法（裸数字会被拒），而且在回答处按 Ctrl+C 会把整条记录丢掉。
    # 现在改由 ask_stock 回调在写盘之后补问，答案走 stock 的值语法。
    #
    # 顺序上的连带好处：类型确认与包含关系确认都排在数量之前了——那两个才是
    # 决定「加不加」的问题，答否时不该先白答一轮数量。
    #
    # 复用 cmd_add，这样标签归一化、包含关系检查、next_seq 递增、原子保存
    # 全都是命令行那份实现，不会漂移。
    #
    # SimpleNamespace 的字段形状对应 build_parser 里 add 子命令的定义。注意 cmd_add
    # 用 `args.level is None` 这样的属性访问而非 getattr，所以 level 和 qty
    # **必须存在**（值是 None 也行），少一个就是运行时 AttributeError。force 则
    # 相反：cmd_add 用 getattr(args, "force", False) 读它，字段缺失会静默退化成
    # False，把 --force 重新变成被自己的追问挡住，所以它也必须在这儿出现。
    return cmd_add(
        SimpleNamespace(
            tags=tags,
            level=opts.get("level"),
            qty=opts.get("qty"),
            note=opts.get("note", ""),
            force=bool(opts.get("force")),
        ),
        path,
        extra_package=provided,
        # 永远传回调；--force 的判定只发生在 cmd_add 里，这一层不分叉。
        confirm=confirm_containment,
        # 同理，又一个只由交互模式传入的确认回调——命令行没有它，弱证据推出来的
        # 类型会直接补上，那边靠回显自证。
        confirm_inference=confirm_inference,
        # 第四个同类通道，但性质不同：它在写入**之后**才被调用，只补问一个可选
        # 字段，不决定加不加。命令行不传，改为打印一行「未指定存量」的提示。
        ask_stock=ask_stock_after_add,
    )


def do_stock(rest, path):
    opts, words = scan_options(rest, STOCK_SPEC)
    if not words:
        raise AppError("stock 需要 #编号，例如：stock #7 plenty", EXIT_USAGE)
    if len(words) > 2:
        raise AppError("stock 最多写两个词：#编号 和值。例如：stock #7 plenty", EXIT_USAGE)

    # 复用 cmd_stock，这样编号解析、三个来源的互斥、档位词表与相对增减、写盘
    # 全都是命令行那份实现，不会漂移。SimpleNamespace 的字段形状对应 build_parser
    # 里 stock 子命令的定义：value 用 getattr 读，字段缺失会静默退化成「没给值」，
    # 所以它必须在这儿出现。
    return cmd_stock(
        SimpleNamespace(
            target=words[0],
            value=words[1] if len(words) == 2 else None,
            level=opts.get("level"),
            qty=opts.get("qty"),
        ),
        path,
    )


def do_list(rest, path):
    opts, _ = scan_options(rest, LIST_SPEC)
    inv = get_inv(path)
    low = bool(opts.get("low"))
    comps = sorted(inv.components, key=lambda c: c.seq)
    if low:
        comps = [c for c in comps if c.stock.is_low()]
    # 和 do_search 一样，校验排在渲染之前：`-n 0` 是用法错误，不该被「反正没东西
    # 可显示」提前吞掉。
    page, _total, note = apply_limit(comps, opts.get("limit", PAGE), "list",
                                     low, opts.get("all", False))

    # 空结果那句话由 render_components 出。原先这里自己判一次、命令行那边一律说
    # 「库存为空」，同一件事有两种说法，且命令行那句在 --low 下是错的。
    render_components(page, low)
    if note:
        print(note)
    return EXIT_OK


def confirm_remove(comp, path):
    """把要删的元件摊开，再问一次。

    无条件二次确认，即便用户输入的是明确的 #7——#序号 保证的是定位无歧义，
    不是意图无误。把完整信息摊开，让用户在按 y 之前看到的和他将删掉的是同一个东西。

    这一整块缩进：它是「确认删除？」这个问题的上下文，和那个输入点属于同一段。
    详情行在字符串里已经自带两格，加上追问的一级正好比它再深一级。
    """
    sub_print("即将删除：")
    sub_print(f"  #{comp.seq}  {format_tag_line(comp.tags)}   存量: {comp.stock.label()}")
    if comp.note:
        sub_print(f"  备注: {comp.note}")
    sub_print(f"（上一版数据在 {Path(path).name}.bak，可以从那里恢复这次删除）")
    if confirm("确认删除？(y/N) > "):
        return True
    # 取消的结果行顶格：追问一有答案，缩进就结束，结果回到第一列。
    print("已取消。")
    return False


def do_remove(rest, path):
    if len(rest) != 1:
        raise AppError("remove 需要且只需要一个 #编号，例如：remove #7", EXIT_USAGE)
    # 问不问由本层决定，删除本体转交 cmd_remove——和 cmd_add 的 confirm 一个套路，
    # 于是「next_seq 不回收」的理由也只有一份。
    return cmd_remove(SimpleNamespace(target=rest[0]), path, confirm=confirm_remove)


def sync_report(path):
    """`sync`：把同步状态摊开，不动任何东西，也不联网。

    标出的 ahead/behind 是相对上次同步点的，不是远端此刻的样子——不 fetch。
    """
    root, _rel = require_syncable(path)
    branch = git_branch(root)
    remote = git_remote(root, branch)
    head = git_text(root, "rev-parse", "--short", "HEAD")
    dirty, ahead, behind = parse_status(root)

    print(f"仓库    {root}")
    print(f"分支    {branch}  →  {remote}/{branch}")
    print(f"提交    {head}")
    if dirty:
        print(f"工作区  有 {len(dirty)} 项未提交的改动：")
        for line in dirty:
            print(f"  {line}")
    else:
        print("工作区  干净")
    if ahead and behind:
        print(f"同步    与上次同步点分叉：本地领先 {ahead}、落后 {behind}")
    elif ahead:
        print(f"同步    本地领先上次同步点 {ahead} 个提交（收工记得 sync push）")
    elif behind:
        print(f"同步    本地落后上次同步点 {behind} 个提交（坐下记得 sync pull）")
    else:
        print("同步    与上次同步点一致")
    try:
        inv = get_inv(path)
    except AppError:
        pass
    else:
        print(f"库存    {len(inv.components)} 条")
    return EXIT_OK


def sync_pull(path):
    """`sync pull`：用远端覆盖本地。破坏性——本地没推过的改动会没有。"""
    root, _rel = require_syncable(path)
    branch = git_branch(root)
    remote = git_remote(root, branch)
    upstream = f"{remote}/{branch}"

    # 脏检查排在 fetch 之前，而且直接拒绝、不发问：有未提交的改动意味着本机录了
    # 元件还没推，reset --hard 会把它们抹掉。旧 sync_pull.cmd 只在泛泛警告之后
    # 就 reset，这里改成硬拦——宁可报错也不静默丢。
    dirty, _ahead, _behind = parse_status(root)
    if dirty:
        raise AppError(
            "本地有未推送的改动，pull 会把它们丢掉（pull 是覆盖，不做合并）：\n"
            + "\n".join(f"  {line}" for line in dirty)
            + "\n\n先 sync push 把它们推上去；确实要丢弃本地改动重来，"
              "就在仓库里手动执行：\n"
            + f"  git -C {root} stash\n"
            + f"  git -C {root} reset --hard {upstream}",
            EXIT_ERROR,
        )

    if git_inherit(root, "fetch", remote) != 0:
        raise AppError(
            f"从 {remote} 取回失败。检查网络，或者看看代理（clash）在不在跑。",
            EXIT_ERROR,
        )

    up = git_capture(root, "rev-parse", upstream)
    if up.returncode != 0:
        raise AppError(f"远端没有 {upstream} 这个分支，没法 pull。", EXIT_ERROR)
    up_sha = up.stdout.strip()
    local = git_capture(root, "rev-parse", "HEAD")
    if local.returncode != 0:
        raise AppError("本地仓库还没有任何提交，没有可覆盖的东西。", EXIT_ERROR)
    local_sha = local.stdout.strip()

    if up_sha == local_sha:
        print(f"两边的提交已经一致，不需要 pull。（{upstream}  {up_sha[:9]}）")
        return EXIT_OK

    # 追问块缩进：这几行是下面那个问题的上下文，和输入点属于同一段。
    sub_print(f"远端 {upstream}  {up_sha[:9]}")
    sub_print(f"本地 {branch}       {local_sha[:9]}")
    sub_print("继续会用远端覆盖本地，本地没推过的改动都会丢。")
    if not confirm("确认 pull？(y/N) > "):
        print("已取消。")
        return EXIT_OK

    if git_inherit(root, "reset", "--hard", upstream) != 0:
        raise AppError(f"reset --hard {upstream} 失败，本地可能还停在原样。",
                       EXIT_ERROR)
    print(f"已覆盖。本地现在是 {up_sha[:9]}。")
    return EXIT_OK


def sync_push(path, auto=False, emit=print):
    """`sync push`：把工作树 amend 进那个唯一的提交，再强推上去。

    auto=True 是后台自动同步走的路：不继承终端，所有输出交给 emit。代价是看不到
    git 的进度和认证提示，所以那条路禁止终端认证（见 _git_quiet）——没缓存凭据时
    快速失败，由调用方提示用户手动推一次，而不是无声地挂住。
    """
    root, _rel = require_syncable(path)
    branch = git_branch(root)
    remote = git_remote(root, branch)
    upstream = f"{remote}/{branch}"

    dirty, _ahead, _behind = parse_status(root)
    head = git_capture(root, "rev-parse", "HEAD")
    up = git_capture(root, "rev-parse", upstream)
    in_sync = (head.returncode == 0 and up.returncode == 0
               and head.stdout.strip() == up.stdout.strip())
    if not dirty and in_sync:
        # 必须走 emit：auto=True 时这个函数跑在后台线程里，直接 print 会把输出
        # 插进用户正在输入的提示符。
        emit(f"没有需要推送的改动，本地与 {upstream} 一致。")
        return EXIT_OK

    # 这里**不 fetch**，见本层的第 1 条：push 前 fetch 会让 --force-with-lease
    # 失去意义，静默覆盖另一台机器推过的提交。
    if auto:
        for args, failure in (
            (("add", "-A"), "git add -A 失败。"),
            (("commit", "--amend", "--no-edit"),
             "git commit --amend 失败。最常见的原因是仓库里还没有任何提交——\n"
             "amend 得有个可改的东西。先手动 git commit 建一个，再来 sync push。"),
        ):
            r = _git_quiet(root, *args)
            if r.returncode != 0:
                detail = r.stderr.strip() or r.stdout.strip()
                raise AppError(f"{failure}\n{detail}" if detail else failure, EXIT_ERROR)
        pushed = _git_quiet(root, "push", "--force-with-lease", "-u", remote, branch,
                            timeout=AUTOSYNC_PUSH_TIMEOUT)
        push_ok = pushed.returncode == 0
        push_detail = pushed.stderr.strip() or pushed.stdout.strip()
    else:
        if git_inherit(root, "add", "-A") != 0:
            raise AppError("git add -A 失败。", EXIT_ERROR)
        if git_inherit(root, "commit", "--amend", "--no-edit") != 0:
            raise AppError(
                "git commit --amend 失败。最常见的原因是仓库里还没有任何提交——\n"
                "amend 得有个可改的东西。先手动 git commit 建一个，再来 sync push。",
                EXIT_ERROR,
            )
        push_ok = git_inherit(root, "push", "--force-with-lease", "-u", remote, branch) == 0
        push_detail = ""

    if not push_ok:
        message = (
            "推送失败。本机改动没有丢：数据已经写进 "
            f"{_rel}，并 amend 进本地提交，工作区是干净的。\n"
            "如果另一台机器推过东西，--force-with-lease 会拒绝覆盖它——远端有本机没见过的提交。\n"
            "不要直接执行 sync pull：pull 会用远端 reset --hard 覆盖本地，刚录的数据会没有。\n"
            "安全的顺序：\n"
            f"  1. 把 {_rel} 复制到仓库外留一份；\n"
            "  2. 执行 sync pull，接收另一台机器的版本；\n"
            "  3. 对照留底，把本机新增或修改的条目用 add / stock 重新录入；\n"
            "  4. 执行 sync push。\n"
            "如果只是网络或认证失败，修好后直接重试 sync push，同样不要先 pull。\n"
            "不要用 git push -f：那会静默抹掉对方的提交，而这份历史里没有可恢复的东西。"
        )
        if push_detail:
            message += f"\n\ngit 的输出：\n{push_detail}"
        raise AppError(message, EXIT_ERROR)
    emit(f"已推送。本机与 {upstream} 同步在 "
         f"{git_text(root, 'rev-parse', '--short', 'HEAD')}。")
    return EXIT_OK


def do_sync(rest, path):
    if len(rest) > 1:
        raise AppError("sync 最多写一个子动作：sync | sync pull | sync push",
                       EXIT_USAGE)
    action = rest[0].lower() if rest else "status"
    if action in ("status", "sync"):
        return sync_report(path)
    if action in ("pull", "push"):
        # 后台自动推送正在跑时，两边会抢 .git/index。手动操作是用户明确要求的，
        # 让它等一会儿，而不是撞出一个 index.lock 的错。
        autosync_wait_idle()
        try:
            code = sync_pull(path) if action == "pull" else sync_push(path)
        finally:
            _GIT_OP_LOCK.release()
        # 手动推成功就等于把账面上的待同步一起结清了，别让后台再推一次空车。
        # pull 不能这么记：用户取消 pull 时它也返回 0。
        if action == "push" and code == EXIT_OK:
            autosync_mark_synced()
        return code
    raise AppError(f"未知的 sync 子动作：{action}（可用：pull / push）", EXIT_USAGE)


def do_autosync(rest, path):
    """查看或切换自动同步。只动配置文件，不碰库存数据。"""
    if len(rest) > 1:
        raise AppError("autosync 最多写一个子动作：on / off", EXIT_USAGE)
    action = rest[0].lower() if rest else "status"
    cfg = autosync_config_path()
    _data, err = _read_autosync_config()

    if action == "status":
        print(f"自动同步    {'开启' if autosync_is_enabled(path) else '关闭（默认）'}")
        print(f"数据文件    {Path(path).resolve()}")
        print(f"配置文件    {cfg}")
        if err is not None:
            print(f"配置错误    {err}")
            print("提示：修好或删除这个文件后再改开关。")
            return EXIT_ERROR
        if autosync_pending():
            print("后台状态    有待同步的改动（下次写库或退出时会重试）")
        elif _AUTOSYNC_BUSY.is_set():
            print("后台状态    正在推送")
        else:
            print("后台状态    空闲")
        if autosync_is_enabled(path):
            print("说明        只对交互模式生效：add / stock / remove 写盘后自动 push，-c 不会触发。")
        else:
            print("说明        用 autosync on 开启。")
        return EXIT_OK

    if action == "on":
        if err is not None:
            raise AppError(
                f"自动同步配置读不了：{cfg}（{err}）。\n修好或删除这个文件后再试。",
                EXIT_ERROR,
            )
        if autosync_is_enabled(path):
            print("自动同步已经开启。")
            return EXIT_OK
        # 预检放在写配置之前：在开发仓库（数据没被 git 跟踪）或 detached HEAD 下
        # 开启，只会得到一个永远不生效的开关。
        try:
            root, _rel = require_syncable(path)
            git_branch(root)
        except AppError as e:
            raise AppError(f"不能开启自动同步：{e}", e.code)
        set_autosync(path, True)
        print("已开启自动同步：add / stock / remove 写盘后会自动 sync push。")
        print("推送在后台线程里跑，不会卡住提示符；连敲几条只推一次，"
              "两次推送之间至少隔 15 秒。")
        print("只对交互模式生效；-c 不会自动推送。"
              f"工作区有 {Path(path).name} 之外的改动时会跳过。")
        return EXIT_OK

    if action == "off":
        if err is not None:
            raise AppError(
                f"自动同步配置读不了：{cfg}（{err}）。\n修好或删除这个文件后再试。",
                EXIT_ERROR,
            )
        set_autosync(path, False)
        autosync_mark_synced()
        print("已关闭自动同步。写库不再自动推送；本机若有未推送的改动，请手动 sync push。")
        return EXIT_OK

    raise AppError(f"未知的 autosync 子动作：{action}（可用：on / off）", EXIT_USAGE)


def do_selftest(rest, path):
    return run_selftest()


def do_help(rest, path):
    print(HELP_TEXT)
    return EXIT_OK


COMMANDS = {
    "search": do_search,
    "add": do_add,
    "stock": do_stock,
    "list": do_list,
    "remove": do_remove,
    "sync": do_sync,
    "autosync": do_autosync,
    "help": do_help,
    "selftest": do_selftest,
}


# =============================================================================
# 会话层
# =============================================================================


def get_inv(path):
    """取库存。每条命令都重新读盘，不做会话级缓存。

    理由不是性能（几 KB 的 JSON，开销不可测量），而是正确性：

    README 明确鼓励手工编辑 inventory.json。如果启动时加载一次、之后一直在内存里改，
    用户在编辑器里改完保存，回到这里做任何一次修改就把它静默抹掉了。另一个终端窗口
    改了库存也一样会被覆盖。

    还有一个更微妙的好处：写入失败时天然回滚。inventory.py 的 save_inventory 在文件
    被别的程序占用时抛 AppError，此时那个已经改脏的对象随函数返回被丢弃，下一条命令
    重新读到的还是磁盘上的旧值——用户看到的错误信息和磁盘状态是一致的。换成常驻内存
    的话，用户会看到新值、磁盘上却还是旧值，直到下一次保存成功才「莫名其妙」生效。
    """
    return load_inventory(path)


def setup_stdin_encoding():
    """只对非 tty 的 stdin 强制 UTF-8。

    真控制台下 stdin 走 _WindowsConsoleIO 的 UTF-16 桥接，中文输入本来就正确，不该碰它。
    但 stdin 被重定向或走管道时（`python WarehouseKeeper.py < cmds.txt`），它是普通文本流、
    编码跟随 locale（本机 cp936），喂一个 UTF-8 保存的命令文件会抛 UnicodeDecodeError——
    而这个异常是从 input() 里抛出来的，不在 EOFError / KeyboardInterrupt 的捕获名单里，
    会让整个 REPL 带着 traceback 崩掉。
    """
    try:
        if not sys.stdin.isatty():
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError, LookupError):
        pass


def print_banner(path):
    p = Path(path).resolve()
    print("元件库存  ·  交互模式")
    try:
        inv = get_inv(path)
    except AppError as e:
        # 文件坏了也照样进 REPL，不退出。每条命令都会重新读盘，用户完全可以在
        # 另一个窗口把它修好再回来继续，不必重启。这与命令行直接以 4 退出不同，
        # 是有意的：命令行是一次性调用，失败就该失败；REPL 是长驻会话，应该尽量存活。
        print(f"  数据：{p}")
        print(f"  警告：现在读不了 —— {e}")
    else:
        low = sum(1 for c in inv.components if c.stock.is_low())
        tail = f"，其中 {low} 条存量偏低" if low else ""
        print(f"  数据：{p}     共 {len(inv.components)} 条{tail}")
        if not inv.components and not p.exists():
            print("  （文件还不存在，第一次 add 会创建它。）")
    data, cfg_err = _read_autosync_config()
    auto_on = False
    if data is not None:
        entry = data["data_files"].get(autosync_data_key(path))
        auto_on = isinstance(entry, dict) and bool(entry.get("autosync"))

    # 同步状态只在「数据被 git 跟踪」的仓库里出现，别处连这一步都不走。
    line = sync_banner_line(path, auto=auto_on)
    if line:
        print(f"  {line}")
    # 自动同步只在这一行有话说时占屏：默认关闭就不提，免得每次启动多一行噪音。
    if cfg_err is not None:
        print(f"  自动同步：配置读不了，按关闭处理（{cfg_err}）")
    elif auto_on:
        print("  自动同步：开启（改库后后台推送）")
    print("  输入 help 看命令，quit 退出。")
    print()


def advise(code, path):
    """按退出码给出下一步建议。

    同一个常量在命令行里是给 shell 判断用的，在这里变成「该提示用户什么」的分派依据。
    """
    if code == EXIT_USAGE:
        print("提示：输入 help 查看命令用法。")
    elif code == EXIT_DATA:
        print(f"提示：数据文件格式有问题（{path}）；上一版在 {Path(path).name}.bak。")


def dispatch_verb(verb, rest, path):
    """执行已经切好的命令，返回退出码。

    REPL 用这个版本：它本来就要知道 verb 才能跳过 sync 自己的自动推送，再回
    去 split_line 一次等于把同一行解析两遍。
    """
    if not verb:
        return EXIT_OK
    handler = COMMANDS.get(verb)
    if handler is None:
        raise AppError(f"未知命令：{verb}（要检索可以直接用 search {verb}）", EXIT_USAGE)
    return handler(rest, path)


def dispatch(line, path):
    """执行一行命令，返回退出码。`-c` 与命令行入口用这个签名。"""
    verb, rest = split_line(line)
    return dispatch_verb(verb, rest, path)


def repl(path):
    setup_stdin_encoding()
    print_banner(path)

    last_code = EXIT_OK
    while True:
        # 后台推送的结果在这里落地：此刻用户还没开始敲下一行，打印不会弄乱提示符。
        # 后台线程绝不自己 print，原因见自动同步层的第 2 条。
        autosync_drain()

        try:
            raw = input(PROMPT)
        except EOFError:
            autosync_finish(path)
            print("\n再见。")
            return last_code
        except KeyboardInterrupt:
            # 主提示符处的 Ctrl+C 不退出，只放弃当前这一行。Python 自己的 REPL、
            # bash、psql 都是这个行为——在 REPL 里让一次误按丢掉整个会话代价太大。
            # 代价是可能让人困惑「怎么退不出去」，所以直接把退出方式写在这里。
            print("\n^C（要退出请输入 quit）")
            continue
        except UnicodeDecodeError as e:
            print(f"\n输入解码失败：{e}")
            last_code = EXIT_ERROR
            continue

        if not raw.strip():
            continue
        if raw.strip().lower() in QUIT_WORDS:
            # 退出前把还没推上去的改动补推一次（最多等 AUTOSYNC_EXIT_WAIT 秒）。
            autosync_finish(path)
            # 前面补一个换行，不然「再见。」会挤在提示符后面（EOF 那条路的
            # print 本来就带 \n，两边对齐）。
            print("\n再见。")
            return last_code

        # 指纹在解析之前取：命令无论成功、失败还是被取消，都能如实反映它有没有
        # 动过盘。verb 同时用于跳过 sync 自己——sync pull 会改文件，但刚 pull
        # 完立刻 push 正是「覆盖不是合并」里最不该发生的事。
        verb = None
        before = inventory_fingerprint(path)
        try:
            verb, rest = split_line(raw)
            last_code = dispatch_verb(verb, rest, path)
        except AppError as e:
            print(f"错误：{e}", file=sys.stderr)
            last_code = e.code
            advise(e.code, path)
        except KeyboardInterrupt:
            # KeyboardInterrupt 继承自 BaseException 而不是 Exception，
            # 不会被下面的 except Exception 顺带捕获，必须单列。
            print("\n已取消。")
            last_code = EXIT_ERROR
        except BrokenPipeError:
            # 输出被下游（比如 | head）提前关掉，不是错误。
            try:
                sys.stdout.close()
            except Exception:
                pass
            return EXIT_OK
        except Exception:
            # 兜底捕获，但不吞掉：打印完整回溯。否则 search_components 里任何一个
            # 边界情况炸出来的 KeyError 都永远修不掉。
            print("内部错误（这是程序缺陷，请把下面的回溯报告出来）：", file=sys.stderr)
            traceback.print_exc()
            last_code = EXIT_ERROR

        after = inventory_fingerprint(path)
        if verb and verb != "sync" and inventory_changed(before, after):
            # autosync_request 自己吞掉全部异常：同步是附加功能，不能反过来把
            # REPL 弄坏，也不能改变刚才那条命令的退出码。
            autosync_request(path)

        # 每条命令之后空一行，把这次的输出和下一个提示符隔开。
        # 放在 try 外面，成功和失败都走到——出错时的文字同样需要呼吸空间。
        print()


# =============================================================================
# 入口
# =============================================================================


def build_top_parser():
    p = argparse.ArgumentParser(
        prog="WarehouseKeeper.py",
        description="元件库存的交互式入口（REPL）。核心逻辑来自同目录的 inventory.py。",
    )
    p.add_argument("--file", "-f", default=None, metavar="PATH",
                   help="数据文件路径（默认是 inventory.py 同目录的 inventory.json，"
                        "也可以用环境变量 COMPONENT_INVENTORY_FILE 覆盖）")
    p.add_argument("-c", "--command", default=None, metavar="CMD",
                   help="不进入交互，执行一条命令后退出（调试和自动化用）")
    return p


def main(argv=None):
    # 必须在任何 print 之前，包括提示符——input() 的提示串也是写到 stdout 的。
    _setup_console_encoding()

    args = build_top_parser().parse_args(sys.argv[1:] if argv is None else argv)
    path = Path(args.file) if args.file else default_data_path()

    if args.command:
        try:
            return dispatch(args.command, path)
        except AppError as e:
            print(f"错误：{e}", file=sys.stderr)
            return e.code

    try:
        return repl(path)
    except KeyboardInterrupt:
        print()
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
