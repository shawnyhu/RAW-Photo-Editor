"""Validate the real Apple Silicon helper and externally supplied official OFX.

Hosted Mac runners can lack a Metal device. Always require real plugin loading;
report that restriction explicitly and render only when a device is available.
"""
import hashlib
import argparse
import importlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--installed', action='store_true')
parser.add_argument('bundle', type=Path)
parser.add_argument('report', type=Path)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else sys.argv[1:])
if args.installed:
    import bpy
    package = 'bl_ext.user_default.raw_photo_editor'
    assert package in bpy.context.preferences.addons, 'Install and enable the ZIP before running this test'
    engine = importlib.import_module(f'{package}.spektrafilm_ofx_engine')
    assert Path(engine.__file__).resolve().parent != ROOT / 'blender_raw', engine.__file__
else:
    sys.path.insert(0, str(ROOT / 'blender_raw'))
    import spektrafilm_ofx_engine as engine

assert sys.platform == 'darwin' and platform.machine() == 'arm64'
bundle, report_path = args.bundle, args.report
helper = engine._helper()
report = {'source_commit': os.environ.get('GITHUB_SHA'), 'platform': platform.platform(),
          'installed_extension': args.installed,
          'helper_sha256': hashlib.sha256(helper.read_bytes()).hexdigest()}
try:
    if args.installed:
        # Reproduce ZIP extractors dropping POSIX execute permission on the
        # installed binary. The real describe process must restore it itself.
        helper.chmod(helper.stat().st_mode & ~0o111)
        assert not os.access(helper, os.X_OK)
    assert not engine.backend_error(str(bundle)), engine.backend_error(str(bundle))
    metadata = engine.describe(str(bundle))
    if args.installed:
        assert os.access(helper, os.X_OK), 'Installed host execute permission was not restored'
        assert hashlib.sha256(helper.read_bytes()).hexdigest() == report['helper_sha256']
        report['execute_permission_restored'] = True
    fields = {p['name']: p for p in metadata['parameters']}
    hints = [p for p in fields.values() if p.get("hint")]
    assert hints and engine.parameter_help(hints[0]["name"], str(bundle)) == hints[0]["hint"]
    looks = engine.builtin_looks(str(bundle))
    assert len(looks) == 60 and len({(c, i) for c, i, _, _ in looks}) == len(looks)
    report.update(local_hints=len(hints), local_presets=len(looks))
    assert metadata['plugin'] == 'org.spektrafilm'
    assert fields['film']['options'] and fields['paper']['options']
    assert 'lensDistortionEnabled' in fields and 'filmExposureEv' in fields
    report.update(plugin_loaded=True, parameters=len(fields),
                  version=fields['infoVersion'].get('value', fields['infoVersion'].get('default')))
    with tempfile.TemporaryDirectory(prefix='metal-probe-') as temp:
        source, probe = Path(temp) / 'probe.mm', Path(temp) / 'probe'
        source.write_text('#import <Metal/Metal.h>\nint main() { @autoreleasepool { return MTLCreateSystemDefaultDevice() ? 0 : 77; } }\n')
        subprocess.run(['clang++', '-arch', 'arm64', '-framework', 'Metal', '-framework', 'Foundation',
                        str(source), '-o', str(probe)], check=True)
        status = subprocess.run([str(probe)], check=False).returncode
        assert status in (0, 77), status
    report['metal_available'] = status == 0
    if status == 0:
        image = np.ones((32, 64, 4), np.float32)
        image[..., :3] = np.linspace(.01, .8, 64)[None, :, None]
        result = engine.render_rgba(image, parameters={'grainEnabled': False}, plugin_path=str(bundle))
        assert np.isfinite(result).all() and result.shape == image.shape
        assert np.ptp(result[..., :3]) > .05
        changed = engine.render_rgba(image, parameters={'grainEnabled': False, 'filmExposureEv': 1.0},
                                     plugin_path=str(bundle))
        assert np.max(np.abs(changed - result)) > .01
        report['render_passed'] = True
    else:
        report['render_passed'] = None
        report['render_limitation'] = 'Hosted runner has no Metal device; physical-Mac GPU validation is pending.'
    print(json.dumps(report, indent=2), flush=True)
except BaseException as error:
    report['error'] = str(error)
    raise
finally:
    report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
