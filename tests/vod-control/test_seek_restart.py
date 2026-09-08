#!/usr/bin/env python3
"""Model the seek-restart demand race; this does not execute Wine C code.

A3.19 mf/session.c flushes transform request counters before async source
Start. A sink request during Start can recreate a transform request while
WineDMO's stream is inactive. WRONGSTATE leaves that transform request
outstanding, so the existing completion re-prime sees had_requests=True and
does not pull upstream. The proposed guard retains sink credit until restart
completion instead of submitting that premature request. No retry is modeled.
"""

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys
import unittest


WINE_TREE = None
SEEK_RESTART_GUARD = (
    "if (session->command_state == COMMAND_STATE_RESTARTING_SOURCES "
    "|| (session->command_state == COMMAND_STATE_STARTING_SOURCES "
    "&& (session->presentation.flags & SESSION_FLAG_RESTARTING))) return;"
)


@dataclass
class RestartDemand:
    guard: bool
    command: str = "complete"
    restarting: bool = False
    source_active: bool = True
    sink_requests: int = 0
    transform_requests: int = 0
    source_attempts: int = 0
    accepted_requests: int = 0
    wrong_state: int = 0
    sink_flushes: int = 0

    def request_sample(self) -> None:
        # session_request_sample: credit is retained before deciding whether
        # to enter the upstream chain. Initial start/preroll are not gated.
        self.sink_requests += 1
        if self.guard and (
            self.command == "restarting_sources"
            or (self.command == "starting_sources" and self.restarting)
        ):
            return

        # session_request_sample_from_node, synchronous transform branch:
        # a pending request coalesces demand rather than pulling again.
        had_requests = bool(self.transform_requests)
        self.transform_requests += 1
        if had_requests:
            return
        self.source_attempts += 1
        if self.source_active:
            self.accepted_requests += 1
        else:
            # transform_node_deliver_samples only warns; it returns void.
            # Neither transform nor sink credit is rolled back here.
            self.wrong_state += 1

    def stop_for_seek(self) -> None:
        self.command = "restarting_sources"
        self.source_active = False

    def finish_stop(self) -> None:
        self.command = "starting_sources"
        self.restarting = True
        self.transform_requests = 0

    def finish_start(self) -> None:
        self.source_active = True
        # The existing completion clears this BEFORE session_flush_sinks.
        self.restarting = False
        if self.sink_requests:
            self.sink_requests -= 1
            self.request_sample()
        self.sink_flushes += 1
        self.command = "complete"

    def fail_start(self) -> None:
        # Narrowly model completion of a recognized terminal source error:
        # no successful-start callback, no re-prime, no timer/retry.
        self.command = "complete"
        self.source_active = False


class SeekRestartDemandTests(unittest.TestCase):
    def test_legacy_request_during_start_strands_the_pull_chain(self) -> None:
        model = RestartDemand(guard=False)
        model.stop_for_seek()
        model.finish_stop()
        model.request_sample()
        self.assertEqual((model.source_attempts, model.accepted_requests), (1, 0))
        self.assertEqual(model.wrong_state, 1)
        model.finish_start()
        self.assertEqual(model.sink_requests, 1)
        self.assertEqual(model.transform_requests, 2)
        self.assertEqual((model.source_attempts, model.accepted_requests), (1, 0))

    def test_guard_defers_start_gap_demand_until_source_ready(self) -> None:
        model = RestartDemand(guard=True)
        model.stop_for_seek()
        model.finish_stop()
        model.request_sample()
        self.assertEqual(model.sink_requests, 1)
        self.assertEqual((model.transform_requests, model.source_attempts), (0, 0))
        model.finish_start()
        self.assertEqual((model.sink_requests, model.transform_requests), (1, 1))
        self.assertEqual((model.source_attempts, model.accepted_requests), (1, 1))
        self.assertEqual(model.wrong_state, 0)

    def test_guard_also_defers_requests_while_sources_are_stopping(self) -> None:
        model = RestartDemand(guard=True)
        model.stop_for_seek()
        model.request_sample()
        self.assertEqual((model.sink_requests, model.source_attempts), (1, 0))
        model.finish_stop()
        model.finish_start()
        self.assertEqual(model.accepted_requests, 1)
        self.assertEqual(model.wrong_state, 0)

    def test_multiple_callbacks_preserve_credit_but_prime_only_once(self) -> None:
        model = RestartDemand(guard=True)
        model.stop_for_seek()
        for _ in range(3):
            model.request_sample()
        model.finish_stop()
        for _ in range(4):
            model.request_sample()
        self.assertEqual(model.source_attempts, 0)
        model.finish_start()
        self.assertEqual(model.sink_requests, 7)
        self.assertEqual(model.transform_requests, 1)
        self.assertEqual((model.source_attempts, model.accepted_requests), (1, 1))

    def test_zero_demand_is_not_fabricated(self) -> None:
        model = RestartDemand(guard=True)
        model.stop_for_seek()
        model.finish_stop()
        model.finish_start()
        self.assertEqual((model.sink_requests, model.transform_requests), (0, 0))
        self.assertEqual((model.source_attempts, model.accepted_requests), (0, 0))
        self.assertEqual(model.sink_flushes, 1)

    def test_existing_outstanding_demand_still_reprimes_without_gap_callback(self) -> None:
        for guard in (False, True):
            with self.subTest(guard=guard):
                model = RestartDemand(guard=guard)
                model.request_sample()
                model.stop_for_seek()
                model.finish_stop()
                model.finish_start()
                self.assertEqual((model.sink_requests, model.transform_requests), (1, 1))
                self.assertEqual(model.accepted_requests, 2)

    def test_ordinary_transform_demand_remains_coalesced(self) -> None:
        model = RestartDemand(guard=True)
        model.request_sample()
        model.request_sample()
        self.assertEqual((model.sink_requests, model.transform_requests), (2, 2))
        self.assertEqual((model.source_attempts, model.accepted_requests), (1, 1))

    def test_initial_start_preroll_and_pause_are_not_seek_restart(self) -> None:
        for command in ("starting_sources", "prerolling_sinks", "pausing_sources"):
            with self.subTest(command=command):
                model = RestartDemand(guard=True, command=command)
                model.request_sample()
                self.assertEqual(model.accepted_requests, 1)

    def test_stale_restart_flag_cannot_gate_unrelated_command_states(self) -> None:
        for command in ("complete", "stopping_sources", "prerolling_sinks"):
            with self.subTest(command=command):
                model = RestartDemand(guard=True, command=command, restarting=True)
                model.request_sample()
                self.assertEqual(model.accepted_requests, 1)

    def test_terminal_start_does_not_replay_deferred_demand(self) -> None:
        model = RestartDemand(guard=True)
        model.stop_for_seek()
        model.finish_stop()
        model.request_sample()
        model.fail_start()
        self.assertFalse(model.source_active)
        self.assertEqual((model.source_attempts, model.accepted_requests), (0, 0))
        self.assertEqual(model.sink_flushes, 0)

    def test_wrongstate_is_not_suppressed_outside_the_seek_transition(self) -> None:
        model = RestartDemand(guard=True, source_active=False)
        model.request_sample()
        self.assertEqual((model.source_attempts, model.accepted_requests), (1, 0))
        self.assertEqual(model.wrong_state, 1)


