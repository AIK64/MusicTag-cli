# 音乐标签命令行工具

复现你在 MusicTag v1.0.9.0 中使用的“批量自动匹配标签”工作流，无需打开 MusicTag，无需模拟点击，也不调用大模型。

## 已按你的要求设置

| 项目 | 行为 |
| --- | --- |
| 数据源 | 你当前启用的网易云音乐、QQ 音乐，默认网易云优先、QQ 其次 |
| 标题、艺术家、专辑 | 始终保留原值，不覆盖，也不自动填入这三个字段 |
| 其他字段 | 用匹配得到的非空信息覆盖：年份、音轨号、碟号、风格、专辑艺术家、作曲、作词、注释、歌词、封面 |
| 歌词、封面 | 写进音频文件标签；音乐目录中不生成 `.lrc` 或封面图片 |
| 源里没有的信息 | 保留原值，报告中列出缺项，不用空值清空 |
| 没有歌词或翻译 | 不阻止其他标签写入；歌词缺失保留原歌词，翻译缺失仍写入原文歌词；仅歌曲身份/版本匹配不确定才进入 `review` |
| 翻译歌词 | 有翻译时与原文按时间戳合并，内嵌保存 |
| 伴奏/纯音乐 | 标题含 instrumental、off vocal、伴奏、纯音乐时不抓取歌词，保留原歌词 |
| 并发 | 默认 5 个任务，请求另有速率限制；不会消耗模型 token |
| 文件格式 | MP3、FLAC；NCM 请先用现有 ncmdump 转换 |
| 写入方式 | 先在临时副本写入并验证，再备份、替换原文件；不会重新编码音频 |

说明：这是同数据源、同写入规则的独立实现，不是 MusicTag 官方 CLI，不调用其内部匹配代码。只有你当前启用的网易云和 QQ 两个源已实现；MusicTag 中其他未启用的数据源没有实现，若配置中启用它们，工具会明确报错，不会悄悄换源。公共 Web 接口可能随平台更新失效，错误会进入报告。

## 运行环境

Windows x64，Python 3.10 或更新版本。已在本机 Python 3.11.5 验证。依赖随 `vendor` 目录附带，不需要先安装包。`music-tag.cmd` 优先使用 `MUSIC_TAG_PYTHON` 指定的解释器，然后使用 `py -3`、`python`。

以下命令在本工具目录的 PowerShell 中运行。所有路径请用引号包裹。

## 一条命令批量匹配并写入

```powershell
.\music-tag.cmd tag "D:\CloudMusicDownload\output" -r
```

这条命令会处理目录及子目录内的 MP3/FLAC，覆盖允许修改的标签。默认不保存预览或执行报告，结果只在控制台显示。写入前创建完整备份，成功文件的本次备份在批处理结束后自动清理；失败文件的备份保留。

如需读取你现有 MusicTag 的数据源顺序、歌词翻译、纯音乐跳过设置和并发数：

```powershell
.\music-tag.cmd tag "D:\CloudMusicDownload\output" -r --musictag-config "E:\音乐标签\MusicTag v1.0.9.0\MusicTag.config"
```

仍然强制执行“保护三个基础字段、其他字段覆盖、全部内嵌”的规则，不会被配置里的旁置歌词等设置改变。

## 先预览，再执行

```powershell
.\music-tag.cmd plan "D:\CloudMusicDownload\output" -r --out ".\reports\my-plan.json"
.\music-tag.cmd apply ".\reports\my-plan.json"
```

`plan` 只读取音频和联网获取元数据，不修改音频。JSON 中包含匹配的歌曲 ID、专辑、时长、拟修改字段、各字段来源和缺项。`apply` 使用已缓存的计划和封面资源离线写入，不再联网。

`--out` 不能覆盖已有计划，防止覆盖未执行的预览。不指定时只在控制台显示，内部计划保存在内存中，封面临时资源在本次运行结束后自动清理。需要先保存预览再 `apply` 时，必须显式指定 `--out`。

`apply` 默认也不保存结果文件；需要时使用 `apply "my-plan.json" --out "my-result.json"`。`tag` 指定 `--out "my-plan.json"` 时，会保留预览、内部资源和 `my-plan.result.json`。

## 单文件、精确 ID、NCM 元数据

