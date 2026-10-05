"""Build clean distributions from an explicit source allowlist, never local data."""
import argparse
import hashlib
import re
import tempfile
import urllib.request
import zipfile
from pathlib import Path

MODULES = ('server.py', 'collector.py', 'adapters.py', 'analytics.py', 'performance.py',
    'codex_tasks.py', 'codex_auxiliary.py', 'deepseek_harness.py', 'official_usage.py', 'reconciliation.py')
COMMON = (*MODULES, 'public/index.html', 'public/app.js', 'public/style.css',
    'launch_dashboard.ps1', 'stop_dashboard.ps1', 'windows_common.ps1')
SOURCE = (*COMMON, '启动看板.cmd', '停止看板.cmd', 'README.md', '.gitignore', '.gitattributes', 'CHANGELOG.md', 'tools/build_release.py')
RUNTIME_URL = 'https://www.python.org/ftp/python/3.14.7/python-3.14.7-embed-amd64.zip'
RUNTIME_SHA256 = 'd297e5ff019966817ad8502465176139f2d3d840fa4ed84b13bed399a6ab1f15'
GUIDE = '''# Token 看板 · Windows 64 位便携版

适用 Windows 10/11 64 位。先解压整个 TokenMeter 文件夹，再双击“启动看板.cmd”，浏览器会打开 http://127.0.0.1:18741/ 。内置官方 Python 3.14.7，无需安装 Python，也不需要原作者的账户或计划任务。

首次启动会在本文件夹创建 data/，读取当前 Windows 用户的 Codex、WorkBuddy、ZCode、MiMo 和 DeepSeek Harness 留存的用量日志。没有日志或日志格式不兼容的软件会显示未发现/未知，不会捏造数据。Codex 账户对账还需要当前电脑安装支持接口的官方 Codex，并由使用者自己登录；不支持时，本机日志统计仍能使用。

关闭浏览器后服务仍在后台采集。双击“停止看板.cmd”停止这份看板；数据保留。再次启动自动读入新增记录。默认不创建开机任务；需要自动启动时，可按 Win+R 输入 shell:startup，把启动入口的快捷方式放进去。

根目录只需关注启动、停止、data/ 和本说明。app/ 是程序与运行环境，保留完整。不要放在压缩包内部或无写权限的系统目录运行，也不要在多个目录同时启动同一端口。停止后可以移动文件夹；如果自己加了开机快捷方式，要更新其路径。

本分享包不包含原作者的统计数据库、官方缓存、账户指纹、登录文件、API Key、聊天内容、导出或个人配置。它只读取收件人电脑的日志；官方接口由收件人的官方 Codex 使用其现有登录。页面只监听 127.0.0.1，本机统计不上传。官方对账会向官方服务读取该使用者的账户用量。

你的使用数据保存在 data/，不要把用过的整个文件夹再转发。转发原始 ZIP；若要清理，先停止，再删除 data/。源软件日志还在时，之后会重新索引历史。3 秒刷新不会创建一份新的完整快照。

本项目采用 MIT 许可证，见根目录 LICENSE。统计口径与计划任务原理见 app/README.md。Python 的许可在 app/runtime/LICENSE.txt；官方运行环境来源和校验值见 app/RUNTIME.txt。
'''

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def build(root, output, runtime_archive=None):
    root, output = root.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_files = SOURCE + (('LICENSE',) if (root / 'LICENSE').is_file() else ())
    for name in source_files:
        path = root / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Missing or unsafe source: ' + name)
        text = path.read_text(encoding='utf-8-sig')
        if re.search(r'(?<![A-Za-z0-9_])[A-Za-z]:[\\/]', text):
            raise ValueError('Machine-specific content: ' + name)
    with tempfile.TemporaryDirectory(prefix='token-meter-build-') as temporary:
        runtime = Path(runtime_archive) if runtime_archive else Path(temporary) / 'runtime.zip'
        if not runtime_archive:
            with urllib.request.urlopen(RUNTIME_URL, timeout=45) as response:
                runtime.write_bytes(response.read())
        if sha(runtime) != RUNTIME_SHA256:
            raise ValueError('Official Python runtime checksum mismatch')
        portable = output / 'TokenMeter-Windows-x64.zip'
        with zipfile.ZipFile(portable, 'w', zipfile.ZIP_DEFLATED) as bundle:
            for name in COMMON:
                bundle.write(root / name, 'TokenMeter/app/' + name)
            portable_readme = (root / 'README.md').read_text(encoding='utf-8').replace('[MIT 许可证](LICENSE)', '[MIT 许可证](../LICENSE)')
            bundle.writestr('TokenMeter/app/README.md', portable_readme)
            if (root / 'LICENSE').is_file():
                bundle.write(root / 'LICENSE', 'TokenMeter/LICENSE')
            bundle.writestr('TokenMeter/启动看板.cmd', '@echo off\r\npowershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0app\\launch_dashboard.ps1" -Open -DataDirectory "%~dp0data"\r\nexit /b %errorlevel%\r\n')
            bundle.writestr('TokenMeter/停止看板.cmd', '@echo off\r\npowershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\\stop_dashboard.ps1"\r\npause\r\n')
            bundle.writestr('TokenMeter/使用说明.md', GUIDE)
            bundle.writestr('TokenMeter/app/RUNTIME.txt', 'Official CPython 3.14.7 embeddable distribution for Windows x64\nSource: ' + RUNTIME_URL + '\nSHA256: ' + RUNTIME_SHA256 + '\nOnly python314._pth changed to add relative parent directory for application imports.\n')
            with zipfile.ZipFile(runtime) as interpreter:
                for entry in interpreter.infolist():
                    if entry.is_dir():
                        continue
                    if Path(entry.filename).name != entry.filename:
                        raise ValueError('Unexpected runtime archive path')
                    raw = interpreter.read(entry)
                    if entry.filename == 'python314._pth':
                        raw = b'python314.zip\n.\n..\n# import site\n'
                    bundle.writestr('TokenMeter/app/runtime/' + entry.filename, raw)
        source = output / 'TokenMeter-Source.zip'
        with zipfile.ZipFile(source, 'w', zipfile.ZIP_DEFLATED) as bundle:
            for name in source_files:
                bundle.write(root / name, 'token-meter/' + name)
        for path in (portable, source):
            with zipfile.ZipFile(path) as bundle:
                assert bundle.testzip() is None
                assert not any('/data/' in name or name.endswith(('.sqlite', '.sqlite3', '.log')) for name in bundle.namelist())
        (output / 'SHA256SUMS.txt').write_text(''.join(sha(path) + '  ' + path.name + '\n' for path in (portable, source)), encoding='ascii')
        return portable, source

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--runtime-archive', type=Path)
    options = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    for artifact in build(project, options.output or project.parent / 'token-meter-distribution', options.runtime_archive):
        print(artifact.name, artifact.stat().st_size, 'bytes')
