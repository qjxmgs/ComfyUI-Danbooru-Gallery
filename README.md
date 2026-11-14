> [!CAUTION]
> ### 🚚 项目已迁移
>
> 本仓库已停止新功能开发，仅保留历史代码与文档。节点功能及后续更新已迁移至 **[ComfyUI-Aaalice-Nodes](https://github.com/Aaalice233/ComfyUI-Aaalice-Nodes)**，请前往新项目安装和使用。
>
> 新版工作流请查看 **[Aaalice-Workflows](https://github.com/Aaalice233/Aaalice-Workflows)**；工作流管理与分享功能请查看 **[ComfyUI-Aaalice-Workflow-Hub](https://github.com/Aaalice233/ComfyUI-Aaalice-Workflow-Hub)**。

# 🎨 ComfyUI-Danbooru-Gallery

🇨🇳 本节点专为 [ShiQi_Workflow](https://github.com/Aaalice233/ShiQi_Workflow) 工作流打造，聚焦中文用户的实际使用场景与体验。

✨ 面向 ComfyUI 的实用节点集合，重点覆盖 Danbooru 图像检索、提示词编辑/清洗、缓存管理、Krita 联动与工作流辅助工具。

## 📌 当前状态

- ✅ 当前注册节点数：`23`
- 🧩 前端扩展：包含若干 JS 面板与交互增强（如快速组导航）
- 🗂️ 已移除的旧节点不再列出

## 🚀 安装

1. 进入 ComfyUI 自定义节点目录：

```bash
cd ComfyUI/custom_nodes
```

2. 克隆仓库：

```bash
git clone https://github.com/Aaalice233/ComfyUI-Danbooru-Gallery.git
```

3. 安装依赖：

```bash
cd ComfyUI-Danbooru-Gallery
pip install -r requirements.txt
```

4. 重启 ComfyUI。

## 🛠️ 依赖与环境

- Python `>=3.8`
- 主要依赖：`requests`、`aiohttp`、`aiosqlite`、`psutil`、`Pillow`、`torch`、`numpy`

## 🧠 节点清单（当前有效）

### 🖼️ 图像与提示词核心

- `DanbooruGalleryNode`：D站画廊 (Danbooru Gallery)
- `PromptSelector`：提示词选择器 (Prompt Selector)
- `PromptCleaningMaid`：提示词清洁女仆 (Prompt Cleaning Maid)
- `CharacterFeatureSwapNode`：角色特征交换 (Character Feature Swap)
- `MultiCharacterEditorNode`：多角色编辑器 (Multi Character Editor)

### 📂 图像输入输出与文件

- `SimpleLoadImage`：简易加载图像 (Simple Load Image)
- `SaveImagePlus`：保存图像增强版 (Save Image Plus)
- `SimpleImageCompare`：简易图像对比 (Simple Image Compare)
- `SimpleCheckpointLoaderWithName`：简易Checkpoint加载器 (Simple Checkpoint Loader)
- `ModelNameExtractor`：模型名称提取器 (Model Name Extractor)

### 🔧 工作流控制与字符串工具

- `ParameterControlPanel`：参数控制面板 (Parameter Control Panel)
- `ParameterBreak`：参数展开 (Parameter Break)
- `SimpleStringSplit`：简易字符串分隔 (Simple String Split)
- `SimpleValueSwitch`：简易值切换 (Simple Value Switch)
- `EnumSwitch`：枚举切换 (Enum Switch)
- `WorkflowDescription`：工作流说明 (Workflow Description)
- `SimpleNotify`：简易通知 (Simple Notify)
- `ResolutionMasterSimplify`：分辨率大师简化版 (Resolution Master Simplify)

### 🧷 组管理相关

- `GroupMuteManager`：组静音管理器 (Group Mute Manager)
- `GroupIgnoreManager`：组忽略管理器 (Group Ignore Manager)
- `GroupIsEnabled`：组是否启用 (Group Is Enabled)

### 🎨 Krita 联动

- `OpenInKrita`：从Krita获取数据 (Fetch From Krita)
- `FetchFromKrita`：从Krita获取数据 (Fetch From Krita)

## 🧩 非节点型前端扩展

以下功能以 JS 扩展形态存在，不会在节点列表中单独占一个节点名：

- `Quick Group Navigation`（快速组导航）
- 部分节点的设置弹窗、缓存面板、多语言与日志上报能力

## 📎 兼容与说明

- 本项目是 [ShiQi_Workflow](https://github.com/Aaalice233/ShiQi_Workflow) 的配套节点集，也可独立使用。
- 部分节点（如多角色编辑相关）可能依赖额外生态插件，请按节点 UI 提示安装。
- 若从旧版本升级，请以本 README 的“节点清单（当前有效）”为准，旧文档中的已删除节点不再维护。

## ❓ 常见问题

### 1. 🔍 节点没出现

- 确认仓库路径为 `ComfyUI/custom_nodes/ComfyUI-Danbooru-Gallery`
- 确认依赖安装完成后重启 ComfyUI
- 查看 ComfyUI 控制台是否有导入异常日志

### 2. 🖥️ 前端面板没加载

- 检查 `WEB_DIRECTORY` 是否成功注册（插件初始化日志会输出）
- 清理浏览器缓存后刷新 ComfyUI 页面

### 3. 🧪 Krita 功能不可用

- 检查 Krita 插件安装状态
- 查看节点内的安装/重装按钮日志提示

## 🧑‍💻 开发

- 代码入口：`__init__.py`
- Python 节点：`py/`
- 前端扩展：`js/`
- 工具脚本：`tools/`

## 📄 许可证

[MIT](LICENSE)

## 提示词翻译词库

画廊独立使用 ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table 的 `tag.sqlite`（MIT），与 Prompt Weaver 使用相同上游，但不共享文件和设置。旧 JSON/CSV 不再参与翻译；未收录标签保留英文。精确翻译不按使用次数过滤。

在本项目目录手动管理：

```powershell
python tools/manage_translation_dictionary.py --download
python tools/manage_translation_dictionary.py --import-file "D:\词库\tag.sqlite"
python tools/manage_translation_dictionary.py --status
```

词库存放在 `py/shared/data/tag.sqlite`。下载和导入先验证，再替换；失败保留原词库。更新后刷新浏览器以清除前端查询缓存。服务启动不会自动联网更新。

画廊设置 → 界面 → 自动补全中的“词条最少作品数”默认 100，最低 10；Danbooru 联想及中文搜索只返回作品数达到阈值的词条。图片已有标签的精确翻译不受限制。词库按需查询，不会一次载入全部词条。

## D 站离线浏览

ComfyUI 本地服务运行时，可在设置 → 离线管理中选择“自动联网”或“仅本地”。自动模式在 API 请求失败后尝试恢复相同查询的历史页面，并暂停联网；“重试联网”才恢复在线请求。没有缓存的页面会保留当前内容并提示缺页，不视为网站没有更多图片。

- “历史记录”恢复已保存页面及顺序；“搜索本地已缓存图片”在当前账号的全部历史帖子中搜索。普通标签取交集，`-tag` 排除；支持中文标签、评分、`date:2026-01-01..2026-12-31`（可省略一侧日期）、`order:newest`、`order:oldest`、`order:id`、`order:id_asc`、`order:id_desc` 和 `order:score`。排行榜等其他站点语法仅支持恢复已有历史，不作为本地搜索条件执行。
- 成功页面的元数据立即保存；缩略图下载最多并发两个，后台补齐最多占用一个名额，可见图片优先。补齐前断网的图片显示占位卡，标签仍可编辑、启停、翻译和复制；原图查看、下载和工作流图像输出需要联网；本地收藏无需联网。
- 数据保存在项目 `py/danbooru_gallery/cache/offline/` 下，按 ComfyUI 用户和 D 站账号隔离，不保存账号密钥。不自动淘汰历史或离线缩略图；可在“离线缓存管理”查看占用并确认清空，或单独删除历史，仅清理不再被其他历史引用的数据。
- 已有代理缩略图按 URL 复用；过去只在内存中的帖子列表不能自动补成完整历史。开启本功能后成功访问的页面才会建立可恢复记录。G 站维持原有行为。
- 页面保留已加载图片的顺序和元数据，只挂载视口及上下各一屏的卡片；选中项和编辑数据独立保存，离屏仍可输出，滚回时恢复。不为每张历史图片保留 DOM 占位。浏览器刷新及 ComfyUI 重启不会删除离线历史。

升级后重启 ComfyUI，并保存工作流后刷新浏览器加载新前端。

### 连续浏览性能

瀑布流按图片尺寸计算布局，滚动时只查询附近卡片并分帧挂载；慢图保留尺寸和加载提示。缩略图请求在画廊节点间去重、限流，离屏取消无人使用的请求，失败最多自动尝试三次。已解码的缩略图按用户范围和来源复用，空闲缓存限制为 128 张或 32 MiB 解码像素，超限按最近使用顺序淘汰并显式释放。卡片卸载时同时释放绘图缓冲区，避免只限制压缩文件大小而遗漏浏览器原生图片内存。滚动期间暂停标签浮窗生成，停下悬停后再显示。原图查看和下载仍使用原媒体文件。

开发验证命令：

```powershell
node --test tools/test_gallery_viewport.mjs
python -B -m unittest tools.test_thumbnail_priority
$env:GALLERY_QA_DIR = 'D:\Develop\QA\ComfyUI-Danbooru-Gallery\your-run'
$env:GALLERY_SOAK_SECONDS = '1800'
$env:GALLERY_NATIVE_SOAK = '1'
python -B tools/check_gallery_scroll_browser.py
```

浏览器测试使用隔离的本地接口、10,000 张混合比例图片和独立浏览器配置目录，需要已有 Playwright、aiohttp、Pillow 和 Chrome；可选 psutil 记录渲染进程内存。不设置 `GALLERY_NATIVE_SOAK` 时运行多选、编辑、卡片复用、收藏兼容、竞态与慢图等功能检查；设置为 `1` 时使用不启用网络记录的独立浏览器做内存验收，避免调试器保留请求历史干扰结果。`GALLERY_SCROLL_STEP=256` 可增加快速滚动压力。报告记录帧间隔、长任务、DOM/监听器、解码缓存、内存和持续滚动结果；它不代表外部站点始终能及时响应，也不代替实际工作流生成验收。

### 本地收藏夹 / Local favorites

D站画廊的爱心按钮将图片资料和缩略图保存在 ComfyUI 服务端，Danbooru 与 Gelbooru 分别管理，无需线上账号。点击“本地收藏夹”可独立搜索，退出后恢复在线搜索。新收藏夹从空列表开始，不导入或修改线上收藏。

收藏库位于 `py/danbooru_gallery/data/local_favorites.sqlite`，不受缓存清理影响。停止 ComfyUI 后复制此数据库即可备份。原图仍按需联网加载；资料未完整保存时显示“补全资料”，断网仍可保留收藏并使用已有标签和缩略图。收藏内支持标签、排除标签、分级及日期筛选；默认最近收藏在前，不支持 `order:rank` 等线上专用搜索语法。

Favorites are stored on the ComfyUI server, independently of website accounts and browser storage, with separate lists for each site. Saved metadata and thumbnails survive cache cleanup. Originals require an online fetch. Existing online favorites are neither imported nor modified.
