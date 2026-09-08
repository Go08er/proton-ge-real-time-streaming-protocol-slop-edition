#!/usr/bin/env python3
"""Exercise callPackage-style argument injection without downloading or building."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which('nix-instantiate'), 'Nix evaluator required')
class PackageArguments(unittest.TestCase):
    def evaluate(self, override='{}'):
        # Nixpkgs includes a package called src; optional arguments are still
        # automatically injected by callPackage when their names match it.
        expr = '''let
          fn = import %s/nix/prebuilt.nix;
          scope = {
            lib = {};
            stdenvNoCC.mkDerivation = attrs: attrs;
            fetchurl = attrs: attrs;
            src = throw "unrelated Nixpkgs src package was injected";
          };
          args = builtins.intersectAttrs (builtins.functionArgs fn) scope;
        in (fn (args // %s)).src
        ''' % (ROOT, override)
        result = subprocess.run(['nix-instantiate', '--eval', '--strict', '--json',
                                 '--expr', expr], text=True, capture_output=True,
                                timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_default_uses_pinned_release_not_nixpkgs_src(self):
        result = self.evaluate()
        self.assertTrue(result['url'].endswith('/se1/proton-ge-11-6-rtsp-se1.tar.gz'))
        self.assertEqual(result['hash'], 'sha256-1FCWrUfiSJEptitUF1F2kUVV1j9N1bi0tYwctCuciJU=')

    def test_explicit_local_archive(self):
        self.assertEqual(self.evaluate('{ localArchive = "synthetic-local-archive"; }'),
                         'synthetic-local-archive')


if __name__ == '__main__':
    unittest.main(verbosity=2)
