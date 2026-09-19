"""N10/N73 -- the agentic nodes, and the sealed side-channel tools.

Two things follow the CHIA examples exactly:

**Two LLM instances, different system prompts.** `riscv_extensions` builds an
implement-LLM from `system.md` and a separate debug-LLM from `debug.md`
(`single_loop.py:649-655, 742, 761`). Same tools, different framing. That split
is also the review's Sec 2.1 recommendation (CHIA Fig. 7's Implement/Debug LLM
split) and it removes the diagnose-and-propose conflict of interest.

**The editor is a plain BashTool pinned to the build container.** Not a custom
tool -- `examples/riscv_extensions/tools.py:1-8` is explicit that the write path
is bash, pinned to the build placement group "because it must edit source inside
the build container". Custom ChiaTools are sealed, read-only side channels on
`head_local` so their files land on the driver's disk.

The model is configured but NOT authenticated here. Credentials arrive through
the cluster YAML (bind mounts + env), never through code.
"""

from __future__ import annotations

import json
import os
import string
from pathlib import Path
from typing import List

from chia.base.tools.BashTool import BashTool
from chia.base.tools.ChiaTool import ChiaTool

from constants import CHIPYARD_PATH, HEAD_LOCAL

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# --------------------------------------------------------------------------
# The model.
#
# One dict is the whole backend story. Every entry below reaches the SAME
# CHIA contract -- ``LLMCallBase.prompt(user_message, tools) -> QueryResult`` --
# so the loop never learns which provider it is talking to, and switching is an
# environment variable rather than a code change.
#
# Two kinds, and the difference is only how they authenticate:
#
#   openai_compat  an API KEY and a base_url. Nothing else: no CLI, no gcloud,
#                  no cloud project. This is the path to use if you just want
#                  the loop to run. Google, OpenAI, Anthropic, Groq and
#                  OpenRouter all speak this wire format.
#   vertex         Google Application Default Credentials plus a GCP project.
#                  More setup, but it bills to a GCP account.
#
# ``env`` lists the environment variables searched IN ORDER for the credential.
# ``needs`` names the python package that must be importable on whichever
# worker serves the call -- which is why cluster.yaml advertises the `llm`
# resource on the NATIVE head pool, not inside a container: chia_env has these
# packages and the EDA images do not.
# --------------------------------------------------------------------------
PROVIDERS: dict[str, dict] = {
    # Google AI Studio key against Gemini's OpenAI-compatible endpoint. The
    # lowest-setup option there is: create a key, export it, run the loop.
    "gemini": {
        "kind": "openai_compat",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "env": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "default_model": "gemini-2.5-pro",
        "needs": "openai",
        "get_key": "https://aistudio.google.com/apikey",
    },
    # Gemini on Vertex, via google-genai. Uses a GCP project and ADC rather
    # than a key, so it bills to GCP credits.
    "vertex": {
        "kind": "vertex",
        "base_url": None,
        "env": ("GOOGLE_CLOUD_PROJECT",),
        # Pro, not Flash. Measured 2026-09-19: the WHOLE planned study (3 arms x
        # 15 iterations + repair turns + 3x for false starts) costs ~$8 at Pro
        # rates and ~$2 at Flash rates, against $250 of credits -- so price is
        # not a input to this choice. Wall clock is: a proposal that compiles
        # but is semantically wrong costs a full ~30 min iteration, and N12
        # cannot catch that class. 5 of 12 iterations in runs/agent-1 were
        # DUPLICATE on the EASY config-only task.
        #
        # NOTE the Pro line in this project stops at 3.1-preview while Flash
        # reaches 3.8, so tier and generation point in opposite directions.
        # 3.1-pro-preview is the newest Pro and the default; gemini-2.5-pro is
        # the documented fallback and is the only model verified end-to-end
        # through THIS harness including MCP tool calling (smoke_agent.py).
        # Validate tool calling with smoke_agent.py before the first long run.
        # gemini-2.5-pro, NOT 3.1-pro-preview: it is the only model verified end
        # to end through THIS harness including MCP tool calling (smoke_agent.py).
        # A preview model on top of CHIA's own experimental VertexGeminiLLM is two
        # unknowns stacked, for a run that goes unattended for hours.
        "default_model": "gemini-2.5-pro",
        "needs": "google.genai",
        "get_key": "gcloud auth application-default login, "
                   "or set GOOGLE_APPLICATION_CREDENTIALS to a service-account key",
    },
    "openai": {
        "kind": "openai_compat",
        "base_url": None,                      # the SDK's own default endpoint
        "env": ("OPENAI_API_KEY",),
        "default_model": "gpt-4o",
        "needs": "openai",
        "get_key": "https://platform.openai.com/api-keys",
    },
    "anthropic": {
        "kind": "openai_compat",
        "base_url": "https://api.anthropic.com/v1/",
        "env": ("ANTHROPIC_API_KEY",),
        "default_model": "claude-sonnet-5",
        "needs": "openai",
        "get_key": "https://console.anthropic.com/settings/keys",
    },
    "openrouter": {
        "kind": "openai_compat",
        "base_url": "https://openrouter.ai/api/v1",
        "env": ("OPENROUTER_API_KEY",),
        "default_model": "google/gemini-2.5-pro",
        "needs": "openai",
        "get_key": "https://openrouter.ai/keys",
    },
    "groq": {
        "kind": "openai_compat",
        "base_url": "https://api.groq.com/openai/v1",
        "env": ("GROQ_API_KEY",),
        "default_model": "llama-3.3-70b-versatile",
        "needs": "openai",
        "get_key": "https://console.groq.com/keys",
    },
    # Anything else that speaks the OpenAI wire format: a self-hosted vLLM or
    # TGI, Ollama, a corporate gateway, an on-prem model. base_url comes from
    # the environment because there is no sensible default for "somewhere else".
    "custom": {
        "kind": "openai_compat",
        "base_url": None,      # filled from SPARSECRAFT_LLM_BASE_URL below
        "env": ("SPARSECRAFT_LLM_API_KEY", "OPENAI_API_KEY"),
        "default_model": "local-model",
        "needs": "openai",
        "get_key": "set SPARSECRAFT_LLM_BASE_URL to the endpoint, and "
                   "SPARSECRAFT_LLM_API_KEY if it wants one",
    },
    # The CLI backend. Kept because it needs no key at all if the container is
    # already signed in, but it runs in the opencode image, so the `llm`
    # resource has to move there for it to be reachable.
    "opencode": {
        "kind": "opencode",
        "base_url": None,
        "env": (),
        "default_model": "gemini-2.5-pro",
        "needs": None,
        "get_key": "sign in inside the chia-opencode container",
    },
}

