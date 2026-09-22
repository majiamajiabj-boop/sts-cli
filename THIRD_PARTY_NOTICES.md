# 第三方组件与分发范围

核对日期：2026-09-21。以下是本包的组件清单与保留声明说明。

| 组件 | 分发方式与条件 | 来源 |
|---|---|---|
| CommunicationMod（含本项目修改） | MIT；附原始版权与完整许可证，修改为只读快照、命令阻断和 JSON 参数路径支持 | [上游许可证](https://github.com/ForgottenArbiter/CommunicationMod/blob/master/LICENSE) |
| spirecomm（含本项目策略修改） | MIT；附原始版权与完整许可证 | [上游许可证](https://github.com/ForgottenArbiter/spirecomm/blob/master/LICENSE) |
| CPython 3.12、标准库 | PSF 及其包含组件条款；保留 runtime/LICENSE.txt。解释器二进制未修改；删除第三方 site-packages、开发脚本、缓存与测试目录，只保留本应用运行所需文件 | [Python 许可证](https://docs.python.org/3/license.html) |
| Tcl/Tk | Python 的 Tk 界面依赖；保留 runtime/tcl 内的 license.terms 及相关声明 | [Tcl/Tk 许可证](https://www.tcl-lang.org/software/tcltk/license.html) |
| OpenSSL 3.5.8 | Apache-2.0；附 OpenSSL-LICENSE.txt，库未修改 | [OpenSSL 官方许可证](https://raw.githubusercontent.com/openssl/openssl/openssl-3.5.8/LICENSE.txt) |
| libffi | MIT；附 libffi-LICENSE.txt | [官方许可证](https://github.com/libffi/libffi/blob/master/LICENSE) |
| SQLite 3.53.1 | 公有领域；库未修改 | [官方声明](https://www.sqlite.org/copyright.html) |
| CPython 其他内置库（zlib、bzip2、liblzma、Expat 等） | 保留其许可与致谢全文：licenses/CPython-included-software.html | [Python 3.12 内置软件许可](https://docs.python.org/3.12/license.html) |
| Windows Visual C++ 运行库 DLL | CPython 官方 Windows 发行包所附运行时；保持原样及名称 | [Python Windows 分发说明](https://docs.python.org/3/using/windows.html) |
| .NET Framework | 不随包重分发；使用 Windows 10/11 自带的 4.x | Windows 系统组件 |
| Slay the Spire / Java 游戏运行时 | **不分发**；用户通过自己的合法游戏安装取得 | Steam 游戏库 |
| ModTheSpire / BaseMod | **不随 ZIP 分发**；用户自行订阅下载或选择自己的 JAR。只用于本机编译/运行；启动时可将用户本机 BaseMod 复制到游戏 mods 目录 | [ModTheSpire](https://steamcommunity.com/sharedfiles/filedetails/?id=1605060445)、[BaseMod](https://steamcommunity.com/sharedfiles/filedetails/?id=1605833019) |
| Gson / 游戏类 / BaseMod 类 | 编译时引用用户已有游戏及 Mod JAR，本助手 JAR 不打包这些第三方类 | 游戏/Mod 的运行时类路径 |

本发布包不使用 PyInstaller，不包含 Codex 私有运行服务或第三方 Python 包。没有游戏资源、个人密钥、.env、历史日志、存档或个人战报。自动模式初始案例集仅含 12 个 attempt_id 以 fixture- 标记的固定合成回归样例，不含个人对局。本项目新增的助手及构建源码随包提供，底层 MIT 部分继续保留其原许可。

分享的是原始 ZIP。使用后目录可能包含用户自己的设置、临时游戏状态和日志，不应直接转发使用后的目录。
