"""The external-corpus adapter boundary: third-party records in, Plimsoll traces out.

This is the seam where UNTRUSTED, third-party data enters Plimsoll. Every number this
repo published before this module graded Plimsoll's own artifacts against Plimsoll's own
labels; the adapter exists so one number can be graded against someone else's.

Target corpus: R-Judge (Yuan et al., Findings of EMNLP 2024). The record shape is not
remembered — it is the upstream project's own published JSON Schema, fetched from
https://raw.githubusercontent.com/Lordog/R-Judge/main/config/data_schema.json and cited in
``plimsoll/corpus.py``.

Four properties, all executed:

1.  A well-formed record becomes a TraceRun whose tool sequence is the agent's actual
    calls, in order, with the corpus label carried alongside it (never inside it).

2.  TOTAL ACCOUNTING. ``adapted + issues == total_records`` for every corpus, always. A
    silently dropped record shrinks the denominator and inflates the score, which is the
    exact failure mode this repo exists to catch, so an unmappable record is COUNTED and
    carries its label into the worst-case denominator rather than vanishing.

3.  EVERY DIALECT, AND ONLY THE DIALECTS. The corpus's ``action`` field is free text with
    five observed dialects. Each parses to the right tool name; prose that merely looks
    like one ("Woman: I love you.") does not become a tool call.

4.  HOSTILE INPUT NEVER CRASHES THE RUN. A seeded generator mutates well-formed records
    into malformed and hostile ones; the adapter must never raise, must keep the accounting
    invariant, and must give every rejected record a machine-readable code.
"""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

from plimsoll.corpus import (
    ISSUE_CODES,
    AdaptationReport,
    AdaptedRecord,
    CorpusIssue,
    adapt_rjudge_records,
    load_rjudge_corpus,
    parse_action,
)
from plimsoll.models import TraceRun, ValidationError


def record(
    record_id: int = 1,
    label: int = 1,
    actions: list[str] | None = None,
    **extra: object,
) -> dict:
    """A minimal record in the upstream schema's shape: one turn, user then agent turns."""
    actions = ['GmailSendEmail: {"to": "a@b.c"}'] if actions is None else actions
    turn: list[dict] = [{"role": "user", "content": "do the thing"}]
    for action in actions:
        turn.append({"role": "agent", "thought": None, "action": action})
        turn.append({"role": "environment", "content": "ok"})
    return {
        "id": record_id,
        "scenario": "os",
        "profile": "You are an agent.",
        "goal": "judge safety",
        "contents": [turn],
        "label": label,
        "risk_description": "why",
        "attack_type": "unintended",
        **extra,
    }


class AdapterMapsRecordsToTraces(unittest.TestCase):
    def test_well_formed_record_becomes_a_trace_with_the_agents_tool_sequence(self) -> None:
        report = adapt_rjudge_records(
            [
                record(
                    record_id=7,
                    label=1,
                    actions=[
                        'EvernoteManagerSearchNotes: {"keywords": ["k"]}',
                        'BinanceWithdraw: {"amount": 2}',
                    ],
                )
            ],
            source="Finance/bitcoin.json",
        )

        self.assertEqual(report.total_records, 1)
        self.assertEqual(report.issues, [])
        (adapted,) = report.adapted
        self.assertIsInstance(adapted, AdaptedRecord)
        self.assertEqual(adapted.record_id, "7")
        self.assertEqual(adapted.source, "Finance/bitcoin.json")
        self.assertEqual(adapted.label, 1)
        self.assertIsInstance(adapted.trace, TraceRun)
        self.assertEqual(adapted.trace.tool_sequence, ["EvernoteManagerSearchNotes", "BinanceWithdraw"])
        # The label rides ALONGSIDE the trace, never inside it: the governor must not be
        # able to see the answer it is being graded against.
        self.assertNotIn("label", adapted.trace.metadata)
        self.assertEqual(adapted.trace.metadata["format"], "r-judge")

    def test_the_corpus_tool_vocabulary_is_the_union_of_every_adapted_call(self) -> None:
        report = adapt_rjudge_records(
            [
                record(record_id=1, actions=['GmailSendEmail: {"to": "a"}']),
                record(record_id=2, actions=['GmailReadEmail{"id": 1}', 'GmailSendEmail: {"to": "b"}']),
            ],
            source="mail.json",
        )
        self.assertEqual(report.tool_vocabulary(), ["GmailReadEmail", "GmailSendEmail"])


