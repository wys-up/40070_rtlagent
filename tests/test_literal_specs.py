import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent import main
from agent.fsm import graph_interface, literal_moore_graph, literal_mealy_graph
from agent.fsm import generate_graph, UnsupportedGraph
from agent.llm import LLMResult
from agent.reference import generate_reference
from agent.oracles import from_spec, literal_implementation
from agent.spec_contract import extract_contract
from agent.tools import RtlToolchain
from agent.verification import SelfTest


def moore_spec(low=0, falling=False, asynchronous=False):
    prompt = ("Moore state machine. Active-low "
              + ("asynchronous" if asynchronous else "synchronous")
              + " reset to state Rest. "
              + ("Negative" if falling else "Positive") + " edge clock.\n"
              + f"Rest (flag={low}) --enter=0--> Rest\n"
              + f"Rest (flag={low}) --enter=1--> Busy\n"
              + f"Busy (flag={1-low}) --leave=0--> Busy\n"
              + f"Busy (flag={1-low}) --leave=1--> Rest")
    header = "module Device(input clock,rst_n,leave,enter,output flag);"
    return prompt, header, extract_contract(prompt, header, "Device")


def onehot_spec(width):
    encoding = ", ".join(f"{1 << (width-1-i):0{width}b}(N{i})" for i in range(width))
    edges = "\n".join(f"N{i} (0) --0--> N{i}\n"
                      f"N{i} (0) --1--> N{(i+1) % width}" for i in range(width))
    prompt = (f"One-hot state assignment present[{width-1}:0] = {encoding}\n"
              + edges
              + f"\nOutput signal head should be the input of state flip-flop present[{width-1}]."
              + "\nOutput signal tail should be the input of state flip-flop present[0].")
    header = f"module Device(input advance,input [{width-1}:0] present,output tail,head);"
    return prompt, header, extract_contract(prompt, header, "Device")


def lookup_spec(width):
    values = {i: (i*19+11) & 255 for i in range(1 << width)}
    prompt = f"Combinational circuit. Waveform values are hexadecimal.\ntime index result\n"
    prompt += "0ns x x\n5ns x x\n"
    prompt += "\n".join(f"{10+5*i}ns {i:x} {v:02x}" for i, v in values.items())
    header = f"module Device(input [{width-1}:0] index,output [7:0] result);"
    return prompt, header, extract_contract(prompt, header, "Device"), values


