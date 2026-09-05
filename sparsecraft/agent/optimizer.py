import json
import subprocess


CONFIG_FILE = "sparsecraft/config/design_config.json"


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def run_hardware(config):
    print("\n================================")
    print("CHIA Hardware Evaluation")
    print("================================")

    print("Configuration:")
    print(f"  Sparsity : {config['sparsity']}")
    print(f"  NUM_PE   : {config['num_pe']}")

    print("\nRunning Verilator...")

    command = [
        "./obj_dir/Vtb_sparse_mac"
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True
    )

    print(result.stdout)

    return result.stdout


def main():

    config = load_config()

    output = run_hardware(config)

    if "STATUS        = PASS" in output:
        print("CHIA AGENT: Hardware configuration is valid.")
    else:
        print("CHIA AGENT: Hardware configuration failed.")


if __name__ == "__main__":
    main()