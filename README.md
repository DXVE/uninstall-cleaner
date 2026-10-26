# uninstall-cleaner

找出并清理 Windows 软件**卸载后遗留的目录、注册表项、启动项、服务、计划任务与快捷方式**。删除文件与快捷方式会移入回收站，注册表、服务与计划任务先备份，均可还原。

<table>
  <tr>
    <td width="40%"><img src="/screenshot/1.png"></td>
    <td width="40%"><img src="/screenshot/2.png"></td>
  </tr>
  <tr>
    <td align="center">主界面</td>
    <td align="center">搜索结果（以 JetBrains 为例）</td>
  </tr>
</table>

## 快速开始

### 直接使用

从 Releases 下载 `uninstall-cleaner-v<版本号>.exe`，双击运行。单文件绿色版，无需安装，也不在程序目录写文件。

### 从源码运行

需要 Python 3.14 与 [uv](https://docs.astral.sh/uv/)。源码位于 `src/uninstall_cleaner/`：

```bash
uv sync
uv run uninstall-cleaner
```

`uv sync` 会把项目以可编辑方式装入项目内 `.venv`；该虚拟环境由系统 CPython 创建，tkinter 随基础解释器提供，若改用其他解释器需确认其自带 tkinter。根目录 `main.py` 是等价的兼容入口，桌面快捷方式指向它。

无界面自检（只输出结果，不做删除）：

```bash
uv run uninstall-cleaner --cli --keyword 软件名 --json
```

参数：`--no-unmatched` 关闭“不在程序列表中”判定，`--no-orphan` 关闭卸载登记检测，`--no-startup` 关闭启动与后台检测，`--drive-roots` 额外扫描各磁盘根目录一级目录，`--limit N` 限制输出条数，`--version` 显示版本号。

## 检测内容

| 检测项 | 命中条件 | 风险 |
| --- | --- | --- |
| 卸载登记残留 | 卸载后仍留在系统中的卸载信息：卸载命令或安装目录指向不存在的路径 | 高/中 |
| 启动项 | `Run` / `RunOnce` 命令指向的文件不存在 | 高 |
| 服务 | 服务 `ImagePath` 指向的文件不存在 | 高 |
| 计划任务 | 任务动作的可执行文件不存在 | 高 |
| 快捷方式 | 开始菜单或桌面快捷方式的目标不存在 | 高 |
| 关键词命中 | 目录名、注册表路径、显示名称、发布者、服务名、任务名或快捷方式目标包含关键词 | 中/低 |
| 不在程序列表中 | 目录或注册表键找不到对应的已安装程序 | 低 |

- 目标文件已不存在判为高风险；关键词命中但对应程序仍在安装则降为低风险。
- 目录类候选要求内部存在 `.exe`，纯缓存、纯数据目录不列为残留。
- 未登记安装信息的便携软件、驱动可能出现在低风险候选中，需人工确认；MSIX/应用商店应用不在检测范围。

## 使用

- 查找方式二选一：**按关键词**（结果只保留命中项，多个关键词用中英文逗号分隔、不区分大小写），或**不在程序列表中**（不使用关键词，只找找不到已安装程序的目录与注册表键）。
- 检测范围至少选一项：文件与目录、注册表、启动与后台；需要时勾选“额外扫描各磁盘根目录（较慢）”。
- 结果页顶部为统计与“返回”；可按风险、类别筛选，搜索框匹配名称、位置、判定依据与关键词，点表头排序；高风险项默认勾选；选中一行后右侧显示详情，可“打开位置”“复制路径”。
- 快捷键：`F5` 开始扫描；表格内 `Ctrl+A` 全选可见项、`空格` 勾选当前行、`Delete` 删除选中项；右键菜单提供全选、反选、复制路径等操作。
- 受保护项不可勾选删除：磁盘根目录、Windows 目录、系统与厂商共用目录（Microsoft、Windows、Common Files、Package Cache、Intel、NVIDIA、Realtek 等）、GUID 形态注册表键，以及包含本工具或用户目录的上级目录。

## 删除与恢复

- 文件与快捷方式：移入回收站，可从回收站还原。
- 注册表项、服务、计划任务：选中项包含这三类时，清理前弹窗选择备份位置，在所选目录下新建 `<时间戳>\` 文件夹，导出 `.reg` / `.xml` 与 `manifest.json`；备份失败的项目不会删除。只删文件与快捷方式时不询问，也不生成备份。
- 恢复：双击 `.reg` 导入；服务需先导入注册表键，再按原 `ImagePath` 重建；计划任务用 `schtasks /create /tn <名称> /xml <备份文件>` 重建。

## 权限

扫描不需要管理员权限。删除 `Program Files` 下的目录、HKLM 注册表项、服务需要管理员权限，标准用户下这些操作会失败并说明原因；界面右上角可“以管理员身份重启”。

## 打包

配置文件为 `packaging/uninstall-cleaner.spec`：单文件、无控制台窗口，并把 README 一并内嵌，供界面“帮助 → 使用说明”读取。PyInstaller 属开发依赖，由 `dev` 依赖组提供，`uv sync` 时一并安装。

```bash
uv sync
uv run pyinstaller --noconfirm --clean --distpath dist --workpath build packaging/uninstall-cleaner.spec
```

- 产物为 `dist\uninstall-cleaner-v<版本号>.exe`（版本号取自 `src/uninstall_cleaner/__init__.py`），约 13.5 MB，启动时解包到临时目录。
- spec 内路径以项目根为基准，`--distpath`、`--workpath` 可改到任意位置。
- 运行时只依赖 Python 标准库，不读外置配置；备份位置由清理时选择。

## 免责声明

本项目为个人学习作品，部分代码与文档由 AI 辅助生成，无法完整追溯该部分内容的来源，代码质量未经严格测试，不提供任何明示或暗示的保证，**不建议在生产环境或未备份的重要数据上使用**。使用前请务必备份原始数据，作者不对因使用本工具造成的任何数据丢失或损坏承担责任。
