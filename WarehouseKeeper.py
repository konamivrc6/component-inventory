#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""元件库存的交互式入口（REPL）。

这是叠在 inventory.py 之上的一层薄壳，**必须作为脚本运行**（`python WarehouseKeeper.py`），
不要 `import WarehouseKeeper`——脚本所在目录要出现在 sys.path[0] 才能 import 到同目录的
inventory.py，而这只在「作为脚本运行」时成立。

分工：

  inventory.py          核心层（归一化 / 匹配 / 存储）与命令行层
  WarehouseKeeper.py    本文件。交互循环、行解析、多命中选择。业务逻辑一律不复制。

相对命令行的两个增量：一是循环，不必每条命令重新起进程；二是**多命中时列出候选让人挑**，
而命令行在多命中时直接报错（那是为脚本安全考虑，交互场景下不该迁就它）。

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
    Stock,
    canon_package,
    classify_tags,
    cmd_add,
    cmd_show,
    default_data_path,
    display_width,
    extract_stock_tags,
    load_inventory,
    pad,
    render_components,
    render_hits,
    resolve_target,
    run_selftest,
    save_inventory,
    search_components,
    tokenize_query,
    # 下面两个是 inventory.py 的内部函数（下划线开头），跨模块导入私有名并不漂亮，
    # 但它们各自承载着必须全项目一致的东西，复制一份等于让两套实现各自演化：
    #   _setup_console_encoding —— 管道和重定向下 stdout 的默认编码是 gbk，而 gbk
    #       编不出 µ (U+00B5)，不处理会抛 UnicodeEncodeError；真控制台则本来就是
    #       utf-8，不该碰。两种情况都归它管，是功能正确性的前提。
    #   _now —— 时间戳格式，要和 CLI 写出来的记录保持一致。
    # 改动 inventory.py 时请勿破坏这两个签名。
    _now,
    _setup_console_encoding,
)

# 一页显示多少条。超过就截断并**如实报出总数**。
PAGE = 20

PROMPT = "元件库> "

QUIT_WORDS = frozenset({"quit", "exit", ":q", "q"})

# 每个命令认识的选项。值是一个 (namespace 键, 类型) 元组，类型为 None 表示这是个开关。
#
# 这些规格必须与 inventory.py 里 build_parser 的子命令定义保持一致——我们复用 cmd_add
# 和 cmd_show，靠 SimpleNamespace 构造出形状相同的假 args。
ADD_SPEC = {"--level": ("level", int), "--qty": ("qty", int), "--note": ("note", str)}
STOCK_SPEC = {"--level": ("level", int), "--qty": ("qty", int)}
SEARCH_SPEC = {
    "-A": ("any", None), "--any": ("any", None),
    "-n": ("limit", int), "--limit": ("limit", int),
}
LIST_SPEC = {
    "-n": ("limit", int), "--limit": ("limit", int),
    "--low": ("low", None),
}