```powershell
# 单文件匹配并写入
.\music-tag.cmd tag "D:\CloudMusicDownload\output\某首歌.mp3"

# 已知原网易云数字歌曲 ID，优先精确匹配
.\music-tag.cmd tag "D:\CloudMusicDownload\output\某首歌.mp3" --id 17822715

# 关联转换前的同名 NCM，读取元数据头中的歌曲 ID；不转换或解密音频
.\music-tag.cmd tag "D:\CloudMusicDownload\output" -r --ncm-root "D:\CloudMusicDownload\VipSongsDownload"

# 只使用 QQ 源，可指定 QQ songmid
.\music-tag.cmd tag "D:\CloudMusicDownload\output\某首歌.flac" --sources qq --source qq --id 000vcpGa0HNZKm

# 读取标签，不修改文件
.\music-tag.cmd inspect "D:\CloudMusicDownload\output\某首歌.mp3"

# 查看 MusicTag 配置中启用的源
.\music-tag.cmd settings --musictag-config "E:\音乐标签\MusicTag v1.0.9.0\MusicTag.config"
```

上面的歌曲 ID 仅是用法示例，实际应使用对应歌曲的 ID；ID 对应的标题、艺术家或时长与本地不符时会拒绝该匹配。工具也会读取已有的 `163key`、`NETEASE_SONG_ID`、`QQ_SONG_MID`。匹配成功后把稳定 ID 写进自定义标签，便于下次直接定位，不改标题/艺术家/专辑。

## 如何防止版本匹配错误

优先使用已知 ID；没有 ID 时检查标题、艺术家、已有专辑和时长。仅标点、全半角、大小写差异可归一化，Live、Remastered、Deluxe 等版本信息不直接丢弃。

多个匹配、未找到匹配或关键标签缺失时，标记为 `review` 并跳过。两个源分别匹配成功，才允许从对应源补充字段；不拿另一个专辑或翻唱的歌词填进来。它比“只拿搜索第一条”更保守，可能有一些文件需要指定 ID。

## 备份、缓存、报告

- 修改前的原文件：处理期间临时保存到同目录 `.music-tag-backups`。批次结束后自动删除成功文件本次新建的备份，目录为空时一并删除；失败文件的备份以及已有历史备份保留。
- 临时写入副本：成功或失败都会尝试清理；若文件被其他程序占用导致无法删除，会在报告中说明并返回失败状态。
- 如需长期保留成功文件的备份，给 `tag` 或 `apply` 添加 `--keep-backups`。
- 不会清空整个备份目录，也不会自动删除之前批次产生的备份。
- 接口缓存：工具目录 `cache`，缓存 7 天；不读取账号密码或浏览器 Cookie。
- 运行报告：默认不生成，不会创建或写入 `reports` 目录；仅手动指定 `--out` 时保存到指定路径。已有历史报告不自动删除。
- 预览内部封面资源：默认使用系统临时目录并在运行结束后清理；显式指定 `--out` 时保留报告同名的 `.assets` 目录。这是离线执行计划所需的内部资源，播放器仍然只需要音频文件，不需要这些图片。
- 回退：关闭占用该音频的播放器，用对应 `.bak` 完整原文件替换处理后的文件并恢复原扩展名即可。请先核对备份和文件名。

程序返回码：`0` 成功/无需修改；`1` 执行失败；`2` 有待人工核对或未完成的文件。默认查看控制台输出；显式保存报告时可查看 `.result.json` 和原计划中的 `reason`、`warnings`、`unavailable`。

## 验证

```powershell
python -m unittest discover -s . -p test_music_tag.py -v
```

文件写入测试需要 PATH 中有 ffmpeg，只使用自动生成的测试音频。实际验收见 [验证报告](验证报告.md)。本次没有对你的原音乐库或转换目录中的原文件写入标签。

参考：[MusicTag 作者发布页](https://www.cnblogs.com/vinlxc/p/11347744.html)、[Mutagen ID3 文档](https://mutagen.readthedocs.io/en/latest/user/id3.html)、[Mutagen FLAC 文档](https://mutagen.readthedocs.io/en/latest/api/flac.html)。

## 终端颜色

成功写入的文件显示绿色，修改或处理失败显示红色，未修改/跳过和待处理状态保持默认白色。汇总中的成功数量、失败数量分别着色。重定向到文件或不支持颜色的终端输出纯文本；每条彩色消息后恢复默认颜色。设置 NO_COLOR 环境变量也可关闭颜色。