DEFAULT_BACKEND = "gemini"

LLM_BACKEND = os.environ.get("SPARSECRAFT_LLM_BACKEND", DEFAULT_BACKEND)
LLM_MODEL = os.environ.get("SPARSECRAFT_LLM_MODEL", "")


def provider(backend: str | None = None) -> tuple[str, dict]:
    """Resolve a backend name to its spec, with a usable error if unknown."""
    name = (backend or LLM_BACKEND).strip().lower()
    spec = PROVIDERS.get(name)
    if spec is None:
        raise ValueError(
            f"unknown SPARSECRAFT_LLM_BACKEND={name!r}. "
            f"Known backends: {', '.join(sorted(PROVIDERS))}")
    # SPARSECRAFT_LLM_BASE_URL overrides any provider's endpoint, which is what
    # makes "custom" work and also lets a regional or proxied endpoint be used
    # for a named provider without editing this table.
    override = os.environ.get("SPARSECRAFT_LLM_BASE_URL")
    if override:
        spec = {**spec, "base_url": override}
    return name, spec


def credential(backend: str | None = None) -> tuple[str | None, str | None]:
    """Return ``(env_var_that_was_set, its value)``, or ``(None, None)``.

    Searched in the order the provider declares, so GEMINI_API_KEY wins over
    GOOGLE_API_KEY when both are set.
    """
    _, spec = provider(backend)
    for var in spec["env"]:
        value = os.environ.get(var)
        if value:
            return var, value
    return None, None


