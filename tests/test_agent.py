import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent import main, tools
from agent.llm import LLM, LLMError, LLMResult, extract_code
from agent.skills import load_skills, select
from agent.spec_contract import extract_contract, check_semantics, repair_synchronous_events
from agent.verification import SelfTest, generate_selftest, parse_selftest, review_selftest
from agent.oracles import from_spec


INTERFACE = "module TopModule(input a, input b, output q);"
BAD = INTERFACE + "\nassign q = a;\nendmodule\n"
GOOD = INTERFACE + "\nassign q = a & b;\nendmodule\n"
TEST = SelfTest("", """
    a=1; b=0; #1; check(q, 1'b0, 1'b1, "10");
    a=1; b=1; #1; check(q, 1'b1, 1'b1, "11");
""", "both outputs")


class InterfaceAndContractTests(unittest.TestCase):
    def test_ansi_shared_width_and_helper_before_top(self):
        code = ("module helper(input x); endmodule\n"
                "module TopModule(input [7:0] a,b, output [7:0] q); endmodule")
        rc, msg = tools.check_interface(
            code, "module TopModule(input [7:0] a, input [7:0] b, output [7:0] q);")
        self.assertEqual((rc, msg), (0, "interface matches the declaration"))

    def test_non_ansi_ports(self):
        code = "module TopModule(a,b,q); input [7:0] a,b; output [7:0] q; endmodule"
        self.assertEqual(tools.module_ports(code)[1],
                         [("input", 8, "a"), ("input", 8, "b"), ("output", 8, "q")])

    def test_prompt_extra_clock_blocked_but_direction_typo_advisory(self):
        prompt = "module named TopModule\n - input a\n - input q\n"
        self.assertEqual(tools.check_interface(
            "module TopModule(input a, output q, input clk); endmodule", "", prompt)[0], 1)
        rc, msg = tools.check_interface(
            "module TopModule(input a, output q); endmodule", "", prompt)
        self.assertEqual(rc, 0)
        self.assertTrue(msg.startswith("⚠"))

    def test_partial_prose_port_list_does_not_invent_hard_contract(self):
        prompt = "module named TopModule\n - input a\n - output q (result)\n"
        self.assertEqual(tools.ports_from_prompt(prompt), (None, []))
        self.assertEqual(tools.check_interface(GOOD, "", prompt)[0], 0)

    def test_wrapped_sync_reset_and_scope(self):
        prompt = "Reset is active-high\nsynchronous. Implement only the combinational logic."
        interface = "module TopModule(input clk, reset, a, output q);"
        contract = extract_contract(prompt, interface, "TopModule")
        self.assertEqual(contract.reset_kind, "synchronous")
        self.assertTrue(contract.combinational_only)
        self.assertEqual(check_semantics(
            "always @(posedge clk or posedge reset) q <= a;", contract)[0], 1)
        self.assertEqual(check_semantics("assign q=a;", contract)[0], 0)

    def test_comb_scope_with_clock_and_waveform_latches(self):
        contract = extract_contract(
            "The module should implement the output logic and next state logic for an FSM.",
            INTERFACE, "TopModule")
        self.assertTrue(contract.combinational_only)
        contract = extract_contract("Read the simulation waveforms.", INTERFACE, "TopModule")
        self.assertTrue(contract.allow_latches)

    def test_reset_uncertainty_is_not_invented(self):
        contract = extract_contract("A reset input.", INTERFACE, "TopModule")
        self.assertIsNone(contract.reset_kind)
        contract = extract_contract("Asynchronous reset. Synchronous reset.",
                                    INTERFACE, "TopModule")
        self.assertIsNone(contract.reset_kind)

    def test_explicit_async_polarity(self):
        interface = "module TopModule(input clk, resetn, a, output q);"
        contract = extract_contract("Active-low asynchronous reset.",
                                    interface, "TopModule")
        self.assertEqual(check_semantics(
            "always @(posedge clk or posedge resetn) q<=a;", contract)[0], 1)
        self.assertEqual(check_semantics(
            "always @(posedge clk or negedge resetn) q<=a;", contract)[0], 0)

    def test_undeclared_clock_cannot_be_invented_for_combinational_task(self):
        contract = extract_contract("Implement a combinational AND function.",
                                    INTERFACE, "TopModule")
        rc, message = check_semantics("always @(posedge clk) q<=a & b;", contract)
        self.assertEqual(rc, 1)
        self.assertIn("no declared clock or reset input", message)
        clocked = extract_contract(
            "Update q on the positive edge of clk.",
            "module TopModule(input clk, input a, output reg q);", "TopModule")
        self.assertEqual(check_semantics(
            "always @(posedge clk) q<=a;", clocked)[0], 0)

    def test_explicit_synchronous_event_repair_preserves_body(self):
        interface = "module TopModule(input clk, reset, d, output q);"
        contract = extract_contract("Active-high synchronous reset.", interface, "TopModule")
        source = ("// @(posedge clk or posedge reset)\n"
                  "always @(posedge clk or posedge reset) if(reset) q<=0; else q<=d;")
        fixed, changes = repair_synchronous_events(source, contract)
        self.assertTrue(changes)
        self.assertIn("// @(posedge clk or posedge reset)", fixed)
        self.assertIn("always @(posedge clk) if(reset) q<=0; else q<=d;", fixed)
        async_contract = extract_contract("Active-high asynchronous reset.",
                                         interface, "TopModule")
        self.assertEqual(repair_synchronous_events(source, async_contract), (source, []))

    def test_timing_skill_routes_nonadjacent_reset_words(self):
        skills = select(load_skills(main.SKILL_DIR), "Reset is active-low synchronous.")
        self.assertIn("rtl-sequential-timing", [s.name for s in skills])

    def test_domain_skills_precede_broad_timing_skill(self):
        skills = load_skills(main.SKILL_DIR)
        selected = select(skills, "Implement a gshare branch predictor with a PHT.")
        self.assertIn("rtl-branch-predictor", [s.name for s in selected])
        self.assertEqual(selected[0].name, "rtl-branch-predictor")
        selected = select(skills, "Implement a serial receiver with a start bit and stop bit.")
        self.assertEqual(selected[0].name, "rtl-serial-receiver")
        selected = select(skills, "Moore FSM output changes on the state transition.")
        self.assertEqual(selected[0].name, "rtl-fsm-output-timing")
        selected = select(skills, "Count the ones in this 255-bit vector without truncation.")
        self.assertEqual(selected[0].name, "rtl-arithmetic-widths")
        selected = select(skills, "Implement a right-shifting Galois LFSR with numbered taps.")
        self.assertEqual(selected[0].name, "rtl-shift-register-pattern")

    def test_prompts_preserve_behavior_and_cover_rtl_timing_hazards(self):
        root = Path(main.ROOT)
        system = (root / "agent/prompts/system.md").read_text(encoding="utf-8")
        repair = (root / "agent/prompts/repair.md").read_text(encoding="utf-8")
        for phrase in ("Moore", "LFSR", "分支预测器", "并发查询",
                       "每个 data bit", "错误 stop"):
            self.assertIn(phrase, system)
        self.assertIn("保持原有状态转移条件", repair)
        self.assertIn("行为等价", repair)
        self.assertIn("每个寄存器分别", repair)

    def test_dangerous_warning_filter_and_summary(self):
        log = ("WARNING: [VRFC 10-3705] select index 255 is out of bounds\n"
               "CRITICAL WARNING: [Synth 8-6859] multi-driven net count\n"
               "Synthesis finished with 0 errors")
        self.assertEqual(len(tools.dangerous_warnings(log)), 2)
        self.assertIn("10-3705", tools.summarize_log(log))
        self.assertEqual(tools.dangerous_warnings("WARNING: license renewal"), [])
        self.assertTrue(tools.dangerous_warnings("INFO: inferred latch for q"))
        self.assertFalse(tools.dangerous_warnings("INFO: inferred latch for q", True))

    def test_log_summary_keeps_first_error_ahead_of_warning_flood(self):
        warnings = "\n".join(
            f"CRITICAL WARNING: [Synth 8-6859] multi-driven net n{i}" for i in range(80))
        log = warnings + "\nERROR: [VRFC 10-4982] syntax error near endmodule"
        summary = tools.summarize_log(log)
        self.assertLess(summary.index("ERROR: [VRFC 10-4982]"),
                        summary.index("Synth 8-6859"))

    def test_compiler_directed_type_repair_preserves_logic(self):
        source = "module TopModule(input a, output q);\nalways @* q=a;\nendmodule"
        fixed, changes = tools.repair_output_declarations(
            source, "procedural assignment to a non-register q is not permitted", "TopModule")
        self.assertTrue(changes)
        self.assertIn("output reg q", fixed)
        self.assertIn("always @* q=a;", fixed)
        fixed, changes = tools.repair_output_declarations(
            source.replace("\nalways", "\nreg q;\nalways"),
            "redeclaration of ANSI port 'q' is not allowed", "TopModule")
        self.assertTrue(changes)
        self.assertNotIn("\nreg q;", fixed)
        self.assertIn("output reg q", fixed)


