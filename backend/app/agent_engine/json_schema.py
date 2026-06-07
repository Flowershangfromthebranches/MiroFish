"""Small JSON Schema validator for agent response contracts.

The project already depends on Pydantic, but not jsonschema. This validator
implements the subset we use in queue contracts so smoke tests stay offline.
"""

from __future__ import annotations

from typing import Any, Dict, List


def validate_json_schema(value: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    errors: List[str] = []
    schema_type = schema.get("type")

    if schema_type == "null":
        return [] if value is None else [f"{path}: expected null"]

    if schema_type == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object"]
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key}: missing required field")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                errors.extend(validate_json_schema(item, properties[key], f"{path}.{key}"))
            elif additional is False:
                errors.append(f"{path}.{key}: extra field is not allowed")
        return errors

    if schema_type == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        item_schema = schema.get("items", {})
        for index, item in enumerate(value):
            errors.extend(validate_json_schema(item, item_schema, f"{path}[{index}]"))
        return errors

    if schema_type == "string":
        if not isinstance(value, str):
            return [f"{path}: expected string"]
        if schema.get("minLength") is not None and len(value) < int(schema["minLength"]):
            errors.append(f"{path}: shorter than minLength")
        return errors

    if schema_type == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return [f"{path}: expected number"]
        if schema.get("minimum") is not None and value < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if schema.get("maximum") is not None and value > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")
        return errors

    if schema_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return [f"{path}: expected integer"]
        return errors

    if schema_type == "boolean":
        if not isinstance(value, bool):
            return [f"{path}: expected boolean"]
        return errors

    if isinstance(schema_type, list):
        matched = False
        nested_errors: List[str] = []
        for candidate in schema_type:
            candidate_schema = {**schema, "type": candidate}
            candidate_errors = validate_json_schema(value, candidate_schema, path)
            if not candidate_errors:
                matched = True
                break
            nested_errors.extend(candidate_errors)
        if not matched:
            errors.append(f"{path}: did not match any allowed type {schema_type}; {nested_errors[:2]}")
        return errors

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        errors.append(f"{path}: value {value!r} not in enum")
    return errors


TRIPLE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "subject",
        "predicate",
        "object",
        "fact",
        "valid_at",
        "invalid_at",
        "source",
        "source_file",
        "evidence",
        "confidence",
        "metadata",
    ],
    "properties": {
        "subject": {"type": "string", "minLength": 1},
        "predicate": {"type": "string", "minLength": 1},
        "object": {"type": "string", "minLength": 1},
        "fact": {"type": "string", "minLength": 1},
        "valid_at": {"type": ["string", "null"]},
        "invalid_at": {"type": ["string", "null"]},
        "source": {"type": ["string", "null"]},
        "source_file": {"type": ["string", "null"]},
        "evidence": {"type": "string", "minLength": 1},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "metadata": {"type": "object"},
    },
}


def object_schema(properties: Dict[str, Any], required: List[str] | None = None) -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required or list(properties.keys()),
        "properties": properties,
    }
