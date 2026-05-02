"""
MHTML Web Saver - FastAPI Backend
Usage: python server.py
"""

import asyncio
import base64
import email.utils
import hashlib
import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="MHTML Web Saver")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Filename", "X-Resource-Count"],
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
MAX_RESOURCE_SIZE = 8 * 1024 * 1024
MAX_RESOURCES = 120
TIMEOUT = 20


class SaveRequest(BaseModel):
    url: str
    cookies: dict[str, str] = {}


class ProxyRequest(BaseModel):
    urls: list[str]
    referer: str
    cookies: dict[str, str] = {}


class ResourceItem(BaseModel):
    url: str
    content_type: str
    data_b64: str


class AssembleRequest(BaseModel):
    page_url: str
    html: str
    resources: list[ResourceItem]


def make_boundary():
    return "----=_NextPart_" + hashlib.md5(str(time.time()).encode()).hexdigest().upper()


def extract_resource_urls(html: str, base_url: str) -> list[str]:
    urls = set()
    img_attrs = ['src', 'data-src', 'data-lazy-src', 'data-original',
                 'data-img', 'data-lazy', 'data-thumb', 'data-real-src']

    for attr in img_attrs:
        for m in re.finditer(rf'''{attr}=["']([^"']+)["']''', html, re.I):
            u = m.group(1).strip()
            if not u.startswith('data:'):
                try:
                    urls.add(urljoin(base_url, u))
                except Exception:
                    pass

    for m in re.finditer(r'<link[^>]+href=["\']([^"\']+)["\']', html, re.I):
        if 'stylesheet' in m.group(0).lower():
            try:
                urls.add(urljoin(base_url, m.group(1).strip()))
            except Exception:
                pass

    for m in re.finditer(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.I):
        try:
            urls.add(urljoin(base_url, m.group(1).strip()))
        except Exception:
            pass

    for m in re.finditer(r'''url\(["']?([^"')]+)["']?\)''', html):
        u = m.group(1).strip()
        if not u.startswith('data:'):
            try:
                urls.add(urljoin(base_url, u))
            except Exception:
                pass

    return [u for u in urls if u.startswith('http')][:MAX_RESOURCES]


def build_client(cookies: dict) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": UA,
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        },
        cookies=cookies,
        follow_redirects=True,
        timeout=TIMEOUT,
    )


async def fetch_one(client: httpx.AsyncClient, url: str, referer: str) -> tuple | None:
    parsed_ref = urlparse(referer)
    parsed_url = urlparse(url)
    headers = {"Referer": referer}
    if parsed_ref.netloc != parsed_url.netloc:
        headers["Referer"] = f"{parsed_ref.scheme}://{parsed_ref.netloc}/"
        headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
    try:
        r = await client.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return None
        ct = r.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
        data = r.content
        if len(data) > MAX_RESOURCE_SIZE:
            return None
        return url, ct, data
    except Exception:
        return None


def build_mhtml(page_url: str, html: str, resources: list[tuple]) -> bytes:
    boundary = make_boundary()
    now = email.utils.formatdate(localtime=True)

    lines = [
        "From: <Saved by MHTML Web Saver>",
        f"Subject: {page_url}",
        f"Date: {now}",
        "MIME-Version: 1.0",
        f'Content-Type: multipart/related; type="text/html"; boundary="{boundary}"',
        f"X-Snapshot-URL: {page_url}",
        "",
    ]

    def add_part(ct: str, loc: str, data: bytes):
        lines.append(f"--{boundary}")
        lines.append(f"Content-Type: {ct}")
        lines.append("Content-Transfer-Encoding: base64")
        lines.append(f"Content-Location: {loc}")
        lines.append("")
        enc = base64.b64encode(data).decode("ascii")
        for i in range(0, len(enc), 76):
            lines.append(enc[i:i+76])
        lines.append("")

    add_part('text/html; charset="utf-8"', page_url, html.encode("utf-8"))
    for url, ct, data in resources:
        add_part(ct, url, data)
    lines.append(f"--{boundary}--")
    return "\r\n".join(lines).encode("utf-8")


def make_response(mhtml: bytes, page_url: str, count: int) -> Response:
    parsed = urlparse(page_url)
    hostname = parsed.hostname or "page"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{hostname}_{ts}.mhtml"
    return Response(
        content=mhtml,
        media_type="multipart/related",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Filename": filename,
            "X-Resource-Count": str(count),
        }
    )


@app.post("/save")
async def save_page(req: SaveRequest):
    """서버가 직접 fetch. 브라우저 쿠키를 전달받아 인증 통과."""
    url = req.url.strip()
    if not url.startswith("http"):
        url = "https://" + url

    async with build_client(req.cookies) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(502, detail=f"페이지 요청 실패: {e.response.status_code}")
        except Exception as e:
            raise HTTPException(502, detail=f"연결 실패: {str(e)}")

        final_url = str(resp.url)
        html = resp.text
        resource_urls = extract_resource_urls(html, final_url)
        tasks = [fetch_one(client, u, final_url) for u in resource_urls]
        results = await asyncio.gather(*tasks)
        resources = [r for r in results if r]

    mhtml = build_mhtml(final_url, html, resources)
    return make_response(mhtml, final_url, len(resources))


@app.post("/proxy")
async def proxy_resources(req: ProxyRequest):
    """
    브라우저가 CORS로 못 가져오는 리소스를 서버가 대신 fetch.
    브라우저 쿠키를 받아 인증 이미지도 처리.
    """
    async with build_client(req.cookies) as client:
        tasks = [fetch_one(client, u, req.referer) for u in req.urls]
        results = await asyncio.gather(*tasks)

    items = []
    for r in results:
        if r:
            url, ct, data = r
            items.append({
                "url": url,
                "content_type": ct,
                "data_b64": base64.b64encode(data).decode("ascii"),
            })
    return {"resources": items}


@app.post("/assemble")
async def assemble_mhtml(req: AssembleRequest):
    """HTML + 리소스를 받아 MHTML 조립."""
    resources = []
    for item in req.resources:
        try:
            data = base64.b64decode(item.data_b64)
            resources.append((item.url, item.content_type, data))
        except Exception:
            continue
    mhtml = build_mhtml(req.page_url, req.html, resources)
    return make_response(mhtml, req.page_url, len(resources))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def root():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    print("🚀 서버가 시작됩니다...")
    print("📍 http://localhost:8765 에서 접속하세요")
    print("⏹️  종료하려면 Ctrl+C를 누르세요\n")
    uvicorn.run(app, host="0.0.0.0", port=8765)
