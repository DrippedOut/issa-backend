from flask import Flask, jsonify, request
from flask_cors import CORS

from build_conversation_turns import (
    ApiValidationError,
    InstructionGuardrailRejected,
    build_user_turn,
    consultant_reply_string_to_messages,
    default_editor_markdown_path,
    generate_claude_reply,
    load_markdown_fenced_system_prompt,
    load_service_config_from_env,
    load_system_prompt_from_supabase,
    messages_from_api_payload,
    run_manual_instruction_editor,
    run_manual_instruction_guardrail,
    run_prompt_editor,
    update_prompt_text_in_supabase,
)

app = Flask(__name__)
CORS(app, origins=["http://localhost:3000"])


def _load_editor_system_prompt() -> str:
    path = default_editor_markdown_path()
    if not path.is_file():
        raise FileNotFoundError(f"Editor prompt markdown not found: {path}")
    return load_markdown_fenced_system_prompt(path)


def _json_error(message: str, status: int = 400, *, code: str | None = None) -> tuple:
    body: dict = {"error": message}
    if code:
        body["code"] = code
    return jsonify(body), status


def _get_config():
    try:
        return load_service_config_from_env()
    except ValueError as e:
        raise ConfigError(str(e)) from e
    except RuntimeError as e:
        raise ConfigError(str(e)) from e


class ConfigError(Exception):
    pass


def _parse_json_body() -> dict | tuple:
    data = request.get_json(silent=True)
    if data is None or not isinstance(data, dict):
        return _json_error("Request body must be a JSON object", 400)
    return data


def _get(d: dict, *keys: str):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


@app.route("/")
def hello():
    return {"message": "Hello World"}, 200


@app.post("/generate-reply")
def generate_reply():
    try:
        cfg = _get_config()
    except ConfigError as e:
        return _json_error(str(e), 500, code="config")

    parsed = _parse_json_body()
    if isinstance(parsed, tuple):
        return parsed
    data = parsed

    client_raw = _get(data, "clientSequence", "client_sequence")
    history_raw = _get(data, "chatHistory", "chat_history")
    if client_raw is None:
        return _json_error("Missing required field: clientSequence", 400)
    if history_raw is None:
        return _json_error("Missing required field: chatHistory", 400)

    try:
        client_seq = messages_from_api_payload(
            client_raw, "clientSequence", default_direction_if_missing="in"
        )
        if len(client_seq) == 0:
            return _json_error("clientSequence must contain at least one message", 400)
        chat_history = messages_from_api_payload(
            history_raw, "chatHistory", default_direction_if_missing=None
        )
        system_prompt = load_system_prompt_from_supabase(
            cfg.supabase_url, cfg.supabase_key, prompt_name=cfg.prompt_name
        )
        user_turn = build_user_turn(chat_history, client_seq)
        ai_reply = generate_claude_reply(
            api_key=cfg.anthropic_api_key,
            model=cfg.anthropic_model,
            system_prompt=system_prompt,
            user_turn=user_turn,
        )
    except ApiValidationError as e:
        return _json_error(str(e), 400, code="validation")
    except ValueError as e:
        return _json_error(str(e), 502, code="upstream")
    except FileNotFoundError as e:
        return _json_error(str(e), 500, code="config")
    except Exception as e:
        return _json_error(
            f"Upstream error: {e}",
            502,
            code="upstream",
        )

    return jsonify({"aiReply": ai_reply}), 200


