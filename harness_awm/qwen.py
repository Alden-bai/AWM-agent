"""Single-request Qwen smoke check using the same transport as the harness."""
import argparse
import asyncio
import json
import os
import uuid

from dotenv import load_dotenv

from .backend import Budget, api_request
from .contracts import ROOT, RunConfig
from .io import read_json


def load_qwen_config():
    load_dotenv(ROOT / ".env", override=False)
    return RunConfig.model_validate(read_json(ROOT / "configs/harness/qwen3.5-flash.json"))


async def ask(prompt, config):
    folder = ROOT / "runs" / "qwen_smoke" / uuid.uuid4().hex
    folder.mkdir(parents=True)
    budget = Budget(folder / "budget.sqlite", max_requests=1, max_tokens=128000)
    data = await api_request(
        config.actor_api,
        {"model": config.actor_api.model,
         "messages": [{"role": "user", "content": prompt}],
         "max_completion_tokens": config.max_output_tokens},
        budget, "smoke", "smoke", folder,
    )
    choice = data["choices"][0]
    print(choice["message"].get("content") or "")
    print(json.dumps({"usage": data.get("usage"), "finish_reason": choice.get("finish_reason"),
                      "evidence": str(folder)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Qwen3.5-Flash，关闭思考；默认只检查配置")
    parser.add_argument("prompt", nargs="?", help="提供问题时发送一次请求（会计费），不自动重试")
    parser.add_argument("--check", action="store_true", help="仅检查配置，不发送请求")
    args = parser.parse_args()
    config = load_qwen_config()
    key_present = bool(os.environ.get(config.actor_api.api_key_env, "").strip())
    if args.check or args.prompt is None:
        print(json.dumps({"model": config.actor_api.model, "base_url": config.actor_api.base_url,
                          "enable_thinking": config.actor_api.enable_thinking,
                          "context_limit": config.context_limit, "max_output_tokens": config.max_output_tokens,
                          "api_key_configured": key_present, "requests_sent": 0}, ensure_ascii=False, indent=2))
        return
    if not key_present:
        parser.error(f"请先在 {ROOT / '.env'} 填写 DASHSCOPE_API_KEY")
    try:
        asyncio.run(ask(args.prompt, config))
    except Exception as exc:
        # Never echo SDK exceptions that could contain credentials or headers.
        parser.exit(1, f"请求失败：{type(exc).__name__}；请检查 Key、地域、额度及请求长度。\n")


if __name__ == "__main__":
    main()