class ProposedPatchContractTests(unittest.TestCase):
    """Tie the model predicate to the proposed overlay, not runtime behavior."""

    def test_overlay_preserves_credit_before_guard_and_guards_before_pull(self) -> None:
        root = Path(__file__).resolve().parents[2]
        patch = (root / "patches/proposed/0021-mf-defer-sink-demand-during-seek-restart.patch").read_text()
        targets = re.findall(r"^diff --git a/(\S+) b/(\S+)$", patch, re.MULTILINE)
        self.assertEqual(targets, [("dlls/mf/session.c", "dlls/mf/session.c")])
        hunk = patch.split("@@", 2)[2]
        postimage = "\n".join(line[1:] for line in hunk.splitlines() if line[:1] in (" ", "+"))
        normalized = " ".join(postimage.split())
        credit = normalized.index("sink_node->u.sink.requests++;")
        guard = normalized.index(SEEK_RESTART_GUARD)
        pull = normalized.index("if (FAILED(session_request_sample_from_node(")
        self.assertLess(credit, guard)
        self.assertLess(guard, pull)


class IntegratedSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if WINE_TREE is None:
            raise unittest.SkipTest("pass --wine-tree for integrated source contracts")
        cls.source = (WINE_TREE / "dlls/mf/session.c").read_text()

    def body(self, text, name):
        match = re.search(r"^static [^\n]*\b" + re.escape(name)
                          + r"\([^;]*?\n\{.*?^}", text, re.M | re.S)
        self.assertIsNotNone(match, "missing function " + name)
        return " ".join(match.group().split())

    def assert_source_contract(self, source):
        request = self.body(source, "session_request_sample")
        credit = "sink_node->u.sink.requests++;"
        pull = "if (FAILED(session_request_sample_from_node("
        for label, token in (("sink credit", credit), ("seek-restart guard", SEEK_RESTART_GUARD),
                             ("upstream pull", pull)):
            self.assertTrue(token in request, "session_request_sample: missing " + label)
        self.assertLess(request.index(credit), request.index(SEEK_RESTART_GUARD))
        self.assertLess(request.index(SEEK_RESTART_GUARD), request.index(pull))

        completion = self.body(source, "session_set_source_object_state")
        self.assertTrue(
            "session->presentation.flags &= ~SESSION_FLAG_RESTARTING; session_flush_sinks(session);"
            in completion, "source-start completion must clear RESTARTING before sink re-prime")
        flush = self.body(source, "session_flush_sinks")
        self.assertTrue("if (node->u.sink.requests)" in flush,
                        "session_flush_sinks must re-prime only existing sink demand")
        credit_restore = "node->u.sink.requests--;"
        reprime = "session_request_sample(session, node->object.sink_stream);"
        for label, token in (("credit restoration", credit_restore), ("sink re-prime", reprime)):
            self.assertTrue(token in flush, "session_flush_sinks: missing " + label)
        self.assertLess(flush.index(credit_restore), flush.index(reprime))

    def test_integrated_source_keeps_demand_until_source_ready(self):
        self.assert_source_contract(self.source)

    def test_missing_guard_credit_or_completion_order_is_rejected(self):
        for old, new in (
            ("session->command_state == COMMAND_STATE_RESTARTING_SOURCES\n            ||", "FALSE\n            ||"),
            ("session->command_state == COMMAND_STATE_STARTING_SOURCES\n                &&", "TRUE\n                &&"),
            ("sink_node->u.sink.requests++;", "/* credit lost */"),
            ("session->presentation.flags &= ~SESSION_FLAG_RESTARTING;", "/* restart flag not cleared */"),
            ("node->u.sink.requests--; /* session_request_sample() restores it. */", "/* credit duplicated */"),
        ):
            with self.subTest(regression=old):
                mutated = self.source.replace(old, new, 1)
                self.assertTrue(mutated != self.source, "mutation anchor missing: " + old)
                with self.assertRaises(AssertionError):
                    self.assert_source_contract(mutated)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--wine-tree", type=Path)
    args, remaining = parser.parse_known_args()
    WINE_TREE = args.wine_tree
    unittest.main(argv=[sys.argv[0], *remaining])
