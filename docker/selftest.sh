#!/usr/bin/env bash
# Build-time proof that the synthesis tier actually works end to end.
#
# Drives the real chain -- hammer-vlsi -> hammer.synthesis.yosys -> yosys ->
# abc -> NanGate45 Liberty -- on a small systolic PE array and asserts that a
# gate-level netlist and a non-zero cell area come back out. If this passes,
# the only thing standing between the image and a Gemmini synthesis run is the
# size of the design.
# NOT -u. chipyard's env.sh sources
# .conda-env/etc/conda/activate.d/activate-riscv-tools.sh, which reads $RISCV
# before setting it; under `set -u` that aborts the whole script. Every
# expansion below carries its own default instead.
set -eo pipefail

source /home/ray/chipyard/env.sh
EDA="${SPARSECRAFT_EDA_PREFIX:-/home/ray/eda}"
export PATH="$PATH:$EDA/yosys/bin:$EDA/openroad/bin:$EDA/klayout/bin"

PDK="${SPARSECRAFT_PDK_ROOT:-/home/ray/pdk}"
WORK=/tmp/sparsecraft-selftest
rm -rf "$WORK"; mkdir -p "$WORK"

echo "=== tool inventory ==="
printf '  %-14s %s\n' hammer-vlsi "$(command -v hammer-vlsi)"
printf '  %-14s %s\n' yosys       "$(command -v yosys)"
printf '  %-14s %s\n' openroad    "$(command -v openroad)"
printf '  %-14s %s\n' klayout     "$(command -v klayout || echo '(absent)')"
yosys -V
openroad -version
cat "${SPARSECRAFT_EDA_PREFIX:-/home/ray/eda}/klayout.status" 2>/dev/null || true

# --- the design under test: a 4x4 output-stationary MAC array -------------
# Deliberately Gemmini-shaped (a mesh of accumulating PEs) so that a failure
# here is a failure on the kind of logic the loop will actually synthesize,
# not on a toy inverter.
cat > "$WORK/pe_array.v" <<'VERILOG'
module pe #(parameter W = 8, parameter A = 32) (
  input              clk,
  input              rst,
  input              en,
  input  signed [W-1:0] a_in,
  input  signed [W-1:0] b_in,
  output signed [W-1:0] a_out,
  output signed [W-1:0] b_out,
  output signed [A-1:0] acc_out
);
  reg signed [W-1:0] a_r, b_r;
  reg signed [A-1:0] acc_r;
  always @(posedge clk) begin
    if (rst) begin
      a_r <= {W{1'b0}}; b_r <= {W{1'b0}}; acc_r <= {A{1'b0}};
    end else if (en) begin
      a_r <= a_in; b_r <= b_in; acc_r <= acc_r + (a_in * b_in);
    end
  end
  assign a_out = a_r;
  assign b_out = b_r;
  assign acc_out = acc_r;
endmodule

module pe_array #(parameter N = 4, parameter W = 8, parameter A = 32) (
  input                    clk,
  input                    rst,
  input                    en,
  input  signed [N*W-1:0]  a_west,
  input  signed [N*W-1:0]  b_north,
  output signed [N*A-1:0]  acc
);
  wire signed [W-1:0] ah [0:N][0:N];
  wire signed [W-1:0] bv [0:N][0:N];
  wire signed [A-1:0] ac [0:N-1][0:N-1];
  genvar i, j;
  generate
    for (i = 0; i < N; i = i + 1) begin : row_in
      assign ah[i][0] = a_west[i*W +: W];
    end
    for (j = 0; j < N; j = j + 1) begin : col_in
      assign bv[0][j] = b_north[j*W +: W];
    end
    for (i = 0; i < N; i = i + 1) begin : rows
      for (j = 0; j < N; j = j + 1) begin : cols
        pe #(.W(W), .A(A)) u_pe (
          .clk(clk), .rst(rst), .en(en),
          .a_in(ah[i][j]), .b_in(bv[i][j]),
          .a_out(ah[i][j+1]), .b_out(bv[i+1][j]),
          .acc_out(ac[i][j]));
      end
    end
    for (i = 0; i < N; i = i + 1) begin : row_out
      assign acc[i*A +: A] = ac[i][N-1];
    end
  endgenerate
endmodule
VERILOG

# --- the hammer configs ---------------------------------------------------
# install_dir points at the nangate45 DIRECTORY, not its parent. Hammer treats
# the install id as a path PREFIX to strip: a library declared as
# "nangate45/lib/x.lib" resolves to "<install_dir>/lib/x.lib", so pointing at
# the parent yields /home/ray/pdk/lib/x.lib and every library goes missing.
cat > "$WORK/tech.yml" <<YAML
vlsi.core.technology: "hammer.technology.nangate45"
technology.nangate45.install_dir: "${PDK}/nangate45"
YAML

