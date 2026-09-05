import json
import subprocess
import re


CONFIG_FILE = "sparsecraft/config/design_config.json"


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def run_hardware(config):

    num_pe = config["num_pe"]

    print("\n================================")
    print("CHIA Hardware Evaluation")
    print("================================")

    print("Configuration:")
    print(f"  Sparsity : {config['sparsity']}")
    print(f"  NUM_PE   : {num_pe}")

    print("\nCompiling hardware...")

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

        print("Hardware compilation failed.")
        print(compile_result.stderr)

        return None


    print("Compilation successful.")

    print("\nRunning hardware...")

    run_result = subprocess.run(
        ["./obj_dir/Vtb_sparse_array"],
        capture_output=True,
        text=True
    )

    output = run_result.stdout

    print(output)


    # Extract result
    result_match = re.search(
        r"Result\s*=\s*(-?\d+)",
        output
    )

    # Extract expected value
    expected_match = re.search(
        r"Expected\s*=\s*(-?\d+)",
        output
    )


    if result_match:
        result = int(result_match.group(1))
    else:
        result = None


    if expected_match:
        expected = int(expected_match.group(1))
    else:
        expected = None


    # Check correctness
    if result is not None and expected is not None:

        if result == expected:

            print("CHIA AGENT: PASS")
            print(f"Result = {result}")

        else:

            print("CHIA AGENT: FAIL")
            print(f"Result  = {result}")
            print(f"Expected = {expected}")


    return result


def main():

    config = load_config()

    run_hardware(config)


if __name__ == "__main__":
    main()