# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 这个项目

电子元件库存管理 CLI。**纯标准库、无第三方依赖、无构建步骤、无 lint / formatter 配置**，改完直接跑。Python 3.14.6。

`README.md`（870 行）是**权威规格**，中文写成，从命令用法到模糊识别的每一条规则都记了。本文件只写 README 里没有、或者要读好几个文件才能拼出来的东西。两者冲突时以实际行为为准，并把 README 一起改掉——这个仓库的 README 是活文档，用户自己一直在做代码与文档的同步演进，不要留下「文档说的和代码做的不一样」的状态。

## 数据文件

`inventory.json` 在脚本同目录，被 `.gitignore` 排除（git 里没有副本），**是纯测试副本、随便动**。回滚只能靠写入时自动生成的 `inventory.json.bak`。换路径用 `--file` / `-f` 或环境变量 `COMPONENT_INVENTORY_FILE`。

试验一律 `-f` 指向临时副本（好清理、好重来）。**成功路径就是写盘路径**——设计复现用例时要挑**确定会失败**的输入，否则你以为在验报错，其实是在写盘。踩过一个路径坑：**Git Bash 的 `$(mktemp -d)` 给出 `/tmp/...`，Windows 的 Python 看不见那个路径**，要用 `python -c "import tempfile;print(tempfile.gettempdir())"` 拿 Windows 侧的临时目录。

## 命令

```bash
python inventory.py --selftest          # 唯一的测试，见下
python inventory.py search 51R --json
python inventory.py add C 0805 100nF 50V --level 3
python inventory.py list --low
python WarehouseKeeper.py               # 交互式 REPL
python WarehouseKeeper.py -c "search 0.1uF"   # 不进入循环，退出码就是那条命令的结果
```

**没有单测可以挑。** `run_selftest()` 是一个整体函数、902 项断言（`ok` / `eq` / `close` / `same` / `val` / `dim` / `_raises` 这些局部断言助手），没有过滤参数。要单独验一条规则，直接 import 模块调函数——注意 **`python -I` 会失败**，因为 `sys.path[0]` 得是仓库根目录：

```bash
cd T:/component-inventory && python -c "import inventory as inv; print(inv.canon_unit('51r'))"
```

`WarehouseKeeper.py` 要**作为脚本跑**（`python WarehouseKeeper.py`）。它靠「脚本所在目录出现在 `sys.path[0]`」来 import 同目录的 `inventory.py`，所以只有作为脚本运行时这个前提才无条件成立；在仓库根目录下 `import WarehouseKeeper` 碰巧也能通，但**工作目录一换就断**，而它的 docstring 也明确要求不要 import 它。

自检不是装饰。归一化的规则密度极高且全是边界情况，出错方式是**静默错**：不报错，只是匹配不到或匹配错。改 `parse_quantity` / `classify_tags` / `canon_unit` / `canon_package` 之后必须跑一遍，并且**为新规则补断言**（自检内按 `# --- 分组名 ---` 分节，找到对应那节就近加）。

退出码：`0` 成功 · `1` IO · `2` 用法错误/歧义未消解 · `3` 未找到 · `4` 数据文件缺失或非法。**`-n` 不改变任何退出码**，`3` 始终只表示「库里真的没有」。

## 架构

`inventory.py` 单文件 4188 行，内部按**六层严格分层、依赖方向单向**，每层有 `# 第 N 层` 横幅注释：

```
常量与表 → 归一化 → 匹配 → 数据模型与持久化 → 渲染 → CLI
```

上层可以依赖下层，下层绝不依赖上层。`WarehouseKeeper.py` 是这套分层的第一个消费者：只做交互循环、行解析与追问，**业务逻辑一行都没复制**（`add` / `stock` / `remove` 都是构造一个假 args 转交给 `inventory.py` 的 `cmd_*`）。再加界面（Web UI / TUI）照同样路子来。`import inventory` 不会触发 argparse（有 `if __name__ == "__main__"` 保护）。

两个跨模块的私有名是**故意**的，别顺手"清理"：`WarehouseKeeper.py` 从 `inventory` 导入 `_setup_console_encoding`（这是它唯一一个下划线开头的跨模块导入，注释也写在导入处）。那个函数管着「管道与重定向下 stdout 默认是 gbk，而 gbk 编不出 `µ`(U+00B5)」这件事，复制一份等于让两套实现各自演化。改动时**不要破坏它的签名**。同理 `-n` 的处理只有一份 `apply_limit`，命令行与 REPL 四条路径共用。

### 两套「归一化」必须分清

这是全项目最容易踩的坑，README 与模块 docstring 在这里用词不一致：

| | 谁 | 何时生效 | 是否回写盘 |
|---|---|---|---|
| **写法规范化** | `canon_unit` → `canonical_tags` | 每次写盘前对**库中每条记录**跑一遍 | **是** |
| **判定用归一化** | `parse_quantity` / `canon_type` / `canon_package` / `canon_medium` | 只在查询与判定期 | **否** |

