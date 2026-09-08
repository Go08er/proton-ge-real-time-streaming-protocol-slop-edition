#!/usr/bin/env python3
"""Compile the selected Wine output-pull function verbatim against ownership stubs.

Use --prior to require the known missing-buffer failure in the old function.
Not a replacement for the actual MediaEngine runtime regression.
"""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wine-tree', type=Path, required=True)
    parser.add_argument('--cc', required=True)
    parser.add_argument('--prior', action='store_true')
    args = parser.parse_args()
    text = (args.wine_tree / 'dlls/mf/session.c').read_text()
    start = text.index('static HRESULT transform_node_pull_samples(')
    end = text.index('\nstatic ', start + 1)
    fixture = Path(__file__).with_name('preroll_function_fixture.c').read_text()
    fixture = fixture.replace('/* SELECTED_WINE_FUNCTION */', text[start:end])
    with tempfile.TemporaryDirectory(prefix='rtsp-preroll-function-') as temporary:
        source, binary = Path(temporary) / 'test.c', Path(temporary) / 'test'
        source.write_text(fixture)
        subprocess.run([args.cc, '-std=c11', '-Wall', '-Wextra', '-Werror',
                        str(source), '-o', str(binary)], check=True, timeout=30)
        expected = {
            'caller': (0, 4, 0, 1, 0, 0), 'provided': (0, 4, 0, 1, 0, 0),
            'ordinary': (0, 1, 0, 1, 0, 0), 'events': (0, 1, 0, 0, 1, 0),
            'alloc-fail': (-1, 1, 0, 0, 0, 0), 'partial-fail': (-1, 1, 0, 0, 0, 0),
            'change': (0, 4, 0, 1, 0, 0), 'multi': (0, 4, 0, 2, 0, 0),
            'need-input': (-2, 1, 0, 0, 0, 1),
        }
        if args.prior:
            for case in ('caller', 'provided', 'alloc-fail', 'partial-fail', 'multi'):
                expected[case] = (-4, 2, 0, 0, 0, 0)
            expected['change'] = (-4, 3, 0, 0, 0, 0)
        for case, wanted in expected.items():
            result = subprocess.run([str(binary), case], capture_output=True, text=True,
                                    check=True, timeout=5)
            actual = tuple(map(int, re.findall(r'=(-?\d+)', result.stdout)))
            assert actual == wanted, (case, actual, wanted)
            print(f'{case}: {result.stdout.strip()} PASS', flush=True)


if __name__ == '__main__':
    main()