class SelfTestTests(unittest.TestCase):
    def setUp(self):
        self.contract = extract_contract("q is a AND b", INTERFACE, "TopModule")

    def test_empty_or_verdict_faking_checker_rejected(self):
        for stimulus in ("#10;", '$display("AGENT_SELFTEST_PASS checks=10");',
                         "agent_checks=10; check(q,0,1,\"x\");",
                         "check(dut.internal,0,1,\"x\");"):
            with self.subTest(stimulus=stimulus), self.assertRaises(ValueError):
                parse_selftest(json.dumps(dict(
                    declarations="", stimulus=stimulus, coverage="bad")), self.contract)

    def test_checker_owns_verdict_and_checks_unknowns(self):
        parsed = parse_selftest(json.dumps(TEST.__dict__), self.contract)
        rendered = parsed.render(self.contract)
        self.assertIn("agent_checks == 0", rendered)
        self.assertIn("actual & mask) !==", rendered)
        self.assertIn("unknown expected/mask", rendered)
        self.assertIn("TopModule dut", rendered)

    def test_duplicate_ports_normalized_and_formatted_labels_allowed(self):
        data = dict(TEST.__dict__)
        data["declarations"] = "reg a;"
        normalized = parse_selftest(json.dumps(data), self.contract)
        self.assertNotIn("reg a;", normalized.declarations)
        data["declarations"] = "reg [7:0] a;"
        with self.assertRaisesRegex(ValueError, "already declares 1 bits"):
            parse_selftest(json.dumps(data), self.contract)
        data["declarations"] = "integer i;"
        data["stimulus"] = 'check(q, 0, 1, $sformatf("cycle %0d", i));'
        parse_selftest(json.dumps(data), self.contract)

    def test_generator_receives_spec_only(self):
        llm = Mock()
        llm.chat.return_value = LLMResult(json.dumps(TEST.__dict__))
        generate_selftest(llm, "AND specification", INTERFACE, self.contract, 9)
        messages = llm.chat.call_args.args[0]
        self.assertIn("AND specification", messages[1]["content"])
        self.assertNotIn("assign q", str(messages))
        self.assertEqual(llm.chat.call_args.kwargs["timeout_s"], 9)
        self.assertEqual(llm.chat.call_args.kwargs["max_tokens"], 2048)

    def test_checker_recovery_feedback_follows_previous_assistant_response(self):
        llm = Mock()
        llm.chat.return_value = LLMResult(json.dumps(TEST.__dict__))
        previous = json.dumps(dict(declarations="", stimulus="a=1; b=0; #1;",
                                   coverage='check(q,0,1,"misplaced");'))
        with self.assertRaises(ValueError) as rejected:
            parse_selftest(previous, self.contract)
        generate_selftest(llm, "AND specification", INTERFACE, self.contract, 9,
                          previous_error=str(rejected.exception),
                          previous_response=previous)
        messages = llm.chat.call_args.args[0]
        self.assertEqual([m["role"] for m in messages],
                         ["system", "user", "assistant", "user"])
        self.assertEqual(messages[-2]["content"], previous)
        self.assertIn(str(rejected.exception), messages[-1]["content"])
        self.assertNotIn(previous, messages[1]["content"])
        self.assertNotIn("assign q", str(messages))

    def test_truncated_checker_is_rejected_even_if_json_is_complete(self):
        llm = Mock()
        llm.chat.return_value = LLMResult(json.dumps(TEST.__dict__),
                                         finish_reason="length")
        record = Mock()
        with self.assertRaisesRegex(ValueError, "generation was truncated"):
            generate_selftest(llm, "AND specification", INTERFACE,
                              self.contract, 9, record=record)
        record.assert_called_once()

    def test_class_only_helper_qualifiers_rejected_before_review(self):
        for qualifier in ("local", "protected", "virtual"):
            with self.subTest(qualifier=qualifier):
                data = dict(TEST.__dict__)
                data["declarations"] = (
                    f"{qualifier} task tick; begin #1; end endtask")
                llm = Mock()
                llm.chat.return_value = LLMResult(json.dumps(data))
                with self.assertRaisesRegex(ValueError, "module scoped"):
                    generate_selftest(llm, "AND specification", INTERFACE,
                                      self.contract, 9)
                self.assertEqual(llm.chat.call_count, 1)

    def test_module_scoped_automatic_helper_and_keyword_label_are_allowed(self):
        data = dict(TEST.__dict__)
        data["declarations"] = "task automatic settle; begin #1; end endtask"
        data["stimulus"] = (
            'a=1; b=1; settle(); check(q,1,1,"local task is only a label");')
        parse_selftest(json.dumps(data), self.contract)

    def test_helper_tasks_are_normalized_to_automatic_once(self):
        for declaration in ("task settle;", "task automatic settle;"):
            with self.subTest(declaration=declaration):
                data = dict(TEST.__dict__)
                data["declarations"] = declaration + " begin #1; end endtask"
                data["stimulus"] = 'a=1; b=1; settle(); check(q,1,1,"settled");'
                checker = parse_selftest(json.dumps(data), self.contract)
                self.assertIn("task automatic settle;", checker.declarations)
                self.assertNotIn("automatic automatic", checker.declarations)

    def test_stimulus_locals_cannot_shadow_controller_ports(self):
        for declaration in ("reg q;", "logic signed [3:0] q;", "integer i,q;",
                            "integer a;", "string q;",
                            "for(int b=0;b<2;b=b+1) begin end",
                            "localparam q=0;", "typedef int counter_t; counter_t q;",
                            "enum {q=0} counter;", "foreach(data[q]) begin end",
                            "reg [1:0] ref_bits; logic [$bits(ref_bits[0])-1:0] q;",
                            "logic \\q ;"):
            with self.subTest(declaration=declaration), self.assertRaisesRegex(
                    ValueError, "shadows a DUT port|unsupported declaration or loop"):
                data = dict(TEST.__dict__)
                data["stimulus"] = declaration + "\n" + TEST.stimulus
                parse_selftest(json.dumps(data), self.contract)

    def test_stimulus_initializer_arguments_are_not_declaration_names(self):
        data = dict(TEST.__dict__)
        data["declarations"] = (
            "function integer sum; input x,y; begin sum=x+y; end endfunction")
        data["stimulus"] = (
            "integer i=sum(a,b), j; reg [1:0] pair={a,b};\n"
            "reg [$bits(pair[0])-1:0] saved;\n" + TEST.stimulus)
        parse_selftest(json.dumps(data), self.contract)
        data["declarations"] = "typedef integer count_t;"
        data["stimulus"] = 'count_t q; q=0; check(q,0,1,"aliased shadow");'
        with self.assertRaisesRegex(ValueError, "unsupported declaration or loop"):
            parse_selftest(json.dumps(data), self.contract)

    def test_simulator_requires_positive_completed_verdict(self):
        rtl = tools.RtlToolchain()
        rtl.xvlog, rtl.xelab, rtl.xsim = "xvlog", "xelab", "xsim"
        for simulation, expected in [
            ("ordinary clean exit", 2), ("AGENT_SELFTEST_PASS checks=0", 2),
            ("AGENT_SELFTEST_PASS checks=4", 0),
            ("AGENT_MISMATCH bad\nAGENT_SELFTEST_PASS checks=4", 1),
            ("AGENT_CHECKER_ERROR invalid mask", 2),
        ]:
            with self.subTest(log=simulation), patch.object(
                    tools, "_run", side_effect=[(0, ""), (0, ""), (0, simulation)]):
                self.assertEqual(rtl.simulate(GOOD, TEST.render(self.contract), 9)[0], expected)

    def test_review_approval_preserves_checker_and_receives_spec_only(self):
        llm = Mock()
        llm.chat.return_value = LLMResult('```json\n{"approved":true,"issues":[]}\n```')
        original = dict(TEST.__dict__)
        record = Mock()
        approved = review_selftest(llm, "AND specification", INTERFACE,
                                   self.contract, TEST, 8, record=record)
        self.assertIs(approved, TEST)
        self.assertEqual(TEST.__dict__, original)
        messages = llm.chat.call_args.args[0]
        self.assertIn("AND specification", messages[1]["content"])
        self.assertIn(TEST.stimulus.strip(), json.loads(
            messages[1]["content"].split("Proposed checker:\n", 1)[1])["stimulus"])
        self.assertNotIn("assign q", str(messages))
        self.assertEqual(llm.chat.call_args.kwargs, dict(timeout_s=8, max_tokens=512))
        record.assert_called_once()

    def test_review_rejection_supplies_rebuild_feedback(self):
        llm = Mock()
        llm.chat.return_value = LLMResult(json.dumps({
            "approved": False, "issues": ["Reset never spans a sampling edge"]}))
        with self.assertRaisesRegex(ValueError, "Reset never spans a sampling edge"):
            review_selftest(llm, "AND specification", INTERFACE, self.contract, TEST, 8)

    def test_review_never_approves_malformed_or_conflicting_verdict(self):
        for value in (None, {}, {"approved": "true", "issues": []},
                      {"approved": 1, "issues": []},
                      {"approved": True, "issues": ["Reset is wrong"]},
                      {"approved": False, "issues": []},
                      {"approved": True, "issues": [], "stimulus": "changed"},
                      {"approved": False, "issues": [""]},
                      {"approved": False, "issues": [1]},
                      {"approved": False, "issues": ["x" * 501]},
                      {"approved": False, "issues": ["problem"] * 5}):
            with self.subTest(verdict=value), self.assertRaises(ValueError):
                llm = Mock()
                llm.chat.return_value = LLMResult(json.dumps(value))
                review_selftest(llm, "AND specification", INTERFACE,
                                self.contract, TEST, 8)

    def test_review_truncation_never_becomes_approval(self):
        llm = Mock()
        llm.chat.return_value = LLMResult(
            '{"approved":true,"issues":[]}', finish_reason="length")
        with self.assertRaisesRegex(ValueError, "truncated"):
            review_selftest(llm, "AND specification", INTERFACE, self.contract, TEST, 8)


