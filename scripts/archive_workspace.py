"""Preview, archive, or restore known obsolete workspace artifacts.

Run from any directory. No source, current release, active state, logs, or keys
are selected. Moves stay within the repository; every move is journaled.
"""
import argparse
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]


def inside(root, path):
    path = path.resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError('Path is outside the workspace: ' + str(path))
    return path


def candidates(root):
    selected = set()
    for pattern in ('.tmp_*.py', 'build-java-*', 'CommunicationMod*.jar*',
                    '*.pre-*', 'decision-case-*.tmp', '.decision-case-*.tmp',
                    'freeze-build.*.tmp', 'release-preflight.json.tmp'):
        selected.update(root.glob(pattern))
    selected.discard(root / 'CommunicationMod.jar')
    for name in ('CommunicationMod-v1.2.1.zip', 'spirecomm-master.zip',
                 'screen.png', 'screen2.png', 'full-test-output.txt', 'wheel-javap.txt'):
        if (root / name).is_file():
            selected.add(root / name)
    # Only archive old distributions when a valid latest-release marker exists.
    marker = root / 'dist/latest-assistant-release.json'
    if marker.is_file():
        latest = json.loads(marker.read_text(encoding='utf-8'))
        current_zip = Path(latest['zip']).name
        current_name = current_zip.removesuffix('.zip')
        dist = root / 'dist'
        if not (dist / current_zip).is_file() or not (dist / current_name).is_dir():
            raise ValueError('Latest release marker does not identify an existing package')
        keep = {current_name, current_zip, current_name+'.sha256', current_zip+'.sha256'}
        selected.update(p for p in dist.glob('SpireAssistant-Windows-*') if p.name not in keep)
    for p in selected:
        if p.is_symlink() or p.is_junction():
            raise ValueError('Refusing to archive a link: '+str(p))
        inside(root, p)
    return sorted(selected)


def write_journal(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def archive(root, paths):
    base = inside(root, root/'.local-archive'/time.strftime('%Y%m%d-%H%M%S'))
    base.mkdir(parents=True, exist_ok=False)
    journal = base/'moves.json'
    data = {'version': 1, 'moves': [
        {'source': p.relative_to(root).as_posix(),
         'destination': (base/p.relative_to(root)).relative_to(root).as_posix(),
         'status': 'pending'} for p in paths]}
    write_journal(journal, data)
    for row in data['moves']:
        source = inside(root, root/row['source'])
        target = inside(root, root/row['destination'])
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        row['status'] = 'archived'
        write_journal(journal, data)
    return journal


def restore(root, journal):
    journal = inside(root, journal)
    if not journal.is_relative_to(root/'.local-archive'):
        raise ValueError('Restore requires a journal in .local-archive')
    data = json.loads(journal.read_text(encoding='utf-8'))
    for row in reversed(data['moves']):
        source = inside(root, root/row['destination'])
        target = inside(root, root/row['source'])
        if row['status'] == 'restored' or not source.exists():
            continue
        if not source.is_relative_to(journal.parent):
            raise ValueError('Archive item is outside its journal directory')
        if target.exists():
            raise FileExistsError('Refusing to overwrite: '+str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        row['status'] = 'restored'
        write_journal(journal, data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--restore', type=Path)
    args = parser.parse_args()
    if args.restore:
        restore(ROOT, args.restore.resolve())
        print('Archive restored; existing files were not overwritten.')
    else:
        paths = candidates(ROOT)
        for p in paths:
            print(p.relative_to(ROOT))
        print('Candidates:', len(paths))
        if args.apply and paths:
            print('Recovery journal:', archive(ROOT, paths))
        elif not args.apply:
            print('Preview only. Close game/controller processes before using --apply.')


if __name__ == '__main__':
    main()
