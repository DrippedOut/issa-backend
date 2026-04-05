"""
Parse conversations.json into (client_sequence, consultant_sequence, preceding_history) tuples.

Each conversation alternates blocks: one or more client ("in") DMs, then one or more consultant ("out")
DMs. Every such pair becomes one row; preceding_chat_history is the full message list before that
client block (may be empty for the first exchange).

Loads the chatbot system prompt from Supabase (`prompts` where name = 'immigration_chatbot').

Use `--tune-prompt` to run the prompt-editor loop (see prompt_editor_prompt.md), write the improved
prompt back to Supabase, then generate fresh sample replies for before/after comparison.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[misc, assignment]


class Message(TypedDict, total=False):
    message_id: int
    direction: str
    text: str
    timestamp: int


class ApiValidationError(ValueError):
    """Invalid API request payload (HTTP 400)."""


class InstructionGuardrailRejected(Exception):
    """Instruction failed safety review; caller should return HTTP 400 guardrail_rejected."""


INSTRUCTION_GUARDRAIL_SYSTEM = """You are a narrow safety reviewer for proposed edits to an immigration-consultancy chatbot system prompt.

The operator sends INSTRUCTIONS that will be used to MODIFY that system prompt.

APPROVE ({"approved": true}) by default. Style, tone, voice, and personality instructions are always fine — examples: "be cute", "be more friendly", "warmer tone", "use more emojis", "shorter replies", "more formal". Clarity, structure, formatting, and factual accuracy improvements are also fine unless they fall under REJECT below.

REJECT ({"approved": false}) ONLY when the instructions EXPLICITLY ask the prompt editor to make the chatbot do one or more of the following:
1. Give false, misleading, or fabricated visa/immigration facts (e.g. lie about requirements, approval odds, or legal rules).
2. Be rude, abusive, insulting, or hostile toward clients.
3. Change specific prices, fees, or monetary amounts stated in the prompt (unless the instruction is only to fix a clear typo with no policy impact — still approve typo fixes).
4. Remove, cancel, or materially weaken the money-back guarantee as described in the prompt.

Do NOT reject for: impersonation vs AI disclosure, discouraging applications, general "company policy", or vague risks — only the four bullets above when clearly requested.

If the instructions are ambiguous or unrelated to those four harms, approve.

