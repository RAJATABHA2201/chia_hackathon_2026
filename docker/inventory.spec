# SparseCraft canonical tool inventory -- THE single source of truth.
#
# Every image built by docker/build.sh probes exactly this list and records the
# result in /opt/sparsecraft/manifest.json. A name that is absent in an image is
# recorded as EXPLICITLY absent, never omitted -- docker/sparsecraft-tools.sh
# enforces that, and docker/sparsecraft-tools-selftest.sh asserts it. That is
# what lets a consumer tell "this image does not have yosys" apart from "this
# manifest predates yosys being a concept". The /home/ray/eda/klayout.status
# marker written by SparseCraftSynthDockerfile is the precedent.
#
# Fields, '|' separated. Whitespace around each field is stripped, so the
# columns below are cosmetic. Blank lines and #-comments are ignored.
#
#   kind          bin | dir | file
#   name          Logical name. For kind=bin this is also the wrapper name in
#                 /opt/sparsecraft/bin/. Must be unique, and must stay unique
#                 after mangling [^A-Za-z0-9] -> _ and uppercasing (the
#                 generator enforces both).
#   required_in   Comma list of roles that MUST have it, or '-' for never.
#                 A REQUIRED entry that probes absent FAILS the image build.
#                 Roles: chisel synth riscv verirun llm
#   source        Provenance -- where it comes from, or why it cannot exist.
#                 Copied verbatim into the manifest, including into the
#                 "reason" string of an absent entry, so the manifest explains
#                 its own gaps.
#   candidates    ':' separated ABSOLUTE paths, FIRST HIT WINS. Order matters:
#                 chipyard's own copy is listed first so the chisel and synth
#                 images record the toolchain their env.sh actually puts on
#                 PATH, rather than a second, independently-solved copy.
#   vargs         Argv for a version probe, or '-' for none. Best effort: a
#                 probe that fails or times out never fails the build, it is
#                 recorded as failed. '-' is used for tools whose probe writes
#                 into $HOME (sbt -> ~/.sbt ~/.ivy2, hammer-vlsi -> ~/.cache)
#                 or costs seconds for no information.
#   vgrep         ERE selecting the interesting line of the probe output, or
#                 '-' to take the first non-blank line. firtool needs one
#                 because it prints LLVM's banner first.
#
# ---------------------------------------------------------------------------
# Deliberately NOT in this inventory, and why:
#
#   python python3 pip ray conda   /home/ray/anaconda3/bin/python is CHIA's
#                                  3.10.19 / Ray 2.54.0 -- the interpreter Ray
#                                  refuses to connect a worker without -- while
#                                  chipyard's env.sh prepends a DIFFERENT one.
#                                  There is no single correct answer to "what
#                                  is python", so the farm must not pretend
#                                  there is. patch_hammer.py is already invoked
#                                  by absolute path for exactly this reason.
#   gcc g++ cc ld as ar make java  In chisel/synth the correct gcc is
#                                  chipyard's conda one; in riscv/verirun it is
#                                  /usr/bin/gcc from build-essential. A farm
#                                  entry named `gcc` would mean a different
#                                  thing in different images -- precisely the
#                                  bug class this directory exists to kill.
#                                  Only the unambiguous prefixed cross names
#                                  (riscv64-unknown-elf-*) are listed.
#   hammer-shell                   An internal of hammer-vlsi, satisfied by the
#                                  wrapper's PATH prepend. Keeping it out keeps
#                                  the farm a contract surface rather than a
#                                  mirror of someone's bin/.
#   git bash sh coreutils          Never.
# ---------------------------------------------------------------------------

# kind | name                        | required_in                | source                                                                       | candidates                                                                                                                                    | vargs        | vgrep

# --- Chisel elaboration / RTL generation (N30, N31) ------------------------
bin    | sbt                         | chisel,synth               | chipyard .conda-env (conda, chipyard pin)                                    | /home/ray/chipyard/.conda-env/bin/sbt                                                                                                         | -            | -
bin    | firtool                     | chisel,synth               | CIRCT, chipyard .conda-env/riscv-tools                                       | /home/ray/chipyard/.conda-env/riscv-tools/bin/firtool:/home/ray/chipyard/.conda-env/bin/firtool                                                | --version    | firtool-[0-9]
bin    | verilator                   | chisel,synth               | chipyard .conda-env (conda). NOT in chia-verilator-run: that image runs an already-linked simulator, it does not build one. | /home/ray/chipyard/.conda-env/bin/verilator                                       | --version    | ^Verilator

# --- RISC-V cross toolchain (N32) ------------------------------------------
bin    | riscv64-unknown-elf-gcc     | chisel,synth,riscv         | ucb-bar::riscv-tools 1.0.6                                                   | /home/ray/chipyard/.conda-env/riscv-tools/bin/riscv64-unknown-elf-gcc:/home/ray/conda/envs/riscv-tools/riscv-tools/bin/riscv64-unknown-elf-gcc | -dumpversion | -
bin    | riscv64-unknown-elf-objdump | chisel,synth,riscv         | ucb-bar::riscv-tools 1.0.6 (binutils)                                        | /home/ray/chipyard/.conda-env/riscv-tools/bin/riscv64-unknown-elf-objdump:/home/ray/conda/envs/riscv-tools/riscv-tools/bin/riscv64-unknown-elf-objdump | --version | objdump
bin    | riscv64-unknown-elf-objcopy | chisel,synth,riscv         | ucb-bar::riscv-tools 1.0.6 (binutils)                                        | /home/ray/chipyard/.conda-env/riscv-tools/bin/riscv64-unknown-elf-objcopy:/home/ray/conda/envs/riscv-tools/riscv-tools/bin/riscv64-unknown-elf-objcopy | --version | objcopy
# spike is required only where chipyard's riscv-tools is: VERIFIED absent from
# chia-riscv-cross (a whole-filesystem find turns up nothing), despite
# RiscvCrossDockerfile:55-58 claiming the riscv-tools meta-package ships it.
bin    | spike                       | chisel,synth               | ucb-bar::riscv-tools 1.0.6 (riscv-isa-sim); NOT in chia-riscv-cross          | /home/ray/chipyard/.conda-env/riscv-tools/bin/spike                                                                                           | -            | -
bin    | spike-dasm                  | chisel,synth,verirun       | riscv-tools; COPYed verbatim into chia-verilator-run at /usr/local/bin       | /home/ray/chipyard/.conda-env/riscv-tools/bin/spike-dasm:/usr/local/bin/spike-dasm                                                             | -            | -