class SpecificationOracleTests(unittest.TestCase):
    def test_circuit_prose_has_no_prebuilt_algorithmic_checker(self):
        cases = [
            ('A "population count" circuit.',
             "module TopModule(input [254:0] in,output [7:0] out);"),
            ("Sign-extend the smaller number.",
             "module TopModule(input [3:0] in,output [7:0] out);"),
            ("A Galois LFSR shifts right. A tap is XORed with q[0]. "
             "Implement a 5-bit maximal-length Galois LFSR with taps at bit positions 5 and 3. "
             "The active-high synchronous reset should reset the LFSR output to 1. "
             "Trigger on the positive edge of the clock.",
             "module TopModule(input clk,reset,output [4:0] q);"),
            ("Implement a gshare branch predictor with train_mispredicted. "
             "Reset is asynchronous active-high. Training takes precedence. "
             "A prediction sees the PHT state before training.",
             "module TopModule(input clk,areset,predict_valid,input [6:0] predict_pc,"
             "output predict_taken,output [6:0] predict_history,"
             "input train_valid,train_taken,train_mispredicted,"
             "input [6:0] train_history,train_pc);"),
            ("Capture when input changes from 1 in one clock cycle to 0 the next. "
             "The output will remain 1 until reset (active high synchronous reset).",
             "module TopModule(input clk,reset,input [7:0] in,output [7:0] out);"),
        ]
        for prompt, interface in cases:
            with self.subTest(prompt=prompt):
                self.assertIsNone(from_spec(
                    prompt, extract_contract(prompt, interface, "TopModule")))


    def test_kmap_keeps_actual_column_order_and_skips_dont_cares(self):
        prompt = """Karnaugh map
          ab
cd  01 00 10 11
00 | 1 | 0 | x | 0 |
01 | 0 | 1 | 0 | 1 |
11 | 1 | 0 | 1 | 0 |
10 | 0 | 1 | 0 | 1 |
"""
        interface = "module TopModule(input a,b,c,d, output out);"
        test = from_spec(prompt, extract_contract(prompt, interface, "TopModule"))
        self.assertEqual(test.stimulus.count("check("), 15)
        self.assertIn('a=1\'b0; b=1\'b1; c=1\'b0; d=1\'b0; #1; check(out, 1\'d1',
                      test.stimulus)

    def test_waveform_uses_expected_values_without_inferring_circuit(self):
        prompt = """time clock a p q
0ns 0 0 x x
5ns 1 0 0 x
10ns 1 1 1 x
15ns 0 0 1 1
"""
        interface = "module TopModule(input clock,a, output p,q);"
        test = from_spec(prompt, extract_contract(prompt, interface, "TopModule"))
        self.assertEqual(test.stimulus.count("check("), 4)
        self.assertIn('check(p, 1\'d1, 1\'h1, "waveform 15ns p")', test.stimulus)
        self.assertEqual(test.uncertainties, "")

    def test_waveform_replay_does_not_supply_implementation_hypotheses(self):
        prompt = """time clock a p q
0ns 0 0 x x
5ns 1 0 0 x
10ns 1 1 1 x
15ns 1 0 0 x
20ns 1 1 1 x
25ns 0 0 1 1
30ns 0 1 1 1
35ns 1 0 0 1
40ns 0 1 0 0
"""
        interface = "module TopModule(input clock,a, output p,q);"
        test = from_spec(prompt, extract_contract(prompt, interface, "TopModule"))
        self.assertEqual(test.uncertainties, "")
        self.assertNotIn("always", test.stimulus)
        self.assertNotIn("negedge", test.stimulus)
        self.assertEqual(test.stimulus.count("check("), 12)

    def test_moore_oracle_uses_prompt_transition_table(self):
        prompt = """The reset state is B and reset is active-high synchronous.
B (out=1) --in=0--> A
B (out=1) --in=1--> B
A (out=0) --in=0--> B
A (out=0) --in=1--> A
"""
        interface = "module TopModule(input clk,reset,in, output out);"
        test = from_spec(prompt, extract_contract(prompt, interface, "TopModule"))
        self.assertIn("synchronous reset off-edge holds", test.stimulus)
        self.assertIn("Moore pre-edge", test.stimulus)
        self.assertIn("0: ref_state=in ? 0 : 1;", test.stimulus)


