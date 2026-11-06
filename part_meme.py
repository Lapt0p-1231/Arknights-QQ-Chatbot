import json
import random
import os


MEME_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memes_all.json")


def _load_memes():
    """从本地 JSON 文件加载所有烂梗"""
    try:
        with open(MEME_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ["data", "memes", "list", "items", "result"]:
                if key in data and isinstance(data[key], list):
                    return data[key]
        print("[part_meme] 数据格式无法识别，返回空列表")
        return []
    except FileNotFoundError:
        print(f"[part_meme] 数据文件不存在: {MEME_FILE}")
        return []
    except json.JSONDecodeError as e:
        print(f"[part_meme] JSON 解析失败: {e}")
        return []


_all_memes = _load_memes()
print(f"[系统] 已加载 {len(_all_memes)} 条烂梗")


_FALLBACK_MEMES = [
    "这世界就是一个巨大的草台班子。",
    "雪豹，闭嘴！",
    "无所谓，我会出手。",
    "坤音未来，启动！",
    "你的素养很差，我现在每天玩原神都能赚150原石。"
]


async def fetch_random_meme() -> str:
    """随机返回一条烂梗"""
    if not _all_memes:
        return random.choice(_FALLBACK_MEMES)


    item = random.choice(_all_memes)


    if isinstance(item, str):
        return item.strip()


    if isinstance(item, dict):

        for key in ["content", "text", "meme", "message", "data", "msg", "sentence", "value"]:
            if key in item:
                val = item[key]
                if isinstance(val, str):
                    return val.strip()
                if isinstance(val, dict):
                    for nested_key in ["content", "text", "meme"]:
                        if nested_key in val and isinstance(val[nested_key], str):
                            return val[nested_key].strip()


        all_strings = [v for v in item.values() if isinstance(v, str) and v.strip()]
        if all_strings:

            best_text = max(all_strings, key=len).strip()

            if len(best_text) > 2:
                return best_text


    print(f"[警告] 无法解析的数据结构: {item}")
    return random.choice(_FALLBACK_MEMES)


# 测试代码
if __name__ == "__main__":
    import asyncio

    meme = asyncio.run(fetch_random_meme())
    print(f"获取到的烂梗: {meme}")