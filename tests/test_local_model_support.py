import importlib.util
from importlib.machinery import SourceFileLoader
import io
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = REPO_ROOT / "scripts" / "sv-generate"


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChatOpenAI:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.messages = None
        self.__class__.instances.append(self)

    def invoke(self, messages):
        self.messages = messages
        return types.SimpleNamespace(
            content="[BEGIN]\nmodule TopModule(output zero); assign zero = 1'b0; endmodule\n[DONE]"
        )


class UnexpectedChatNVIDIA:
    def __init__(self, **_kwargs):
        raise AssertionError("local OpenAI-compatible models must not use ChatNVIDIA")


@contextmanager
def fake_openai_callback():
    yield types.SimpleNamespace(
        prompt_tokens=11,
        completion_tokens=7,
        total_tokens=18,
        total_cost=0,
    )


def load_generator():
    langchain = types.ModuleType("langchain")
    schema = types.ModuleType("langchain.schema")
    schema.SystemMessage = FakeMessage
    schema.HumanMessage = FakeMessage
    callbacks = types.ModuleType("langchain.callbacks")
    callbacks.get_openai_callback = fake_openai_callback

    langchain_openai = types.ModuleType("langchain_openai")
    langchain_openai.ChatOpenAI = FakeChatOpenAI
    langchain_nvidia = types.ModuleType("langchain_nvidia_ai_endpoints")
    langchain_nvidia.ChatNVIDIA = UnexpectedChatNVIDIA

    modules = {
        "langchain": langchain,
        "langchain.schema": schema,
        "langchain.callbacks": callbacks,
        "langchain_openai": langchain_openai,
        "langchain_nvidia_ai_endpoints": langchain_nvidia,
    }

    loader = SourceFileLoader("sv_generate_under_test", str(GENERATOR_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    bootstrap_argv = [str(GENERATOR_PATH), "--list-models", "unused_prompt.txt"]
    with patch.dict(sys.modules, modules), patch.object(
        sys, "argv", bootstrap_argv
    ), redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


class LocalModelSupportTests(unittest.TestCase):
    def setUp(self):
        FakeChatOpenAI.instances.clear()

    def test_qwen_is_listed_as_openai_compatible(self):
        generator = load_generator()
        argv = [str(GENERATOR_PATH), "--list-models", "unused_prompt.txt"]

        stdout = io.StringIO()
        with patch.object(sys, "argv", argv), redirect_stdout(stdout):
            generator.main()

        self.assertIn("OpenAI-Compatible Models", stdout.getvalue())
        self.assertIn("qwen3.6-coder", stdout.getvalue())

    def generate(self, model="qwen3.6-coder", qwen_thinking="on"):
        generator = load_generator()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "Prob001_zero_prompt.txt"
            output = root / "Prob001_zero_sample01.sv"
            prompt.write_text("Implement TopModule with a constant-zero output.")
            argv = [
                str(GENERATOR_PATH),
                f"--model={model}",
                "--task=spec-to-rtl",
                "--temperature=0.6",
                "--top-p=0.95",
                "--max-tokens=8192",
                f"--qwen-thinking={qwen_thinking}",
                f"--output={output}",
                str(prompt),
            ]

            stdout = io.StringIO()
            with patch.object(sys, "argv", argv), redirect_stdout(stdout):
                generator.main()

            return stdout.getvalue(), output.read_text() if output.exists() else ""

    def test_qwen_uses_chat_openai_with_its_real_model_name(self):
        _stdout, output = self.generate()

        self.assertEqual(len(FakeChatOpenAI.instances), 1)
        client = FakeChatOpenAI.instances[0]
        self.assertEqual(client.kwargs["model"], "qwen3.6-coder")
        self.assertEqual(client.kwargs["max_tokens"], 8192)
        self.assertEqual(
            client.kwargs["extra_body"],
            {"chat_template_kwargs": {"enable_thinking": True}},
        )
        self.assertIn("module TopModule", output)

    def test_qwen_thinking_can_be_disabled_at_request_level(self):
        self.generate(qwen_thinking="off")

        client = FakeChatOpenAI.instances[0]
        self.assertEqual(
            client.kwargs["extra_body"],
            {"chat_template_kwargs": {"enable_thinking": False}},
        )

    def test_qwen_thinking_off_is_rejected_for_other_models(self):
        stdout, _output = self.generate(model="gpt-4o", qwen_thinking="off")

        self.assertEqual(FakeChatOpenAI.instances, [])
        self.assertIn("--qwen-thinking=off requires a Qwen model", stdout)


if __name__ == "__main__":
    unittest.main()