class AgentLoopTests(unittest.TestCase):
    def run_agent(self, responses, simulations, checker=TEST):
        llm = Mock(backend="openai")
        llm.chat.side_effect = [LLMResult("```verilog\n" + c + "```")
                               if isinstance(c, str) else c for c in responses]
        rtl = Mock(available=True, simulation_available=True, reason="")
        rtl.lint.return_value = (0, "compiled")
        rtl.synth.return_value = (0, "Synthesis finished")
        rtl.simulate.side_effect = simulations
        with tempfile.TemporaryDirectory() as tmp:
            trace = main.Trace(os.path.join(tmp, "trace.jsonl"))
            saved = []
            try:
                with patch.object(main, "RtlToolchain", return_value=rtl), \
                        patch.object(main, "generate_selftest", return_value=checker), \
                        patch.object(main, "review_selftest", return_value=checker), \
                        patch.object(main, "MAX_ROUNDS", len(responses)):
                    answer = main.solve_agent(llm, trace, "q is a AND b", INTERFACE,
                                              "TopModule", save=saved.append)
            finally:
                trace.close()
            events = [json.loads(s) for s in Path(trace.path).read_text(
                encoding="utf-8").splitlines()]
        return answer, saved, events, llm, rtl

    def test_failed_rtl_request_is_timed_without_response_token_usage(self):
        answer, _, events, _, _ = self.run_agent(
            [LLMError("model request failed: ReadTimeout"), GOOD],
            [(0, "AGENT_SELFTEST_PASS checks=4")])
        self.assertEqual(answer, GOOD)
        errors = [e for e in events if e.get("tool") == "llm"
                  and e.get("event") == "error"]
        self.assertEqual(len(errors), 1)
        self.assertGreaterEqual(errors[0]["elapsed_s"], 0)
        self.assertNotIn("tokens_out", errors[0])

    def test_checker_request_errors_keep_phase_and_recover_independently(self):
        for phase in ("selftest", "selftest_review"):
            with self.subTest(phase=phase):
                llm = Mock(backend="openai")
                tail = [LLMResult(json.dumps(TEST.__dict__)),
                        LLMResult('{"approved":true,"issues":[]}')]
                first = [LLMResult(json.dumps(TEST.__dict__))]
                llm.chat.side_effect = (
                    [LLMResult("```verilog\n" + GOOD + "```")]
                    + (first if phase == "selftest_review" else [])
                    + [LLMError("model request failed: ReadTimeout")] + tail)
                rtl = Mock(available=True, simulation_available=True, reason="")
                rtl.lint.return_value = (0, "compiled")
                rtl.synth.return_value = (0, "Synthesis finished")
                rtl.simulate.return_value = (0, "AGENT_SELFTEST_PASS checks=2")
                with tempfile.TemporaryDirectory() as tmp:
                    trace = main.Trace(os.path.join(tmp, "trace.jsonl"))
                    try:
                        with patch.object(main, "RtlToolchain", return_value=rtl), \
                                patch.object(main, "MAX_ROUNDS", 1):
                            answer = main.solve_agent(llm, trace, "q is a AND b",
                                                      INTERFACE, "TopModule")
                    finally:
                        trace.close()
                    events = [json.loads(s) for s in Path(trace.path).read_text(
                        encoding="utf8").splitlines()]
                self.assertEqual(answer, GOOD)
                rtl.simulate.assert_called_once()
                errors = [e for e in events if e.get("tool") == "llm"
                          and e.get("event") == "error"]
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0]["purpose"], phase)
                self.assertGreaterEqual(errors[0]["elapsed_s"], 0)
                self.assertNotIn("tokens_out", errors[0])

    def test_functional_failure_is_repaired_with_counterexample(self):
        answer, saved, events, llm, rtl = self.run_agent(
            [BAD, GOOD], [(1, "AGENT_MISMATCH case=10 actual=1 expected=0"),
                          (0, "AGENT_SELFTEST_PASS checks=4")])
        self.assertEqual(answer, GOOD)
        self.assertEqual(saved[-1], GOOD)
        self.assertIn("AGENT_MISMATCH", str(llm.chat.call_args_list[1]))
        self.assertEqual(rtl.simulate.call_count, 2)
        self.assertTrue(any(e.get("event") == "accept" for e in events))

    def test_bad_latest_candidate_does_not_overwrite_compiled_candidate(self):
        broken = "module Wrong(input a); endmodule\n"
        answer, saved, events, _, _ = self.run_agent(
            [GOOD, broken], [(1, "AGENT_MISMATCH case=x")])
        self.assertEqual(answer, GOOD)
        self.assertEqual(saved[-1], GOOD)
        self.assertFalse(any(e.get("event") == "accept" for e in events))

    def test_only_wrong_interface_returns_empty_solution(self):
        answer, saved, _, _, _ = self.run_agent(
            ["module Wrong(input a); endmodule\n"], [])
        self.assertEqual(answer, "")
        self.assertEqual(saved, [])

    def test_duplicate_candidate_skips_tools(self):
        _, _, events, _, rtl = self.run_agent(
            [BAD, BAD, GOOD], [(1, "AGENT_MISMATCH case=10"),
                               (0, "AGENT_SELFTEST_PASS checks=4")])
        self.assertEqual(rtl.lint.call_count, 2)
        self.assertTrue(any(e.get("event") == "duplicate" for e in events))

    def test_repeated_failures_restart_without_previous_rtl(self):
        _, _, events, llm, _ = self.run_agent(
            [BAD, BAD, BAD, GOOD], [(1, "AGENT_MISMATCH case=10"),
                                    (0, "AGENT_SELFTEST_PASS checks=4")])
        fourth_request = llm.chat.call_args_list[3].args[0][-1]["content"]
        self.assertIn("old RTL is deliberately omitted", fourth_request)
        self.assertNotIn("assign q = a;", fourth_request)
        self.assertTrue(any(e.get("event") == "restart_from_spec" for e in events))

    def test_invalid_checker_never_counts_as_dut_failure_or_acceptance(self):
        answer, _, events, _, _ = self.run_agent(
            [GOOD], [(2, "ERROR: invalid checker")] * 3)
        self.assertEqual(answer, GOOD)
        self.assertFalse(any(e.get("event") == "accept" for e in events))

    def test_failed_oracle_review_never_runs_unreviewed_checker(self):
        llm = Mock(backend="openai")
        llm.chat.return_value = LLMResult("```verilog\n" + GOOD + "```")
        rtl = Mock(available=True, simulation_available=True, reason="")
        rtl.lint.return_value = (0, "compiled")
        rtl.synth.return_value = (0, "Synthesis finished")
        with tempfile.TemporaryDirectory() as tmp:
            trace = main.Trace(os.path.join(tmp, "trace.jsonl"))
            try:
                with patch.object(main, "RtlToolchain", return_value=rtl), \
                        patch.object(main, "generate_selftest", return_value=TEST), \
                        patch.object(main, "review_selftest", side_effect=ValueError("bad review")), \
                        patch.object(main, "MAX_ROUNDS", 1):
                    self.assertEqual(main.solve_agent(
                        llm, trace, "AND", INTERFACE, "TopModule"), GOOD)
            finally:
                trace.close()
        rtl.simulate.assert_not_called()

    def test_review_rejection_rebuilds_independently_before_simulation(self):
        rebuilt = SelfTest("", TEST.stimulus +
                           '\na=0; b=1; #1; check(q,0,1,"added mixed input");',
                           "Includes a=0 and b=1")
        llm = Mock(backend="openai")
        llm.chat.side_effect = [
            LLMResult("```verilog\n" + GOOD + "```"),
            LLMResult(json.dumps(TEST.__dict__)),
            LLMResult('{"approved":false,"issues":["Cover a=0,b=1"]}'),
            LLMResult(json.dumps(rebuilt.__dict__)),
            LLMResult('{"approved":true,"issues":[]}'),
        ]
        rtl = Mock(available=True, simulation_available=True, reason="")
        rtl.lint.return_value = (0, "compiled")
        rtl.synth.return_value = (0, "Synthesis finished")
        rtl.simulate.return_value = (0, "AGENT_SELFTEST_PASS checks=3")
        with tempfile.TemporaryDirectory() as tmp:
            trace = main.Trace(os.path.join(tmp, "trace.jsonl"))
            try:
                with patch.object(main, "RtlToolchain", return_value=rtl), \
                        patch.object(main, "MAX_ROUNDS", 1):
                    self.assertEqual(main.solve_agent(
                        llm, trace, "q is a AND b", INTERFACE, "TopModule"), GOOD)
            finally:
                trace.close()
            events = [json.loads(s) for s in Path(trace.path).read_text(
                encoding="utf-8").splitlines()]
        self.assertEqual(llm.chat.call_count, 5)
        rtl.simulate.assert_called_once()
        self.assertIn("added mixed input", rtl.simulate.call_args.args[1])
        rebuild_request = llm.chat.call_args_list[3].args[0]
        self.assertIn("Cover a=0,b=1", str(rebuild_request))
        for call in llm.chat.call_args_list[1:]:
            self.assertNotIn("assign q", str(call.args[0]))
        starts = [e for e in events if e.get("purpose") == "selftest_review"
                  and e.get("event") == "start"]
        approvals = [e for e in events if e.get("event") == "review_approved"]
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["checker_sha256"], starts[-1]["checker_sha256"])