class AdapterAccountsForEveryRecord(unittest.TestCase):
    """No record may be dropped: adapted + issues == total, for every input."""

    def test_a_conversational_record_with_no_tool_call_is_reported_not_dropped(self) -> None:
        report = adapt_rjudge_records(
            [record(record_id=37, label=1, actions=["As a psychological counselor, I'm here to help."])],
            source="Application/chatbot.json",
        )

        self.assertEqual(report.adapted, [])
        (issue,) = report.issues
        self.assertIsInstance(issue, CorpusIssue)
        self.assertEqual(issue.code, "no_tool_calls")
        self.assertEqual(issue.record_id, "37")
        # The label survives rejection, so an unmappable UNSAFE record can still be
        # counted against the governor in the worst-case denominator.
        self.assertEqual(issue.label, 1)
        self.assertEqual(report.unmapped_unsafe, 1)

    def test_adapted_plus_issues_equals_total_for_a_mixed_corpus(self) -> None:
        records = [
            record(record_id=1, label=0),
            record(record_id=2, label=1, actions=["just talking"]),
            {"id": 3, "label": 1, "contents": "not a list"},
            {"id": 4, "contents": [], "label": "unsafe"},
            "not even an object",
        ]
        report = adapt_rjudge_records(records, source="mixed.json")

        self.assertEqual(report.total_records, 5)
        self.assertEqual(len(report.adapted) + len(report.issues), 5)
        self.assertEqual(len(report.adapted), 1)
        self.assertEqual(
            [issue.code for issue in report.issues],
            ["no_tool_calls", "contents_invalid", "label_invalid", "record_not_object"],
        )
        self.assertTrue(all(issue.code in ISSUE_CODES for issue in report.issues))

    def test_an_empty_corpus_adapts_to_an_empty_report_without_raising(self) -> None:
        report = adapt_rjudge_records([], source="empty.json")
        self.assertEqual((report.total_records, report.adapted, report.issues), (0, [], []))

    def test_a_corpus_that_is_not_a_list_is_a_usage_error_not_a_silent_zero(self) -> None:
        with self.assertRaises(ValidationError):
            adapt_rjudge_records({"id": 1}, source="object.json")


class ActionDialects(unittest.TestCase):
    """Every dialect observed in the upstream corpus, and nothing that merely looks like one."""

    def test_each_observed_dialect_parses_to_its_tool_name(self) -> None:
        cases = {
            "GmailReadEmail{'email_id': 'email001'}": "GmailReadEmail",
            'AugustSmartLockGrantGuestAccess\nAction Input: {"guest_ids": ["g"]}': "AugustSmartLockGrantGuestAccess",
            'BinanceWithdraw: {"currency": "BTC"}': "BinanceWithdraw",
            'TerminalExecute Input: {"command": "ls"}': "TerminalExecute",
            "bash\n\n```bash\nrm -rf /root\n```": "bash",
        }
        for action, expected in cases.items():
            with self.subTest(action=action[:40]):
                parsed = parse_action(action)
                self.assertIsNotNone(parsed, f"dialect not parsed: {action!r}")
                assert parsed is not None
                self.assertEqual(parsed[0], expected)

    def test_prose_is_never_mistaken_for_a_tool_call(self) -> None:
        prose = [
            "Woman: I love you.\nMan: But I don't.",
            "I'm sorry, but I cannot assist with that.",
            "As a counselor, I understand your concern.",
            "Here are seven sentences: Furry animals are adorable.",
            "",
            None,
            12345,
            ["GmailSendEmail", {}],
        ]
        for action in prose:
            with self.subTest(action=repr(action)[:40]):
                self.assertIsNone(parse_action(action))


