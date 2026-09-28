# AGENTS.md —— 在这个仓库里干活之前先读这个

这份文件是**约束**，不是介绍。项目是干什么的看 `README.md`。

改界面 / 审美 / 动效之前，另有两份必须读：`gui/README.md`（桌面版的设计约定与踩过的坑）
和 `gui/DESIGN.md`（颜色、排版、动效的规则）。很多看起来像「风格」的东西其实是**契约**，
`tests/test_gui.py` 把它们钉死了。

---

## 一、四条不能破的契约

1. **`CLEAN` 必须等于「查证过」。**
   「没拿到答案」绝不写成 CLEAN；「没量成功」绝不升级成「量出了问题」。
   这条是整个工具可信度的地基 —— 一个会把「超时」报成「没问题」的工具，
   比没有工具更糟。

2. **严重度和置信度是两个独立字段。**
   `severity` 说的是「如果属实有多严重」，`confidence` 说的是「我们有多确定」。
   **不许合并、不许互相推导。**

3. **退出码不重叠。** `relaycheck/cli.py`：
   `EXIT_OK=0` / `EXIT_FINDINGS=1` / `EXIT_ERROR=2`。
   0/1/2 各自只能有一个含义。曾经有个 cp936 控制台崩溃就是因为编码异常退成了 1
   —— 和「发现了问题」撞车，收脚本的人会把它读成「这家店有问题」。

4. **结论只到家族级（family）。**
   定位原话：**「当审计工具能用，当判决书不能用。」**
   报告里出现的是线索，不是定论。任何措辞、颜色、图标都不许把这个界限糊掉。

还有一条工程纪律：**只读、便宜、自证。** 探针不改动对方任何状态；
默认集必须是「第一次面对一个没人量过的站」时敢跑的东西。

## 二、目录速查

| 路径 | 是什么 |
| --- | --- |
| `relaycheck/` | 审计核心。**唯一进 wheel 的目录。** |
| `relaycheck/probes/` | 11 个探针。注册表在 `__init__.py` |
| `relaycheck/cli.py` | 命令行入口，退出码在这里 |
| `relaycheck/reporter.py` | 报告生成。**全文不引用 `api_key`** |
| `gui/` | Windows 桌面版。**不进 wheel，但进 sdist** |
| `gui/relaycheck_gui.py` | 界面主体都在这一个文件里，约 1800 行 |
| `gui/preview_ui.py` | 六态界面预览。**不发请求、不写报告**，动外观先看这个 |
| `tests/` | `test_gui.py` 34 项 / `test_mock_relay.py` 29 项 / `test_selection.py` 16 项 |
| `tests/mock_relay.py` | 8 个场景的假中转站。**不需要花真额度就能复现一切** |
| `examples/` | mock 跑出来的样例报告，**不含任何真实站点数据** |

## 三、硬性技术约束

* **运行时不加新依赖。** `requirements` 只有 `requests`；其余全是标准库。
* **测试不许依赖 Pillow** —— CI 只装 `requests` + `pytest`。
  （`gui/make_icon.py` 用 Pillow，但它不在测试路径上。）
* **`requires-python >= 3.9`。** CI 跑 3.9 / 3.11 / 3.13，别用 3.10+ 语法。
* **输出必须活过 cp936 控制台。** 任何往 stdout 打的字符都要能编码成 GBK，
  否则会 `UnicodeEncodeError` —— 而那个异常在 CLI 里意味着退出码变成了
  `EXIT_FINDINGS`。有回归测试钉着。
* **界面的色值不许用 `hash()`。** 它按进程加盐，同一份报告每次跑出来颜色都不一样。
  厂商→颜色走 `family_color()` 里的确定性加权和。
* **`gui/` 新增文件要确认 `MANIFEST.in` 覆盖到后缀。** 0.1.4 的 sdist 漏了
  `*.ico`/`*.png`，PyInstaller 抛 `FileNotFoundError` —— 不是「没图标」，
  是**根本构建不出来**，最后用 0.1.5 补的。

