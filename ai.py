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


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
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
    resp = await client().chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": raw_text},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    try:
        return json.loads(resp.choices[0].message.content)
    except (json.JSONDecodeError, IndexError):
        return {"issues": ["تعذر تحليل النص، الرجاء إعادة الصياغة بشكل أوضح."]}


async def generate_ad_copy(data: dict) -> str:
    resp = await client().chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": AD_COPY_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ],
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()
