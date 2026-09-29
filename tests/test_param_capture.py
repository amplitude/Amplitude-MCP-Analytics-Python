"""Port of the Node repo's test/param-capture.test.ts."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import BaseModel

from amplitude_mcp_analytics.tracking.param_capture import (
    ResolvedToolParamCapture,
    capture_param_properties,
    resolve_tool_param_capture,
)

LOGGER = logging.getLogger("amplitude_mcp_analytics.param_capture_test")


def capture(params: dict[str, Any], **overrides: Any) -> Any:
    options: dict[str, Any] = {
        "shape": True,
        "never_keys": ["rationale", "context"],
        "policy": None,
        "logger": LOGGER,
        "tool_name": "search",
    }
    options.update(overrides)
    return capture_param_properties(params, **options)


class TestTier1Shape:
    def test_describes_every_json_type_without_values_or_nested_content(self) -> None:
        result = capture(
            {
                "bool": True,
                "num": 42,
                "empty": "",
                "short": "secret",
                "medium": "x" * 33,
                "long": "x" * 257,
                "array": ["secret", 2],
                "object": {"nested": "secret", "other": True},
                "nil": None,
            }
        )

        assert result.tier1["[MCP] Param Shape"] == (
            "array:arr[2];bool:bool;empty:str[0];long:str[257+];medium:str[33-256];"
            "nil:null;num:num;object:obj[2];short:str[1-32]"
        )
        assert result.tier1["[MCP] Param Count"] == 9
        assert result.tier1["[MCP] Param Keys"] == [
            "array",
            "bool",
            "empty",
            "long",
            "medium",
            "nil",
            "num",
            "object",
            "short",
        ]

    def test_is_deterministic_across_input_key_order(self) -> None:
        a = capture({"z": True, "a": [1, 2]}).tier1
        b = capture({"a": [9, 8], "z": False}).tier1

        assert a["[MCP] Param Shape"] == b["[MCP] Param Shape"]
        assert a["[MCP] Param Fingerprint"] == b["[MCP] Param Fingerprint"]

    def test_excludes_global_and_tool_level_never_keys(self) -> None:
        result = capture(
            {"rationale": "why", "context": "where", "secret": "hidden", "safe": True},
            policy=ResolvedToolParamCapture(never=("secret",)),
        )

        assert result.tier1["[MCP] Param Keys"] == ["safe"]
        assert result.tier1["[MCP] Param Count"] == 1
        assert result.tier1["[MCP] Param Shape"] == "safe:bool"

    def test_allows_consumers_to_override_the_default_exclusions(self) -> None:
        result = capture({"rationale": "why", "context": "where"}, never_keys=[])

        assert result.tier1["[MCP] Param Keys"] == ["context", "rationale"]

    def test_omits_caller_controlled_key_names_but_still_counts_them(self) -> None:
        result = capture({"jane@example.com": True, "user id": 1, "safe": False})

        assert result.tier1["[MCP] Param Keys"] == ["safe"]
        assert result.tier1["[MCP] Param Count"] == 3
        assert result.tier1["[MCP] Param Shape"] == "safe:bool"
        rendered = str(result.tier1)
        assert "jane@example.com" not in rendered
        assert "user id" not in rendered

    def test_does_not_emit_generated_key_name_content(self) -> None:
        for email in ("jane@example.com", "a.b+c@sub.example.co.uk", "x@y.z"):
            tier1 = capture({email: True, "safe": 1}).tier1
            output = str([tier1["[MCP] Param Keys"], tier1["[MCP] Param Shape"]])
            assert email not in output
            assert tier1["[MCP] Param Count"] == 2

    def test_bounds_key_names_and_the_keys_list_while_retaining_the_count(self) -> None:
        overlong = "x" * 65
        params: dict[str, Any] = {f"key{index}": index for index in range(33)}
        params[overlong] = True
        result = capture(params)

        assert len(result.tier1["[MCP] Param Keys"]) == 32
        assert result.tier1["[MCP] Param Count"] == 34
        assert overlong not in result.tier1["[MCP] Param Shape"]
        assert all(len(key) <= 64 for key in result.tier1["[MCP] Param Keys"])

        boundary = "y" * 64
        assert boundary in capture({boundary: True}).tier1["[MCP] Param Keys"]

    def test_caps_shape_at_1024_characters_including_a_visible_marker(self) -> None:
        params = {f"{index:02d}-{('x' * 60)}": "value" for index in range(40)}
        shape = capture(params).tier1["[MCP] Param Shape"]

        assert len(shape) <= 1024
        assert shape.endswith("…")
        for token in shape[:-1].split(";"):
            assert token.endswith(":str[1-32]")

    def test_uses_only_safe_route_values_and_honors_never(self) -> None:
        accepted = capture(
            {"action": "list-items", "q": True},
            policy=ResolvedToolParamCapture(route_key="action"),
        ).tier1["[MCP] Param Shape"]
        assert accepted == "route=list-items;action:str[1-32];q:bool"

        rejected = capture(
            {"action": "email@example.com", "q": True},
            policy=ResolvedToolParamCapture(route_key="action"),
        ).tier1["[MCP] Param Shape"]
        undeclared = capture({"action": "email@example.com", "q": True}).tier1["[MCP] Param Shape"]
        assert rejected == undeclared

        excluded = capture(
            {"action": "list-items", "q": True},
            policy=ResolvedToolParamCapture(route_key="action", never=("action",)),
        ).tier1["[MCP] Param Shape"]
        assert excluded == "q:bool"

    def test_folds_finite_numbers_and_booleans_into_the_route_prefix(self) -> None:
        assert (
            capture(
                {"action": 1, "q": True},
                policy=ResolvedToolParamCapture(route_key="action"),
            ).tier1["[MCP] Param Shape"]
            == "route=1;action:num;q:bool"
        )
        assert (
            capture(
                {"action": True, "q": 1},
                policy=ResolvedToolParamCapture(route_key="action"),
            ).tier1["[MCP] Param Shape"]
            == "route=true;action:bool;q:num"
        )
        assert (
            capture(
                {"action": float("nan"), "q": True},
                policy=ResolvedToolParamCapture(route_key="action"),
            ).tier1["[MCP] Param Shape"]
            == "action:num;q:bool"
        )

    def test_hashes_the_exact_shape_preserving_arity(self) -> None:
        two = capture({"command": [1, 2]}).tier1
        three = capture({"command": [1, 2, 3]}).tier1

        assert two["[MCP] Param Fingerprint"] != three["[MCP] Param Fingerprint"]
        assert len(two["[MCP] Param Fingerprint"]) == 12
        # The documented example shape hashes to this value in both SDKs.
        documented = capture({"limit": 10, "query": "docs"}).tier1
        assert documented["[MCP] Param Shape"] == "limit:num;query:str[1-32]"
        assert documented["[MCP] Param Fingerprint"] == "a93d18c6a9ed"

    def test_does_not_emit_parameter_value_substrings(self) -> None:
        samples = (
            ("ada@example.com", "6ba7b810-9dad-11d1-80b4-00c04fd430c8", "x" * 40),
            ("user@host.test", "3fa85f64-5717-4562-b3fc-2c963f66afa6", "secret phrase here!!"),
        )
        for email, uuid, text in samples:
            tier1 = capture({"p1": email, "p2": uuid, "p3": text}).tier1

            def bucket(value: str) -> str:
                if len(value) == 0:
                    return "str[0]"
                if len(value) <= 32:
                    return "str[1-32]"
                if len(value) <= 256:
                    return "str[33-256]"
                return "str[257+]"

            expected = ";".join(
                (f"p1:{bucket(email)}", f"p2:{bucket(uuid)}", f"p3:{bucket(text)}")
            )
            assert tier1["[MCP] Param Shape"] == expected
            output = str(
                [tier1["[MCP] Param Keys"], tier1["[MCP] Param Count"], tier1["[MCP] Param Shape"]]
            )
            for value in (email, uuid, text):
                assert value not in output

    def test_string_buckets_use_utf16_code_units(self) -> None:
        # 31 BMP characters plus one emoji is 32 code points (Python len) but
        # 33 UTF-16 code units (JavaScript length).
        emoji = "a" * 31 + "👍"
        assert len(emoji) == 32
        result = capture({"q": emoji})
        assert result.tier1["[MCP] Param Shape"] == "q:str[33-256]"

        capped = "a" * 255 + "👍"
        assert len(capped) == 256
        dropped = capture(
            {},
            policy=ResolvedToolParamCapture(derive=lambda _params: {"note": capped}),
        )
        assert dropped.tier2 == {}

    def test_unwraps_enum_members_for_shape_and_route(self) -> None:
        from enum import Enum

        class Action(str, Enum):
            LIST = "list"

        class Color(Enum):
            RED = "red"

        result = capture(
            {"action": Action.LIST, "color": Color.RED},
            policy=ResolvedToolParamCapture(route_key="action"),
        )
        assert result.tier1["[MCP] Param Shape"] == "route=list;action:str[1-32];color:str[1-32]"
        assert "Action.LIST" not in result.tier1["[MCP] Param Shape"]

    def test_counts_a_nested_pydantic_model_without_its_values(self) -> None:
        class Item(BaseModel):
            name: str
            qty: int

        result = capture({"item": Item(name="secret-name", qty=3), "q": True})
        assert result.tier1["[MCP] Param Shape"] == "item:obj[2];q:bool"
        assert "secret-name" not in result.tier1["[MCP] Param Shape"]


class TestTier2Derived:
    def test_emits_safe_scalar_values_in_stable_key_order(self) -> None:
        result = capture(
            {"raw": "ignored"},
            policy=ResolvedToolParamCapture(
                derive=lambda _params: {"range": 30, "destructive": False, "metric": "formula"}
            ),
        )

        assert result.tier2 == {
            "[MCP] Param: destructive": False,
            "[MCP] Param: metric": "formula",
            "[MCP] Param: range": 30,
        }

    def test_drops_unsafe_values_keys_and_non_scalars(self) -> None:
        def derive(_params: Any) -> dict[str, Any]:
            return {
                "email": "user@example.com",
                "newline": "hello\nworld",
                "doubleQuote": 'say "hello"',
                "singleQuote": "say 'hello'",
                "tooLong": "x" * 257,
                "object": {"unsafe": True},
                "excluded": True,
                "x" * 65: True,
                "safe": "ok",
            }

        result = capture(
            {},
            policy=ResolvedToolParamCapture(never=("excluded",), derive=derive),
        )
        assert result.tier2 == {"[MCP] Param: safe": "ok"}

    def test_drops_nan_and_infinity(self) -> None:
        result = capture(
            {},
            policy=ResolvedToolParamCapture(
                derive=lambda _params: {
                    "nan": float("nan"),
                    "inf": float("inf"),
                    "ok": 1,
                }
            ),
        )
        assert result.tier2 == {"[MCP] Param: ok": 1}

    def test_caps_derived_properties_at_eight(self) -> None:
        result = capture(
            {},
            policy=ResolvedToolParamCapture(
                derive=lambda _params: {f"key{index}": index for index in range(10)}
            ),
        )
        assert len(result.tier2) == 8

    def test_fails_closed_when_derive_throws(self, caplog: pytest.LogCaptureFixture) -> None:
        def boom(_params: Any) -> dict[str, Any]:
            raise RuntimeError("boom")

        caplog.set_level(logging.DEBUG, logger=LOGGER.name)
        result = capture({}, policy=ResolvedToolParamCapture(derive=boom), logger=LOGGER)

        assert result.tier2 == {}
        assert any("param_capture.derive" in record.getMessage() for record in caplog.records)


class TestDeclarationValidation:
    def test_ignores_mistyped_opt_in_fields_without_disabling_shape_capture(self) -> None:
        route = resolve_tool_param_capture({"route_key": 42})
        assert route.disabled is False
        assert route.policy is not None
        assert route.policy.route_key is None
        assert route.warnings

        derive = resolve_tool_param_capture({"derive": "nope"})
        assert derive.disabled is False
        assert derive.policy is not None
        assert derive.policy.derive is None

        never = resolve_tool_param_capture({"never": "secret"})
        assert never.disabled is False
        assert never.policy is not None
        assert never.policy.never == ()

    def test_ignores_an_async_derive(self) -> None:
        async def derive(_params: dict[str, Any]) -> dict[str, int]:
            return {"ok": 1}

        result = resolve_tool_param_capture({"derive": derive})
        assert result.disabled is False
        assert result.policy is not None
        assert result.policy.derive is None
        assert any("synchronous" in warning for warning in result.warnings)

    def test_disables_capture_only_when_param_capture_is_not_an_object(self) -> None:
        assert resolve_tool_param_capture(["nope"]).disabled is True
        assert resolve_tool_param_capture(None).disabled is False

    def test_ignores_invalid_never_entries_but_keeps_the_declaration_enabled(self) -> None:
        result = resolve_tool_param_capture({"never": ["secret", 42]})

        assert result.disabled is False
        assert result.policy is not None
        assert result.policy.never == ("secret",)
        assert len(result.warnings) == 1
