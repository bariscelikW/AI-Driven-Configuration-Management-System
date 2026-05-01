from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests
import json
import os
import ollama
from jsonschema import validate, ValidationError
import copy
import logging

app = FastAPI()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Service URLs
SCHEMA_SERVICE_URL = os.getenv("SCHEMA_SERVICE_URL", "http://schema-server:5001")
VALUES_SERVICE_URL = os.getenv("VALUES_SERVICE_URL", "http://values-server:5002")

# Model name
MODEL_NAME = "qwen2.5-coder:3b"
# Ollama host
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434")
# Create Ollama client
ollama_client = ollama.Client(host=OLLAMA_HOST)


class UserInput(BaseModel):
    input: str


@app.post("/message")
async def process_message(user_input: UserInput):
    """Process user message and return updated configuration."""
    try:
        # Phase 1: Identify application
        app_name = identify_application(user_input.input)
        if not app_name:
            raise HTTPException(status_code=400, detail="Could not identify application from input")

        # Fetch schema and current values
        schema = fetch_schema(app_name)
        current_values = fetch_values(app_name)

        # Phase 2: Identify the JSON path and new value
        path_info = identify_modification_path(user_input.input, current_values)
        if not path_info:
            raise HTTPException(status_code=400, detail="Could not determine what to modify")

        # Phase 3: Apply modification
        updated_values = apply_modification(current_values, path_info)

        # Validate against schema
        try:
            validate(instance=updated_values, schema=schema)
        except ValidationError as e:
            raise HTTPException(status_code=500, detail=f"Generated config invalid: {str(e)}")

        return updated_values

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        raise HTTPException(status_code=500, detail=f"Error processing message: {str(e)}")


def identify_application(user_input: str) -> str:
    """Phase 1: Identify which application to modify."""
    prompt = f"""You are a classifier. Given a user request, identify which application they want to modify.

Available applications: chat, matchmaking, tournament

User request: "{user_input}"

Respond with ONLY the application name. Nothing else."""

    try:
        response = ollama_client.generate(
            model=MODEL_NAME,
            prompt=prompt,
            options={"temperature": 0}
        )
        app_name = response['response'].strip().lower().strip('"\'., ')

        valid_apps = ["chat", "matchmaking", "tournament"]
        if app_name in valid_apps:
            return app_name

        # Fallback: search in response
        for name in valid_apps:
            if name in app_name:
                return name

        return None

    except Exception as e:
        logger.error(f"Error identifying application: {e}")
        return None


def identify_modification_path(user_input: str, current_values: dict) -> dict:
    """
    Phase 2: Identify WHAT to change and HOW.

    1. Flatten the JSON to get all possible paths.
    2. Filter paths based on relevance to user input.
    3. Ask LLM to pick the correct path from the candidates.
    """
    # 1. Flatten all paths
    all_paths = flatten_json(current_values)

    # 2. Filter candidates by keyword scoring
    input_lower = user_input.lower()
    input_terms = input_lower.split()
    common_words = {'service', 'set', 'to', 'of', 'the', 'app', 'application',
                    'value', 'config', 'for', 'lower', 'change', 'update', 'modify'}

    scored_candidates = []

    for path, value in all_paths.items():
        path_lower = path.lower()
        score = 0

        for term in input_terms:
            if len(term) <= 2 or term in common_words:
                continue
            if term in path_lower:
                score += 10
                if f".{term}." in path_lower or path_lower.endswith(f".{term}"):
                    score += 5

        # Boost resource-related paths when user mentions memory/cpu/env
        resource_keywords = {'resources', 'envs', 'cpu', 'memory', 'limit', 'request'}
        for kw in resource_keywords:
            if kw in input_lower and kw in path_lower:
                score += 5

        if score > 0:
            scored_candidates.append((score, path, value))

    # Sort by score descending
    scored_candidates.sort(key=lambda x: x[0], reverse=True)
    top_candidates = scored_candidates[:30]

    if not top_candidates:
        # Fallback: send first 30 paths
        top_candidates = [(0, p, v) for p, v in list(all_paths.items())[:30]]

    candidates_str = "\n".join(f'"{p}": {v}' for _, p, v in top_candidates)

    prompt = f"""You are a configuration assistant. Given a user request and candidate paths, select the correct path to modify.

USER REQUEST: "{user_input}"

CANDIDATE PATHS (path: current_value):
{candidates_str}

RULES:
- "memory" refers to resources.memory.limitMiB 
- "cpu limit" refers to resources.cpu.limitMilliCPU (the LIMIT path, not request)
- "env" or environment variable names refer to envs.VARIABLE_NAME
- If the user specifies a percentage like "%80" or "80%", set action to "percentage" and value to "80%"
- If the user specifies an absolute value like "1024mb", set action to "set_to" and value to "1024"
- If user explicitly says "request", select request path.(set cpu request of matchmaking service to 200 -> select resources.cpu.requestMilliCPU = 200)
- If user explicitly says "limit", select limit path.
- If user does not specify, default to limit.
- Keep in mind "request" is always lower or equal to "limit".

Respond with ONLY this JSON:
{{"path": "exact.path.from.candidates", "action": "set_to or percentage", "value": "the new value"}}"""

    logger.info(f"Phase 2 candidates:\n{candidates_str}")

    try:
        response = ollama_client.generate(
            model=MODEL_NAME,
            prompt=prompt,
            format="json",
            options={"temperature": 0}
        )

        result_text = response['response'].strip()
        path_info = json.loads(result_text)

        # Store current value for reference
        path_info['current_value'] = all_paths.get(path_info.get('path'))

        logger.info(f"Phase 2 result: {path_info}")
        return path_info

    except Exception as e:
        logger.error(f"Error identifying path: {e}")
        return None


