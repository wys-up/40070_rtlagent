import json
import unittest

from agent.fsm import compile_graph, graph_interface, generate_graph, UnsupportedGraph
from agent.llm import LLMResult
from unittest.mock import Mock
from agent.spec_contract import extract_contract
from agent.tools import RtlToolchain
from agent.verification import SelfTest


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.prompt = "Moore FSM. Active-low asynchronous reset, negative edge clock."
        self.interface = "module Unit(input clock,rst_n,x,output q);"
        self.contract = extract_contract(self.prompt, self.interface, "Unit")
        self.graph = dict(reset_state="Odd", states=[
            dict(name="Even", outputs=dict(q=0), next={"0": "Even", "1": "Odd"}),
            dict(name="Odd", outputs=dict(q=1), next={"0": "Odd", "1": "Even"})])

    def test_compiler_uses_graph_reset_polarity_edges_and_outputs(self):
        code = compile_graph(json.dumps(self.graph), self.prompt, self.contract, "Unit")
        self.assertIn("always @(negedge clock or negedge rst_n)", code)
        self.assertIn("if(!rst_n) state <= 1'd1;", code)
        self.assertIn("1'b1: state <= 1'd0;", code)
        self.assertIn("q=1'b1", code)

    def test_incomplete_or_fabricated_transitions_are_rejected(self):
        for bad in ("Missing", None):
            graph = json.loads(json.dumps(self.graph))
            graph["states"][0]["next"]["0"] = bad
            with self.assertRaises(ValueError):
                compile_graph(json.dumps(graph), self.prompt, self.contract, "Unit")
        del self.graph["states"][0]["next"]["0"]
        with self.assertRaises(ValueError):
            compile_graph(json.dumps(self.graph), self.prompt, self.contract, "Unit")

    def test_reset_edge_cannot_change_clock_edge(self):
        prompt = "Moore FSM. Active-low asynchronous reset: negedge rst_n; posedge clock."
        contract = extract_contract(prompt, self.interface, "Unit")
        code = compile_graph(json.dumps(self.graph), prompt, contract, "Unit")
        self.assertIn("always @(posedge clock or negedge rst_n)", code)

    def test_explanatory_derivation_cannot_change_graph_compilation(self):
        annotated = dict(self.graph, reasoning="Each input toggles parity iff it is one.")
        self.assertEqual(
            compile_graph(json.dumps(annotated), self.prompt, self.contract, "Unit"),
            compile_graph(json.dumps(self.graph), self.prompt, self.contract, "Unit"))

    def test_one_fenced_graph_can_be_separated_from_explanation(self):
        raw = "Derivation follows.\n```json\n" + json.dumps(self.graph) + "\n```\nExplanation."
        self.assertEqual(
            compile_graph(raw, self.prompt, self.contract, "Unit"),
            compile_graph(json.dumps(self.graph), self.prompt, self.contract, "Unit"))
        self.assertEqual(compile_graph(raw + raw, self.prompt, self.contract, "Unit"),
                         compile_graph(raw, self.prompt, self.contract, "Unit"))
        alternate = json.loads(json.dumps(self.graph))
        alternate["states"][0]["outputs"]["q"] = 1
        with self.assertRaisesRegex(ValueError, "unique"):
            compile_graph(raw + "\n```json\n" + json.dumps(alternate) + "\n```",
                          self.prompt, self.contract, "Unit")

    def test_one_corrected_graph_can_follow_a_structurally_invalid_draft(self):
        invalid = json.loads(json.dumps(self.graph))
        invalid["states"][0]["next"]["0"] = "Missing"
        reply = ("```json\n" + json.dumps(invalid) + "\n```\nCorrected:\n"
                 "```json\n" + json.dumps(self.graph) + "\n```")
        self.assertEqual(compile_graph(reply, self.prompt, self.contract, "Unit"),
                         compile_graph(json.dumps(self.graph), self.prompt, self.contract, "Unit"))

    def test_unsupported_task_can_use_general_reference_fallback(self):
        with self.assertRaises(UnsupportedGraph):
            compile_graph('{"unsupported":true}', self.prompt, self.contract, "Unit")
        self.assertIsNone(graph_interface("Combinational gates.", self.contract))

    def test_request_contains_exact_pattern_keys_for_current_interface(self):
        llm = Mock()
        llm.chat.return_value = LLMResult(json.dumps(self.graph))
        recorded = Mock()
        generate_graph(llm, self.prompt, self.contract, "Unit", 30, 2000,
                       record_plan=recorded)
        self.assertEqual(llm.chat.call_count, 2)
        recorded.assert_called_once()
        request = llm.chat.call_args.args[0][1]["content"]
        self.assertIn('literal keys: ["0", "1"]', request)
        self.assertIn('"outputs": {"q": 0}', request)
        self.assertIn('"0": {"state": "TARGET_STATE_NAME", "outputs": {"q": 0}}', request)

    def test_graph_planner_receives_relevant_domain_rules(self):
        llm = Mock()
        llm.chat.return_value = LLMResult(json.dumps(self.graph))
        prompt = self.prompt + " Serial receiver with a start bit and stop bit."
        generate_graph(llm, prompt, self.contract, "Unit", 30, 2000)
        planning = llm.chat.call_args_list[0].args[0][0]["content"]
        self.assertIn("rtl-serial-receiver", planning)
        self.assertIn("has already consumed it", planning)

    def test_repair_receives_its_own_graph_and_first_counterexample(self):
        llm = Mock()
        llm.chat.return_value = LLMResult(json.dumps(self.graph))
        previous = json.dumps(self.graph)
        generate_graph(llm, self.prompt, self.contract, "Unit", 30, 2000,
                       feedback="AGENT_MISMATCH first sampled edge",
                       previous_graph=previous)
        messages = llm.chat.call_args.args[0]
        self.assertEqual(messages[-2], dict(role="assistant", content=previous))
        self.assertIn("AGENT_MISMATCH first", messages[-1]["content"])

    def test_state_width_and_patterns_are_derived_from_graph(self):
        graph = dict(reset_state="S0", states=[
            dict(name=f"S{i}", outputs=dict(q=i % 2),
                 next={"0": f"S{(i+1)%5}", "1": f"S{(i+2)%5}"})
            for i in range(5)])
        code = compile_graph(json.dumps(graph), self.prompt, self.contract, "Unit")
        self.assertIn("reg [2:0] state;", code)
        self.assertIn("3'd4: case", code)

    def test_repeat_count_controls_expansion_and_unreachable_states_fail(self):
        for count in (2, 5, 13):
            graph = dict(reset_state="Hold", states=[
                dict(name="Hold", repeat=count, outputs=dict(q=0),
                     next={"0": "Ready", "1": "Ready"}),
                dict(name="Ready", outputs=dict(q=1),
                     next={"0": "Hold", "1": "Ready"})])
            code = compile_graph(json.dumps(graph), self.prompt, self.contract, "Unit")
            width = count.bit_length()
            self.assertIn(f"reg [{width-1}:0] state;", code)
            self.assertIn(f"{width}'d{count}: begin q=1'b1;", code)
        self.graph["states"][0]["next"] = {"0": "Even", "1": "Even"}
        self.graph["states"][1]["next"] = {"0": "Odd", "1": "Odd"}
        with self.assertRaisesRegex(ValueError, "unreachable"):
            compile_graph(json.dumps(self.graph), self.prompt, self.contract, "Unit")

    def test_registered_emissions_preserve_output_history_in_product_states(self):
        graph = dict(reset_state="Observe", reset_outputs=dict(q=1), states=[
            dict(name="Observe", next={
                "0": dict(state="Observe", outputs=dict(q=0)),
                "1": dict(state="Observe", outputs=dict(q=1))})])
        code = compile_graph(json.dumps(graph), self.prompt, self.contract, "Unit")
        self.assertIn("reg [0:0] state;", code)
        self.assertIn("1'd0: begin q=1'b1;", code)
        self.assertIn("1'd1: begin q=1'b0;", code)
        graph["states"][0]["next"]["0"]["outputs"] = {}
        with self.assertRaisesRegex(ValueError, "AFTER this edge"):
            compile_graph(json.dumps(graph), self.prompt, self.contract, "Unit")