Return ONLY a JSON object with no other text and no markdown fences:
{"approved": true}
or
{"approved": false}"""


def parse_guardrail_approval_json(raw: str) -> bool:
    data = json.loads(_strip_json_fences(raw))
    if not isinstance(data, dict):
        raise ValueError(f"Guardrail output must be a JSON object, got: {type(data)}")
    approved = data.get("approved")
    if not isinstance(approved, bool):
        raise ValueError(
            f"Guardrail JSON must include boolean 'approved', got: {data!r}"
        )
    return approved


def run_manual_instruction_guardrail(
    *,
    api_key: str,
    model: str,
    instructions: str,
    max_tokens: int = 256,
) -> None:
    """
    Separate Claude call: rejects only explicit requests for false visa info, rudeness,
    changing stated prices, or removing/weakening the money-back guarantee.
    Raises InstructionGuardrailRejected if not approved.
    Raises ValueError if the model response is not valid guardrail JSON.
    """
    user_turn = (
        "INSTRUCTIONS TO REVIEW (these would be applied to edit the chatbot system prompt):\n"
        f'"""\n{instructions.strip()}\n"""'
    )
    raw = claude_complete_text(
        api_key=api_key,
        model=model,
        system_prompt=INSTRUCTION_GUARDRAIL_SYSTEM,
        user_turn=user_turn,
        max_tokens=max_tokens,
    )
    approved = parse_guardrail_approval_json(raw)
    if not approved:
        raise InstructionGuardrailRejected()


_CONTENT_KEYS = ("content", "body", "text", "system_prompt", "prompt", "value")


def extract_turns(conversation: list[Message]) -> list[tuple[list[Message], list[Message], list[Message]]]:
    """
    Return list of (client_messages, consultant_messages, preceding_chat_history).
    Only includes turns where the client block is immediately followed by at least one outbound reply.
    """
    results: list[tuple[list[Message], list[Message], list[Message]]] = []
    history: list[Message] = []
    i = 0
    n = len(conversation)

    while i < n:
        msg = conversation[i]
        if msg.get("direction") == "out":
            history.append(msg)
            i += 1
            continue

        client_seq: list[Message] = []
        while i < n and conversation[i].get("direction") == "in":
            client_seq.append(conversation[i])
            i += 1

        consultant_seq: list[Message] = []
        while i < n and conversation[i].get("direction") == "out":
            consultant_seq.append(conversation[i])
            i += 1

        if client_seq and consultant_seq:
            results.append((client_seq, consultant_seq, list(history)))

        history.extend(client_seq)
        history.extend(consultant_seq)

    return results


def load_all_turns(
    data_path: Path,
) -> list[tuple[str, str, list[Message], list[Message], list[Message]]]:
    """
    Load JSON and return rows:
    (contact_id, scenario, client_sequence, consultant_sequence, preceding_chat_history)
    """
    raw = json.loads(data_path.read_text(encoding="utf-8"))
    rows: list[tuple[str, str, list[Message], list[Message], list[Message]]] = []

    for conv in raw:
        contact_id = str(conv.get("contact_id", ""))
        scenario = str(conv.get("scenario", ""))
        messages = conv.get("conversation") or []
        if not isinstance(messages, list):
            continue
        for client_seq, consultant_seq, preceding in extract_turns(messages):
            rows.append((contact_id, scenario, client_seq, consultant_seq, preceding))

    return rows


def load_turn_triples(
    data_path: Path | None = None,
) -> list[tuple[list[Message], list[Message], list[Message]]]:
    """Same as load_all_turns but only (client_sequence, consultant_sequence, preceding_history)."""
    path = data_path or Path(__file__).resolve().parent / "conversations.json"
    return [(c, o, h) for _, _, c, o, h in load_all_turns(path)]


def supabase_rest_base_url(supabase_url: str) -> str:
    """
    PostgREST expects https://<project-ref>.supabase.co.
    .env often uses postgresql://...@db.<ref>.supabase.co:5432/postgres — map that to the REST base.
    """
    s = (supabase_url or "").strip()
    if not s:
        raise ValueError("SUPABASE_URL is empty")
    if s.startswith("https://") or s.startswith("http://"):
        return s.rstrip("/")
    if not (s.startswith("postgresql://") or s.startswith("postgres://")):
        raise ValueError(
            "SUPABASE_URL must be https://<ref>.supabase.co or postgresql://...@db.<ref>.supabase.co:5432/..."
        )
    parsed = urllib.parse.urlparse(s)
    host = (parsed.hostname or "").lower()
    if host.startswith("db.") and host.endswith(".supabase.co"):
        ref = host[len("db.") : -len(".supabase.co")]
        if not ref:
            raise ValueError(f"Could not parse project ref from DB host: {host!r}")
        return f"https://{ref}.supabase.co"
    raise ValueError(
        f"Expected DB host db.<ref>.supabase.co for postgresql SUPABASE_URL; got host {host!r}"
    )


def _prompt_text_from_row(row: dict[str, Any]) -> str:
    for key in _CONTENT_KEYS:
        val = row.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    raise ValueError(
        "Prompt row has no non-empty string field among "
        "content, body, text, system_prompt, prompt, value. "
        f"Keys present: {sorted(row.keys())}"
    )


def _content_column_for_row(row: dict[str, Any]) -> str:
    """Column to PATCH when updating prompt text (same priority as read)."""
    for key in _CONTENT_KEYS:
        val = row.get(key)
        if isinstance(val, str) and val.strip():
            return key
    for key in _CONTENT_KEYS:
        if key in row:
            return key
    raise ValueError(
        f"Cannot determine content column for prompts row; keys: {sorted(row.keys())}"
    )


def _supabase_request(
    url: str,
    supabase_key: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout_s: float = 120.0,
) -> tuple[int, str]:
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Accept": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers, method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.getcode(), resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"Supabase HTTP {e.code}: {body or e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Supabase request failed: {e.reason}") from e


def fetch_prompt_row_from_supabase(
    supabase_url: str,
    supabase_key: str,
    *,
    prompt_name: str = "immigration_chatbot",
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    base = supabase_rest_base_url(supabase_url)
    q = urllib.parse.urlencode({"name": f"eq.{prompt_name}", "select": "*"})
    url = f"{base}/rest/v1/prompts?{q}"
    _, raw = _supabase_request(url, supabase_key, timeout_s=timeout_s)
    data = json.loads(raw)
    if not isinstance(data, list) or len(data) == 0:
        raise ValueError(
            f"No row in prompts where name={prompt_name!r} (check RLS and that the row exists)."
        )
    row = data[0]
    if not isinstance(row, dict):
        raise ValueError("Unexpected Supabase response shape for prompts row")
    return row


def load_system_prompt_from_supabase(
    supabase_url: str,
    supabase_key: str,
    *,
    prompt_name: str = "immigration_chatbot",
    timeout_s: float = 30.0,
) -> str:
    row = fetch_prompt_row_from_supabase(
        supabase_url, supabase_key, prompt_name=prompt_name, timeout_s=timeout_s
    )
    return _prompt_text_from_row(row)


def update_prompt_text_in_supabase(
    supabase_url: str,
    supabase_key: str,
    *,
    prompt_name: str,
    new_text: str,
    timeout_s: float = 60.0,
) -> None:
    """PATCH prompts row matching name; updates the same text column returned by the initial GET."""
    row = fetch_prompt_row_from_supabase(
        supabase_url, supabase_key, prompt_name=prompt_name, timeout_s=timeout_s
    )
    col = _content_column_for_row(row)
    base = supabase_rest_base_url(supabase_url)
    q = urllib.parse.urlencode({"name": f"eq.{prompt_name}"})
    url = f"{base}/rest/v1/prompts?{q}"
    payload = json.dumps({col: new_text}, ensure_ascii=False).encode("utf-8")
    _supabase_request(
        url,
        supabase_key,
        method="PATCH",
        data=payload,
        extra_headers={"Content-Type": "application/json", "Prefer": "return=minimal"},
        timeout_s=timeout_s,
    )


def load_markdown_fenced_system_prompt(md_path: Path, *, anchor: str = "## System Prompt") -> str:
    """First ```...``` block after anchor (same pattern as prompt_editor_prompt.md)."""
    text = md_path.read_text(encoding="utf-8")
    i = text.find(anchor)
    if i == -1:
        raise ValueError(f"Missing {anchor!r} in {md_path}")
    fence = text.find("```", i)
    if fence == -1:
        raise ValueError(f"No opening code fence after anchor in {md_path}")
    nl = text.find("\n", fence)
    if nl == -1:
        raise ValueError("Malformed code fence in markdown")
    content_start = nl + 1
    end = text.find("\n```", content_start)
    if end == -1:
        raise ValueError(f"No closing code fence in {md_path}")
    return text[content_start:end].strip()


def _message_to_prompt_line(m: Message) -> str:
    direction = m.get("direction", "")
    body = (m.get("text") or "").strip()
    if direction == "in":
        label = "Client"
    elif direction == "out":
        label = "Consultant"
    else:
        label = "Unknown"
    return f"[{label}]: {body}"


def format_chat_history_block(preceding: list[Message]) -> str:
    if not preceding:
        return "(no prior messages)"
    return "\n".join(_message_to_prompt_line(m) for m in preceding)


def format_client_message_block(client_seq: list[Message]) -> str:
    return "\n".join(_message_to_prompt_line(m) for m in client_seq)


def format_real_consultant_reply(consultant_seq: list[Message]) -> str:
    parts = [(m.get("text") or "").strip() for m in consultant_seq]
    return "\n\n".join(p for p in parts if p)


def build_user_turn(preceding: list[Message], client_seq: list[Message]) -> str:
    """History in chronological order, then latest client message(s), for the chatbot."""
    parts = [_message_to_prompt_line(m) for m in preceding]
    parts.extend(_message_to_prompt_line(m) for m in client_seq)
    return "\n".join(parts)


def build_editor_user_turn(
    current_prompt: str,
    preceding: list[Message],
    client_seq: list[Message],
    consultant_seq: list[Message],
    ai_reply: str,
) -> str:
    return (
        f'CURRENT_PROMPT:\n"""\n{current_prompt}\n"""\n\n'
        f'CHAT_HISTORY:\n"""\n{format_chat_history_block(preceding)}\n"""\n\n'
        f'CLIENT_MESSAGE:\n"""\n{format_client_message_block(client_seq)}\n"""\n\n'
        f'REAL_REPLY:\n"""\n{format_real_consultant_reply(consultant_seq)}\n"""\n\n'
        f'AI_REPLY:\n"""\n{ai_reply}\n"""'
    )


def _strip_json_fences(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = s.removeprefix("```json").removeprefix("```").strip()
        end_fence = s.find("```")
        if end_fence != -1:
            s = s[:end_fence].strip()
    return s


def claude_complete_text(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_turn: str,
    max_tokens: int,
) -> str:
    if anthropic is None:
        raise RuntimeError("Install dependencies: pip install anthropic python-dotenv")
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_turn}],
    )
    block = message.content[0]
    return (getattr(block, "text", None) or str(block)).strip()


def parse_model_reply_json(raw: str) -> str:
    data = json.loads(_strip_json_fences(raw))
    reply = data.get("reply")
    if not isinstance(reply, str):
        raise ValueError(f"Expected JSON object with string 'reply', got: {data!r}")
    return reply


def parse_editor_output_json(raw: str) -> tuple[dict[str, Any], str]:
    data = json.loads(_strip_json_fences(raw))
    if not isinstance(data, dict):
        raise ValueError(f"Editor output must be a JSON object, got: {type(data)}")
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"Editor JSON missing non-empty string 'prompt': keys={list(data.keys())}")
    analysis = data.get("analysis")
    if not isinstance(analysis, dict):
        analysis = {}
    return analysis, prompt.strip()


def generate_claude_reply(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_turn: str,
    max_tokens: int = 1024,
) -> str:
    raw_text = claude_complete_text(
        api_key=api_key,
        model=model,
        system_prompt=system_prompt,
        user_turn=user_turn,
        max_tokens=max_tokens,
    )
    return parse_model_reply_json(raw_text)


def run_prompt_editor(
    *,
    api_key: str,
    model: str,
    editor_system_prompt: str,
    current_prompt: str,
    preceding: list[Message],
    client_seq: list[Message],
    consultant_seq: list[Message],
    ai_reply: str,
    max_tokens: int = 16384,
) -> tuple[dict[str, Any], str]:
    user_turn = build_editor_user_turn(
        current_prompt, preceding, client_seq, consultant_seq, ai_reply
    )
    raw = claude_complete_text(
        api_key=api_key,
        model=model,
        system_prompt=editor_system_prompt,
        user_turn=user_turn,
        max_tokens=max_tokens,
    )
    return parse_editor_output_json(raw)


_dotenv_loaded = False


def ensure_dotenv_loaded() -> None:
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    if load_dotenv is not None:
        root = Path(__file__).resolve().parent
        load_dotenv(dotenv_path=root / ".env")
    _dotenv_loaded = True


@dataclass(frozen=True)
class ServiceConfig:
    anthropic_api_key: str
    anthropic_model: str
    supabase_url: str
    supabase_key: str
    prompt_name: str


def load_service_config_from_env() -> ServiceConfig:
    ensure_dotenv_loaded()
    if load_dotenv is None:
        raise RuntimeError("python-dotenv is not installed")
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise ValueError("ANTHROPIC_API_KEY is not set in the environment")
    supabase_url = (os.environ.get("SUPABASE_URL") or "").strip()
    supabase_key = (os.environ.get("SUPABASE_KEY") or "").strip()
    if not supabase_url or not supabase_key:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in the environment")
    model = (os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-20250514").strip()
    prompt_name = (os.environ.get("SUPABASE_PROMPT_NAME") or "immigration_chatbot").strip()
    return ServiceConfig(
        anthropic_api_key=key,
        anthropic_model=model,
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        prompt_name=prompt_name,
    )


def default_editor_markdown_path() -> Path:
    return _project_dir() / "prompt_editor_prompt.md"


def messages_from_api_payload(
    items: Any,
    field_label: str,
    *,
    default_direction_if_missing: str | None,
) -> list[Message]:
    if not isinstance(items, list):
        raise ApiValidationError(f"{field_label} must be a JSON array")
    out: list[Message] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ApiValidationError(f"{field_label}[{i}] must be an object")
        text = item.get("text")
        if not isinstance(text, str):
            raise ApiValidationError(f"{field_label}[{i}].text must be a string")
        direction_raw = item.get("direction")
        role_raw = item.get("role")
        d: str | None = None
        if direction_raw is not None:
            d = str(direction_raw).strip().lower()
            if d not in ("in", "out"):
                raise ApiValidationError(f"{field_label}[{i}].direction must be 'in' or 'out'")
        elif role_raw is not None:
            r = str(role_raw).strip().lower()
            if r in ("client", "user"):
                d = "in"
            elif r in ("consultant", "assistant"):
                d = "out"
            else:
                raise ApiValidationError(
                    f"{field_label}[{i}].role must be 'client', 'consultant', 'user', or 'assistant'"
                )
        elif default_direction_if_missing is not None:
            d = default_direction_if_missing
        else:
            raise ApiValidationError(
                f"{field_label}[{i}] must include 'direction' ('in'|'out') or 'role' (client|consultant)"
            )
        out.append({"direction": d, "text": text})
    return out


def consultant_reply_string_to_messages(consultant_reply: str) -> list[Message]:
    text = consultant_reply.strip()
    if not text:
        raise ApiValidationError("consultantReply must be a non-empty string")
    return [{"direction": "out", "text": text}]


def build_manual_editor_user_turn(current_prompt: str, instructions: str) -> str:
    return (
        "The operator provided MANUAL INSTRUCTIONS. There is no REAL_REPLY/AI_REPLY pair to compare.\n"
        "Apply INSTRUCTIONS to CURRENT_PROMPT with minimal intervention. Preserve unrelated content.\n"
        "Return the same JSON OUTPUT FORMAT defined in your system prompt (including "
        '"analysis" and "prompt"; analysis may briefly summarize changes under notes).\n\n'
        f'INSTRUCTIONS:\n"""\n{instructions.strip()}\n"""\n\n'
        f'CURRENT_PROMPT:\n"""\n{current_prompt}\n"""'
    )


def run_manual_instruction_editor(
    *,
    api_key: str,
    model: str,
    editor_system_prompt: str,
    current_prompt: str,
    instructions: str,
    max_tokens: int = 16384,
) -> tuple[dict[str, Any], str]:
    user_turn = build_manual_editor_user_turn(current_prompt, instructions)
    raw = claude_complete_text(
        api_key=api_key,
        model=model,
        system_prompt=editor_system_prompt,
        user_turn=user_turn,
        max_tokens=max_tokens,
    )
    return parse_editor_output_json(raw)


def _trunc(text: str, limit: int = 900) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def print_claude_sample(
    contact_id: str,
    scenario: str,
    client_seq: list[Message],
    preceding: list[Message],
    ai_reply: str,
    *,
    label_real: str | None = None,
) -> None:
    """Human-readable block (newest-first history)."""
    print("=" * 72)
    print(f"contact_id: {contact_id}")
    print(f"scenario: {scenario}")
    print()
    print("CLIENT:")
    print()
    for m in client_seq:
        print((m.get("text") or "").strip())
    print()
    print("CHAT HISTORY:")
    print()
    if not preceding:
        print("(no prior messages)")
    else:
        for m in reversed(preceding):
            tag = "CONSULTANT" if m.get("direction") == "out" else "CLIENT"
            print(f"({tag}) {(m.get('text') or '').strip()}")
    print()
    if label_real:
        print(f"{label_real}")
        print()
    print(f"AI REPLY: {ai_reply}")
    print()


def _preview_message(m: Message, max_chars: int = 160) -> dict[str, Any]:
    text = m.get("text", "")
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    return {
        "message_id": m.get("message_id"),
        "direction": m.get("direction"),
        "text": text,
    }


def print_structural_sample(
    rows: list[tuple[str, str, list[Message], list[Message], list[Message]]],
    n: int = 2,
) -> None:
    for idx, (contact_id, scenario, client_seq, consultant_seq, preceding) in enumerate(rows[:n]):
        print(f"--- Sample {idx + 1} ---")
        print(f"contact_id: {contact_id}")
        print(f"scenario: {scenario}")
        print(f"preceding_chat_history ({len(preceding)} msgs):")
        for m in preceding:
            print(f"  {_preview_message(m)}")
        print(f"client_sequence ({len(client_seq)} msgs):")
        for m in client_seq:
            print(f"  {_preview_message(m)}")
        print(f"consultant_sequence ({len(consultant_seq)} msgs):")
        for m in consultant_seq:
            print(f"  {_preview_message(m)}")
        print()


def _sample_row_indices(num_rows: int) -> list[int]:
    """Prefer row 1 then 0 (follow-up with history), then the rest."""
    indices = list(range(num_rows))
    if len(indices) > 1:
        indices = [1, 0] + [i for i in indices if i not in (0, 1)]
    return indices


def _project_dir() -> Path:
    return Path(__file__).resolve().parent


def run_tune_prompt_pipeline(
    *,
    data_path: Path,
    api_key: str,
    model: str,
    supabase_url: str,
    supabase_key: str,
    prompt_name: str,
    editor_md: Path,
    tune_count: int = 5,
    fresh_count: int = 2,
) -> None:
    rows = load_all_turns(data_path)
    if len(rows) == 0:
        print("No turns found in conversations.json.", file=sys.stderr)
        sys.exit(1)

    order = _sample_row_indices(len(rows))
    tune_idx = order[: min(tune_count, len(rows))]

    editor_system = load_markdown_fenced_system_prompt(editor_md)
    initial_prompt = load_system_prompt_from_supabase(
        supabase_url, supabase_key, prompt_name=prompt_name
    )
    current_prompt = initial_prompt

    print("=" * 72)
    print("PROMPT TUNING PIPELINE")
    print("=" * 72)
    print()
    print(f"Using {len(tune_idx)} sample turn(s) (requested {tune_count}).")
    print(f"Initial immigration_chatbot prompt from Supabase ({len(initial_prompt)} chars):")
    print("-" * 72)
    print(_trunc(initial_prompt, 2500))
    print("-" * 72)
    print()

    for step, idx in enumerate(tune_idx, start=1):
        contact_id, scenario, client_seq, consultant_seq, preceding = rows[idx]
        user_turn = build_user_turn(preceding, client_seq)
        print("=" * 72)
        print(f"SAMPLE {step}/{len(tune_idx)}  (row index {idx})")
        print("=" * 72)
        print(f"contact_id: {contact_id} | scenario: {scenario}")
        print()
        print("CLIENT (latest):")
        print(format_client_message_block(client_seq))
        print()
        print("CHAT_HISTORY (chronological):")
        print(format_chat_history_block(preceding))
        print()
        real_reply = format_real_consultant_reply(consultant_seq)
        print("REAL CONSULTANT REPLY:")
        print(real_reply)
        print()

        try:
            ai_reply = generate_claude_reply(
                api_key=api_key,
                model=model,
                system_prompt=current_prompt,
                user_turn=user_turn,
            )
        except Exception as e:
            print(f"Claude (chatbot) error: {e}", file=sys.stderr)
            raise

        print("AI REPLY (using current prompt):")
        print(ai_reply)
        print()

        try:
            analysis, new_prompt = run_prompt_editor(
                api_key=api_key,
                model=model,
                editor_system_prompt=editor_system,
                current_prompt=current_prompt,
                preceding=preceding,
                client_seq=client_seq,
                consultant_seq=consultant_seq,
                ai_reply=ai_reply,
            )
        except Exception as e:
            print(f"Claude (editor) error: {e}", file=sys.stderr)
            raise

        diffs = analysis.get("differences") if isinstance(analysis.get("differences"), list) else []
        edits = analysis.get("edits_planned") if isinstance(analysis.get("edits_planned"), list) else []
        notes = analysis.get("notes") if isinstance(analysis.get("notes"), list) else []
        print("EDITOR ANALYSIS (summary):")
        print(f"  differences: {len(diffs)} | edits_planned: {len(edits)} | notes: {len(notes)}")
        if notes:
            print("  notes:")
            for n in notes[:8]:
                print(f"    - {n}")
        if edits:
            print("  edits_planned (first 5):")
            for e in edits[:5]:
                print(f"    - {e}")
        print()
        print(f"PROMPT AFTER SAMPLE {step} ({len(new_prompt)} chars), excerpt:")
        print("-" * 72)
        print(_trunc(new_prompt, 2500))
        print("-" * 72)
        print()

        current_prompt = new_prompt

    print("=" * 72)
    print("WRITING FINAL PROMPT TO SUPABASE")
    print("=" * 72)
    try:
        update_prompt_text_in_supabase(
            supabase_url,
            supabase_key,
            prompt_name=prompt_name,
            new_text=current_prompt,
        )
    except Exception as e:
        print(f"Supabase PATCH failed: {e}", file=sys.stderr)
        raise
    print("PATCH OK (prompts row updated).")
    print()

    reloaded = load_system_prompt_from_supabase(
        supabase_url, supabase_key, prompt_name=prompt_name
    )
    print("=" * 72)
    print("RELOADED PROMPT FROM SUPABASE (verify)")
    print("=" * 72)
    print(_trunc(reloaded, 2500))
    print()

    if fresh_count > 0:
        tune_set = set(tune_idx)
        remaining = [i for i in order if i not in tune_set]
        fresh_indices: list[int] = []
        for i in remaining:
            if len(fresh_indices) >= fresh_count:
                break
            fresh_indices.append(i)
        if len(fresh_indices) < fresh_count:
            for i in range(len(rows)):
                if i not in tune_set and i not in fresh_indices:
                    fresh_indices.append(i)
                if len(fresh_indices) >= fresh_count:
                    break
        if len(fresh_indices) < fresh_count:
            for i in range(len(rows)):
                if i not in fresh_indices:
                    fresh_indices.append(i)
                if len(fresh_indices) >= fresh_count:
                    break

        print("=" * 72)
        print(f"FRESH AI REPLIES ({min(fresh_count, len(fresh_indices))}) USING UPDATED PROMPT FROM SUPABASE")
        print("=" * 72)
        for i, idx in enumerate(fresh_indices[:fresh_count], start=1):
            contact_id, scenario, client_seq, consultant_seq, preceding = rows[idx]
            user_turn = build_user_turn(preceding, client_seq)
            reply = generate_claude_reply(
                api_key=api_key,
                model=model,
                system_prompt=reloaded,
                user_turn=user_turn,
            )
            real_excerpt = _trunc(format_real_consultant_reply(consultant_seq), 500)
            print_claude_sample(
                contact_id,
                scenario,
                client_seq,
                preceding,
                reply,
                label_real=f"REAL CONSULTANT REPLY (reference): {real_excerpt}",
            )

    print("=" * 72)
    print("DONE — BEFORE (initial Supabase) vs AFTER (final tuned prompt)")
    print("=" * 72)
    print("BEFORE excerpt:")
    print(_trunc(initial_prompt, 1200))
    print()
    print("AFTER excerpt:")
    print(_trunc(current_prompt, 1200))
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build conversation turns and/or call Claude for sample replies.")
    parser.add_argument(
        "data_file",
        nargs="?",
        default=None,
        help="Path to conversations.json (default: next to this script)",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only print parsed JSON structure samples (no API calls).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print CLIENT / CHAT HISTORY blocks without calling the API.",
    )
    parser.add_argument(
        "--tune-prompt",
        action="store_true",
        help="Run editor loop on 5 turns, save prompt to Supabase, print 2 fresh replies.",
    )
    parser.add_argument(
        "--tune-samples",
        type=int,
        default=5,
        help="With --tune-prompt: number of tuning turns (default: 5).",
    )
    parser.add_argument(
        "--fresh-samples",
        type=int,
        default=2,
        help="With --tune-prompt: fresh AI replies after save (default: 2).",
    )
    parser.add_argument(
        "--editor-md",
        type=Path,
        default=None,
        help="Path to prompt_editor_prompt.md (default: next to this script).",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=3,
        help="Number of turns to print or send to Claude (default: 3).",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
        help="Claude model id (default: claude-sonnet-4-20250514 or ANTHROPIC_MODEL env).",
    )
    parser.add_argument(
        "--prompt-name",
        default=os.environ.get("SUPABASE_PROMPT_NAME", "immigration_chatbot"),
        help="Value of prompts.name in Supabase (default: immigration_chatbot or SUPABASE_PROMPT_NAME).",
    )
    args = parser.parse_args()

    data_path = Path(args.data_file).resolve() if args.data_file else _project_dir() / "conversations.json"
    if not data_path.is_file():
        print(f"File not found: {data_path}", file=sys.stderr)
        sys.exit(1)

    rows = load_all_turns(data_path)
    triples = [(c, o, h) for _, _, c, o, h in rows]

    print(f"Loaded {data_path.name}: {len(rows)} client->consultant turns across all conversations.")
    print(f"(As triples: {len(triples)}; use load_turn_triples() when importing this module.)")
    print()

    if args.list_only:
        print_structural_sample(rows, n=2)
        return

    n_samples = max(0, args.samples)
    indices = _sample_row_indices(len(rows))

    if args.dry_run:
        for k in range(min(n_samples, len(rows))):
            idx = indices[k]
            contact_id, scenario, client_seq, _c, preceding = rows[idx]
            print_claude_sample(
                contact_id,
                scenario,
                client_seq,
                preceding,
                "(dry-run: no API call)",
            )
        return

    if args.tune_prompt:
        if load_dotenv is None:
            print("Missing dependency: pip install python-dotenv anthropic", file=sys.stderr)
            sys.exit(1)
        load_dotenv(dotenv_path=_project_dir() / ".env")
        api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
            sys.exit(1)
        supabase_url = (os.environ.get("SUPABASE_URL") or "").strip()
        supabase_key = (os.environ.get("SUPABASE_KEY") or "").strip()
        if not supabase_url or not supabase_key:
            print("SUPABASE_URL and SUPABASE_KEY must be set in .env.", file=sys.stderr)
            sys.exit(1)
        editor_md = Path(args.editor_md).resolve() if args.editor_md else _project_dir() / "prompt_editor_prompt.md"
        if not editor_md.is_file():
            print(f"Editor prompt file not found: {editor_md}", file=sys.stderr)
            sys.exit(1)
        try:
            run_tune_prompt_pipeline(
                data_path=data_path,
                api_key=api_key,
                model=args.model,
                supabase_url=supabase_url,
                supabase_key=supabase_key,
                prompt_name=args.prompt_name,
                editor_md=editor_md,
                tune_count=max(1, args.tune_samples),
                fresh_count=max(0, args.fresh_samples),
            )
        except Exception as e:
            print(f"Tuning pipeline failed: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if load_dotenv is None:
        print("Missing dependency: pip install python-dotenv anthropic", file=sys.stderr)
        sys.exit(1)

    load_dotenv(dotenv_path=_project_dir() / ".env")
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        print(
            "ANTHROPIC_API_KEY is not set. Add it to .env, or use --list-only / --dry-run.",
            file=sys.stderr,
        )
        sys.exit(1)

    supabase_url = (os.environ.get("SUPABASE_URL") or "").strip()
    supabase_key = (os.environ.get("SUPABASE_KEY") or "").strip()
    if not supabase_url or not supabase_key:
        print(
            "SUPABASE_URL and SUPABASE_KEY must be set in .env to load the system prompt from Supabase.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        system_prompt = load_system_prompt_from_supabase(
            supabase_url,
            supabase_key,
            prompt_name=args.prompt_name,
        )
    except Exception as e:
        print(f"Failed to load system prompt from Supabase: {e}", file=sys.stderr)
        sys.exit(1)

    for k in range(min(n_samples, len(rows))):
        idx = indices[k]
        contact_id, scenario, client_seq, _consultant_seq, preceding = rows[idx]
        user_turn = build_user_turn(preceding, client_seq)
        try:
            reply = generate_claude_reply(
                api_key=api_key,
                model=args.model,
                system_prompt=system_prompt,
                user_turn=user_turn,
            )
        except Exception as e:
            err = str(e)
            print(f"Claude API error on row {idx}: {err}", file=sys.stderr)
            low = err.lower()
            if "credit balance" in low or "billing" in low:
                print(
                    "Add credits under Anthropic Plans & Billing, or use --dry-run to preview formatting only.",
                    file=sys.stderr,
                )
            sys.exit(1)
        print_claude_sample(contact_id, scenario, client_seq, preceding, reply)


if __name__ == "__main__":
    main()
