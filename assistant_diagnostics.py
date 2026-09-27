"""Local, bounded diagnostics. Never reads credentials or game saves."""
import os
import json
import re
from pathlib import Path
from assistant_paths import installation


def current_bridge_error(root):
    """Read only the known bridge log suffix written during this launch."""
    try:
        launch = json.loads((Path(root) / 'launch-latest.json').read_text(encoding='utf-8'))
        game = installation().game
        if game is None:
            return ''
        path = Path(game) / 'communication_mod_errors.log'
        if Path(launch.get('bridge_stderr_path', '')).resolve() != path.resolve():
            return ''
        offset = launch.get('bridge_stderr_start_offset')
        if type(offset) is not int or offset < 0:
            return ''
        with path.open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            if size <= offset:
                return ''
            stream.seek(max(offset, size - 6000))
            return stream.read(6000).decode('utf-8', errors='replace')
    except (OSError, ValueError, TypeError, AttributeError):
        return ''


def tail(path, limit=12000):
    try:
        with Path(path).open('rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - limit))
            return stream.read(limit).decode('utf-8', errors='replace')
    except OSError:
        return '（日志不可读取或尚未生成）'


def redact(text, roots=()):
    for value in (*roots, os.environ.get('USERPROFILE', '')):
        if value:
            text = re.sub(re.escape(str(value)), '<本地目录>', text, flags=re.I)
    text = re.sub(r'(?i)[a-z]:[\\/]Users[\\/][^\\/\s"]+', '<用户目录>', text)
    text = re.sub(r'(?i)(Bearer\s+)[^\s"]+', r'\1<已隐藏>', text)
    text = re.sub(r'''(?i)((?:api[_-]?key|token|password|secret)["']?\s*[:=]\s*["']?)[^\s,"']+''', r'\1<已隐藏>', text)
    text = re.sub(r'sk-[A-Za-z0-9_-]{12,}', '<已隐藏>', text)
    return text


def collect(root, data, mode=None, error=''):
    root, data = Path(root), Path(data)
    parts = ['尖塔助手诊断信息', '模式：' + str(mode or '尚未启动'), '错误：' + str(error)]
    names = [data / 'auto-start.log'] if mode == 'auto' else [data / 'advisor-game.log']
    if mode == 'auto':
        try:
            logs = list((root / 'logs/campaign-starts').glob('campaign-*.log'))
            if logs:
                names.append(max(logs, key=lambda p: p.stat().st_mtime_ns))
        except OSError:
            pass
    for path in names:
        parts.extend(['--- ' + path.name + ' ---', tail(path)])
    if mode == 'auto':
        bridge_error = current_bridge_error(root)
        if bridge_error:
            parts.extend(['--- 本次通信进程错误 ---', bridge_error])
    return redact('\n'.join(parts), (root, data))
