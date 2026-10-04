"""工具协议、schema 生成与注册表执行语义。"""

from __future__ import annotations

from typing import Annotated

import pytest

from agentkit.tools.base import ToolContext, ToolSpec, build_args_model
from agentkit.tools.builtin import default_registry, register_builtin_tools
from agentkit.tools.registry import ToolRegistry


class TestBuildArgsModel:
    def test_generates_schema_from_signature(self):
        async def sample(ctx, file_path: Annotated[str, "文件路径"], count: int = 3) -> str:
            return ""

        model = build_args_model(sample)
        schema = model.model_json_schema()

        assert set(schema["properties"]) == {"file_path", "count"}
        assert schema["properties"]["file_path"]["description"] == "文件路径"
        assert schema["properties"]["file_path"]["type"] == "string"
        assert schema["properties"]["count"]["type"] == "integer"
        assert schema["properties"]["count"]["default"] == 3
        assert schema["required"] == ["file_path"]

    def test_context_parameter_is_excluded(self):
        async def sample(ctx: ToolContext, value: str) -> str:
            return ""

        assert set(build_args_model(sample).model_fields) == {"value"}

    def test_annotated_is_resolved_under_future_annotations(self):
        """模块里有 ``from __future__ import annotations`` 时注解是字符串，必须能解析。"""

        async def sample(ctx, path: Annotated[str, "路径"], flag: bool = False) -> str:
            return ""

        schema = build_args_model(sample).model_json_schema()
        assert schema["properties"]["path"]["type"] == "string"
        assert schema["properties"]["flag"]["type"] == "boolean"

    def test_varargs_rejected(self):
        async def sample(ctx, *args) -> str:  # noqa: ANN002
            return ""

        with pytest.raises(TypeError, match="可变参数"):
            build_args_model(sample)


class TestToolSpec:
    def test_description_falls_back_to_first_docstring_line(self):
        async def sample(ctx, x: str) -> str:
            """第一行描述。
            第二行不应该出现在 schema 里。
            """
            return ""

        spec = ToolSpec.from_function(sample)
        assert spec.description == "第一行描述。"

    def test_missing_description_is_an_error(self):
        async def sample(ctx, x: str) -> str:
            return ""

        with pytest.raises(ValueError, match="没有描述"):
            ToolSpec.from_function(sample)

    def test_sync_function_rejected(self):
        def sample(ctx, x: str) -> str:
            """描述"""
            return ""

        with pytest.raises(TypeError, match="必须是 async"):
            ToolSpec.from_function(sample)

    def test_dangerous_and_idempotent_flags(self):
        async def sample(ctx, x: str) -> str:
            """描述"""
            return ""

        spec = ToolSpec.from_function(sample, dangerous=True, idempotent=False)
        assert spec.dangerous is True
        assert spec.idempotent is False


class TestValidationErrorMessages:
    def test_names_the_offending_field(self):
        """错误信息会回灌给模型——不指名字段它就没法修正。"""

        async def sample(ctx, count: int) -> str:
            """描述"""
            return ""

        spec = ToolSpec.from_function(sample)
        with pytest.raises(Exception) as info:
            spec.validate({"count": "不是数字"})
        assert "count" in str(info.value)

    def test_reports_missing_required_field(self):
        async def sample(ctx, needed: str) -> str:
            """描述"""
            return ""

        spec = ToolSpec.from_function(sample)
        with pytest.raises(Exception) as info:
            spec.validate({})
        assert "needed" in str(info.value)


