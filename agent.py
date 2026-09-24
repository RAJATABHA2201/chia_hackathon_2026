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
    # Claude Code as the agent. Structurally DIFFERENT from every other entry
    # here and the difference is the whole point: the others hand CHIA a chat
    # completion and let CHIA drive the tool loop; this one hands the turn to
    # an agent that drives its own loop -- plans, calls the MCP tools, reads
    # what came back, tries again -- and returns only when it is finished. One
    # `prompt()` is one complete agentic session, not one message.
    #
    # Authenticates itself: the CLI reads the OAuth login in
    # ~/.claude/.credentials.json, or ANTHROPIC_API_KEY if one is exported
    # (a key, if present, WINS over the OAuth login). So unlike every other
    # backend, nothing is read by this process and nothing travels in the
    # constructed object -- see the note in make_llm.
    #
    # `needs` is None because there is no python package to import: the
    # requirement is the `claude` BINARY on the PATH of whichever worker
    # serves the call. That is ~/bin/claude, and `llm` is advertised on the
    # native head pool, so the shim on this host's PATH is the one that runs.
    "claude": {
        "kind": "claude_cli",
        "base_url": None,
        "env": ("ANTHROPIC_API_KEY",),
        "default_model": "claude-opus-5",
        "needs": None,
        # A CLI session is an agentic session, not a single completion: it can
        # spend twenty minutes reading the tree through the BashTool before it
        # writes anything. 900s (the default the other backends use) is a
        # timeout that fires MID-TASK and then burns two more retries doing the
        # same thing again, so this one gets its own, longer budget.
        "timeout": 2400,
        "get_key": "run `claude` once interactively to log in, or export "
                   "ANTHROPIC_API_KEY",
    },
    # The same model through the Anthropic SDK instead of the CLI: CHIA drives
    # the tool loop, as with every other backend. Here as the fallback for when
    # the CLI is the thing that is broken -- it isolates "the model is
    # unreachable" from "the CLI is misconfigured". CHIA warns on construction
    # that its api backend is unit-tested but not production-exercised, so it
    # is not the default.
    "claude_api": {
        "kind": "claude_api",
        "base_url": None,
        "env": ("ANTHROPIC_API_KEY",),
        "default_model": "claude-opus-5",
        "needs": "anthropic",
        "timeout": 1800,
        "get_key": "https://console.anthropic.com/settings/keys",
    },
    # Claude through Anthropic's OpenAI-compatibility shim. Third path to the
    # same model, and the least good one -- the shim is a translation layer
    # that does not carry thinking blocks -- but it reuses the openai_compat
    # code path that every other provider here is already proven on.
    "anthropic": {
        "kind": "openai_compat",
        "base_url": "https://api.anthropic.com/v1/",
        "env": ("ANTHROPIC_API_KEY",),
        "default_model": "claude-opus-5",
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

# Claude Code, because it is the only backend here that is an AGENT rather than
# a completion endpoint, and the loop's task -- read a Chisel tree, change it,
# see what the compile gate says, change it again -- is agentic. Switch with
# --backend or SPARSECRAFT_LLM_BACKEND; `gemini` is what this defaulted to
# before and still works unchanged.
DEFAULT_BACKEND = "claude"

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


# --------------------------------------------------------------------------
# Claude Code specifics.
#
# The CLI arrives with a whole environment of its own -- built-in Read/Write/
# Bash tools, the user's MCP servers, ~/.claude/settings.json, CLAUDE.md,
# skills. None of that is wanted here and one part of it is actively
# dangerous, so the flags below take it all away.
#
# `--tools ""` is the one that matters. Without it the agent gets Claude
# Code's OWN Bash and Edit tools, which run on the HEAD node with
# --dangerously-skip-permissions -- i.e. on the same filesystem as loop.py,
# metrics.py and t1_model.py. Those are in IMMUTABLE_FILES precisely because a
# metric the agent can edit is a metric it can fake, and an agent that can
# rewrite the scorer is not measuring anything. `--tools ""` removes every
# built-in tool, leaving exactly the MCP tools CHIA passes in: the BashTool
# pinned to the chipyard container, plus the sealed read-only status/history
# side channels. That is the SAME tool surface the gemini and vertex arms get,
# which is also what makes the arms comparable.
#
# The rest close smaller leaks: --strict-mcp-config drops the developer's
# personal MCP servers, --setting-sources "" drops user/project settings (so a
# stray `model` or `effort` in ~/.claude/settings.json cannot silently change
# the arm mid-study), --disable-slash-commands drops skills.
# --------------------------------------------------------------------------
CLAUDE_SEALED_ARGS = ["--tools", "",
                     "--strict-mcp-config",
                     "--setting-sources", "",
                     "--disable-slash-commands"]


def claude_cli_args() -> list[str]:
    """Extra `claude` CLI flags, assembled from the environment."""
    args: list[str] = []
    # SPARSECRAFT_CLAUDE_BUILTIN_TOOLS=1 hands the built-in toolset back. It
    # exists for debugging the harness, NOT for running an arm: it changes what
    # the agent can reach, so a run made with it is not comparable to one
    # without, and it un-seals the scorer. Never set it for a scored run.
    if os.environ.get("SPARSECRAFT_CLAUDE_BUILTIN_TOOLS", "") not in ("1", "true", "yes"):
        args += CLAUDE_SEALED_ARGS

    # xhigh: the binding constraint in this loop is wall clock, not tokens --
    # a proposal costs cents to generate and 20-40 minutes to elaborate, so
    # thinking harder before proposing is close to free. Levels: low, medium,
    # high, xhigh, max.
    effort = os.environ.get("SPARSECRAFT_CLAUDE_EFFORT", "xhigh").strip()
    if effort:
        args += ["--effort", effort]

    # Both OFF by default and both deliberately so.
    #
    # A fallback model would keep an unattended overnight run alive when Opus
    # is overloaded -- by silently running part of the arm on a DIFFERENT
    # model, which is exactly the kind of un-recorded variable that makes a
    # result unpublishable. Set it only for a run whose results you will not
    # compare against another arm.
    fallback = os.environ.get("SPARSECRAFT_CLAUDE_FALLBACK_MODEL", "").strip()
    if fallback:
        args += ["--fallback-model", fallback]

    # A dollar cap applies to API-key billing only; on an OAuth login the limit
    # is a rate limit, not a budget, and this does nothing.
    budget = os.environ.get("SPARSECRAFT_CLAUDE_MAX_BUDGET_USD", "").strip()
    if budget:
        args += ["--max-budget-usd", budget]

    return args


def claude_cli_status() -> dict:
    """Is the `claude` CLI present, and how would it authenticate?

    Both halves are needed and they fail differently: no binary is a PATH
    problem on this host, no credential is a login the operator has to do
    interactively. Reported separately so the preflight can say which.
    """
    import json as _json
    import shutil

    exe = shutil.which("claude")

    # ANTHROPIC_API_KEY WINS over the stored OAuth login, so it is checked
    # first -- reporting "oauth" while the CLI will actually bill an API key is
    # the sort of preflight that is worse than none.
    if os.environ.get("ANTHROPIC_API_KEY"):
        auth, detail = "api_key", "ANTHROPIC_API_KEY is set (takes precedence over any login)"
    else:
        creds = Path.home() / ".claude" / ".credentials.json"
        try:
            blob = _json.loads(creds.read_text()).get("claudeAiOauth") or {}
            plan = blob.get("subscriptionType") or "unknown plan"
            auth, detail = "oauth", f"logged in ({plan}), {creds}"
        except (FileNotFoundError, ValueError, AttributeError):
            auth, detail = None, f"no ANTHROPIC_API_KEY and no usable login in {creds}"

    return {"exe": exe, "auth": auth, "auth_detail": detail}


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
    info = {
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
    if spec["kind"] == "claude_cli":
        # Readiness here is NOT "is there a key": the CLI authenticates itself
        # and an unset ANTHROPIC_API_KEY is the normal, working case. It is
        # "is the binary reachable and is it logged in".
        cli = claude_cli_status()
        info.update(cli)
        info["credential_var"] = var or ("(oauth login)" if cli["auth"] else None)
        info["credential_present"] = bool(cli["auth"])
        info["ready"] = bool(cli["exe"] and cli["auth"])
    return info


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

    # 900s unless the provider asks for longer; SPARSECRAFT_LLM_TIMEOUT wins
    # over both. An agentic backend needs a bigger budget than a completion
    # one -- see the "timeout" note on the claude provider.
    timeout = int(os.environ.get("SPARSECRAFT_LLM_TIMEOUT") or spec.get("timeout", 900))

    common = dict(model=chosen_model, system_message=system_message,
                  timeout_seconds=timeout, retries=3)
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

    if spec["kind"] == "claude_cli":
        # NOTE the credential is deliberately NOT passed and NOT read. Every
        # other backend here reads its key on the driver and carries it inside
        # the constructed object through Ray's object store; the CLI instead
        # authenticates itself, on the worker, from ~/.claude/.credentials.json
        # (or ANTHROPIC_API_KEY if the operator exported one). So this is the
        # one backend where no secret rides the object store at all. `llm` is
        # advertised on the native head pool, which is the only place that file
        # exists, so the call lands where the login is.
        cli = claude_cli_status()
        if not cli["exe"]:
            raise RuntimeError(
                "backend 'claude' needs the `claude` CLI on PATH and it is not "
                "there. On this host that is ~/bin/claude (a shim onto the VS "
                "Code extension's binary); make sure ~/bin is on PATH, or set "
                "SPARSECRAFT_CLAUDE_BIN to a Claude Code binary.")
        if not cli["auth"]:
            raise RuntimeError(
                f"backend 'claude' found the CLI at {cli['exe']} but no "
                f"credential: {cli['auth_detail']}. {spec['get_key']}")

        from chia.models.claude import ClaudeCodeLLM
        return ClaudeCodeLLM(
            backend="cli",
            logging_name="sparsecraft_claude",
            # One prompt() is one COMPLETE agentic session -- the agent's own
            # turns happen inside the CLI and never come back here -- so there
            # is no conversation for the loop to resume. Leaving this False
            # also switches off claude.py's cross-worker transcript shuttling,
            # which only exists to make --resume work.
            resume_session=False,
            # Only read when resuming. Its default points at a path inside the
            # CHIA container images (/home/ray/...), which does not exist on
            # this native head; None makes it derive from the cwd instead, so a
            # future resume_session=True does not silently write nowhere.
            projects_cwd=None,
            # The MCP tools ARE the sandbox: the only write path is a BashTool
            # pinned to the build container. Prompting for permission on each
            # of them would deadlock an unattended run, and there is nothing
            # left to protect once --tools "" has removed the built-ins.
            dangerously_skip_permissions=True,
            extra_cli_args=claude_cli_args(),
            **common)

    if spec["kind"] == "claude_api":
        if not key:
            raise RuntimeError(
                "backend 'claude_api' needs ANTHROPIC_API_KEY (the CLI's OAuth "
                "login does not apply to the SDK). Use --backend claude for the "
                f"CLI instead. Get a key: {spec['get_key']}")
        from chia.models.claude import ClaudeCodeLLM
        return ClaudeCodeLLM(
            backend="api",
            api_key=key,
            logging_name="sparsecraft_claude_api",
            # Adaptive, not a token budget: budget_tokens is rejected outright
            # by Opus 5. CHIA sends this straight through as
            # thinking={"type": <this>}.
            thinking="adaptive",
            max_tokens=16000,
            **common)

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