def apply_modification(data: dict, path_info: dict) -> dict:
    """Phase 3: Apply the modification with smart logic."""
    result = copy.deepcopy(data)

    path = path_info['path']
    action = str(path_info.get('action', 'set_to'))
    value_str = str(path_info['value'])
    current = path_info.get('current_value')

    # If current_value wasn't found in flatten, try to get it directly
    if current is None:
        current = get_nested_value(data, path)

    # Force percentage action if value contains %
    if '%' in value_str:
        action = "percentage"

    # Calculate new value
    new_value = None

    if action == "percentage":
        if isinstance(current, (int, float)):
            pct_str = value_str.replace('%', '').strip()
            try:
                percentage = float(pct_str) / 100
            except ValueError:
                percentage = 1.0
            new_value = int(current * percentage)
        else:
            logger.warning(f"Cannot apply percentage to non-numeric value: {current}")
            new_value = current
    else:
        # Absolute value with unit conversion
        val_lower = value_str.lower().strip()
        if 'gb' in val_lower:
            nums = ''.join(filter(str.isdigit, val_lower))
            new_value = int(nums) * 1024 if nums else 0
        elif 'mb' in val_lower:
            nums = ''.join(filter(str.isdigit, val_lower))
            new_value = int(nums) if nums else 0
        elif val_lower.isdigit():
            new_value = int(val_lower)
        else:
            # Try to extract number, otherwise keep as string
            try:
                new_value = int(val_lower)
            except ValueError:
                try:
                    new_value = float(val_lower)
                except ValueError:
                    new_value = value_str  # String value (e.g., env var)

    # Apply the change
    if new_value is not None:
        set_nested_value(result, path, new_value)
        logger.info(f"Updated {path}: {current} -> {new_value}")

    # Auto-sync: if we changed a limit, also update the matching request
    if 'limit' in path.lower():
        request_path = path.replace('limit', 'request').replace('Limit', 'Request')
        orig_limit = get_nested_value(data, path)
        orig_request = get_nested_value(data, request_path)

        if orig_request is not None:
            should_sync = False

            # Case 1: limit and request were equal (Guaranteed QoS) -> keep them equal
            if orig_limit == orig_request:
                should_sync = True

            # Case 2: new limit < current request (invalid state) -> must lower request
            if isinstance(new_value, (int, float)) and isinstance(orig_request, (int, float)):
                if new_value < orig_request:
                    should_sync = True

            if should_sync:
                set_nested_value(result, request_path, new_value)
                logger.info(f"Auto-synced {request_path}: {orig_request} -> {new_value}")

    return result


def flatten_json(data, prefix=""):
    """Flatten nested dict into dot-notation paths."""
    out = {}

    def _flatten(obj, name=""):
        if isinstance(obj, dict):
            for key in obj:
                _flatten(obj[key], f"{name}{key}.")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _flatten(item, f"{name}{i}.")
        else:
            out[name[:-1]] = obj

    _flatten(data, prefix)
    return out


def get_nested_value(data, path: str):
    """Get value from nested dict using dot notation."""
    keys = path.split('.')
    current = data
    try:
        for key in keys:
            if isinstance(current, list) and key.isdigit():
                current = current[int(key)]
            elif isinstance(current, dict):
                current = current.get(key)
            else:
                return None
            if current is None:
                return None
        return current
    except Exception:
        return None


def set_nested_value(data, path: str, value):
    """Set value in nested dict using dot notation."""
    keys = path.split('.')
    current = data
    for key in keys[:-1]:
        if isinstance(current, list) and key.isdigit():
            current = current[int(key)]
        elif key in current:
            current = current[key]
        else:
            current[key] = {}
            current = current[key]

    last_key = keys[-1]
    if isinstance(current, list) and last_key.isdigit():
        current[int(last_key)] = value
    else:
        current[last_key] = value


def fetch_schema(app_name: str) -> dict:
    """Fetch schema from schema service."""
    try:
        response = requests.get(f"{SCHEMA_SERVICE_URL}/{app_name}")
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Schema not found: {app_name}")
        response.raise_for_status()
        return response.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch schema: {str(e)}")


def fetch_values(app_name: str) -> dict:
    """Fetch current values from values service."""
    try:
        response = requests.get(f"{VALUES_SERVICE_URL}/{app_name}")
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Values not found: {app_name}")
        response.raise_for_status()
        return response.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch values: {str(e)}")


@app.get("/")
async def health_check():
    return {"status": "ok", "service": "bot-service"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5003)