def model_name(backend: str | None = None) -> str:
    name, spec = provider(backend)
    return LLM_MODEL or spec["default_model"]


def describe(backend: str | None = None) -> dict:
    """Everything the preflight needs to say whether this backend will work."""
    import importlib.util
    name, spec = provider(backend)
    var, value = credential(name)
    needs = spec["needs"]
    try:
        has_pkg = needs is None or importlib.util.find_spec(needs) is not None
    except (ImportError, ValueError):
        has_pkg = False
    return {
        "backend": name,
        "kind": spec["kind"],
        "model": model_name(name),
        "base_url": spec["base_url"],
        "credential_var": var,
        "credential_present": bool(value),
        "credential_hint": spec["get_key"],
        "searched": list(spec["env"]),
        "package": needs,
        "package_present": has_pkg,
        # A local endpoint often wants no key at all, so "custom" is ready as
        # soon as it has been pointed somewhere.
        "ready": bool(value
                      or spec["kind"] == "opencode"
                      or (name == "custom" and spec["base_url"])) and has_pkg,
    }


def load_prompt(prompt_file: str, **values: object) -> str:
    """Render a ``prompts/*.md`` template against ``values``.

    ``task.md`` uses shell-style ``${NAME}`` placeholders, so this is
    ``string.Template`` rather than ``str.format`` -- the templates contain
    Scala and JSON braces that ``format`` would try to interpret. Same choice
    as ``examples/memcpy/llm.py``, which does the substitution by hand for the
    same reason.

    Placeholders and keyword arguments must match EXACTLY, in both directions,
    and a mismatch raises. ``safe_substitute`` would be the obvious call here
    and is deliberately not used: it leaves an unmatched ``${FOO}`` sitting in
    the rendered text, so a renamed placeholder or a typo'd keyword ships the
    literal string to the model and shows up as a baffling proposal several
    hours into a run rather than as an error at iteration 1.
    """
    path = PROMPTS_DIR / prompt_file
    try:
        raw = path.read_text()
    except FileNotFoundError:
        available = ", ".join(sorted(p.name for p in PROMPTS_DIR.glob("*.md"))) or "none"
        raise FileNotFoundError(
            f"no prompt {prompt_file!r} in {PROMPTS_DIR}. Available: {available}"
        ) from None

    template = string.Template(raw)
    # get_identifiers() is 3.11+; derive the set from the pattern so this keeps
    # working on the 3.10.19 interpreter CHIA pins.
    wanted = {
        m.group("named") or m.group("braced")
        for m in template.pattern.finditer(raw)
        if m.group("named") or m.group("braced")
    }
    given = set(values)
    if wanted != given:
        missing = ", ".join(sorted(wanted - given)) or "none"
        extra = ", ".join(sorted(given - wanted)) or "none"
        raise KeyError(
            f"{prompt_file}: placeholder/argument mismatch. "
            f"In the template but not passed: {missing}. "
            f"Passed but not in the template: {extra}.")

    return template.substitute({k: str(v) for k, v in values.items()})


