# Understanding the SparseCraft / CHIA Setup — A First-Time Guide

This document explains, in plain language, how Docker images, the CHIA loop
framework, and Ray fit together in this project. Every claim below was
verified against the actual code and the actual running system on
2026-09-15 — file paths and commands are real, not illustrative.

---

## 1. Docker/Podman images — the basics

### What an image actually is

An **image** is a frozen snapshot of an entire small computer: its own Linux
filesystem, its own installed programs, its own configuration — all bundled
into one file. Think of it as a saved photograph of a hard drive at one
moment in time. It does nothing by itself; it is just a template sitting on
disk.

A **container** is that photograph switched on and running. The moment you
start a container, the OS boots (in a lightweight sense — it shares your
host machine's actual Linux kernel, so it is much cheaper than a full virtual
machine) and whatever program the image was told to run starts executing.

You can start the **same image** as a container many times, and each running
copy is completely independent of the others — changes in one do not appear
in another, and none of them change the original image.

### Where the recipe for building our custom image lives

This project needed one image that does not exist anywhere pre-built, so we
wrote the recipe for it ourselves. It lives in:

```
/home/chia-sparsecraft/sparsecraft/docker/
├── SparseCraftSynthDockerfile   the recipe itself
├── build.sh                     the script that runs the recipe
├── patch_hammer.py              fixes two bugs in a tool inside the image
├── nangate45_latch_map.v        a small file the recipe copies in
└── selftest.sh                  proves the finished image actually works
```

`SparseCraftSynthDockerfile` is a plain text file of instructions such as
"start from this other image," "run this shell command," "copy this file
in." `build.sh` runs `podman build -f docker/SparseCraftSynthDockerfile ...`,
which executes those instructions one by one and saves the result as a new
image.

### The images available on this machine