# --- Synthesis / STA, the T3 tier (N52) ------------------------------------
bin    | hammer-vlsi                 | chisel,synth               | hammer 1.2.0 console script in chipyard .conda-env (see patch_hammer.py)     | /home/ray/chipyard/.conda-env/bin/hammer-vlsi                                                                                                  | -            | -
bin    | yosys                       | synth                      | litex-hub conda, SparseCraftSynthDockerfile step 1                           | /home/ray/eda/yosys/bin/yosys                                                                                                                  | -V           | ^Yosys
bin    | yosys-abc                   | synth                      | same conda package as yosys                                                  | /home/ray/eda/yosys/bin/yosys-abc                                                                                                              | -            | -
bin    | openroad                    | synth                      | litex-hub conda, SparseCraftSynthDockerfile step 1                           | /home/ray/eda/openroad/bin/openroad                                                                                                            | -version     | -
bin    | sta                         | synth                      | OpenSTA, shipped INSIDE the openroad conda package (no standalone pkg)       | /home/ray/eda/openroad/bin/sta                                                                                                                 | -version     | -
# required_in '-': expected absent EVERYWHERE. litex-hub's klayout pins
# qt>=5.9.7,<5.10, which conda-forge no longer carries. GDS streamout and DRC
# are unavailable; synthesis and STA are unaffected.
bin    | klayout                     | -                          | litex-hub conda -- UNINSTALLABLE: pins qt>=5.9.7,<5.10, gone from conda-forge | /home/ray/eda/klayout/bin/klayout                                                                                                             | -v           | -

# --- Agentic proposer, only when SPARSECRAFT_LLM_BACKEND=opencode ----------
bin    | opencode                    | llm                        | npm opencode-ai, chia-opencode image only                                    | /usr/bin/opencode:/usr/local/bin/opencode                                                                                                      | --version    | -

# --- Directories and collateral --------------------------------------------
# Half the duplicated definitions this inventory replaces were directories, not
# binaries (SPARSECRAFT_EDA_PREFIX, SPARSECRAFT_PDK_ROOT), so they are first
# class here and emit as "paths" / SPARSECRAFT_PATH_* rather than "tools".
dir    | chipyard                    | chisel,synth               | ChipyardDockerfile build-setup.sh -s 4 -s 9                                  | /home/ray/chipyard                                                                                                                            | -            | -
file   | chipyard_env                | chisel,synth               | chipyard build-setup.sh; sourced by cluster.yaml worker_env_commands         | /home/ray/chipyard/env.sh                                                                                                                      | -            | -
dir    | riscv_sysroot               | chisel,synth,riscv         | $RISCV -- libgloss-htif install prefix                                       | /home/ray/chipyard/.conda-env/riscv-tools:/home/ray/conda/envs/riscv-tools/riscv-tools                                                          | -            | -
dir    | eda_prefix                  | synth                      | SparseCraftSynthDockerfile (was SPARSECRAFT_EDA_PREFIX)                      | /home/ray/eda                                                                                                                                  | -            | -
dir    | pdk_root                    | synth                      | SparseCraftSynthDockerfile (was SPARSECRAFT_PDK_ROOT)                        | /home/ray/pdk                                                                                                                                  | -            | -
dir    | nangate45                   | synth                      | OpenROAD-flow-scripts sparse checkout, flow/platforms/nangate45              | /home/ray/pdk/nangate45                                                                                                                        | -            | -
file   | nangate45_lib               | synth                      | OpenROAD-flow-scripts; hammer technology.nangate45 liberty                   | /home/ray/pdk/nangate45/lib/NangateOpenCellLibrary_typical.lib                                                                                  | -            | -
file   | nangate45_latch_map         | synth                      | docker/nangate45_latch_map.v, vendored here (hammer 1.2.0 ships none)        | /home/ray/pdk/nangate45_latch_map.v                                                                                                            | -            | -
# required_in '-': only exists when the synth image was built WITH_SKY130=1.
dir    | sky130A                     | -                          | open_pdks.sky130a conda, only when WITH_SKY130=1                             | /home/ray/pdk/sky130A                                                                                                                          | -            | -

# --- chia-verilator-run runtime libraries ----------------------------------
# libriscv.so IS the golden model (cluster.yaml:69-71). Recording its sha256
# here is what finally makes VerilatorRunDockerfile's "identical files"
# invariant checkable -- see docker/compare-manifests.sh.
file   | libriscv                    | verirun                    | chia-verilator-run /usr/local/lib; the T2a golden model                      | /usr/local/lib/libriscv.so                                                                                                                     | -            | -
file   | libdramsim                  | verirun                    | chia-verilator-run /usr/local/lib                                            | /usr/local/lib/libdramsim.so                                                                                                                   | -            | -
file   | libsoftfloat                | verirun                    | chia-verilator-run /usr/local/lib                                            | /usr/local/lib/libsoftfloat.so                                                                                                                 | -            | -
