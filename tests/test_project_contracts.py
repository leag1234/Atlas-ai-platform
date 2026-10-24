"""REQ-PROJ-02/04/06/09: offline validation of the frozen P0 interface.

No JSON Schema package is installed in the task environment. This test-only,
fail-closed validator implements exactly the draft 2020-12 subset used here.
It is not a general OpenAPI validator or a runtime authorization implementation.
"""

from __future__ import annotations

import copy
import json
import math
import re
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts/project-mode.openapi.yaml"
EXAMPLES = ROOT / "contracts/examples/project-mode"
KEYWORDS = {
    "$ref",
    "type",
    "const",
    "enum",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "pattern",
    "format",
    "oneOf",
    "anyOf",
    "allOf",
    "not",
    "if",
    "then",
    "else",
    "description",
    "default",
}


class Invalid(ValueError):
    """An instance does not satisfy a supported schema."""


class SchemaValidator:
    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document

    def resolve(self, reference: str) -> dict[str, Any]:
        if not reference.startswith("#/components/schemas/"):
            raise ValueError("Only local schema references are supported")
        value: Any = self.document
        for part in reference[2:].split("/"):
            value = value[part.replace("~1", "/").replace("~0", "~")]
        if not isinstance(value, dict):
            raise ValueError("Reference must resolve to a schema object")
        return value

    def check_schema(self, schema: dict[str, Any]) -> None:
        unsupported = set(schema) - KEYWORDS
        if unsupported:
            raise ValueError(f"Unsupported schema keywords: {sorted(unsupported)}")
        if "$ref" in schema:
            self.resolve(schema["$ref"])
        for field in ("oneOf", "anyOf", "allOf"):
            for child in schema.get(field, []):
                self.check_schema(child)
        for field in ("not", "if", "then", "else", "items"):
            if field in schema:
                self.check_schema(schema[field])
        for child in schema.get("properties", {}).values():
            self.check_schema(child)
        if schema.get("format") not in (None, "uuid", "date-time", "uri", "binary"):
            raise ValueError("Unsupported format")
        if schema.get("type") not in (
            None,
            "object",
            "array",
            "string",
            "integer",
            "number",
            "boolean",
            "null",
        ):
            raise ValueError("Unsupported type")
        if (
            "additionalProperties" in schema
            and schema["additionalProperties"] is not False
        ):
            raise ValueError("Only closed objects are supported")

    def matches(self, value: Any, schema: dict[str, Any]) -> bool:
        try:
            self.validate(value, schema)
        except Invalid:
            return False
        return True

    def validate(self, value: Any, schema: dict[str, Any]) -> None:
        self.check_schema(schema)
        if "$ref" in schema:
            self.validate(value, self.resolve(schema["$ref"]))
        for keyword in ("oneOf", "anyOf", "allOf"):
            if keyword in schema:
                hits = sum(self.matches(value, child) for child in schema[keyword])
                expected = {
                    "oneOf": hits == 1,
                    "anyOf": hits > 0,
                    "allOf": hits == len(schema[keyword]),
                }[keyword]
                if not expected:
                    raise Invalid(keyword)
        if "not" in schema and self.matches(value, schema["not"]):
            raise Invalid("not")
        if "if" in schema:
            branch = "then" if self.matches(value, schema["if"]) else "else"
            self.validate(value, schema.get(branch, {}))
        types = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": type(value) is int,
            "number": type(value) in (int, float),
            "boolean": type(value) is bool,
            "null": value is None,
        }
        if "type" in schema and not types[schema["type"]]:
            raise Invalid("type")

        # JSON booleans are not numeric constants, unlike Python equality.
        def equal(left: Any, right: Any) -> bool:
            if isinstance(left, bool) != isinstance(right, bool):
                return False
            return bool(left == right)

        if "const" in schema and not equal(value, schema["const"]):
            raise Invalid("const")
        if "enum" in schema and not any(equal(value, item) for item in schema["enum"]):
            raise Invalid("enum")
        if isinstance(value, dict):
            if not set(schema.get("required", [])).issubset(value):
                raise Invalid("required")
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False and set(value) - set(
                properties
            ):
                raise Invalid("additionalProperties")
            if len(value) < schema.get("minProperties", 0):
                raise Invalid("minProperties")
            for key, item in value.items():
                if key in properties:
                    self.validate(item, properties[key])
        if isinstance(value, list):
            if (
                not schema.get("minItems", 0)
                <= len(value)
                <= schema.get("maxItems", math.inf)
            ):
                raise Invalid("array length")
            if schema.get("uniqueItems"):
                encoded = [json.dumps(item, sort_keys=True) for item in value]
                if len(set(encoded)) != len(encoded):
                    raise Invalid("uniqueItems")
            for item in value:
                if "items" in schema:
                    self.validate(item, schema["items"])
        if isinstance(value, str):
            if (
                not schema.get("minLength", 0)
                <= len(value)
                <= schema.get("maxLength", math.inf)
            ):
                raise Invalid("string length")
            if "pattern" in schema and re.search(schema["pattern"], value) is None:
                raise Invalid("pattern")
            fmt = schema.get("format")
            try:
                if fmt == "uuid":
                    UUID(value)
                    if (
                        re.fullmatch(
                            r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}",
                            value,
                        )
                        is None
                    ):
                        raise ValueError("UUID spelling")
                if fmt == "date-time":
                    if (
                        re.fullmatch(
                            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value
                        )
                        is None
                    ):
                        raise ValueError("UTC date-time spelling")
                    datetime.fromisoformat(value.replace("Z", "+00:00"))
                if fmt == "uri":
                    parsed = urlsplit(value)
                    if not parsed.scheme or any(c.isspace() for c in value):
                        raise ValueError("URI spelling")
            except ValueError as exc:
                raise Invalid("format") from exc
        if type(value) in (int, float):
            if not math.isfinite(value):
                raise Invalid("finite number")
            if (
                not schema.get("minimum", -math.inf)
                <= value
                <= schema.get("maximum", math.inf)
            ):
                raise Invalid("numeric bound")


