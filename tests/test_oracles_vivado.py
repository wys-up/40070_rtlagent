"""Optional tool tests for literal specification tables and the generic checker harness."""

import unittest

from agent.oracles import from_spec
from agent.spec_contract import extract_contract
from agent.tools import RtlToolchain
from agent.verification import SelfTest


class VivadoOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rtl = RtlToolchain()
        if not cls.rtl.simulation_available:
            raise unittest.SkipTest("Vivado simulation tools unavailable")

    def compare(self, prompt, header, correct, faulty):
        contract = extract_contract(prompt, header + ";", "TopModule")
        test = from_spec(prompt, contract)
        self.assertIsNotNone(test)
        tb = test.render(contract)
        for body, expected in ((correct, 0), (faulty, 1)):
            rc, log = self.rtl.simulate(header + ";\n" + body + "\nendmodule\n", tb, 90)
            self.assertEqual(rc, expected, log)

    def test_stimulus_local_declaration_after_controller_initialization(self):
        header = "module TopModule(input a,b,output q);"
        contract = extract_contract("q is a AND b", header, "TopModule")
        checker = SelfTest("", """
integer i;
for(i=0;i<4;i=i+1) begin
  a=i[0]; b=i[1]; #1;
  check(q,a&b,1'b1,"truth table with a block-local loop variable");
end
""", "All four input pairs after controller input initialization")
        for expression, expected in (("a&b", 0), ("a|b", 1)):
            with self.subTest(expression=expression):
                rc, log = self.rtl.simulate(
                    header + f" assign q={expression}; endmodule",
                    checker.render(contract), 90)
                self.assertEqual(rc, expected, log)

    def test_kmap(self):
        self.compare("""Karnaugh map
         ab
cd 00 01 11 10
00 | 0 | 1 | 0 | 1 |
01 | 1 | 0 | 1 | 0 |
11 | 0 | 1 | 0 | 1 |
10 | 1 | 0 | 1 | 0 |
""", "module TopModule(input a,b,c,d,output out)",
                     "assign out=a^b^c^d;", "assign out=(a^b)&(c^d);")

    def test_moore_timing(self):
        self.compare("""Reset state is B and reset is active-high synchronous.
B (out=1) --in=0--> A
B (out=1) --in=1--> B
A (out=0) --in=0--> B
A (out=0) --in=1--> A
""", "module TopModule(input clk,reset,in,output out)",
                     "reg state; always @(posedge clk) if(reset) state<=1; "
                     "else if(!in) state<=~state; assign out=state;",
                     "reg state,q; always @(posedge clk) begin if(reset) state<=1; "
                     "else if(!in) state<=~state; q<=state; end assign out=q;")

    def test_combinational_state_table(self):
        self.compare("""Implement the output logic and next state logic for an FSM.
The output Y0 is Y[0] of the next state signal.
Present state input y[2:0] | Next state Y[2:0] when x=0, Next state Y[2:0] when x=1 | Output z
000 | 000, 001 | 0
001 | 001, 100 | 0
010 | 010, 001 | 0
011 | 001, 010 | 1
100 | 011, 100 | 1
""", "module TopModule(input clk,x,input [2:0] y,output reg Y0,output z)",
                     "assign z=(y==3||y==4); always @* case(y) "
                     "0,2:Y0=x; 1,3,4:Y0=~x; default:Y0=0; endcase",
                     "assign z=0; always @* Y0=0;")

    def test_waveform_transparent_latch_and_negedge_register(self):
        self.compare("""time clock a p q
0ns 0 0 x x
5ns 1 0 0 x
10ns 1 1 1 x
15ns 1 0 0 x
20ns 1 1 1 x
25ns 0 0 1 1
30ns 0 1 1 1
35ns 1 0 0 1
40ns 0 1 0 0
""", "module TopModule(input clock,a,output reg p,q)",
                     "always @* if(clock) p=a; always @(negedge clock) q<=p;",
                     "always @(posedge clock) begin p<=a; q<=p; end")


if __name__ == "__main__":
    unittest.main()
