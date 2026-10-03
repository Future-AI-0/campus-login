# -*- mode: python ; coding: utf-8 -*-
"""精简发布：目录版和单文件版共用同一份依赖裁剪规则。"""
import argparse
import json
from pathlib import Path

project = Path(SPECPATH)
parser = argparse.ArgumentParser()
parser.add_argument('--directory', action='store_true')
options = parser.parse_args()
a = Analysis(
    [str(project / 'gui.py')], pathex=[str(project)],
    binaries=[], datas=[], hiddenimports=[], hookspath=[], hooksconfig={},
    runtime_hooks=[], excludes=[], noarchive=False, optimize=0,
)


def unused(destination):
    name = destination.replace('\\', '/').lower()
    # 普通 QWidget 界面不使用 QOpenGLWidget 或 Qt Quick。
    if name.endswith('/opengl32sw.dll'):
        return True
    # 未加载 QTranslator；应用提供中文，文件选择使用 Windows 原生对话框。
    if name.startswith('pyside6/translations/'):
        return True
    if name in ('libssl-3-x64.dll', 'libcrypto-3-x64.dll'):
        # QtNetwork 仅用于本机单实例通信。Python 的 OpenSSL 1.1 仍保留。
        return True
    if name.startswith('pyside6/plugins/'):
        if '/platforms/' in name:
            return not name.endswith(('/qwindows.dll', '/qoffscreen.dll'))
        if '/imageformats/' in name:
            return not name.endswith('/qico.dll')
        if '/tls/' in name:
            return name.endswith('/qopensslbackend.dll')
        if '/generic/' in name or '/iconengines/' in name:
            return True
    prefix = 'playwright/driver/package/'
    if name.startswith(prefix):
        relative = name[len(prefix):]
        # 类型声明、开发工具网页和非 Windows 安装脚本不参与登录。
        if relative.startswith(('types/', 'lib/vite/')) or relative.endswith('.d.ts'):
            return True
        if relative == 'readme.md' or relative.endswith('.sh'):
            return True
    return False


removed = [item for item in a.binaries + a.datas if unused(item[0])]
a.binaries = [item for item in a.binaries if not unused(item[0])]
a.datas = [item for item in a.datas if not unused(item[0])]

# 打包前统一 VC runtime，避免 Python 3.10 的旧 DLL 先加载。
qt_runtime = project / '.venv' / 'Lib' / 'site-packages' / 'PySide6'
for index, item in enumerate(a.binaries):
    basename = Path(item[0]).name
    if basename.lower() in ('vcruntime140.dll', 'vcruntime140_1.dll'):
        replacement = qt_runtime / basename
        if replacement.exists():
            a.binaries[index] = (item[0], str(replacement), item[2])

report = {
    'removed_files': len(removed),
    'removed_bytes': sum(Path(item[1]).stat().st_size for item in removed if Path(item[1]).is_file()),
    'embedded_files': len(a.binaries) + len(a.datas),
    'embedded_bytes': sum(Path(item[1]).stat().st_size for item in a.binaries + a.datas if Path(item[1]).is_file()),
    'removed_names': [item[0] for item in removed],
}
report_path = project / 'build' / 'slim-size-report.json'
report_path.parent.mkdir(parents=True, exist_ok=True)
report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(f"Trimmed {report['removed_files']} unused files ({report['removed_bytes'] / 2**20:.1f} MiB).")

pyz = PYZ(a.pure)
exe_options = dict(
    name='CampusLogin', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=False,
    argv_emulation=False, target_arch=None, codesign_identity=None,
    entitlements_file=None,
)
if options.directory:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **exe_options)
    coll = COLLECT(exe, a.binaries, a.datas, name='CampusLoginSlim', strip=False, upx=False)
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **exe_options)