def make_llm(system_prompt_file: str, backend: str | None = None,
             model: str | None = None, log_dir: str | None = None):
    """Construct the backend named by SPARSECRAFT_LLM_BACKEND.

    The credential is read HERE, on the driver, and travels inside the
    constructed object to whichever worker serves ``prompt``. That is
    deliberate: it means the loop works without provisioning secrets into any
    container. It also means the key rides Ray's object store, which is fine
    while every worker is on this one machine and would need revisiting if the
    cluster ever spanned hosts.
    """
    name, spec = provider(backend)
    system_message = (PROMPTS_DIR / system_prompt_file).read_text()
    chosen_model = model or model_name(name)
    var, key = credential(name)

    common = dict(model=chosen_model, system_message=system_message,
                  timeout_seconds=900, retries=3)
    if log_dir:
        common["log_dir"] = log_dir

    if spec["kind"] == "openai_compat":
        if not key and name == "custom":
            # A local server that wants no auth still needs the SDK to send
            # something, and it ignores whatever it gets.
            key = "no-key-required"
        if not key:
            raise RuntimeError(
                f"backend {name!r} needs an API key. Set one of "
                f"{' or '.join(spec['env'])} and re-run. Get a key: "
                f"{spec['get_key']}")
        from chia.models.openai_compat import OpenAICompatLLM
        return OpenAICompatLLM(base_url=spec["base_url"], api_key=key,
                               logging_name=f"sparsecraft_{name}", **common)

    if spec["kind"] == "vertex":
        from chia.models.vertex import VertexGeminiLLM
        return VertexGeminiLLM(logging_name="sparsecraft_vertex", **common)

    if spec["kind"] == "opencode":
        from chia.models.opencode import OpenCodeLLM
        return OpenCodeLLM(**common)

    raise ValueError(f"backend {name!r} has unknown kind {spec['kind']!r}")


# --------------------------------------------------------------------------
# Sealed side-channel tools. Pinned to head_local so their files land on the
# driver's disk and get archived, per examples/riscv_extensions/tools.py.
# --------------------------------------------------------------------------
class StatusTool(ChiaTool):
    """Read-only view of the CURRENT measured state.

    Deliberately read-only and recomputed by the harness from actual results.
    The model cannot write its own status: `single_loop._write_status` rebuilds
    it from the test outcomes each iteration precisely so self-reporting is
    impossible.
    """

    def __init__(self, name: str, status_path: str, task_options=None):
        self.status_path = status_path
        super().__init__(name, task_options=task_options)
        self.mcp.add_tool(self.read_status, name=f"{name}_read_status")
        super().__post_init__()

    def read_status(self) -> str:
        """Return the harness-computed status of the current design."""
        try:
            return Path(self.status_path).read_text()
        except FileNotFoundError:
            return "(no status yet)"


class HistoryTool(ChiaTool):
    """Agentic PULL of search history, rather than pushing it into the prompt.

    The review's Sec 2.2(h): at 60 iterations a pushed metric history grows
    without bound and dilutes attention. Push only the current state, last
    verdict and a <=5-point front summary; let the model pull the rest.
    """

    def __init__(self, name: str, history_path: str, task_options=None):
        self.history_path = history_path
        super().__init__(name, task_options=task_options)
        self.mcp.add_tool(self.query_history, name=f"{name}_query_history")
        self.mcp.add_tool(self.get_pareto_front, name=f"{name}_get_pareto_front")
        super().__post_init__()

    def _load(self) -> dict:
        try:
            return json.loads(Path(self.history_path).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {"iterations": [], "front": []}

    def query_history(self, top_k: int = 10) -> str:
        """Return the most recent evaluated designs with their measured metrics."""
        return json.dumps(self._load().get("iterations", [])[-top_k:], indent=2)

    def get_pareto_front(self) -> str:
        """Return the current Pareto front over (time, energy, area)."""
        return json.dumps(self._load().get("front", []), indent=2)


def make_editor(pg_opts: dict) -> BashTool:
    """The write path: a bash shell inside the build container, at chipyard root.

    Pinned into the same placement-group bundle as every build/diff node, so the
    tree the model edits is the tree that gets elaborated.
    """
    return BashTool(name="sparsecraft_edit", work_dir=CHIPYARD_PATH,
                    timeout_seconds=600, task_options=pg_opts)


def make_sealed_tools(status_path: str, history_path: str) -> List[ChiaTool]:
    return [
        StatusTool("sparsecraft_status", status_path, task_options=HEAD_LOCAL),
        HistoryTool("sparsecraft_history", history_path, task_options=HEAD_LOCAL),
    ]