class TestRegistry:
    async def test_unknown_tool_returns_error_result(self, registry, ctx):
        registry.register(echo)
        result = await registry.invoke("nope", {}, ctx)
        assert result.is_error
        assert "不存在" in result.content
        # 错误信息里列出可用工具，模型才知道下一步该调谁
        assert "echo" in result.content

    async def test_validation_failure_is_reflected_not_raised(self, registry, ctx):
        """参数校验失败必须回灌成结果块，而不是抛异常中断整个 run。"""
        registry.register(echo)
        result = await registry.invoke("echo", {"text": 123}, ctx)
        assert result.is_error
        assert "text" in result.content

    async def test_execution_error_is_reflected(self, registry, ctx):
        registry.register(boom)
        result = await registry.invoke("boom", {"text": "x"}, ctx)
        assert result.is_error
        assert "RuntimeError" in result.content

    async def test_successful_call(self, registry, ctx):
        registry.register(echo)
        result = await registry.invoke("echo", {"text": "你好"}, ctx, tool_use_id="c1")
        assert not result.is_error
        assert result.content == "你好"
        assert result.tool_use_id == "c1"

    async def test_non_string_return_is_reported_as_defect(self, registry, ctx):
        async def returns_int(ctx, text: str) -> int:
            """返回非字符串的工具"""
            return 42

        registry.register(returns_int)
        result = await registry.invoke("returns_int", {"text": "x"}, ctx)
        assert result.is_error
        assert "缺陷" in result.content

    async def test_duplicate_registration_raises(self, registry):
        registry.register(echo)
        with pytest.raises(ValueError, match="重复"):
            registry.register(echo)

    async def test_merge_does_not_mutate_original(self, registry):
        registry.register(echo)
        other = ToolRegistry()
        other.register(boom)

        merged = registry.merge(other)
        assert set(merged.names()) == {"echo", "boom"}
        assert set(registry.names()) == {"echo"}

    def test_schemas_are_openai_ready(self, registry):
        registry.register(echo)
        schema = registry.schemas()[0]
        assert schema.name == "echo"
        assert schema.parameters["type"] == "object"

    def test_describe_lists_names(self, registry):
        registry.register(echo)
        assert "echo" in registry.describe()


class TestBuiltinTools:
    def test_default_registry_has_all_groups(self):
        assert set(default_registry().names()) == {
            "read_file",
            "write_file",
            "list_directory",
            "search_in_file",
            "find_files",
            "run_command",
        }

    def test_group_selection(self):
        """只读场景可以只要 fs + search，把 shell 排除掉。"""
        registry = register_builtin_tools(groups=["fs", "search"])
        assert "run_command" not in registry
        assert "read_file" in registry

    def test_unknown_group_raises(self):
        with pytest.raises(ValueError, match="未知的工具分组"):
            register_builtin_tools(groups=["nope"])

    def test_write_tools_marked_dangerous(self):
        registry = default_registry()
        assert registry.get("write_file").dangerous is True
        assert registry.get("run_command").dangerous is True
        assert registry.get("run_command").idempotent is False
        assert registry.get("read_file").dangerous is False


