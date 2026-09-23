"""Setup DeepSeek LLM and Ollama Embedding models in FastGPT's MongoDB."""

import json
import os
import subprocess
import sys

from dotenv import load_dotenv


def setup_models() -> int:
    load_dotenv()
    deepseek_key = os.getenv("EA_DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not deepseek_key:
        print("Error: DEEPSEEK_API_KEY not found in environment.", file=sys.stderr)
        return 1

    deepseek_base_url = (
        os.getenv("EA_DEEPSEEK_BASE_URL")
        or os.getenv("DEEPSEEK_BASE_URL")
        or "https://api.deepseek.com/v1"
    )

    llm_doc = {
        "_id": {"$oid": "6aab9d60747117636e8b39f1"},
        "scope": "system",
        "provider": "DeepSeek",
        "model": "deepseek-chat",
        "name": "DeepSeek-Chat",
        "type": "llm",
        "isActive": True,
        "requestUrl": deepseek_base_url.rstrip("/"),
        "requestAuth": deepseek_key,
        "config": {
            "maxContext": 64000,
            "maxResponse": 4000,
            "quoteMaxToken": 13000,
            "maxTemperature": 1.2,
            "censor": False,
            "vision": False,
            "functionCall": True,
            "toolChoice": True,
        },
    }

    emb_doc = {
        "_id": {"$oid": "6aab9d60747117636e8b39f2"},
        "scope": "system",
        "provider": "Ollama",
        "model": "bge-m3:latest",
        "name": "bge-m3",
        "type": "embedding",
        "isActive": True,
        "requestUrl": "http://host.docker.internal:11434/v1/embeddings",
        "requestAuth": "",
        "config": {
            "defaultToken": 512,
            "maxToken": 8192,
            "weight": 100,
            "hidden": False,
        },
    }

    llm_json = json.dumps(llm_doc)
    emb_json = json.dumps(emb_doc)

    js_lines = [
        f"var llmDoc = {llm_json};",
        "llmDoc._id = ObjectId(llmDoc._id['$oid']);",
        (
            "db.ai_models.replaceOne("
            "{ scope: 'system', model: llmDoc.model }, llmDoc, { upsert: true });"
        ),
        f"var embDoc = {emb_json};",
        "embDoc._id = ObjectId(embDoc._id['$oid']);",
        (
            "db.ai_models.replaceOne("
            "{ scope: 'system', model: embDoc.model }, embDoc, { upsert: true });"
        ),
        (
            "db.ai_default_models.updateOne({ scope: 'system' }, { $set: { "
            "defaultModelIds: { "
            "llm: '6aab9d60747117636e8b39f1', "
            "embedding: '6aab9d60747117636e8b39f2', "
            "datasetTextLLM: '6aab9d60747117636e8b39f1', "
            "chatTitleLLM: '6aab9d60747117636e8b39f1' "
            "}, catalogRevision: new Date().getTime() } }, { upsert: true });"
        ),
        "print('Models upserted successfully.');",
    ]
    script_content = "\n".join(js_lines)

    cmd = [
        "docker",
        "exec",
        "-i",
        "fastgpt-mongo",
        "mongo",
        "-u",
        "myusername",
        "-p",
        "mypassword",
        "--authenticationDatabase",
        "admin",
        "fastgpt",
    ]

    proc = subprocess.run(cmd, input=script_content, text=True, capture_output=True)
    if proc.returncode != 0:
        print(f"Failed to execute Mongo commands: {proc.stderr}", file=sys.stderr)
        return 1

    print(proc.stdout.strip())

    print("Restarting fastgpt-app container to reload models...")
    restart_proc = subprocess.run(
        ["docker", "restart", "fastgpt-app"], text=True, capture_output=True
    )
    if restart_proc.returncode != 0:
        print(f"Failed to restart fastgpt-app: {restart_proc.stderr}", file=sys.stderr)
        return 1

    print("fastgpt-app restarted successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(setup_models())
