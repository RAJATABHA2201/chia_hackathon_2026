import subprocess
import re


# ============================================================
# CHIA Hardware Design Space
# ============================================================

CANDIDATE_PE = [1, 2, 4, 8]

# Optimization weights
LATENCY_WEIGHT = 0.7
AREA_WEIGHT = 0.3


# ============================================================
# Architectural Area Estimate
# ============================================================

def estimate_area(num_pe):

    # Early architectural area proxy.
    #
    # This is NOT physical synthesis area.
    #
    # Assume:
    #   Base control/interconnect = 10 units
    #   Each PE = 20 units

    BASE_AREA = 10
    AREA_PER_PE = 20

    return BASE_AREA + (num_pe * AREA_PER_PE)


# ============================================================
# Hardware Evaluation
# ============================================================

def evaluate_hardware(num_pe):

    print("\n----------------------------------------")
    print(f"Evaluating NUM_PE = {num_pe}")
    print("----------------------------------------")


    # --------------------------------------------------------
    # Compile RTL
    # --------------------------------------------------------

    compile_command = [
        "verilator",
        "--binary",
        "--timing",

        "-DNUM_PE=" + str(num_pe),

        "sparsecraft/hardware/sparse_array.sv",
        "sparsecraft/testbench/tb_sparse_array.sv",

        "--top-module",
        "tb_sparse_array"
    ]


    compile_result = subprocess.run(
        compile_command,
        capture_output=True,
        text=True
    )


    if compile_result.returncode != 0:

        print("Compilation FAILED")
        print(compile_result.stderr)

        return None


    # --------------------------------------------------------
    # Run simulation
    # --------------------------------------------------------

    run_result = subprocess.run(
        ["./obj_dir/Vtb_sparse_array"],
        capture_output=True,
        text=True
    )


    output = run_result.stdout

    print(output)


    # --------------------------------------------------------
    # Extract result
    # --------------------------------------------------------

    result_match = re.search(
        r"Result\s*=\s*(-?\d+)",
        output
    )

    expected_match = re.search(
        r"Expected\s*=\s*(-?\d+)",
        output
    )


    if not result_match or not expected_match:

        print("Could not extract simulation result.")

        return None


    result = int(result_match.group(1))
    expected = int(expected_match.group(1))


    # --------------------------------------------------------
    # Correctness check
    # --------------------------------------------------------

    if result != expected:

        print("STATUS = FAIL")

        return None


    # --------------------------------------------------------
    # Extract cycles
    # --------------------------------------------------------

    cycle_match = re.search(
        r"Cycles\s*=\s*(\d+)",
        output
    )


    if not cycle_match:

        print("Could not extract cycle count.")

        return None


    cycles = int(cycle_match.group(1))


    # --------------------------------------------------------
    # Area estimate
    # --------------------------------------------------------

    area = estimate_area(num_pe)


    print(f"Correctness = PASS")
    print(f"Cycles      = {cycles}")
    print(f"Area proxy  = {area}")


    return {
        "num_pe": num_pe,
        "cycles": cycles,
        "area": area,
        "correct": True
    }


# ============================================================
# Cost Function
# ============================================================

def calculate_cost(results):

    if not results:
        return results


    min_cycles = min(
        r["cycles"] for r in results
    )

    max_cycles = max(
        r["cycles"] for r in results
    )

    min_area = min(
        r["area"] for r in results
    )

    max_area = max(
        r["area"] for r in results
    )


    for r in results:

        # Normalize latency

        if max_cycles == min_cycles:

            latency_norm = 0

        else:

            latency_norm = (
                (r["cycles"] - min_cycles)
                /
                (max_cycles - min_cycles)
            )


        # Normalize area

        if max_area == min_area:

            area_norm = 0

        else:

            area_norm = (
                (r["area"] - min_area)
                /
                (max_area - min_area)
            )


        # Combined objective

        r["cost"] = (
            LATENCY_WEIGHT * latency_norm
            +
            AREA_WEIGHT * area_norm
        )


    return results


# ============================================================
# Main
# ============================================================

def main():

    print("")
    print("========================================")
    print("       CHIA HARDWARE EXPLORER")
    print("========================================")

    print("")
    print(f"Candidates : {CANDIDATE_PE}")

    print(
        f"Objective  : "
        f"{LATENCY_WEIGHT} × latency + "
        f"{AREA_WEIGHT} × area"
    )


    results = []


    # --------------------------------------------------------
    # Explore candidates
    # --------------------------------------------------------

    for num_pe in CANDIDATE_PE:

        result = evaluate_hardware(num_pe)

        if result is not None:

            results.append(result)


    # --------------------------------------------------------
    # Calculate cost
    # --------------------------------------------------------

    results = calculate_cost(results)


    # --------------------------------------------------------
    # Print table
    # --------------------------------------------------------

    print("")
    print("========================================")
    print("        DESIGN SPACE RESULTS")
    print("========================================")

    print(
        f"{'NUM_PE':<10}"
        f"{'CYCLES':<10}"
        f"{'AREA':<10}"
        f"{'COST':<10}"
    )

    print("----------------------------------------")


    for r in results:

        print(
            f"{r['num_pe']:<10}"
            f"{r['cycles']:<10}"
            f"{r['area']:<10}"
            f"{r['cost']:.4f}"
        )


    # --------------------------------------------------------
    # Select best design
    # --------------------------------------------------------

    if results:

        best = min(
            results,
            key=lambda r: r["cost"]
        )


        print("")
        print("========================================")
        print("          SELECTED DESIGN")
        print("========================================")

        print(f"NUM_PE = {best['num_pe']}")
        print(f"Cycles = {best['cycles']}")
        print(f"Area   = {best['area']}")
        print(f"Cost   = {best['cost']:.4f}")

        print("========================================")


if __name__ == "__main__":
    main()