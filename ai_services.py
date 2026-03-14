import logging
from openai import AsyncOpenAI
from config import GROQ_API_KEY, GROQ_BASE_URL

aclient = AsyncOpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL)

async def process_groq_transcription(file_path: str) -> str:
    """第一引擎：Whisper 极速语音转文字"""
    try:
        with open(file_path, "rb") as audio_file:
            transcript = await aclient.audio.transcriptions.create(
                model="whisper-large-v3",
                file=audio_file,
                response_format="text"
            )
        return transcript
    except Exception as e:
        return f"❌ Whisper 听写失败: {e}"

async def format_text_with_llm(original_text: str) -> str:
    """第二引擎：调用 Llama 大模型进行智能润色与排版"""
    if len(original_text.strip()) < 10 or "❌" in original_text:
        return original_text
        
    system_prompt = (
        "你是一个专业的文字编辑。请对下面的口语化语音转写稿进行整理："
        "1. 加上正确的标点符号进行断句，并进行合理的段落划分；"
        "2. 顺滑语句，去除无意义的口癖（如“啊”、“那个”、“呃”等），但【绝对不要修改原文的核心意思和句子结构】；"
        "3. 适当在段落间添加符合语境的 Emoji 表情符号，增加阅读趣味性。"
        "请直接输出整理好的文本，不要带有任何你的解释或废话前缀。"
    )
    
    try:
        response = await aclient.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": original_text}
            ],
            temperature=0.3
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"LLM 润色失败: {e}")
        return "⚠️ 【大模型排版超时或失败，仅为您保留原始文本】"