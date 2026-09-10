"""
ai.py — طبقة الذكاء الاصطناعي: تحويل نص التاجر الحر إلى بيانات منظمة + إعلان جاهز.
تُستخدم فقط في مرحلة إنشاء الحملة (البند 13 و17 في المخطط الأصلي):
  1) استخراج JSON منظم (فئة، مدينة، نسبة خصم، جمهور مستهدف...)
  2) توليد نص إعلاني جذاب من الفئات الأربع المطلوبة.
لا يقوم هذا الملف بأي نشر تلقائي — الحملة تبقى pending_review حتى تعتمدها الإدارة.
"""

import json
import os
from openai import AsyncOpenAI

_client: AsyncOpenAI | None = None


GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        # Groq يوفّر واجهة متوافقة مع OpenAI مجاناً بدون بطاقة دفع
        _client = AsyncOpenAI(
            api_key=os.environ["GROQ_API_KEY"],
            base_url=os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            timeout=float(os.environ.get("AI_TIMEOUT_SECONDS", "45")),
            max_retries=2,
        )
    return _client


EXTRACTION_SYSTEM_PROMPT = """
أنت مساعد يستخرج بيانات منظمة من وصف عرض تجاري مكتوب بالعامية أو الفصحى.
أعد **فقط** كائن JSON بالحقول التالية ولا شيء غيره:
{
  "business_type": "restaurant|shop|beauty|service|other",
  "category_code": "FOOD|SHOPPING|BEAUTY|SERVICES",
  "city_code": "KHARTOUM|OMDURMAN|BAHRI|UNKNOWN",
  "discount": <رقم أو null>,
  "target": "وصف قصير للجمهور المستهدف",
  "title": "عنوان قصير للحملة",
  "start": "YYYY-MM-DD أو null",
  "end": "YYYY-MM-DD أو null",
  "issues": ["أي معلومة ناقصة أو غير منطقية أو مخالفة"]
}
لا تكتب أي نص خارج كائن الـ JSON.
"""

AD_COPY_SYSTEM_PROMPT = """
أنت كاتب إعلانات تسويقية بالعربية لمنصة عروض محلية على Telegram.
اكتب إعلاناً قصيراً (3-5 أسطر) جذاباً بناءً على البيانات المعطاة،
مع إيموجي مناسب باعتدال، وادعُ القارئ في النهاية للحصول على كود الخصم عبر البوت.
لا تخترع تفاصيل غير موجودة في البيانات.
"""


async def extract_campaign_data(raw_text: str) -> dict:
    try:
        resp = await client().chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": raw_text},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or ""
        data = json.loads(content)
        return data if isinstance(data, dict) else {"issues": ["استجابة الذكاء الاصطناعي غير صالحة."]}
    except (json.JSONDecodeError, IndexError, TypeError, ValueError) as exc:
        return {"issues": ["تعذر تحليل النص، الرجاء إعادة الصياغة بشكل أوضح."], "error": str(exc)}
    except Exception:
        return {"issues": ["تعذر تحليل النص، الرجاء إعادة الصياغة بشكل أوضح."]}


async def generate_ad_copy(data: dict) -> str:
    try:
        resp = await client().chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": AD_COPY_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
            temperature=0.7,
        )
        content = (resp.choices[0].message.content or "").strip()
        return content or "تعذر إنشاء نص الإعلان. الرجاء المحاولة مرة أخرى."
    except Exception:
        return "تعذر إنشاء نص الإعلان مؤقتاً. الرجاء المحاولة مرة أخرى."


LISTING_REVIEW_PROMPT = """
أنت مراجع إعلانات عربية لسوق أفراد محلي. أعد JSON فقط:
{"title":"عنوان محسن أو نفس العنوان","description":"وصف محسن دون اختراع معلومات","issues":[]}
تحقق من وضوح الصنف والوصف والعنوان والسعر والتواصل والتوصيل. لا تحكم على السعر بأنه غالٍ أو رخيص.
إذا كانت البيانات واضحة اجعل issues قائمة فارغة. لا تضف روابط أو أرقامًا غير موجودة.
"""


async def review_listing(data: dict) -> dict:
    try:
        resp = await client().chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": LISTING_REVIEW_PROMPT},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        result = json.loads(resp.choices[0].message.content or "{}")
        return result if isinstance(result, dict) else {"issues": []}
    except Exception:
        # لا نمنع المستخدم من النشر عند تعذر خدمة الذكاء؛ الإدارة تراجع الإعلان.
        return {"issues": [], "ai_unavailable": True}


LISTING_PARSE_PROMPT = """
استخرج من رسالة بائع عربية بيانات إعلان فردي. أعد JSON فقط:
{"title":"","description":"","price":"","negotiable":true,"address":"","contact":"","delivery":"","condition":"جديد أو مستعمل","category":"","issues":[]}
اعتبر الحقول ناقصة إذا لم يذكرها البائع بوضوح. السعر يمكن أن يكون «عند التواصل»، والتفاوض يجب أن يكون نعم أو لا بوضوح.
لا تخترع أي معلومة. ضع الملاحظات الناقصة في issues.
"""


async def extract_listing_data(raw_text: str) -> dict:
    try:
        resp = await client().chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": LISTING_PARSE_PROMPT},
                {"role": "user", "content": raw_text},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        result = json.loads(resp.choices[0].message.content or "{}")
        return result if isinstance(result, dict) else {"issues": ["تعذر قراءة البيانات."]}
    except Exception:
        return {"issues": ["تعذر تحليل الرسالة. اكتب البيانات بوضوح في رسالة واحدة."]}


async def rank_listing_ids(query: str, listings: list[dict]) -> list[int]:
    if not listings:
        return []
    try:
        compact = [{"id": x["id"], "title": x["title"], "description": x.get("description", ""), "category": x.get("category", "")} for x in listings]
        resp = await client().chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "رتب الإعلانات حسب مطابقتها لبحث المستخدم. أعد JSON فقط بالشكل {\"ids\":[أرقام]}. لا تضف أرقامًا غير موجودة."},
                {"role": "user", "content": json.dumps({"query": query, "listings": compact}, ensure_ascii=False)},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        ids = json.loads(resp.choices[0].message.content or "{}").get("ids", [])
        valid = {x["id"] for x in listings}
        return [int(x) for x in ids if str(x).isdigit() and int(x) in valid]
    except Exception:
        words = set(query.lower().split())
        scored = sorted(listings, key=lambda x: len(words & set((x["title"] + " " + (x["description"] or "")).lower().split())), reverse=True)
        return [x["id"] for x in scored]
