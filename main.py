"""
CS 문의 자동화 웹훅 서버
──────────────────────────
POST /webhook  →  아임웹 Q&A 이벤트 수신
  1) 노션 CS 문의 관리 DB 저장
  2) Claude API 답변 초안 생성
  3) 슬랙 알림 전송
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta

import httpx
import anthropic
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional

# ──────────────────────────────────────────────
# 환경 변수
# ──────────────────────────────────────────────
from dotenv import load_dotenv

load_dotenv()

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DB_ID = os.getenv("NOTION_DB_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

# ──────────────────────────────────────────────
# 로깅
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# FastAPI 앱
# ──────────────────────────────────────────────
app = FastAPI(
    title="CS 문의 자동화 웹훅",
    version="1.0.0",
)

KST = timezone(timedelta(hours=9))

# ──────────────────────────────────────────────
# Pydantic 모델  — 아임웹 Q&A 웹훅 페이로드
# ──────────────────────────────────────────────
class QnAPayload(BaseModel):
    """아임웹에서 수신하는 Q&A 웹훅 페이로드"""
    qna_no: Optional[int] = Field(None, description="Q&A 번호")
    title: Optional[str] = Field("(제목 없음)", description="문의 제목")
    content: Optional[str] = Field("", description="문의 내용")
    writer_name: Optional[str] = Field("익명", description="작성자 닉네임")
    product_code: Optional[str] = Field("", description="상품 코드")
    category: Optional[str] = Field("기타", description="문의 유형")
    channel: Optional[str] = Field("아임웹Q&A", description="유입 채널")


# ──────────────────────────────────────────────
# 1) Claude AI 답변 초안 생성
# ──────────────────────────────────────────────
SYSTEM_PROMPT = """너는 트루스(TRUTHS) 브랜드의 CS 전문 상담사야.
고객 문의에 대해 정중하고 명확한 답변 초안을 작성해.

규칙:
- 존댓말 사용 (합쇼체)
- 200자 이내로 핵심만 간결하게
- 확인이 필요한 사항은 "확인 후 안내드리겠습니다"로 마무리
- 환불/교환은 정책 확인 필요 문구 포함
- 배송 문의는 "영업일 기준 2~3일 이내 출고" 안내
- 불확실한 정보는 추측하지 말고 담당자 확인 필요로 표시

답변 마지막에 신뢰도를 판단해서 아래 형식으로 추가해:
[신뢰도: 높음] — 정책/FAQ로 충분히 답변 가능
[신뢰도: 보통] — 대체로 맞지만 확인 필요
[신뢰도: 낮음] — 담당자 직접 확인 필수"""


async def generate_ai_draft(title: str, content: str, category: str) -> tuple[str, str]:
    """Claude API로 답변 초안 + 신뢰도 반환"""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        user_message = f"""[문의 유형] {category}
