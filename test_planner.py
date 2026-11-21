import json
import os
import tempfile
import unittest
from pathlib import Path

import planner

HERE = Path(__file__).resolve().parent
GOOD = json.loads((HERE / "samples" / "rainbow.json").read_text())
BROKEN = json.loads((HERE / "samples" / "broken.json").read_text())


class FakeLLM:
    """Returns scripted replies and records the messages it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def complete(self, system, messages, max_tokens):
        self.seen.append(json.loads(json.dumps(messages)))
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return text, {"input_tokens": 1000, "output_tokens": 500}


def with_marks(marks):
    """GOOD lesson with the first figure replaced by one part holding `marks`."""
    lesson = json.loads(json.dumps(GOOD))
    for beat in lesson["beats"]:
        for op in beat["ops"]:
            if op["type"] == "figure":
                op["frame"] = {"width": 800, "height": 400}
                op["parts"] = [{"name": "p", "cue": 0, "marks": marks}]
                return lesson
    raise AssertionError("sample has no figure")


class LabelGeometryTests(unittest.TestCase):
    def codes(self, marks):
        return {i["code"] for i in planner.validate_with_node(with_marks(marks))}

    def test_line_through_label_is_flagged(self):
        marks = [{"kind": "label", "x": 300, "y": 200, "text": "Sunlight", "size": 20},
                 {"kind": "line", "x1": 250, "y1": 195, "x2": 450, "y2": 195}]
        self.assertIn("label_on_mark", self.codes(marks))

    def test_leader_line_starting_at_label_is_allowed(self):
        marks = [{"kind": "label", "x": 100, "y": 100, "text": "Leaf", "size": 20},
                 {"kind": "line", "x1": 150, "y1": 95, "x2": 350, "y2": 200},
                 {"kind": "circle", "cx": 380, "cy": 210, "r": 40}]
        self.assertNotIn("label_on_mark", self.codes(marks))

    def test_label_clear_of_shapes_and_label_fully_inside_shape_are_allowed(self):
        marks = [{"kind": "circle", "cx": 400, "cy": 200, "r": 120},
                 {"kind": "label", "x": 400, "y": 205, "text": "Earth", "size": 20, "align": "middle"},
                 {"kind": "label", "x": 400, "y": 360, "text": "ocean", "size": 20, "align": "middle"}]
        self.assertNotIn("label_on_mark", self.codes(marks))

    def test_curve_through_label_is_flagged(self):
        on_curve = [{"kind": "label", "x": 330, "y": 200, "text": "river", "size": 20},
                    {"kind": "path", "d": "M 300 250 C 360 150 440 150 500 250"}]
        self.assertIn("label_on_mark", self.codes(on_curve))
        clear_of_curve = [{"kind": "label", "x": 380, "y": 290, "text": "river", "size": 20},
                          {"kind": "path", "d": "M 300 250 C 360 150 440 150 500 250"}]
        self.assertNotIn("label_on_mark", self.codes(clear_of_curve))


class ConnectivityTests(unittest.TestCase):
    SUN = {"kind": "circle", "cx": 100, "cy": 200, "r": 40}
    EARTH = {"kind": "circle", "cx": 500, "cy": 200, "r": 50}

    def codes(self, marks):
        return {i["code"] for i in planner.validate_with_node(with_marks(marks))}

    def test_arrow_touching_both_objects_is_fine(self):
        arrow = {"kind": "arrow", "x1": 140, "y1": 200, "x2": 450, "y2": 200}
        self.assertNotIn("dangling_line", self.codes([self.SUN, self.EARTH, arrow]))

    def test_arrow_pointing_at_nothing_is_flagged(self):
        arrow = {"kind": "arrow", "x1": 140, "y1": 200, "x2": 330, "y2": 200}  # tip is 120px short of Earth
        self.assertIn("dangling_line", self.codes([self.SUN, self.EARTH, arrow]))

    def test_leader_line_with_free_end_is_flagged_but_standalone_axis_is_not(self):
        label = {"kind": "label", "x": 250, "y": 100, "text": "Low tide", "size": 16, "align": "middle"}
        floating_leader = {"kind": "line", "x1": 290, "y1": 190, "x2": 450, "y2": 200}  # start touches nothing, end is in Earth
        self.assertIn("dangling_line", self.codes([self.EARTH, label, floating_leader]))
        axis = {"kind": "line", "x1": 50, "y1": 350, "x2": 750, "y2": 350}  # both ends free: a deliberate axis
        self.assertNotIn("dangling_line", self.codes([self.EARTH, axis]))

    def test_label_far_from_its_own_part_is_orphaned(self):
        near_other_part = [{"kind": "circle", "cx": 400, "cy": 200, "r": 60}]
        marks = near_other_part + [
            {"kind": "line", "x1": 100, "y1": 100, "x2": 300, "y2": 100},
            {"kind": "label", "x": 200, "y": 330, "text": "pull", "size": 16, "align": "middle"},
        ]
        lesson = with_marks(marks)
        fig = next(o for b in lesson["beats"] for o in b["ops"] if o["type"] == "figure")
        # same part holds line + label (label is 230px from the line) while a circle in another part is close by
        fig["parts"] = [
            {"name": "ball", "cue": 0, "marks": [{"kind": "circle", "cx": 200, "cy": 290, "r": 20}]},
            {"name": "pull", "cue": 0, "marks": marks[1:]},
        ]
        codes = {i["code"] for i in planner.validate_with_node(lesson)}
        self.assertIn("orphan_label", codes)

    def test_arrow_is_a_valid_mark_kind(self):
        arrow = {"kind": "arrow", "x1": 140, "y1": 200, "x2": 450, "y2": 200, "color": "red", "dashed": True}
        self.assertEqual([i for i in planner.validate_with_node(with_marks([self.SUN, self.EARTH, arrow])) if i["code"] in {"bad_mark", "missing_field"}], [])


class HoistTests(unittest.TestCase):
    def nested(self):
        lesson = json.loads(json.dumps(GOOD))
        first, second = lesson["beats"][0], lesson["beats"][1]
        # model forgot to close beat 1's ops array, so beat 2 (and the rest) ended up inside it
        first["ops"] += lesson["beats"][1:]
        lesson["beats"] = [first]
        return lesson, len(GOOD["beats"])

    def test_beats_nested_in_ops_are_hoisted_back_in_order(self):
        lesson, n = self.nested()
        self.assertTrue(planner.validate_with_node(lesson))  # broken as the model wrote it
        moved = planner.hoist_nested_beats(lesson)
        self.assertEqual(moved, n - 1)
        self.assertEqual([b["id"] for b in lesson["beats"]], [b["id"] for b in GOOD["beats"]])
        self.assertEqual(planner.validate_with_node(lesson), [])

    def test_well_formed_lesson_is_untouched(self):
        lesson = json.loads(json.dumps(GOOD))
        self.assertEqual(planner.hoist_nested_beats(lesson), 0)
        self.assertEqual(lesson, GOOD)

    def test_generate_lesson_recovers_nested_beats_without_a_retry(self):
        lesson, _ = self.nested()
        llm = FakeLLM([json.dumps(lesson)])
        res = planner.generate_lesson(llm, "t")
        self.assertEqual(res.issues, [])
        self.assertEqual(res.usage.calls, 1)

    def test_hoist_ignores_garbage(self):
        for bad in ({}, {"beats": "x"}, {"beats": [None, 3, {"ops": "x"}]}):
            planner.hoist_nested_beats(bad)  # must not raise


class AutofixTests(unittest.TestCase):
    EARTH = {"kind": "circle", "cx": 500, "cy": 200, "r": 50}

    def fix(self, marks):
        src = with_marks(marks)
        where = next(f"beats[{bi}].ops[{oi}]" for bi, b in enumerate(src["beats"])
                     for oi, o in enumerate(b["ops"]) if o["type"] == "figure")
        lesson, changes = planner.autofix_with_node(src)
        fig = next(o for b in lesson["beats"] for o in b["ops"] if o["type"] == "figure")
        mine = [c for c in changes if c.startswith(where + ":")]  # other figures in the sample may change too
        return fig["parts"][0]["marks"], mine, planner.validate_with_node(lesson)

    def test_arrow_tip_in_empty_space_is_snapped_onto_the_object(self):
        sun = {"kind": "circle", "cx": 100, "cy": 200, "r": 40}
        marks, changes, issues = self.fix([sun, self.EARTH, {"kind": "arrow", "x1": 140, "y1": 200, "x2": 330, "y2": 200}])
        arrow = marks[2]
        self.assertAlmostEqual(arrow["x2"], 450, delta=1)  # left edge of Earth (500 - 50)
        self.assertTrue(changes)
        self.assertNotIn("dangling_line", {i["code"] for i in issues})

    def test_half_attached_leader_line_ends_up_connected_to_its_label(self):
        label = {"kind": "label", "x": 250, "y": 100, "text": "Low tide", "size": 16, "align": "middle"}
        leader = {"kind": "line", "x1": 290, "y1": 190, "x2": 450, "y2": 200}
        marks, _, issues = self.fix([self.EARTH, label, leader])
        codes = {i["code"] for i in issues}
        self.assertFalse(codes & {"dangling_line", "orphan_label", "label_on_mark"}, codes)
        self.assertLess(marks[2]["x1"], 440)  # the leader did not collapse onto the Earth

    def test_label_crossed_by_a_line_is_moved_off_it(self):
        label = {"kind": "label", "x": 300, "y": 200, "text": "Sunlight", "size": 20}
        marks, _, issues = self.fix([label, {"kind": "line", "x1": 250, "y1": 195, "x2": 450, "y2": 195}])
        self.assertNotIn("label_on_mark", {i["code"] for i in issues})
        self.assertNotEqual((marks[0]["x"], marks[0]["y"]), (300, 200))

    def test_orphan_label_is_pulled_next_to_its_drawing(self):
        marks = [self.EARTH, {"kind": "label", "x": 500, "y": 380, "text": "Earth", "size": 18, "align": "middle"}]
        out, _, issues = self.fix(marks)
        self.assertNotIn("orphan_label", {i["code"] for i in issues})
        self.assertLess(out[1]["y"], 380)

    def test_clean_figure_is_left_alone_and_standalone_axis_stays_put(self):
        axis = {"kind": "line", "x1": 50, "y1": 350, "x2": 750, "y2": 350}
        label = {"kind": "label", "x": 500, "y": 280, "text": "Earth", "size": 18, "align": "middle"}
        before = [self.EARTH, axis, label]
        out, changes, _ = self.fix(json.loads(json.dumps(before)))
        self.assertEqual(out, before)
        self.assertEqual(changes, [])

    def test_fixer_never_crashes_on_malformed_marks(self):
        junk = [{"kind": "circle", "cx": "x"}, {"kind": "nope"}, None, {"kind": "arrow", "x1": 1}]
        planner.autofix_with_node(with_marks(junk))  # must not raise


class SoftIssueTests(unittest.TestCase):
    CROSSED = [{"kind": "label", "x": 300, "y": 200, "text": "Sunlight", "size": 20},
               {"kind": "line", "x1": 250, "y1": 195, "x2": 450, "y2": 195}]

    def test_cosmetic_issue_is_repaired_when_the_model_fixes_it(self):
        fixed = [{"kind": "label", "x": 300, "y": 240, "text": "Sunlight", "size": 20},
                 {"kind": "line", "x1": 250, "y1": 195, "x2": 450, "y2": 195}]
        llm = FakeLLM([json.dumps(with_marks(self.CROSSED)), json.dumps(with_marks(fixed))])
        res = planner.generate_lesson(llm, "t", autofix=None)
        self.assertEqual((res.issues, res.warnings, res.repairs), ([], [], 1))

    def test_only_cosmetic_issues_left_is_accepted_with_warnings(self):
        llm = FakeLLM([json.dumps(with_marks(self.CROSSED))])  # model never fixes it
        res = planner.generate_lesson(llm, "t", max_repairs=1, autofix=None)
        self.assertEqual(res.issues, [])
        self.assertEqual([w["code"] for w in res.warnings], ["label_on_mark"])
        self.assertIsNotNone(res.lesson)
        self.assertEqual(res.usage.calls, 2)  # it still got its repair round

    def test_structural_issue_is_never_accepted(self):
        llm = FakeLLM([json.dumps(BROKEN)])
        res = planner.generate_lesson(llm, "t", max_repairs=1)
        self.assertTrue(res.issues)
        self.assertEqual(res.warnings, [])

    def test_repair_message_explains_auto_closed_brackets(self):
        llm = FakeLLM([json.dumps(BROKEN)[:-1], json.dumps(GOOD)])
        res = planner.generate_lesson(llm, "t")
        self.assertEqual(res.issues, [])
        self.assertIn("missing closing brackets", llm.seen[1][-1]["content"])


class PlannerTests(unittest.TestCase):
    def test_reply_missing_closing_brackets_is_recovered_without_a_retry(self):
        truncated = json.dumps(GOOD)[:-1]  # model forgot the final "}"
        llm = FakeLLM([truncated])
        res = planner.generate_lesson(llm, "How a rainbow forms")
        self.assertEqual(res.issues, [])
        self.assertEqual(res.usage.calls, 1)

    def test_close_open_brackets_ignores_brackets_inside_strings(self):
        self.assertEqual(planner.close_open_brackets('{"a":"]}[{","b":[1,2'), '{"a":"]}[{","b":[1,2]}')

    def test_valid_first_try(self):
        llm = FakeLLM([json.dumps(GOOD)])
        res = planner.generate_lesson(llm, "How a rainbow forms")
        self.assertEqual(res.issues, [])
        self.assertEqual(res.repairs, 0)
        self.assertEqual(res.usage.calls, 1)

    def test_repair_loop_fixes_broken_lesson(self):
        llm = FakeLLM(["```json\n" + json.dumps(BROKEN) + "\n```", "Here you go: " + json.dumps(GOOD)])
        res = planner.generate_lesson(llm, "How a rainbow forms")
        self.assertEqual(res.issues, [])
        self.assertEqual(res.repairs, 1)
        self.assertEqual(res.usage.calls, 2)
        self.assertEqual(res.usage.input_tokens, 2000)
        repair_prompt = llm.seen[1][-1]["content"]
        self.assertIn("cue 99", repair_prompt)
        self.assertIn("beats[0].ops[1].parts[0].cue", repair_prompt)
        self.assertEqual(llm.seen[1][-2]["role"], "assistant")

    def test_gives_up_after_max_repairs(self):
        llm = FakeLLM(["not json at all"])
        res = planner.generate_lesson(llm, "x", max_repairs=2)
        self.assertEqual(res.usage.calls, 3)
        self.assertIsNone(res.lesson)
        self.assertEqual(res.issues[0]["code"], "invalid_json")

    def test_zero_repairs_means_one_call(self):
        llm = FakeLLM([json.dumps(BROKEN)])
        res = planner.generate_lesson(llm, "x", max_repairs=0)
        self.assertEqual(res.usage.calls, 1)
        self.assertTrue(res.issues)

    def test_cost_report(self):
        u = planner.Usage(calls=2, input_tokens=2000, output_tokens=1000)
        self.assertIn("cost=$0.00700", planner.cost_report(u, 1.0, 5.0, None))
        self.assertIn("INR 0.616", planner.cost_report(u, 1.0, 5.0, 88))
        self.assertIn("--price-in", planner.cost_report(u, None, None, None))

    def test_cli_writes_valid_file_and_accepts_dynamic_input(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "l.json")
            code = planner.main(
                ["--topic", "Tides", "--minutes", "3", "--language", "en", "--out", out,
                 "--price-in", "1", "--price-out", "5", "--inr-per-usd", "88"],
                llm=FakeLLM([json.dumps(GOOD)]),
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(Path(out).read_text())["meta"]["title"], GOOD["meta"]["title"])

    def test_cli_failure_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            code = planner.main(["--topic", "Tides", "--out", os.path.join(d, "l.json"), "--max-repairs", "0"],
                                llm=FakeLLM([json.dumps(BROKEN)]))
            self.assertEqual(code, 1)

    def test_extract_json_variants(self):
        self.assertEqual(planner.extract_json('```json\n{"a":1}\n```'), {"a": 1})
        self.assertEqual(planner.extract_json('Sure! {"a": {"b": 2}} done'), {"a": {"b": 2}})
        with self.assertRaises(ValueError):
            planner.extract_json("nothing here")

    def test_prompt_uses_dynamic_values(self):
        p = planner.build_user_prompt("Black holes", "hi", 7, "advanced", "notes here")
        for s in ("Black holes", "hi", "advanced", "notes here", "1050"):
            self.assertIn(s, p)

    def test_arg_validation(self):
        with self.assertRaises(SystemExit):
            planner.parse_args(["--topic", "x", "--minutes", "0"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
