# tapescribe · ASR 工作台

本机网页工具：管理录音、用 ffmpeg 合并分段、转写并区分说话人（A / B），结果带时间戳并按句换行。项目名 `tapescribe` = tape（录音带）+ scribe（抄写员）。

本地优先，不需要云端账号：识别后端可接本机 MLX 模型、任意 OpenAI 兼容接口，或阿里云百炼录音文件识别。

## 启动

```bash
./start.sh                        # 按 data/network.json 的访问范围启动，默认仅本机
ASRWB_HOST=0.0.0.0 ./start.sh     # 这一次临时开放局域网（不写入设置）
```

访问范围有两种，保存在 `data/network.json`（默认 `localhost`）：

| 模式 | 监听 | 入口 |
| --- | --- | --- |
| `localhost`（仅本机） | `127.0.0.1:8770` | `http://127.0.0.1:8770` |
| `lan`（局域网） | `0.0.0.0:8770` | `http://<本机IP>:8770`，本机 IP 用 `ipconfig getifaddr en0`（无线网卡常是 `en1`） |

在页面「识别模型设置 → 访问范围」里切换后点「保存并重启」，服务会自己重启生效，排队中的任务从断点继续；也可以直接改 `data/network.json` 再重启。`ASRWB_HOST` 环境变量只覆盖当次启动，优先级高于文件。

局域网模式下没有登录验证，任何能连上该端口的人都能读写录音和转写结果。

依赖一个本地识别服务，默认地址 `http://127.0.0.1:8765`，在页面「识别模型设置」里改。想让队列在服务掉线时自动拉起，把启动脚本通过环境变量传进去：

```bash
ASR_START_SCRIPT=/path/to/start-all.sh ./start.sh
```

## 桌面快捷方式

`desktop/make-app.sh` 生成并安装 `~/Desktop/ASR 工作台.app`：双击时若服务未运行则拉起（已装 LaunchAgent 就走 launchd，否则后台启动），然后打开页面。

```bash
./desktop/make-app.sh      # 重新生成（改了 launch.sh 或图标后执行）
```

## 常驻（登录自启 + 卡死自恢复）

```bash
./desktop/install-agent.sh     # 安装或更新，可重复执行
```

两个 plist 是模板（`desktop/*.plist.in`），安装时按当前项目路径、`$HOME` 和 `.venv` 里的解释器渲染到 `~/Library/LaunchAgents/`，仓库里不含任何本机绝对路径。安装时若环境里有 `ASR_START_SCRIPT`，会一并写进 LaunchAgent：

```bash
ASR_START_SCRIPT=/path/to/start-all.sh ./desktop/install-agent.sh
```

装好后有两个 LaunchAgent：

| 名字 | 作用 |
| --- | --- |
| `com.kbrain.asrworkbench` | 登录即启动；进程退出后 3 秒内自动拉起 |
| `com.kbrain.asrworkbench.watchdog` | 每 30 秒请求一次 `/api/state`，连续 6 次（约 3 分钟）无响应就强杀并重启（说话人分离子进程仍在占 CPU 时跳过） |

LaunchAgent 里不再写死监听地址，登录自启时同样读 `data/network.json`，所以切换访问范围后不用重新安装。

看门狗是必要的补充：KeepAlive 只能处理进程退出，进程活着但事件循环卡死时它不会重启，网页只会一直转圈。

说话人分离（sherpa-onnx）跑在独立子进程里（`app/diarize_worker.py`），因为它在整个推理过程中一直持着 GIL，放在线程里会让整个 HTTP 服务整段冻结——7 小时级录音足够让看门狗把正常干活的进程当成假死杀掉，转写任务因此被中断。子进程化后主进程心跳稳定在毫秒级；看门狗另外会识别「说话人分离子进程仍在吃 CPU」的情况并跳过重启，重启时也会一并清掉孤儿子进程。

服务重启（改代码、看门狗重启、开机）不会作废队列：启动时排队中和进行中的任务会自动重新排队，只有源音频也被删掉的任务才标记失败。重排和手动重试都从断点继续，不会从头再跑一遍：

