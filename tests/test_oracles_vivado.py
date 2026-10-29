"""Optional real-tool integration tests with hand-derived, public specifications."""

import unittest

from example.agent.oracles import from_spec
from example.agent.spec_contract import extract_contract
from example.agent.tools import RtlToolchain


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

    def test_population_count(self):
        self.compare('A "population count" circuit.',
                     "module TopModule(input [14:0] in, output reg [3:0] out)",
                     "integer i; always @* begin out=0; for(i=0;i<15;i=i+1) out=out+in[i]; end",
                     "always @* out=0;")

    def test_sign_extension(self):
        self.compare("Sign-extend the smaller number.",
                     "module TopModule(input [3:0] in, output [7:0] out)",
                     "assign out={{4{in[3]}},in};", "assign out={4'b0,in};")

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

    def test_sticky_capture(self):
        prompt = ("Capture when input changes from 1 in one clock cycle to 0 the next. "
                  "The output will remain 1 until reset (active high synchronous reset).")
        header = "module TopModule(input clk,reset,input [7:0] in,output reg [7:0] out)"
        self.compare(prompt, header,
                     "reg [7:0] prev; always @(posedge clk) begin prev<=in; "
                     "if(reset) out<=0; else out<=out|(prev&~in); end",
                     "reg [7:0] prev; always @(posedge clk) begin if(reset) "
                     "begin prev<=0; out<=0; end else begin prev<=in; "
                     "out<=out|(prev&~in); end end")

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