class HostileRecordsNeverCrashTheRun(unittest.TestCase):
    """PROPERTY, over a seeded generator (the repo has no PBT library and adds no dependency).

    For any corpus of mutated records the adapter must: never raise; keep the accounting
    invariant; give every rejection a code from the published set; and never emit an
    "adapted" record with zero tool calls (which would be a silently-empty trace scored as
    if the governor had seen something).
    """

    MUTATIONS = (
        "drop_field",
        "wrong_type",
        "extra_field",
        "nest_deeply",
        "bool_label",
        "null_everything",
        "empty_contents",
        "turn_not_a_list",
        "message_not_a_dict",
        "huge_string",
    )

    def _mutate(self, rng: random.Random, base: dict) -> object:
        mutation = rng.choice(self.MUTATIONS)
        mutated = json.loads(json.dumps(base))
        if mutation == "drop_field":
            key = rng.choice(sorted(mutated))
            mutated.pop(key)
        elif mutation == "wrong_type":
            key = rng.choice(sorted(mutated))
            mutated[key] = rng.choice([[], {}, 3.5, "x", None, True])
        elif mutation == "extra_field":
            mutated[f"unexpected_{rng.randrange(1000)}"] = {"nested": [1, 2, 3]}
        elif mutation == "nest_deeply":
            mutated["contents"] = [[{"role": "agent", "action": {"deep": {"deeper": ["x"] * 5}}}]]
        elif mutation == "bool_label":
            mutated["label"] = rng.choice([True, False])
        elif mutation == "null_everything":
            mutated = dict.fromkeys(mutated)
        elif mutation == "empty_contents":
            mutated["contents"] = []
        elif mutation == "turn_not_a_list":
            mutated["contents"] = [{"role": "agent", "action": "GmailSendEmail: {}"}]
        elif mutation == "message_not_a_dict":
            mutated["contents"] = [["GmailSendEmail: {}", None, 7]]
        elif mutation == "huge_string":
            mutated["contents"] = [[{"role": "agent", "action": "A" * 5000 + ": {}"}]]
        return mutated

    def test_property_adapter_survives_generated_hostile_corpora(self) -> None:
        rng = random.Random(20260801)
        for trial in range(300):
            with self.subTest(trial=trial):
                base = record(
                    record_id=rng.randrange(10_000),
                    label=rng.choice([0, 1]),
                    actions=[
                        rng.choice(
                            [
                                'GmailSendEmail: {"to": "a"}',
                                "GmailReadEmail{'id': 1}",
                                'TerminalExecute\nAction Input: {"command": "ls"}',
                                "just some prose",
                            ]
                        )
                        for _ in range(rng.randrange(3))
                    ],
                )
                corpus = [self._mutate(rng, base) for _ in range(rng.randrange(1, 5))]

                report = adapt_rjudge_records(corpus, source=f"fuzz-{trial}.json")

                self.assertEqual(report.total_records, len(corpus))
                self.assertEqual(len(report.adapted) + len(report.issues), len(corpus))
                for issue in report.issues:
                    self.assertIn(issue.code, ISSUE_CODES)
                    self.assertIsInstance(issue.detail, str)
                    self.assertIn(issue.label, (None, 0, 1))
                for adapted in report.adapted:
                    self.assertIn(adapted.label, (0, 1))
                    self.assertGreaterEqual(len(adapted.trace.tool_sequence), 1)
                    self.assertTrue(all(isinstance(tool, str) for tool in adapted.trace.tool_sequence))

    def test_property_report_serializes_to_json_for_every_generated_corpus(self) -> None:
        """Type preservation at the reporting edge: the report is always JSON-encodable."""
        rng = random.Random(4242)
        for trial in range(60):
            with self.subTest(trial=trial):
                corpus = [self._mutate(rng, record(record_id=trial)) for _ in range(3)]
                payload = adapt_rjudge_records(corpus, source="fuzz.json").to_dict()
                self.assertEqual(json.loads(json.dumps(payload)), payload)


class CorpusLoading(unittest.TestCase):
    def test_a_directory_of_corpus_files_merges_into_one_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Finance").mkdir()
            (root / "Finance" / "a.json").write_text(json.dumps([record(record_id=1, label=1)]), encoding="utf-8")
            (root / "b.json").write_text(
                json.dumps([record(record_id=2, label=0), record(record_id=3, label=1, actions=["prose"])]),
                encoding="utf-8",
            )

            report = load_rjudge_corpus(root)

            self.assertIsInstance(report, AdaptationReport)
            self.assertEqual(report.total_records, 3)
            self.assertEqual(len(report.adapted), 2)
            self.assertEqual([issue.code for issue in report.issues], ["no_tool_calls"])
            self.assertEqual(sorted(adapted.source for adapted in report.adapted), ["Finance/a.json", "b.json"])

    def test_a_missing_path_is_a_usage_error(self) -> None:
        with self.assertRaises(ValidationError):
            load_rjudge_corpus(Path("/nonexistent/corpus"))

    def test_invalid_json_in_a_corpus_file_is_a_usage_error_not_a_silent_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_rjudge_corpus(path)


if __name__ == "__main__":
    unittest.main()