class GraphVivadoTests(unittest.TestCase):
    def test_registered_repeat_matches_independent_phase_counter(self):
        rtl = RtlToolchain()
        if not rtl.simulation_available:
            self.skipTest("Vivado simulation tools unavailable")
        prompt = "Moore FSM, active-high synchronous reset; positive edge clock."
        header = "module Unit(input clk,reset,x,output q);"
        contract = extract_contract(prompt, header, "Unit")
        for count in (3, 6):
            graph = dict(reset_state="Hold", reset_outputs=dict(q=0), states=[
                dict(name="Hold", repeat=dict(count=count, outputs=dict(q=0)), next={
                    "0": dict(state="Ready", outputs=dict(q=1)),
                    "1": dict(state="Ready", outputs=dict(q=0))}),
                dict(name="Ready", next={
                    "0": dict(state="Hold", outputs=dict(q=0)),
                    "1": dict(state="Hold", outputs=dict(q=0))})])
            code = compile_graph(json.dumps(graph), prompt, contract, "Unit")
            checker = SelfTest("integer edge_index; reg expected;", f"""
clk=0; reset=1; x=0; #1; clk=1; #1;
clk=0; reset=0; #1;
for(edge_index=1;edge_index<40;edge_index=edge_index+1) begin
  x=(edge_index % 3)==0; expected=((edge_index % {count+1}) == {count}) && !x;
  #1; clk=1; #1; check(q,expected,1,"independent emitted pulse");
  clk=0; #1; x=~x; #1;
  check(q,expected,1,"output must hold before next edge");
end
""", "Independent phase counter and registered output hold")
            rc, log = rtl.simulate(code, checker.render(contract), 90)
            self.assertEqual(rc, 0, log)

    def test_repeat_phase_consumes_exact_count_after_entry(self):
        rtl = RtlToolchain()
        if not rtl.simulation_available:
            self.skipTest("Vivado simulation tools unavailable")
        prompt = "Moore FSM, active-high synchronous reset; positive edge clock."
        header = "module Unit(input clk,reset,x,output q);"
        contract = extract_contract(prompt, header, "Unit")
        for count in (2, 5):
            graph = dict(reset_state="Hold", states=[
                dict(name="Hold", repeat=count, outputs=dict(q=0),
                     next={"0": "Ready", "1": "Ready"}),
                dict(name="Ready", outputs=dict(q=1),
                     next={"0": "Hold", "1": "Hold"})])
            code = compile_graph(json.dumps(graph), prompt, contract, "Unit")
            checker = SelfTest("integer edge_index;", f"""
clk=0; reset=1; x=0; #1; clk=1; #1;
clk=0; reset=0; #1;
for(edge_index=1;edge_index<30;edge_index=edge_index+1) begin
  x=~x; #1; clk=1; #1;
  check(q,(edge_index % {count+1}) == {count},1,"independent duration");
  clk=0; #1;
end
""", "Independent modular count across repeated phases")
            rc, log = rtl.simulate(code, checker.render(contract), 90)
            self.assertEqual(rc, 0, log)

    def test_compiled_graph_matches_independent_parity_recurrence(self):
        rtl = RtlToolchain()
        if not rtl.simulation_available:
            self.skipTest("Vivado simulation tools unavailable")
        prompt = "Moore FSM, active-high synchronous reset; positive edge clock."
        header = "module Unit(input clk,reset,x,output q);"
        contract = extract_contract(prompt, header, "Unit")
        graph = dict(reset_state="Clear", states=[
            dict(name="Clear", outputs=dict(q=0), next={"0": "Clear", "1": "Set"}),
            dict(name="Set", outputs=dict(q=1), next={"0": "Set", "1": "Clear"})])
        code = compile_graph(json.dumps(graph), prompt, contract, "Unit")
        checker = SelfTest("integer i,seed; reg expected;", """
clk=0; reset=1; expected=0; seed=27;
#1; clk=1; #1; check(q,0,1,"reset");
clk=0; reset=0; #1;
for(i=0;i<64;i=i+1) begin
  x=$random(seed); expected=expected^x; #1;
  clk=1; #1; check(q,expected,1,"independent parity recurrence");
  clk=0; #1;
end
""", "Parity recurrence independent from state-graph representation")
        rc, log = rtl.simulate(code, checker.render(contract), 90)
        self.assertEqual(rc, 0, log)
