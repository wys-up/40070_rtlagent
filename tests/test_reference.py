import unittest
import json
from unittest.mock import Mock, patch

from agent.main import repair_feedback, simulation_evidence
from agent.reference import build_reference, validate_review_witness
from agent.spec_contract import extract_contract
from agent.tools import RtlToolchain, source_safety_warnings
from agent.llm import LLMResult
from agent.verification import review_selftest
from agent.oracles import from_spec


class ReferenceTests(unittest.TestCase):
    def test_disproved_allegation_needs_no_second_model_opinion(self):
        prompt = "Output q is the parity of d."
        interface = "module TopModule(input [3:0] d,output q);"
        contract = extract_contract(prompt, interface, "TopModule")
        test = build_reference(
            "module SpecReference(input [3:0] d,output q); assign q=^d; endmodule",
            contract, prompt)
        issue = dict(requirement="q is the parity of d", location="assign q",
                     counterexample=dict(inputs=[dict(d=0)], output="q", expected=0))
        llm = Mock()
        llm.chat.return_value = LLMResult(json.dumps(dict(approved=False, issues=[issue])))
        with patch("agent.reference.validate_review_witness",
                   side_effect=ValueError("disproved by simulation")):
            with self.assertRaisesRegex(ValueError, "disproved"):
                review_selftest(llm, prompt, interface, contract, test, 45)
        llm.chat.assert_called_once()

    def test_wrong_witness_expectation_cannot_reject_reference(self):
        prompt = "Output q is the parity of d."
        interface = "module TopModule(input [3:0] d,output q);"
        contract = extract_contract(prompt, interface, "TopModule")
        test = build_reference(
            "module SpecReference(input [3:0] d,output q); assign q=^d; endmodule",
            contract, prompt)
        issue = dict(requirement="q is the parity of d", location="assign q",
                     counterexample=dict(inputs=[dict(d=0)], output="q", expected=1))
        llm = Mock()
        llm.chat.side_effect = [
            LLMResult(json.dumps(dict(approved=False, issues=[issue]))),
            LLMResult('{"expected":0,"reason":"zero has even parity"}')]
        with patch("agent.reference.validate_review_witness") as replay:
            with self.assertRaisesRegex(ValueError, "not supported"):
                review_selftest(llm, prompt, interface, contract, test, 45)
            replay.assert_called_once()
        independent = str(llm.chat.call_args.args[0])
        self.assertNotIn("assign q=^d", independent)
        self.assertNotIn('"expected": 1', independent)

    def test_review_audits_source_graph_without_candidate_or_expanded_noise(self):
        prompt = "Moore FSM with active-high synchronous reset."
        interface = "module Unit(input clk,reset,x,output reg q);"
        contract = extract_contract(prompt, interface, "Unit")
        test = build_reference(
            interface.replace("Unit", "SpecReference") +
            "always @(posedge clk) if(reset) q<=0; else q<=x; endmodule", contract, prompt)
        test.derivation = '{"source_graph":"from current specification"}'
        llm = Mock()
        llm.chat.return_value = LLMResult('{"approved":true,"issues":[]}')
        review_selftest(llm, prompt, interface, contract, test, 30)
        payload = llm.chat.call_args.args[0][1]["content"]
        self.assertIn("source_graph", payload)
        self.assertNotIn("else q<=x", payload)
        self.assertIn("repeat.count", llm.chat.call_args.args[0][0]["content"])

    def test_unsigned_negative_comparison_is_not_a_trusted_reference(self):
        interface = "module Device(input [5:0] d,output q);"
        contract = extract_contract("", interface, "Device")
        for name, width, bound in (("temporary", 7, 12), ("wide_delta", 15, 27)):
            code = (f"module SpecReference(input [5:0] d,output q); "
                    f"reg [{width-1}:0] {name}; always @* {name}=$signed(d); "
                    f"assign q=({name} < -{bound}); endmodule")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unsigned"):
                build_reference(code, contract, "")
            signed = code.replace(f"reg [{width-1}:0]", f"reg signed [{width-1}:0]")
            self.assertFalse(source_safety_warnings(signed, contract.ports))
            self.assertFalse(source_safety_warnings(
                code.replace(f"{name} < -{bound}", f"$signed({name}) < -{bound}"),
                contract.ports))

    def test_literal_observations_change_with_axes_cells_and_port_order(self):
        prompt = """Karnaugh map
     yz
x 10 00 11 01
1 | 0 | 1 | x | 0 |
0 | 1 | 0 | 0 | 1 |
"""
        contract = extract_contract("", "module Device(input z,x,y,output answer);", "Device")
        test = from_spec(prompt, contract)
        self.assertIsNotNone(test)
        self.assertIn(dict(inputs=dict(y=0, z=0, x=1),
                           outputs=dict(answer=1)), test.facts)
        changed = from_spec(prompt.replace("1 | 0 | 1 | x | 0 |",
                                           "1 | 1 | 0 | x | 1 |"), contract)
        self.assertNotEqual(test.facts, changed.facts)
    def test_small_combinational_exhaustive_driver_uses_real_outputs(self):
        contract = extract_contract("", "module TopModule(input [2:0] a,output q);",
                                    "TopModule")
        test = build_reference(
            "module SpecReference(input [2:0] a,output q); assign q=^a; endmodule",
            contract, "")
        self.assertIn("vector_index<8", test.stimulus)
        self.assertIn("check(q,ref_q,care_q", test.stimulus)
        self.assertIn("SpecReference reference_model", test.render(contract))

    def test_reference_cannot_read_dut_or_write_verdict(self):
        contract = extract_contract("", "module TopModule(input a,output q);", "TopModule")
        for body in ("assign q=dut.q;", '$display("pass");',
                     "assign q=agent_checks;", "assign q=other.hidden;"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                build_reference(
                    "module SpecReference(input a,output q); " + body + " endmodule",
                    contract, "")

    def test_namespace_and_negative_literal_normalization_preserve_behavior(self):
        interface = "module TopModule(input a,output signed [5:0] q);"
        contract = extract_contract("", interface, "TopModule")
        test = build_reference(
            "module TopModule(input a,output signed [5:0] q); "
            "assign q=a ? 6'sd-7 : 6'sd3; endmodule", contract, "")
        self.assertIn("module SpecReference", test.reference)
        self.assertIn("-6'sd7", test.reference)

    def test_clock_reset_and_width_are_derived_without_task_identity(self):
        contract = extract_contract(
            "Active-low synchronous reset. Negative edge clock.",
            "module TopModule(input clock,rst_n,input [12:0] data,output [12:0] q);",
            "TopModule")
        test = build_reference(
            "module SpecReference(input clock,rst_n,input [12:0] data,"
            "output reg [12:0] q); always @(negedge clock) "
            "if(!rst_n) q<=0; else q<=data; endmodule", contract, "")
        self.assertIn("clock=1;", test.stimulus)
        self.assertIn("rst_n=0;", test.stimulus)
        self.assertIn("post-edge", test.stimulus)
        self.assertIn("care_q", test.declarations)

    def test_incomplete_runs_cannot_be_ranked_by_error_count(self):
        self.assertIsNone(simulation_evidence("AGENT_MISMATCH x\nfatal", "hash"))
        self.assertEqual(simulation_evidence(
            "AGENT_SELFTEST_RESULT checks=14 failures=6", "hash"), ("hash", 14, 6))
        self.assertIsNone(simulation_evidence(
            "AGENT_SELFTEST_RESULT checks=2 failures=3", "hash"))

    def test_repair_keeps_first_counterexample_and_input_history(self):
        text = ("noise\nAGENT_INPUT sample=1 d=0\n"
                "AGENT_MISMATCH first\nAGENT_MISMATCH later\n"
                "AGENT_SELFTEST_RESULT checks=10 failures=2\ncompiler noise")
        feedback = repair_feedback(text)
        self.assertIn("sample=1", feedback)
        self.assertIn("AGENT_MISMATCH first", feedback)
        self.assertTrue(feedback.startswith("AGENT_MISMATCH first"))
        self.assertNotIn("later", feedback)
        self.assertNotIn("compiler noise", feedback)

    def test_reference_review_requires_a_real_requirement_and_code_location(self):
        prompt = "Output q is the parity of d."
        interface = "module TopModule(input [3:0] d,output q);"
        contract = extract_contract(prompt, interface, "TopModule")
        test = build_reference(
            "module SpecReference(input [3:0] d,output q); assign q=^d; endmodule",
            contract, prompt)
        issue = dict(requirement="q is the parity of d", location="assign q=^d",
                     counterexample="d=1: expected q=1")
        for key, bad in (("requirement", "data byte must be observable"),
                         ("location", "assign missing=1"),
                         ("counterexample", "The code correctly implements this.")):
            llm = Mock()
            wrong = dict(issue, **{key: bad})
            llm.chat.return_value = LLMResult(json.dumps(dict(
                approved=False, issues=[wrong])))
            with self.subTest(key=key), self.assertRaises(ValueError):
                review_selftest(llm, prompt, interface, contract, test, 20)


class ReferenceVivadoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rtl = RtlToolchain()
        if not cls.rtl.simulation_available:
            raise unittest.SkipTest("Vivado simulation tools unavailable")

    def test_full_measurement_and_counterexample_history(self):
        contract = extract_contract("", "module TopModule(input [3:0] d,output q);",
                                    "TopModule")
        test = build_reference(
            "module SpecReference(input [3:0] d,output q); assign q=^d; endmodule",
            contract, "")
        for expression, expected in (("^d", 0), ("~^d", 1)):
            rc, log = self.rtl.simulate(
                "module TopModule(input [3:0] d,output q); assign q="
                + expression + "; endmodule", test.render(contract), 90)
            self.assertEqual(rc, expected, log)
            self.assertIn(f"checks=16 failures={16 if expected else 0}", log)
            if expected:
                self.assertIn("AGENT_INPUT", log)

    def test_false_review_allegation_is_disproved_before_rebuilding(self):
        contract = extract_contract("", "module TopModule(input a,b,output q);", "TopModule")
        test = build_reference(
            "module   SpecReference(input a,b,output q); assign q=a&b; endmodule",
            contract, "")
        witness = dict(inputs=[dict(a=1, b=0)], output="q", expected=0)
        with self.assertRaisesRegex(ValueError, "disproved by simulation"):
            validate_review_witness(test, contract, witness, 90)
        test.reference = test.reference.replace("a&b", "a|b")
        log = validate_review_witness(test, contract, witness, 90)
        self.assertIn("AGENT_MISMATCH", log)

    def test_review_repeat_is_expanded_into_exact_sampling_edges(self):
        prompt = "Active-high synchronous reset; positive edge clock."
        header = "module TopModule(input clk,reset,x,output reg [2:0] q);"
        contract = extract_contract(prompt, header, "TopModule")
        reference = header.replace("TopModule", "SpecReference") + (
            "always @(posedge clk) if(reset) q<=0; else if(x) q<=q+1'b1; endmodule")
        test = build_reference(reference, contract, prompt)
        witness = dict(inputs=[dict(values=dict(x=1), repeat=4)], output="q", expected=4)
        with self.assertRaisesRegex(ValueError, "disproved by simulation"):
            validate_review_witness(test, contract, witness, 90)
        witness["inputs"][0]["repeat"] = 65
        with self.assertRaisesRegex(ValueError, "1..64"):
            validate_review_witness(test, contract, witness, 90)

    def test_clocked_driver_catches_output_latency(self):
        prompt = "Active-high synchronous reset. Sample d on the positive edge."
        header = "module TopModule(input clk,reset,input [3:0] d,output reg [3:0] q);"
        contract = extract_contract(prompt, header, "TopModule")
        reference = header.replace("TopModule", "SpecReference") + (
            "always @(posedge clk) if(reset) q<=0; else q<=d; endmodule")
        test = build_reference(reference, contract, prompt)
        for body, expected in (
                ("always @(posedge clk) if(reset) q<=0; else q<=d;", 0),
                ("reg [3:0] delayed; always @(posedge clk) begin "
                 "delayed<=d; if(reset) q<=0; else q<=delayed; end", 1)):
            rc, log = self.rtl.simulate(header + body + "\nendmodule",
                                        test.render(contract), 90)
            self.assertEqual(rc, expected, log)
            if expected:
                self.assertIn("AGENT_INPUT sample=0 ", log)
                self.assertNotIn("AGENT_INPUT sample=1 ", log)
