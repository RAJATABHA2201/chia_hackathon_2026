### S-2 — Resource sizing (capacity, banking, queues, DMA)

Typed config fields on the design state. Changing any of them changes `hw_hash`, so
the iteration pays a full elaboration and synthesis — roughly 3x the cost of a
software-schedule move. Spend that only when you can name the counter you expect to
move.

| lever | when it is the right move |
|---|---|
| `sp_capacity_kb`, `acc_capacity_kb` | SRAM dominates the energy breakdown, or the working set no longer needs the capacity |
| `sp_banks`, `acc_banks` | **bank-conflict stalls are the measured bottleneck** — see the warning below |
| `max_in_flight_mem_reqs`, `dma_maxbytes`, `dma_buswidth` | `dma_wait_fraction` is high; the memory path is starved |
| `ld/st/ex_queue_length`, `reservation_station_entries_*` | the array is idle and issue-queue pressure exceeds scratchpad pressure |
| `tlb_size` | `DMA_TLB_MISS_CYCLE` is a meaningful share of cycles |

**Shrinking is a move.** Capacity is usually treated as something to grow, and on
this workload the opposite has paid twice: `sp_capacity_kb` 256 → 128 → 64 cut area
22% with **cycles bit-identical**, because `x_resident` had already made the surplus
redundant. The energy model scales per-access SRAM cost as
`sqrt(max(SRAM, 32KB) / 256KB)`, where SRAM is scratchpad + accumulator, so a
smaller one is cheaper per access as well as smaller, down to a 32 KB floor. (Before
2026-09-25 the floor was 256 KB itself, so these shrinks earned area but no energy.) When a software change makes a hardware resource unnecessary, removing it
is a real cross-layer win and it is the kind this loop exists to find.

**BANK COUNT HAS NEVER WORKED HERE. Read this before proposing it.**
`sp_banks` has been proposed repeatedly and has never once changed cycles:

- an earlier run spent **five of its first six moves** on banking, cycles
  bit-identical every time
- `sp_banks 4 → 16` at fixed capacity: cycles bit-identical, **+4.3% area**,
  rejected

The reason is a measurement trap. The scratchpad/accumulator wait counters and
`RESERVATION_STATION_FULL_CYCLES` are **free-running accumulations across banks and
ports** — they routinely exceed the cycle count (measured at 2.4x–2.9x of cycles on
the baseline). They are not per-cycle fractions, so a large absolute value does
**not** mean the design is conflict-bound. Comparing two of them against each other
is sound; comparing either against cycles is not. If you want to propose more banks,
first name the bounded fraction that says conflicts are the problem.

**Queue depths need the array to be idle.** `exe_active_fraction` below ~0.40 with
`rs_full` exceeding scratchpad wait is the signature that issue-queue depth is
binding. Above that the queues are not the constraint and deepening them buys area
and nothing else.

**What "no clear bottleneck" means.** When `exe_active` sits between roughly 0.40
and 0.80 and `dma_wait` is near zero, the array is neither starved nor saturated and
no single resource is the constraint. That is not an invitation to tune resources at
random — it is the signal to **change the mechanism instead** (T-A gating scope, T-B
granularity, N:M) rather than resize what already fits.