## 四、怎么跑

```powershell
# 全量（约 6 分钟；dead 场景自己就吃 250 秒）
python -m pytest -q -rs

# 日常快跑（约 27 秒）
python -m pytest -q -k "not dead and not slow"

# 单个文件
python tests\test_gui.py          # 34 项，不需要构建产物
python tests\test_mock_relay.py   # 29 项
python tests\test_selection.py    # 16 项
```

**不要用 `--deselect`** —— 在 Windows 上不生效，会静默地什么都没排除。

跑假站，不花自己的额度：

```powershell
python tests\mock_relay.py --port 8123 --scenario fraudulent --verbose
python -m relaycheck.cli -u http://127.0.0.1:8123 -k sk-relaycheck-local-test --probes all
```

## 五、这个环境独有的陷阱

**读源码文件不要用 PowerShell 的 `Get-Content` / `Select-String` / `.Count`。**
UTF-8 中文会被按 ANSI 解成乱码，行数也会撒谎（实测一个 718 行的文件报 434）。
用能指定编码的工具读。

**`gui/build.ps1` 的 UTF-8 BOM 是功能性的，不是风格。**
编辑工具会顺手吃掉它。Windows PowerShell 5.1 把 BOM-less 的 `.ps1` 按 ANSI(cp936) 解码，
中文注释末尾的字节会和下一行的 `LF` 配对，**整行代码被吞进注释里**。
实测吞掉了 6 行，其中一行是 `$Probe = '...'`，于是 `build.ps1 -Python` 报
「Argument expected for the -c option」，然后声称找不到可用的解释器 ——
而那个解释器手动敲是好的。
**CI 永远抓不到这个 bug**（runner 是 en-US，cp1252 没有前导字节），
所以只能靠 `tests/test_gui.py` 那条读字节的测试钉住。

**frozen 的 exe 不认 `PYTHONIOENCODING`。**
父进程设了也没用，打包后照样按 GBK 输出。修法只能是在自己的代码里强制
（`relaycheck.cli.force_utf8_output`）。症状特别难查：`report.json` 完全正确，
只有用户盯着看的管道那一侧是乱码。

**别按进程名全局 kill `python`。** 会顺手杀掉正在跑的审计和 MCP server。

## 六、发布顺序（不可颠倒）

1. 推 `main`
2. **等 CI 全绿**
3. 才打 tag `v*`

`RELEASING.md` 有完整流程。原因：tag 一推，`release.yml` 就把版本号发上 PyPI，
而 **PyPI 的版本号不可复用** —— yank 了也不能再用。构建挂在中间就只能浪费下一个号。

`desktop.yml` 除 tag 外还会每周一在 `main` 上跑一次（只构建 + 传 artifact，
**不碰 Release**），用来提前发现 runner 镜像 / Python / PyInstaller 的漂移。

## 七、提交前检查单

- [ ] `python -m pytest -q -k "not dead and not slow"` 全绿
- [ ] 动了界面外观 → 改跑 `python -m pytest -q -rs`，并重建一遍看真东西
- [ ] 动了 `gui/` → `.venv-build\Scripts\python.exe gui\e2e_bundle.py`（21 项，跑真的 exe）
- [ ] 动了 `gui/build.ps1` → 确认 BOM 还在（`raw[:3] == b"\xef\xbb\xbf"`）
- [ ] 新增文件进了 sdist？检查 `MANIFEST.in`
- [ ] `CHANGELOG.md` 加了一条
- [ ] `git status --short` 里没有临时脚本

## 八、一条经验

这个项目是被**误报**喂大的。`CONTRIBUTING.md` 里那两类 issue（`false-positive` /
`missed-detection`）比任何 feature request 都值钱 —— 一个诚实的站被报了 MEDIUM，
比少一个功能严重得多。