class TestFsTools:
    async def test_read_file(self, ctx):
        result = await default_registry().invoke("read_file", {"file_path": "hello.txt"}, ctx)
        assert not result.is_error
        assert "第一行" in result.content

    async def test_read_file_outside_workspace_is_reflected(self, ctx):
        result = await default_registry().invoke(
            "read_file", {"file_path": "../secret.txt"}, ctx
        )
        assert result.is_error
        assert "越出工作区" in result.content

    async def test_read_missing_file(self, ctx):
        result = await default_registry().invoke("read_file", {"file_path": "nope.txt"}, ctx)
        assert not result.is_error  # 业务性失败，不是工具故障
        assert "不存在" in result.content

    async def test_write_then_read(self, ctx):
        registry = default_registry()
        written = await registry.invoke(
            "write_file", {"file_path": "new/out.txt", "content": "内容\n"}, ctx
        )
        assert not written.is_error
        read = await registry.invoke("read_file", {"file_path": "new/out.txt"}, ctx)
        assert "内容" in read.content

    async def test_write_outside_workspace_blocked(self, ctx):
        result = await default_registry().invoke(
            "write_file", {"file_path": "../evil.txt", "content": "x"}, ctx
        )
        assert result.is_error
        assert "越出工作区" in result.content

    async def test_list_directory(self, ctx):
        result = await default_registry().invoke("list_directory", {"path": "."}, ctx)
        assert "hello.txt" in result.content
        assert "sub" in result.content

    async def test_list_recursive_skips_noise_dirs(self, ctx):
        (ctx.workspace / ".venv").mkdir()
        (ctx.workspace / ".venv" / "junk.py").write_text("x", encoding="utf-8")
        result = await default_registry().invoke(
            "list_directory", {"path": ".", "recursive": True}, ctx
        )
        assert "junk.py" not in result.content

    async def test_search_in_file(self, ctx):
        result = await default_registry().invoke(
            "search_in_file", {"pattern": "第二行", "file_path": "hello.txt"}, ctx
        )
        assert "1 处命中" in result.content
        assert "第二行" in result.content

    async def test_search_literal_mode_handles_regex_chars(self, ctx):
        (ctx.workspace / "code.txt").write_text("a.b\naxb\n", encoding="utf-8")
        registry = default_registry()

        as_regex = await registry.invoke(
            "search_in_file", {"pattern": "a.b", "file_path": "code.txt"}, ctx
        )
        assert "2 处命中" in as_regex.content

        as_literal = await registry.invoke(
            "search_in_file",
            {"pattern": "a.b", "file_path": "code.txt", "literal": True},
            ctx,
        )
        assert "1 处命中" in as_literal.content

    async def test_search_invalid_regex_suggests_literal(self, ctx):
        result = await default_registry().invoke(
            "search_in_file", {"pattern": "([", "file_path": "hello.txt"}, ctx
        )
        assert result.is_error
        assert "literal" in result.content

    async def test_find_files(self, ctx):
        result = await default_registry().invoke("find_files", {"pattern": "*.txt"}, ctx)
        assert "hello.txt" in result.content
        assert "nested.txt" in result.content


class TestShellTool:
    async def test_allowed_command_runs(self, ctx):
        result = await default_registry().invoke(
            "run_command", {"command": "echo hello"}, ctx
        )
        assert not result.is_error
        assert "hello" in result.content

    async def test_denied_command_is_reflected(self, ctx):
        result = await default_registry().invoke(
            "run_command", {"command": "shutdown -h now"}, ctx
        )
        assert result.is_error
        assert "破坏性" in result.content

    async def test_command_needing_approval_is_refused_without_a_channel(
        self, ctx, workspace
    ):
        """没有审批通道时**默认拒绝**，不是默认放行。

        这是刻意的安全默认值：没人看着的时候，让 agent 自主执行写操作或命令，
        出了事没人能及时拦。
        """
        no_channel = ToolContext(workspace=workspace, approver=None)
        result = await default_registry().invoke(
            "run_command", {"command": "frobnicate --all"}, no_channel
        )
        assert result.is_error
        assert "未经批准" in result.content

    async def test_approved_command_runs(self, workspace):
        """有审批通道且批准时，命令照常执行。"""
        from agentkit.runtime.approval import AutoApprover

        permitted = ToolContext(workspace=workspace, approver=AutoApprover())
        result = await default_registry().invoke(
            "run_command", {"command": "frobnicate --all"}, permitted
        )
        # 批准了，但命令本身不存在，所以是执行失败而不是被拒绝
        assert "未经批准" not in result.content

    async def test_denied_command_reports_the_denial(self, workspace):
        from agentkit.runtime.approval import DenyApprover

        denied = ToolContext(workspace=workspace, approver=DenyApprover("测试拒绝"))
        result = await default_registry().invoke(
            "run_command", {"command": "frobnicate --all"}, denied
        )
        assert result.is_error
        assert "未经批准" in result.content


# ---------------------------------------------------------------- 测试用工具


async def echo(ctx, text: Annotated[str, "要回显的文本"]) -> str:
    """把输入原样返回。"""
    return text


async def boom(ctx, text: str) -> str:
    """总是抛异常。"""
    raise RuntimeError("炸了")