class BudgetTests(unittest.TestCase):
    def test_truncated_module_never_becomes_fallback(self):
        self.assertEqual(extract_code("```verilog\nmodule TopModule(input a);\n```"), "")
        self.assertEqual(extract_code(
            "```verilog\nmodule TopModule;\n// endmodule\n```"), "")
        self.assertEqual(extract_code("```verilog\n" + GOOD + "```"), GOOD)

    def test_expired_budget_does_not_call_model(self):
        llm = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            trace = main.Trace(os.path.join(tmp, "trace.jsonl"))
            try:
                with patch.object(main.time, "monotonic", return_value=100):
                    self.assertEqual(main.solve_agent(
                        llm, trace, "AND", INTERFACE, "TopModule", deadline_at=99), "")
            finally:
                trace.close()
        llm.chat.assert_not_called()

    def test_generation_call_reserves_time_for_verification(self):
        llm = Mock(backend="mock")
        llm.chat.return_value = LLMResult("```verilog\n" + GOOD + "```")
        rtl = Mock(available=False, reason="no EDA")
        rtl.lint.return_value = (-1, "no EDA")
        with tempfile.TemporaryDirectory() as tmp:
            trace = main.Trace(os.path.join(tmp, "trace.jsonl"))
            try:
                with patch.object(main, "RtlToolchain", return_value=rtl), \
                        patch.object(main.time, "monotonic", return_value=100):
                    main.solve_agent(llm, trace, "AND", INTERFACE, "TopModule",
                                     deadline_at=200)
            finally:
                trace.close()
        self.assertEqual(llm.chat.call_args.kwargs["timeout_s"], 65)

    def test_error_fingerprint_ignores_scratch_path_and_line(self):
        self.assertEqual(main.error_fingerprint(
            "WARNING: [VRFC 10-3705] index 255 out of bounds [/tmp/a/solution.sv:12]"),
            main.error_fingerprint(
            "WARNING: [VRFC 10-3705] index 255 out of bounds [/var/tmp/b/solution.sv:90]"))

    def test_llm_timeout_clamped_and_network_errors_wrapped(self):
        llm = LLM()
        llm.backend, llm.base_url = "openai", "http://localhost:1/v1"
        import requests
        with patch.object(requests, "post", side_effect=requests.Timeout) as post:
            with self.assertRaises(LLMError):
                llm.chat([], timeout_s=2)
        self.assertEqual(post.call_args.kwargs["timeout"], 2)

    def test_compile_and_elaboration_share_one_budget(self):
        rtl = tools.RtlToolchain()
        rtl.reason = ""
        with patch.object(tools.time, "monotonic", side_effect=[0, 4]), \
                patch.object(tools, "_run", return_value=(0, "")) as run:
            rtl.lint(GOOD, "TopModule", timeout_s=10)
        self.assertEqual(run.call_args_list[1].args[-1], 6)

    def test_exhausted_budget_does_not_launch_process(self):
        with patch.object(tools.subprocess, "Popen") as popen:
            self.assertEqual(tools._run(["nothing"], ".", 0)[0], 124)
        popen.assert_not_called()