| Image | Size | Origin | Purpose |
|---|---|---|---|
| `ghcr.io/ucb-bar/chia-chisel-build` | 31.1 GB | **pulled** (downloaded ready-made from GitHub's image registry) | Turns Chisel/Scala code into an elaborated design and a compiled simulator |
| `ghcr.io/ucb-bar/chia-verilator-run` | 3.53 GB | pulled | Runs a simulator binary that was already compiled elsewhere |
| `ghcr.io/ucb-bar/chia-riscv-cross` | 4.21 GB | pulled | Compiles small C test programs for the RISC-V CPU |
| `ghcr.io/ucb-bar/chia-opencode` | 5.6 GB | pulled | An AI coding-agent CLI, for one particular way of driving the model |
| `localhost/sparsecraft-synth` | 43.4 GB | **built locally**, yesterday, from our own Dockerfile | Everything `chia-chisel-build` has, plus yosys, OpenROAD/OpenSTA, and standard-cell libraries, for measuring chip area and speed |

"Pulled" and "built" images are not different *kinds* of thing once
finished — both just sit on disk as ordinary images. The difference is only
where they came from: downloaded ready-made, versus assembled locally by
following a written recipe. `sparsecraft-synth` is 43.4 GB largely *because*
it contains the entire 31 GB `chia-chisel-build` image inside it — the
recipe starts with `FROM ghcr.io/ucb-bar/chia-chisel-build:latest` and adds
more on top.

### One-time snapshot, runs many times, has its own scratch space

An image is written once and never modifies itself. Every time you start a
container from it, that container gets its **own private writable space** —
a scratch area layered on top of the read-only image. Any file the container
creates or changes while running goes into that scratch space, not into the
original image.

That scratch space is where the important distinction lives:

- **Stopping** a container (pausing it) keeps that scratch space intact —
  restart it later and everything the container wrote is still there.
- **Removing** a container deletes that scratch space permanently. The next
  container started from the same image begins completely empty again, as
  if nothing had ever run.

Our cluster teardown command, `chia down`, does the second one — see
[§4](#4-the-files-that-implement-it-and-the-chia-updown-commands) below for
exactly what it runs and why that matters.

One extra fact worth knowing: this host only has **podman**, not Docker
itself, but CHIA's own configuration files hardcode the word `docker`
everywhere. The fix already in place is a tiny wrapper script,
`~/bin/docker`, that silently redirects every `docker ...` command to
`podman ...`. You will see `docker:` in `cluster.yaml` — that is still
podman underneath.

### Where images actually live on disk (and what only *looks* spread out)

It is natural to assume images end up scattered across the system, because
several different-looking paths come up in the same breath as "Docker." They
are not the same thing, and only one of them is actually image storage.

Checked directly with `podman info` on this host: every image's data lives in
exactly **one** location —

```
/home/rajatabha/.local/share/containers/storage
```

No second storage location is configured (`podman system df` confirms all
image layers, 49.5 GB worth, sit in that single place). What makes it *look*
spread out is that several unrelated things also happen to be filesystem
paths:

| Path | What it actually is | Is it image storage? |
|---|---|---|
| `~/.local/share/containers/storage` | The real image store — every image's layers, permanently | **Yes** |
| `/home/rajatabha/podman-tmp` (`TMPDIR`) | Scratch space used only *while building* an image; the finished result moves into the store above once the build ends | Only transiently |
| `sparsecraft/docker/` | The **recipe** (the Dockerfile) describing how to build an image — source code, not the artifact it produces | No |
| `/home/ray/eda`, `/home/ray/pdk`, `/home/ray/chipyard` | Paths **inside** a running container's own private filesystem — only visible via `podman exec` into that specific container | It's content *of* an image, not a host location |
| `chia/` and `chia-work/` | Two copies of CHIA's own Python source code — the tool that manages images, not an image | No |
| `/tmp/rajatabha_ray/...` | Ray's own runtime logs and sockets while it is running | No |

One real storage location for images; several unrelated things nearby that
happen to also be described by filesystem paths.

---

## 2. Why you will not see Chisel/RTL changes appear in your own folders

This surprises most people the first time, so it is worth stating plainly:
**editing a file inside a container does not create or change any file on
your host machine**, unless something was specifically set up to connect
the two.

We checked the configuration directly. In
`/home/chia-sparsecraft/sparsecraft/cluster.yaml`:

```yaml
file_mounts: {}
```

Empty. This means **no folder on your host is shared into any container.**
The full Chipyard checkout — including the one file the AI model is allowed
to edit, `SparseCraftParams.scala` — lives entirely on the `chipyard`
container's own private disk. It is a completely separate filesystem from
`/home/chia-sparsecraft` that you see in your terminal or IDE.

### How to actually look at the live file, if you want to

You can look directly inside the running container:

```bash
export PATH="$HOME/bin:$PATH"   # the docker -> podman shim

# The Scala file the model is allowed to edit, live, right now:
podman exec -it sparsecraft-chipyard-rajatabha-0 \
  cat /home/ray/chipyard/generators/gemmini/src/main/scala/gemmini/SparseCraftParams.scala

# Or get an interactive shell inside it and look around freely:
podman exec -it sparsecraft-chipyard-rajatabha-0 bash
```

(`sparsecraft-chipyard-rajatabha-0` is the container's name — you can see
every running container's name with `podman ps`.)

### How the project makes the change durable anyway

Because the container's disk can vanish at any time (see §1), the loop does
not rely on the file staying there. After every model edit, the code runs a
plain `git diff` **inside the container** to capture the change as text, and
immediately copies that text back out to your host disk. This happens in
`diff_nodes.py`:

- `collect_diff()` — runs `git diff` inside the container, returns the text
- `reset_and_apply_diff()` — wipes the container's tree back to a clean
  state and re-applies a previously saved diff

The saved result lands on your **actual host filesystem** at:
```
/home/chia-sparsecraft/runs/<run-name>/diff_001.json
/home/chia-sparsecraft/runs/<run-name>/diff_002.json
...
```

Open one of those and you will see the same information as the live Scala
file — which lines were added or removed — just captured as a text diff
instead of a live file. That JSON diff is the real, permanent record of what
the model changed; the file inside the container is disposable.

---

## 3. Loops, clusters, nodes, edges, logical workers — the vocabulary

These five words come from CHIA's own documentation
(`/home/chia-sparsecraft/chia/docs/getting-started/chia-basics.rst`) and map
directly onto specific files in this project.

| Term | Meaning | Where it lives here |
|---|---|---|
| **Loop** | A Python script describing a pipeline of tasks and the data/control flow between them | `sparsecraft/loop.py` |
| **Node** | One task in the pipeline — any Python function | Any function decorated `@ChiaFunction` — see table below |
| **Edge** | The flow from one node's output to the next node's input | The literal order of function calls written inside `loop.py`'s iteration loop |
| **Cluster** | The collection of physical machines the loop runs on | `sparsecraft/cluster.yaml` |
| **Logical worker** | A named "slot" on the cluster exposing some resource (CPU, a container, a credential) that nodes get scheduled onto | Each entry under `available_node_types:` in `cluster.yaml` |

**"Worker" and "logical worker" are the same thing.** CHIA's docs say
"logical" once, to make one point: a worker is not necessarily one dedicated
physical machine. It's a *named slot* Ray can schedule tasks onto, and that
slot can be backed by a container, or it can just run natively on your host —
`head_local` and `database` do the latter here, with no container at all.

### Nodes, and which file each one lives in

| Node (as named in our design) | Function | File |
|---|---|---|
| N13 — apply the design to the tree | `apply_design_state()` | `nodes.py` |
| N30/N31 — elaborate Chisel → simulator | `elaborate()` | `nodes.py` |
| N32 — cross-compile the test kernel | `build_kernel()` | `nodes.py` |
| N50 — run the simulator (T2a) | `simulate()` | `nodes.py` |
| N52 — physical synthesis (T3) | `synthesize()` | `synth_node.py` |
| N13 (persistence half) | `collect_diff()`, `reset_and_apply_diff()` | `diff_nodes.py` |
| N20 — legality check (T0) | `check()` | `t0_legality.py` |
| N22 — analytical prediction (T1) | `predict()` | `t1_model.py` |
| N60/N62 — Pareto admission and archive | `admit()`, `Archive` | `pareto.py` |
| N10 — the AI model itself | `.prompt()` on whatever `agent.make_llm()` returns | `agent.py` |

A **node** is nothing exotic — it is a plain Python function with one
decorator on it, `@ChiaFunction(resources={...})`. That decorator is what
turns "just a function" into "something Ray can send off to run on whichever
worker has the resource it asked for." Example, from `nodes.py`:

```python
@ChiaFunction(resources={R_CHIPYARD: BUILD_FRACTION})   # BUILD_FRACTION = 0.9
def elaborate(state_json, ...):
    ...
```

This one line says: *"running this function requires 0.9 units of whatever
worker advertises the `chipyard` resource."* Ray reads that requirement,
looks at the cluster, and picks a matching worker.

### `cluster.yaml` — what this one file actually does

`cluster.yaml` has two jobs:

1. **Declares every logical worker type** under `available_node_types:` —
   how many of it to run, what resource name(s) it advertises, and (if it
   should be a container) which image to use. There are six of them here:

   | Worker | Resource(s) it advertises | Container image | Runs |
   |---|---|---|---|
   | `head_local` | `head_local: 8`, `llm: 2`, plus per-provider credential slots | *(none — runs natively on your host)* | Bookkeeping, scoring, the AI model API calls |
   | `chipyard` | `chipyard: 2` | `chia-chisel-build` | Elaboration, kernel compile |
   | `verilator` | `verilator_run: 12` | `chia-verilator-run` | Running the simulator |
   | `hammer` | `hammer: 1` | `sparsecraft-synth` | Physical synthesis |
   | `database` | `database: 2` | *(native)* | Durable storage of results |
   | `riscv_build` | `riscv_build: 4` | `chia-riscv-cross` | Standalone cross-compiles |

2. **Tells `chia up`/`chia down` how to actually start and stop each one** —
   the `docker:` block under each worker names the image, container name,
   and startup options; `worker_env_commands:` / `head_start_ray_commands:`
   say what shell commands to run when bringing a worker online.

### Cluster vs. image — different layers, not the same thing

The table above is worth pausing on, because it directly answers a natural
question: is a cluster just another name for an image, and is
`sparsecraft-synth` somehow "chipyard merged into a bigger cluster"? No —
they are two different layers, and one cluster normally references *several*
different images at once, exactly as the table shows: five images, backing
five different workers, all inside this one cluster called `sparsecraft`.

An **image** is a filesystem template for *one* container. A **cluster** is
a *topology* — the list of every worker that should exist, what each
advertises, and which image (if any) backs it. `sparsecraft-synth` is the
image behind exactly *one* of the six workers (`hammer`); the `chipyard`
worker keeps its own separate image and its own separate running container,
right alongside it, in this same cluster.

There **is** a real relationship between `sparsecraft-synth` and
`chia-chisel-build`, but it is a *build-time* one, not a cluster-time one:
`docker/SparseCraftSynthDockerfile` literally starts with
`FROM ghcr.io/ucb-bar/chia-chisel-build:latest` and adds tools on top — the
way photocopying a page and drawing more on it makes a new page. That is
ancestry between two *images*. It says nothing about how many clusters
exist or how many workers one cluster can have — that is a separate axis.

**Multiple clusters are the normal CHIA pattern, not an exception.** Nearly
every example project under `chia/examples/` ships its own `cluster.yaml`
with its own `cluster_name`:

```
examples/hammer-pwr/cluster.yaml           cluster_name: hammer-pwr-...
examples/circt_issue_solver/cluster.yaml   cluster_name: circt_issue_solver
examples/gem5_align/cluster.yaml           cluster_name: gem5-align
examples/bypass_cache/cluster.yaml         cluster_name: bypass-cache-example
```

Each is an independent text file describing an independent topology of
workers and images — nothing merges them, nothing requires them to share
images. The one caveat specific to *this* setup: our cluster's head Ray
process runs directly on this one physical host and binds a fixed network
port, so in practice one cluster is brought up at a time on this machine,
rather than two simultaneously.

### Resource numbers, on one real example — two scales that share a name

The same word — `chipyard` — carries two unrelated numbers at two different
scales, and mixing them up is the easiest way to misread this system. Worth
walking through once, carefully, on the exact numbers this project uses.

**Scale 1 — the whole cluster: `chipyard: 2`.** This is the total supply,
for the entire cluster, for as long as it is up. `chipyard` is not a
resource Ray knows about natively — it's a name *this project* invented,
purely as a bookkeeping label meaning "one running Chisel-build container's
worth of elaboration capacity." The `2` is a deliberate ceiling, derived
from a RAM calculation stated directly in the file's own comment:

```yaml
# WORKER SIZING IS RAM-BOUND, NOT CORE-BOUND. This host has 64 cores but 30 GB
# of RAM. A Chisel elaboration peaks at 8-12 GB...
chipyard:
    resources: {"chipyard": 2}
```

`30 GB ÷ ~12 GB per elaboration ≈ 2` — calculated by hand, not by Ray, and
typed in.

**Scale 2 — one reservation, carved out of that supply.** Every time
`loop.py` starts an iteration, this line runs:

```python
pg = placement_group([{"CPU": 1, "chipyard": 1}], strategy="STRICT_PACK")
```

This takes **one whole ticket (1.0)** out of the cluster-wide supply of 2
and sets it aside, exclusively, for this one iteration:

```
Cluster-wide supply:  2.0  →  reserve 1.0 for this iteration  →  1.0 remains for anything else
```

That is the correct subtraction — `2 − 1 = 1`.

**Scale 3 — inside that one reservation: `BUILD_FRACTION = 0.9`.** Once the
1.0-ticket "box" is set aside, what happens *inside* it is a separate,
smaller accounting problem, completely disconnected from the cluster-wide
number:

```python
# Build nodes request 0.9 of a placement-group bundle that reserves 1.0, so the
# editor BashTool actor fits in the same bundle as the builder.
BUILD_FRACTION = 0.9
```

```
Box capacity:        1.0
elaborate task uses: 0.9
left inside the box: 0.1
```

`1.0 − 0.9 = 0.1` — entirely local to this one box; it never touches the
number 2. (A natural but incorrect way to read this is `2 − 0.9 = 1.1` — that
subtracts a Scale-3 number from a Scale-1 total, which is mixing the two
levels together.)

**What that leftover 0.1 is actually for.** The AI model's editor (a
`BashTool`, started as CHIA's `_ToolServerActor`) shares this same box.
Checked directly in CHIA's own source:

```python
@ray.remote(num_cpus=0)
class _ToolServerActor:
```

It is declared with `num_cpus=0` — it claims **zero** capacity from the box.
It only needs to be *pinned* to the same box (same container), which it gets
by naming this exact reservation as its scheduling target, not by consuming
any of the box's capacity. So the 0.1 left over isn't consumed by anything —
it is a safety margin, so the build task never claims the box down to the
exact last drop while something else also asks to share it.

**The full picture, both scales at once:**

```
CLUSTER-WIDE SUPPLY (cluster.yaml)
  Total: 2.0
  ──────────────────────────────────
  One iteration's reservation: −1.0   →  1.0 remains for anything else
  ──────────────────────────────────
        │
        └── THE ONE BOX just reserved (its own private 1.0, unrelated to the 1.0 left above)
              Total: 1.0
              ────────────────────────
              elaborate task:  0.9
              spare margin:    0.1   (the editor tool actually claims 0 of this)
```

Two different subtractions, at two different scales, that happen to both
involve the number 1 — which is exactly why they are easy to conflate on a
first read.

---

## 4. The files that implement it, and the `chia up`/`down` commands

### Role of each Python file in `sparsecraft/`

| File | Role |
|---|---|
| `loop.py` | **The driver.** Contains the actual `for it in range(...)` loop; calls every node in order each iteration |
| `nodes.py` | The four core build/run nodes (elaborate, build kernel, simulate, apply state) |
| `synth_node.py` | The physical-synthesis node (added yesterday) |
| `diff_nodes.py` | Captures and re-applies the model's edits as git diffs, since the container's own disk is not durable |
| `t0_legality.py` | Instant, free legality checks — catches an illegal design in microseconds instead of paying for a 20–40 minute failed build |
| `t1_model.py` | A closed-form (formula-based) prediction of area/energy/period, used to filter obviously-bad proposals before spending real compute |
| `pareto.py` | Decides whether a measured design is an improvement worth keeping, and archives it |
| `design_state.py` | The typed description of one design (array size, banking, tiling, ...), and how to turn it into Scala text |
| `agent.py` | Everything about the AI model: which backend, how to authenticate, what tools it is given |
| `constants.py` | Shared paths, resource names, and the `runtime_env()` function that ships these files to the workers |
| `metrics.py` | Parses the hardware counters the simulator prints out |
| `check_setup.py` | Diagnostic: what is installed where, probed from the live machine |
| `check_llm.py` | Diagnostic: is the configured AI backend actually reachable |
| `smoke_agent.py` | Diagnostic: does one real agentic turn actually work end-to-end |
| `run_synth.py` | Standalone: synthesize the baseline vs. the candidate and print area/Fmax |
| `run.sh` | One-command wrapper: preflight checks → cluster up → loop → cluster down |

### How §3's concepts show up inside `loop.py`, concretely

Every iteration of `loop.py`'s loop calls nodes in this fixed order (the
"edges"):

```
apply_design_state()  ->  elaborate()  ->  build_kernel()  ->  simulate()
      ->  [synthesize()  if --synth]  ->  pareto.admit()
```

Each `->` is one edge. Each function name is one node. The whole sequence,
run once per pass of the `for` loop, is one iteration of the loop.

### `chia up` and `chia down` — where they are defined, and what they actually do

The command-line tool `chia` is a small Python package. On this machine it
is installed in **editable mode**, pointing at:

```
/home/chia-sparsecraft/chia-work/chia/
```

**Important distinction:** there are two similarly-named folders on this
host.
- `/home/chia-sparsecraft/chia` — the original checkout, owned by another
  user, kept **read-only and untouched** per this project's constraints.
- `/home/chia-sparsecraft/chia-work` — a writable copy of it, and this is
  the one actually running whenever you type `chia ...` or any Python code
  does `import chia`. (Verified: `chia_env`'s editable install points here,
  and its `chia/cli` folder is currently identical to the read-only copy.)

The commands themselves:

- **`chia up cluster.yaml`** — defined in `chia-work/chia/cli/up.py`. Starts
  Ray on your host (the "head"), then for each worker in `cluster.yaml`,
  starts the container (pulling the image first if needed), runs its setup
  commands, and starts a Ray process inside it so the head can dispatch work
  to it.
- **`chia down cluster.yaml`** — defined in `chia-work/chia/cli/down.py`,
  which calls into `worker_provisioner.py`. For every container it runs, in
  order:
  ```
  docker stop <container-name>
  docker rm -f <container-name>
  ```
  That second command is the one to remember: it **deletes** the container,
  not merely pauses it. As explained in §1, that wipes the container's
  private scratch space — any half-finished build cache is gone, while
  anything already copied out to `runs/` on your host (per §2) survives
  untouched.

---

## 5. Ray — the dispatcher underneath everything

> A separate program called Ray (installed on your actual host machine, not
> inside any container) is the dispatcher. When `loop.py` needs an
> elaboration done, Ray sends that one task into the `chipyard` container,
> waits, and gets the result back. Your host machine itself (`head_local`)
> does no heavy lifting — it just does bookkeeping, scoring, and talks to
> the LLM API.

Ray ([ray.io](https://www.ray.io/)) is a general-purpose open-source system
for one specific job: taking a normal Python function call and running it
**somewhere else** — a different process, a different container, even a
different physical machine — instead of running it immediately where it was
called, then handing back a handle you can wait on for the result.

(Note: `/home/ray` as a folder name inside the container images is an
unrelated coincidence — see §4's file table above and the earlier
discussion in this conversation. Same word, two unconnected things.)

### What "scheduling" concretely means here

`cluster.yaml` has each worker declare what it has available:

```yaml
chipyard:   resources: {"chipyard": 2}
verilator:  resources: {"verilator_run": 12}
hammer:     resources: {"hammer": 1}
```

Ray keeps a live table of this across every running worker. Separately,
every node function declares what it *needs*:

```python
@ChiaFunction(resources={"chipyard": 0.9})     # nodes.py: elaborate()
@ChiaFunction(resources={"verilator_run": 1})  # nodes.py: simulate()
@ChiaFunction(resources={"hammer": 1})         # synth_node.py: synthesize()
```

When `loop.py` calls `elaborate(...)`, Ray checks its table, finds a worker
currently holding a free `chipyard` slot, reserves it, and sends the actual
work there. **If both `chipyard` slots are already busy, the new request
simply waits in a queue** until one becomes free — that queueing is the
entire meaning of "scheduling" in this system.

(The `0.9` above is not subtracted directly from the cluster-wide `2` — there
is an intermediate step, a per-iteration reservation that `loop.py` makes
before calling `elaborate` at all, and the `0.9` only matters *inside* that
reservation. It's a small enough detail that it deserves its own walkthrough
rather than a hand-wave here: see
[§3's "Resource numbers, on one real example"](#resource-numbers-on-one-real-example--two-scales-that-share-a-name)
for the full, correct arithmetic on this exact pair of numbers.)

This is also exactly how the AI model itself gets dispatched. `constants.py`
defines:

```python
R_LLM = "llm"
LLM_OPTS = {"resources": {R_LLM: 1.0}}
```

and `head_local` in `cluster.yaml` advertises `llm: 2`. So a call to the
model goes through the identical mechanism as a call to elaborate Chisel or
run synthesis — Ray does not treat "call the AI" as special; it is just
another task requesting a named resource, matched against whichever worker
currently has it free.

This is also why several *different* design candidates could, in principle,
have their Chisel elaborated at the same time on this one physical
machine — Ray treats each container as an independent pool of slots and
packs pending work into whichever slot is free, the same way a restaurant
host seats new parties at whichever table just opened up.
