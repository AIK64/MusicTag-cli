"""Unicode-safe entry point for Fill-Tag.bat. --check never tags audio."""
from pathlib import Path
import sys

def run():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    root = Path(__file__).resolve().parent.parent
    output = root / 'output'
    tool = root / 'music-tag-cli' / 'music_tag.py'
    config = Path('E:/音乐标签/MusicTag v1.0.9.0/MusicTag.config')
    if not tool.is_file() or not output.is_dir():
        print('未找到命令行工具或 output 文件夹，请检查脚本位置。')
        return 1
    args = ['tag', str(output), '-r']
    ncm = root / 'VipSongsDownload'
    if ncm.is_dir():
        args.extend(['--ncm-root', str(ncm)])
    if config.is_file():
        args.extend(['--musictag-config', str(config)])
    print('正在批量填充 output 及子目录中所有 MP3、FLAC 文件的标签……', flush=True)
    print('标题、艺术家、专辑保持原值，歌词和封面内嵌写入。', flush=True)
    if '--check' in sys.argv[1:]:
        print('脚本检查通过，本次不修改音乐文件。')
        print('输出目录：' + str(output))
        print('MusicTag 配置：' + (str(config) if config.is_file() else '使用默认配置'))
        return 0
    import music_tag
    sys.argv = [str(tool), *args]
    result = music_tag.main()
    if result == 0:
        print('批处理完成。成功文件本次产生的备份已自动清理。')
    elif result == 2:
        print('部分文件被跳过或需要核对，请查看上方提示。')
    else:
        music_tag.status_print('处理出现错误，请查看上方提示。','error')
    print('默认不保存报告，本次处理结果已在上方显示。')
    return result

if __name__ == '__main__':
    try:
        sys.exit(run())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as error:
        try:
            from music_tag import status_print
            status_print('错误：' + str(error),'error',file=sys.stderr)
        except ImportError:
            print('错误：' + str(error),file=sys.stderr)
        sys.exit(1)
