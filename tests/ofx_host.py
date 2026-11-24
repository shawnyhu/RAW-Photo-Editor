"""Exercise the real external OFX plugin through its isolated native host.

Run with a Python containing numpy: python tests/ofx_host.py /path/to/spektrafilm.ofx.bundle
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'blender_raw/vendor/spektrafilm_ofx' / (
    'photo_ofx_host.exe' if sys.platform == 'win32' else 'photo_ofx_host')


def main():
    bundle = Path(sys.argv[1]).resolve()
    environment = dict(os.environ, BPE_OFX_PARENT_PID=str(os.getpid()))
    with tempfile.TemporaryDirectory(prefix='OFX 测试 ') as directory:
        work = Path(directory)
        # A first-run private config starts the official plugin's update notifier;
        # successful helper shutdown must not wait for its online request.
        environment['APPDATA'] = str(work / 'config')
        metadata = work / 'descriptor.json'
        subprocess.run([str(HELPER), 'describe', str(bundle), str(metadata)], env=environment, check=True, timeout=120)
        schema = json.loads(metadata.read_text(encoding='utf8'))
        fields = {p['name']: p for p in schema['parameters']}
        assert schema['plugin'] == 'org.spektrafilm'
        assert fields['film']['options'] and fields['paper']['options']
        assert 'lensDistortionEnabled' in fields
        assert fields['filmExposureEv']['minimum'] < 0 < fields['filmExposureEv']['maximum']
        preset_request = work / 'preset.json'
        preset_request.write_text(json.dumps(dict(parameters=dict(quickPresetCategory=1,
                                  quickPresetSelection=0, inputColorSpace=15, rcmInputColorSpace=7))), encoding='utf8')
        subprocess.run([str(HELPER), 'describe', str(bundle), str(work / 'preset-result.json'),
                        str(preset_request)], env=environment, check=True, timeout=15)
        linear = fields['inputColorSpace']['options'].index('Linear Rec.709')
        srgb = fields['sdrOutputColorSpace']['options'].index('sRGB')
        image = np.ones((48, 64, 4), dtype=np.float32)
        image[..., :3] = np.linspace(0.01, 1.2, 48)[:, None, None]
        source, output = work / 'input.f32', work / 'output.f32'
        image.tofile(source)

        def render(overrides=None, success=True):
            request = dict(width=64, height=48, frame=4.25,
                           parameters=dict(inputColorSpace=linear, sdrOutputColorSpace=srgb))
            request['parameters'].update(overrides or {})
            request['result_metadata'] = str(work / 'effective.json')
            path = work / 'request.json'
            path.write_text(json.dumps(request), encoding='utf8')
            result = subprocess.run([str(HELPER), 'render', str(bundle), str(path),
                                     str(source), str(output)], env=environment, capture_output=True, timeout=600)
            assert (result.returncode == 0) == success, result.stderr.decode(errors='replace')
            if success:
                data = np.fromfile(output, dtype=np.float32).reshape(image.shape)
                assert np.isfinite(data).all()
                return data.copy()
            assert result.stderr

        baseline = render()
        repeat = render()
        assert np.array_equal(baseline, repeat), 'deterministic render changed'
        assert baseline[0, 20, :3].mean() < baseline[-1, 20, :3].mean(), 'row order changed'
        exposure = render({'filmExposureEv': 1.0})
        assert np.max(np.abs(baseline - exposure)) > 0.01, 'film exposure had no effect'
        print_exposure = render({'printExposureEv': 1.0})
        assert np.max(np.abs(baseline - print_exposure)) > 0.01, 'print exposure had no effect'
        render({'nonexistentParameter': 1}, success=False)
        render({'filmExposureEv': 9999}, success=False)
        for size in (33, 65):
            lut_work = work / f'lut-{size}'
            lut_work.mkdir()
            lut_request = lut_work / 'request.json'
            lut_request.write_text(json.dumps(dict(lut_size=size, parameters=dict(
                inputColorSpace=linear, sdrOutputColorSpace=srgb))), encoding='utf8')
            cube = work / f'look-{size}.cube'
            subprocess.run([str(HELPER), 'export-lut', str(bundle), str(lut_request), str(cube)],
                           env=environment, check=True, capture_output=True, timeout=600)
            text = cube.read_text(encoding='utf8')
            assert f'LUT_3D_SIZE {size}' in text
            rows = [line for line in text.splitlines()
                    if line and (line[0].isdigit() or line[0] in '-+.')]
            assert len(rows) == size ** 3
            values = np.array([list(map(float, line.split())) for line in rows])
            assert values.shape == (size ** 3, 3) and np.isfinite(values).all()
            assert cube.with_suffix('.cube.LICENSE.txt').read_bytes() == (
                bundle / 'Contents/Resources/Legal/SPEKTRAFILM_OFX_LUT_LICENSE.txt').read_bytes()
        if sys.platform == 'win32':
            # The native Windows watchdog holds a parent process identity handle.
            # This short-lived sentinel is deliberately independent of the test.
            sentinel = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            helper = None
            try:
                request = work / 'request.json'
                request.write_text(json.dumps(dict(width=64, height=48, parameters={
                    'inputColorSpace': linear, 'sdrOutputColorSpace': srgb})), encoding='utf8')
                helper = subprocess.Popen([str(HELPER), 'render', str(bundle), str(request),
                                           str(source), str(output)],
                                          env=dict(os.environ, BPE_OFX_PARENT_PID=str(sentinel.pid)),
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                time.sleep(0.1)
                sentinel.kill()
                sentinel.wait(timeout=5)
                helper.communicate(timeout=5)
                assert helper.returncode == 125, f'parent watchdog returned {helper.returncode}'
            finally:
                if sentinel.poll() is None:
                    sentinel.kill()
                sentinel.wait(timeout=5)
                if helper is not None and helper.poll() is None:
                    helper.kill()
                    helper.wait(timeout=5)
        print('OFX HOST PASS', schema['version'], len(schema['parameters']),
              'metadata, presets, repeatability, orientation, exposures, validation, Unicode, LUT33/65, parent watchdog')


if __name__ == '__main__':
    main()