- 已识别完的片段留在 `transcript` 里，重跑时按 `done_until`（最后一片的结束时间）跳过，片段进度和已出文本都保留。
- 说话人分离结果缓存在 `data/jobs/<id>/turns.json`，同一份音频再跑时直接复用，省掉几十分钟的分离计算；分离失败（非零退出）时才删掉它。
- 排队中的任务在开跑前先探一次识别服务 `/health`，离线就停在这一步等待，不会直接把任务判死；最多等 30 分钟。
- 识别服务中途掉线时，当前片段先退避重试（5→60 秒），仍然连不上就改探 `/health` 等服务恢复（最长 30 分钟）；期间任务状态显示「识别服务离线，等待恢复」。
- 服务拉不起来时，队列会自己执行 `ASR_START_SCRIPT` 指向的启动脚本（每个任务最多 2 次），不需要手动开终端。
- 识别服务端口还在、但连着 5 分钟不应答（换成卡死而不是没启动），队列会杀掉该端口的进程再执行该脚本拉起来；只对本机的 `mlx` 服务这么做。

长录音的说话人分离是分段跑的（默认 30 分钟一段、段间重叠 60 秒，`DIARIZE_CHUNK_SEC` / `DIARIZE_CHUNK_OVERLAP_SEC` 可调），内存按段封顶：1 小时音频约 2 分钟、峰值约 0.6 GB；7.2 小时约 23 分钟、峰值约 1.7 GB。每段跑完存断点，被系统杀掉后重试只补没做完的段。

绕开系统限制的两处调整：`~/Documents` 受 TCC 保护，launchd 里的 `zsh` 读不到项目内的 `start.sh`，launchd 自己也打不开项目目录里的日志文件。因此主服务直接由 uv 的 `python3.12` 运行 `app.main`（`PYTHONPATH` 指向 `.venv` 的 site-packages），日志写到 `~/Library/Logs/asrWorkbench.log`，`data/workbench.log` 是指向该文件的软链接；看门狗脚本复制到 `~/Library/Application Support/asrWorkbench/` 后再由 zsh 执行。

日志：`tail -f data/workbench.log`（等同 `~/Library/Logs/asrWorkbench.log`，看门狗自身的输出在 `~/Library/Logs/asrWorkbench-watchdog.log`）。

卸载常驻：

```bash
launchctl bootout gui/$(id -u)/com.kbrain.asrworkbench.watchdog
launchctl bootout gui/$(id -u)/com.kbrain.asrworkbench
rm -f ~/Library/LaunchAgents/com.kbrain.asrworkbench*.plist
```

## 用法

1. 「上传音频」或用路径「导入」录音，列表里点条目可多选，序号即合并顺序。
2. 「合并所选音频」按序号拼接；「转写所选音频」逐个排队，结果带时间戳与 A / B 标记。
3. 任务完成后，「导出 TXT」把当前页面上的文字（含尚未点「保存修改」的改动）下载成 `.txt`，格式为 `[起 - 止] 说话人：内容`，与后端 TXT 导出一致；SRT / Markdown / JSON 按钮导出已保存的版本。浏览器只在 HTTPS 或 localhost 下允许写剪贴板，内网 `http://<本机IP>:8770` 访问时用不了「复制」，所以没做复制按钮。
4. 任务失败可以重试：任务详情里点「重试」，或在任务列表顶部点「重试失败任务（N）」批量重新排队，已识别完的片段会保留，从断点接着做。源音频已从 `data/audio/` 删除、但任务目录里还留着 16k AAC 副本（`data/jobs/<id>/audio.m4a`）时，重试直接用这份副本；两处都没有则跳过该任务。
5. 「删除所选」批量删除多选的音频：没选文件时点它会提示先点选条目（并高亮列表），选中后按钮显示「删除所选（N）」，再点弹窗确认；被排队中或进行中任务占用的音频会跳过，单个音频行上的「删除」同样如此。删除只是把文件移到 `data/trash/`（带时间戳前缀），要真正腾空间再执行 `rm -rf data/trash/*`，误删也能从这里移回 `data/audio/`。

## 结构

- `app/media.py`：ffmpeg 探测、合并、解码为 16 kHz 单声道
- `app/netcfg.py`：访问范围（localhost / lan）的读写与监听地址解析
- `app/diarize.py`：说话人分离（sherpa-onnx，模型在 `models/`）与分句
- `app/diarize_worker.py`：说话人分离的子进程入口，避免 sherpa-onnx 长时间占用 GIL 卡死服务
- `app/providers.py`：识别模型接入（本地 mlx-qwen3-asr、OpenAI 兼容接口、阿里云百炼录音文件识别）
- `app/jobs.py`：合并 / 转写任务与导出
- `data/`：音频库、任务结果、模型配置（含密钥，不要提交）
