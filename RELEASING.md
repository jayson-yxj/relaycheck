# 发布流程

给维护者看的。用户不需要读这个。

## 为什么不用 API token

发布用的是 **Trusted Publishing（OIDC）**：PyPI 直接验证「这次上传来自
`jayson-yxj/relaycheck` 的 `release.yml`」，仓库、secret、本地磁盘上都不存在任何
PyPI 凭据。没有东西可以泄漏，也没有东西需要定期轮换。

代价是首次要手工配一次（下面那段），之后每次发布只是推一个 tag。

## 一次性设置

1. 在 <https://pypi.org/account/register/> 注册 PyPI 账号（要邮箱验证）。
2. 强烈建议开 2FA —— 开了之后仍然可以用 Trusted Publishing，不需要任何 token。
3. 打开 <https://pypi.org/manage/account/publishing/>，在
   **「Add a new pending publisher」** 表单里逐字填：

   | 字段 | 值 |
   |---|---|
   | PyPI Project Name | `relaycheck` |
   | Owner | `jayson-yxj` |
   | Repository name | `relaycheck` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   「Workflow name」要的是**文件名**（`release.yml`），不是 workflow 里 `name:` 那一行。

   项目此时还不存在，所以只能填 pending publisher；第一次发布成功后 PyPI 会自动把这个
   pending 记录转成项目的正式 publisher，不用再回来改。

4. （可选但推荐）在 GitHub 仓库的 **Settings → Environments** 里新建一个叫 `pypi` 的
   environment，加上 Required reviewers。这样打 tag 之后还要有人点一下批准，才真的上传。
   不建也行 —— 第一次跑 workflow 时 GitHub 会自动创建这个 environment。

## 每次发布

1. 改 `relaycheck/__init__.py` 里的 `__version__` —— 这是唯一的版本号来源，
   `pyproject.toml` 用 `dynamic = ["version"]` 从这里读，不用（也不要）另改一处。
2. 在 `CHANGELOG.md` 里加一节，把那句「未发布」改成实际日期。
3. 确认 `main` 上这条提交的 CI 是**绿的** —— 打 tag 不会触发 `ci.yml`（它只在分支和 PR 上跑），
   所以 tag 有可能指向一条从没被测试过的提交。
4. 打 tag 并推：

   ```bash
   git tag -a v0.1.1 -m "relaycheck 0.1.1"
   git push origin v0.1.1
   ```

5. `release.yml` 会构建 sdist + wheel、跑 `twine check`、校验 **tag 和构建出来的版本号一致**，
   然后才上传。任何一步不过就是红灯，不会发出半成品。

## 同一个 tag 还会做什么

推 `v*` 会同时触发 **`desktop.yml`**：在 `windows-latest` 上构建桌面版、跑一遍
`gui/e2e_bundle.py` 确认产物真的能跑，然后把 `relaycheck-<版本>-windows-x64.zip` 挂到
这次 tag 对应的 **GitHub Release**（Release 由它创建，不在 `release.yml` 里）。

两者**互不依赖**，这是刻意的：PyPI 的 trusted publisher 没配好、或者 PyPI 挂了，都不该
让用户下不到 exe；反过来桌面构建失败也不该挡住 pip 用户。所以：

* 只想先出个 exe 给不会 pip 的人 → Actions → **desktop** → Run workflow。
  这条手动路径不推 PyPI，产物在同一次运行的 Artifacts 里。
* 只发 PyPI → 正常打 tag，桌面那条腿红了也不影响 `release.yml` 的结果。

`desktop.yml` 不校验 tag 与版本号是否一致（它不做不可逆的事），但如果 tag 和
`relaycheck/__init__.py` 里的版本对不上，zip 的文件名会跟着 `__version__` 走，和 tag
不一致 —— 看到这种不一致，说明第 1 步漏了。

## 不可逆

**PyPI 不允许同一个版本号被复用，yank 之后也不行。** 发错版本的唯一修法是烧掉下一个号。
所以 `release.yml` 里那道「tag 与构建版本必须一致」的检查不是仪式 —— 它拦的是那类
「tag 写了 v0.2.0、`pyproject.toml` 还停在 0.1.0」的经典事故，那种错误一旦上传就没法回头。

## 本地预演

不推 tag 也能验证产物能不能过 PyPI 的元数据检查：

```bash
python -m pip install build twine
rm -rf dist/
python -m build
python -m twine check dist/*
```

`twine check` 只验证元数据（尤其是 `README.md` 作为长描述能不能被渲染），不会上传任何东西。
真正要试传可以打 `--repository-url https://test.pypi.org/legacy/`，但那需要 TestPyPI 的凭据 ——
本项目的定位是宁可少一道工序，也不引入长期凭据。
