"""Web-search concern: one endpoint across the TinyFish/Tavily/Serper providers."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from web_app import common

logger = logging.getLogger("chatbot.sidecar")

router = APIRouter()


class SearchRequest(BaseModel):
    query: str
    key: str | None = None


def _clean_key(value: str | None) -> str:
    key = (value or "").strip().strip("'\"")
    if "=" in key and key.split("=", 1)[0].strip().endswith("API_KEY"):
        key = key.split("=", 1)[1].strip().strip("'\"")
    return key.split()[0] if key.split() else ""


def _resolve_search_key(user_key: str) -> str:
    return user_key or common.TINYFISH_KEY or common.SERPER_KEY or common.TAVILY_KEY


def _search_provider(key: str) -> str:
    if key.startswith("tvly-"):
        return "tavily"
    if key.startswith("sk-tinyfish-"):
        return "tinyfish"
    return "serper"


async def _tinyfish_search(client: httpx.AsyncClient, query: str, key: str) -> dict:
    response = await client.get(
        common.TINYFISH_SEARCH_URL,
        params={"query": query},
        headers={"X-API-Key": key},
        timeout=common.SEARCH_TIMEOUT_S,
    )
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Search provider error ({response.status_code}).")
    data = response.json()
    results = [
        {
            "title": item.get("title", ""),
            "snippet": (item.get("snippet") or "")[:300],
            "url": item.get("url", ""),
        }
        for item in (data.get("results") or [])[: common.MAX_RESULTS]
    ]
    return {"query": query, "answer": None, "results": results}


async def _tavily_search(client: httpx.AsyncClient, query: str, key: str) -> dict:
    response = await client.post(
        common.TAVILY_URL,
        headers={"Authorization": f"Bearer {key}"},
        json={"query": query, "max_results": common.MAX_RESULTS, "include_answer": True},
        timeout=common.SEARCH_TIMEOUT_S,
    )
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Search provider error ({response.status_code}).")
    data = response.json()
    results = [
        {"title": item.get("title", ""), "snippet": (item.get("content") or "")[:300], "url": item.get("url", "")}
        for item in (data.get("results") or [])[: common.MAX_RESULTS]
    ]
    return {"query": query, "answer": data.get("answer") or None, "results": results}


async def _serper_search(client: httpx.AsyncClient, query: str, key: str) -> dict:
    response = await client.post(
        common.SERPER_URL,
        headers={"X-API-KEY": key},
        json={"q": query, "num": common.MAX_RESULTS},
        timeout=common.SEARCH_TIMEOUT_S,
    )
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Search provider error ({response.status_code}).")
    data = response.json()
    results = [
        {"title": item.get("title", ""), "snippet": item.get("snippet", ""), "url": item.get("link", "")}
        for item in (data.get("organic") or [])[: common.MAX_RESULTS]
    ]
    answer_box = data.get("answerBox") or {}
    answer = answer_box.get("answer") or answer_box.get("snippet")
    if not answer:
        answer = (data.get("knowledgeGraph") or {}).get("description")
    return {"query": query, "answer": answer, "results": results}


@router.post("/api/search")
async def search(req: SearchRequest) -> JSONResponse:
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Empty query.")
    key = _resolve_search_key(_clean_key(req.key))
    if not key:
        raise HTTPException(status_code=503, detail="Search is not configured.")
    provider = _search_provider(key)
    client = common._client()
    try:
        if provider == "tinyfish":
            payload = await _tinyfish_search(client, query, key)
        elif provider == "tavily":
            payload = await _tavily_search(client, query, key)
        else:
            payload = await _serper_search(client, query, key)
    except HTTPException:
        raise
    except httpx.RequestError as exc:
        logger.warning("Search provider unavailable: %r", exc)
        raise HTTPException(status_code=502, detail="Search provider unreachable.") from exc
    return JSONResponse(payload)