[문의 제목] {title}
[문의 내용]
{content}"""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        draft = response.content[0].text

        # 신뢰도 파싱
        confidence = "🟡보통"  # 기본값
        if "[신뢰도: 높음]" in draft:
            confidence = "🟢높음"
        elif "[신뢰도: 낮음]" in draft:
            confidence = "🔴낮음"
        elif "[신뢰도: 보통]" in draft:
            confidence = "🟡보통"

        # 답변 본문에서 신뢰도 태그 제거
        for tag in ["[신뢰도: 높음]", "[신뢰도: 보통]", "[신뢰도: 낮음]"]:
            draft = draft.replace(tag, "").strip()

        logger.info(f"AI 답변 생성 완료 (신뢰도: {confidence})")
        return draft, confidence

    except Exception as e:
        logger.error(f"Claude API 오류: {e}")
        return "(AI 답변 생성 실패 — 수동 작성 필요)", "🔴낮음"


# ──────────────────────────────────────────────
# 2) 노션 DB 저장
# ──────────────────────────────────────────────
async def save_to_notion(
    payload: QnAPayload,
    ai_draft: str,
    confidence: str,
) -> dict:
    """노션 CS 문의 관리 DB에 페이지 생성"""
    now_kst = datetime.now(KST).strftime("%Y-%m-%d")

    # 문의유형 매핑 (아임웹 → 노션 select)
    category_map = {
        "배송": "배송",
        "교환": "교환반품",
        "반품": "교환반품",
        "교환반품": "교환반품",
        "상품": "상품정보",
        "상품정보": "상품정보",
        "결제": "결제",
        "기타": "기타",
    }
    mapped_category = category_map.get(payload.category, "기타")

    # 채널 매핑
    channel_map = {
        "아임웹Q&A": "아임웹Q&A",
        "아임웹": "아임웹Q&A",
        "imweb": "아임웹Q&A",
        "무신사": "무신사",
        "musinsa": "무신사",
        "채널톡": "채널톡",
        "channel": "채널톡",
    }
    mapped_channel = channel_map.get(payload.channel, "아임웹Q&A")

    notion_payload = {
        "parent": {"database_id": NOTION_DB_ID},
        "properties": {
            "문의제목": {
                "title": [{"text": {"content": payload.title or "(제목 없음)"}}]
            },
            "qnaNo": {"number": payload.qna_no},
            "고객닉네임": {
                "rich_text": [{"text": {"content": payload.writer_name or "익명"}}]
            },
            "문의내용": {
                "rich_text": [{"text": {"content": (payload.content or "")[:2000]}}]
            },
            "상품코드": {
                "rich_text": [{"text": {"content": payload.product_code or ""}}]
            },
            "문의유형": {"select": {"name": mapped_category}},
            "채널": {"select": {"name": mapped_channel}},
            "AI답변초안": {
                "rich_text": [{"text": {"content": ai_draft[:2000]}}]
            },
            "신뢰도": {"select": {"name": confidence}},
            "담당자확인": {"checkbox": False},
            "최종발송답변": {"rich_text": [{"text": {"content": ""}}]},
            "상태": {"select": {"name": "대기"}},
            "등록일": {"date": {"start": now_kst}},
        },
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.notion.com/v1/pages",
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Content-Type": "application/json",
                "Notion-Version": "2022-06-28",
            },
            json=notion_payload,
        )

    if resp.status_code != 200:
        logger.error(f"노션 저장 실패: {resp.status_code} {resp.text}")
        raise HTTPException(status_code=502, detail="노션 저장 실패")

    data = resp.json()
    page_id = data["id"]
    notion_url = data["url"]
    logger.info(f"노션 저장 완료: {page_id}")
    return {"page_id": page_id, "url": notion_url}


# ──────────────────────────────────────────────
# 3) 슬랙 알림 전송
# ──────────────────────────────────────────────
async def send_slack_notification(
    payload: QnAPayload,
    ai_draft: str,
    confidence: str,
    notion_url: str,
):
    """슬랙 웹훅으로 알림 전송"""

    confidence_emoji = {
        "🟢높음": "🟢",
        "🟡보통": "🟡",
        "🔴낮음": "🔴",
    }.get(confidence, "⚪")

    slack_message = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "📩 새 CS 문의 접수",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*문의번호:*\n#{payload.qna_no or '-'}"},
                    {"type": "mrkdwn", "text": f"*채널:*\n{payload.channel}"},
                    {"type": "mrkdwn", "text": f"*고객:*\n{payload.writer_name}"},
                    {"type": "mrkdwn", "text": f"*유형:*\n{payload.category}"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*📋 문의 내용:*\n>{(payload.content or '')[:300]}",
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*🤖 AI 답변 초안:*  {confidence_emoji} {confidence}\n```{ai_draft[:500]}```",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "📝 노션에서 확인", "emoji": True},
                        "url": notion_url,
                        "style": "primary",
                    }
                ],
            },
        ],
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(SLACK_WEBHOOK_URL, json=slack_message)

    if resp.status_code != 200:
        logger.error(f"슬랙 전송 실패: {resp.status_code} {resp.text}")
    else:
        logger.info("슬랙 알림 전송 완료")


# ──────────────────────────────────────────────
# 엔드포인트
# ──────────────────────────────────────────────
@app.get("/")
async def health():
    return {"status": "ok", "service": "CS 문의 자동화 웹훅", "version": "1.0.0"}


@app.post("/webhook")
async def webhook(request: Request):
    """
    아임웹 Q&A 웹훅 수신 엔드포인트
    ─────────────────────────────────
    아임웹에서 전달되는 다양한 형태의 payload를 유연하게 처리
    """
    try:
        raw_body = await request.json()
    except Exception:
        raw_body = {}

    logger.info(f"웹훅 수신: {json.dumps(raw_body, ensure_ascii=False, default=str)[:500]}")

    # 아임웹 payload → QnAPayload 매핑
    # 아임웹 필드명이 다를 수 있으므로 유연하게 처리
    mapped = {
        "qna_no": raw_body.get("qna_no") or raw_body.get("no") or raw_body.get("qnaNo"),
        "title": raw_body.get("title") or raw_body.get("subject") or "(제목 없음)",
        "content": raw_body.get("content") or raw_body.get("body") or raw_body.get("question") or "",
        "writer_name": (
            raw_body.get("writer_name")
            or raw_body.get("nickname")
            or raw_body.get("member_name")
            or raw_body.get("name")
            or "익명"
        ),
        "product_code": (
            raw_body.get("product_code")
            or raw_body.get("prod_code")
            or raw_body.get("productCode")
            or ""
        ),
        "category": raw_body.get("category") or raw_body.get("type") or "기타",
        "channel": raw_body.get("channel") or "아임웹Q&A",
    }

    payload = QnAPayload(**mapped)

    # ① Claude AI 답변 초안 생성
    ai_draft, confidence = await generate_ai_draft(
        title=payload.title,
        content=payload.content,
        category=payload.category,
    )

    # ② 노션 DB 저장
    notion_result = await save_to_notion(payload, ai_draft, confidence)
    notion_url = notion_result["url"]

    # ③ 노션 페이지에 슬랙링크는 나중에 슬랙 메시지가 없으므로
    #    노션 URL 자체를 슬랙링크로 저장 (노션 페이지 링크)
    try:
        async with httpx.AsyncClient() as client:
            await client.patch(
                f"https://api.notion.com/v1/pages/{notion_result['page_id']}",
                headers={
                    "Authorization": f"Bearer {NOTION_TOKEN}",
                    "Content-Type": "application/json",
                    "Notion-Version": "2022-06-28",
                },
                json={
                    "properties": {
                        "슬랙링크": {"url": notion_url},
                    }
                },
            )
    except Exception as e:
        logger.warning(f"슬랙링크 업데이트 실패 (무시): {e}")

    # ④ 슬랙 알림 전송
    await send_slack_notification(payload, ai_draft, confidence, notion_url)

    return JSONResponse(
        status_code=200,
        content={
            "success": True,
            "message": "문의 접수 완료",
            "data": {
                "qna_no": payload.qna_no,
                "notion_page_id": notion_result["page_id"],
                "notion_url": notion_url,
                "ai_confidence": confidence,
            },
        },
    )


# ──────────────────────────────────────────────
# 로컬 실행
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
