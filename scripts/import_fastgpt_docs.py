"""Import cleaned official website documentation into FastGPT dataset."""

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def chunk_markdown(content: str, max_chunk_len: int = 1000) -> list[tuple[str, str]]:
    """Split markdown into clean, heading-based (q, a) chunks without frontmatter."""
    # 1. Strip YAML frontmatter
    text = content.strip()
    title = ""
    tags: list[str] = []
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm_text = parts[1]
            text = parts[2].strip()
            # Extract title and tags from frontmatter if available
            for fm_line in fm_text.splitlines():
                if fm_line.startswith("title:"):
                    title = fm_line.split("title:", 1)[1].strip().strip("'\"")
                elif fm_line.strip().startswith("- "):
                    tags.append(fm_line.strip()[2:].strip())

    lines = text.splitlines()
    if not title:
        for line in lines:
            if line.startswith("# "):
                title = line[2:].strip()
                break

    # If document is relatively short (e.g. FAQ or short announcement), keep it intact
    clean_body = "\n".join(line for line in lines if not line.startswith("# ")).strip()
    if len(clean_body) <= max_chunk_len:
        q_text = title or "知识文档"
        if tags:
            q_text = f"{q_text} (标签: {', '.join(tags[:4])})"
        return [(q_text, clean_body)]

    # For longer documents, split by ## headings
    sections: list[tuple[str, list[str]]] = []
    current_heading = title or "概述"
    current_lines: list[str] = []

    for line in lines:
        if line.startswith("# "):
            continue
        if line.startswith("## "):
            if current_lines:
                sections.append((current_heading, current_lines))
                current_lines = []
            current_heading = f"{title} - {line[3:].strip()}" if title else line[3:].strip()
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_heading, current_lines))

    chunks: list[tuple[str, str]] = []
    for heading, sec_lines in sections:
        sec_text = "\n".join(sec_lines).strip()
        if not sec_text:
            continue
        chunks.append((heading, sec_text))

    return chunks if chunks else [(title or "文档内容", clean_body)]


class FastGptImporter:
    """Manages dataset creation and document batch import in FastGPT."""

    def __init__(self, base_url: str, root_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.root_key = root_key
        self.client = httpx.Client(base_url=self.base_url, timeout=60.0)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.root_key}",
            "Content-Type": "application/json",
        }

    def get_or_create_dataset(self, name: str = "官方网站知识库", recreate: bool = False) -> str:
        """Find existing dataset by name or create a new one."""
        resp = self.client.post("/api/core/dataset/list", headers=self._headers(), json={})
        resp.raise_for_status()
        datasets = resp.json().get("data") or []
        for ds in datasets:
            if isinstance(ds, dict) and ds.get("name") == name:
                if recreate:
                    logger.info(
                        "Deleting existing dataset %s (%s) for fresh recreate...",
                        name,
                        ds["_id"],
                    )
                    try:
                        del_url = f"/api/core/dataset/delete?id={ds['_id']}"
                        self.client.delete(del_url, headers=self._headers())
                    except Exception as e:
                        logger.warning("Delete old dataset warning: %s", e)
                    break
                else:
                    logger.info("Found existing dataset %s (ID: %s)", name, ds["_id"])
                    return str(ds["_id"])

        # Create
        logger.info("Creating new dataset %s...", name)
        create_resp = self.client.post(
            "/api/core/dataset/create",
            headers=self._headers(),
            json={"name": name, "type": "dataset", "intro": "瀚思官网29篇清洗知识文档"},
        )
        create_resp.raise_for_status()
        data = create_resp.json()
        ds_id = str(data.get("data") or data.get("_id"))
        logger.info("Created dataset %s (ID: %s)", name, ds_id)
        return ds_id

    def import_directory(self, dataset_id: str, docs_dir: Path) -> dict[str, Any]:
        """Import all markdown files under docs_dir into the dataset."""
        md_files = sorted(docs_dir.glob("*.md"))
        logger.info("Found %d markdown documents in %s", len(md_files), docs_dir)

        total_chunks = 0
        imported_files = 0
        errors: list[str] = []

        for f in md_files:
            try:
                content = f.read_text(encoding="utf-8")
                chunks = chunk_markdown(content)
                if not chunks:
                    continue

                # 1. Create collection for this file
                col_resp = self.client.post(
                    "/api/core/dataset/collection/create",
                    headers=self._headers(),
                    json={
                        "datasetId": dataset_id,
                        "name": f.name,
                        "type": "virtual",
                    },
                )
                col_resp.raise_for_status()
                col_raw = col_resp.json().get("data")
                col_id = str(col_raw.get("_id")) if isinstance(col_raw, dict) else str(col_raw)

                # 2. Push chunks
                push_data = [{"q": q, "a": a} for q, a in chunks]
                push_resp = self.client.post(
                    "/api/core/dataset/data/pushData",
                    headers=self._headers(),
                    json={
                        "collectionId": col_id,
                        "trainingType": "chunk",
                        "data": push_data,
                    },
                )
                push_resp.raise_for_status()
                imported_files += 1
                total_chunks += len(chunks)
                logger.info("Imported %s (%d chunks)", f.name, len(chunks))
            except Exception as exc:
                err = f"Failed to import {f.name}: {exc}"
                logger.error(err)
                errors.append(err)

        return {
            "dataset_id": dataset_id,
            "total_files": len(md_files),
            "imported_files": imported_files,
            "total_chunks": total_chunks,
            "errors": errors,
        }


def _update_env_dataset_id(env_file: Path, new_id: str) -> None:
    if not env_file.exists():
        return
    text = env_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    found = False
    new_lines = []
    for line in lines:
        if line.startswith("FASTGPT_DATASET_ID=") or line.startswith("EA_FASTGPT_DATASET_ID="):
            new_lines.append(f"FASTGPT_DATASET_ID={new_id}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"FASTGPT_DATASET_ID={new_id}")
    env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    logger.info("Updated %s with FASTGPT_DATASET_ID=%s", env_file, new_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import markdown docs into FastGPT dataset")
    parser.add_argument(
        "--fastgpt-url",
        default="http://localhost:3001",
        help="FastGPT base URL (default: http://localhost:3001)",
    )
    parser.add_argument(
        "--api-key",
        "--root-key",
        dest="api_key",
        default=os.getenv("FASTGPT_ROOT_KEY") or os.getenv("FASTGPT_API_KEY", "fastgpt-local-enterprise-key"),
        help="FastGPT API_KEY or ROOT_KEY",
    )
    parser.add_argument(
        "--dataset-name",
        default="官方网站知识库",
        help="Target FastGPT dataset name",
    )
    parser.add_argument(
        "--docs-dir",
        default="knowledge/website_main",
        help="Path to markdown docs directory",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Recreate dataset cleanly from scratch",
    )
    args = parser.parse_args()

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    project_root = Path(__file__).resolve().parent.parent
    docs_dir = project_root / args.docs_dir

    if not docs_dir.is_dir():
        print(f"Error: Docs directory not found: {docs_dir}", file=sys.stderr)
        return 1

    api_key = args.api_key or os.getenv("FASTGPT_API_KEY") or "fastgpt-local-enterprise-key"
    importer = FastGptImporter(args.fastgpt_url, api_key)
    try:
        ds_id = importer.get_or_create_dataset(args.dataset_name, recreate=args.recreate)
        summary = importer.import_directory(ds_id, docs_dir)
        _update_env_dataset_id(project_root / ".env", ds_id)
        print("\nImport Summary:")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        logger.exception("Import failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
