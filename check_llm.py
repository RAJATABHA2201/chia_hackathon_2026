#!/usr/bin/env python3
"""Prove the model is reachable before the loop spends an hour finding out.

Elaboration is 20-40 minutes. Discovering a typo'd API key after that is the
most expensive way to learn it. So this makes a real, minimal call to the
configured provider and prints what came back.

    python check_llm.py                 # call the configured backend
    python check_llm.py --list          # every backend and whether it is ready
    python check_llm.py --backend gemini --model gemini-2.5-flash

Exit status is 0 only if the model actually answered.

Deliberately does NOT go through Ray. A failure here should mean "the
credential or the model name is wrong", never "the cluster is misconfigured" --
keeping those two diagnoses separate is the whole value of a preflight. The
CHIA backend class is still constructed, so a constructor-level mistake is
caught too.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent  # noqa: E402

PING = ("Reply with exactly the word READY and nothing else.")


def call_openai_compat(spec: dict, model: str, key: str, timeout: int) -> tuple[bool, str]:
    try:
        from openai import OpenAI
    except ImportError:
        return False, ("the `openai` package is not installed in this "
                       "environment. `pip install openai` inside chia_env.")
    try:
        client = OpenAI(api_key=key, base_url=spec["base_url"], timeout=timeout)
        r = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": PING}],
            max_tokens=16,
        )
        text = (r.choices[0].message.content or "").strip()
        return True, text or "(empty response)"
    except Exception as e:                                   # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def call_vertex(model: str, timeout: int) -> tuple[bool, str]:
    try:
        from google import genai
    except ImportError:
        return False, "the `google-genai` package is not installed."
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if not project:
        return False, ("GOOGLE_CLOUD_PROJECT is not set. Vertex needs a project "
                       "id plus Application Default Credentials.")
    try:
        client = genai.Client(vertexai=True, project=project, location=location)
        r = client.models.generate_content(model=model, contents=PING)
        return True, (r.text or "").strip() or "(empty response)"
    except Exception as e:                                   # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def show_all() -> int:
    print(f"{'backend':<12} {'ready':<7} {'model':<26} credential")
    print("-" * 78)
    any_ready = False
    for name in sorted(agent.PROVIDERS):
        d = agent.describe(name)
        mark = "yes" if d["ready"] else "no"
        any_ready |= d["ready"]
        cred = (d["credential_var"] if d["credential_present"]
                else "set " + " or ".join(d["searched"]) if d["searched"]
                else "(none needed)")
        print(f"{name:<12} {mark:<7} {d['model']:<26} {cred}")
        if not d["package_present"]:
            print(f"{'':<12} {'':<7} needs the `{d['package']}` package, "
                  f"which is not importable here")
    print()
    if not any_ready:
        print("Nothing is ready. The shortest path is a Google AI Studio key:")
        print(f"  {agent.PROVIDERS['gemini']['get_key']}")
        print("  export GEMINI_API_KEY=...")
    return 0 if any_ready else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--list", action="store_true",
                    help="show every backend and whether it is ready, no calls")
    args = ap.parse_args()

    if args.list:
        return show_all()

    try:
        name, spec = agent.provider(args.backend)
    except ValueError as e:
        print(f"FAIL  {e}")
        return 2
    model = args.model or agent.model_name(name)
    var, key = agent.credential(name)

    print(f"backend      {name}  ({spec['kind']})")
    print(f"model        {model}")
    print(f"endpoint     {spec['base_url'] or '(provider default)'}")
    print(f"credential   {var or 'NOT SET'}"
          + (f"  [{len(key)} chars, ...{key[-4:]}]" if key else ""))

    # Readiness is agent.describe's call, not re-derived here. A local endpoint
    # that wants no key is ready without one, and duplicating that rule in two
    # places is how the two drift apart.
    info = agent.describe(name)
    if not info["ready"]:
        print()
        if not info["package_present"]:
            print(f"FAIL  the `{info['package']}` package is not importable "
                  f"in this environment.")
        else:
            print(f"FAIL  no credential. Set one of: {', '.join(spec['env'])}")
        print(f"      {spec['get_key']}")
        return 1
    print()

    # Constructing the CHIA object first: catches a bad model name or a
    # backend/package mismatch before any network call is attempted.
    try:
        agent.make_llm("propose.md", backend=name, model=model)
        print("chia backend constructs OK")
    except Exception as e:                                   # noqa: BLE001
        print(f"FAIL  the CHIA backend would not construct: {type(e).__name__}: {e}")
        return 1

    print(f"calling {model} ...")
    if spec["kind"] == "vertex":
        ok, text = call_vertex(model, args.timeout)
    elif spec["kind"] == "openai_compat":
        # Same placeholder agent.make_llm uses: the SDK requires a non-empty
        # key, a keyless local server ignores whatever arrives.
        ok, text = call_openai_compat(spec, model, key or "no-key-required",
                                      args.timeout)
    else:
        print("SKIP  the opencode backend runs a CLI inside its container; "
              "there is nothing to call from here.")
        return 0

    if ok:
        print(f"model replied: {text!r}")
        print()
        print(f"OK  {name}/{model} is reachable. The loop can run:")
        print(f"      ./run.sh --iters 5")
        return 0

    print(f"FAIL  {text}")
    print()
    print(f"      credential searched: {', '.join(spec['env']) or '(none)'}")
    print(f"      get a key: {spec['get_key']}")
    print(f"      other backends: python check_llm.py --list")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
