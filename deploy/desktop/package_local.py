"""Build a desktop upgrade archive without config, credentials or project data."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import tomllib
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    version = tomllib.loads((source.parents[1] / 'pyproject.toml').read_text())['project']['version']
    if args.wheel.name != f'hoh_control_plane-{version}-py3-none-any.whl':
        raise ValueError('Wheel must match the source release version')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='hoh-upgrade-bundle-') as scratch:
        bundle = Path(scratch)
        shutil.copy2(args.wheel, bundle / args.wheel.name)
        for name in (
            'hoh_desktop.py', 'start-host.ps1', 'upgrade_local.py',
            'verify_shared_profiles.py', 'README.md',
        ):
            shutil.copy2(source / name, bundle / name)
        files = {path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in bundle.iterdir()}
        (bundle / 'manifest.json').write_text(json.dumps({'version':version, 'wheel':args.wheel.name, 'files':files}, indent=2))
        with zipfile.ZipFile(args.output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for path in bundle.iterdir():
                archive.write(path, path.name)
    checksum = hashlib.sha256(args.output.read_bytes()).hexdigest()
    args.output.with_suffix(args.output.suffix + '.sha256').write_text(checksum + '  ' + args.output.name + '\n')
    print(json.dumps({'archive':str(args.output), 'sha256':checksum}))


if __name__ == '__main__':
    main()
