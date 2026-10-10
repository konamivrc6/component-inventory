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

明确不做的事：全屏 TUI（当前是 Git Bash 伪终端，msvcrt 与 Windows 控制台 API 都读不到按键）；
edit 命令（标签写错了 remove 掉重录即可，这是既有决定）。
"""

import argparse
import shlex
import sys
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


HELP_TEXT = """\
命令
  search 查询词...               检索（多个词之间是「全部命中」）
  add    标签...                 添加元件（缺封装会先问你；没给数量的先按「无」入库，随后再问一句；
                                 类型能推断的会自动补；与已有元件重复时会确认）
  stock  #编号 [值]              更改存量（值可以直接写：多 / plenty / 23 / +5）
  list                           列出全部（--low 只看存量偏低的；-a 不分页）
  remove #编号                   删除元件（会二次确认）
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

    # 问句只打一次，重问时只重出输入点：那句话 80 列宽，答错一次就再糊一整行更难读，
    # 而它要说的（封装有哪些写法）不会因为上一次答错而变。重问时旁边那句提示足够提醒。
    sub_print(PACKAGE_QUESTION)
    while True:
        try:
            ans = sub_ask(PACKAGE_PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return None, None
        if not ans:
            sub_print("封装不能为空。确实没有封装的话，输入 NO PACKAGE。")
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


def dispatch(line, path):
    """执行一行命令，返回退出码。"""
    verb, rest = split_line(line)
    if not verb:
        return EXIT_OK
    handler = COMMANDS.get(verb)
    if handler is None:
        raise AppError(f"未知命令：{verb}（要检索可以直接用 search {verb}）", EXIT_USAGE)
    return handler(rest, path)


def repl(path):
    setup_stdin_encoding()
    print_banner(path)

    last_code = EXIT_OK
    while True:
        try:
            raw = input(PROMPT)
        except EOFError:
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
            # 前面补一个换行，不然「再见。」会挤在提示符后面（EOF 那条路的
            # print 本来就带 \n，两边对齐）。
            print("\n再见。")
            return last_code

        try:
            last_code = dispatch(raw, path)
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
