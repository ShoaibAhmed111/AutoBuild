# llm_client.py
import asyncio
import os
import time

import litellm
from dotenv import load_dotenv
from litellm import completion

load_dotenv()

litellm.suppress_debug_info = True
litellm.set_verbose = False
litellm.logging = False

MODEL_PRESETS = {
    "ollama_14b": "ollama/qwen2.5-coder:14b",
    "ollama_7b": "ollama/qwen2.5-coder:7b",
    "gemini_flash": "gemini/gemini-2.5-flash",
    "openrouter_qwen": "openrouter/qwen/qwen-2.5-coder-32b-instruct:free",
    "openrouter_r1": "openrouter/deepseek/deepseek-r1:free",
}

# Keys whose requests never leave the machine. Used only to give the user an
# explicit, truthful log line about where their code/prompts are actually
# going -- this project's "local/autonomous" positioning is only honest if
# this distinction is visible and enforced rather than assumed.
LOCAL_MODEL_KEYS = {"ollama_14b", "ollama_7b"}


class UnknownModelError(ValueError):
    """Raised when an unrecognized model key is requested. Callers MUST
    surface this as a visible configuration error -- the old behavior of
    silently falling back to a cloud provider on an unrecognized/mistyped
    key (e.g. server.py's old "ollama_local" default, which isn't a real
    preset key) is exactly the kind of silent behavior this project cannot
    afford: it would send local-only requests to a cloud API without the
    user ever knowing."""
    pass


class UniversalLLM:
    def __init__(self, selected_model_key: str = "openrouter_qwen"):
        self.active_key = None
        self.active_model = None
        self.set_active_model(selected_model_key)

    def set_active_model(self, key: str):
        """Fail CLOSED on an unrecognized key -- no silent fallback to any
        default preset, local or cloud."""
        if key not in MODEL_PRESETS:
            valid = ", ".join(sorted(MODEL_PRESETS.keys()))
            raise UnknownModelError(f"Unknown model key '{key}'. Valid keys are: {valid}")
        self.active_key = key
        self.active_model = MODEL_PRESETS[key]

    def is_local(self) -> bool:
        return self.active_key in LOCAL_MODEL_KEYS

    def _provider_kwargs(self) -> dict:
        """Raises a clear, provider-specific error immediately if a required
        credential is missing, rather than deferring to an opaque error from
        litellm/the provider itself."""
        kwargs = {}
        if "openrouter" in self.active_model:
            api_key = os.getenv("OPENROUTER_API_KEY")
            if not api_key:
                raise ValueError("OPENROUTER_API_KEY is missing from your .env file.")
            kwargs["api_key"] = api_key
            kwargs["custom_llm_provider"] = "openrouter"
        elif "gemini" in self.active_model:
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise ValueError("GEMINI_API_KEY is missing from your .env file.")
            # litellm reads GEMINI_API_KEY from the environment itself; this
            # check exists purely to fail fast with a clear message instead
            # of surfacing litellm's more opaque error later.
        return kwargs

    @staticmethod
    def _is_permanent_error(exc: Exception) -> bool:
        """Best-effort classification: configuration/auth errors will not
        resolve themselves on retry, so retrying them just wastes time and
        produces a confusing final message. Everything else is treated as
        potentially transient (network blip, rate limit, timeout)."""
        if isinstance(exc, (ValueError, UnknownModelError)):
            return True
        text = str(exc).lower()
        permanent_markers = ("api key", "authentication", "unauthorized", "invalid api", "not found: model")
        return any(m in text for m in permanent_markers)

    def generate(self, prompt: str, system_prompt: str = "You are an expert developer.", max_retries: int = 2) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                kwargs = self._provider_kwargs()
                response = completion(model=self.active_model, messages=messages, temperature=0.2, **kwargs)
                return response.choices[0].message.content
            except Exception as e:
                last_error = e
                if self._is_permanent_error(e):
                    # Don't burn retries on an error that retrying can't fix.
                    break
                if attempt < max_retries:
                    time.sleep(1.0)

        raise RuntimeError(f"Failed using model [{self.active_model}]. Error: {last_error}")

    async def generate_async(self, prompt: str, system_prompt: str = "You are an expert developer.", max_retries: int = 2) -> str:
        """Runs the (blocking) generate() call in a worker thread so it never
        blocks an async event loop -- callers running inside an async server
        must use this (or asyncio.to_thread(self.generate, ...)) rather than
        calling generate() directly from an async function."""
        return await asyncio.to_thread(self.generate, prompt, system_prompt, max_retries)