cat > "$WORK/tools.yml" <<YAML
vlsi.core.synthesis_tool: "hammer.synthesis.yosys"
vlsi.core.par_tool: "hammer.par.openroad"
synthesis.yosys.yosys_bin: "$(command -v yosys)"
par.openroad.openroad_bin: "$(command -v openroad)"
par.openroad.klayout_bin: "$(command -v klayout || echo klayout)"
synthesis.yosys.latch_map_file: "${PDK}/nangate45_latch_map.v"
vlsi.core.max_threads: 4
YAML

cat > "$WORK/design.yml" <<YAML
vlsi.inputs.clocks:
  - {name: "clk", period: "2ns", uncertainty: "0.1ns"}
synthesis.inputs:
  top_module: "pe_array"
  input_files: ["${WORK}/pe_array.v"]
YAML

echo "=== hammer-vlsi syn ==="
set +e
hammer-vlsi \
  -p "$WORK/tech.yml" -p "$WORK/tools.yml" -p "$WORK/design.yml" \
  --obj_dir "$WORK/build" syn > "$WORK/syn.log" 2>&1
RC=$?
set -e
tail -40 "$WORK/syn.log"
if [ $RC -ne 0 ]; then
  echo "SELFTEST FAILED: hammer-vlsi syn exited $RC" >&2
  exit $RC
fi

# --- assert the flow produced real collateral -----------------------------
NETLIST=$(find "$WORK/build" -name '*.mapped.v' -print -quit)
[ -n "$NETLIST" ] || { echo "SELFTEST FAILED: no mapped netlist" >&2; exit 1; }
grep -q 'NangateOpenCellLibrary\|DFF_X1\|AND2_X1\|INV_X1' "$NETLIST" \
  || { echo "SELFTEST FAILED: netlist has no NanGate45 cells" >&2; exit 1; }

echo "=== netlist: $NETLIST ==="
grep -c ';' "$NETLIST" | sed 's/^/  statements: /'

# The assertion that matters, and the one whose absence let a broken image
# through once already: every register must be mapped to a real library cell.
# With hammer's stock `dfflibmap -map-only`, yosys leaves $_SDFFE_* internal
# cells in the netlist, `stat` reports "sequential elements: 0.000000", and the
# area silently excludes every flip-flop. On a systolic array that is most of
# the design, and the run still exits 0.
if grep -qE '^\s*\$_(S?DFFE?|DLATCH)' "$NETLIST"; then
  echo "SELFTEST FAILED: unmapped yosys internal FF cells left in the netlist:" >&2
  grep -oE '\$_[A-Z0-9_]+_' "$NETLIST" | sort | uniq -c | sort -rn | head >&2
  exit 1
fi
STAT=$(find "$WORK/build" -name '*.synth_stat.txt' -print -quit)
if [ -n "$STAT" ]; then
  SEQ=$(grep -oE 'sequential elements: [0-9.]+' "$STAT" | awk '{print $3}' | tail -1)
  echo "  sequential area: ${SEQ:-unknown}"
  case "${SEQ:-0}" in
    0|0.0|0.000000|"")
      echo "SELFTEST FAILED: sequential area is zero -- the flip-flops in a" >&2
      echo "  register-heavy MAC array were not mapped, so the reported area" >&2
      echo "  is combinational only and not usable." >&2
      exit 1 ;;
  esac
  grep -E "Chip area|sequential elements|Number of cells" "$STAT" | tail -5
fi
find "$WORK/build" -path '*reports*' -type f | head -20
for r in $(find "$WORK/build" -path '*reports*' -name '*.rpt' -o -path '*reports*' -name '*.json' | head -5); do
  echo "--- $r ---"; head -30 "$r"
done

# --- the timing half of the tier ---------------------------------------
# yosys only TARGETS a period (abc -D); it reports no slack. Fmax comes from
# OpenSTA on the mapped netlist, which is a separate tool and a separate way to
# fail -- the openroad binary, for instance, refuses read_verilog with
# "no technology has been read" unless LEFs are loaded first. Standalone
# OpenSTA needs only Liberty and Verilog, so that is what the node uses, and
# this proves it here.
LIB="${PDK}/nangate45/lib/NangateOpenCellLibrary_typical.lib"
cat > "$WORK/sta.tcl" <<TCL
read_liberty $LIB
read_verilog $NETLIST
link_design pe_array
create_clock -name core_clk -period 2.0 [get_ports -quiet {clk}]
set_propagated_clock [all_clocks]
report_checks -path_delay max -format summary -digits 4
report_worst_slack -max -digits 4
exit
TCL
if ! sta -no_init -exit "$WORK/sta.tcl" > "$WORK/sta.log" 2>&1; then
  echo "SELFTEST FAILED: OpenSTA exited non-zero" >&2
  tail -25 "$WORK/sta.log" >&2
  exit 1
fi
grep -E "Startpoint|worst slack" "$WORK/sta.log" || true
if ! grep -qE "^worst slack +-?[0-9]" "$WORK/sta.log"; then
  echo "SELFTEST FAILED: OpenSTA reported no worst slack, so there is no Fmax" >&2
  tail -25 "$WORK/sta.log" >&2
  exit 1
fi

echo "SELFTEST OK: hammer + yosys + NanGate45 -> mapped netlist; OpenSTA -> slack"
rm -rf "$WORK"