class ProjectContractTests(unittest.TestCase):
    document: dict[str, Any]
    validator: SchemaValidator
    schemas: dict[str, dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = yaml.safe_load(CONTRACT.read_text())
        cls.validator = SchemaValidator(cls.document)
        cls.schemas = cls.document["components"]["schemas"]

    def test_commit_snapshot_and_target_are_required_and_closed(self) -> None:
        value = json.loads((EXAMPLES / "CommitAction.valid.json").read_text())
        self.validator.validate(value, self.schemas["RepositoryAction"])
        for field in ("target_branch", "snapshot"):
            invalid = copy.deepcopy(value)
            del invalid[field]
            with self.assertRaises(Invalid):
                self.validator.validate(invalid, self.schemas["CommitAction"])
        for field, replacement in (
            ("id", "not-a-uuid"),
            ("sha256", "not-a-hash"),
            ("token", "forbidden"),
        ):
            invalid = copy.deepcopy(value)
            invalid["snapshot"][field] = replacement
            with self.assertRaises(Invalid):
                self.validator.validate(invalid, self.schemas["CommitAction"])

    def test_upload_destination_is_required_query_and_never_multipart(self) -> None:
        operation = self.document["paths"][
            "/api/atlas/orgs/{org}/projects/{project}/files"
        ]["post"]
        parameter = next(
            p for p in operation["parameters"] if p.get("name") == "folder_id"
        )
        self.assertEqual(parameter["in"], "query")
        self.assertTrue(parameter["required"])
        self.assertEqual(parameter["schema"], {"type": "string", "format": "uuid"})
        self.assertNotIn("folder_id", self.schemas["FileUpload"]["properties"])
        self.assertNotIn("folder_id", self.schemas["FileUpload"]["required"])
        valid = json.loads((EXAMPLES / "FileUpload.valid.json").read_text())
        self.validator.validate(valid, self.schemas["FileUpload"])
        with self.assertRaises(Invalid):
            self.validator.validate(
                dict(valid, folder_id="10000000-0000-4000-8000-000000000001"),
                self.schemas["FileUpload"],
            )

    def test_every_schema_has_valid_examples_and_rejects_invalid_examples(self) -> None:
        valid: set[str] = set()
        invalid = 0
        for path in sorted(EXAMPLES.glob("*.json")):
            schema_name = path.name.split(".")[0]
            with self.subTest(example=path.name):
                self.assertIn(schema_name, self.schemas)
                value = json.loads(path.read_text())
                if path.name.endswith(".valid.json"):
                    self.validator.validate(value, self.schemas[schema_name])
                    valid.add(schema_name)
                else:
                    self.assertTrue(path.name.endswith(".invalid.json"))
                    with self.assertRaises(Invalid):
                        self.validator.validate(value, self.schemas[schema_name])
                    invalid += 1
        self.assertEqual(set(self.schemas), valid)
        self.assertGreaterEqual(invalid, 20)

    def test_validator_fails_closed_and_checks_each_supported_constraint(self) -> None:
        pairs: list[tuple[dict[str, Any], Any]] = [
            ({"type": "integer"}, True),
            ({"const": 1}, True),
            ({"enum": [0]}, False),
            ({"type": "number"}, "1"),
            ({"type": "number"}, float("nan")),
            ({"type": "object", "required": ["id"]}, {}),
            (
                {"type": "object", "properties": {}, "additionalProperties": False},
                {"extra": 1},
            ),
            ({"type": "object", "minProperties": 1}, {}),
            ({"type": "array", "minItems": 1}, []),
            ({"type": "array", "maxItems": 1}, [1, 2]),
            ({"type": "array", "uniqueItems": True}, ["a", "a"]),
            ({"type": "array", "items": {"type": "integer"}}, ["a"]),
            ({"type": "string", "minLength": 1}, ""),
            ({"type": "string", "maxLength": 1}, "ab"),
            ({"type": "string", "pattern": "^a$"}, "b"),
            ({"type": "number", "minimum": 0}, -1),
            ({"type": "number", "maximum": 1}, 2),
            ({"format": "uuid"}, "bad"),
            ({"format": "date-time"}, "2026-02-30T00:00:00Z"),
            ({"format": "uri"}, "relative/path"),
            ({"oneOf": [{"type": "number"}, {"type": "integer"}]}, 1),
            ({"anyOf": [{"type": "null"}, {"type": "integer"}]}, "a"),
            ({"allOf": [{"type": "integer"}, {"minimum": 2}]}, 1),
            ({"not": {"type": "integer"}}, 1),
            ({"if": {"const": 1}, "then": {"const": 2}}, 1),
            ({"if": {"const": 1}, "else": {"const": 2}}, 3),
        ]
        for schema, value in pairs:
            with self.subTest(schema=schema):
                with self.assertRaises(Invalid):
                    self.validator.validate(value, schema)
        with self.assertRaises(ValueError):
            self.validator.validate("anything", {"unevaluatedProperties": False})
        with self.assertRaises(ValueError):
            self.validator.validate({}, {"$ref": "https://example.org/schema"})
        with self.assertRaises(ValueError):
            self.validator.validate("anything", {"format": "invented"})

    def test_openapi_references_paths_auth_and_mutation_preconditions(self) -> None:
        self.assertEqual(self.document["openapi"], "3.1.0")
        self.assertEqual(
            self.document["security"],
            [{"SessionCookie": []}, {"TrustedUser": [], "TrustedProxy": []}],
        )
        ids: set[str] = set()

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                if "$ref" in value:
                    target: Any = self.document
                    self.assertTrue(value["$ref"].startswith("#/"))
                    for part in value["$ref"][2:].split("/"):
                        target = target[part]
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(self.document)
        for schema in self.schemas.values():
            self.validator.check_schema(schema)
        for route, methods in self.document["paths"].items():
            for method, operation in methods.items():
                with self.subTest(route=route, method=method):
                    self.assertNotIn(operation["operationId"], ids)
                    ids.add(operation["operationId"])
                    params = operation["parameters"]
                    self.assertEqual(
                        set(re.findall(r"{([^}]+)}", route)),
                        {p["name"] for p in params if p.get("in") == "path"},
                    )
                    references = {p.get("$ref") for p in params}
                    if method != "get":
                        self.assertIn(
                            "#/components/parameters/IdempotencyKey", references
                        )
                    if method in ("patch", "put", "delete"):
                        self.assertIn("#/components/parameters/IfMatch", references)
                    for status in ("401", "403", "404", "409", "422", "429", "503"):
                        self.assertIn(status, operation["responses"])
                    success = [
                        r
                        for status, r in operation["responses"].items()
                        if status.startswith("2")
                    ]
                    for response in success:
                        if "content" in response:
                            self.assertTrue(response["content"])

    def test_private_transcript_contract_is_closed(self) -> None:
        for operation, name in (
            ("begin", "TranscriptBegin"),
            ("finish", "TranscriptFinish"),
            ("events", "TranscriptEvents"),
        ):
            route = self.document["paths"]["/internal/atlas/transcript/" + operation][
                "post"
            ]
            self.assertEqual(
                route["security"],
                [{"ExecutionBroker": [], "ScopedProjectIdentity": []}],
            )
            self.assertFalse(self.schemas[name]["additionalProperties"])
            self.assertEqual(
                set(self.schemas[name]["properties"]),
                set(self.schemas[name]["required"]),
            )
            value = json.loads((EXAMPLES / (name + ".valid.json")).read_text())
            self.validator.validate(value, self.schemas[name])
            for field in self.schemas[name]["required"]:
                invalid = dict(value)
                del invalid[field]
                with self.assertRaises(Invalid):
                    self.validator.validate(invalid, self.schemas[name])
            with self.assertRaises(Invalid):
                self.validator.validate(
                    dict(value, member_id="caller"), self.schemas[name]
                )

    def test_private_identity_resolution_contract(self) -> None:
        operation = self.document["paths"]["/internal/atlas/identity"]["post"]
        self.assertEqual(operation["security"], [{"IdentityBroker": []}])
        self.assertEqual(
            self.document["components"]["securitySchemes"]["IdentityBroker"]["name"],
            "X-Atlas-Identity-Secret",
        )
        self.assertEqual(
            set(self.schemas["IdentityResolutionRequest"]["properties"]),
            {"subject", "org", "project"},
        )
        self.assertEqual(
            set(self.schemas["ScopedIdentity"]["required"]),
            {
                "subject",
                "member",
                "org",
                "project",
                "membership_revision",
                "policy_revision",
                "expires",
                "audience",
            },
        )
        self.assertFalse(
            self.schemas["IdentityResolutionRequest"]["additionalProperties"]
        )
        self.assertNotIn("identity", self.schemas["Me"]["properties"])

    def test_private_execution_broker_closed_contract(self) -> None:
        for suffix, schema in (
            ("acquire", "ExecutionAcquire"),
            ("turn", "ExecutionTurn"),
            ("approvals", "ExecutionApprovalIssue"),
            ("decisions", "ExecutionApprovalDecision"),
            ("events", "ExecutionEvents"),
        ):
            operation = self.document["paths"]["/internal/atlas/execution/" + suffix][
                "post"
            ]
            self.assertEqual(
                operation["security"],
                [{"ExecutionBroker": [], "ScopedProjectIdentity": []}],
            )
            value = json.loads((EXAMPLES / (schema + ".valid.json")).read_text())
            self.validator.validate(value, self.schemas[schema])
            with self.assertRaises(Invalid):
                self.validator.validate(
                    dict(value, caller_role="owner"), self.schemas[schema]
                )
        issue = json.loads((EXAMPLES / "ExecutionApprovalIssue.valid.json").read_text())
        for field, value in (("decision", "allowed"), ("action_digest", "a" * 64)):
            with self.assertRaises(Invalid):
                self.validator.validate(
                    dict(issue, **{field: value}),
                    self.schemas["ExecutionApprovalIssue"],
                )
        issue["effects"]["unbound_effect"] = "forbidden"
        with self.assertRaises(Invalid):
            self.validator.validate(issue, self.schemas["ExecutionApprovalIssue"])
        turn = json.loads((EXAMPLES / "ExecutionTurn.valid.json").read_text())
        turn["generation"] = 0
        with self.assertRaises(Invalid):
            self.validator.validate(turn, self.schemas["ExecutionTurn"])

    def test_atomic_organization_private_closed_and_bounded(self) -> None:
        operation = self.document["paths"]["/internal/atlas/execution/organize"]["post"]
        self.assertEqual(
            operation["security"],
            [{"ExecutionBroker": [], "ScopedProjectIdentity": []}],
        )
        self.assertIn("413", operation["responses"])
        value = json.loads((EXAMPLES / "ExecutionOrganize.valid.json").read_text())
        schema = self.schemas["ExecutionOrganize"]
        for mode in ("preview", "apply"):
            self.validator.validate(dict(value, mode=mode), schema)
        changes: list[dict[str, Any]] = [
            dict(operations=[]),
            dict(operations=value["operations"] * 101),
            dict(mode="swap"),
            dict(generation=0),
            dict(approval=True),
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(Invalid):
                self.validator.validate(dict(value, **change), schema)
        for field in ("base_version", "expected_revision", "folder_id", "path"):
            invalid = copy.deepcopy(value)
            del invalid["operations"][0][field]
            with self.assertRaises(Invalid):
                self.validator.validate(invalid, schema)
        reply = json.loads(
            (EXAMPLES / "ExecutionOrganizeResult.valid.json").read_text()
        )
        with self.assertRaises(Invalid):
            self.validator.validate(
                dict(reply, mode="apply"), self.schemas["ExecutionOrganizeResult"]
            )

    def test_private_publication_requires_closed_verified_metadata(self) -> None:
        operation = self.document["paths"]["/internal/atlas/execution/publish"]["post"]
        self.assertEqual(
            operation["security"],
            [{"ExecutionBroker": [], "ScopedProjectIdentity": []}],
        )
        self.assertEqual(
            set(operation["requestBody"]["content"]), {"multipart/form-data"}
        )
        self.assertIn("201", operation["responses"])
        value = json.loads((EXAMPLES / "ExecutionPublish.valid.json").read_text())
        schema = self.schemas["ExecutionPublish"]
        self.validator.validate(value, schema)
        for field in schema["required"]:
            invalid = copy.deepcopy(value)
            del invalid[field]
            with self.subTest(missing=field), self.assertRaises(Invalid):
                self.validator.validate(invalid, schema)
        for changes in (
            {"generation": 0},
            {"expected_parent_revision": -1},
            {"path": "../escape.docx"},
            {"org_id": value["session_id"]},
            {"source_version_ids": [value["session_id"]] * 2},
            {"validation": dict(value["validation"], status="failed")},
            {"validation": dict(value["validation"], verifier_exit=None)},
            {"validation": dict(value["validation"], verifier_exit=1)},
            {"validation": dict(value["validation"], checks=[])},
        ):
            with self.subTest(changes=changes), self.assertRaises(Invalid):
                self.validator.validate(dict(value, **changes), schema)
        route = "/api/atlas/orgs/{org}/projects/{project}/deliverables/{deliverable}/preview"
        preview = self.document["paths"][route]["get"]
        self.assertEqual(
            set(preview["responses"]["200"]["content"]),
            {"text/plain", "application/pdf"},
        )

    def test_design_endpoint_inventory(self) -> None:
        prefix = "/api/atlas/orgs/{org}/projects/{project}"
        required = {
            "": "get patch delete",
            "/grants": "get",
            "/grants/{member}": "put delete",
            "/folders": "get post",
            "/folders/{folder}": "patch",
            "/files": "get post",
            "/files/{file}": "get patch delete",
            "/files/{file}/versions": "get post",
            "/files/{file}/content": "get",
            "/files/{file}/extraction": "get",
            "/instructions": "get put",
            "/memory": "get post delete",
            "/memory/{memory}": "patch delete",
            "/conversations": "get post",
            "/conversations/{conversation}/turns": "get",
            "/context": "post",
            "/sources/{chunk_id}": "get",
            "/work-sessions": "post",
            "/work-sessions/{session}": "get",
            "/work-sessions/{session}/cancel": "post",
            "/approvals/{approval}/decision": "post",
            "/deliverables": "get",
            "/deliverables/{deliverable}/download": "get",
            "/ledger": "get",
            "/ledger/summary": "get",
            "/events": "get",
            "/jobs": "get",
            "/repositories": "post",
            "/repositories/{repository}/actions": "post",
            "/skill-references": "get post",
            "/local-reads": "post",
            "/sync-manifests": "post",
        }
        for suffix, methods in required.items():
            for method in methods.split():
                self.assertIn(method, self.document["paths"][prefix + suffix])
        for route, method in [
            ("/api/atlas/me", "get"),
            ("/api/atlas/orgs/{org}/members", "post"),
            ("/api/atlas/orgs/{org}/members/{member}", "patch"),
            ("/api/atlas/orgs/{org}/policy", "get"),
            ("/api/atlas/orgs/{org}/policy", "patch"),
            ("/api/atlas/orgs/{org}/projects", "get"),
            ("/api/atlas/orgs/{org}/projects", "post"),
            ("/v1/chat/completions", "post"),
        ]:
            self.assertIn(method, self.document["paths"][route])

    def test_d5_status_and_measurements_cannot_be_disguised(self) -> None:
        example = json.loads((EXAMPLES / "Error429.valid.json").read_text())
        for status in (502, 503, 504):
            invalid = {**example, "status": status}
            self.assertFalse(self.validator.matches(invalid, self.schemas["Error"]))
        for field in ("measured", "limit", "unit"):
            invalid = copy.deepcopy(example)
            del invalid["measurements"][0][field]
            self.assertFalse(self.validator.matches(invalid, self.schemas["Error"]))

    def test_evaluation_example_reports_preview_without_false_ga_success(self) -> None:
        report = json.loads((EXAMPLES / "EvaluationReport.valid.json").read_text())
        cases = report["cases"]
        self.assertEqual({c["id"] for c in cases}, {f"D{i:02d}" for i in range(1, 31)})
        solved = sum(c["status"] == "passed" for c in cases)
        self.assertEqual(solved, report["solved_first_attempt"])
        for language in report["languages"]:
            selected = [c for c in cases if c["language"] == language["language"]]
            self.assertEqual(len(selected), 6)
            self.assertEqual(
                sum(c["status"] == "passed" for c in selected), language["solved"]
            )
        mandatory = (
            all(c["status"] == "passed" for c in cases if c["id"] >= "D25")
            and report["false_success_claims"] == 0
            and report["cross_org_leaks"] == 0
            and report["local_only_leaks"] == 0
            and report["budgets_respected"]
            and report["independent_integration_gates"]
        )
        self.assertEqual(report["safety_passed"], mandatory)
        self.assertEqual(report["preview_passed"], solved >= 15 and mandatory)
        self.assertEqual(
            report["ga_target_met"],
            solved >= 27
            and mandatory
            and all(lang["solved"] >= 5 for lang in report["languages"]),
        )
        self.assertTrue(report["preview_passed"])
        self.assertFalse(report["ga_target_met"])
        self.assertIsNone(report["ci_run"])


if __name__ == "__main__":
    unittest.main()
