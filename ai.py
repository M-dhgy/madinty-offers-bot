"""
ai.py — طبقة الذكاء الاصطناعي
v0.2:
  • إضافة match_city لمطابقة اسم مدينة كتبها المستخدم بالعربي.
  • باقي الدوال كما هي (استخراج حملة، توليد إعلان، مراجعة إعلان، تصنيف نتائج).
"""

import json
import os
from openai import AsyncOpenAI

_client: AsyncOpenAI | None = None


def _model() -> str:
    # تُقرأ عند الطلب — بعد تحميل .env في bot.py
    return os.environ.get("GROQ_MODEL") or "openai/gpt-oss-120b"


def _base_url() -> str:
    return os.environ.get("GROQ_BASE_URL") or "https://api.groq.com/openai/v1"


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=os.environ["GROQ_API_KEY"],
            base_url=_base_url(),
            timeout=float(os.environ.get("AI_TIMEOUT_SECONDS") or "45"),
            max_retries=2,
        )
    return _client


# ---------------------------------------------------------------
# City matching
# ---------------------------------------------------------------
async def match_city(user_text: str, cities: list[dict]) -> int | None:
    """يطابق اسم مدينة كتبه المستخدم مع قائمة المدن المتاحة. يعيد city_id أو None."""
    if not cities or not user_text:
        return None

    compact = [{"id": c["id"], "name": c["name"], "code": c["code"]} for c in cities]
    try:
        resp = await client().chat.completions.create(
            model=_model(),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "طابق نص المستخدم مع مدينة واحدة من القائمة. "
                        'أعد JSON فقط بالشكل {"id": رقم_المدينة أو null}. '
                        "لا تخترع أرقامًا غير موجودة. إذا لم تتأكد أعِد null."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"query": user_text, "cities": compact},
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        cid = data.get("id")
        if cid is None:
            return None
        cid_int = int(cid)
        return cid_int if cid_int in {c["id"] for c in cities} else None
    except Exception:
        # fallback: مطابقة نصية بسيطة
        text = user_text.strip().lower()
        for c in cities:
            name = c["name"].lower()
            code = c["code"].lower()
            if text in name or name in text or text in code:
                return c["id"]
        return None


# ---------------------------------------------------------------
# Campaign — extraction + ad copy
# ---------------------------------------------------------------
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
            model=_model(),
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
            model=_model(),
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


# ---------------------------------------------------------------
# Listing — review + extraction + ranking
# ---------------------------------------------------------------
LISTING_REVIEW_PROMPT = """
أنت مراجع إعلانات عربية لسوق أفراد محلي. أعد JSON فقط:
{"title":"عنوان محسن أو نفس العنوان","description":"وصف محسن دون اختراع معلومات","issues":[]}
تحقق إلزاميًا فقط من وجود اسم الصنف والعنوان أو مكان التواجد ورقم التواصل. الصورة يجري التحقق منها خارج المراجعة.
الوصف والسعر والتفاوض والحالة والتوصيل اختيارية، فلا تضعها في issues إذا غابت. لا تحكم على السعر بأنه غالٍ أو رخيص.
إذا كانت الحقول الإلزامية واضحة اجعل issues قائمة فارغة. لا تضف روابط أو أرقامًا غير موجودة.
"""


async def review_listing(data: dict) -> dict:
    try:
        resp = await client().chat.completions.create(
            model=_model(),
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
        return {"issues": [], "ai_unavailable": True}


LISTING_PARSE_PROMPT = """
استخرج من رسالة بائع عربية بيانات إعلان فردي. أعد JSON فقط:
{"title":"","description":"","price":"","negotiable":true,"address":"","contact":"","delivery":"","condition":"جديد أو مستعمل","category":"","issues":[]}
الحقول الإلزامية التي يجب فحصها فقط هي: اسم المنتج title، رقم التواصل contact، ومكان التواجد أو العنوان address.
الصورة إلزامية ويجري التحقق منها خارج هذا التحليل، فيجب عدم اعتبار غيابها من النص مشكلة.
كل الحقول الأخرى اختيارية: الوصف، السعر، التفاوض، الحالة، التصنيف، والتوصيل.
إذا غاب أحد الحقول الإلزامية الثلاثة من النص، اذكره بوضوح داخل issues باللغة العربية.
لا تخترع أي معلومة. اترك الحقول الاختيارية فارغة أو null إذا لم تذكر.
"""


async def extract_listing_data(raw_text: str) -> dict:
    try:
        resp = await client().chat.completions.create(
            model=_model(),
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
        compact = [
            {"id": x["id"], "title": x["title"],
             "description": x.get("description", ""),
             "category": x.get("category", "")}
            for x in listings
        ]
        resp = await client().chat.completions.create(
            model=_model(),
            messages=[
                {
                    "role": "system",
                    "content": (
                        'رتب الإعلانات حسب مطابقتها لبحث المستخدم. '
                        'أعد JSON فقط بالشكل {"ids":[أرقام]}. لا تضف أرقامًا غير موجودة.'
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"query": query, "listings": compact},
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        ids = json.loads(resp.choices[0].message.content or "{}").get("ids", [])
        valid = {x["id"] for x in listings}
        return [int(x) for x in ids if str(x).isdigit() and int(x) in valid]
    except Exception:
        words = set(query.lower().split())
        scored = sorted(
            listings,
            key=lambda x: len(words & set((x["title"] + " " + (x["description"] or "")).lower().split())),
            reverse=True,
        )
        return [x["id"] for x in scored]
