#!/usr/bin/env python3
"""Buffer-ownership model and source contract, not a Wine runtime test.

The actual prior-failing regression is progressive-http-vod/run_hls_clock.py.
These small checks cover the allocation/cleanup cases its AAC route cannot.
"""
import argparse
from pathlib import Path
import unittest

WINE_TREE = None


def drain(*, replenish=True, provided=False, early=3, events=False,
          fail_allocation=0, need_input=False):
    allocations = releases = calls = delivered = 0
    sample, stale = None, False
    while True:
        if calls == 0 or replenish:
            if not provided:
                allocations += 1
                if allocations == fail_allocation:
                    return 'allocation-failed', calls, releases, delivered
                sample = object()
            stale = False
        calls += 1
        if not provided and sample is None:
            return 'missing-buffer', calls, releases, delivered
        if stale:
            return 'stale-status', calls, releases, delivered
        if need_input:
            # Caller-owned sample is cached by the existing NEED_MORE_INPUT path.
            return 'need-input', calls, releases, delivered
        if provided:
            sample = object()
        if calls <= early:
            sample = None
            releases += 1
            stale = True
            if not events:
                continue
        else:
            delivered += 1
            releases += 1
        return 'events' if events else 'delivered', calls, releases, delivered


class PrerollBuffers(unittest.TestCase):
    def test_old_caller_allocated_path_loses_its_buffer(self):
        self.assertEqual(drain(replenish=False), ('missing-buffer', 2, 1, 0))

    def test_old_transform_owned_path_retains_no_sample_status(self):
        self.assertEqual(drain(replenish=False, provided=True), ('stale-status', 2, 1, 0))

    def test_many_discarded_samples_are_replenished(self):
        for provided in (False, True):
            self.assertEqual(drain(provided=provided), ('delivered', 4, 4, 1))

    def test_ordinary_output_does_not_add_an_extra_call(self):
        self.assertEqual(drain(early=0), ('delivered', 1, 1, 1))

    def test_events_are_not_discarded_by_the_drain_loop(self):
        self.assertEqual(drain(events=True), ('events', 1, 1, 0))

    def test_replenishment_failure_ends_without_another_output_call(self):
        self.assertEqual(drain(fail_allocation=2), ('allocation-failed', 1, 1, 0))

    def test_need_input_is_not_retried(self):
        self.assertEqual(drain(need_input=True), ('need-input', 1, 0, 0))

    def test_source_contract(self):
        if WINE_TREE is None:
            patch = (Path(__file__).resolve().parents[2] /
                     'patches/ge-proton11-6-a323/0026-mf-replenish-output-buffers-after-preroll.patch').read_text()
            source = '\n'.join(line[1:] for line in patch.splitlines()
                               if line.startswith((' ', '+')) and not line.startswith('+++'))
        else:
            text = (WINE_TREE / 'dlls/mf/session.c').read_text()
            source = text.split('static HRESULT transform_node_pull_samples(', 1)[1].split('\nstatic ', 1)[0]
        loop = source.index('for (;;)')
        allocate = source.index('allocate_output_samples(', loop)
        output = source.index('IMFTransform_ProcessOutput(', allocate)
        predicate = source.index('if (hr != S_OK || !transform_node_markin_need_more_input(', output)
        release = source.index('release_output_samples(node, buffers);', predicate)
        reset = source.index('memset(buffers, 0,', release)
        self.assertTrue(loop < allocate < output < predicate < release < reset)
        self.assertIn('goto done;', source[allocate:output])
        self.assertIn('status = 0;', source[allocate:output])
        self.assertIn('break;', source[predicate:release])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wine-tree', type=Path)
    args, rest = parser.parse_known_args()
    WINE_TREE = args.wine_tree
    unittest.main(argv=[__file__, *rest])
