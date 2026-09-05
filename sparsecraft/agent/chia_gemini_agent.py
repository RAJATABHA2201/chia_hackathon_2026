import subprocess
import json
import urllib.request
import urllib.error


PROJECT_ID = "chia-hackathon-2026"
LOCATION = "us-central1"

# Use a current Gemini model available through Vertex AI.
MODEL = "gemini-2.5-flash"


def get_access_token():
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=True
    )

    return result.stdout.strip()


def ask_gemini(prompt):

    token = get_access_token()

    url = (
        f"https://{LOCATION}-aiplatform.googleapis.com/v1/"
        f"projects/{PROJECT_ID}/locations/{LOCATION}/"
        f"publishers/google/models/{MODEL}:generateContent"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ]
    }

    data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:

        with urllib.request.urlopen(request) as response:

            result = json.loads(
                response.read().decode("utf-8")
            )

        return result["candidates"][0]["content"]["parts"][0]["text"]

    except urllib.error.HTTPError as e:

        print("Gemini API ERROR")
        print(e.read().decode())

        return None


def main():

    prompt = """
You are the CHIA hardware optimization agent.

We are designing a sparse accelerator using:

- 2:4 structured sparsity
- 8 sparse groups
- NUM_PE controls the number of processing elements
- More PEs can reduce execution cycles but increase hardware area

The available configurations are:

NUM_PE = 1, 2, 4, 8

The hardware simulator has measured:

NUM_PE = 1:
Cycles = 9
Area = 30

NUM_PE = 2:
Cycles = 5
Area = 50

These measurements are real simulation results.

Choose the next NUM_PE configuration that should be evaluated.

Return:

1. NUM_PE to evaluate next
2. Short reason
3. Expected tradeoff between latency and area

Do not invent simulation results.
"""

    print("")
    print("========================================")
    print("          CHIA GEMINI AGENT")
    print("========================================")
    print("")

    response = ask_gemini(prompt)

    if response:

        print("Gemini decision:")
        print("----------------------------------------")
        print(response)
        print("----------------------------------------")

    else:

        print("Gemini request failed.")


if __name__ == "__main__":
    main()