# ZECTRIX NAS 相册

从已挂载的 NAS 照片文件夹随机选取人像，在 NOTE4 上以十六灰阶铺满显示，并在右下角标注 `YYYY-MM-DD` 拍摄日期。

这是安装到 **ZECTRIX 本地助手（Hub）** 的独立应用，应用与 Hub 分别更新。当前发行包要求 **macOS 15 或更高版本、Apple Silicon Mac**；NAS 提供照片共享，播放程序运行在 Mac 上。

[下载最新安装包](https://github.com/itopinion/zectrix-nas-gallery/releases/latest) · [English](README.en.md) · [Hub 接口约定](docs/HUB_CONTRACT.md)

## 使用前准备

- 在访达中连接 NAS 照片共享，确保 Mac 能打开其中的原图。
- 使用包含 **2026-10-17 图片接口扩展**、支持 **`/runtime/image` 十六灰阶图片接口** 的 ZECTRIX Hub。应用会检查 `GET /runtime/status` 的 `features.image.formats` 是否包含 `gray16`；仅有黑白布局接口的旧 Hub 需要更新。不能仅凭 Hub 显示的 `0.1.0` 版本号判断兼容。
- 当前兼容的图片接口构建尚未作为公开 Hub 发行版发布。[Hub 仓库](https://github.com/itopinion/zectrix-hub) 为私有仓库，需要访问权限；本应用公开发布不代表任意现有公开 Hub 安装包已兼容。
- NOTE4 与 Mac 位于可互通的局域网，设备已进入本地推送模式，并由 Hub 建立连接。SDK、固件和色板协议应使用配套版本，固件安装按 ZECTRIX 官方提供的指引操作。

发行 ZIP 内含应用运行时与本地人像检测组件，安装使用时无需另装 Python 或 Pillow。

## 安装与设置

1. 下载适合 `macos-aarch64` 的应用 ZIP，在本地助手中选择「安装应用包」。
2. 启用「NAS 相册」，打开其管理页。
3. 填入已挂载照片文件夹的本机绝对路径。默认小图缓存位于应用独立数据目录，也可另选照片来源目录之外的位置。
4. 保存设置。应用会在后台扫描文件路径，随机检查并准备照片，随后开始播放。

默认每 **5 分钟** 换一张，并提前准备 **6 张 400×300 小图**。可在设置中调整轮播间隔、缓存上限和预备数量。

| 设备按键 | 操作 |
| --- | --- |
| 上键短按 | 回看上一张已显示照片 |
| 下键短按 | 随机下一张 |
| 确认键短按 | 暂停或继续自动轮播 |

管理页也提供相同的翻页和暂停控制，以及重新扫描照片的入口。切到 Hub 中的其他应用后，NAS 相册暂停使用设备；重新启用时继续播放。

## 筛选与日期

应用在本机使用 macOS Vision 检测人脸，同时检查相机来源元数据，跳过截屏标记、缺少可靠相机来源、纯风景、过小或低置信的人脸。检测结果不会发送到第三方人像服务。

图片按人脸位置裁剪为 400×300，完整铺满屏幕。日期优先读取原始拍摄元数据，保留照片自身的日历日期；仅在来源明确时回退到相机原生数字化日期。明确且无歧义的文件名日期可补充缺失元数据。文件修改时间和一般 TIFF 编辑时间不作为拍摄日期；无法确认日期的照片会跳过。

严格筛选可能跳过已去除元数据的真实照片、远景人像或侧脸。相机来源检查与人脸检测不构成照片真实性鉴定。

## 缓存与运行位置

- 原图保留在 NAS。应用持久缓存的图片尺寸为 400×300，并在设备返回显示完成后记录到缓存和历史。
- 后台准备中的小图暂存在工作目录中，退出时清理。缓存有容量和数量限制，较久未使用的小图会自动清理。
- 已索引路径、人像检测结果和设置保存在应用独立数据目录，更新应用时保留。
- 更换照片来源目录或缓存目录时，应用会清理自身管理的旧小图缓存及相册索引，再建立新相册；NAS 原图保留在原位置。
- NAS 暂时不可用时，可继续使用已准备或已缓存的小图；相册首次运行且缓存为空时，需要先恢复照片目录。
- Mac、本地助手及相册应用需要保持运行。当前包不是在 Synology DSM 上直接运行的 Linux 应用，也不提供脱离 Mac 的设备端照片库。

预备小图减少读取原图、检测和转换带来的等待；墨水屏自身刷新仍需要时间。显示完成回执表示设备报告完成，光学显示效果以实机为准。

## 从源码构建

在 Apple Silicon Mac 上安装 Xcode Command Line Tools，然后在仓库目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.txt
PYTHON=.venv/bin/python ./scripts/build-macos.sh
```

脚本编译本地 Vision 组件，将 Python/Pillow、网页和组件打成独立可执行文件，再生成 Hub 安装 ZIP：

```text
dist/zectrix-nas-gallery-0.1.0-macos-aarch64.zip
dist/zectrix-nas-gallery-0.1.0-macos-aarch64.zip.sha256
dist/app.json
dist/release-metadata.json
```

ZIP 根目录仅包含 `app.json` 和 `bin/zectrix-nas-gallery`。manifest 的 `files` 使用实际可执行文件 SHA-256，打包脚本会复核哈希及 Hub 大小限制。

仓库源码不包含个人照片、NAS 路径索引、缓存、日志、设置、设备 ID、Hub token 或固件。请将个人配置留在 Hub 分配的数据目录中。

## 许可

[MIT](LICENSE) · [第三方软件说明](THIRD_PARTY_NOTICES.md)
