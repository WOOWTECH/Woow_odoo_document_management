# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""CA-23: require a recent PDF.js or an explicit eval-disabled viewer."""

from sc_ca_runtime import ScenarioError, run


IMAGE_CHECK = r'''import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
bundle = root / 'build/pdf.js'
worker = root / 'build/pdf.worker.js'
viewer = root / 'web/viewer.html'
if not all(path.is_file() and path.stat().st_size for path in (bundle, worker, viewer)):
    sys.exit(1)
source = bundle.read_text(errors='replace')
versions = re.findall(r"\bpdfjsVersion\s*=\s*['\"](\d+)\.(\d+)\.(\d+)['\"]", source)
if len(versions) == 1 and tuple(map(int, versions[0])) >= (4, 2, 67):
    sys.stdout.write('CA23_VERSION_OK\n')
    sys.exit(0)
viewer_js = root / 'web/viewer.js'
if viewer_js.is_file():
    viewer_source = viewer_js.read_text(errors='replace')
    disabled = re.search(
        r"\b(?:AppOptions|PDFViewerApplicationOptions)\.set\(\s*['\"]isEvalSupported['\"]\s*,\s*false\s*\)",
        viewer_source,
    )
    if disabled:
        sys.stdout.write('CA23_EVAL_DISABLED\n')
        sys.exit(0)
sys.exit(1)
'''


def check(sandbox):
    root = '/usr/lib/python3/dist-packages/odoo/addons/web/static/lib/pdfjs'
    output = sandbox.call('exec', 'python3', '-c', IMAGE_CHECK, root)
    if output.strip() not in {'CA23_VERSION_OK', 'CA23_EVAL_DISABLED'}:
        raise ScenarioError('pdfjs_policy_unverified')


if __name__ == '__main__':
    raise SystemExit(run('T-CA-23', check))