@app.post("/improve-ai")
def improve_ai():
    try:
        cfg = _get_config()
    except ConfigError as e:
        return _json_error(str(e), 500, code="config")

    parsed = _parse_json_body()
    if isinstance(parsed, tuple):
        return parsed
    data = parsed

    client_raw = _get(data, "clientSequence", "client_sequence")
    history_raw = _get(data, "chatHistory", "chat_history")
    consultant_raw = _get(data, "consultantReply", "consultant_reply")
    if client_raw is None:
        return _json_error("Missing required field: clientSequence", 400)
    if history_raw is None:
        return _json_error("Missing required field: chatHistory", 400)
    if consultant_raw is None:
        return _json_error("Missing required field: consultantReply", 400)
    if not isinstance(consultant_raw, str):
        return _json_error("consultantReply must be a string", 400)

    try:
        client_seq = messages_from_api_payload(
            client_raw, "clientSequence", default_direction_if_missing="in"
        )
        if len(client_seq) == 0:
            return _json_error("clientSequence must contain at least one message", 400)
        chat_history = messages_from_api_payload(
            history_raw, "chatHistory", default_direction_if_missing=None
        )
        consultant_seq = consultant_reply_string_to_messages(consultant_raw)
        current_prompt = load_system_prompt_from_supabase(
            cfg.supabase_url, cfg.supabase_key, prompt_name=cfg.prompt_name
        )
        user_turn = build_user_turn(chat_history, client_seq)
        predicted = generate_claude_reply(
            api_key=cfg.anthropic_api_key,
            model=cfg.anthropic_model,
            system_prompt=current_prompt,
            user_turn=user_turn,
        )
        editor_system = _load_editor_system_prompt()
        _analysis, updated_prompt = run_prompt_editor(
            api_key=cfg.anthropic_api_key,
            model=cfg.anthropic_model,
            editor_system_prompt=editor_system,
            current_prompt=current_prompt,
            preceding=chat_history,
            client_seq=client_seq,
            consultant_seq=consultant_seq,
            ai_reply=predicted,
        )
        update_prompt_text_in_supabase(
            cfg.supabase_url,
            cfg.supabase_key,
            prompt_name=cfg.prompt_name,
            new_text=updated_prompt,
        )
    except ApiValidationError as e:
        return _json_error(str(e), 400, code="validation")
    except ValueError as e:
        return _json_error(str(e), 502, code="upstream")
    except FileNotFoundError as e:
        return _json_error(str(e), 500, code="config")
    except Exception as e:
        return _json_error(
            f"Upstream error: {e}",
            502,
            code="upstream",
        )

    return jsonify({"predictedReply": predicted, "updatedPrompt": updated_prompt}), 200


@app.post("/improve-ai-manually")
def improve_ai_manually():
    try:
        cfg = _get_config()
    except ConfigError as e:
        return _json_error(str(e), 500, code="config")

    parsed = _parse_json_body()
    if isinstance(parsed, tuple):
        return parsed
    data = parsed

    instructions = _get(data, "instructions")
    if instructions is None:
        return _json_error("Missing required field: instructions", 400)
    if not isinstance(instructions, str) or not instructions.strip():
        return _json_error("instructions must be a non-empty string", 400)

    try:
        run_manual_instruction_guardrail(
            api_key=cfg.anthropic_api_key,
            model=cfg.anthropic_model,
            instructions=instructions,
        )
    except InstructionGuardrailRejected:
        return _json_error(
            "This feedback was flagged as potentially harmful and was not applied.",
            400,
            code="guardrail_rejected",
        )
    except ValueError as e:
        return _json_error(str(e), 502, code="upstream")
    except Exception as e:
        return _json_error(f"Guardrail check failed: {e}", 502, code="upstream")

    try:
        current_prompt = load_system_prompt_from_supabase(
            cfg.supabase_url, cfg.supabase_key, prompt_name=cfg.prompt_name
        )
        editor_system = _load_editor_system_prompt()
        _analysis, updated_prompt = run_manual_instruction_editor(
            api_key=cfg.anthropic_api_key,
            model=cfg.anthropic_model,
            editor_system_prompt=editor_system,
            current_prompt=current_prompt,
            instructions=instructions,
        )
        update_prompt_text_in_supabase(
            cfg.supabase_url,
            cfg.supabase_key,
            prompt_name=cfg.prompt_name,
            new_text=updated_prompt,
        )
    except ApiValidationError as e:
        return _json_error(str(e), 400, code="validation")
    except ValueError as e:
        return _json_error(str(e), 502, code="upstream")
    except FileNotFoundError as e:
        return _json_error(str(e), 500, code="config")
    except Exception as e:
        return _json_error(
            f"Upstream error: {e}",
            502,
            code="upstream",
        )

    return jsonify({"updatedPrompt": updated_prompt}), 200


if __name__ == "__main__":
    app.run(debug=True)