HELP_TEXT = """\
命令
  search 查询词...               检索（多个词之间是「全部命中」）
  add    标签...                 添加元件（缺封装或数量时会问你；类型能推断的会自动补）
  stock  目标                    更改存量（需要 --level 或 --qty）
  list                           列出全部（--low 只看存量偏低的）
  show   目标                    查看详情
  remove 目标                    删除元件（会二次确认）
  help                           显示本帮助
  selftest                       跑一遍 inventory.py 的内置自检
  quit                           退出

选项
  --level 0-4     粗略存量：0 无 / 1 极少 / 2 少 / 3 多 / 4 极多
  --qty N         精确存量个数（与 --level 互斥）
  --note 文本     备注（只有 add 有）
  -A, --any       检索时放宽为「任一命中」，默认是全部命中
  -n N            最多显示 N 条（默认 20）

目标怎么写
  #7              永久编号为 7 的元件。编号只增不减，删除后不回收
  51R / 0.1uF     任意检索词，支持模糊识别：0.1uF ≡ 100nF、51R ≡ 51Ω、电容 ≡ C
  a1b2c3          id 前缀
  匹配到多个时会列出候选让你挑，不会像命令行那样直接报错

候选列表怎么读
  [2]  #7  ...    [2] 是本次列表里的位置，#7 是元件的永久编号
  选择时输入 2 表示列表第 2 项，输入 #7 表示永久编号为 7 的那个元件

输入技巧
  search "100 nF"        标签里带空格时用引号括起来
  search 电容 -A         任一命中
  add R 0603 -- -40~85C  标签以 - 开头且与选项同名时，用 -- 转义
  add C 0805 100nF 多    存量可以直接写在标签里：多 / 很少 / 极少 / qty23
  add C 0805 100nF lots  同上，英文按同一架刻度：none/few/some/many/lots
  每行一条命令，不支持跨行

退出
  quit（或 q / exit）—— 任何环境都能用，推荐
  Windows 控制台：Ctrl+Z 再回车
  Git Bash：Ctrl+D
  Ctrl+C 不会退出，只取消当前输入或当前操作
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
    """校验 --level / --qty 的取值。

    互斥检查**不在这里**，因为它是各命令自己的事（add 允许两个都不给，stock 不允许）。
    """
    level = opts.get("level")
    if level is not None and not (0 <= level <= 4):
        raise AppError(f"--level 应该在 0-4 之间，实际是 {level}", EXIT_USAGE)
    qty = opts.get("qty")
    if qty is not None and qty < 0:
        raise AppError(f"--qty 不能是负数，实际是 {qty}", EXIT_USAGE)


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
# 宽度取 2，和 render_candidates 的候选列表、do_remove 的详情行对齐：追问的输入点
# 正好落在它上面那组候选/详情下面缩进同一级，读起来是同一个块。
SUB_INDENT = "  "


def sub_ask(prompt):
    """追问的输入点。缩进由这里统一加，调用点不再自己拼空格。"""
    return input(SUB_INDENT + prompt)


def sub_print(text):
    """追问块里的一行输出：问句、重问的提示、非法输入的抱怨。"""
    print(SUB_INDENT + text)


# =============================================================================
# 定位层
# =============================================================================


def render_candidates(comps):
    """渲染候选列表。

    自己写而不用 render_components，是因为后者会把「共 N 条」打成它收到的列表长度，
    而这里要显示的是「匹配到 M 个，只展示前 20 个」——两个数不一样。
    """
    rows = [
        (f"[{i}]", f"#{c.seq}", " ".join(c.tags), f"存量: {c.stock.label()}")
        for i, c in enumerate(comps, 1)
    ]
    w0 = max(display_width(r[0]) for r in rows)
    w1 = max(display_width(r[1]) for r in rows)
    w2 = min(max(display_width(r[2]) for r in rows), 60)
    for r in rows:
        print(f"  {pad(r[0], w0)} {pad(r[1], w1)} {pad(r[2], w2)} {r[3]}".rstrip())


def pick_one(inv, words, verb):
    """定位到唯一一个元件。多命中时列候选让用户挑，取消则返回 None。

    `[i]` 与 `#seq` 两个编号都会被显示，因为都从 1 开始、含义完全不同：
    前者是本次列表里的位置（只在本屏有效），后者是元件的永久编号。
    """
    found = resolve_target(inv, words)
    if not found:
        print(f"没有匹配的元件：{' '.join(words)}")
        print("提示：用 list 看全部；检索时加 -A 放宽为「任一命中」；或用 #序号 直接定位。")
        return None
    if len(found) == 1:
        return found[0]

    shown = found[:PAGE]
    print(f"匹配到 {len(found)} 个元件：")
    render_candidates(shown)
    if len(found) > PAGE:
        print(f"（只显示前 {PAGE} 个；可以用 #序号 直接指定，或把条件写得更精确）")

    while True:
        try:
            ans = sub_ask(f"选择 [1-{len(shown)}] / #序号 / 回车取消 > ").strip()
        except (EOFError, KeyboardInterrupt):
            # 必须在这里自己捕获。让它冒泡到主循环的话，一次 Ctrl+C 会变成
            # 「退出整个程序」而不是「取消这次选择」。
            print("\n已取消。")
            return None

        if ans == "" or ans.lower() == "q":
            # 回车是取消而不是重新显示列表：改存量、删元件都有副作用，
            # 默认动作必须是无害的。
            print("已取消。")
            return None

        if ans.startswith("#") and ans[1:].isdigit():
            seq = int(ans[1:])
            hit = next((c for c in found if c.seq == seq), None)
            if hit is not None:
                return hit
            sub_print(f"本次匹配里没有 #{seq} 的元件。")
            continue

        if ans.isdigit():
            i = int(ans)
            if 1 <= i <= len(shown):
                return shown[i - 1]
            sub_print(f"请输入 1-{len(shown)} 之间的数字，或回车取消。")
            continue

        # 非法输入只重新问，不取消——手指打滑不该让人把整条命令重敲一遍。
        sub_print("无法识别，请输入列表编号、#序号，或回车取消。")


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
    limit = opts.get("limit", PAGE)
    if limit <= 0:
        raise AppError(f"-n 应该是正整数，实际是 {limit}", EXIT_USAGE)

    inv = get_inv(path)
    # 先拿全集再自己切片。若把 limit 直接传给 search_components，它会在返回前切掉，
    # 而 render_hits 打印的「共 N 条」数的是它收到的长度——那会变成
    # 「实际匹配 200 条，却告诉你共 20 条」，是主动误导。
    hits = search_components(inv.components, words, any_mode=opts.get("any", False))
    if not hits:
        print("没有匹配的元件。")
        print("提示：加 -A 放宽为「任一命中」；用 list 看全部；用 #序号 直接定位。")
        return EXIT_NOTFOUND

    render_hits(hits[:limit], words)
    if len(hits) > limit:
        # 措辞上刻意避开「共」字：render_hits 上面那行「共 N 条」数的是它显示了多少条，
        # 这里要说的是实际匹配了多少条。两个数字含义不同，用同一个字会看混。
        print(f"（实际匹配 {len(hits)} 条，这里只列前 {limit} 条；"
              f"用 search ... -n {len(hits)} 看全部）")
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
        canon = canon_package(ans)
        if canon is not None:
            return tags + [canon], None
        return tags + [ans], ans


def ensure_stock(tags, opts):
    """没有存量信息时问一句。返回 (要追加的存量标签或 None, 是否被取消)。

    「没有存量信息」= 既没给 --level / --qty，标签里也没有 qty23 / level1 /
    中文等级词 / 英文等级词。判断输入是否合法复用 extract_stock_tags，
    不另写一套正则——解析规则只有一份，就在 inventory.py 里。
    """
    if "level" in opts or "qty" in opts:
        return None, False
    if extract_stock_tags(tags)[1] is not None:
        return None, False
    # 与 ensure_package 同样的道理：类型本身说不通时就别问了，直接交给 cmd_add
    # 把问题一次报清楚。存量是最后一问，没必要让用户答完才看到类型错了。
    if any(k in _TYPE_PROBLEMS for k, _ in classify_tags(tags).issues):
        return None, False

    # 同 ensure_package：问句只打一次，重问只重出输入点。
    sub_print(STOCK_QUESTION)
    while True:
        try:
            ans = sub_ask(STOCK_PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return None, True
        if not ans:
            # 回车跳过。存量不是必需信息，每次 add 都被拦住必答会很烦。
            return None, False
        _, stock, issues = extract_stock_tags([ans])
        if issues:
            sub_print(issues[0][1])
            continue
        if stock is None:
            sub_print("看不懂。请写 qty23（23 个）、level2（等级），"
                      "或 无 / 极少 / 少 / 多 / 极多。"
                      "英文 none / few / some / many / lots 也可以。回车跳过。")
            continue
        return ans, False


def do_add(rest, path):
    opts, tags = scan_options(rest, ADD_SPEC)
    if not tags:
        raise AppError("至少要有一个标签。用法：add 标签... [--level 0-4 | --qty N] [--note 文本]",
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

    extra, cancelled = ensure_stock(tags, opts)
    if cancelled:
        return EXIT_OK
    if extra:
        # 只是把答案拼回标签，解析交给 cmd_add——规则只有一份。
        tags = tags + [extra]

    # 复用 cmd_add，这样标签归一化、未指定存量的提示、重复录入的相似度提醒、
    # next_seq 递增、原子保存全都是命令行那份实现，不会漂移。
    #
    # SimpleNamespace 的字段形状对应 build_parser 里 add 子命令的定义。注意 cmd_add
    # 用 `args.level is None` 这样的属性访问而非 getattr，所以 level 和 qty
    # **必须存在**（值是 None 也行），少一个就是运行时 AttributeError。
    return cmd_add(
        SimpleNamespace(
            tags=tags,
            level=opts.get("level"),
            qty=opts.get("qty"),
            note=opts.get("note", ""),
        ),
        path,
        extra_package=provided,
    )


def do_stock(rest, path):
    opts, words = scan_options(rest, STOCK_SPEC)

    # 校验全部放在 pick_one 之前。否则用户先费劲看完一屏候选、选完，
    # 才被告知「忘了写 --level」——那是最差的交互顺序。
    if "level" in opts and "qty" in opts:
        raise AppError("--level 与 --qty 互斥，只能给一个", EXIT_USAGE)
    if "level" not in opts and "qty" not in opts:
        raise AppError("stock 需要 --level 0-4 或 --qty N", EXIT_USAGE)
    check_stock_opts(opts)
    if not words:
        raise AppError("stock 需要目标，例如：stock 51R --level 2", EXIT_USAGE)

    inv = get_inv(path)
    comp = pick_one(inv, words, "修改")
    if comp is None:
        return EXIT_OK

    old = comp.stock.label()
    # 整个 stock 对象被替换，而不是改字段——两种模式在结构上就不可能共存。
    if "qty" in opts:
        comp.stock = Stock("accurate", count=opts["qty"])
    else:
        comp.stock = Stock("coarse", level=opts["level"])
    comp.updated_at = _now()
    save_inventory(inv)
    print(f"#{comp.seq}  {' '.join(comp.tags)}   存量: {old} → {comp.stock.label()}")
    return EXIT_OK


def do_list(rest, path):
    opts, _ = scan_options(rest, LIST_SPEC)
    limit = opts.get("limit", PAGE)
    if limit <= 0:
        raise AppError(f"-n 应该是正整数，实际是 {limit}", EXIT_USAGE)

    inv = get_inv(path)
    comps = sorted(inv.components, key=lambda c: c.seq)
    if opts.get("low"):
        comps = [c for c in comps if c.stock.is_low()]

    if not comps:
        print("没有存量偏低的元件。" if opts.get("low") else "库存为空。")
        return EXIT_OK

    total = len(comps)
    render_components(comps[:limit])
    if total > limit:
        print(f"（库里实际 {total} 条，这里只列前 {limit} 条；用 list -n {total} 看全部）")
    return EXIT_OK


def do_show(rest, path):
    if not rest:
        raise AppError("show 需要目标，例如：show #7", EXIT_USAGE)
    # 查看是只读操作，命中多条就全列出来，不强迫用户先选一个。
    return cmd_show(SimpleNamespace(target=rest), path)


def do_remove(rest, path):
    if not rest:
        raise AppError("remove 需要目标，例如：remove #7", EXIT_USAGE)

    inv = get_inv(path)
    comp = pick_one(inv, rest, "删除")
    if comp is None:
        return EXIT_OK

    # 无条件二次确认，即便用户输入的是明确的 #7——#序号 保证的是定位无歧义，
    # 不是意图无误。把完整信息摊开，让用户在按 y 之前看到的和他将删掉的是同一个东西。
    #
    # 这一整块缩进：它是「确认删除？」这个问题的上下文，和那个输入点属于同一段。
    # 详情行在字符串里已经自带两格，加上追问的一级正好比它再深一级。
    sub_print("即将删除：")
    sub_print(f"  #{comp.seq}  {' '.join(comp.tags)}   存量: {comp.stock.label()}")
    if comp.note:
        sub_print(f"  备注: {comp.note}")
    sub_print(f"（上一版数据在 {Path(path).name}.bak，可以从那里恢复这次删除）")
    if not confirm("确认删除？(y/N) > "):
        print("已取消。")
        return EXIT_OK

    inv.components.remove(comp)
    save_inventory(inv)
    print(f"已删除 #{comp.seq}  {' '.join(comp.tags)}")
    return EXIT_OK


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
    "show": do_show,
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

    还有一个更微妙的好处：写入失败时天然回滚。save_inventory 在文件被别的程序占用时
    抛 AppError，此时那个已经改脏的对象随函数返回被丢弃，下一条命令重新读到的还是
    磁盘上的旧值——用户看到的错误信息和磁盘状态是一致的。换成常驻内存的话，用户会
    看到新值、磁盘上却还是旧值，直到下一次保存成功才「莫名其妙」生效。
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
    if code == EXIT_NOTFOUND:
        print("提示：用 list 看全部；检索时加 -A 放宽为「任一命中」；或用 #序号 直接定位。")
    elif code == EXIT_USAGE:
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
