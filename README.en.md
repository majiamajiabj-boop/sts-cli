# Spire Assistant for Windows

English | [简体中文](README.md)

A local companion for **Slay the Spire**. Play the game yourself while the assistant shows a recommended action, its target, and an explanation. Local advice does not require an API key.

The current desktop interface is in Chinese. This README provides English setup instructions; game card and enemy names follow the language exported by the game.

## Download and play

1. Install your own copy of Slay the Spire through Steam.
2. Subscribe to [ModTheSpire](https://steamcommunity.com/sharedfiles/filedetails/?id=1605060445) and [BaseMod](https://steamcommunity.com/sharedfiles/filedetails/?id=1605833019), and wait for Steam to finish downloading them.
3. Download **SpireAssistant-Windows-20260921-230502.zip** from the [Windows release](https://github.com/majiamajiabj-boop/sts-cli/releases/tag/windows-20260921-230502). Choose the application ZIP, not GitHub's source-code archives.
4. Extract the entire ZIP into a writable folder and open **SpireAssistant.exe**. Do not copy only the EXE or run it from inside the archive.
5. Open **游戏设置** (Game settings), then **检查依赖** (Check dependencies). The assistant detects Steam libraries automatically; use **选择文件夹** (Choose folder) if necessary.
6. Close any existing game session. Click **启动实时顾问** (Start live advisor) and confirm. The bundled CommunicationMod is installed automatically, with an existing copy backed up.
7. Start or continue a run manually. Keep the assistant visible, or choose **精简悬浮窗** (Compact floating window).

**Requirements:** Windows 10/11 x64, the game, ModTheSpire, and BaseMod. Python and the application runtime are bundled. Players do not need Python, Git, a JDK, Codex, or an API account.

## Modes

| Mode | What it does |
| --- | --- |
| Live advisor / 实时顾问 | Reads passive game snapshots and displays advice. You make every game action. |
| Automatic play / 原有自动游玩 | Uses the existing fast-policy-v5 controller and its safety gates to operate the game. Full unattended play in this EXE has not completed live acceptance testing. |
| Run reports / 战报查看 | Opens a local, read-only report viewer. A fresh installation has no previous runs. |

Advisor mode does not start the command bridge, consume its action queue, or execute game commands. Advisor and automatic modes are mutually exclusive. Advice is bound to its game state and cleared when the state changes, the game is resolving actions, or the connection becomes stale.

Supported characters: **Ironclad, Silent, and Defect**. Watcher and third-party characters are not supported by the strategy. Recommendations are heuristic and do not guarantee optimal play or a win.

## Validation and limitations

- The published ZIP passed **2,947 tests** and was launched outside the source directory using its bundled runtime. The reorganized source tree passed **2,948 tests** and a complete Windows build.
- A player confirmed combat advice, updates after manual play and ending a turn, reward screens, and map advice.
- Full live checks for shops, campfires, card-selection flows, disconnecting after game exit, automatic-mode startup, and mode switching remain incomplete.
- DPI rendering was checked at 125% scaling on one Windows machine. Other PCs and switching between monitors have not been validated.
- The EXE is unsigned. The compact window may be covered by exclusive-fullscreen games; windowed or borderless mode may be more convenient.

See the [acceptance record and known limitations](docs/ASSISTANT_TEST_RECORD.md) (Chinese). Replay tests are not presented as live gameplay verification.

## Optional remote model

Live advice uses the local strategy. The existing automatic mode has an optional DeepSeek integration, disabled by default. Advanced users supply their own credentials and must satisfy the normal policy and release checks after changing configuration. Remote API functionality has not been validated for this release. See [configuration notes](docs/DEEPSEEK_MACRO.md).

## Build from source

Developers need Windows, CPython 3.12 with Tk, JDK 8, and locally installed game/Mod dependencies:

```powershell
python -m unittest discover -p "test_*.py"
python build_windows.py --python-home 'C:\Python312' --jdk 'C:\Java\jdk8' --game-dir 'D:\SteamLibrary\steamapps\common\SlayTheSpire'
```

The build runs the complete regression suite and release checks, compiles CommunicationMod, bundles the runtime, and writes a new distribution under `dist/`. It does not launch the game. Java integration tests require the actual game and Mod dependencies; an ordinary test run without a JDK may skip Java checks, while a release build requires them.

| Directory | Contents |
| --- | --- |
| `tests/`, `test_fixtures/` | Regression tests and fixtures |
| `docs/` | User instructions, protocol, acceptance records, and progress |
| `src/` | CommunicationMod and spirecomm sources |
| `packaging/` | EXE launcher and third-party license files |
| `viewer/` | Local report viewer |
| `scripts/` | Workspace maintenance utilities |
| `experiments/` | Separate experiments; not the live policy |

## Sharing and third-party components

Share the original release ZIP rather than repacking a directory you have used: a used directory can contain settings and personal run data. Releases exclude personal credentials, `.env` files, runtime logs, and game saves.

The game, its Java runtime, ModTheSpire, and BaseMod are installed by the player and are not redistributed. Third-party licenses are retained in their source directories and `packaging/licenses`; see [third-party notices](docs/THIRD_PARTY_NOTICES.md). This release has not been uploaded to the Steam Workshop.

See the [public publication review](docs/PUBLICATION_REVIEW.md) for the reviewed scope and handling of private history.