- 于是 `51r` 落盘成 `51Ω`（写法），而 `0.1uF ≡ 100nF` 的关系**永不写回**（判定）。
- **模块 docstring 第 3 条「标签永远原样存盘，归一化结果只在查询期派生」是过时的**：它对"判定"成立，对"写法"不成立。以 `canonical_tags()` 的 docstring 与 README「写法在存盘时统一」为准。
- `canonical_tags` **幂等**，所以「规范化」和「迁移」是同一件事——改一条记录会让全库的写法与列序一起收敛，没有单独的迁移命令。代价是「改一条会碰全库」这个隐性副作用，是知情取舍，不要"修"掉它。每条记录的 `updated_at` 不动。
- 两套的**粒度也不同**：`canonical_tags` 只重写「数字 + ASCII 单位简写」，中缀记号（`4k7`）、裸数字（`0805`）、中文单位（`1欧`）、容差（`1%`）、型号（`1N4148`）一个字符都不碰。而 `canon_package("5x11mm")` 返回 `5x11` 只是**判定层**的身份归约：盘里那条标签仍然原样是 `5x11mm`（自检 3771 行那条 `_c5` 就是拿真实数据里这种记录在断言）。README「已知限制」里「`5x11mm` 归一到 `5x11`」说的是后者，读成存盘写法会误解。

### 类型有推断引擎，封装只能查表

永久的、只有类型才有的能力（见 README「必需的两类标签」）：

- **只有类型可以推断**，封装一律查表。REPL 独有一条通道 `extra_package` 把用户答的封装名直接送进 `classify_tags` 绕过封装表——**用户说是什么就是什么**；命令行没有这条通道，认不出就报错。
- 推断证据分**强档（2）**（带明确单位的量、型号表、连接器系列）与**弱档（1）**（惯例裸前缀、介质词、中文描述词、频率、`Np` 引脚数）。取最强一条；**同强度内出现两个不同的码就报错，绝不按标签顺序任选**。弱档互相平级，所以 `薄膜 100k` 会如实报冲突。
- `classify_tags` **刻意永不抛异常**，把所有问题当数据返回（`TagPlan.issues`），这样自检能直接断言 issues 内容，用户可见的错误由 `render_issues` 渲染。新加校验规则时跟着这个风格。
- 设计取向是**宁可报错也不要猜错**：`u` / `n` / `m` 前缀、大写 `K`、`M7` 丝印都拒绝推断，就是为了不把猜错的结果静默写进盘。放宽任何一条前，先看清 README「已知限制」里对应那条的代价。

### 存量：零只有一个形状

`Stock` 是判别式对象（`{"mode": "coarse", "level": n}` 或 `{"mode": "accurate", "count": n}`），靠**结构**而非约定排除非法状态。**粗略档的 0 在 `__post_init__` 里折成精确 0**，由类型本身保证「0 只有一个形状」，而不是靠每个调用点记得转换。理由很实际：新元件默认就是 0，不折叠的话 `apply_stock_value` 的相对增减在最常见的路径（`add` 完想 `+1`）上必然失效。改 `Stock` 时别把这个折叠挪回各个解析点——那是它当初被消掉的双重表示。

`extract_stock_tags`（标签位置）与 `parse_stock_value`（`stock` 命令的值位置、认裸数字与相对增减）是**两处刻意分开**的写法，README 说明了两者的分歧（标签里不能写裸数字，否则 `0805` 会被吃成 805 个）。别为了"统一"而合并。

## 风格

- **注释与 docstring 沿用 `inventory.py` / `WarehouseKeeper.py` 现状的折行风格**（折在 80 列上下）。全局 CLAUDE.md 的「不要手动折行」在这个仓库落到**提交信息与 README 正文**上，代码注释跟着文件走——不要把现有注释抻平，那是几千行的机械 diff。
- **提交信息不手动折行**，一条一行、多长都不断开，多段就空一行。格式是 Conventional Commits + scope：`feat(add):` / `fix(infer):` / `refactor(validate):` / `fix(cli):`，正文用 `-` 逐条展开。这条没有任何余地。
- 中文优先（文件名、注释、界面、文档）；`_` 作后缀分隔符，不用 `-` 或 `()`；`.gitattributes` 把换行统一成 LF（`* text=auto eol=lf`），**不要手动或批量转换行符**。
- 用户重视「同一个概念只有一个形状」。摆方案时把「概念上更干净但改动更大」那条明确列出并**如实报出连带代价**（改了哪些显示、哪些既有决定被推翻、旧数据会怎样），不要因为改动面大就藏起来或主动降级为备选——他有能力也愿意承担更大的改动。