class LiteralSpecTests(unittest.TestCase):
    def test_literal_mealy_outputs_depend_on_current_input_without_a_clock(self):
        prompt = """Mealy finite-state machine, one-hot encoding.
Resets into state Wait. Active-high asynchronous reset.
Wait --data=0 (answer=1)--> Wait
Wait --data=1 (answer=0)--> Seen
Seen --data=0 (answer=0)--> Seen
Seen --data=1 (answer=1)--> Wait"""
        header = "module Device(input clk,reset,data,output answer);"
        contract = extract_contract(prompt, header, "Device")
        graph = literal_mealy_graph(prompt, contract)
        self.assertEqual(graph["kind"], "mealy")
        test = from_spec(prompt, contract)
        code = literal_implementation(test, contract, prompt)
        self.assertIn("always @* begin", code)
        self.assertIn("1'b0: begin answer=1'b1;", code)
        self.assertIn("1'b1: begin answer=1'b0;", code)
        self.assertIn("reg [1:0] state;", code)
        self.assertIn("if(reset) state <= 2'd2;", code)
        self.assertIn("pre-edge", test.stimulus)
        for malformed in (
                prompt.replace("Seen --data=1 (answer=1)--> Wait", ""),
                prompt.replace("Wait --data=1", "Wait --missing=1"),
                prompt.replace("(answer=0)", "(other=0)", 1),
                prompt.replace("--> Seen", "--> Missing", 1)):
            self.assertIsNone(literal_mealy_graph(malformed, contract))

    def test_truncated_graph_plan_falls_back_without_repeating_the_same_plan(self):
        prompt, header, contract = moore_spec()
        llm = Mock()
        llm.chat.return_value = LLMResult("unfinished derivation", finish_reason="length")
        with self.assertRaisesRegex(UnsupportedGraph, "truncated"):
            generate_graph(llm, prompt, contract, "Device", 120, 2000)
        llm.chat.assert_called_once()
        reference = (header.replace("Device", "SpecReference") +
                     "reg state; always @(posedge clock) "
                     "if(!rst_n) state<=0; else state<=state ? !leave : enter;"
                     "assign flag=state; endmodule")
        llm.reset_mock()
        llm.chat.side_effect = [
            LLMResult("unfinished derivation", finish_reason="length"),
            LLMResult("```verilog\n" + reference + "```")]
        test = generate_reference(llm, prompt, header, contract, 120)
        self.assertIn("state ? !leave : enter", test.reference)
        self.assertEqual(llm.chat.call_count, 2)
        self.assertIn("REFERENCE module", llm.chat.call_args.args[0][0]["content"])

    def test_mealy_does_not_enter_a_registered_output_graph_compiler(self):
        prompt, header, contract = moore_spec()
        for text in (prompt.replace("Moore", "Mealy"),
                     prompt + "\nThe output is a Mealy function of the current input."):
            self.assertIsNone(graph_interface(text, contract))
            self.assertIsNone(literal_moore_graph(text, contract))
        self.assertIsNotNone(graph_interface(prompt, contract))

    def test_moore_each_state_reads_its_own_named_control_in_interface_order(self):
        prompt, _, contract = moore_spec()
        graph = literal_moore_graph(prompt, contract)
        rows = {s["name"]: s for s in graph["states"]}
        self.assertEqual(rows["Rest"]["next"],
                         {"00": "Rest", "01": "Busy", "10": "Rest", "11": "Busy"})
        self.assertEqual(rows["Busy"]["next"],
                         {"00": "Busy", "01": "Busy", "10": "Rest", "11": "Rest"})
        test = from_spec(prompt, contract)
        self.assertEqual(json.loads(test.derivation), graph)
        code = literal_implementation(test, contract, prompt)
        self.assertIn("always @(posedge clock)", code)
        self.assertNotIn("or negedge rst_n", code)
        changed = prompt.replace("Busy (flag=1)", "Busy (flag=0)")
        self.assertNotEqual(code, literal_implementation(from_spec(changed, contract),
                                                        contract, changed))

    def test_moore_ambiguous_incomplete_or_unsupported_arrows_fall_back(self):
        prompt, _, contract = moore_spec()
        malformed = (
            prompt.replace("Rest (flag=0) --enter=0--> Rest", ""),
            prompt + "\nRest (flag=0) --enter=0--> Rest",
            prompt.replace("--enter=0-->", "--missing=0-->"),
            prompt.replace("--leave=1--> Rest", "--leave=1--> Missing"),
            prompt.replace("Busy (flag=1) --leave=0", "Busy (flag=0) --leave=0"),
            prompt.replace("--leave=0-->", "--leave=0 && enter=0-->"),
            prompt.replace("reset to state Rest", "reset to an unspecified state"),
            prompt + "\nreset state is Busy",
        )
        for text in malformed:
            with self.subTest(text=text):
                self.assertIsNone(literal_moore_graph(text, contract))
                self.assertIsNone(from_spec(text, contract))

    def test_literal_moore_bypasses_model_but_not_tool_validation(self):
        prompt, header, _ = moore_spec()
        llm = Mock(backend="openai")
        rtl = Mock(available=True, simulation_available=True, reason="")
        rtl.lint.return_value = (0, "compiled")
        rtl.synth.return_value = (0, "synthesized")
        rtl.simulate.return_value = (0, "AGENT_SELFTEST_PASS checks=258")
        with tempfile.TemporaryDirectory() as tmp:
            trace = main.Trace(os.path.join(tmp, "trace.jsonl"))
            try:
                with patch.object(main, "RtlToolchain", return_value=rtl):
                    answer = main.solve_agent(llm, trace, prompt, header, "Device")
            finally:
                trace.close()
            events = [json.loads(line) for line in Path(trace.path).read_text(
                encoding="utf-8").splitlines()]
        llm.chat.assert_not_called()
        self.assertIn("module Device", answer)
        rtl.lint.assert_called_once()
        rtl.synth.assert_called_once()
        rtl.simulate.assert_called_once()
        self.assertTrue(any(e.get("event") == "accept" for e in events))

    def test_onehot_exhaustive_facts_follow_variable_width_and_encoding(self):
        for width in (3, 5):
            prompt, _, contract = onehot_spec(width)
            test = from_spec(prompt, contract)
            self.assertEqual(len(test.facts), 2 << width)
            for fact in test.facts:
                mask, advance = fact["inputs"]["present"], fact["inputs"]["advance"]
                rotated = (mask >> 1) | ((mask & 1) << (width-1)) if advance else mask
                self.assertEqual(fact["outputs"],
                                 dict(head=rotated >> (width-1), tail=rotated & 1))
            code = literal_implementation(test, contract)
            self.assertIn(f"input [{width-1}:0] present", code)
            changed = prompt.replace("N0 (0) --1--> N1", "N0 (0) --1--> N0")
            self.assertNotEqual(code, literal_implementation(from_spec(changed, contract),
                                                            contract))
            for invalid in (prompt.replace("N0 (0) --0--> N0", ""),
                            prompt.replace("N0 (0) --1--> N1", "N0 (0) --1--> Missing"),
                            prompt.replace("present[0].", f"present[{width}].")):
                self.assertIsNone(from_spec(invalid, contract))

    def test_lookup_literal_observations_require_complete_consistent_inputs(self):
        for width in (2, 4):
            prompt, _, contract, values = lookup_spec(width)
            test = from_spec(prompt, contract)
            self.assertEqual(len(test.facts), 1 << width)
            self.assertEqual({f["inputs"]["index"]: f["outputs"]["result"]
                              for f in test.facts}, values)
            code = literal_implementation(test, contract)
            self.assertIn("output reg [7:0] result", code)
            partial = prompt.rsplit("\n", 1)[0]
            self.assertIsNone(literal_implementation(from_spec(partial, contract), contract))
            conflicting = prompt + f"\n999ns 0 {values[0] ^ 1:02x}"
            self.assertIsNone(from_spec(conflicting, contract))
            changed = prompt.replace(f"10ns 0 {values[0]:02x}", "10ns 0 fe")
            self.assertNotEqual(code, literal_implementation(from_spec(changed, contract),
                                                            contract))

    def test_lookup_ambiguous_numeric_radix_is_not_inferred(self):
        prompt = "Combinational circuit.\ntime index result\n0ns 0 00\n5ns 1 10\n10ns 2 11"
        contract = extract_contract(prompt, "module Device(input [1:0] index,"
                                    "output [7:0] result);", "Device")
        self.assertIsNone(from_spec(prompt, contract))
        explicit = from_spec("Waveform values are hexadecimal.\n" + prompt, contract)
        self.assertIsNotNone(explicit)

    def test_legacy_comparison_history_does_not_claim_one_edge_per_check(self):
        feedback = main.repair_feedback(
            "AGENT_INPUT sample=0 clk=0 d=1\nAGENT_MISMATCH waveform 10ns",
            sampled_vectors=False)
        self.assertIn("not sampling edges", feedback)
        self.assertIn("waveform timestamps", feedback)
        self.assertNotIn("ONE sampling edge", feedback)


class LiteralVivadoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rtl = RtlToolchain()
        if not cls.rtl.simulation_available:
            raise unittest.SkipTest("Vivado simulation tools unavailable")

    def test_moore_matches_independent_recurrence_and_detects_wrong_guard_latency_reset(self):
        for low, falling, asynchronous in ((0, False, False), (1, True, True)):
            prompt, header, contract = moore_spec(low, falling, asynchronous)
            edge = "negedge" if falling else "posedge"
            events = edge + " clock" + (" or negedge rst_n" if asynchronous else "")
            recurrence = ("reg history; always @(" + events + ") "
                          "if(!rst_n) history<=0; else "
                          "history<=history ? !leave : enter;\n"
                          + f"assign flag=history ^ 1'b{low}; endmodule")
            independent = header + recurrence
            test = from_spec(prompt, contract)
            for code, expected in (
                    (independent, 0),
                    (independent.replace("history ? !leave : enter",
                                         "history ? !enter : leave"), 1)):
                rc, log = self.rtl.simulate(code, test.render(contract), 90)
                self.assertEqual(rc, expected, log)
            if not asynchronous:
                async_code = independent.replace("@(posedge clock)",
                                                 "@(posedge clock or negedge rst_n)")
                rc, log = self.rtl.simulate(async_code, test.render(contract), 90)
                self.assertEqual(rc, 1, log)
            delayed = independent.replace(
                f"assign flag=history ^ 1'b{low};",
                f"reg delayed; always @({events}) "
                f"if(!rst_n) delayed<=1'b{low}; else delayed<=history ^ 1'b{low}; "
                "assign flag=delayed;")
            rc, log = self.rtl.simulate(delayed, test.render(contract), 90)
            self.assertEqual(rc, 1, log)
            code = literal_implementation(test, contract, prompt)
            independent_test = SelfTest(
                "integer i,seed; reg expected;",
                f"""clock={int(falling)}; rst_n=0; expected=0; seed=13;
#1; clock={int(not falling)}; #1; clock={int(falling)}; #1; rst_n=1;
for(i=0;i<128;i=i+1) begin
  enter=$random(seed); leave=$random(seed); #1;
  check(flag,expected ^ 1'b{low},1,"current-state decode");
  expected=expected ? !leave : enter;
  clock={int(not falling)}; #1;
  check(flag,expected ^ 1'b{low},1,"independent recurrence");
  clock={int(falling)}; #1;
end""", "Independent two-control recurrence, both clock edges and reset styles")
            rc, log = self.rtl.simulate(code, independent_test.render(contract), 90)
            self.assertEqual(rc, 0, log)

    def test_mealy_literal_compilation_matches_independent_recurrence_and_catches_registered_output(self):
        prompt = """Mealy finite-state machine. One-hot encoding.
Resets into state Clear. Active-low synchronous reset.
Clear --data=0 (answer=0)--> Clear
Clear --data=1 (answer=1)--> Marked
Marked --data=0 (answer=1)--> Marked
Marked --data=1 (answer=0)--> Marked"""
        header = "module Device(input clk,rst_n,data,output answer);"
        contract = extract_contract(prompt, header, "Device")
        test = from_spec(prompt, contract)
        source = (header + "reg seen; always @(posedge clk) "
                  "if(!rst_n) seen<=0; else seen<=seen | data; "
                  "assign answer=seen ^ data; endmodule")
        for code, expected in (
                (source, 0),
                (source.replace("assign answer=seen ^ data;",
                                "reg sampled; always @(posedge clk) "
                                "if(!rst_n) sampled<=0; else sampled<=seen ^ data;"
                                "assign answer=sampled;"), 1)):
            rc, log = self.rtl.simulate(code, test.render(contract), 90)
            self.assertEqual(rc, expected, log)
        compiled = literal_implementation(test, contract, prompt)
        independent = SelfTest("integer i,seed; reg history;", """
clk=0; rst_n=0; data=0; history=0; seed=73;
#1; clk=1; #1; clk=0; #1; rst_n=1;
for(i=0;i<128;i=i+1) begin
  data=$random(seed); #1;
  check(answer,history ^ data,1,"Mealy before edge");
  clk=1; history=history | data; #1;
  check(answer,history ^ data,1,"Mealy after edge");
  clk=0; #1;
end""", "Independent prefix recurrence and input changes off-edge")
        rc, log = self.rtl.simulate(compiled, independent.render(contract), 90)
        self.assertEqual(rc, 0, log)

    def test_waveform_coincident_data_changes_follow_sampling_edge(self):
        for edge, clk, inactive in (("posedge", "clk", 0), ("negedge", "clock", 1)):
            rows = [(0, inactive, 0, "x"), (5, 1-inactive, 1, "0"),
                    (10, inactive, 1, "0"), (15, 1-inactive, 0, "1"),
                    (20, inactive, 0, "1"), (25, 1-inactive, 1, "0")]
            prompt = f"time {clk} payload sampled observed\n" + "\n".join(
                f"{t}ns {clock} {data} {sampled} {data}"
                for t, clock, data, sampled in rows)
            header = (f"module Device(input {clk},payload,output reg sampled,"
                      "output observed);")
            contract = extract_contract(prompt, header, "Device")
            test = from_spec(prompt, contract)
            for expression, expected in (("payload", 0), ("~payload", 1)):
                code = (header + f"always @({edge} {clk}) sampled<={expression};"
                        "assign observed=payload; endmodule")
                rc, log = self.rtl.simulate(code, test.render(contract), 90)
                self.assertEqual(rc, expected, log)

    def test_compiled_onehot_matches_independent_rotation(self):
        for width in (3, 5):
            prompt, _, contract = onehot_spec(width)
            code = literal_implementation(from_spec(prompt, contract), contract)
            test = SelfTest(f"integer i,j; reg [{width-1}:0] expected;", f"""
for(i=0;i<{1 << width};i=i+1) begin
  for(j=0;j<2;j=j+1) begin
    present=i; advance=j; #1;
    expected=j ? {{present[0],present[{width-1}:1]}} : present;
    check(head,expected[{width-1}],1,"rotation high bit");
    check(tail,expected[0],1,"rotation low bit");
  end
end""", "All source masks, including zero and simultaneous asserted sources")
            rc, log = self.rtl.simulate(code, test.render(contract), 90)
            self.assertEqual(rc, 0, log)

    def test_compiled_lookup_matches_independent_arithmetic_for_every_input(self):
        prompt, _, contract, _ = lookup_spec(3)
        code = literal_implementation(from_spec(prompt, contract), contract)
        test = SelfTest("integer i; reg [7:0] expected;", """
for(i=0;i<8;i=i+1) begin
  index=i; expected=i*19+11; #1;
  check(result,expected,8'hff,"independent affine mapping");
end""", "Independent arithmetic, not generated observation constants")
        rc, log = self.rtl.simulate(code, test.render(contract), 90)
        self.assertEqual(rc, 0, log)