@unittest.skipUnless(os.name == "posix", "requires Linux signals and process groups")
class DeadlineProcessTests(unittest.TestCase):
    def test_alarm_returns_saved_candidate_while_solver_is_blocked(self):
        source = """
import sys, time
from unittest.mock import patch
from agent import main
def blocked(llm, trace, prompt, interface, top, save, deadline_at):
    save('module TopModule; endmodule\\n')
    time.sleep(60)
with patch.object(main, 'DEADLINE_S', 31), \\
     patch.object(main, 'solve_agent', side_effect=blocked):
    raise SystemExit(main.main(['--input', sys.argv[1], '--output', sys.argv[2]]))
"""
        with tempfile.TemporaryDirectory() as tmp:
            started = time.monotonic()
            env = dict(os.environ, LLM_BACKEND="mock", AGENT_RESERVE_S="30")
            env.pop("AGENT_DEADLINE_MONOTONIC", None)
            result = subprocess.run(
                [sys.executable, "-c", source, tmp, tmp],
                cwd=Path(__file__).resolve().parents[1], env=env,
                capture_output=True, text=True, timeout=6)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(time.monotonic() - started, 5)
            code = (Path(tmp) / "solution.v").read_text()
            self.assertEqual(code, "module TopModule; endmodule\n")
            events = [json.loads(line) for line in
                      (Path(tmp) / "trace.jsonl").read_text().splitlines()]
            self.assertTrue(any(e.get("tool") == "budget" for e in events))
            self.assertEqual(events[-1]["bytes"], len(code.encode()))

    def test_tool_inherits_task_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, output = tools._run(
                [sys.executable, "-c", "import os; print(os.getpgrp())"], tmp, 3)
        self.assertEqual(rc, 0)
        self.assertEqual(int(output), os.getpgrp())


if __name__ == "__main__":
    unittest.main()
